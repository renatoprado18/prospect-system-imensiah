"""F2: Engagement-driven prospecting.

Captura quem comentou nos posts proprios (editorial_posts published) nos
ultimos N dias, cruza com contacts, cria tarefas warm pra contatos existentes
e contatos+dossier pra leads novos.

LinkdAPI /posts/likes nao retorna dados no Hobby (testado 13/05) — v1 cobre
so commenters. Comments sao sinal mais forte de engajamento de qualquer forma.

Cron: GET /api/cron/linkedin-engagement-prospecting (08h BRT diario).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from database import get_db
from services.linkedin_outbound_monitor import _resolve_user_urn, _fetch_comments_page

logger = logging.getLogger(__name__)

LINKDAPI_BASE = "https://linkdapi.com"
DEFAULT_DAYS = 7
MAX_PAGES_PER_POST = 5  # ate ~50 comments por post (10/pagina)


# ============================================================================
# Helpers
# ============================================================================


def _extract_comment_urn(permalink: str) -> Optional[str]:
    """Extrai urn:li:comment:(...) do permalink LinkdAPI.

    Permalink vem URL-encoded com query params commentUrn= e replyUrn= — pegamos
    o replyUrn (se reply) ou commentUrn (top-level). Sem URN -> sem dedup.
    """
    if not permalink:
        return None
    try:
        qs = parse_qs(urlparse(permalink).query)
    except Exception:
        return None
    # replyUrn tem precedencia (mais especifico)
    for key in ("replyUrn", "commentUrn"):
        vals = qs.get(key) or []
        if vals:
            decoded = unquote(vals[0])
            if decoded.startswith("urn:li:comment:"):
                return decoded
    # fallback: regex no permalink decoded (caso shape mude)
    decoded_full = unquote(permalink)
    m = re.search(r"urn:li:comment:\([^)]+\)", decoded_full)
    return m.group(0) if m else None


def _normalize_linkedin_url(url: str) -> str:
    """Normaliza pra match: https://www.linkedin.com/in/handle/ -> linkedin.com/in/handle"""
    if not url:
        return ""
    u = url.lower().strip()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.rstrip("/")
    # remove query params
    u = u.split("?")[0].split("#")[0]
    return u


def _match_contact_by_linkedin(
    conn, profile_url: str, profile_urn: Optional[str]
) -> Tuple[Optional[int], Optional[str]]:
    """Tenta achar contact por URL do LinkedIn (normalizada)."""
    if not profile_url:
        return (None, None)

    norm = _normalize_linkedin_url(profile_url)
    if not norm:
        return (None, None)

    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM contacts
        WHERE linkedin IS NOT NULL
          AND REGEXP_REPLACE(REGEXP_REPLACE(LOWER(linkedin), '^https?://', ''), '^www\\.', '') LIKE %s
        LIMIT 1
        """,
        (f"%{norm}%",),
    )
    row = cur.fetchone()
    return (row["id"], "url_exact") if row else (None, None)


def _fetch_recent_published_posts(conn, days: int) -> List[Dict]:
    """Posts publicados nos ultimos N dias com URN preenchido."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, article_title, linkedin_activity_urn, linkedin_post_url, data_publicado
        FROM editorial_posts
        WHERE status = 'published'
          AND linkedin_activity_urn IS NOT NULL
          AND data_publicado >= NOW() - (%s || ' days')::interval
        ORDER BY data_publicado DESC
        """,
        (str(days),),
    )
    return [dict(r) for r in cur.fetchall()]


def _persist_signal(
    conn,
    post_id: int,
    comment: Dict,
    contact_id: Optional[int],
    contact_match_type: Optional[str],
    status: str,
) -> Optional[int]:
    """Insert signal idempotente. Retorna id ou None se ja existia."""
    author = comment.get("author") or {}
    comment_urn = _extract_comment_urn(comment.get("permalink") or "")

    # LinkdAPI retorna createdAt como Unix milliseconds (bigint) — converter
    created_raw = comment.get("createdAt")
    comment_at: Optional[datetime] = None
    if isinstance(created_raw, (int, float)) and created_raw > 0:
        try:
            comment_at = datetime.utcfromtimestamp(created_raw / 1000)
        except (OverflowError, OSError, ValueError):
            comment_at = None
    elif isinstance(created_raw, str) and created_raw:
        # fallback: alguns endpoints podem retornar ISO string
        try:
            comment_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            comment_at = None

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO linkedin_engagement_signals (
            post_id, action, comment_urn, profile_urn, profile_url,
            profile_name, profile_headline, comment_text, comment_at,
            contact_id, contact_match_type, status, processed_at
        ) VALUES (
            %s, 'comment', %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, NOW()
        )
        ON CONFLICT (post_id, comment_urn) WHERE comment_urn IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        (
            post_id,
            comment_urn,
            author.get("urn"),
            author.get("url"),
            author.get("name"),
            author.get("headline"),
            (comment.get("comment") or "")[:500],
            comment_at,
            contact_id,
            contact_match_type,
            status,
        ),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def _create_warm_task(
    conn, signal_id: int, contact_id: int, post_title: str, profile_name: str, post_url: str
) -> int:
    """Cria task de follow-up pra contato existente que engajou com post."""
    cur = conn.cursor()
    title = f"LinkedIn: {profile_name} comentou seu post — vale aproximar"
    desc = (
        f"{profile_name} comentou seu post \"{post_title[:80]}\". "
        f"Sinal de interesse — considere mensagem privada ou seguir-de-perto.\n\n"
        f"Post: {post_url or '(sem URL)'}"
    )
    cur.execute(
        """
        INSERT INTO tasks (titulo, descricao, contact_id, status, prioridade, contexto, origem, ai_generated)
        VALUES (%s, %s, %s, 'pending', 4, 'professional', 'engagement_prospecting', false)
        RETURNING id
        """,
        (title, desc, contact_id),
    )
    task_id = cur.fetchone()["id"]
    cur.execute(
        "UPDATE linkedin_engagement_signals SET task_id = %s WHERE id = %s",
        (task_id, signal_id),
    )
    return task_id


def _create_cold_lead(conn, signal_id: int, author: Dict) -> Optional[int]:
    """Cria contact stub pra commenter desconhecido. Retorna contact_id."""
    nome = (author.get("name") or "").strip()
    if not nome:
        return None

    linkedin_url = author.get("url") or ""
    headline = author.get("headline") or ""

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO contacts (
            nome, linkedin, linkedin_headline,
            origem, circulo, criado_em, atualizado_em
        ) VALUES (%s, %s, %s, 'engagement_prospecting', 5, NOW(), NOW())
        RETURNING id
        """,
        (nome, linkedin_url, headline),
    )
    contact_id = cur.fetchone()["id"]
    cur.execute(
        """
        UPDATE linkedin_engagement_signals
        SET contact_id = %s, contact_match_type = 'created_cold_lead'
        WHERE id = %s
        """,
        (contact_id, signal_id),
    )
    return contact_id


