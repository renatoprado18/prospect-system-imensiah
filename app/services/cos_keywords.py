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
from typing import List, Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)


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

    Match ILIKE %keyword% case-insensitive. Retorna primeiro match (frente
    menor primeiro = peso maior). None se nenhum match ou texto vazio.
    """
    if not text or not isinstance(text, str):
        return None
    text_lc = text.lower()
    for frente, kw in _load_keywords():
        if kw.lower() in text_lc:
            return frente
    return None


def matching_keywords(text: Optional[str]) -> List[Tuple[int, str]]:
    """Retorna todos os (frente, keyword) que casam no texto. Util pra debug."""
    if not text or not isinstance(text, str):
        return []
    text_lc = text.lower()
    matches: List[Tuple[int, str]] = []
    for frente, kw in _load_keywords():
        if kw.lower() in text_lc:
            matches.append((frente, kw))
    return matches
