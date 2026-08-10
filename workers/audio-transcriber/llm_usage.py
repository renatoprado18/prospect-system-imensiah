"""Registro de custo LLM do WORKER — espelho do `app/services/llm_usage.py`.

POR QUE EXISTE UMA SEGUNDA COPIA (10/08/2026)
---------------------------------------------
O worker e um servico Railway com build ISOLADO: `Procfile` proprio,
`requirements.txt` sem as dependencias do INTEL, e o `app/` do INTEL nao esta
no container. Ele nao pode importar `services.llm_usage`. Ate hoje o resultado
disso era pior que duplicacao: as **8 chamadas ao Claude daqui nao registravam
nada** — bot por texto, transcricao de audio, analise de imagem, PDF e ata, ou
seja, o volume pesado. O medidor de custo reportava um numero que excluia tudo
isso **sem sinalizar que excluia**, e medidor cego corta o que aparece e poupa
o que nao aparece.

A DUPLICACAO E' DO I/O, NAO DA REGRA
------------------------------------
Processos separados, drivers diferentes (psycopg2 la, psycopg3 aqui) — dois
INSERTs sao legitimos. O que NAO pode divergir e a tabela de precos: dois
precos que discordam produzem dois relatorios que discordam, e nenhum dos dois
avisa. `tests/test_llm_usage_paridade.py` compara as duas tabelas e reprova se
uma mudar sem a outra. Ao bumpar preco, mude os dois — o teste lembra.

DEFENSIVO: telemetria NUNCA quebra a chamada de LLM. Todo caminho e
try/except silencioso — se a tabela sumir, se o banco estiver fora, se o JSON
vier torto, engole e loga warning.
"""
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# USD por milhao de tokens: (input, output, cache_read, cache_write)
# ESPELHO de app/services/llm_usage.py::_PRICING — mudou la, muda aqui.
_PRICING = {
    "haiku":  (Decimal("1.00"),  Decimal("5.00"),  Decimal("0.10"), Decimal("1.25")),
    "sonnet": (Decimal("3.00"),  Decimal("15.00"), Decimal("0.30"), Decimal("3.75")),
    "opus":   (Decimal("5.00"),  Decimal("25.00"), Decimal("0.50"), Decimal("6.25")),
}
_DEFAULT_TIER = "sonnet"  # fallback conservador (nunca zera custo)
_MILLION = Decimal(1_000_000)


def _tier_for_model(model: str) -> str:
    m = (model or "").lower()
    for tier in ("haiku", "sonnet", "opus"):
        if tier in m:
            return tier
    logger.warning("llm_usage: modelo desconhecido %r -> preco %s", model, _DEFAULT_TIER)
    return _DEFAULT_TIER


def compute_cost(model: str, input_tokens: int = 0, output_tokens: int = 0,
                 cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> float:
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
    """Normaliza o bloco `usage` da Messages API pros nomes de coluna da tabela."""
    usage = (response_json or {}).get("usage") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }


def record_response(function: str, model: str, response_json: Dict[str, Any],
                    *, conversation_id: Optional[int] = None,
                    metadata: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """Grava UMA linha em tonia_llm_usage a partir da resposta da Messages API.

    Chamar logo depois de `resp.json()`, no caminho de sucesso:

        result = ai_resp.json()
        llm_usage.record_response("worker.bot_respond", MODEL, result)

    Best-effort: retorna o custo gravado, ou None se falhou/skip.
    """
    try:
        rj = response_json or {}
        eff_model = rj.get("model") or model  # a API devolve o id resolvido
        toks = _extract_usage(rj)

        # Sem tokens nao ha o que medir: gravar zero poluiria a media com
        # linhas vazias e faria parecer que a chamada foi de graca.
        if not any(toks.values()):
            return None

        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            logger.warning("llm_usage: DATABASE_URL ausente — custo de %s nao registrado", function)
            return None

        cost = compute_cost(eff_model, **toks)

        import psycopg
        with psycopg.connect(database_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
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
                        function, conversation_id, eff_model,
                        toks["input_tokens"], toks["output_tokens"],
                        toks["cache_read_tokens"], toks["cache_creation_tokens"],
                        round(cost, 6),
                        json.dumps({**(metadata or {}), "origem": "worker"}),
                    ),
                )
        return round(cost, 6)
    except Exception as e:  # noqa: BLE001 — telemetria nunca quebra o caller
        logger.warning("llm_usage.record_response falhou (funcao=%s): %s", function, e)
        return None
