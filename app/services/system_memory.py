"""
System memories — memórias persistentes do INTEL coach que não estão
atreladas a um contato específico. Cobre decisões, compromissos, padrões,
estados emocionais, e a síntese diária.

Why: o coach precisa lembrar do que ficou dito além da janela de mensagens.
Sem isso, cada conversa começa do zero. Ver project_life_coaching.md.

F6 (vector search): save_system_memory agora gera embedding via Voyage AI
quando VOYAGE_API_KEY esta configurada, e search_memories suporta tres
modos (keyword | semantic | hybrid). Hybrid eh o default na tool do bot —
combina keyword + semantic, dedup por id, mantendo ordem de keyword
primeiro (matches literais geralmente sao mais precisos quando existem).
"""
import json
import logging
from typing import Dict, List, Optional
from datetime import date, datetime, timedelta

from database import get_db
from services.embeddings import embed_sync, embedding_to_pg_literal, is_enabled as embeddings_enabled

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
    """Save a memory not tied to a specific contact. Returns new id or None.

    Tambem gera embedding via Voyage AI quando disponivel — falha de embedding
    NAO bloqueia o save (memoria fica sem embedding e cai no backfill depois).
    """
    if not titulo or not conteudo:
        return None
    try:
        # Tenta gerar embedding (best-effort; sem isso, cai pra keyword na busca)
        embedding_literal: Optional[str] = None
        if embeddings_enabled():
            text_for_embedding = f"{titulo}\n\n{conteudo}"
            try:
                vec = embed_sync(text_for_embedding, input_type="document")
                if vec:
                    embedding_literal = embedding_to_pg_literal(vec)
            except Exception as e:
                logger.warning(f"save_system_memory: embedding generation failed: {e}")

        with get_db() as conn:
            cursor = conn.cursor()
            if embedding_literal is not None:
                cursor.execute(
                    """
                    INSERT INTO system_memories (
                        titulo, conteudo, tipo, tags, fonte,
                        referencia_inicio, referencia_fim, embedding
                    )
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::vector)
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
                        embedding_literal,
                    ),
                )
            else:
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


def get_active_cos_config() -> Optional[Dict]:
    """Get the most recent active CoS config (prioridades + politicas + mandato).

    Alimenta briefing 7h, propostas de acao, triagem. Sem isso, sistema chuta
    prioridade. Atualizar via novo INSERT (mantemos historico — caller pega o
    mais recente).
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, titulo, conteudo, tags, criado_em
                FROM system_memories
                WHERE tipo = 'cos_config'
                ORDER BY criado_em DESC
                LIMIT 1
                """
            )
            r = cursor.fetchone()
            return dict(r) if r else None
    except Exception as e:
        logger.error(f"get_active_cos_config error: {e}")
        return None


# Buraco #3 (memória em 2 silos) — leitura reconciliada.
# `tonia_memories` é da tonIAH (repo/serviço separado): o MCP CoPiloto GRAVA
# fatos/decisões/regras lá (save_memory), mas a camada INTEL só lia
# `system_memories` — logo fatos salvos via MCP (ex.: "David Azzi", "parceria
# Jabô×Guaxupé") eram invisíveis pro frente_review/briefing. Aqui a leitura une
# os dois silos SEM tocar na escrita (INTEL NUNCA escreve em tonia_*). Read-only,
# degrada gracioso se a tabela não existir (dev local pode não ter). Kill-switch
# abaixo caso a tonIAH mude o schema e queira desligar sem redeploy de lógica.
_INCLUDE_TONIA = True
# ids de tonia_memories colidem com system_memories (ambos SERIAL de 1). Namespace
# em string evita colisão no dedup-por-id dos consumidores (frente_review, bot).
_TONIA_ID_PREFIX = "tonia:"


def _embed_query_literal(query: str) -> Optional[str]:
    """Embeda a query 1x (input_type='query') e devolve o literal pg::vector.
    Compartilhado pelas buscas semânticas dos 2 silos — 1 chamada Voyage só."""
    if not embeddings_enabled():
        return None
    try:
        vec = embed_sync(query, input_type="query")
        if not vec:
            return None
        return embedding_to_pg_literal(vec)
    except Exception as e:
        logger.error(f"_embed_query_literal error: {e}")
        return None


def _search_tonia_keyword(query: str, limit: int) -> List[Dict]:
    """Keyword (ILIKE) sobre key+value de tonia_memories. Mapeia pro mesmo
    schema de saída (titulo←key, conteudo←value, tipo←kind). Read-only."""
    if not _INCLUDE_TONIA:
        return []
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            like = f"%{query}%"
            cursor.execute(
                """
                SELECT id, key AS titulo, value AS conteudo, kind AS tipo,
                       created_at AS criado_em, NULL::float AS similarity
                FROM tonia_memories
                WHERE key ILIKE %s OR value ILIKE %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (like, like, limit),
            )
            rows = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        # tabela pode não existir (dev local) — degrada gracioso
        logger.warning(f"_search_tonia_keyword skip: {e}")
        return []
    for r in rows:
        r["id"] = f"{_TONIA_ID_PREFIX}{r['id']}"
    return rows


