"""Policy central de modelos LLM por tier de tarefa (F-E).

v0 comportamento-preservador: cada tier = o modelo que os call sites JÁ usam
hoje. Centralizar aqui permite (a) trocar o modelo de um tier inteiro em 1
linha e (b) medir custo por tier (PDCA). NÃO migrar modelo no v0 — só mover o
literal pra cá. Ver skill claude-api pra migração futura (Opus 4.8/Sonnet 5).

v1 (ADVISOR, flag TRIAGE_ADVISOR, default off): além de ROTEAR (mapa estático
função→tier), agora sabe ESCALAR em run-time. Um classificador barato (FAST/
Haiku) roda; se está em DÚVIDA (confidence baixa) OU bate um gate crítico
(contato círculo 1-2, borda de urgência), re-roda a MESMA classificação no
forte (BALANCED/Sonnet), que SUBSTITUI o veredito do Haiku. Alvo: matar o
falso-negativo caro ("por que isso não apareceu no briefing?"). Ver
classify_with_advisor.
"""
import os
import asyncio
import logging

logger = logging.getLogger(__name__)

FAST = "claude-haiku-4-5-20251001"     # classificação, triagem, OCR, extração barata
BALANCED = "claude-sonnet-4-6"          # geração, draft, análise média (default)
DEEP = "claude-opus-4-7"                # análise profunda (raro)


# ─────────────────────────────────────────────────────────────────────────────
# ADVISOR — escalada de confiança Haiku→Sonnet na triagem
# ─────────────────────────────────────────────────────────────────────────────
# Threshold de confiança: abaixo disso, o Haiku está "em dúvida" e escalamos.
TRIAGE_ADVISOR_THRESHOLD = 0.7
# Threshold pra contatos CRÍTICOS (círculo 1-2 — falso-negativo caro): bar mais
# alto (escala com menos dúvida), MAS não é "sempre". Calibração 19/07: o gate
# antigo forçava escalada em 100% dos círculo 1-2 (toda msg casual da Emma pagava
# Sonnet). Bar 0.9 = escala a dúvida real do contato próximo sem queimar Sonnet
# no óbvio (conf>=0.9). A retro PDCA afina via telemetria (verdict_changed/rate).
TRIAGE_ADVISOR_CRITICAL_THRESHOLD = 0.9
# Cap de sanidade: se o advisor escalar mais que isso do volume, ele virou
# "sempre Sonnet" (perdeu o ganho de custo) — logamos alerta 1x pra revisar
# threshold/gate. Não BLOQUEIA (nunca queremos degradar precisão), só sinaliza.
_ADVISOR_MAX_ESCALATION_RATE = 0.5
_ADVISOR_MIN_SAMPLE = 20

# Telemetria de processo (best-effort, reinicia a cada boot; alimenta o warning
# do cap e um agregado leve. O log estruturado por-escalada é a fonte da retro).
_advisor_calls = 0          # chamadas com advisor ON
_advisor_escalations = 0    # quantas realmente escalaram (e o strong respondeu)
_advisor_changed = 0        # quantas o strong VIROU o veredito do fast
_advisor_rate_warned = False


def advisor_enabled() -> bool:
    """Flag TRIAGE_ADVISOR. Default OFF. Liga com on/1/true/yes.

    strip().lower() = padrão anti-whitespace do repo (Vercel cola \\n em env
    vars — ver [[feedback_env_var_whitespace]]).
    """
    return (os.getenv("TRIAGE_ADVISOR") or "").strip().lower() in ("1", "on", "true", "yes")


def advisor_telemetry() -> dict:
    """Snapshot dos contadores de processo (pra endpoint/retro PDCA)."""
    rate = (_advisor_escalations / _advisor_calls) if _advisor_calls else 0.0
    return {
        "advisor_on": advisor_enabled(),
        "calls": _advisor_calls,
        "escalations": _advisor_escalations,
        "verdict_changed": _advisor_changed,
        "escalation_rate": round(rate, 4),
        "threshold": TRIAGE_ADVISOR_THRESHOLD,
        "critical_threshold": TRIAGE_ADVISOR_CRITICAL_THRESHOLD,
    }


def _call_model(model: str, prompt: str, max_tokens: int = 200):
    """Chamada síncrona (bloqueante) ao Claude via SDK. Retorna texto bruto ou
    None (sem key / sem SDK / erro de API). SÍNCRONA de propósito —
    classify_with_advisor envolve em asyncio.to_thread.
    """
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        logger.warning("llm._call_model: ANTHROPIC_API_KEY ausente")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("llm._call_model: anthropic SDK ausente")
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        try:  # F-E: custo por-funcao (telemetria nunca quebra a chamada real)
            from services import llm_usage
            llm_usage.record_response("triage.advisor", model, msg.model_dump())
        except Exception:
            pass
        return msg.content[0].text if msg.content else ""
    except Exception as e:
        logger.warning(f"llm._call_model {model} erro: {e}")
        return None


