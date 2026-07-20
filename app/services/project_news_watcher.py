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
from services import llm
from services import llm_usage
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
ANTHROPIC_MODEL_HAIKU = llm.FAST
# Modo D: digest diario interativo usa Sonnet (mais qualidade no resumo).
# Custo aceitavel: ~5 watchers x ~300 tokens = ~1.5k tokens / dia.
ANTHROPIC_MODEL_SONNET = llm.BALANCED

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
    "digest_target": str,
}

_VALID_DELIVERY_MODES = {"silent", "critical_push", "digest_weekly", "digest_daily"}


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
        elif key in ("feed_url", "wa_target", "digest_target"):
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

        _llm_resp = resp.json()
        llm_usage.record_response("news_watcher.triage", ANTHROPIC_MODEL_HAIKU, _llm_resp)  # F-E: custo por-funcao
        text = _llm_resp["content"][0]["text"]
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

    # Modo D (digest_daily): NAO cria action_proposal. Hits ficam aguardando
    # o cron diario consolidar e mandar resumo via WA. Action proposals em modo
    # digest = ruido no dashboard (Renato pediu 28/06: "canal e WA, nao dashboard").
    skip_proposal = delivery_mode == "digest_daily"

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
        if not skip_proposal:
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


# =============================================================================
# MODO D — Digest diario interativo (28/06/2026)
# =============================================================================
# Fluxo:
#   1. cron 8h BRT (11h UTC) chama send_daily_digest()
#   2. build_daily_digest() pega hits ultimas 24h de TODOS watchers em
#      delivery_mode='digest_daily' com digest_id IS NULL
#   3. Pra cada watcher com hits, Claude Sonnet faz resumo 1-2 frases
#   4. Monta WA listavel (1. <projeto> — <summary>) e manda pro digest_target
#      (override) ou DEFAULT_WA_TARGET_RENATO
#   5. Salva news_digests row e marca hits.digest_id
#   6. Renato responde:
#      - "ok" -> archived_at = NOW() em todos hits do digest
#      - <num> ou <nome do projeto> -> manda WA com lista de titulos+URLs
#
# Anti-spam: 0 hits = sai sem WA. Anti-loop: hit em UM digest so (digest_id setado).
# Idempotencia: rodar 2x seguido = 2a chamada nao acha hits novos, sai.

# Janela maxima pra digest considerar "novos" hits (24h padrao + folga).
DIGEST_HITS_WINDOW_HOURS = 26

# Janela pra handle_digest_response procurar digest pendente ack.
DIGEST_ACK_WINDOW_HOURS = 48

# Tokens de resposta OK do Renato (lowercase + stripped).
_DIGEST_OK_TOKENS = {"ok", "ok!", "okay", "arquivar", "arquivar tudo", "arquivar td"}


async def _summarize_watcher_hits(project_name: str, hits: list[dict]) -> str:
    """Resume hits de UM watcher em 1-2 frases via Claude Sonnet.

    Args:
        project_name: nome do projeto pra contexto.
        hits: lista de dicts com 'title', 'source'.

    Returns:
        String 1-2 frases. Em caso de falha, fallback descritivo.
    """
    if not hits:
        return ""
    if not ANTHROPIC_API_KEY:
        # Fallback sem IA: lista primeiros 3 titulos truncados.
        titles = ", ".join((h.get("title") or "")[:50] for h in hits[:3])
        return f"{len(hits)} noticia(s): {titles}"

    items_text = "\n".join(
        f"{i+1}. {(h.get('title') or '').strip()} — {(h.get('source') or '').strip()}"
        for i, h in enumerate(hits[:20])  # cap em 20 pra controlar tokens
    )
    prompt = f"""Projeto: {project_name}
Noticias ultimas 24h ({len(hits)} hits):
{items_text}

Resuma em 1-2 frases o tema geral desses {len(hits)} hits. Tom factual, conciso, sem opiniao. Em portugues."""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": ANTHROPIC_MODEL_SONNET,
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            logger.warning(
                f"_summarize_watcher_hits: API {resp.status_code} body={resp.text[:200]}"
            )
            return f"{len(hits)} noticia(s) capturada(s) (resumo IA falhou)."
        _llm_resp = resp.json()
        llm_usage.record_response("news_watcher.digest", ANTHROPIC_MODEL_SONNET, _llm_resp)  # F-E: custo por-funcao
        text = _llm_resp["content"][0]["text"].strip()
        return text or f"{len(hits)} noticia(s) capturada(s)."
    except Exception as e:
        logger.warning(f"_summarize_watcher_hits falhou: {type(e).__name__}: {e}")
        return f"{len(hits)} noticia(s) capturada(s) (resumo IA indisponivel)."


