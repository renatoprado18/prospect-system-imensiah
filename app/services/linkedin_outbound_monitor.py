"""Monitor de engajamentos outbound LinkedIn (Fase 1.5 do funil de aquisicao).

O Renato comenta em posts de potenciais leads (CEOs/founders PME). O LinkedIn
nao notifica bem replies a comentarios em posts dos outros, entao perdemos o
melhor momento pra DM follow-up. Esse modulo registra os comments outbound,
checa periodicamente via LinkdAPI e dispara WhatsApp quando o autor do post
responde.

Endpoints LinkdAPI usados (confirmados via probe 06/05/2026):
- `GET /api/v1/posts/info?urn=URN` -> retorna post.author{name,headline,urn} + post.urn
  Usado em register_engagement pra resolver autor do post (1 call por register).
- `GET /api/v1/posts/comments?urn=URN&start=N` -> data.comments[] + data.cursor
  Usado em check_replies_for_engagement (1 call por check, ate paginar se preciso).
  Comments vem FLAT — replies tem permalink com `replyUrn=` E `commentUrn=` (parent).
  Top-level comments tem so `commentUrn=`.

Identificacao do comentario do Renato:
- author.urn == LINKEDIN_USER_URN (cache em env, fallback resolve via /profile/full)
- ou author.username == LINKEDIN_USERNAME (mais fragil, comments nao trazem username)

Ciclo de vida:
- register: insere row, primeiro check em 24h
- monitor_due: checa rows com next_check_at <= NOW(), reagenda +3d
- arquiva: rows sem reply apos 14d (commented_at < NOW() - 14d) sao marcadas archived_at
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

from database import get_db

logger = logging.getLogger(__name__)

LINKDAPI_BASE = "https://linkdapi.com"
COLD_THRESHOLD_DAYS = int((os.getenv("LINKEDIN_OUTBOUND_COLD_DAYS") or "14").strip() or 14)
RECHECK_INTERVAL_DAYS = int((os.getenv("LINKEDIN_OUTBOUND_RECHECK_DAYS") or "3").strip() or 3)
FIRST_CHECK_DELAY_HOURS = int((os.getenv("LINKEDIN_OUTBOUND_FIRST_CHECK_HOURS") or "24").strip() or 24)
# Self-healing: apos N falhas engagement-specific seguidas, row vai pra quarentine
# e sai do due-set ate triagem manual. Backoff entre tentativas vai escalando.
MAX_FAILURES_BEFORE_QUARANTINE = int((os.getenv("LINKEDIN_OUTBOUND_MAX_FAILURES") or "3").strip() or 3)
FAILURE_BACKOFF_HOURS = [1, 4]  # hours pra failure_count=1 e 2; >=3 -> quarantine


def _track_call(endpoint: str, status_code: int) -> None:
    """Telemetria nao deve quebrar o monitor."""
    try:
        from services.linkedin_funnel import track_linkdapi_call
        track_linkdapi_call(endpoint, status_code)
    except Exception:
        logger.debug(f"_track_call({endpoint}) falhou — telemetria offline?")


def _extract_post_urn(post_url: str) -> Optional[str]:
    """Extrai urn:li:activity:NNN de um post URL.
    Aceita formatos:
    - https://www.linkedin.com/feed/update/urn:li:activity:1234567/
    - https://www.linkedin.com/posts/USER_activity-1234567-abcd
    - https://www.linkedin.com/posts/USER_titulo-1234567-XyZ (sem palavra "activity"
      no slug — acontece quando o titulo nao contem "activity" nem outras chaves)
    """
    if not post_url:
        return None
    m = re.search(r"urn:li:activity:(\d+)", post_url)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    m = re.search(r"activity[-:](\d{15,})", post_url)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    m = re.search(r"-(\d{15,20})-[a-zA-Z0-9_]+/?(?:[?#]|$)", post_url)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    return None


def _parse_comment_urns(permalink: str) -> Tuple[Optional[str], Optional[str]]:
    """Extrai (commentUrn, replyUrn) do permalink de um comment.
    - Top-level comment: (commentUrn, None)
    - Reply: (parent_commentUrn, this_replyUrn)
    """
    if not permalink:
        return (None, None)
    decoded = urllib.parse.unquote(permalink)
    cu = re.search(r"commentUrn=([^&]+)", decoded)
    ru = re.search(r"replyUrn=([^&]+)", decoded)
    return (cu.group(1) if cu else None, ru.group(1) if ru else None)


async def _resolve_user_urn(client: httpx.AsyncClient, api_key: str) -> Optional[str]:
    """Pega o URN do dono via env ou via /profile/full (1 call). Cached em env
    em runtime via os.environ pra evitar fan-out em monitor_due_engagements."""
    cached = (os.getenv("LINKEDIN_USER_URN") or "").strip()
    if cached:
        return cached

    username = (os.getenv("LINKEDIN_USERNAME") or "").strip()
    if not username:
        logger.warning("_resolve_user_urn: LINKEDIN_USERNAME ausente")
        return None

    try:
        resp = await client.get(
            f"{LINKDAPI_BASE}/api/v1/profile/full",
            headers={"X-linkdapi-apikey": api_key},
            params={"username": username},
        )
        _track_call("profile/full", resp.status_code)
        if resp.status_code != 200:
            return None
        urn = ((resp.json() or {}).get("data") or {}).get("urn")
        if urn:
            os.environ["LINKEDIN_USER_URN"] = urn  # cache em runtime
        return urn
    except Exception as e:
        logger.warning(f"_resolve_user_urn falhou: {e}")
        _track_call("profile/full", 0)
        return None


async def _fetch_post_info(client: httpx.AsyncClient, api_key: str, post_urn: str) -> Optional[Dict]:
    """Pega detalhes do post (autor, urn). Retorna dict ou None em erro."""
    try:
        resp = await client.get(
            f"{LINKDAPI_BASE}/api/v1/posts/info",
            headers={"X-linkdapi-apikey": api_key},
            params={"urn": post_urn},
        )
        _track_call("posts/info", resp.status_code)
        if resp.status_code != 200:
            return None
        return ((resp.json() or {}).get("data") or {}).get("post")
    except Exception as e:
        logger.warning(f"_fetch_post_info({post_urn}) falhou: {e}")
        _track_call("posts/info", 0)
        return None


async def _fetch_comments_page(
    client: httpx.AsyncClient, api_key: str, post_urn: str, start: int = 0
) -> Tuple[List[Dict], Optional[str]]:
    """Retorna (comments, cursor). cursor=None quando nao ha mais paginas."""
    try:
        params: Dict = {"urn": post_urn}
        if start:
            params["start"] = start
        resp = await client.get(
            f"{LINKDAPI_BASE}/api/v1/posts/comments",
            headers={"X-linkdapi-apikey": api_key},
            params=params,
        )
        _track_call("posts/comments", resp.status_code)
        if resp.status_code != 200:
            return ([], None)
        data = (resp.json() or {}).get("data") or {}
        return (data.get("comments") or [], data.get("cursor"))
    except Exception as e:
        logger.warning(f"_fetch_comments_page({post_urn}) falhou: {e}")
        _track_call("posts/comments", 0)
        return ([], None)


def _find_my_comment(comments: List[Dict], my_urn: str) -> Optional[Dict]:
    """Localiza o comentario top-level do Renato (ignora replies).
    Retorna o comment dict ou None."""
    if not my_urn:
        return None
    for c in comments:
        author = c.get("author") or {}
        if author.get("urn") != my_urn:
            continue
        # Filtra replies — quero so o comment top-level que o Renato fez
        _, reply_urn = _parse_comment_urns(c.get("permalink") or "")
        if reply_urn:
            continue
        return c
    return None


def _count_replies_to_comment(
    comments: List[Dict], parent_comment_urn: str, post_author_urn: Optional[str]
) -> Tuple[int, bool]:
    """Conta replies a um comment especifico. Retorna (total, autor_do_post_respondeu)."""
    total = 0
    author_replied = False
    for c in comments:
        cu, ru = _parse_comment_urns(c.get("permalink") or "")
        if not ru:
            continue  # nao eh reply
        if cu != parent_comment_urn:
            continue  # reply pra outro comment
        total += 1
        if post_author_urn and (c.get("author") or {}).get("urn") == post_author_urn:
            author_replied = True
    return (total, author_replied)


async def register_engagement(post_url: str, comment_text: Optional[str] = None) -> Dict:
    """Registra um comment outbound. Idempotente por post_url (ON CONFLICT DO NOTHING).
    Resolve post_urn + autor via 1 call /posts/info.
    Primeiro check fica agendado em FIRST_CHECK_DELAY_HOURS (default 24h).
    """
    if not post_url or not post_url.strip():
        return {"ok": False, "error": "post_url vazio"}

    post_urn = _extract_post_urn(post_url)
    api_key = (os.getenv("LINKDAPI_KEY") or "").strip()

    post_author_urn = None
    post_author_name = None
    post_author_headline = None
    resolved_urn = post_urn

    if api_key and post_urn:
        timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            info = await _fetch_post_info(client, api_key, post_urn)
            if info:
                resolved_urn = info.get("urn") or post_urn
                author = info.get("author") or {}
                post_author_urn = author.get("urn")
                post_author_name = author.get("name")
                post_author_headline = author.get("headline")

    next_check_at = datetime.utcnow() + timedelta(hours=FIRST_CHECK_DELAY_HOURS)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO linkedin_outbound_engagements (
                post_url, post_urn, post_author_urn, post_author_name, post_author_headline,
                my_comment_text, next_check_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (post_url) DO NOTHING
            RETURNING id, commented_at, next_check_at
            """,
            (
                post_url.strip(),
                resolved_urn,
                post_author_urn,
                post_author_name,
                post_author_headline,
                (comment_text or "").strip() or None,
                next_check_at,
            ),
        )
        row = cursor.fetchone()
        conn.commit()

    if not row:
        # ja existia — devolve a row atual
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, post_url, post_author_name, next_check_at FROM linkedin_outbound_engagements WHERE post_url = %s",
                (post_url.strip(),),
            )
            existing = cursor.fetchone()
        if existing:
            return {
                "ok": True,
                "duplicate": True,
                "id": existing["id"],
                "post_url": existing["post_url"],
                "post_author_name": existing["post_author_name"],
                "scheduled_check_at": existing["next_check_at"].isoformat() if existing["next_check_at"] else None,
            }
        return {"ok": False, "error": "insert falhou e row nao encontrada"}

    return {
        "ok": True,
        "duplicate": False,
        "id": row["id"],
        "post_url": post_url.strip(),
        "post_urn": resolved_urn,
        "post_author_name": post_author_name,
        "scheduled_check_at": row["next_check_at"].isoformat() if row.get("next_check_at") else None,
    }


