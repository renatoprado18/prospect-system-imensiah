"""
Project News Watchers - Monitora noticias por projeto via RSS.

Fluxo:
1. Cada projeto pode ter N watchers (query + feed_url opcional).
2. check_watcher() busca RSS, dedup por sha256 da URL normalizada,
   salva hits novos e cria action_proposals (Propor, nao Auto).
3. check_all_active_watchers() itera ativos e agrega stats.

MVP: NAO ativa cron em prod automaticamente. Endpoint
/api/cron/run-project-news-watchers existe mas precisa ser adicionado
ao vercel.json ou disparado manual via X-API-Key.

Dedup robusto:
- URL normalizada: lowercase + remove query params utm_*, gclid, fbclid.
- sha256 hex no campo url_hash (UNIQUE).
- Limit per watcher: max 50 hits novos por execucao (evita flood na 1a rodada).

Dedup proposals: contact_id=NULL nao usa o dedup nativo de action_proposals
(que e por contato+tipo). Usamos url_hash UNIQUE pra evitar reprocessar.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from database import get_db
from services.hot_takes import fetch_rss_feed
from services.tz import now_utc

logger = logging.getLogger(__name__)

# Limite max de hits novos processados por watcher numa unica execucao.
# Protege contra "primeira rodada" floodando o Renato com 200+ propostas.
MAX_HITS_PER_RUN = 50

# Prefixos de query params de tracking que devem ser removidos antes do hash.
# Capturar via prefixo evita combinatorial bloat (utm_source, utm_medium, ...).
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = {"gclid", "fbclid", "mc_cid", "mc_eid", "msclkid", "yclid"}


def _normalize_url(url: str) -> str:
    """Normaliza URL pra dedup estavel.

    - lowercase scheme + host
    - remove query params utm_*, gclid, fbclid (tracking params variam por share)
    - mantem path e demais query params (relevantes pra identidade da pagina)
    - mantem fragment vazio (anchors normalmente sao share-side noise)
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        # Filtra params de tracking
        keep_params = []
        for key, val in parse_qsl(parsed.query, keep_blank_values=True):
            kl = key.lower()
            if kl in _TRACKING_EXACT:
                continue
            if any(kl.startswith(p) for p in _TRACKING_PREFIXES):
                continue
            keep_params.append((key, val))
        new_query = urlencode(keep_params, doseq=True)
        return urlunparse((scheme, netloc, parsed.path, parsed.params, new_query, ""))
    except Exception:
        # Fallback conservador — qualquer falha cai pro lowercase puro
        return url.strip().lower()


def _url_hash(url: str) -> str:
    """sha256 hex da URL normalizada."""
    normalized = _normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _default_feed_url(query: str) -> str:
    """Gera URL RSS Google News pra um query."""
    # Google News RSS aceita query simples; urlencode pra escapar espacos/acentos.
    from urllib.parse import quote_plus

    encoded = quote_plus(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def _parse_pub_date(pub_date_str: Optional[str]) -> Optional[datetime]:
    """Parseia RFC822 pubDate do RSS pra datetime naive UTC.

    Google News usa formato 'Sun, 28 Jun 2026 14:30:00 GMT'. Email.utils
    lida com varios formatos RFC822 sem dor.
    """
    if not pub_date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(pub_date_str)
        if dt is None:
            return None
        # Normaliza pra naive UTC (DB stores TIMESTAMP sem TZ, padrao do repo)
        if dt.tzinfo is not None:
            from datetime import timezone

            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        logger.debug(f"_parse_pub_date: falhou pra '{pub_date_str}'")
        return None


def _strip_html(text: str) -> str:
    """Remove tags HTML basicas. Description do Google News vem com <a>, <font>, etc."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def get_watcher(watcher_id: int) -> Optional[dict]:
    """Busca watcher por ID com nome do projeto."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT w.*, p.nome AS project_name
            FROM project_news_watchers w
            LEFT JOIN projects p ON p.id = w.project_id
            WHERE w.id = %s
            """,
            (watcher_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def list_active_watchers() -> list[dict]:
    """Lista watchers ativos com nome do projeto."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT w.*, p.nome AS project_name
            FROM project_news_watchers w
            LEFT JOIN projects p ON p.id = w.project_id
            WHERE w.active = TRUE
            ORDER BY w.last_check NULLS FIRST, w.id
            """
        )
        return [dict(r) for r in cursor.fetchall()]


def list_all_watchers() -> list[dict]:
    """Lista todos os watchers (ativos + pausados) pra UI admin."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT w.*, p.nome AS project_name,
                   (SELECT COUNT(*) FROM project_news_hits h WHERE h.watcher_id = w.id) AS total_hits
            FROM project_news_watchers w
            LEFT JOIN projects p ON p.id = w.project_id
            ORDER BY w.active DESC, w.id DESC
            """
        )
        return [dict(r) for r in cursor.fetchall()]


def list_recent_hits(limit: int = 20) -> list[dict]:
    """Lista N hits mais recentes pra UI admin."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT h.*, w.query, p.nome AS project_name, p.id AS project_id,
                   ap.status AS proposal_status
            FROM project_news_hits h
            JOIN project_news_watchers w ON w.id = h.watcher_id
            LEFT JOIN projects p ON p.id = w.project_id
            LEFT JOIN action_proposals ap ON ap.id = h.proposal_id
            ORDER BY h.hit_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]


