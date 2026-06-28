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
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from database import get_db
from services.hot_takes import fetch_rss_feed
from services.tz import now_utc

logger = logging.getLogger(__name__)

# Limite max de hits novos processados por watcher numa unica execucao.
# Protege contra "primeira rodada" floodando o Renato com 200+ propostas.
MAX_HITS_PER_RUN = 50

# Modo B: filtro IA + push WA critico.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Numero default pra push (Renato). Pode ser overridado por watcher.wa_target.
# Hardcoded pq ja foi acordado em memoria/codigo varias vezes.
DEFAULT_WA_TARGET_RENATO = "+5511984153337"

# Timeout maximo total pra scoring IA por execucao do check_watcher (segundos).
# Garante que 1 cron de 20 watchers nao trava por API lenta.
AI_SCORING_TIMEOUT_TOTAL = 300.0  # 5 min

# Fallback score quando Claude API falha. 0.5 = nem critical (< 0.7 default)
# nem zero (continua sendo proposta silenciosa via fluxo normal).
AI_FALLBACK_SCORE = 0.5

# Cache em memoria de scores por (project_id, url_hash) -> float.
# Vive enquanto o processo Python vive (Vercel: por invocacao; worker: ate
# restart). Cache duro fica no DB via project_news_hits.ai_relevance_score.
_score_cache: dict[tuple[int, str], float] = {}

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
    """Liga/desliga watcher. Retorna True se atualizou.

    Mantido pra compat com chamadas antigas. Pra updates Modo B, use
    update_watcher_fields().
    """
    return update_watcher_fields(watcher_id, {"active": active})


# Whitelist de campos editaveis via UI/API (anti-injection no UPDATE dinamico).
_EDITABLE_FIELDS = {
    "active": bool,
    "query": str,
    "feed_url": str,
    "delivery_mode": str,
    "criticality_threshold": float,
    "wa_target": str,
}

_VALID_DELIVERY_MODES = {"silent", "critical_push", "digest_weekly"}


def update_watcher_fields(watcher_id: int, fields: dict) -> bool:
    """Update parcial dos campos editaveis. Retorna True se atualizou.

    Valida tipos e valores conhecidos. Strings vazias em campos opcionais
    (feed_url, wa_target) viram NULL.
    """
    if not fields:
        return False

    set_clauses = []
    params = []
    for key, val in fields.items():
        if key not in _EDITABLE_FIELDS:
            continue
        # Validacao tipo-especifica
        if key == "delivery_mode":
            if val not in _VALID_DELIVERY_MODES:
                raise ValueError(f"delivery_mode invalido: {val}")
        elif key == "criticality_threshold":
            try:
                val = float(val)
            except (TypeError, ValueError) as e:
                raise ValueError(f"criticality_threshold deve ser float: {val}") from e
            if not (0.0 <= val <= 1.0):
                raise ValueError(f"criticality_threshold fora do range [0,1]: {val}")
        elif key in ("feed_url", "wa_target"):
            # Strings vazias -> NULL
            if val is not None:
                val = val.strip() or None
        elif key == "query":
            val = (val or "").strip()
            if not val:
                raise ValueError("query nao pode ser vazia")
        elif key == "active":
            val = bool(val)

        set_clauses.append(f"{key} = %s")
        params.append(val)

    if not set_clauses:
        return False

    params.append(watcher_id)
    sql = f"UPDATE project_news_watchers SET {', '.join(set_clauses)} WHERE id = %s"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
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


async def score_news_relevance(project: dict, news_item: dict) -> float:
    """Pontua relevancia de uma noticia pra um projeto via Claude Haiku.

    Args:
        project: dict com pelo menos 'nome' e idealmente 'descricao'.
        news_item: dict com 'title', 'description' (snippet), 'source'.

    Returns:
        float em [0.0, 1.0]. 0.0=irrelevante, 1.0=decisivo.
        Em caso de falha (API down, JSON invalido, key ausente) retorna
        AI_FALLBACK_SCORE=0.5 e loga warning.

    Custo aproximado: ~150 input tokens + ~10 output tokens por chamada.
    Claude Haiku 4.5: ~$0.80/M input + ~$4/M output. Por chamada ~$0.00016.
    50 hits/cron * ~$0.00016 = ~$0.008 por execucao. Diaria: irrelevante.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("score_news_relevance: ANTHROPIC_API_KEY ausente, fallback=0.5")
        return AI_FALLBACK_SCORE

    project_nome = (project.get("nome") or project.get("project_name") or "").strip()
    project_desc = (project.get("descricao") or "").strip()[:300]
    if not project_nome:
        logger.warning("score_news_relevance: projeto sem nome, fallback=0.5")
        return AI_FALLBACK_SCORE

    title = (news_item.get("title") or "").strip()
    snippet = _strip_html(news_item.get("description") or "")[:400]
    source = (news_item.get("source") or "").strip()
    if not title:
        return 0.0

    prompt = f"""Voce avalia o quao critica uma noticia eh pra um projeto especifico.

