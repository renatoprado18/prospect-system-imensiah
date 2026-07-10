"""
db.py — Camada de dados do CoPiloto MCP server.

Conexao PROPRIA (psycopg2) lendo `DATABASE_URL` do env. NAO importa o db.py /
database.py do INTEL de proposito — assim o MCP nao acopla com DB_TARGET e roda
isolado (local em dev, Neon em prod, controlado por env).

Disciplina de leitura/escrita (ver docs/MCP_SERVER_DESIGN.md + COPILOT_CONTRACT.md):
  - LEITURA  -> views `copilot.*` sempre que existirem (contrato estavel).
               Excecoes hoje sem view: `projects` e `documentos` -> lidas das
               tabelas fisicas ate existirem copilot.projects / copilot.documents.
  - ESCRITA  -> tabelas fisicas, com whitelist de campos + INSERT em mcp_audit_log.
               Catalogo fechado de atuadores. Nunca SQL cru vindo do modelo.

Single-tenant: assume Renato (RENATO_CONTACT_ID, default 25613).

Esta camada e testavel standalone (sem a lib `mcp`): basta DATABASE_URL setada.
"""

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

logger = logging.getLogger("copilot_mcp.db")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    """Le env var com strip (Vercel/shell as vezes colam \\n)."""
    return (os.getenv(name) or default).strip()


DATABASE_URL = _env("DATABASE_URL", "postgresql://rap@localhost/intel")
CONSELHOOS_DATABASE_URL = _env("CONSELHOOS_DATABASE_URL")
RENATO_CONTACT_ID = int(_env("RENATO_CONTACT_ID", "25613") or "25613")

# Voyage (embeddings semanticos) — reutiliza o mesmo padrao de app/services/embeddings.py
VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-4-lite"
VOYAGE_DIMS = 1024
VOYAGE_TIMEOUT_S = 15.0


def _voyage_key() -> Optional[str]:
    """Chave Voyage: env VOYAGE_API_KEY (pa-...) e o caminho canonico.
    Fallback: ~/.voyage_atlas_key (al-...) como o memory da Tonia usa."""
    key = _env("VOYAGE_API_KEY")
    if key:
        return key
    try:
        with open(os.path.expanduser("~/.voyage_atlas_key")) as fh:
            k = fh.read().strip()
            return k or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pool de conexao (INTEL DB)
# ---------------------------------------------------------------------------
_pool: Optional[SimpleConnectionPool] = None


def _get_pool() -> SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = SimpleConnectionPool(1, 5, dsn=DATABASE_URL)
        logger.info("MCP db pool aberto: %s", DATABASE_URL.split("@")[-1])
    return _pool


@contextmanager
def _conn():
    """Conexao do pool. Commit no sucesso, rollback na excecao."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    r = _rows(sql, params)
    return r[0] if r else None


# ---------------------------------------------------------------------------
# Audit — toda escrita passa por aqui
# ---------------------------------------------------------------------------
def _audit(cur, tool: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
    cur.execute(
        "INSERT INTO mcp_audit_log (tool, args, result) VALUES (%s, %s, %s)",
        (tool, json.dumps(args, default=str), json.dumps(result, default=str)),
    )


# ===========================================================================
# EMBEDDINGS (para search_memories semantica; degrade -> keyword)
# ===========================================================================
def embed(text: str, *, input_type: str = "query") -> Optional[List[float]]:
    """Gera embedding voyage-4-lite (1024d). Retorna None se sem chave/erro
    (caller deve cair pra keyword search)."""
    if not text or not text.strip():
        return None
    key = _voyage_key()
    if not key:
        logger.warning("embed(): sem VOYAGE_API_KEY/atlas key; keyword fallback")
        return None
    try:
        import httpx  # lazy — nao obriga dep se so usa keyword

        payload = {
            "input": text.strip(),
            "model": VOYAGE_MODEL,
            "input_type": input_type,
            "output_dimension": VOYAGE_DIMS,
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=VOYAGE_TIMEOUT_S) as client:
            resp = client.post(VOYAGE_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        vecs = data.get("data") or []
        if not vecs:
            return None
        vec = vecs[0].get("embedding")
        if not isinstance(vec, list) or len(vec) != VOYAGE_DIMS:
            return None
        return vec
    except Exception as e:
        logger.error("embed() erro: %s", e)
        return None


def _pg_vector(vec: List[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# ===========================================================================
# READS — via copilot.* (excecoes fisicas anotadas)
# ===========================================================================
def search_projects(query: Optional[str] = None, status: Optional[str] = None,
                    limit: int = 30) -> List[Dict[str, Any]]:
    # NOTA: copilot.projects NAO existe ainda -> lendo public.projects direto.
    where, params = [], []
    if query:
        where.append("(nome ILIKE %s OR descricao ILIKE %s OR empresa_relacionada ILIKE %s)")
        like = f"%{query}%"
        params += [like, like, like]
    if status:
        where.append("status = %s")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT id, nome, descricao, tipo, status, prioridade,
               data_inicio, data_previsao, empresa_relacionada, valor_estimado,
               tags, criado_em, atualizado_em
        FROM projects {clause}
        ORDER BY prioridade ASC, atualizado_em DESC
        LIMIT %s
    """
    return _rows(sql, tuple(params) + (limit,))


