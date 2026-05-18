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
# Urgency decision (v1 stub — D3 expande com 5 regras reais)
# ============================================================================


def is_urgent(payload: Dict, urgency_score: Optional[int], source: str, msg_type: Optional[str]) -> bool:
    """V1 stub: retorna True so se urgency_score >= 8 OR caller marcou.

    D3 vai expandir com 5 regras:
    - Reuniao <30min sem confirmacao
    - LinkedIn outbound: autor do post respondeu
    - Resposta de prospect em campanha (contact circulo <=3)
    - Alerta financeiro > limite
    - Erro de cron em prod
    """
    if payload.get("force_immediate"):
        return True
    if isinstance(urgency_score, int) and urgency_score >= 8:
        return True
    return False


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

    urgent = is_urgent(payload, urgency_score, source, msg_type)

    # Mode 'shadow' — manda sempre + grava pending pra auditoria
    if mode == MODE_SHADOW:
        ok = await _send_now(text)
        pid = _enqueue_pending(source, payload, msg_type, urgency_score, digest_target, dedup_key)
        return {
            "action": "shadow",
            "pending_id": pid,
            "would_have": "sent_immediate" if urgent else "queued",
            "actually_sent": ok,
            "mode": mode,
        }

    # Mode 'on' — respeita urgencia
    if urgent:
        ok = await _send_now(text)
        return {"action": "sent" if ok else "skipped", "pending_id": None, "urgent": True, "mode": mode}

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