async def _send_reply_notification(eng: Dict) -> bool:
    """A7/F-A (porta-voz único, 12/07): emite SIGNAL urgencia=5 (o briefing da Tônia
    consome) sobre reply do autor do post, em vez de WA direto. Reativo mas não
    urgente (decisão Renato: 'não precisa urgent') — chega consolidado no briefing,
    não fica órfão como porta-voz WA solto."""
    try:
        from services.detectors._base import emit_signal, make_signal_hash
    except Exception as e:
        logger.warning(f"_send_reply_notification: import emit_signal falhou: {e}")
        return False

    author_name = eng.get("post_author_name") or "Autor do post"
    post_url = eng.get("post_url") or ""
    try:
        with get_db() as conn:
            emit_signal(
                conn,
                tipo="linkedin_author_reply",
                signal_hash=make_signal_hash("linkedin_author_reply", eng.get("id") or post_url),
                urgencia=5,
                contexto={
                    "engagement_id": eng.get("id"),
                    "author_name": author_name,
                    "post_url": post_url,
                    "hint": "autor respondeu seu comentario — considere DM follow-up (calor alto)",
                },
                detector="linkedin_outbound",
            )
            conn.commit()
        return True
    except Exception as e:
        logger.warning(f"_send_reply_notification: emit_signal falhou: {e}")
        return False