def get_project(project_id: int) -> Optional[Dict[str, Any]]:
    """Projeto + tasks + notas + documentos (visao 360 pra trabalhar)."""
    proj = _one(
        """SELECT id, nome, descricao, tipo, status, prioridade, data_inicio,
                  data_previsao, data_conclusao, empresa_relacionada, valor_estimado,
                  notas, tags, metadata, criado_em, atualizado_em
           FROM projects WHERE id = %s""",
        (project_id,),
    )
    if not proj:
        return None
    proj["tasks"] = _rows(
        """SELECT id, titulo, descricao, status, prioridade, due_date, completed_at,
                  contact_id, criado_em, atualizado_em
           FROM copilot.tasks WHERE project_id = %s
           ORDER BY (status = 'completed'), due_date NULLS LAST, prioridade""",
        (project_id,),
    )
    proj["notes"] = _rows(
        """SELECT id, tipo, titulo, conteudo, autor, criado_em
           FROM project_notes WHERE project_id = %s
           ORDER BY criado_em DESC LIMIT 50""",
        (project_id,),
    )
    proj["documents"] = get_project_documents(project_id)
    return proj


def search_tasks(project_id: Optional[int] = None, status: Optional[str] = None,
                due_before: Optional[str] = None, contact_id: Optional[int] = None,
                limit: int = 50) -> List[Dict[str, Any]]:
    where, params = [], []
    if project_id is not None:
        where.append("project_id = %s"); params.append(project_id)
    if status:
        where.append("status = %s"); params.append(status)
    if due_before:
        where.append("due_date <= %s"); params.append(due_before)
    if contact_id is not None:
        where.append("contact_id = %s"); params.append(contact_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT id, titulo, descricao, status, prioridade, due_date, completed_at,
               contact_id, project_id, criado_em, atualizado_em
        FROM copilot.tasks {clause}
        ORDER BY due_date NULLS LAST, prioridade ASC
        LIMIT %s
    """
    return _rows(sql, tuple(params) + (limit,))


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    return _one(
        """SELECT id, titulo, descricao, status, prioridade, due_date, completed_at,
                  contact_id, project_id, source_table, source_id, criado_em, atualizado_em
           FROM copilot.tasks WHERE id = %s""",
        (task_id,),
    )


def search_contacts(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    like = f"%{query}%"
    return _rows(
        """SELECT id, nome, apelido, empresa, cargo, emails, telefones, circulo,
                  tags, health_score, ultimo_contato, resumo_ai
           FROM copilot.contacts
           WHERE nome ILIKE %s OR apelido ILIKE %s OR empresa ILIKE %s OR cargo ILIKE %s
           ORDER BY circulo ASC NULLS LAST, health_score DESC NULLS LAST
           LIMIT %s""",
        (like, like, like, like, limit),
    )


def get_contact(contact_id: int) -> Optional[Dict[str, Any]]:
    contact = _one(
        """SELECT id, nome, apelido, empresa, cargo, emails, telefones, circulo,
                  tags, health_score, ultimo_contato, resumo_ai, criado_em, atualizado_em
           FROM copilot.contacts WHERE id = %s""",
        (contact_id,),
    )
    if not contact:
        return None
    contact["recent_messages"] = _rows(
        """SELECT id, canal, direction, content, timestamp
           FROM copilot.messages WHERE contact_id = %s
           ORDER BY timestamp DESC NULLS LAST LIMIT 15""",
        (contact_id,),
    )
    return contact


def get_project_documents(project_id: int) -> List[Dict[str, Any]]:
    # NOTA: sem copilot.documents -> public.documentos via documento_links.
    # Vinculo projeto usa entidade_tipo = 'projeto'.
    return _rows(
        """SELECT d.id, d.nome, d.descricao, d.tags, d.google_drive_url,
                  d.mime_type, d.indexado_em, d.atualizado_em
           FROM documentos d
           JOIN documento_links dl ON dl.documento_id = d.id
           WHERE dl.entidade_tipo = 'projeto' AND dl.entidade_id = %s
           ORDER BY d.indexado_em DESC""",
        (project_id,),
    )


def get_document(document_id: int) -> Optional[Dict[str, Any]]:
    doc = _one(
        """SELECT id, nome, descricao, tags, google_drive_url, mime_type,
                  tamanho_bytes, indexado_em, atualizado_em
           FROM documentos WHERE id = %s""",
        (document_id,),
    )
    if not doc:
        return None
    doc["links"] = _rows(
        "SELECT entidade_tipo, entidade_id FROM documento_links WHERE documento_id = %s",
        (document_id,),
    )
    return doc


def search_memories(query: str, k: int = 6) -> List[Dict[str, Any]]:
    """Semantica (voyage cosine em copilot.memories) com fallback keyword."""
    vec = embed(query, input_type="query")
    if vec is not None:
        try:
            return _rows(
                """SELECT id, name, content, type, tags, source, created_at,
                          (embedding <=> %s::vector) AS distance
                   FROM copilot.memories
                   WHERE embedding IS NOT NULL
                   ORDER BY embedding <=> %s::vector
                   LIMIT %s""",
                (_pg_vector(vec), _pg_vector(vec), k),
            )
        except Exception as e:
            logger.error("busca semantica falhou (%s); keyword fallback", e)
    # Fallback keyword
    like = f"%{query}%"
    return _rows(
        """SELECT id, name, content, type, tags, source, created_at
           FROM copilot.memories
           WHERE name ILIKE %s OR content ILIKE %s
           ORDER BY created_at DESC LIMIT %s""",
        (like, like, k),
    )


def get_cockpit() -> Dict[str, Any]:
    """Percepcao: signals abertos + tasks vencidas + agenda proximas 24h."""
    signals = _rows(
        """SELECT id, tipo, urgencia, contexto, detector, status, criado_em
           FROM copilot.signals WHERE status = 'open'
           ORDER BY urgencia DESC, criado_em DESC LIMIT 30"""
    )
    overdue = _rows(
        """SELECT id, titulo, status, prioridade, due_date, project_id, contact_id
           FROM copilot.tasks
           WHERE status = 'pending' AND due_date IS NOT NULL
             AND due_date < (now() AT TIME ZONE 'UTC')
           ORDER BY due_date ASC LIMIT 30"""
    )
    # calendar_events sao BRT naive (excecao do contrato) -> comparar em BRT
    agenda = _rows(
        """SELECT id, titulo, start_datetime, end_datetime, all_day, location, status, contact_id
           FROM copilot.calendar_events
           WHERE status <> 'cancelled'
             AND start_datetime >= (now() AT TIME ZONE 'America/Sao_Paulo')
             AND start_datetime <  (now() AT TIME ZONE 'America/Sao_Paulo') + interval '24 hours'
           ORDER BY start_datetime ASC LIMIT 20"""
    )
    return {
        "open_signals": signals,
        "overdue_tasks": overdue,
        "agenda_24h": agenda,
        "counts": {
            "open_signals": len(signals),
            "overdue_tasks": len(overdue),
            "agenda_24h": len(agenda),
        },
    }


# ---- ConselhoOS (read-only, DB separado) ----------------------------------
def get_conselho(empresa: Optional[str] = None) -> Dict[str, Any]:
    """Reunioes + RACI + decisoes do ConselhoOS. Read-only (outro Neon).
    Fallback {} vazio se CONSELHOOS_DATABASE_URL nao setada."""
    if not CONSELHOOS_DATABASE_URL:
        return {"empresas": [], "reunioes": [], "raci": [], "decisoes": [],
                "note": "CONSELHOOS_DATABASE_URL nao configurada"}
    conn = psycopg2.connect(CONSELHOOS_DATABASE_URL)
    try:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            emp_where, emp_params = "", []
            if empresa:
                emp_where = "WHERE nome ILIKE %s OR nome_fantasia ILIKE %s OR razao_social ILIKE %s"
                emp_params = [f"%{empresa}%"] * 3
            cur.execute(
                f"SELECT id, nome, setor FROM empresas {emp_where} ORDER BY nome",
                tuple(emp_params),
            )
            empresas = [dict(r) for r in cur.fetchall()]
            emp_ids = [e["id"] for e in empresas]
            if not emp_ids:
                return {"empresas": [], "reunioes": [], "raci": [], "decisoes": []}

            cur.execute(
                """SELECT id, empresa_id, titulo, data, status
                   FROM reunioes WHERE empresa_id = ANY(%s::uuid[])
                   ORDER BY data DESC LIMIT 30""",
                (emp_ids,),
            )
            reunioes = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """SELECT id, empresa_id, area, acao, prazo, status,
                          responsavel_r, responsavel_a
                   FROM raci_itens WHERE empresa_id = ANY(%s::uuid[])
                   ORDER BY prazo ASC LIMIT 60""",
                (emp_ids,),
            )
            raci = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """SELECT id, empresa_id, codigo, titulo, urgencia, status, opcao_escolhida
                   FROM decisoes WHERE empresa_id = ANY(%s::uuid[])
                   ORDER BY created_at DESC LIMIT 40""",
                (emp_ids,),
            )
            decisoes = [dict(r) for r in cur.fetchall()]
        return {"empresas": empresas, "reunioes": reunioes,
                "raci": raci, "decisoes": decisoes}
    finally:
        conn.close()


# ===========================================================================
# WRITES — tabelas fisicas, whitelist de campos, + audit
# ===========================================================================
def create_task(titulo: str, project_id: Optional[int] = None,
                due_date: Optional[str] = None, descricao: Optional[str] = None) -> Dict[str, Any]:
    if not titulo or not titulo.strip():
        raise ValueError("titulo obrigatorio")
    args = {"titulo": titulo, "project_id": project_id,
            "due_date": due_date, "descricao": descricao}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO tasks (titulo, descricao, project_id, data_vencimento,
                                      origem, status, contexto)
                   VALUES (%s, %s, %s, %s, 'mcp_copilot', 'pending', 'professional')
                   RETURNING id, titulo, status, project_id, data_vencimento AS due_date""",
                (titulo.strip(), descricao, project_id, due_date),
            )
            row = dict(cur.fetchone())
            _audit(cur, "create_task", args, row)
    return row


