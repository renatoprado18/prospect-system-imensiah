"""
Embeddings service — wrapper sobre Voyage AI pra gerar embeddings semanticos
das memorias persistentes do INTEL coach (system_memories).

Why Voyage: recomendado pela Anthropic, multilingual (inclui PT-BR de qualidade),
voyage-4-lite custa $0.02/M tokens — pra ~6 memorias atuais e crescimento
estimado de ~1 por dia, e <$0.10/mes mesmo com backfill agressivo.

Why nao SDK: voyageai pkg adiciona dep nova. Usamos httpx (ja no requirements)
direto. Cold start fica intacto.

Usage:
    from services.embeddings import embed, embedding_to_pg_literal

    vec = await embed("texto qualquer")  # returns list[float] of len 1024
    if vec is None:
        # provider down — caller deve fallback pra keyword search
        ...

Cache: LRU in-memory in-process (functools.lru_cache nao funciona em async,
entao mantemos dict simples com limite de 256 entries por process). Reset
quando processo reinicia. Pra uso atual (cron + chat ad-hoc) e suficiente.
"""

import logging
import os
from collections import OrderedDict
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


# ---- Config (centralizada aqui pra facilitar troca de provider/modelo) ----
VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-4-lite"      # $0.02/1M tokens, 1024 dims, multilingual
VOYAGE_DIMS = 1024
VOYAGE_TIMEOUT_S = 15.0

# Cache LRU simples (max 256 textos). Em produção seria Redis, mas pro
# volume atual (system_memories tem ~6 entries, chat eh 1 query/min) basta.
_CACHE_MAX = 256
_cache: "OrderedDict[str, List[float]]" = OrderedDict()


def _get_api_key() -> Optional[str]:
    """Le VOYAGE_API_KEY com strip — Vercel as vezes cola \\n no final."""
    key = os.getenv("VOYAGE_API_KEY", "").strip()
    return key or None


async def embed(
    text: str,
    *,
    input_type: str = "document",
) -> Optional[List[float]]:
    """
    Gera embedding pra um texto. Retorna None se:
    - VOYAGE_API_KEY nao configurada
    - Provider retornou erro
    - Texto vazio

    `input_type` deve ser 'document' ao salvar memorias e 'query' ao buscar
    (Voyage usa isso pra otimizar assimetria query<->doc).

    Caller eh responsavel por fallback pra keyword search se retorno for None.
    """
    if not text or not text.strip():
        return None

    api_key = _get_api_key()
    if not api_key:
        logger.warning("embed() called but VOYAGE_API_KEY not set; returning None")
        return None

    text_norm = text.strip()
    cache_key = f"{input_type}::{text_norm}"

    # Cache hit
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        return _cache[cache_key]

    payload = {
        "input": text_norm,
        "model": VOYAGE_MODEL,
        "input_type": input_type,
        "output_dimension": VOYAGE_DIMS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=VOYAGE_TIMEOUT_S) as client:
            resp = await client.post(VOYAGE_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Resposta esperada: {"data": [{"embedding": [...]}], ...}
        embeddings = data.get("data") or []
        if not embeddings:
            logger.error(f"Voyage returned no embeddings for text: {text_norm[:60]!r}")
            return None
        vec = embeddings[0].get("embedding")
        if not isinstance(vec, list) or len(vec) != VOYAGE_DIMS:
            logger.error(
                f"Voyage returned malformed embedding (len={len(vec) if isinstance(vec, list) else 'N/A'})"
            )
            return None

        # Store in cache (with LRU eviction)
        _cache[cache_key] = vec
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
        return vec

    except httpx.HTTPStatusError as e:
        logger.error(f"Voyage HTTP {e.response.status_code}: {e.response.text[:200]}")
        return None
    except httpx.RequestError as e:
        logger.error(f"Voyage request error: {e}")
        return None
    except Exception as e:
        logger.error(f"embed() unexpected error: {e}")
        return None


def embed_sync(text: str, *, input_type: str = "document") -> Optional[List[float]]:
    """Versao sincrona pra contextos onde nao da pra await (scripts, save_memory).
    Usa httpx sync client. Mesmo cache compartilhado."""
    if not text or not text.strip():
        return None

    api_key = _get_api_key()
    if not api_key:
        logger.warning("embed_sync() called but VOYAGE_API_KEY not set")
        return None

    text_norm = text.strip()
    cache_key = f"{input_type}::{text_norm}"

    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        return _cache[cache_key]

    payload = {
        "input": text_norm,
        "model": VOYAGE_MODEL,
        "input_type": input_type,
        "output_dimension": VOYAGE_DIMS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=VOYAGE_TIMEOUT_S) as client:
            resp = client.post(VOYAGE_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        embeddings = data.get("data") or []
        if not embeddings:
            return None
        vec = embeddings[0].get("embedding")
        if not isinstance(vec, list) or len(vec) != VOYAGE_DIMS:
            return None
        _cache[cache_key] = vec
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
        return vec
    except Exception as e:
        logger.error(f"embed_sync error: {e}")
        return None


def embedding_to_pg_literal(vec: List[float]) -> str:
    """
    Converte um embedding (list[float]) pra literal aceito pelo pgvector.
    Pgvector aceita string '[0.1, 0.2, ...]'.

    Usar como param em prepared statement: cursor.execute(sql, (embedding_to_pg_literal(vec),))
    """
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def is_enabled() -> bool:
    """Indica se o servico esta utilizavel (api key configurada)."""
    return _get_api_key() is not None