async def classify_with_advisor(
    prompt,
    parse_fn,
    *,
    decision_key,
    critical_gate=False,
    threshold=TRIAGE_ADVISOR_THRESHOLD,
    critical_threshold=TRIAGE_ADVISOR_CRITICAL_THRESHOLD,
    max_tokens=200,
    fast_model=FAST,
    strong_model=BALANCED,
    label="triage",
    call_fn=None,
):
    """Roda o classificador barato (FAST/Haiku). Com a flag TRIAGE_ADVISOR ON,
    se o resultado é DUVIDOSO (confidence < threshold) OU bate um gate crítico
    (critical_gate=True), re-roda a MESMA classificação no forte (BALANCED/
    Sonnet) — cujo veredito SUBSTITUI o do Haiku.

    Default OFF = só Haiku, 1 chamada, zero custo extra, comportamento idêntico
    ao anterior (o campo confidence extra no output do Haiku é simplesmente
    ignorado). O advisor SÓ melhora precisão — nunca cria ação/proposta/
    notificação por conta própria, e se o Sonnet falhar/parsear mal, mantém o
    Haiku (nunca degrada).

    Args:
        prompt: o prompt completo (o MESMO vai pros dois tiers).
        parse_fn: callable(raw:str|None) -> dict|None. Extrai o veredito +
                  'confidence' (0.0-1.0). Retorna None se não parseou.
        decision_key: chave no dict parseado usada pra detectar se o Sonnet
                      MUDOU o veredito do Haiku (telemetria).
        critical_gate: contato crítico (círculo 1-2) — usa bar de confiança mais
                       alto (critical_threshold), escala com menos dúvida mas
                       NÃO no óbvio (conf alta não paga Sonnet).
        threshold: corte de confidence pra escalar contato normal (default 0.7).
        critical_threshold: corte pra contato crítico (default 0.9).
        call_fn: callable(model, prompt, max_tokens) -> str|None, SÍNCRONO.
                 Injetável pra teste/mock. Default = _call_model.

    Returns:
        dict {
          "parsed": dict|None,     # veredito vencedor (do tier que decidiu)
          "tier": "fast"|"strong", # quem decidiu
          "escalated": bool,       # tentou escalar?
          "changed": bool,         # o strong virou o veredito do fast?
          "confidence": float|None,# confidence do fast
          "advisor_on": bool,
        }
    """
    global _advisor_calls, _advisor_escalations, _advisor_changed, _advisor_rate_warned
    call = call_fn or _call_model

    fast_raw = await asyncio.to_thread(call, fast_model, prompt, max_tokens)
    fast_parsed = parse_fn(fast_raw) if fast_raw is not None else None
    if fast_parsed is None:
        # Sem key/SDK/parse — caller decide o fallback conservador.
        return {"parsed": None, "tier": "fast", "escalated": False,
                "changed": False, "confidence": None, "advisor_on": advisor_enabled()}

    try:
        conf = float(fast_parsed.get("confidence"))
    except (TypeError, ValueError):
        conf = 1.0  # sem confidence → trata como certeza (não escala)

    # ── OFF: caminho byte-a-byte do comportamento anterior (só Haiku) ──
    if not advisor_enabled():
        return {"parsed": fast_parsed, "tier": "fast", "escalated": False,
                "changed": False, "confidence": conf, "advisor_on": False}

    _advisor_calls += 1
    # Contato crítico (círculo 1-2) usa bar de confiança mais alto — escala com
    # menos dúvida, mas NÃO sempre: conf>=critical_threshold no óbvio não paga
    # Sonnet (calibração 19/07, ver constante). Resto usa o threshold normal.
    effective_threshold = critical_threshold if critical_gate else threshold
    should_escalate = conf < effective_threshold
    if not should_escalate:
        return {"parsed": fast_parsed, "tier": "fast", "escalated": False,
                "changed": False, "confidence": conf, "advisor_on": True}

    # ── Escalada: mesma classificação no tier forte ──
    strong_raw = await asyncio.to_thread(call, strong_model, prompt, max_tokens)
    strong_parsed = parse_fn(strong_raw) if strong_raw is not None else None
    if strong_parsed is None:
        # Strong falhou/parse ruim → mantém Haiku. Nunca piora.
        logger.info(
            f"[advisor:{label}] escalou (conf={conf:.2f} gate={critical_gate}) "
            f"mas strong falhou — mantendo veredito fast"
        )
        return {"parsed": fast_parsed, "tier": "fast", "escalated": True,
                "changed": False, "confidence": conf, "advisor_on": True}

    _advisor_escalations += 1
    changed = strong_parsed.get(decision_key) != fast_parsed.get(decision_key)
    if changed:
        _advisor_changed += 1

    # Telemetria por-escalada (fonte da retro PDCA: quantas escaladas viraram o
    # veredito → o advisor paga?).
    logger.info(
        f"[advisor:{label}] ESCALOU fast→strong | conf={conf:.2f} gate={critical_gate} "
        f"changed={changed} fast_{decision_key}={fast_parsed.get(decision_key)!r} "
        f"strong_{decision_key}={strong_parsed.get(decision_key)!r}"
    )

    # Cap de sanidade (loga 1x): escalada virou "sempre Sonnet"?
    if (not _advisor_rate_warned and _advisor_calls >= _ADVISOR_MIN_SAMPLE
            and _advisor_escalations / _advisor_calls > _ADVISOR_MAX_ESCALATION_RATE):
        logger.warning(
            f"[advisor:{label}] taxa de escalada {_advisor_escalations}/{_advisor_calls} "
            f"> {_ADVISOR_MAX_ESCALATION_RATE:.0%} — advisor perto de 'sempre Sonnet' "
            f"(perde o ganho de custo). Revisar threshold/gate."
        )
        _advisor_rate_warned = True

    return {"parsed": strong_parsed, "tier": "strong", "escalated": True,
            "changed": changed, "confidence": conf, "advisor_on": True}