def _list_digest_daily_watchers_with_pending_hits() -> list[dict]:
    """Retorna watchers em digest_daily com hits ultimos 24h sem digest_id.

    Cada dict tem: watcher_id, project_id, project_name, query, digest_target,
    wa_target, hits=[{id, title, url, source, hit_at}].
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT
                w.id AS watcher_id,
                w.project_id,
                w.query,
                w.digest_target,
                w.wa_target,
                p.nome AS project_name,
                h.id AS hit_id,
                h.title,
                h.url,
                h.source,
                h.hit_at
            FROM project_news_watchers w
            LEFT JOIN projects p ON p.id = w.project_id
            JOIN project_news_hits h ON h.watcher_id = w.id
            WHERE w.delivery_mode = 'digest_daily'
              AND w.active = TRUE
              AND h.digest_id IS NULL
              AND h.archived_at IS NULL
              AND h.hit_at >= NOW() - INTERVAL '{DIGEST_HITS_WINDOW_HOURS} hours'
            ORDER BY w.id, h.hit_at DESC
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]

    # Agrupa por watcher_id
    grouped: dict[int, dict] = {}
    for r in rows:
        wid = r["watcher_id"]
        if wid not in grouped:
            grouped[wid] = {
                "watcher_id": wid,
                "project_id": r["project_id"],
                "project_name": r["project_name"] or f"project#{r['project_id']}",
                "query": r["query"],
                "digest_target": r["digest_target"],
                "wa_target": r["wa_target"],
                "hits": [],
            }
        grouped[wid]["hits"].append({
            "id": r["hit_id"],
            "title": r["title"],
            "url": r["url"],
            "source": r["source"],
            "hit_at": r["hit_at"],
        })
    return list(grouped.values())


async def build_daily_digest() -> list[dict]:
    """Constroi payload do digest: pega hits novos + summarize por watcher.

    Returns:
        Lista de dicts:
        [{watcher_id, project_name, hits_ids: [..], hits_count, summary: "..."}]
        Vazia se 0 hits novos.
    """
    watchers = _list_digest_daily_watchers_with_pending_hits()
    if not watchers:
        return []

    digest_items = []
    for w in watchers:
        if not w["hits"]:
            continue
        summary = await _summarize_watcher_hits(w["project_name"], w["hits"])
        digest_items.append({
            "watcher_id": w["watcher_id"],
            "project_name": w["project_name"],
            "digest_target": w["digest_target"],
            "wa_target": w["wa_target"],
            "hits_ids": [h["id"] for h in w["hits"]],
            "hits_count": len(w["hits"]),
            "summary": summary,
        })
    return digest_items


def _format_digest_message(items: list[dict]) -> str:
    """Monta texto WA do digest a partir dos items.

    Formato:
        News digest — N noticias em K projetos

        1. <projeto> (<n> hits)
           <summary>

        2. ...

        Responda: "ok" pra arquivar tudo, ou nome/numero pra ver titulos.
    """
    total_hits = sum(item["hits_count"] for item in items)
    n_watchers = len(items)

    lines = [
        f"News digest — {total_hits} noticia(s) em {n_watchers} projeto(s)",
        "",
    ]
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item['project_name']} ({item['hits_count']} hit{'s' if item['hits_count'] != 1 else ''})")
        lines.append(f"   {item['summary']}")
        lines.append("")
    lines.append("Responda: \"ok\" pra arquivar tudo, ou nome/numero do projeto pra ver titulos.")
    return "\n".join(lines)


def _resolve_digest_target(items: list[dict]) -> str:
    """Escolhe WA target do digest. Se watchers tem digest_target heterogeneo,
    usa o do primeiro (caller pode segmentar se quiser ser fancy)."""
    for item in items:
        target = (item.get("digest_target") or "").strip()
        if target:
            return target
    return DEFAULT_WA_TARGET_RENATO


def _save_news_digest(
    wa_target: str,
    items: list[dict],
    message_text: str,
) -> Optional[int]:
    """Salva row em news_digests + marca digest_id nos hits. Retorna digest id."""
    from datetime import timedelta

    expires = (now_utc() + timedelta(hours=DIGEST_ACK_WINDOW_HOURS)).replace(tzinfo=None)
    total_hits = sum(item["hits_count"] for item in items)
    n_watchers = len(items)

    all_hit_ids: list[int] = []
    for item in items:
        all_hit_ids.extend(item["hits_ids"])

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO news_digests
                    (wa_target, watchers_count, hits_count, message_text, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (wa_target, n_watchers, total_hits, message_text, expires),
            )
            row = cursor.fetchone()
            digest_id = row["id"] if row else None
            if digest_id and all_hit_ids:
                cursor.execute(
                    "UPDATE project_news_hits SET digest_id = %s WHERE id = ANY(%s)",
                    (digest_id, all_hit_ids),
                )
            conn.commit()
            return digest_id
    except Exception:
        logger.exception("_save_news_digest: falha ao persistir digest")
        return None


def _update_digest_message_id(digest_id: int, message_id: Optional[str]) -> None:
    if not message_id:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE news_digests SET message_id_evolution = %s WHERE id = %s",
                (message_id, digest_id),
            )
            conn.commit()
    except Exception:
        logger.exception(f"_update_digest_message_id: falha pra digest {digest_id}")


async def send_daily_digest() -> dict:
    """Orquestra envio do digest diario.

    Returns:
        {
            'sent': bool,
            'digest_id': Optional[int],
            'watchers_count': int,
            'hits_count': int,
            'wa_target': str,
            'skipped_reason': Optional[str],
            'error': Optional[str],
        }
    """
    result = {
        "sent": False,
        "digest_id": None,
        "watchers_count": 0,
        "hits_count": 0,
        "wa_target": None,
        "skipped_reason": None,
        "error": None,
    }

    try:
        items = await build_daily_digest()
    except Exception as e:
        logger.exception("send_daily_digest: build_daily_digest crashou")
        result["error"] = f"build_failed: {type(e).__name__}: {e}"
        return result

    if not items:
        result["skipped_reason"] = "no_new_hits"
        logger.info("send_daily_digest: 0 hits novos em watchers digest_daily, skip WA")
        return result

    target = _resolve_digest_target(items)
    message_text = _format_digest_message(items)
    total_hits = sum(item["hits_count"] for item in items)
    n_watchers = len(items)

    result["wa_target"] = target
    result["watchers_count"] = n_watchers
    result["hits_count"] = total_hits

    # Persiste digest + marca hits ANTES de enviar (idempotencia: se Evolution
    # falhar, hits ja estao marcados, evita re-envio em retry imediato. Caller
    # pode ver ack_status='pending' sem message_id_evolution = enviou sem ack).
    digest_id = _save_news_digest(target, items, message_text)
    if not digest_id:
        result["error"] = "save_digest_failed"
        return result
    result["digest_id"] = digest_id

    # A3 (porta-voz único, F-A, 12/07): o digest NÃO é mais enviado como self-chat
    # WA (instância rap-whatsapp = Renato→Renato). O conteúdo agora chega pela
    # Tônia: o briefing das 7h lê os hits granulares via copilot.news_hits (A5,
    # live) e cruza notícia→projeto. O digest fica GRAVADO (histórico +
    # copilot.news_digests + hits marcados com digest_id = idempotência preservada),
    # só o ENVIO morre. Reativar = F-B (notification_router escolhe canal), não
    # aqui. Ver [[project_plano_tonia_copiloto_12_07]] F-A + [[project_dev_backlog]].
    result["skipped_reason"] = "self_chat_off_porta_voz_unico"
    logger.info(
        f"send_daily_digest: digest #{digest_id} gravado; envio self-chat "
        f"DESLIGADO (A3 porta-voz único) — {n_watchers} watchers/{total_hits} hits "
        f"chegam pela Tônia (briefing via copilot.news_hits)"
    )
    return result

    # Envia via Evolution  [A3: preservado, inalcançável — reativação via F-B]
    try:
        from integrations.whatsapp import WhatsAppIntegration
        wa = WhatsAppIntegration()
        response = await wa.send_text(target, message_text)
        if "error" in response:
            result["error"] = f"wa_send_failed: {response.get('error')}"
            logger.error(f"send_daily_digest: WA falhou {response}")
            return result
        # Evolution geralmente retorna {'key': {'id': '...'}} ou similar
        msg_id = None
        if isinstance(response, dict):
            key = response.get("key") or {}
            msg_id = key.get("id") or response.get("id") or response.get("messageId")
        _update_digest_message_id(digest_id, msg_id)
        result["sent"] = True
        logger.info(
            f"send_daily_digest: digest #{digest_id} enviado pra {target} "
            f"({n_watchers} watchers, {total_hits} hits)"
        )
        return result
    except Exception as e:
        logger.exception("send_daily_digest: erro no envio WA")
        result["error"] = f"wa_exception: {type(e).__name__}: {e}"
        return result


# ===== Webhook handler: resposta do Renato ao digest =====


def _normalize_phone(phone: str) -> str:
    """Remove tudo que nao for digito. Compara digest.wa_target vs incoming.

    DEFAULT_WA_TARGET_RENATO = "+5511984153337". Phone do webhook chega
    "5511984153337" (sem '+') ou "11984153337". Normalizar pra digitos compara
    com tail-match.
    """
    return re.sub(r"\D", "", phone or "")


def _get_pending_digest_for(phone: str) -> Optional[dict]:
    """Retorna digest mais recente pending nas ultimas 48h pra esse phone."""
    phone_digits = _normalize_phone(phone)
    if not phone_digits:
        return None
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT id, wa_target, watchers_count, hits_count, sent_at, message_text
                FROM news_digests
                WHERE ack_status = 'pending'
                  AND sent_at >= NOW() - INTERVAL '{DIGEST_ACK_WINDOW_HOURS} hours'
                ORDER BY sent_at DESC
                LIMIT 10
                """
            )
            rows = [dict(r) for r in cursor.fetchall()]
        # Match por tail: digest.wa_target tem '+' + DDI; phone do webhook pode
        # nao ter. Compara digito a digito por sufixo.
        for row in rows:
            target_digits = _normalize_phone(row["wa_target"])
            if not target_digits:
                continue
            # tail-match >= 10 digitos (DDD+numero)
            min_len = min(len(target_digits), len(phone_digits), 10)
            if phone_digits[-min_len:] == target_digits[-min_len:]:
                return row
        return None
    except Exception:
        logger.exception("_get_pending_digest_for: query falhou")
        return None