def _search_tonia_semantic(vec_literal: Optional[str], limit: int) -> List[Dict]:
    """Semantic sobre tonia_memories reusando o embedding já calculado. Read-only."""
    if not _INCLUDE_TONIA or not vec_literal:
        return []
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, key AS titulo, value AS conteudo, kind AS tipo,
                       created_at AS criado_em,
                       (1 - (embedding <=> %s::vector)) AS similarity
                FROM tonia_memories
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector ASC
                LIMIT %s
                """,
                (vec_literal, vec_literal, limit),
            )
            rows = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"_search_tonia_semantic skip: {e}")
        return []
    for r in rows:
        r["id"] = f"{_TONIA_ID_PREFIX}{r['id']}"
    return rows


def _merge_dedup(primary: List[Dict], extra: List[Dict], limit: int) -> List[Dict]:
    """Une duas listas de hits mantendo `primary` primeiro, dedup por id, teto limit."""
    seen = {r["id"] for r in primary}
    merged = list(primary)
    for r in extra:
        if r["id"] in seen:
            continue
        merged.append(r)
        seen.add(r["id"])
        if len(merged) >= limit:
            break
    return merged[:limit]


def _search_keyword(query: str, limit: int) -> List[Dict]:
    """Keyword search (ILIKE) sobre titulo + conteudo. Une system_memories +
    tonia_memories (leitura reconciliada, buraco #3)."""
    sys_rows: List[Dict] = []
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            like = f"%{query}%"
            cursor.execute(
                """
                SELECT id, titulo, conteudo, tipo, criado_em,
                       NULL::float AS similarity
                FROM system_memories
                WHERE titulo ILIKE %s OR conteudo ILIKE %s
                ORDER BY criado_em DESC
                LIMIT %s
                """,
                (like, like, limit),
            )
            sys_rows = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"_search_keyword error: {e}")
    return _merge_dedup(sys_rows, _search_tonia_keyword(query, limit), limit)


def _search_semantic(query: str, limit: int) -> List[Dict]:
    """Semantic search via pgvector cosine distance sobre os 2 silos.

    Retorna [] se VOYAGE_API_KEY nao configurada ou Voyage falhar — caller
    decide se faz fallback (em hybrid o fallback eh natural). Une system_memories
    + tonia_memories reusando 1 embedding só (buraco #3)."""
    vec_literal = _embed_query_literal(query)
    if not vec_literal:
        return []
    sys_rows: List[Dict] = []
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # cosine distance (<=>) varia 0 (igual) a 2 (oposto). similarity = 1 - dist/2
            # mas pra ranking simples basta o ORDER BY <=> ASC.
            cursor.execute(
                """
                SELECT id, titulo, conteudo, tipo, criado_em,
                       (1 - (embedding <=> %s::vector)) AS similarity
                FROM system_memories
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector ASC
                LIMIT %s
                """,
                (vec_literal, vec_literal, limit),
            )
            sys_rows = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"_search_semantic error: {e}")
    # merge por similaridade decrescente (não system-first) — em semântica o
    # score é comparável entre os silos, então ordenamos por ele.
    tonia_rows = _search_tonia_semantic(vec_literal, limit)
    merged = _merge_dedup(sys_rows, tonia_rows, limit * 2)
    merged.sort(key=lambda r: (r.get("similarity") is not None, r.get("similarity") or 0.0),
                reverse=True)
    return merged[:limit]


# Quantos resultados do topo contam como "puxada". Ver migration 070: contar
# TODOS os retornados inflaria o ruido — o que sempre volta em 5o lugar
# acumularia contador e o auto-aparo passaria a proteger o que nao serve.
RECALL_TOP_N = 3


def _registrar_recall(rows: List[Dict], via: str = "intel") -> None:
    """Marca no banco que estas memorias voltaram no topo de uma busca.

    Best-effort DE PROPOSITO: telemetria nunca derruba a leitura que a produz.
    Falhou, engole — perde-se um ponto de dado, nao a resposta do usuario.

    So conta `system_memories` (ids inteiros). O silo `tonia_memories` entra na
    mesma lista com id `tonia:N` e fica de fora: e outra tabela, com outro ciclo
    de vida, e o auto-aparo do MEMORY.md nao decide nada sobre ela.
    """
    ids = []
    for r in rows[:RECALL_TOP_N]:
        rid = r.get("id")
        if isinstance(rid, int):
            ids.append(rid)
    if not ids:
        return
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE system_memories
                   SET recall_count = recall_count + 1,
                       ultimo_recall_em = NOW(),
                       ultimo_recall_via = %s
                 WHERE id = ANY(%s)
                """,
                (via, ids),
            )
            conn.commit()
    except Exception as e:
        logger.debug(f"_registrar_recall (ignorado): {e}")


def search_memories(query: str, limit: int = 10, mode: str = "hybrid") -> List[Dict]:
    """
    Busca memorias persistentes.

    mode:
    - 'keyword': ILIKE sobre titulo+conteudo (legado, exato)
    - 'semantic': vector search via Voyage embeddings (sinonimos, parafraseamentos)
    - 'hybrid' (default): roda os dois e merge dedupe — keyword primeiro
      (matches literais sao mais precisos), depois semantic preenchendo o resto.
      Se VOYAGE_API_KEY nao configurada, hybrid degrada graciosamente pra keyword.
    """
    if not query or not query.strip():
        return []
    query = query.strip()
    mode = (mode or "hybrid").lower()

    if mode == "keyword":
        out = _search_keyword(query, limit)
        _registrar_recall(out)
        return out

    if mode == "semantic":
        out = _search_semantic(query, limit)
        _registrar_recall(out)
        return out

    # hybrid
    kw_results = _search_keyword(query, limit)
    sem_results = _search_semantic(query, limit)

    seen_ids = {r["id"] for r in kw_results}
    merged = list(kw_results)
    for r in sem_results:
        if r["id"] in seen_ids:
            continue
        merged.append(r)
        seen_ids.add(r["id"])
        if len(merged) >= limit:
            break
    out = merged[:limit]
    _registrar_recall(out)
    return out