_TASK_UPDATABLE = {
    "titulo": "titulo", "descricao": "descricao", "status": "status",
    "prioridade": "prioridade", "project_id": "project_id",
    "contact_id": "contact_id", "due_date": "data_vencimento",
}


def update_task(task_id: int, campos: Dict[str, Any]) -> Dict[str, Any]:
    """Atualiza campos whitelisted. `campos` = {status, titulo, descricao,
    due_date, project_id, prioridade, contact_id}. Marca data_conclusao ao
    completar."""
    sets, params = [], []
    for k, v in (campos or {}).items():
        col = _TASK_UPDATABLE.get(k)
        if col is None:
            raise ValueError(f"campo nao permitido em update_task: {k}")
        sets.append(f"{col} = %s"); params.append(v)
    if not sets:
        raise ValueError("nenhum campo valido pra atualizar")
    if campos.get("status") == "completed":
        sets.append("data_conclusao = (now() AT TIME ZONE 'UTC')")
    params.append(task_id)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""UPDATE tasks SET {', '.join(sets)} WHERE id = %s
                    RETURNING id, titulo, status, prioridade, project_id,
                              contact_id, data_vencimento AS due_date, data_conclusao AS completed_at""",
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"task {task_id} nao encontrada")
            row = dict(row)
            _audit(cur, "update_task", {"id": task_id, "campos": campos}, row)
    return row


def create_document(project_id: int, titulo: str, conteudo: str,
                   tipo: Optional[str] = None) -> Dict[str, Any]:
    """Cria documento e vincula ao projeto.
    NOTA schema: `documentos` nao tem colunas project_id/conteudo. Mapeamos:
      titulo   -> nome
      conteudo -> descricao (texto do doc)
      tipo     -> tags (['mcp:copilot', 'tipo:<tipo>'])
      vinculo  -> documento_links (entidade_tipo='projeto', entidade_id=project_id)"""
    if not titulo or not titulo.strip():
        raise ValueError("titulo obrigatorio")
    if project_id is None:
        raise ValueError("project_id obrigatorio")
    args = {"project_id": project_id, "titulo": titulo, "tipo": tipo,
            "conteudo_len": len(conteudo or "")}
    tags = ["mcp:copilot"]
    if tipo:
        tags.append(f"tipo:{tipo}")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,))
            if not cur.fetchone():
                raise ValueError(f"projeto {project_id} nao existe")
            cur.execute(
                """INSERT INTO documentos (nome, descricao, tags, mime_type)
                   VALUES (%s, %s, %s, 'text/markdown')
                   RETURNING id, nome, descricao, tags, indexado_em""",
                (titulo.strip(), conteudo, json.dumps(tags)),
            )
            doc = dict(cur.fetchone())
            cur.execute(
                """INSERT INTO documento_links (documento_id, entidade_tipo, entidade_id)
                   VALUES (%s, 'projeto', %s) RETURNING id""",
                (doc["id"], project_id),
            )
            doc["link_id"] = cur.fetchone()["id"]
            doc["project_id"] = project_id
            _audit(cur, "create_document", args,
                   {"document_id": doc["id"], "link_id": doc["link_id"]})
    return doc


def create_note(project_id: int, texto: str) -> Dict[str, Any]:
    if not texto or not texto.strip():
        raise ValueError("texto obrigatorio")
    if project_id is None:
        raise ValueError("project_id obrigatorio")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,))
            if not cur.fetchone():
                raise ValueError(f"projeto {project_id} nao existe")
            cur.execute(
                """INSERT INTO project_notes (project_id, tipo, conteudo, autor)
                   VALUES (%s, 'nota', %s, 'copilot_mcp')
                   RETURNING id, project_id, tipo, conteudo, autor, criado_em""",
                (project_id, texto.strip()),
            )
            row = dict(cur.fetchone())
            _audit(cur, "create_note", {"project_id": project_id, "texto_len": len(texto)},
                   {"note_id": row["id"]})
    return row


def save_memory(kind: str, key: str, value: str) -> Dict[str, Any]:
    """UPSERT em tonia_memories (chave = kind+key). Gera embedding se houver
    chave Voyage; senao grava sem (busca cai pra keyword)."""
    if not kind or not key or not value:
        raise ValueError("kind, key e value sao obrigatorios")
    vec = embed(value, input_type="document")
    vec_lit = _pg_vector(vec) if vec else None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO tonia_memories (kind, key, value, source, embedding, updated_at)
                   VALUES (%s, %s, %s, 'mcp_copilot', %s, now())
                   ON CONFLICT (kind, key) DO UPDATE
                     SET value = EXCLUDED.value,
                         embedding = COALESCE(EXCLUDED.embedding, tonia_memories.embedding),
                         updated_at = now()
                   RETURNING id, kind, key, (embedding IS NOT NULL) AS has_embedding""",
                (kind, key, value, vec_lit),
            )
            row = dict(cur.fetchone())
            _audit(cur, "save_memory", {"kind": kind, "key": key, "value_len": len(value)},
                   {"memory_id": row["id"], "has_embedding": row["has_embedding"]})
    return row