PROJETO: {project_nome}
{f'DESCRICAO PROJETO: {project_desc}' if project_desc else ''}

NOTICIA:
Titulo: {title}
{f'Fonte: {source}' if source else ''}
{f'Resumo: {snippet}' if snippet else ''}

Numa escala 0.0 (irrelevante pra esse projeto) a 1.0 (decisivo / acao urgente
necessaria), quao critica eh essa noticia?

Retorne APENAS JSON: {{"score": <float entre 0 e 1>}}"""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": ANTHROPIC_MODEL_HAIKU,
                    "max_tokens": 50,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            logger.warning(
                f"score_news_relevance: API {resp.status_code} body={resp.text[:200]}"
            )
            return AI_FALLBACK_SCORE

        text = resp.json()["content"][0]["text"]
        m = re.search(r'\{[\s\S]*?\}', text)
        if not m:
            logger.warning(f"score_news_relevance: sem JSON na resposta: {text[:100]}")
            return AI_FALLBACK_SCORE
        parsed = json.loads(m.group())
        score = float(parsed.get("score", AI_FALLBACK_SCORE))
        # Clampa pro range valido (Claude as vezes retorna 1.2 ou -0.1)
        score = max(0.0, min(1.0, score))
        return score
    except Exception as e:
        logger.warning(f"score_news_relevance falhou: {type(e).__name__}: {e}")
        return AI_FALLBACK_SCORE


async def _score_with_cache(project: dict, news_item: dict, url_hash: str) -> float:
    """Wrapper de score_news_relevance com cache em memoria + DB.

    Ordem de lookup:
    1. _score_cache (memoria) — barato, mesmo processo.
    2. project_news_hits.ai_relevance_score (DB) — sobrevive entre processos.
    3. Chamar Claude.
    """
    project_id = project.get("id") or project.get("project_id")
    if project_id is None:
        return await score_news_relevance(project, news_item)

    cache_key = (int(project_id), url_hash)
    if cache_key in _score_cache:
        return _score_cache[cache_key]

    # Lookup DB (caso o hit ja exista de execucao anterior)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ai_relevance_score FROM project_news_hits "
                "WHERE url_hash = %s AND ai_relevance_score IS NOT NULL",
                (url_hash,),
            )
            row = cursor.fetchone()
            if row and row["ai_relevance_score"] is not None:
                cached = float(row["ai_relevance_score"])
                _score_cache[cache_key] = cached
                return cached
    except Exception:
        logger.debug("_score_with_cache: lookup DB falhou, indo direto pra IA")

    score = await score_news_relevance(project, news_item)
    _score_cache[cache_key] = score
    return score


async def push_critical_to_wa(
    watcher: dict,
    hit: dict,
    score: float,
) -> dict:
    """Envia WA push pra Renato (ou wa_target custom) de um hit critico.

    Args:
        watcher: dict do watcher (precisa wa_target, project_name).
        hit: dict com title, url, source.
        score: float do scoring IA (entra na mensagem).

    Returns:
        {'sent': bool, 'response': ..., 'target': str, 'error': Optional[str]}
    """
    target = (watcher.get("wa_target") or "").strip() or DEFAULT_WA_TARGET_RENATO
    project_name = watcher.get("project_name") or f"project#{watcher.get('project_id')}"
    title = (hit.get("title") or "").strip()
    url = (hit.get("url") or "").strip()
    source = (hit.get("source") or "").strip()

    # Mensagem compacta. Renato pediu "so notifica se precisa acao manual",
    # entao formato direto: marker + projeto + titulo + URL.
    parts = [
        f"[News watch crítico — score {score:.2f}]",
        f"{project_name}: {title}",
    ]
    if source:
        parts.append(f"Fonte: {source}")
    parts.append(url)
    message = "\n".join(parts)

    try:
        from integrations.whatsapp import WhatsAppIntegration

        wa = WhatsAppIntegration()
        response = await wa.send_text(target, message)
        sent = "error" not in response
        return {
            "sent": sent,
            "target": target,
            "response": response,
            "error": response.get("error") if not sent else None,
        }
    except Exception as e:
        logger.exception(f"push_critical_to_wa: falha pra watcher {watcher.get('id')}")
        return {
            "sent": False,
            "target": target,
            "response": None,
            "error": f"{type(e).__name__}: {e}",
        }


def _mark_hit_pushed(hit_id: int, score: float) -> None:
    """Atualiza pushed_at + ai_relevance_score do hit. Idempotente."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE project_news_hits
                SET pushed_at = %s,
                    ai_relevance_score = COALESCE(ai_relevance_score, %s)
                WHERE id = %s
                """,
                (now_utc().replace(tzinfo=None), score, hit_id),
            )
            conn.commit()
    except Exception:
        logger.exception(f"_mark_hit_pushed: falha pra hit {hit_id}")


def _save_hit_score(hit_id: int, score: float) -> None:
    """Salva score IA no hit sem mexer em pushed_at."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE project_news_hits SET ai_relevance_score = %s WHERE id = %s",
                (score, hit_id),
            )
            conn.commit()
    except Exception:
        logger.exception(f"_save_hit_score: falha pra hit {hit_id}")


