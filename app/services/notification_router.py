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

Sobre o multi-canal (NOTIFICATION_MULTICHANNEL='on') existem ainda duas travas
no candidato a WhatsApp — teto diario de interrupcao (WA_DAILY_BUDGET_MODE) e
dedup cross-produtor (NOTIFICATION_CROSS_DEDUP). Nenhuma descarta nada: quem
nao passa DESCE de canal (push, e pill se push nao estiver disponivel) e o
rebaixamento fica gravado em channel_decisions. Ver o bloco de doc grande logo
abaixo de decide_channel().
"""
from __future__ import annotations

import json
import logging
import os
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import UTC, now_utc, to_brt, to_utc

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


def _sunday_silence_enqueue(
    urgent: bool,
    urgency_rule: Optional[str],
    source: str,
    payload: Dict[str, Any],
    msg_type: Optional[str],
    urgency_score: Optional[int],
    dedup_key: Optional[str],
) -> tuple[bool, Optional[int]]:
    """Ponto UNICO da politica 'domingo sagrado' (condicao + destino).

    Se domingo-silence aplica (non-urgent + domingo BRT + nao SUNDAY_SILENCE_OFF),
    enfileira o item pro digest de segunda 8h ('morning') e retorna (True, pid).
    Senao (False, None). Centraliza a decisao pra o fluxo legado (mode 'off') e o
    _dispatch_multichannel nao driftarem entre kill-switch on/off — cada branch so
    monta o return-shape proprio; o QUE decidir e PRA ONDE adiar mora aqui.

    (mode 'on' usa _is_sunday_silence direto: la o item cai no enqueue geral com
    digest_target='morning', nao num enqueue proprio — nao pode usar este helper
    sob risco de enfileirar 2x.)"""
    if not _is_sunday_silence(urgent, urgency_rule):
        return False, None
    pid = _enqueue_pending(source, payload, msg_type, urgency_score, "morning", dedup_key)
    return True, pid


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


# ============================================================================
# Orcamento diario de interrupcao + dedup cross-produtor (F-B Frente 3)
# ============================================================================
# PROBLEMA MEDIDO (14 dias de producao): 127 WhatsApps do sistema, 5 respostas
# (~1,6% de acao). channel_decisions mostra POR QUE a graduacao de canal nao
# acontece — a urgencia e AUTO-DECLARADA por quem emite e quase todo produtor
# declara 8: email_triage 17x, inbox_smart 9x, platform_costs 6x, inbox_triage
# 3x, TODOS com decision_rule='score_ge_8'. Um alerta de gasto de US$3,88 entra
# com a mesma urgencia que um e-mail do pai do Renato. O roteador executa
# corretamente uma priorizacao que ninguem faz.
#
# Duas travas no candidato a WhatsApp, cada uma com kill-switch proprio:
#
#   1. ORCAMENTO DIARIO (WA_DAILY_BUDGET_MODE / WA_DAILY_BUDGET)
#      Teto de N WhatsApps por dia BRT (default 3). Estourou -> o item NAO
#      some: DESCE de canal (push; push indisponivel -> pill/pending, que
#      entra no badge e no proximo digest). Fica gravado em channel_decisions
#      com decision_rule='budget_exhausted_downgraded'.
#
#   2. DEDUP CROSS-PRODUTOR (NOTIFICATION_CROSS_DEDUP)
#      Dois produtores DIFERENTES notificando a MESMA coisa em <6h: o segundo
#      nao vira WhatsApp (desce igual, rule='cross_producer_duplicate_
#      downgraded'). Caso real: o e-mail do pai ("Re: Regularizacao contabil
#      das minhas 7 empresas") chegou 2x em 26/07 e de novo em 27/07 — uma vez
#      como '📧 Email urgente (P9)' (source email_triage) e outra dentro da
#      '🤖 Triagem inteligente' (source inbox_smart). dedup_key existia mas era
#      POR PRODUTOR; falta uma chave comparavel ENTRE produtores.
#
# INVARIANTE: nada e descartado. A unica coisa que muda e o CANAL, e toda
# mudanca de canal deixa rastro em channel_decisions. Falha de DB em qualquer
# uma das travas => fail-open (manda o WhatsApp), nunca engole.
#
# NADA TERMINA EM PILL (auditoria 27/07). A escada de rebaixamento NAO pode
# acabar na fila pending_notifications: hoje ela e uma DEAD-LETTER. O unico
# dreno e consume_pending_for_digest(), chamado so em main.py:28123 (cron
# daily-morning-briefing) e main.py:28485 (daily-evening-debriefing), e os DOIS
# estao comentados no scheduler do worker (workers/audio-transcriber/main.py:
# 176-177, "briefings legados aposentados em 23/06/2026"). Nenhum outro
# scheduler bate nesses endpoints. Em producao a ultima linha drenada e de
# 13/07; itens enfileirados depois disso (task_reconciler, editorial_selection,
# auto_archive_gate) nunca sairam. Logo, "cair pro pill" == sumir em silencio,
# exatamente o que este trabalho existe pra evitar.
#
# Escada de um item REBAIXADO, portanto:
#     push  ->  (push falhou) WhatsApp de volta, mesmo estourando o teto
#           ->  (WA tambem falhou) ai sim pending, como ultimo recurso, com
#               decision_rule propria e WARNING no log.
# Entregar demais > perder. Quando o dreno da fila voltar (nao e escopo deste
# modulo — e decisao de deploy), o degrau do meio pode ser reavaliado.
#
# ESCOPO: as travas rodam dentro de _dispatch_multichannel, ou seja, so valem
# com NOTIFICATION_MULTICHANNEL='on' (que e o estado de producao e a origem
# dos numeros acima). Com multichannel 'off' os modos legados seguem intactos.

GATE_OFF = "off"
GATE_SHADOW = "shadow"
GATE_ON = "on"

# decision_rule gravadas em channel_decisions (queryaveis com '=')
RULE_BUDGET_DOWNGRADED = "budget_exhausted_downgraded"
RULE_BUDGET_SHADOW = "budget_would_downgrade"
RULE_CROSS_DEDUP_DOWNGRADED = "cross_producer_duplicate_downgraded"
RULE_CROSS_DEDUP_SHADOW = "cross_producer_duplicate_would_downgrade"
RULE_SAME_KEY_DOWNGRADED = "dedup_key_duplicate_downgraded"
RULE_SAME_KEY_SHADOW = "dedup_key_duplicate_would_downgrade"
# Escada de rebaixamento (ver bloco "NADA TERMINA EM PILL" abaixo)
RULE_DOWNGRADE_WA_ESCALATION = "downgrade_push_failed_wa_escalation"
RULE_DOWNGRADE_ALL_FAILED = "downgrade_all_channels_failed_queued"

DEFAULT_WA_DAILY_BUDGET = 3
DEFAULT_CROSS_DEDUP_WINDOW_H = 6

# ESCAPE HATCH — quem fura o teto diario.
#
# As regras de URGENCY_RULES sao verificadas contra um FATO (horario da
# reuniao, dominio do remetente, campo severity), diferente de score>=8, que e
# so o produtor afirmando que e importante. Mas nem toda regra deterministica
# merece furar o teto — furar so se justifica quando o atraso de algumas horas
# torna a informacao INUTIL ou IRREVERSIVEL. Lembrar que rebaixado != mudo:
# quem desce vira PUSH (notificacao no celular), nao digest.
#
# FURAM (default):
#   - meeting_soon_unconfirmed: reuniao em <30min. Chegar no digest = inutil.
#   - press_detection: imprensa/jornalista, verificado contra allowlist de
#     dominio + regex. Janela curta e risco reputacional.
#   - cron_error_prod: campo severity error/critical num source de sistema.
#     Risco operacional; atraso vira incidente.
#
# NAO FURAM (default):
#   - financial_alert: casa em `source == 'cost_tracker'` — isso e IDENTIDADE
#     DE PRODUTOR, nao urgencia verificada. E exatamente o caso do alerta de
#     US$3,88 que o Renato citou. Um teto estourado de custo espera o push.
#   - caller_force_immediate: auto-declarado, mesma classe do score>=8.
#     (cost_tracker.check_budget_threshold passa force_immediate=True — outro
#     motivo pra nao deixar essa flag furar o teto.)
#   - linkedin_author_replied / prospect_campaign_reply: valiosos, mas um push
#     resolve; nada quebra se o Renato ver 40 min depois.
#   - frente_keyword_match: heuristica de keyword sobre texto livre — a mais
#     facil de disparar por acidente, a que menos merece furar teto.
#
# Reconfiguravel por env sem deploy: WA_BUDGET_PIERCE_RULES=a,b,c
# (aceita tambem 'caller_force_immediate'; 'none' desliga o escape hatch).
BUDGET_PIERCING_RULES_DEFAULT = (
    "meeting_soon_unconfirmed",
    "press_detection",
    "cron_error_prod",
)


def _gate_mode(env_name: str, default: str) -> str:
    """Le um kill-switch tri-estado (off|shadow|on) de env. Aceita 1/0/true/false."""
    raw = (os.getenv(env_name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "sim"):
        return GATE_ON
    if raw in ("0", "false", "no", "nao"):
        return GATE_OFF
    if raw not in (GATE_OFF, GATE_SHADOW, GATE_ON):
        logger.warning(f"{env_name} invalido: {raw} — usando '{default}'")
        return default
    return raw


def get_budget_mode() -> str:
    """Kill-switch do teto diario. Default 'shadow'.

    'shadow' e o default DE PROPOSITO: preserva o comportamento atual
    byte-a-byte (o WhatsApp sai) e so grava em channel_decisions a linha com
    decision_rule='budget_would_downgrade', dizendo o que SERIA rebaixado.
    Da 2-3 dias de dado real sobre quantas vezes o teto morde antes de flipar
    pra 'on' — mesmo padrao de NOTIFICATION_DIGEST_MODE. Reversivel por env.
    """
    return _gate_mode("WA_DAILY_BUDGET_MODE", GATE_SHADOW)


def get_daily_budget() -> int:
    """Teto de WhatsApps por dia BRT (env WA_DAILY_BUDGET, default 3)."""
    raw = (os.getenv("WA_DAILY_BUDGET") or "").strip()
    if not raw:
        return DEFAULT_WA_DAILY_BUDGET
    try:
        val = int(raw)
    except ValueError:
        logger.warning(f"WA_DAILY_BUDGET invalido: {raw!r} — usando {DEFAULT_WA_DAILY_BUDGET}")
        return DEFAULT_WA_DAILY_BUDGET
    if val < 0:
        logger.warning(f"WA_DAILY_BUDGET negativo: {val} — usando {DEFAULT_WA_DAILY_BUDGET}")
        return DEFAULT_WA_DAILY_BUDGET
    return val


def get_budget_piercing_rules() -> tuple:
    """Regras deterministicas que furam o teto (ver bloco de doc acima)."""
    raw = (os.getenv("WA_BUDGET_PIERCE_RULES") or "").strip()
    if not raw:
        return BUDGET_PIERCING_RULES_DEFAULT
    if raw.lower() in ("none", "-", "off"):
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def get_cross_dedup_mode() -> str:
    """Kill-switch do dedup de WhatsApp (topico cross-produtor + dedup_key). Default 'on'.

    Cobre as duas metades de find_duplicate_whatsapp():
    - CROSS-PRODUTOR por topico: inerte ate alguem passar topic_key/topic_keys
      (ou payload com subject/thread_id). Ligado, nao muda nada hoje — por isso
      a ponte no produtor ja nasce valendo, sem um segundo flip de env.
    - MESMA dedup_key do MESMO produtor: essa NAO e inerte. O branch whatsapp
      nunca consultou dedup_key nenhuma (o dedup so existia nos branches push e
      pill, via ON CONFLICT do _enqueue_pending), entao ligar isto muda
      comportamento no dia do merge. E deliberado: e o buraco que fazia o dedup
      ser decorativo justamente no canal que interrompe. As chaves reais em uso
      hoje (email_urgent:{gmail_id}, inbox_renato:{gmail_id}) so colidem em
      duplicata legitima. Pra desligar: NOTIFICATION_CROSS_DEDUP=off.
    """
    return _gate_mode("NOTIFICATION_CROSS_DEDUP", GATE_ON)


def get_cross_dedup_window_hours() -> int:
    raw = (os.getenv("NOTIFICATION_CROSS_DEDUP_WINDOW_H") or "").strip()
    if not raw:
        return DEFAULT_CROSS_DEDUP_WINDOW_H
    try:
        val = int(raw)
    except ValueError:
        logger.warning(f"NOTIFICATION_CROSS_DEDUP_WINDOW_H invalido: {raw!r}")
        return DEFAULT_CROSS_DEDUP_WINDOW_H
    return val if val > 0 else DEFAULT_CROSS_DEDUP_WINDOW_H


def _budget_pierce_rule(
    source: str,
    msg_type: Optional[str],
    payload: Dict[str, Any],
    urgency_score: Optional[int],
) -> Optional[str]:
    """Retorna o nome da regra deterministica que fura o teto, ou None.

    IMPORTANTE: reavalia as regras DIRETO, sem passar por is_urgent(). Em
    is_urgent() o atalho `score >= 8` roda ANTES do loop de URGENCY_RULES,
    entao um item que casaria numa regra deterministica e reportado como
    'score_ge_8' (e por isso a producao ve 35 linhas 'score_ge_8' e nenhuma
    linha de regra). Pro escape hatch isso importa: o que decide furar e o
    FATO, nao a etiqueta que sobrou no log.
    """
    allowed = get_budget_piercing_rules()
    if not allowed:
        return None
    if "caller_force_immediate" in allowed and payload.get("force_immediate"):
        return "caller_force_immediate"
    for rule_name, rule_fn in URGENCY_RULES:
        if rule_name not in allowed:
            continue
        try:
            if rule_fn(source, msg_type, payload, urgency_score):
                return rule_name
        except Exception as e:
            logger.warning(f"pierce rule '{rule_name}' raised: {e}")
    return None


def _brt_day_window_utc(now: Optional[datetime] = None) -> tuple:
    """Janela [inicio, fim) do dia BRT corrente, em UTC NAIVE.

    O teto conta o DIA BRT (00:00-23:59 America/Sao_Paulo), nao o dia UTC —
    senao a virada aconteceria as 21h de Brasilia. channel_decisions.created_at
    e TIMESTAMP (sem TZ) gravado em UTC pelo NOW() do Postgres, entao a
    comparacao sai com datetimes naive-UTC (mesma convencao de
    consume_pending_for_digest). `now` injetavel pra teste.
    """
    ref = now or now_utc()
    start_brt = to_brt(ref).replace(hour=0, minute=0, second=0, microsecond=0)
    end_brt = start_brt + timedelta(days=1)
    return (
        start_brt.astimezone(UTC).replace(tzinfo=None),
        end_brt.astimezone(UTC).replace(tzinfo=None),
    )


def wa_sent_today(now: Optional[datetime] = None) -> int:
    """Quantos WhatsApps ja sairam no dia BRT corrente.

    Conta channel_decisions com decided_channel='whatsapp' e sent_ok=TRUE —
    ou seja, so o que REALMENTE interrompeu o Renato. WA que falhou e caiu em
    pill nao consome orcamento. Itens que furaram o teto CONTAM (gastaram
    atencao de verdade); eles nunca sao bloqueados, mas fazem o teto morder
    mais cedo pros auto-declarados. Levanta excecao se o DB falhar (o caller
    trata como fail-open).
    """
    start, end = _brt_day_window_utc(now)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM channel_decisions
            WHERE decided_channel = 'whatsapp'
              AND sent_ok IS TRUE
              AND created_at >= %s
              AND created_at < %s
            """,
            (start, end),
        )
        row = cur.fetchone()
        return int(row["c"]) if row else 0


