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
import re
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


def _load_keywords() -> List[Tuple[int, str]]:
    """Retorna [(frente, keyword), ...] ordenado por frente ASC (menor=mais prio)."""
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
            return [(r["frente"], r["keyword"]) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"cos_keywords._load_keywords falhou: {e}")
        return []


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
