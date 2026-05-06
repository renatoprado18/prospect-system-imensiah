"""Monitor diario de topicos LinkedIn (Fase 1 do funil de aquisicao).

Roda em cron diario (`/api/cron/linkedin-monitor-topics`, 09h UTC = 6h BRT).
Pra cada keyword da lista, chama LinkdAPI `/api/v1/search/posts` filtrando
posts da ultima semana ordenados por data; persiste em linkedin_topics
filtrando por reactions >= 50 (controle de ruido).

Idempotente: ON CONFLICT(post_urn) DO NOTHING — re-runs no mesmo dia nao
duplicam. Custo tipico: 1 call por keyword = 5 calls/dia = ~150/mes.

Endpoint LinkdAPI confirmado via probe (06/05/2026):
- `GET /api/v1/search/posts?keyword=X&datePosted=past-week&sortBy=date_posted`
- Auth: header `X-linkdapi-apikey`
- Response: data.posts[] com urn, postID, postURL, text, author{name,headline,urn},
  engagements{totalReactions,commentsCount}, postedAt{timestamp,fullDate}
- API NAO retorna author.followers no search/posts; campo fica NULL na nossa tabela.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from database import get_db

logger = logging.getLogger(__name__)

LINKDAPI_BASE = "https://linkdapi.com"

# Keywords default — sobrescrevivel via LINKDAPI_MONITOR_KEYWORDS (CSV).
DEFAULT_KEYWORDS = [
    "planejamento estrategico PME",
    "OKR pequena empresa",
    "AI para empresas medias",
    "transformacao digital PME",
    "execucao estrategica",
]

# Filtro de ruido: posts abaixo desse engajamento sao ignorados (followers
# nao vem no payload de search; ficamos so com reactions).
MIN_REACTIONS = int((os.getenv("LINKDAPI_MONITOR_MIN_REACTIONS") or "50").strip() or 50)


def _resolve_keywords(override: Optional[List[str]] = None) -> List[str]:
    if override:
        return [k.strip() for k in override if k and k.strip()]
    env = (os.getenv("LINKDAPI_MONITOR_KEYWORDS") or "").strip()
    if env:
        return [k.strip() for k in env.split(",") if k.strip()]
    return list(DEFAULT_KEYWORDS)


def _track_call(endpoint: str, status_code: int) -> None:
    """Lazy import + try/except — telemetria nao deve quebrar o monitor."""
    try:
        from services.linkedin_funnel import track_linkdapi_call
        track_linkdapi_call(endpoint, status_code)
    except Exception:
        logger.debug(f"_track_call({endpoint}) falhou — telemetria offline?")


def _parse_posted_at(posted_at: Dict) -> Optional[datetime]:
    """Extrai datetime de postedAt.timestamp (millis epoch)."""
    if not posted_at:
        return None
    ts = posted_at.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.utcfromtimestamp(int(ts) / 1000.0)
    except (ValueError, TypeError):
        return None


async def _search_posts(
    client: httpx.AsyncClient, api_key: str, keyword: str
) -> Optional[List[Dict]]:
    """Chama search/posts. Retorna lista de posts ou None em erro."""
    try:
        resp = await client.get(
            f"{LINKDAPI_BASE}/api/v1/search/posts",
            headers={"X-linkdapi-apikey": api_key},
            params={
                "keyword": keyword,
                "datePosted": "past-week",
                "sortBy": "date_posted",
            },
        )
        _track_call("search/posts", resp.status_code)
        if resp.status_code != 200:
            logger.warning(f"LinkdAPI search/posts '{keyword}' -> {resp.status_code}")
            return None
        return ((resp.json() or {}).get("data") or {}).get("posts") or []
    except Exception as e:
        logger.warning(f"LinkdAPI search/posts '{keyword}' falhou: {e}")
        _track_call("search/posts", 0)  # registra debit mesmo em erro
        return None


def _persist_post(cursor, keyword: str, post: Dict) -> bool:
    """Insere se passar filtro e nao for duplicata. Retorna True se novo row criado."""
    engagements = post.get("engagements") or {}
    reactions = int(engagements.get("totalReactions") or 0)
    comments = int(engagements.get("commentsCount") or 0)

    # Filtro: so persiste se >= MIN_REACTIONS (followers nao vem no payload).
    if reactions < MIN_REACTIONS:
        return False

    author = post.get("author") or {}
    posted_at = _parse_posted_at(post.get("postedAt") or {})

    cursor.execute(
        """
        INSERT INTO linkedin_topics (
            keyword, post_url, post_urn, autor_nome, autor_headline, autor_urn,
            autor_followers, texto, reactions, comments, posted_at, raw_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (post_urn) DO NOTHING
        RETURNING id
        """,
        (
            keyword,
            post.get("postURL"),
            post.get("urn"),
            author.get("name"),
            author.get("headline"),
            author.get("urn"),
            None,  # autor_followers nao vem no search/posts (fica NULL)
            post.get("text"),
            reactions,
            comments,
            posted_at,
            json.dumps(post),
        ),
    )
    return cursor.fetchone() is not None


async def monitor_keywords(keywords: Optional[List[str]] = None) -> Dict:
    """Varre keywords e persiste posts novos. Retorna resumo de execucao."""
    api_key = (os.getenv("LINKDAPI_KEY") or "").strip()
    if not api_key:
        logger.warning("monitor_keywords: LINKDAPI_KEY ausente — abortando")
        return {
            "keywords_processed": 0,
            "posts_descobertos_novos": 0,
            "posts_filtrados": 0,
            "calls_feitas": 0,
            "errors": ["LINKDAPI_KEY ausente"],
        }

    kws = _resolve_keywords(keywords)
    novos = 0
    filtrados = 0
    calls = 0
    errors: List[str] = []

    timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for kw in kws:
            posts = await _search_posts(client, api_key, kw)
            calls += 1  # debita 1 call por keyword (mesmo em erro)
            if posts is None:
                errors.append(kw)
                continue

            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    for post in posts:
                        try:
                            if _persist_post(cursor, kw, post):
                                novos += 1
                            else:
                                filtrados += 1
                        except Exception as e:
                            logger.warning(f"monitor_keywords persist '{kw}' post={post.get('urn')}: {e}")
                    conn.commit()
            except Exception as e:
                logger.exception(f"monitor_keywords db falhou pra '{kw}': {e}")
                errors.append(f"{kw}: db error")

    return {
        "keywords_processed": len(kws),
        "posts_descobertos_novos": novos,
        "posts_filtrados": filtrados,
        "calls_feitas": calls,
        "errors": errors,
    }


def get_topics_last_24h(limit_per_keyword: int = 3) -> Dict[str, List[Dict]]:
    """Top N posts por keyword descobertos nas ultimas 24h, ordem reactions DESC.
    Usado pra alimentar a secao informativa do morning briefing."""
    out: Dict[str, List[Dict]] = {}
    with get_db() as conn:
        cursor = conn.cursor()
        # Lista keywords distintas com posts recentes
        cursor.execute(
            """
            SELECT DISTINCT keyword FROM linkedin_topics
            WHERE descoberto_em > NOW() - INTERVAL '24 hours'
            """
        )
        kws = [r["keyword"] for r in cursor.fetchall()]

        for kw in kws:
            cursor.execute(
                """
                SELECT id, keyword, post_url, autor_nome, autor_headline, texto,
                       reactions, comments, posted_at
                FROM linkedin_topics
                WHERE keyword = %s AND descoberto_em > NOW() - INTERVAL '24 hours'
                ORDER BY reactions DESC, comments DESC
                LIMIT %s
                """,
                (kw, limit_per_keyword),
            )
            rows = []
            for r in cursor.fetchall():
                row = dict(r)
                if row.get("posted_at"):
                    row["posted_at"] = row["posted_at"].isoformat()
                rows.append(row)
            if rows:
                out[kw] = rows
    return out


def format_topics_for_briefing(limit_per_keyword: int = 3) -> Optional[str]:
    """Formata seccao informativa pro morning briefing. Retorna None se vazio."""
    topics = get_topics_last_24h(limit_per_keyword=limit_per_keyword)
    if not topics:
        return None

    parts: List[str] = []
    for kw, posts in topics.items():
        parts.append(f"🔥 Esta bombando hoje em \"{kw}\":")
        for p in posts:
            autor = p.get("autor_nome") or "?"
            txt = (p.get("texto") or "").strip().replace("\n", " ")
            preview = txt[:80] + ("..." if len(txt) > 80 else "")
            r = p.get("reactions") or 0
            r_str = f"{r/1000:.1f}k" if r >= 1000 else str(r)
            parts.append(f"  • @{autor}: \"{preview}\" ({r_str} reacoes)")
        parts.append("")  # blank line entre keywords

    # Remove ultima blank line
    while parts and parts[-1] == "":
        parts.pop()

    return "\n".join(parts) if parts else None
