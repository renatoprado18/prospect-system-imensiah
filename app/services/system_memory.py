"""
System memories — memórias persistentes do INTEL coach que não estão
atreladas a um contato específico. Cobre decisões, compromissos, padrões,
estados emocionais, e a síntese diária.

Why: o coach precisa lembrar do que ficou dito além da janela de mensagens.
Sem isso, cada conversa começa do zero. Ver project_life_coaching.md.
"""
import json
import logging
from typing import Dict, List, Optional
from datetime import date, datetime, timedelta

from database import get_db

logger = logging.getLogger(__name__)


def save_system_memory(
    titulo: str,
    conteudo: str,
    tipo: str = "reflexao",
    tags: Optional[List[str]] = None,
    fonte: str = "chat",
    referencia_inicio: Optional[date] = None,
    referencia_fim: Optional[date] = None,
) -> Optional[int]:
    """Save a memory not tied to a specific contact. Returns new id or None."""
    if not titulo or not conteudo:
        return None
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO system_memories (
                    titulo, conteudo, tipo, tags, fonte,
                    referencia_inicio, referencia_fim
                )
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                RETURNING id
                """,
                (
                    titulo[:500],
                    conteudo,
                    tipo,
                    json.dumps(tags or []),
                    fonte,
                    referencia_inicio,
                    referencia_fim,
                ),
            )
            mid = cursor.fetchone()["id"]
            conn.commit()
            return mid
    except Exception as e:
        logger.error(f"save_system_memory error: {e}")
        return None


def list_recent_memories(limit: int = 15, exclude_synthesis: bool = False) -> List[Dict]:
    """Return recent system memories, newest first."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            where = ""
            if exclude_synthesis:
                where = "WHERE tipo <> 'sintese_diaria'"
            cursor.execute(
                f"""
                SELECT id, titulo, conteudo, tipo, tags, fonte,
                       referencia_inicio, referencia_fim, criado_em
                FROM system_memories
                {where}
                ORDER BY criado_em DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"list_recent_memories error: {e}")
        return []


def get_latest_synthesis() -> Optional[Dict]:
    """Get the most recent daily synthesis (or None)."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, titulo, conteudo, referencia_inicio, referencia_fim, criado_em
                FROM system_memories
                WHERE tipo = 'sintese_diaria'
                ORDER BY criado_em DESC
                LIMIT 1
                """
            )
            r = cursor.fetchone()
            return dict(r) if r else None
    except Exception as e:
        logger.error(f"get_latest_synthesis error: {e}")
        return None


def search_memories(query: str, limit: int = 10) -> List[Dict]:
    """Keyword search over titulo + conteudo."""
    if not query:
        return []
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            like = f"%{query}%"
            cursor.execute(
                """
                SELECT id, titulo, conteudo, tipo, criado_em
                FROM system_memories
                WHERE titulo ILIKE %s OR conteudo ILIKE %s
                ORDER BY criado_em DESC
                LIMIT %s
                """,
                (like, like, limit),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"search_memories error: {e}")
        return []