# ============================================================================
# Main pipeline
# ============================================================================


async def run_engagement_prospecting(days: int = DEFAULT_DAYS) -> Dict[str, Any]:
    """Pipeline: posts publicados ultimos N dias -> /comments -> cross contacts -> tasks."""
    api_key = (os.getenv("LINKDAPI_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "error": "LINKDAPI_KEY ausente"}

    stats = {
        "posts_processed": 0,
        "comments_fetched": 0,
        "self_skipped": 0,
        "duplicates_skipped": 0,
        "warm_tasks_created": 0,
        "cold_leads_created": 0,
        "errors": 0,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        my_urn = await _resolve_user_urn(client, api_key)
        if not my_urn:
            return {"ok": False, "error": "Nao consegui resolver URN do Renato (LINKEDIN_USERNAME?)"}

        with get_db() as conn:
            posts = _fetch_recent_published_posts(conn, days)

        for post in posts:
            stats["posts_processed"] += 1
            post_id = post["id"]
            post_urn = post["linkedin_activity_urn"]
            post_title = post.get("article_title") or ""
            post_url = post.get("linkedin_post_url") or ""

            all_comments: List[Dict] = []
            start = 0
            for _ in range(MAX_PAGES_PER_POST):
                page, cursor = await _fetch_comments_page(client, api_key, post_urn, start)
                all_comments.extend(page)
                if not cursor or not page:
                    break
                start += len(page)

            stats["comments_fetched"] += len(all_comments)

            for comment in all_comments:
                author = comment.get("author") or {}
                # skip o proprio Renato
                if author.get("urn") == my_urn:
                    stats["self_skipped"] += 1
                    continue
                # skip se faltar dado minimo
                if not author.get("name") or (not author.get("url") and not author.get("urn")):
                    stats["errors"] += 1
                    continue

                try:
                    with get_db() as conn:
                        contact_id, match_type = _match_contact_by_linkedin(
                            conn, author.get("url"), author.get("urn")
                        )

                        if contact_id:
                            status = "warm_task_created"
                            signal_id = _persist_signal(
                                conn, post_id, comment, contact_id, match_type, status
                            )
                            if signal_id is None:
                                stats["duplicates_skipped"] += 1
                                continue
                            _create_warm_task(
                                conn, signal_id, contact_id, post_title,
                                author.get("name"), post_url,
                            )
                            stats["warm_tasks_created"] += 1
                            conn.commit()
                        else:
                            status = "cold_lead_created"
                            signal_id = _persist_signal(
                                conn, post_id, comment, None, None, status
                            )
                            if signal_id is None:
                                stats["duplicates_skipped"] += 1
                                continue
                            new_contact_id = _create_cold_lead(conn, signal_id, author)
                            if new_contact_id:
                                stats["cold_leads_created"] += 1
                            else:
                                stats["errors"] += 1
                            conn.commit()
                except Exception as e:
                    logger.warning(f"signal post={post_id} author={author.get('name')} falhou: {e}")
                    stats["errors"] += 1

    return {"ok": True, **stats, "ran_at": datetime.utcnow().isoformat()}