def create_watcher(project_id: int, query: str, feed_url: Optional[str] = None, active: bool = True) -> dict:
    """Cria um novo watcher. Nao deduplica por (project_id, query) — caller que controle."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO project_news_watchers (project_id, query, feed_url, active)
            VALUES (%s, %s, %s, %s)
            RETURNING id, project_id, query, feed_url, active, last_check, created_at
            """,
            (project_id, query.strip(), (feed_url or "").strip() or None, active),
        )
        row = cursor.fetchone()
        conn.commit()
        return dict(row)


def update_watcher_active(watcher_id: int, active: bool) -> bool:
    """Liga/desliga watcher. Retorna True se atualizou."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE project_news_watchers SET active = %s WHERE id = %s",
            (active, watcher_id),
        )
        ok = cursor.rowcount > 0
        conn.commit()
        return ok


def delete_watcher(watcher_id: int) -> bool:
    """Remove watcher (cascata deleta hits)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM project_news_watchers WHERE id = %s", (watcher_id,))
        ok = cursor.rowcount > 0
        conn.commit()
        return ok


async def check_watcher(watcher_id: int) -> dict:
    """Roda um watcher: fetch RSS, dedup, salva hits novos, cria proposals.

    Returns:
        {
            'watcher_id': int,
            'project_name': str,
            'query': str,
            'fetched': int,    # items retornados pelo RSS
            'new_hits': int,   # items novos (nao deduplicados)
            'proposals_created': int,
            'error': Optional[str],
        }
    """
    stats = {
        "watcher_id": watcher_id,
        "project_name": None,
        "query": None,
        "fetched": 0,
        "new_hits": 0,
        "proposals_created": 0,
        "error": None,
    }

    watcher = get_watcher(watcher_id)
    if not watcher:
        stats["error"] = "watcher_not_found"
        return stats

    stats["project_name"] = watcher.get("project_name") or f"project#{watcher.get('project_id')}"
    stats["query"] = watcher.get("query")

    feed_url = watcher.get("feed_url") or _default_feed_url(watcher["query"])

    # Fetch RSS — fetch_rss_feed retorna [] em caso de erro, log ja la dentro.
    try:
        items = await fetch_rss_feed(feed_url)
    except Exception as e:
        logger.exception(f"check_watcher #{watcher_id}: falha ao buscar RSS")
        stats["error"] = f"rss_fetch_failed: {type(e).__name__}: {e}"
        return stats

    stats["fetched"] = len(items)

    # Processa items: dedup + cria proposal
    new_count = 0
    proposals_count = 0

    # Importa lazy pra evitar import circular
    from services.action_proposals import get_action_proposals

    proposals_service = get_action_proposals()

    for item in items:
        if new_count >= MAX_HITS_PER_RUN:
            logger.info(
                f"check_watcher #{watcher_id}: MAX_HITS_PER_RUN={MAX_HITS_PER_RUN} atingido, "
                f"resto sera capturado na proxima execucao"
            )
            break

        url = item.get("link") or ""
        title = item.get("title") or ""
        if not url or not title:
            continue

        url_h = _url_hash(url)

        # Check dedup
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM project_news_hits WHERE url_hash = %s",
                (url_h,),
            )
            if cursor.fetchone():
                continue

        # Novo hit — cria proposal primeiro pra associar
        published_at = _parse_pub_date(item.get("pub_date"))
        description = _strip_html(item.get("description") or "")[:500]
        source = item.get("source") or "Google News"

        proposal_title = f"Noticia: {stats['project_name']} — {title[:120]}"
        proposal_desc_parts = [f"Link: {url}"]
        if description:
            proposal_desc_parts.append(description)
        if published_at:
            proposal_desc_parts.append(f"Publicado: {published_at.isoformat()} UTC")
        proposal_desc = "\n\n".join(proposal_desc_parts)

        proposal_id = None
        try:
            proposal = proposals_service.create_proposal(
                {
                    "action_type": "news_alert",
                    "contact_id": None,
                    "message_id": None,
                    "conversation_id": None,
                    "title": proposal_title,
                    "description": proposal_desc,
                    "trigger_text": title,
                    "ai_reasoning": (
                        f"Watcher #{watcher_id} (query='{stats['query']}') capturou "
                        f"noticia nova do feed RSS."
                    ),
                    "confidence": 0.85,
                    "urgency": "low",
                    "action_params": {
                        "watcher_id": watcher_id,
                        "project_id": watcher.get("project_id"),
                        "url": url,
                        "source": source,
                    },
                    "options": [],
                }
            )
            if proposal:
                proposal_id = proposal.get("id")
                proposals_count += 1
        except Exception:
            logger.exception(
                f"check_watcher #{watcher_id}: falha ao criar proposal pra '{title[:80]}'"
            )

        # Salva hit (mesmo se proposal falhou — dedup futuro nao perde)
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO project_news_hits
                        (watcher_id, url_hash, title, url, published_at, source, proposal_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (url_hash) DO NOTHING
                    RETURNING id
                    """,
                    (watcher_id, url_h, title[:500], url, published_at, source, proposal_id),
                )
                inserted = cursor.fetchone()
                conn.commit()
                if inserted:
                    new_count += 1
        except Exception:
            logger.exception(
                f"check_watcher #{watcher_id}: falha ao salvar hit '{title[:80]}'"
            )

    stats["new_hits"] = new_count
    stats["proposals_created"] = proposals_count

    # Atualiza last_check
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE project_news_watchers SET last_check = %s WHERE id = %s",
                (now_utc().replace(tzinfo=None), watcher_id),
            )
            conn.commit()
    except Exception:
        logger.exception(f"check_watcher #{watcher_id}: falha ao atualizar last_check")

    logger.info(
        f"check_watcher #{watcher_id} ({stats['query']}): "
        f"fetched={stats['fetched']}, new={new_count}, proposals={proposals_count}"
    )
    return stats


async def check_all_active_watchers() -> dict:
    """Itera todos watchers ativos e roda check_watcher um a um.

    Sequencial (nao paralelo): RSS de cada watcher e tipicamente <2s e a
    quantidade de watchers e baixa (1-20). Paralelo nao agrega valor real
    e complica logging/diagnose.
    """
    watchers = list_active_watchers()
    result = {
        "watchers_checked": 0,
        "new_hits": 0,
        "proposals_created": 0,
        "errors": 0,
        "details": [],
    }

    for w in watchers:
        try:
            stats = await check_watcher(w["id"])
            result["watchers_checked"] += 1
            result["new_hits"] += stats["new_hits"]
            result["proposals_created"] += stats["proposals_created"]
            if stats.get("error"):
                result["errors"] += 1
            result["details"].append(stats)
        except Exception as e:
            logger.exception(f"check_all_active_watchers: watcher #{w['id']} crashou")
            result["errors"] += 1
            result["details"].append(
                {
                    "watcher_id": w["id"],
                    "query": w.get("query"),
                    "error": f"{type(e).__name__}: {e}",
                }
            )

    logger.info(
        f"check_all_active_watchers: {result['watchers_checked']} watchers, "
        f"{result['new_hits']} novos, {result['proposals_created']} proposals, "
        f"{result['errors']} erros"
    )
    return result
