"""Instrumentacao de custo LLM por-FUNCAO (F-E capability registry).

PROBLEMA (retro F-E): dos ~$42 de custo LLM em 14d, so ~$10 estavam atribuidos a
funcoes; ~$32 ficavam CEGOS porque a maioria dos call-sites de LLM do INTEL NAO
grava em `tonia_llm_usage`. O capability_registry deriva custo-por-funcao lendo
essa tabela (endpoint = "funcao"); sem registro, o custo cai num balde anonimo
`llm:anthropic-platform`.

ESTE MODULO e o ponto unico de gravacao pro lado-INTEL: cada call-site de LLM
chama `record_response(function, model, response_json)` UMA linha depois de
parsear a resposta da Anthropic, e passa a atribuir {funcao, modelo, tokens
in/out/cache, custo} pro registry.

CONTRATO da tabela `tonia_llm_usage` (criada pelo worker Tonia no Neon; o INTEL
so LE via capability_registry ate agora):
    ts, endpoint (=funcao), conversation_id, model,
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
    cost_usd numeric(10,6), metadata jsonb

DEFENSIVO: o registro de custo NUNCA pode quebrar a chamada de LLM. Todo caminho
e try/except silencioso (best-effort telemetry) — se a tabela sumir no alvo, se
o DB estiver fora, se o JSON vier torto, engole e loga warning.

PRECOS (USD por milhao de tokens) — fonte skill claude-api (cached 2026-06):
    haiku-4-5   in 1.00  out 5.00
    sonnet-4-6  in 3.00  out 15.00   (== wa_triage.py, ja existente)
    opus-4-7    in 5.00  out 25.00
Cache read ~= 0.1x input; cache write (5m TTL) ~= 1.25x input.
Atualizar com bump de modelo (mesma nota que llm.py). Modelo desconhecido cai no
preco Sonnet com warning (nunca zera o custo).
"""
import logging
from decimal import Decimal
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# USD por milhao de tokens: (input, output, cache_read, cache_write)
_PRICING = {
    "haiku":  (Decimal("1.00"),  Decimal("5.00"),  Decimal("0.10"), Decimal("1.25")),
    "sonnet": (Decimal("3.00"),  Decimal("15.00"), Decimal("0.30"), Decimal("3.75")),
    "opus":   (Decimal("5.00"),  Decimal("25.00"), Decimal("0.50"), Decimal("6.25")),
}
_DEFAULT_TIER = "sonnet"  # fallback conservador (nunca zera custo)
_MILLION = Decimal(1_000_000)


def _tier_for_model(model: str) -> str:
    """Mapeia model id -> tier de preco por substring. Desconhecido -> sonnet."""
    m = (model or "").lower()
    for tier in ("haiku", "sonnet", "opus"):
        if tier in m:
            return tier
    logger.warning("llm_usage: modelo desconhecido %r -> preco %s", model, _DEFAULT_TIER)
    return _DEFAULT_TIER


def compute_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Custo em USD a partir de tokens + modelo. Puro, sem I/O."""
    pin, pout, pcr, pcw = _PRICING[_tier_for_model(model)]
    total = (
        Decimal(int(input_tokens or 0)) * pin
        + Decimal(int(output_tokens or 0)) * pout
        + Decimal(int(cache_read_tokens or 0)) * pcr
        + Decimal(int(cache_creation_tokens or 0)) * pcw
    ) / _MILLION
    return float(total)


def _extract_usage(response_json: Dict[str, Any]) -> Dict[str, int]:
    """Normaliza o bloco `usage` de uma resposta da Messages API pros nomes de
    coluna do tonia_llm_usage. A API usa cache_read_input_tokens /
    cache_creation_input_tokens; a tabela usa cache_read_tokens /
    cache_creation_tokens."""
    usage = (response_json or {}).get("usage") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }


def record(
    function: str,
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    conversation_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    cost_usd: Optional[float] = None,
) -> Optional[float]:
    """Grava UMA linha em tonia_llm_usage. Best-effort: qualquer erro e engolido
    (nunca propaga pro call-site de LLM). Retorna o custo gravado, ou None se
    falhou/skip.

    `function` vira a coluna `endpoint` (a "funcao" que o registry atribui, ex
    'ocr.screenshot', 'smart_update.project', 'briefing.contact'). `cost_usd`
    pode vir pre-calculado (ex wa_triage ja computa); senao computa aqui.
    """
    if not function or not model:
        return None
    try:
        import json
        from database import get_db

        if cost_usd is None:
            cost_usd = compute_cost(
                model, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
            )

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO tonia_llm_usage (
                    endpoint, conversation_id, model,
                    input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens,
                    cost_usd, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    function, conversation_id, model,
                    int(input_tokens or 0), int(output_tokens or 0),
                    int(cache_read_tokens or 0), int(cache_creation_tokens or 0),
                    round(float(cost_usd), 6),
                    json.dumps(metadata or {}),
                ),
            )
        return round(float(cost_usd), 6)
    except Exception as e:  # noqa: BLE001 — telemetria nunca quebra o caller
        logger.warning("llm_usage.record falhou (funcao=%s): %s", function, e)
        return None


def record_response(
    function: str,
    model: str,
    response_json: Dict[str, Any],
    *,
    conversation_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Conveniencia: extrai o `usage` de uma resposta da Messages API (dict de
    response.json() ou do SDK via .model_dump()) e grava. UMA linha por chamada
    no call-site:

        result = response.json()
        record_response("ocr.screenshot", model, result)

    Best-effort — engole qualquer erro. Usa o `model` retornado pela API se
    presente (mais preciso que o literal enviado)."""
    try:
        rj = response_json or {}
        eff_model = rj.get("model") or model
        toks = _extract_usage(rj)
        return record(
            function, eff_model,
            conversation_id=conversation_id, metadata=metadata,
            **toks,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_usage.record_response falhou (funcao=%s): %s", function, e)
        return None
