"""
Notification Router (M2 — reduzir poluicao WA).

Centraliza decisao de "manda WA agora" vs "enfileira pra proximo digest".
Substitui chamadas diretas a EvolutionAPIClient.send_text() em codigo que
notifica o Renato (NAO inclui mensagens pra contatos terceiros).

Modos (env NOTIFICATION_DIGEST_MODE):
- 'off' (default): comportamento legado — sempre manda direto. Zero risco.
- 'shadow': manda direto + tambem insere pending pra Renato auditar o que
  SERIA silenciado. Sem perda. Usar 2-3 dias antes de flipar pra 'on'.
- 'on': respeita urgencia. Urgente -> direto, resto -> pending pra briefing.

Briefing/debriefing crons chamam consume_pending_for_digest() pra puxar a
fila e renderizar como secao no texto enviado ao Renato.

Pending >24h sem ter sido digerido: ganha expired_at (defensiva — nao apaga),
sai no proximo morning briefing com badge de atraso.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import get_db

logger = logging.getLogger(__name__)

# Numero WhatsApp do Renato (alvo das notificacoes do bot)
RENATO_PHONE_ENV = "WHATSAPP_OWNER_NUMBER"

# Modos validos
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"


def get_mode() -> str:
    raw = (os.getenv("NOTIFICATION_DIGEST_MODE") or MODE_OFF).strip().lower()
    if raw not in (MODE_OFF, MODE_SHADOW, MODE_ON):
        logger.warning(f"NOTIFICATION_DIGEST_MODE invalido: {raw} — usando 'off'")
        return MODE_OFF
    return raw


# ============================================================================
# Urgency decision — 5 regras v1 (calibrado com Renato 19/05/2026)
# ============================================================================


def _rule_meeting_soon_unconfirmed(payload: Dict) -> bool:
    """Reuniao em <30min sem confirmacao do convidado principal.

    Expecta payload com:
    - meeting_at (ISO string) OU minutes_until (int)
    - confirmed (bool, default False)
    """
    if payload.get("confirmed"):
        return False
    mins = payload.get("minutes_until")
    if not isinstance(mins, int) or mins is None:
        meeting_at = payload.get("meeting_at")
        if not meeting_at:
            return False
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(str(meeting_at).replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            mins = int((ts - now).total_seconds() / 60)
        except (ValueError, TypeError):
            return False
    return 0 < mins <= 30


def _rule_linkedin_author_replied(source: str, payload: Dict) -> bool:
    """Autor de post LinkedIn respondeu ao comentario outbound — sinal forte."""
    if source != "linkedin_outbound":
        return False
    return bool(payload.get("reply_from_author"))


def _rule_prospect_campaign_reply(source: str, payload: Dict) -> bool:
    """Resposta de prospect em campanha ativa (contact circulo <= 3).

    Expecta payload com:
    - contact_id (obrigatorio)
    - is_campaign_reply OR source='campaign'/'message_classifier'+msg_type='reply'
    """
    if source not in ("campaign", "message_classifier", "campaign_executor", "action_proposal"):
        return False
    if not payload.get("is_campaign_reply") and source != "campaign_executor":
        return False
    contact_id = payload.get("contact_id")
    if not contact_id:
        return False
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT circulo FROM contacts WHERE id = %s", (contact_id,))
            row = cur.fetchone()
            if not row or row.get("circulo") is None:
                return False
            return int(row["circulo"]) <= 3
    except Exception as e:
        logger.warning(f"_rule_prospect_campaign_reply DB falhou: {e}")
        return False


def _rule_financial_alert(source: str, msg_type: Optional[str]) -> bool:
    """Alerta financeiro do cost_tracker — sempre urgente.

    Source 'cost_tracker' + msg_type 'budget_threshold_hit' ja vem com
    force_immediate=True do check_budget_threshold(). Esta regra e
    defesa adicional caso outro caller esqueca a flag.
    """
    return source == "cost_tracker" or (msg_type or "").startswith("budget_")


def _rule_cron_error_prod(source: str, payload: Dict) -> bool:
    """Erro de cron em prod — risco operacional alto."""
    if source != "cron_telemetry" and source != "cron_health":
        return False
    severity = (payload.get("severity") or "").lower()
    return severity in ("error", "critical", "failed")


URGENCY_RULES = [
    ("meeting_soon_unconfirmed", lambda src, mt, pl, sc: _rule_meeting_soon_unconfirmed(pl)),
    ("linkedin_author_replied",  lambda src, mt, pl, sc: _rule_linkedin_author_replied(src, pl)),
    ("prospect_campaign_reply",  lambda src, mt, pl, sc: _rule_prospect_campaign_reply(src, pl)),
    ("financial_alert",          lambda src, mt, pl, sc: _rule_financial_alert(src, mt)),
    ("cron_error_prod",          lambda src, mt, pl, sc: _rule_cron_error_prod(src, pl)),
]


def is_urgent(
    payload: Dict,
    urgency_score: Optional[int],
    source: str,
    msg_type: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Retorna (urgent, rule_matched) — passou em alguma das 5 regras OR override caller."""
    if payload.get("force_immediate"):
        return True, "caller_force_immediate"
    if isinstance(urgency_score, int) and urgency_score >= 8:
        return True, "score_ge_8"
    for rule_name, rule_fn in URGENCY_RULES:
        try:
            if rule_fn(source, msg_type, payload, urgency_score):
                return True, rule_name
        except Exception as e:
            logger.warning(f"Urgency rule '{rule_name}' raised: {e}")
    return False, None