def _mark_watcher_pushed(watcher_id: int) -> None:
    """Atualiza last_push_at do watcher."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE project_news_watchers SET last_push_at = %s WHERE id = %s",
                (now_utc().replace(tzinfo=None), watcher_id),
            )
            conn.commit()
    except Exception:
        logger.exception(f"_mark_watcher_pushed: falha pra watcher {watcher_id}")


def _get_project_meta(project_id: Optional[int]) -> dict:
    """Busca nome + descricao do projeto pra prompt IA."""
    if not project_id:
        return {}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, nome, descricao FROM projects WHERE id = %s",
                (project_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else {}
    except Exception:
        return {"id": project_id}


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
        "ai_scored": 0,
        "pushes_sent": 0,
        "pushes_skipped_below_threshold": 0,
        "error": None,
    }

    watcher = get_watcher(watcher_id)
    if not watcher:
        stats["error"] = "watcher_not_found"
        return stats

    stats["project_name"] = watcher.get("project_name") or f"project#{watcher.get('project_id')}"
    stats["query"] = watcher.get("query")

    feed_url = watcher.get("feed_url") or _default_feed_url(watcher["query"])

    # Modo B: prepara metadata do projeto pra prompt IA (so se vai pontuar).
    delivery_mode = watcher.get("delivery_mode") or "silent"
    will_score = delivery_mode == "critical_push"
    project_meta = _get_project_meta(watcher.get("project_id")) if will_score else {}
    threshold = float(watcher.get("criticality_threshold") or 0.7)

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
        hit_id = None
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
                    hit_id = inserted["id"]
        except Exception:
            logger.exception(
                f"check_watcher #{watcher_id}: falha ao salvar hit '{title[:80]}'"
            )

        # Modo B — filtro IA + push critico (so pra hits novos salvos)
        if will_score and hit_id is not None:
            try:
                news_for_score = {
                    "title": title,
                    "description": description,
                    "source": source,
                }
                score = await _score_with_cache(project_meta, news_for_score, url_h)
                stats["ai_scored"] += 1
                _save_hit_score(hit_id, score)

                if score >= threshold:
                    push_result = await push_critical_to_wa(
                        watcher=watcher,
                        hit={
                            "title": title,
                            "url": url,
                            "source": source,
                        },
                        score=score,
                    )
                    if push_result.get("sent"):
                        _mark_hit_pushed(hit_id, score)
                        _mark_watcher_pushed(watcher_id)
                        stats["pushes_sent"] += 1
                        logger.info(
                            f"check_watcher #{watcher_id}: push WA enviado "
                            f"(score={score:.2f}, target={push_result.get('target')}) "
                            f"pra '{title[:60]}'"
                        )
                    else:
                        logger.warning(
                            f"check_watcher #{watcher_id}: push WA falhou "
                            f"err={push_result.get('error')}"
                        )
                else:
                    stats["pushes_skipped_below_threshold"] += 1
            except Exception:
                logger.exception(
                    f"check_watcher #{watcher_id}: scoring/push falhou pra hit {hit_id}"
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
        "ai_scored": 0,
        "pushes_sent": 0,
        "pushes_skipped_below_threshold": 0,
        "errors": 0,
        "details": [],
    }

    for w in watchers:
        try:
            stats = await check_watcher(w["id"])
            result["watchers_checked"] += 1
            result["new_hits"] += stats["new_hits"]
            result["proposals_created"] += stats["proposals_created"]
            result["ai_scored"] += stats.get("ai_scored", 0)
            result["pushes_sent"] += stats.get("pushes_sent", 0)
            result["pushes_skipped_below_threshold"] += stats.get(
                "pushes_skipped_below_threshold", 0
            )
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
        f"ai_scored={result['ai_scored']}, pushes={result['pushes_sent']}, "
        f"{result['errors']} erros"
    )
    return result
