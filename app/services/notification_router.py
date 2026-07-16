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
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)

# Numero WhatsApp do Renato (alvo das notificacoes do bot).
# Usa RENATO_PHONE (mesmo env/valor canonico de intel_bot.py e main.py), com
# fallback hardcoded — NUNCA falhar calado por env nao migrada (era
# WHATSAPP_OWNER_NUMBER, env-fantasma nunca provisionada → urgentes sumiam).
RENATO_PHONE_ENV = "RENATO_PHONE"
RENATO_PHONE_FALLBACK = "5511984153337"

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
# Multi-canal (F-B Frente 2) — kill-switch NOTIFICATION_MULTICHANNEL
# ============================================================================
# 'off' (default): comportamento legado byte-a-byte (WhatsApp/fila como hoje).
# 'on': roteia por canal DE VERDADE — urgencia>=8 -> WhatsApp, 5-7 -> Web Push,
#       <5 -> pill (fila pending + badge). Push sem subscriber valido cai em
#       pill (NUNCA vira WhatsApp — preserva "WhatsApp quieto").

MULTICHANNEL_OFF = "off"
MULTICHANNEL_ON = "on"


def get_multichannel_mode() -> str:
    raw = (os.getenv("NOTIFICATION_MULTICHANNEL") or MULTICHANNEL_OFF).strip().lower()
    return MULTICHANNEL_ON if raw == MULTICHANNEL_ON else MULTICHANNEL_OFF


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


# M7 escalation trigger: imprensa = sempre Renato + escalation automática.
# Domínios de veículos relevantes (BR + global financeiro) e regex de
# vocabulário jornalístico. Match em qualquer um → urgente, bypassa silence guards.
_PRESS_DOMAINS = {
    "folha.uol.com.br", "estadao.com.br", "exame.com", "valor.globo.com",
    "valoreconomico.com.br", "oglobo.com.br", "reuters.com", "bloomberg.com",
    "agenciabrasil.ebc.com.br", "infomoney.com.br", "brazil.journal.com",
    "neofeed.com.br", "pipelinevalor.globo.com",
}

import re as _re
_PRESS_REGEX = _re.compile(
    r"\b(jornalista|rep[oó]rter|pauta|mat[eé]ria|"
    r"declara[cç][aã]o [aà] imprensa|entrevista|"
    r"coment[aá]rio pra reportagem|fonte off|fonte on the record)\b",
    _re.IGNORECASE,
)


def _rule_press_detection(payload: Dict) -> bool:
    """M7: imprensa/jornalista no source ou texto -> urgente + escalation."""
    # 1. Email com remetente de veículo conhecido
    if (payload.get("source") or "").lower() == "email" or payload.get("email_from"):
        sender = (payload.get("email_from") or payload.get("from") or "").lower()
        if "@" in sender:
            domain = sender.split("@", 1)[1].strip(" >")
            for press_d in _PRESS_DOMAINS:
                if domain == press_d or domain.endswith("." + press_d):
                    return True
    # 2. Regex no corpo/texto do payload
    haystack_parts = []
    for k in ("body", "text", "conteudo", "subject", "title", "message"):
        v = payload.get(k)
        if isinstance(v, str):
            haystack_parts.append(v)
    haystack = " ".join(haystack_parts)
    if haystack and _PRESS_REGEX.search(haystack):
        return True
    return False


def _rule_frente_keyword_match(payload: Dict) -> bool:
    """Bloco 2.X — keyword de frente 1 ou 2 no payload -> urgent (assunto critico).

    Frentes 3/4/5 nao urgentes mas vao pro morning briefing (atributo
    payload['frente_match'] eh setado, consumido por consumers downstream).
    """
    try:
        from services.cos_keywords import is_frente_keyword
    except Exception:
        return False

    # Junta texto de todos os campos relevantes
    parts = []
    for k in ("body", "text", "conteudo", "subject", "title", "message"):
        v = payload.get(k)
        if isinstance(v, str):
            parts.append(v)
    haystack = " ".join(parts)
    if not haystack:
        return False

    frente = is_frente_keyword(haystack)
    if frente is None:
        return False

    # Anota a frente no payload pra debug/consumers downstream
    payload["frente_match"] = frente
    return frente in (1, 2)