async def _send_quarantine_notification(engagement_id: int, post_url: str, error_msg: str) -> bool:
    """A7/F-A (porta-voz único): emite SIGNAL urgencia=4 (briefing) quando engagement
    vai pra quarentine apos N falhas — alerta técnico, não interrompe."""
    try:
        from services.detectors._base import emit_signal, make_signal_hash
    except Exception as e:
        logger.warning(f"_send_quarantine_notification: import emit_signal falhou: {e}")
        return False
    try:
        with get_db() as conn:
            emit_signal(
                conn,
                tipo="linkedin_engagement_quarantine",
                signal_hash=make_signal_hash("linkedin_engagement_quarantine", engagement_id),
                urgencia=4,
                contexto={
                    "engagement_id": engagement_id,
                    "post_url": post_url,
                    "error": error_msg,
                    "hint": f"saiu da fila apos {MAX_FAILURES_BEFORE_QUARANTINE} falhas — triagem manual",
                },
                detector="linkedin_outbound",
            )
            conn.commit()
        return True
    except Exception as e:
        logger.warning(f"_send_quarantine_notification: emit_signal falhou: {e}")
        return False


async def _record_failure(engagement_id: int, error_msg: str) -> Dict:
    """Registra falha engagement-specific. Incrementa failure_count, aplica backoff
    em next_check_at. Apos MAX_FAILURES_BEFORE_QUARANTINE, marca quarantined_at e
    notifica via WhatsApp. NAO chamado pra erros system-level (env config, etc)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT failure_count, post_url FROM linkedin_outbound_engagements WHERE id = %s",
            (engagement_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {"quarantined": False, "failure_count": 0}
        current = (row["failure_count"] or 0) + 1
        post_url = row["post_url"]

        if current >= MAX_FAILURES_BEFORE_QUARANTINE:
            cursor.execute(
                """
                UPDATE linkedin_outbound_engagements SET
                    failure_count = %s,
                    last_error = %s,
                    last_checked_at = NOW(),
                    quarantined_at = NOW()
                WHERE id = %s
                """,
                (current, (error_msg or "")[:500], engagement_id),
            )
            conn.commit()
            quarantined = True
        else:
            idx = min(current - 1, len(FAILURE_BACKOFF_HOURS) - 1)
            next_at = datetime.utcnow() + timedelta(hours=FAILURE_BACKOFF_HOURS[idx])
            cursor.execute(
                """
                UPDATE linkedin_outbound_engagements SET
                    failure_count = %s,
                    last_error = %s,
                    last_checked_at = NOW(),
                    next_check_at = %s
                WHERE id = %s
                """,
                (current, (error_msg or "")[:500], next_at, engagement_id),
            )
            conn.commit()
            quarantined = False

    if quarantined:
        await _send_quarantine_notification(engagement_id, post_url, error_msg)

    return {"quarantined": quarantined, "failure_count": current}


async def check_replies_for_engagement(engagement_id: int) -> Dict:
    """Checa replies de um engagement especifico. Pode disparar WhatsApp.
    Idempotente: notify_sent_at evita re-notify.

    Erros engagement-specific (post_urn nao resolvido, etc) incrementam
    failure_count e aplicam backoff. Erros system-level (env config) marcam
    `system_error=True` pra monitor abortar sem penalizar engagements."""
    api_key = (os.getenv("LINKDAPI_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "error": "LINKDAPI_KEY ausente", "system_error": True}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM linkedin_outbound_engagements WHERE id = %s",
            (engagement_id,),
        )
        eng = cursor.fetchone()

    if not eng:
        return {"ok": False, "error": f"engagement {engagement_id} nao encontrado", "system_error": True}

    eng = dict(eng)
    post_urn = eng.get("post_urn") or _extract_post_urn(eng.get("post_url") or "")
    if not post_urn:
        rec = await _record_failure(engagement_id, "post_urn nao resolvido")
        return {"ok": False, "error": "post_urn nao resolvido", **rec}

    timeout = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
    notified = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        my_urn = await _resolve_user_urn(client, api_key)
        if not my_urn:
            return {"ok": False, "error": "user URN nao resolvido", "system_error": True}

        # Busca primeira pagina; pagina mais se necessario (ate achar comment do Renato)
        all_comments: List[Dict] = []
        cursor_token: Optional[str] = None
        start = 0
        my_comment: Optional[Dict] = None
        for _ in range(5):  # cap em 5 paginas (~250 comments) pra controlar custo
            page, cursor_token = await _fetch_comments_page(client, api_key, post_urn, start=start)
            if not page:
                break
            all_comments.extend(page)
            my_comment = _find_my_comment(all_comments, my_urn)
            if my_comment or not cursor_token:
                break
            start += len(page)

        # Se nao achou autor do post no register, tenta resolver agora
        post_author_urn = eng.get("post_author_urn")
        if not post_author_urn:
            info = await _fetch_post_info(client, api_key, post_urn)
            if info:
                post_author_urn = (info.get("author") or {}).get("urn")
                if post_author_urn:
                    with get_db() as conn:
                        c2 = conn.cursor()
                        c2.execute(
                            "UPDATE linkedin_outbound_engagements SET post_author_urn = %s, post_author_name = %s, post_author_headline = %s WHERE id = %s",
                            (
                                post_author_urn,
                                (info.get("author") or {}).get("name"),
                                (info.get("author") or {}).get("headline"),
                                engagement_id,
                            ),
                        )
                        conn.commit()
                    eng["post_author_urn"] = post_author_urn
                    eng["post_author_name"] = (info.get("author") or {}).get("name")

    reply_count = 0
    reply_from_author = False
    my_comment_urn = eng.get("my_comment_urn")

    if my_comment:
        cu, _ = _parse_comment_urns(my_comment.get("permalink") or "")
        if cu:
            my_comment_urn = cu
            reply_count, reply_from_author = _count_replies_to_comment(
                all_comments, cu, post_author_urn
            )

    next_check_at = datetime.utcnow() + timedelta(days=RECHECK_INTERVAL_DAYS)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE linkedin_outbound_engagements SET
                post_urn = COALESCE(post_urn, %s),
                my_comment_urn = COALESCE(%s, my_comment_urn),
                has_reply = %s,
                reply_count = %s,
                reply_from_author = %s,
                last_checked_at = NOW(),
                next_check_at = %s,
                failure_count = 0,
                last_error = NULL
            WHERE id = %s
            """,
            (
                post_urn,
                my_comment_urn,
                reply_count > 0,
                reply_count,
                reply_from_author,
                next_check_at,
                engagement_id,
            ),
        )
        conn.commit()

    # Notifica se autor respondeu E nao notificamos antes
    if reply_from_author and not eng.get("notify_sent_at"):
        # refetch row pra payload de notificacao
        with get_db() as conn:
            c2 = conn.cursor()
            c2.execute(
                "SELECT post_author_name, post_url FROM linkedin_outbound_engagements WHERE id = %s",
                (engagement_id,),
            )
            fresh = dict(c2.fetchone() or {})
        notified = await _send_reply_notification(fresh)
        if notified:
            with get_db() as conn:
                c2 = conn.cursor()
                c2.execute(
                    "UPDATE linkedin_outbound_engagements SET notify_sent_at = NOW() WHERE id = %s",
                    (engagement_id,),
                )
                conn.commit()

    return {
        "ok": True,
        "id": engagement_id,
        "found_my_comment": bool(my_comment),
        "reply_count": reply_count,
        "reply_from_author": reply_from_author,
        "notified": notified,
    }