def _ack_digest_ok(digest_id: int) -> int:
    """Marca digest como acked_ok + archived_at em todos os hits. Retorna N hits archived."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE news_digests
                SET ack_status = 'acked_ok', acked_at = NOW()
                WHERE id = %s AND ack_status = 'pending'
                """,
                (digest_id,),
            )
            if cursor.rowcount == 0:
                logger.info(f"_ack_digest_ok: digest #{digest_id} nao estava pending")
                conn.commit()
                return 0
            cursor.execute(
                """
                UPDATE project_news_hits
                SET archived_at = NOW()
                WHERE digest_id = %s AND archived_at IS NULL
                """,
                (digest_id,),
            )
            n = cursor.rowcount or 0
            conn.commit()
            return n
    except Exception:
        logger.exception(f"_ack_digest_ok: falha digest #{digest_id}")
        return 0


def _ack_digest_drilled(digest_id: int) -> None:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE news_digests
                SET ack_status = 'drilled', acked_at = NOW()
                WHERE id = %s AND ack_status = 'pending'
                """,
                (digest_id,),
            )
            conn.commit()
    except Exception:
        logger.exception(f"_ack_digest_drilled: falha digest #{digest_id}")


def _list_digest_watchers_with_hits(digest_id: int) -> list[dict]:
    """Pra um digest, lista [(watcher_id, project_name, hits=[...])] ordenado."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    w.id AS watcher_id,
                    w.project_id,
                    p.nome AS project_name,
                    h.id AS hit_id,
                    h.title,
                    h.url,
                    h.source
                FROM project_news_hits h
                JOIN project_news_watchers w ON w.id = h.watcher_id
                LEFT JOIN projects p ON p.id = w.project_id
                WHERE h.digest_id = %s
                ORDER BY w.id, h.hit_at DESC
                """,
                (digest_id,),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        grouped: dict[int, dict] = {}
        order: list[int] = []
        for r in rows:
            wid = r["watcher_id"]
            if wid not in grouped:
                grouped[wid] = {
                    "watcher_id": wid,
                    "project_name": r["project_name"] or f"project#{r['project_id']}",
                    "hits": [],
                }
                order.append(wid)
            grouped[wid]["hits"].append({
                "id": r["hit_id"],
                "title": r["title"],
                "url": r["url"],
                "source": r["source"],
            })
        return [grouped[wid] for wid in order]
    except Exception:
        logger.exception(f"_list_digest_watchers_with_hits: falha digest #{digest_id}")
        return []


def _format_drill_message(watcher_block: dict) -> str:
    """Lista titulos+URLs do watcher escolhido."""
    name = watcher_block["project_name"]
    hits = watcher_block["hits"]
    lines = [f"{name} — {len(hits)} noticia(s):", ""]
    for i, h in enumerate(hits[:20], start=1):  # cap 20
        title = (h.get("title") or "").strip()
        url = (h.get("url") or "").strip()
        source = (h.get("source") or "").strip()
        lines.append(f"{i}. {title}")
        if source:
            lines.append(f"   Fonte: {source}")
        lines.append(f"   {url}")
        lines.append("")
    if len(hits) > 20:
        lines.append(f"... e mais {len(hits) - 20} (truncado).")
    return "\n".join(lines).rstrip()


async def _send_wa(target: str, message: str) -> bool:
    try:
        from integrations.whatsapp import WhatsAppIntegration
        wa = WhatsAppIntegration()
        response = await wa.send_text(target, message)
        return "error" not in response
    except Exception:
        logger.exception(f"_send_wa: falha pra {target}")
        return False


async def handle_digest_response(text: str, contact_phone: str) -> Optional[str]:
    """Processa resposta do Renato a um digest pendente.

    Args:
        text: conteudo da mensagem WA.
        contact_phone: phone do remetente (pode ter '+' ou nao).

    Returns:
        Texto da resposta enviada (handled), ou None se a msg nao bateu
        com nenhum digest pendente (caller continua processando).
    """
    if not text:
        return None
    text_clean = text.strip()
    if not text_clean:
        return None

    digest = _get_pending_digest_for(contact_phone)
    if not digest:
        return None

    digest_id = digest["id"]
    target = digest["wa_target"]

    # OK -> arquivar tudo
    if text_clean.lower() in _DIGEST_OK_TOKENS:
        archived = _ack_digest_ok(digest_id)
        reply = f"✅ {archived} noticia(s) arquivada(s) (digest #{digest_id})."
        await _send_wa(target, reply)
        logger.info(
            f"handle_digest_response: digest #{digest_id} acked_ok, "
            f"archived={archived}"
        )
        return reply

    # Tenta drill: numero (1, 2, ...) ou nome do projeto
    watcher_blocks = _list_digest_watchers_with_hits(digest_id)
    if not watcher_blocks:
        return None

    chosen: Optional[dict] = None

    # 1) Numero
    if text_clean.isdigit():
        idx = int(text_clean) - 1
        if 0 <= idx < len(watcher_blocks):
            chosen = watcher_blocks[idx]

    # 2) Match parcial case-insensitive em project_name
    if not chosen:
        needle = text_clean.lower()
        # Match exato primeiro
        for block in watcher_blocks:
            if block["project_name"].lower() == needle:
                chosen = block
                break
        # Substring match (so se nao foi exato)
        if not chosen:
            matches = [b for b in watcher_blocks if needle in b["project_name"].lower()]
            if len(matches) == 1:
                chosen = matches[0]
            # Mais de 1 = ambiguo, nao escolhe — deixa Renato re-tentar.

    if not chosen:
        return None

    drill_msg = _format_drill_message(chosen)
    await _send_wa(target, drill_msg)
    _ack_digest_drilled(digest_id)
    logger.info(
        f"handle_digest_response: digest #{digest_id} drilled em watcher "
        f"#{chosen['watcher_id']} ({chosen['project_name']})"
    )
    return drill_msg