# ---------------------------------------------------------------------------
# Chave de topico — o que torna dois produtores comparaveis
# ---------------------------------------------------------------------------
# Marcadores gravados DENTRO de channel_decisions.dedup_key (sem DDL): o valor
# fica "<dedup_key original> #topic:<k1>##topic:<k2>#". O prefixo continua sendo
# a chave do produtor (nada se perde pra quem le o log hoje) e os marcadores
# viram alvo de LIKE. As duas cercas '#' evitam que um topico prefixo de outro
# case por engano.
TOPIC_MARK = "#topic:"
MIN_TOPIC_LEN = 8

# A chave e um PREFIXO curto do assunto normalizado, de proposito: cada
# produtor trunca o assunto num tamanho diferente antes de repassar
# (email_triage.py:2406 corta em 70; :4559 corta em 60; :2883 e :2972 em 60;
# :4666 em 200). Comparar o assunto inteiro faria "o mesmo e-mail" gerar chaves
# diferentes so por causa do corte. 48 cabe embaixo do menor corte em uso (60)
# e ainda e bem discriminante. Colisao aqui custa um rebaixamento pra push
# (o item chega), nao um descarte.
TOPIC_KEY_MAXLEN = 48

# Re:/Res:/Enc:/Fwd:/Fw: (inclusive repetidos e com contador "Re[2]:")
_TOPIC_REPLY_PREFIX_RE = _re.compile(
    r"^\s*(re|res|ref|rv|enc|encaminhad[oa]|fwd|fw)\s*(\[\d+\]|\(\d+\))?\s*:\s*",
    _re.IGNORECASE,
)
_TOPIC_SUBJECT_FIELDS = ("subject", "subjects", "email_subject", "assunto", "topic")
_TOPIC_THREAD_FIELDS = ("thread_id", "gmail_thread_id", "email_thread_id")


