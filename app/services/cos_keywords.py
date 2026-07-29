"""CoS frente keywords — lookup table.

Bloco 2 (E1 zumbi + 2.X filtro assunto de interesse). Usado por:
- notification_router._rule_frente_keyword_match()
- cos_investigator drift detection (frente 1)
- Outros consumidores futuros que queiram priorizar por frente

Estratégia: ILIKE %keyword% case-insensitive contra texto. Retorna a
frente do PRIMEIRO match (frente menor = peso maior por convenção v5).
"""
from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from typing import List, Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)

# Keywords curtas (<= esse tamanho) exigem word boundary pra evitar FP
# tipo "Emma" batendo "clubebemmais" ou "ata" batendo "data" / "RACI"
# batendo "racial". Calibracao 10/06/26.
_SHORT_KEYWORD_MAX_LEN = 5


def _strip_accents(s: str) -> str:
    """Remove acentos pra match acento-agnostico. 'Estratégico' -> 'Estrategico'."""
    if not s:
        return s
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _keyword_matches(kw_norm: str, text_norm: str) -> bool:
    """Substring match com word boundary pra keywords curtas (<=5 chars).
    Long keywords continuam ILIKE %kw% (substring). Curtas exigem \\b...\\b
    pra evitar FP (ex: 'Emma' nao deve bater 'clubebemmais')."""
    if len(kw_norm) <= _SHORT_KEYWORD_MAX_LEN:
        # \b funciona com chars alfanumericos
        pattern = r"\b" + re.escape(kw_norm) + r"\b"
        return bool(re.search(pattern, text_norm))
    return kw_norm in text_norm


# ============================================================================
# CACHE DAS KEYWORDS (29/07/2026) — era 1 CONEXAO AO BANCO POR CHAMADA.
# ============================================================================
# `_load_keywords` abria conexao + query e era chamada DENTRO de
# `is_frente_keyword`, que roda uma vez por item do chamador. Em
# `circulos.get_prioridades_por_contexto` isso da uma conexao por contato:
# medido 57 keywords contra 1.586 contatos = 1.586 conexoes ao Neon por request.
#
# Era a causa dos 31s de /api/contacts/needs-attention (medido 3x: 31,5 / 31,1 /
# 30,4s), o spinner eterno da home. As queries em si sao rapidas — a soma delas
# da ~15ms; o custo era 100% abertura de conexao. Da maquina local, com SSL +
# latencia por conexao, a mesma chamada passava de 7 MINUTOS.
#
# Nao e so essa tela: `notification_router` e `email_triage` tambem chamam
# is_frente_keyword em hot path.
#
# TTL curto em vez de lru_cache permanente: a tabela e editavel e nao quero
# depender de achar todo ponto de escrita pra invalidar. 60s zera o N+1 dentro
# de um request e mantem a edicao visivel em menos de um minuto.
# COS_KEYWORDS_CACHE_TTL=0 desliga o cache (volta ao comportamento anterior).
_KEYWORDS_CACHE: Optional[List[Tuple[int, str]]] = None
_KEYWORDS_CACHE_AT: float = 0.0


def _cache_ttl() -> float:
    raw = (os.getenv("COS_KEYWORDS_CACHE_TTL") or "").strip()
    if not raw:
        return 60.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(f"COS_KEYWORDS_CACHE_TTL invalido: {raw!r} — usando 60s")
        return 60.0


def invalidate_keywords_cache() -> None:
    """Zera o cache. Chamar depois de inserir/editar/remover frente_keywords."""
    global _KEYWORDS_CACHE, _KEYWORDS_CACHE_AT
    _KEYWORDS_CACHE = None
    _KEYWORDS_CACHE_AT = 0.0


def _load_keywords() -> List[Tuple[int, str]]:
    """Retorna [(frente, keyword), ...] ordenado por frente ASC (menor=mais prio).

    Cacheado por TTL — ver bloco acima. Falha de DB devolve o cache velho se
    houver (melhor keyword defasada que nenhuma), senao lista vazia.
    """
    global _KEYWORDS_CACHE, _KEYWORDS_CACHE_AT

    ttl = _cache_ttl()
    if ttl > 0 and _KEYWORDS_CACHE is not None:
        if (time.monotonic() - _KEYWORDS_CACHE_AT) < ttl:
            return _KEYWORDS_CACHE

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT frente, keyword
                FROM frente_keywords
                ORDER BY frente ASC, length(keyword) DESC
                """
            )
            rows = [(r["frente"], r["keyword"]) for r in cur.fetchall()]
        _KEYWORDS_CACHE = rows
        _KEYWORDS_CACHE_AT = time.monotonic()
        return rows
    except Exception as e:
        logger.warning(f"cos_keywords._load_keywords falhou: {e}")
        # cache velho > nada: o consumidor perde priorizacao por frente sem ele
        return _KEYWORDS_CACHE if _KEYWORDS_CACHE is not None else []


def is_frente_keyword(text: Optional[str]) -> Optional[int]:
    """Retorna o numero da frente (1-5) se text contem keyword.

    Match ILIKE %keyword% case-insensitive + acento-agnostico
    ('Estratégico' bate 'estrategico'). Retorna primeiro match (frente
    menor primeiro = peso maior). None se nenhum match ou texto vazio.
    """
    if not text or not isinstance(text, str):
        return None
    text_norm = _strip_accents(text).lower()
    for frente, kw in _load_keywords():
        kw_norm = _strip_accents(kw).lower()
        if _keyword_matches(kw_norm, text_norm):
            return frente
    return None


def matching_keywords(text: Optional[str]) -> List[Tuple[int, str]]:
    """Retorna todos os (frente, keyword) que casam no texto. Util pra debug."""
    if not text or not isinstance(text, str):
        return []
    text_norm = _strip_accents(text).lower()
    matches: List[Tuple[int, str]] = []
    for frente, kw in _load_keywords():
        kw_norm = _strip_accents(kw).lower()
        if _keyword_matches(kw_norm, text_norm):
            matches.append((frente, kw))
    return matches