# ============================================================================
# Persistencia + envio
# ============================================================================


def _enqueue_pending(
    source: str,
    payload: Dict[str, Any],
    msg_type: Optional[str],
    urgency_score: Optional[int],
    digest_target: str,
    dedup_key: Optional[str],
) -> Optional[int]:
    """INSERT em pending_notifications. Retorna id ou None se duplicado."""
    if digest_target not in ("morning", "evening", "either"):
        digest_target = "either"

    with get_db() as conn:
        cur = conn.cursor()
        if dedup_key:
            cur.execute(
                """
                INSERT INTO pending_notifications
                  (source, msg_type, payload, urgency_score, digest_target, dedup_key)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, dedup_key) WHERE dedup_key IS NOT NULL AND sent_at IS NULL
                DO NOTHING
                RETURNING id
                """,
                (source, msg_type, json.dumps(payload), urgency_score, digest_target, dedup_key),
            )
        else:
            cur.execute(
                """
                INSERT INTO pending_notifications
                  (source, msg_type, payload, urgency_score, digest_target)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (source, msg_type, json.dumps(payload), urgency_score, digest_target),
            )
        row = cur.fetchone()
        conn.commit()
        return row["id"] if row else None


async def _send_now(message: str) -> bool:
    """Envia direto via Evolution. Retorna sucesso."""
    phone = (os.getenv(RENATO_PHONE_ENV) or "").strip()
    if not phone:
        logger.error(f"router: {RENATO_PHONE_ENV} ausente — nao consigo enviar")
        return False
    try:
        from integrations.evolution_api import get_evolution_client
        client = get_evolution_client()
        await client.send_text(phone=phone, message=message)
        return True
    except Exception as e:
        logger.warning(f"router send_now falhou: {e}")
        return False


# ============================================================================
# API publica
# ============================================================================


async def route_to_renato(
    *,
    source: str,
    payload: Dict[str, Any],
    msg_type: Optional[str] = None,
    urgency_score: Optional[int] = None,
    digest_target: str = "either",
    dedup_key: Optional[str] = None,
    message_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Decide entre enviar imediato ou enfileirar.

    Args:
        source: 'agent_intent' | 'editorial_alert' | 'linkedin_outbound' | etc
        payload: dict serializavel — vai pra coluna jsonb do pending E pode
                 ser usado pra renderizar texto se message_text nao for dado.
        msg_type: subcategoria opcional (ex: 'reuniao_proxima')
        urgency_score: 0-10 (so referencia; is_urgent() decide final)
        digest_target: 'morning' | 'evening' | 'either'
        dedup_key: se passado, evita 2 pendings (source, dedup_key) abertos.
        message_text: texto pronto pra WhatsApp. Se omitido, usa payload['body'].

    Returns:
        {"action": "sent"|"queued"|"shadow"|"skipped"|"duplicate", "pending_id": int|None}
    """
    mode = get_mode()
    text = message_text or payload.get("body") or json.dumps(payload, ensure_ascii=False)

    # Mode 'off' — comportamento legado
    if mode == MODE_OFF:
        ok = await _send_now(text)
        return {"action": "sent" if ok else "skipped", "pending_id": None, "mode": mode}

    urgent, urgency_rule = is_urgent(payload, urgency_score, source, msg_type)

    # Mode 'shadow' — manda sempre + grava pending pra auditoria
    if mode == MODE_SHADOW:
        ok = await _send_now(text)
        pid = _enqueue_pending(source, payload, msg_type, urgency_score, digest_target, dedup_key)
        return {
            "action": "shadow",
            "pending_id": pid,
            "would_have": "sent_immediate" if urgent else "queued",
            "urgency_rule": urgency_rule,
            "actually_sent": ok,
            "mode": mode,
        }

    # Mode 'on' — respeita urgencia
    if urgent:
        ok = await _send_now(text)
        return {
            "action": "sent" if ok else "skipped",
            "pending_id": None,
            "urgent": True,
            "urgency_rule": urgency_rule,
            "mode": mode,
        }

    pid = _enqueue_pending(source, payload, msg_type, urgency_score, digest_target, dedup_key)
    return {
        "action": "queued" if pid else "duplicate",
        "pending_id": pid,
        "digest_target": digest_target,
        "mode": mode,
    }


def consume_pending_for_digest(
    digest_name: str,
    digest_id_label: str,
    include_expired: bool = True,
) -> List[Dict[str, Any]]:
    """Retorna pending items que devem entrar neste digest e os marca como sent.

    Args:
        digest_name: 'morning' ou 'evening'
        digest_id_label: ex 'morning_2026_05_19' — gravado em sent_in_digest
        include_expired: morning sempre inclui expired_at>24h tb (decisao Renato)

    Comportamento:
    - morning: pega digest_target IN ('morning', 'either') + expired (qualquer target >24h)
    - evening: pega digest_target IN ('evening', 'either')
    - Marca queued >24h como expired_at antes de pegar (pra morning vir com badge)
    """
    if digest_name not in ("morning", "evening"):
        raise ValueError(f"digest_name invalido: {digest_name}")

    with get_db() as conn:
        cur = conn.cursor()

        # 1. Marca pending >24h como expired (sem deletar — sai no morning seguinte)
        if digest_name == "morning":
            cur.execute(
                """
                UPDATE pending_notifications
                SET expired_at = NOW()
                WHERE sent_at IS NULL
                  AND expired_at IS NULL
                  AND queued_at < NOW() - INTERVAL '24 hours'
                """
            )

        # 2. Seleciona itens pro digest
        if digest_name == "morning":
            target_filter = "(digest_target IN ('morning', 'either') OR expired_at IS NOT NULL)"
        else:
            target_filter = "digest_target IN ('evening', 'either')"

        cur.execute(
            f"""
            SELECT id, source, msg_type, payload, urgency_score, digest_target,
                   queued_at, expired_at
            FROM pending_notifications
            WHERE sent_at IS NULL
              AND {target_filter}
            ORDER BY COALESCE(urgency_score, 0) DESC, queued_at ASC
            """
        )
        items = [dict(r) for r in cur.fetchall()]

        if not items:
            return []

        ids = [it["id"] for it in items]
        cur.execute(
            """
            UPDATE pending_notifications
            SET sent_at = NOW(), sent_in_digest = %s
            WHERE id = ANY(%s)
            """,
            (digest_id_label, ids),
        )
        conn.commit()

    # Hidrata payload (psycopg2 ja desserializa jsonb)
    for it in items:
        if isinstance(it.get("payload"), str):
            try:
                it["payload"] = json.loads(it["payload"])
            except Exception:
                pass
        for k in ("queued_at", "expired_at"):
            if it.get(k):
                it[k] = it[k].isoformat()

    return items


def get_pending_count() -> int:
    """Para a pill do dashboard."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM pending_notifications WHERE sent_at IS NULL")
        row = cur.fetchone()
        return row["c"] if row else 0