def normalize_topic_key(raw: Any, min_len: int = MIN_TOPIC_LEN) -> Optional[str]:
    """Normaliza um assunto/id pra uma chave comparavel entre produtores.

    'Re: Regularização contábil das minhas 7 empresas' e
    'Regularização contábil das minhas 7 empresas' -> mesma chave
    'regularizacao-contabil-das-minhas-7-empresas'.

    Retorna None quando a chave sai curta demais pra ser confiavel (assunto
    tipo 'oi', 'ok') — melhor nao deduplicar do que deduplicar errado.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for _ in range(6):  # "Re: Enc: Re: ..." — descasca ate parar de mudar
        stripped = _TOPIC_REPLY_PREFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    if not s:
        return None
    key = "-".join(s.split())[:TOPIC_KEY_MAXLEN].rstrip("-")
    if len(key) < min_len:
        return None
    return key


def collect_topic_keys(
    payload: Optional[Dict[str, Any]],
    topic_key: Optional[str] = None,
    topic_keys: Optional[List[str]] = None,
) -> List[str]:
    """Junta e normaliza as chaves de topico de um envio.

    Fontes, nesta ordem:
      1. topic_key / topic_keys passados pelo produtor (caminho preferido —
         um produtor que agrega N e-mails num resumo passa N chaves);
      2. payload['subject'|'subjects'|'email_subject'|'assunto'|'topic'];
      3. payload['thread_id'|'gmail_thread_id'|'email_thread_id'] (prefixo
         'thread-', sem piso de tamanho — id ja e discriminante).

    Sem nenhuma dessas, devolve [] e o dedup cross-produtor fica inerte.
    """
    raw_subjects: List[Any] = []
    raw_threads: List[Any] = []

    if topic_key:
        raw_subjects.append(topic_key)
    if topic_keys:
        raw_subjects.extend(k for k in topic_keys if k)

    if isinstance(payload, dict):
        for field in _TOPIC_SUBJECT_FIELDS:
            val = payload.get(field)
            if isinstance(val, str):
                raw_subjects.append(val)
            elif isinstance(val, (list, tuple)):
                raw_subjects.extend(v for v in val if isinstance(v, str))
        for field in _TOPIC_THREAD_FIELDS:
            val = payload.get(field)
            if isinstance(val, str) and val.strip():
                raw_threads.append(val)

    out: List[str] = []
    for raw in raw_subjects:
        key = normalize_topic_key(raw)
        if key and key not in out:
            out.append(key)
    for raw in raw_threads:
        key = normalize_topic_key(raw, min_len=1)
        if key:
            key = f"thread-{key}"
            if key not in out:
                out.append(key)
    return out[:12]  # teto defensivo: dedup_key nao vira paragrafo


def encode_dedup_key(dedup_key: Optional[str], topics: List[str]) -> Optional[str]:
    """Serializa dedup_key + marcadores de topico pra channel_decisions."""
    if not topics:
        return dedup_key
    marks = "".join(f"{TOPIC_MARK}{t}#" for t in topics)
    return f"{dedup_key} {marks}" if dedup_key else marks


def _like_literal(value: str) -> str:
    """Escapa os curingas de LIKE pra usar uma string crua como padrao exato."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_duplicate_whatsapp(
    source: str,
    dedup_key: Optional[str],
    topics: List[str],
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Ja saiu WhatsApp sobre esta MESMA coisa dentro da janela?

    Duas formas de "mesma coisa":

    a) CROSS-PRODUTOR (`source IS DISTINCT FROM`): outro produtor ja mandou WA
       com um destes topicos. E o caso Orestes — o e-mail do pai saiu como
       '📧 Email urgente (P9)' por email_triage e de novo dentro da '🤖 Triagem
       inteligente' por inbox_smart, 30-60min depois.

    b) MESMA CHAVE DO MESMO PRODUTOR (`dedup_key`): ate hoje o branch whatsapp
       de _dispatch_multichannel NAO consultava dedup_key nenhuma — so os
       branches push e pill consultavam (via ON CONFLICT do _enqueue_pending).
       Ou seja, no unico canal que interrompe o Renato o dedup era decorativo.
       Aqui a chave passa a valer tambem pro WhatsApp.

    `sent_ok IS TRUE` de proposito: se o WA anterior falhou, o Renato nao viu —
    o segundo tem direito de tentar. Retorna a linha anterior ou None.
    """
    topic_patterns = [f"%{TOPIC_MARK}{t}#%" for t in topics]
    own_patterns: List[str] = []
    if dedup_key:
        esc = _like_literal(dedup_key)
        own_patterns = [esc, f"{esc} {TOPIC_MARK}%"]  # com e sem marcadores
    if not topic_patterns and not own_patterns:
        return None

    since = (to_utc(now or now_utc()) - timedelta(hours=get_cross_dedup_window_hours()))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, source, msg_type, created_at, payload_title
            FROM channel_decisions
            WHERE decided_channel = 'whatsapp'
              AND sent_ok IS TRUE
              AND created_at >= %s
              AND (
                    (source IS DISTINCT FROM %s AND dedup_key LIKE ANY(%s::text[]))
                 OR (source = %s AND dedup_key LIKE ANY(%s::text[]))
                  )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (since.replace(tzinfo=None), source, topic_patterns, source, own_patterns),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def whatsapp_gate(
    *,
    source: str,
    msg_type: Optional[str],
    payload: Dict[str, Any],
    urgency_score: Optional[int],
    topics: List[str],
    dedup_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple:
    """Decide se um candidato a WhatsApp deve DESCER de canal.

    Retorna (rule, mode):
      (None, 'off'/'shadow'/...)  -> segue pra WhatsApp
      (rule, 'on')                -> rebaixa pra push (que escala de volta pro
                                     WhatsApp se o push nao existir)
      (rule, 'shadow')            -> manda WhatsApp, mas loga a rule shadow

    Ordem: dedup primeiro — por chave do produtor ou por topico cross-produtor;
    se a MESMA coisa ja foi entregue por WA na janela, repetir nao acrescenta
    informacao, e isso vale ate pra regra deterministica. Orcamento depois, que
    e o unico atravessado pelo escape hatch. Qualquer falha de DB => fail-open:
    manda o WhatsApp.
    """
    shadow_rule: Optional[str] = None

    cross_mode = get_cross_dedup_mode()
    if cross_mode != GATE_OFF:
        try:
            dup = find_duplicate_whatsapp(source, dedup_key, topics, now=now)
        except Exception as e:
            logger.warning(f"dedup de WhatsApp falhou (fail-open, manda WA): {e}")
            dup = None
        if dup:
            same_producer = dup.get("source") == source
            logger.info(
                f"dedup: {source} repete algo ja enviado por WA por "
                f"'{dup.get('source')}' (channel_decisions#{dup.get('id')})"
            )
            if cross_mode == GATE_ON:
                return (
                    RULE_SAME_KEY_DOWNGRADED if same_producer
                    else RULE_CROSS_DEDUP_DOWNGRADED
                ), GATE_ON
            shadow_rule = (
                RULE_SAME_KEY_SHADOW if same_producer else RULE_CROSS_DEDUP_SHADOW
            )

    budget_mode = get_budget_mode()
    if budget_mode == GATE_OFF:
        return shadow_rule, (GATE_SHADOW if shadow_rule else GATE_OFF)

    pierce = _budget_pierce_rule(source, msg_type, payload, urgency_score)
    if pierce:
        logger.info(f"orcamento: {source} fura o teto (regra deterministica '{pierce}')")
        return shadow_rule, (GATE_SHADOW if shadow_rule else GATE_OFF)

    budget = get_daily_budget()
    try:
        used = wa_sent_today(now=now)
    except Exception as e:
        logger.warning(f"orcamento: contagem falhou (fail-open, manda WA): {e}")
        return shadow_rule, (GATE_SHADOW if shadow_rule else GATE_OFF)

    if used < budget:
        return shadow_rule, (GATE_SHADOW if shadow_rule else GATE_OFF)

    logger.info(f"orcamento diario estourado ({used}/{budget}) — {source} desce de canal")
    if budget_mode == GATE_ON:
        return RULE_BUDGET_DOWNGRADED, GATE_ON
    return (shadow_rule or RULE_BUDGET_SHADOW), GATE_SHADOW


def _send_push(
    title: str,
    body: str,
    urgent: bool = False,
    tag: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> bool:
    """Envia Web Push via push_notifications. Retorna True se sent>0.

    is_configured()=False ou qualquer excecao -> False (caller cai em pill).
    Usa o wrapper unico dispatch_push (raise_on_error=False = degrada; mesmo
    wrapper que actuators usa com raise_on_error=True)."""
    from services.push_notifications import dispatch_push
    res = dispatch_push(
        title=title,
        body=body,
        data=data or {},
        tag=tag,
        urgent=urgent,
        raise_on_error=False,
    )
    return bool((res or {}).get("sent", 0) > 0)


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
    topic_key: Optional[str] = None,
    topic_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Roteia por canal quando NOTIFICATION_MULTICHANNEL='on'.

    whatsapp -> _send_now  |  push -> _send_push (falha -> pill)  |  pill -> fila.
    Domingo (non-urgent) cai em pill/morning (preserva domingo sagrado).
    Candidato a WhatsApp passa antes pelo whatsapp_gate (teto diario + dedup);
    rebaixado vira push e, se o push nao existir, VOLTA pro WhatsApp — a escada
    do rebaixado nao termina na fila pending, que hoje e dead-letter.
    Sempre grava channel_decisions (best-effort).
    """
    channel, rule = decide_channel(payload, urgency_score, source, msg_type)
    title = payload.get("title") or source
    push_data = payload.get("data") if isinstance(payload.get("data"), dict) else None

    # Chaves de topico -> vao gravadas em channel_decisions.dedup_key em TODAS
    # as linhas (inclusive push/pill), pra que o proximo produtor consiga casar.
    topics = collect_topic_keys(payload, topic_key, topic_keys)
    log_key = encode_dedup_key(dedup_key, topics)

    # Teto diario + dedup cross-produtor (so morde candidato a WhatsApp).
    downgraded_from: Optional[str] = None
    if channel == "whatsapp":
        gate_rule, gate_mode = whatsapp_gate(
            source=source,
            msg_type=msg_type,
            payload=payload,
            urgency_score=urgency_score,
            topics=topics,
            dedup_key=dedup_key,
        )
        if gate_rule and gate_mode == GATE_ON:
            logger.info(f"router: {source} rebaixado whatsapp->push ({gate_rule})")
            channel, rule, downgraded_from = "push", gate_rule, "whatsapp"
        elif gate_rule:
            # shadow: o WhatsApp sai igual, mas o log diz o que SERIA rebaixado
            rule = gate_rule

    # Domingo silence: non-urgent (push/pill) adia pra morning como pill.
    # (channel=='whatsapp' so acontece quando urgent; _sunday_silence_enqueue ja
    # e non-urgent, mas mantemos o guard pra nao nem chamar/enfileirar nesse caso.
    # `downgraded_from` tambem e sempre urgent — guard explicito pra que um item
    # rebaixado nunca caia no adiamento de domingo por acidente futuro.)
    if channel != "whatsapp" and not downgraded_from:
        applies, pid = _sunday_silence_enqueue(
            urgent, urgency_rule, source, payload, msg_type, urgency_score, dedup_key,
        )
        if applies:
            logger.info(f"domingo silence (multichannel): {source} -> pill/morning")
            _log_channel_decision(
                source, msg_type, urgency_score, "pill", "sunday_silence",
                pid is not None, MULTICHANNEL_ON, log_key, title,
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
            ok, MULTICHANNEL_ON, log_key, title,
        )
        if not ok:
            # WA falhou -> NAO descarta o urgente: cai em pill (ledger duravel +
            # badge + entra no proximo digest). Melhor chegar tarde que sumir.
            pid = _enqueue_pending(source, payload, msg_type, urgency_score, digest_target, dedup_key)
            _log_channel_decision(
                source, msg_type, urgency_score, "pill", "whatsapp_fail_fallback",
                pid is not None, MULTICHANNEL_ON, log_key, title,
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

    # Item REBAIXADO (teto/dedup) — escada propria, que NAO termina em pill.
    # A fila pending e dead-letter hoje (ver bloco "NADA TERMINA EM PILL"), e
    # o item aqui e urgente de verdade: push, e se o push nao existir volta
    # pro WhatsApp mesmo estourando o teto. Enfileirar so se os dois falharem.
    if downgraded_from:
        pushed = _send_push(title=title, body=text, urgent=True, tag=dedup_key, data=push_data)
        _log_channel_decision(
            source, msg_type, urgency_score, "push", rule,
            pushed, MULTICHANNEL_ON, log_key, title,
        )
        if pushed:
            return {
                "action": "sent",
                "pending_id": None,
                "channel": "push",
                "decision_rule": rule,
                "downgraded_from": downgraded_from,
                "mode": "multichannel",
            }

        ok = await _send_now(text)
        _log_channel_decision(
            source, msg_type, urgency_score, "whatsapp", RULE_DOWNGRADE_WA_ESCALATION,
            ok, MULTICHANNEL_ON, log_key, title,
        )
        logger.warning(
            f"router: rebaixado ({rule}) mas push indisponivel — escalando de "
            f"volta pro WhatsApp ({source}); entregar demais > perder"
        )
        if ok:
            return {
                "action": "sent",
                "pending_id": None,
                "channel": "whatsapp",
                "decision_rule": RULE_DOWNGRADE_WA_ESCALATION,
                "downgraded_from": downgraded_from,
                "mode": "multichannel",
            }

        # Push E WhatsApp fora do ar: pending como ultimo recurso. Fica com
        # rule propria porque, com o dreno desligado, isto e um item EM RISCO.
        pid = _enqueue_pending(source, payload, msg_type, urgency_score, digest_target, dedup_key)
        _log_channel_decision(
            source, msg_type, urgency_score, "pill", RULE_DOWNGRADE_ALL_FAILED,
            pid is not None, MULTICHANNEL_ON, log_key, title,
        )
        logger.error(
            f"router: push E WhatsApp falharam p/ item rebaixado ({source}) — "
            f"enfileirado (pid={pid}); a fila pending nao tem dreno ativo"
        )
        return {
            "action": "queued_all_channels_failed" if pid else "duplicate",
            "pending_id": pid,
            "channel": "pill",
            "decision_rule": RULE_DOWNGRADE_ALL_FAILED,
            "downgraded_from": downgraded_from,
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
                False, MULTICHANNEL_ON, log_key, title,
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
            pushed, MULTICHANNEL_ON, log_key, title,
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
        pid is not None, MULTICHANNEL_ON, log_key, title,
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
    topic_key: Optional[str] = None,
    topic_keys: Optional[List[str]] = None,
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
                   E POR PRODUTOR (entra no UNIQUE (source, dedup_key)).
        message_text: texto pronto pra WhatsApp. Se omitido, usa payload['body'].
        topic_key / topic_keys: DE QUE esta notificacao trata, em linguagem
                   comparavel ENTRE produtores — normalmente o assunto do
                   e-mail (o router tira Re:/Enc:, acento e pontuacao) ou um
                   thread id. Use `topic_keys` quando uma mensagem agrega
                   varios assuntos (ex: resumo com N e-mails). Se dois
                   produtores DIFERENTES mandarem WhatsApp sobre o mesmo
                   topico em <6h, o segundo desce pra push
                   (NOTIFICATION_CROSS_DEDUP). Alternativa sem passar nada:
                   deixar payload['subject'] / payload['thread_id'] no dict.
                   So tem efeito com NOTIFICATION_MULTICHANNEL='on'.

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
            topic_key=topic_key,
            topic_keys=topic_keys,
        )

    # Mode 'off' — comportamento legado (mas com silence guard pra domingo)
    # (topic_key/topic_keys sao ignorados nos modos legados de proposito: eles
    # nao gravam channel_decisions, entao nao ha ledger onde casar topicos.)
    if mode == MODE_OFF:
        applies, pid = _sunday_silence_enqueue(
            urgent, urgency_rule, source, payload, msg_type, urgency_score, dedup_key,
        )
        if applies:
            logger.info(f"domingo silence: msg adiada pra segunda 8h (source={source})")
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


async def notify(
    source: str,
    title: str,
    body: str,
    score: Optional[int] = None,
    *,
    msg_type: Optional[str] = None,
    dedup: Optional[str] = None,
    digest_target: str = "either",
    topic: Optional[str] = None,
    topics: Optional[List[str]] = None,
) -> bool:
    """Helper fino sobre route_to_renato pros call-sites HOMOGENEOS.

    Centraliza o boilerplate repetido em ~10 call-sites migrados: monta
    payload={'title', 'body'}, usa body como message_text (WA), e devolve
    True SSE o item foi enviado imediato (action == 'sent').

    `topic` / `topics`: assunto(s) do que esta sendo notificado, pro dedup
    cross-produtor (ver route_to_renato). `topic` pra uma coisa so, `topics`
    pra mensagem que agrega varias. Opcional e retrocompativel — quem nao
    passa fica exatamente como hoje.

    NAO cobre call-sites que precisam de payload rico (force_immediate,
    contact_id, proposal_id...), que interpretam outras actions
    (queued/shadow/duplicate) ou que fazem fire-and-forget via create_task —
    esses continuam chamando route_to_renato direto. Nao engole excecao: o
    try/except do caller (quando existe) segue valendo."""
    _r = await route_to_renato(
        source=source,
        payload={"title": title, "body": body},
        msg_type=msg_type,
        urgency_score=score,
        digest_target=digest_target,
        dedup_key=dedup,
        message_text=body,
        topic_key=topic,
        topic_keys=topics,
    )
    return _r.get("action") == "sent"


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