URGENCY_RULES = [
    ("meeting_soon_unconfirmed", lambda src, mt, pl, sc: _rule_meeting_soon_unconfirmed(pl)),
    ("linkedin_author_replied",  lambda src, mt, pl, sc: _rule_linkedin_author_replied(src, pl)),
    ("prospect_campaign_reply",  lambda src, mt, pl, sc: _rule_prospect_campaign_reply(src, pl)),
    ("financial_alert",          lambda src, mt, pl, sc: _rule_financial_alert(src, mt)),
    ("cron_error_prod",          lambda src, mt, pl, sc: _rule_cron_error_prod(src, pl)),
    ("press_detection",          lambda src, mt, pl, sc: _rule_press_detection(pl)),
    ("frente_keyword_match",     lambda src, mt, pl, sc: _rule_frente_keyword_match(pl)),
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


# ============================================================================
# Domingo silence guard (Bloco 2 C2 — "domingo sagrado")
# ============================================================================

def _is_sunday_silence(urgent: bool, urgency_rule: Optional[str]) -> bool:
    """True se hoje e domingo (BRT) E o item nao e urgente.

    M7 triggers (press_detection, financial_alert, cron_error_prod) e
    qualquer urgent=True BYPASSAM essa regra. Demais (non-urgent) -> adia
    pra digest morning de segunda 8h.

    Override env: SUNDAY_SILENCE_OFF=1 desliga (debug)."""
    if (os.getenv("SUNDAY_SILENCE_OFF") or "").strip() == "1":
        return False
    if urgent:
        return False
    try:
        now_brt = to_brt(now_utc())
        return now_brt.weekday() == 6  # 6 = domingo
    except Exception as e:
        logger.warning(f"_is_sunday_silence falhou: {e}")
        return False


async def _send_now(message: str) -> bool:
    """Envia direto via Evolution. Retorna sucesso."""
    phone = (os.getenv(RENATO_PHONE_ENV) or RENATO_PHONE_FALLBACK).strip()
    if not phone:
        logger.error(f"router: {RENATO_PHONE_ENV} ausente e sem fallback — nao consigo enviar")
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
# Multi-canal (F-B Frente 2) — decisao de canal + push + log
# ============================================================================


def decide_channel(
    payload: Dict[str, Any],
    urgency_score: Optional[int],
    source: str,
    msg_type: Optional[str],
) -> tuple[str, str]:
    """Decide o canal alvo. Retorna (channel, rule).

    - urgente (is_urgent gate: force_immediate / score>=8 / URGENCY_RULES)
      -> ('whatsapp', <rule>)
    - senao 5<=score<=7 -> ('push', 'score_5_7')
    - senao -> ('pill', 'score_lt_5')
    """
    urgent, urgency_rule = is_urgent(payload, urgency_score, source, msg_type)
    if urgent:
        return "whatsapp", (urgency_rule or "urgent")
    if isinstance(urgency_score, int) and 5 <= urgency_score <= 7:
        return "push", "score_5_7"
    return "pill", "score_lt_5"


def _send_push(
    title: str,
    body: str,
    urgent: bool = False,
    tag: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> bool:
    """Envia Web Push via push_notifications. Retorna True se sent>0.

    is_configured()=False ou qualquer excecao -> False (caller cai em pill)."""
    try:
        from services.push_notifications import get_push_service
        svc = get_push_service()
        if not svc.is_configured():
            logger.info("router _send_push: push nao configurado — caller cai em pill")
            return False
        res = svc.send_notification(
            title=title,
            body=body,
            data=data or {},
            tag=tag,
            urgent=urgent,
        )
        return bool((res or {}).get("sent", 0) > 0)
    except Exception as e:
        logger.warning(f"router _send_push falhou: {e}")
        return False


def _log_channel_decision(
    source: str,
    msg_type: Optional[str],
    urgency_score: Optional[int],
    decided_channel: str,
    decision_rule: Optional[str],
    sent_ok: Optional[bool],
    multichannel_mode: str,
    dedup_key: Optional[str],
    payload_title: Optional[str],
) -> None:
    """Best-effort INSERT em channel_decisions. NUNCA quebra o envio."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO channel_decisions
                  (source, msg_type, urgency_score, decided_channel, decision_rule,
                   sent_ok, multichannel_mode, dedup_key, payload_title)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source,
                    msg_type,
                    urgency_score,
                    decided_channel,
                    decision_rule,
                    sent_ok,
                    multichannel_mode,
                    dedup_key,
                    (payload_title or "")[:500] if payload_title else None,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"router _log_channel_decision falhou (ignorado): {e}")


# ============================================================================
# API publica
# ============================================================================


async def _dispatch_multichannel(
    *,
    source: str,
    payload: Dict[str, Any],
    msg_type: Optional[str],
    urgency_score: Optional[int],
    digest_target: str,
    dedup_key: Optional[str],
    text: str,
    urgent: bool,
    urgency_rule: Optional[str],
) -> Dict[str, Any]:
    """Roteia por canal quando NOTIFICATION_MULTICHANNEL='on'.

    whatsapp -> _send_now  |  push -> _send_push (falha -> pill)  |  pill -> fila.
    Domingo (non-urgent) cai em pill/morning (preserva domingo sagrado).
    Sempre grava channel_decisions (best-effort).
    """
    channel, rule = decide_channel(payload, urgency_score, source, msg_type)
    title = payload.get("title") or source
    push_data = payload.get("data") if isinstance(payload.get("data"), dict) else None

    # Domingo silence: non-urgent (push/pill) adia pra morning como pill
    if channel != "whatsapp" and _is_sunday_silence(urgent, urgency_rule):
        logger.info(f"domingo silence (multichannel): {source} -> pill/morning")
        pid = _enqueue_pending(source, payload, msg_type, urgency_score, "morning", dedup_key)
        _log_channel_decision(
            source, msg_type, urgency_score, "pill", "sunday_silence",
            pid is not None, MULTICHANNEL_ON, dedup_key, title,
        )
        return {
            "action": "queued_sunday_silence" if pid else "duplicate",
            "pending_id": pid,
            "channel": "pill",
            "decision_rule": "sunday_silence",
            "mode": "multichannel",
        }

    # WhatsApp — urgente
    if channel == "whatsapp":
        ok = await _send_now(text)
        _log_channel_decision(
            source, msg_type, urgency_score, "whatsapp", rule,
            ok, MULTICHANNEL_ON, dedup_key, title,
        )
        if not ok:
            # WA falhou -> NAO descarta o urgente: cai em pill (ledger duravel +
            # badge + entra no proximo digest). Melhor chegar tarde que sumir.
            pid = _enqueue_pending(source, payload, msg_type, urgency_score, digest_target, dedup_key)
            _log_channel_decision(
                source, msg_type, urgency_score, "pill", "whatsapp_fail_fallback",
                pid is not None, MULTICHANNEL_ON, dedup_key, title,
            )
            logger.warning(f"router: WA falhou p/ urgente ({source}) -> fallback pill (pid={pid})")
            return {
                "action": "queued_wa_fail" if pid else "duplicate",
                "pending_id": pid,
                "channel": "pill",
                "decision_rule": "whatsapp_fail_fallback",
                "mode": "multichannel",
            }
        return {
            "action": "sent",
            "pending_id": None,
            "channel": "whatsapp",
            "decision_rule": rule,
            "mode": "multichannel",
        }

    # Web Push — medio (5-7). Enfileira SEMPRE em pending (dedup + ledger
    # duravel); o push e so o "toque" em cima. Se o dedup_key ja tem pending
    # aberto, NAO repete o toque. Push falho/sem subscriber -> o item ja esta
    # em pending (vira pill; nunca WhatsApp).
    if channel == "push":
        pid = _enqueue_pending(source, payload, msg_type, urgency_score, digest_target, dedup_key)
        if pid is None:
            # dedup: ja ha pending aberto (mesmo source+dedup_key) -> nao toca de novo
            _log_channel_decision(
                source, msg_type, urgency_score, "push", "dedup_skip",
                False, MULTICHANNEL_ON, dedup_key, title,
            )
            return {
                "action": "duplicate",
                "pending_id": None,
                "channel": "push",
                "decision_rule": "dedup_skip",
                "mode": "multichannel",
            }
        pushed = _send_push(title=title, body=text, urgent=False, tag=dedup_key, data=push_data)
        # pending permanece como ledger duravel (sent_at NULL): se o push for
        # perdido/expirado, o item ainda aparece no badge/digest — nunca some.
        _log_channel_decision(
            source, msg_type, urgency_score,
            "push" if pushed else "pill",
            rule if pushed else "push_fallback_pill",
            pushed, MULTICHANNEL_ON, dedup_key, title,
        )
        return {
            "action": "sent" if pushed else "queued",
            "pending_id": pid,
            "channel": "push" if pushed else "pill",
            "decision_rule": rule if pushed else "push_fallback_pill",
            "mode": "multichannel",
        }

    # Pill — informativo (<5)
    pid = _enqueue_pending(source, payload, msg_type, urgency_score, digest_target, dedup_key)
    _log_channel_decision(
        source, msg_type, urgency_score, "pill", rule,
        pid is not None, MULTICHANNEL_ON, dedup_key, title,
    )
    return {
        "action": "queued" if pid else "duplicate",
        "pending_id": pid,
        "channel": "pill",
        "decision_rule": rule,
        "mode": "multichannel",
    }


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

    urgent, urgency_rule = is_urgent(payload, urgency_score, source, msg_type)

    # ------------------------------------------------------------------
    # Multi-canal (F-B Frente 2) — kill-switch NOTIFICATION_MULTICHANNEL='on'
    # Roteia por canal DE VERDADE. Quando 'off', cai no fluxo legado abaixo
    # (byte-a-byte). Preserva digest/dedup/domingo.
    # ------------------------------------------------------------------
    if get_multichannel_mode() == MULTICHANNEL_ON:
        return await _dispatch_multichannel(
            source=source,
            payload=payload,
            msg_type=msg_type,
            urgency_score=urgency_score,
            digest_target=digest_target,
            dedup_key=dedup_key,
            text=text,
            urgent=urgent,
            urgency_rule=urgency_rule,
        )

    # Mode 'off' — comportamento legado (mas com silence guard pra domingo)
    if mode == MODE_OFF:
        if _is_sunday_silence(urgent, urgency_rule):
            logger.info(f"domingo silence: msg adiada pra segunda 8h (source={source})")
            pid = _enqueue_pending(source, payload, msg_type, urgency_score, "morning", dedup_key)
            return {
                "action": "queued_sunday_silence",
                "pending_id": pid,
                "digest_target": "morning",
                "mode": mode,
            }
        ok = await _send_now(text)
        return {"action": "sent" if ok else "skipped", "pending_id": None, "mode": mode}

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

    # Domingo silence (non-urgent) — força digest_target='morning' (segunda 8h)
    if _is_sunday_silence(urgent, urgency_rule):
        logger.info(f"domingo silence: msg adiada pra segunda 8h (source={source})")
        digest_target = "morning"

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