async def monitor_due_engagements() -> Dict:
    """Loop principal do cron. Checa todos engagements com next_check_at <= NOW(),
    arquiva os frios (>14d sem reply do autor)."""
    api_key = (os.getenv("LINKDAPI_KEY") or "").strip()
    if not api_key:
        logger.warning("monitor_due_engagements: LINKDAPI_KEY ausente — abortando")
        return {"checked": 0, "replies_detected": 0, "archived": 0, "errors": 1, "calls": 0,
                "error": "LINKDAPI_KEY ausente"}

    # 1) Arquiva frios (>14d sem reply do autor)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            UPDATE linkedin_outbound_engagements
            SET archived_at = NOW(), notes = COALESCE(notes, '') || ' [auto-archived: cold]'
            WHERE archived_at IS NULL
              AND reply_from_author = FALSE
              AND commented_at < NOW() - INTERVAL '{COLD_THRESHOLD_DAYS} days'
            RETURNING id
            """
        )
        archived = len(cursor.fetchall() or [])
        conn.commit()

    # 2) Pega rows due (exclui arquivados e quarantined)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM linkedin_outbound_engagements
            WHERE archived_at IS NULL AND quarantined_at IS NULL AND next_check_at <= NOW()
            ORDER BY next_check_at ASC
            """
        )
        due_ids = [r["id"] for r in cursor.fetchall()]

    checked = 0
    replies_detected = 0
    errors = 0
    quarantined = 0
    calls_before = _read_calls_count_today()
    system_error = None

    for eid in due_ids:
        try:
            res = await check_replies_for_engagement(eid)
            checked += 1
            if res.get("system_error"):
                # config/env issue — aborta loop sem penalizar outras rows
                errors += 1
                system_error = res.get("error")
                logger.warning(f"monitor_due_engagements: system_error em {eid}: {system_error}")
                break
            if res.get("reply_from_author"):
                replies_detected += 1
            if not res.get("ok"):
                errors += 1
                if res.get("quarantined"):
                    quarantined += 1
        except Exception as e:
            logger.exception(f"monitor_due_engagements: check {eid} falhou: {e}")
            errors += 1
            try:
                rec = await _record_failure(eid, f"exception: {type(e).__name__}: {str(e)[:200]}")
                if rec.get("quarantined"):
                    quarantined += 1
            except Exception:
                logger.exception(f"monitor_due_engagements: _record_failure({eid}) falhou")

    calls = max(0, _read_calls_count_today() - calls_before)

    summary = {
        "checked": checked,
        "replies_detected": replies_detected,
        "archived": archived,
        "errors": errors,
        "quarantined": quarantined,
        "calls": calls,
    }
    if system_error:
        summary["system_error"] = system_error
    return summary


def _read_calls_count_today() -> int:
    """Conta calls LinkdAPI hoje (best-effort, retorna 0 em erro)."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) AS n FROM linkdapi_usage
                WHERE credits_delta < 0 AND called_at::date = NOW()::date
                """
            )
            row = cursor.fetchone()
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0
