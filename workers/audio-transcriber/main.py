"""
INTEL Worker - Railway
Handles bot message processing, audio transcription, and image analysis.
Runs on Railway with no timeout limit.
"""
import os
import sys
import json
import logging
import httpx
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SP_TZ = ZoneInfo("America/Sao_Paulo")
DIAS_PT = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]


def _now_sp():
    return datetime.now(SP_TZ)


def _format_sp_datetime(dt: datetime = None) -> str:
    if dt is None:
        dt = _now_sp()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=SP_TZ)
    else:
        dt = dt.astimezone(SP_TZ)
    return f"{dt.strftime('%Y-%m-%d')} {DIAS_PT[dt.weekday()]} {dt.strftime('%H:%M')}"
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="INTEL Worker")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
CONSELHOOS_DATABASE_URL = os.getenv("CONSELHOOS_DATABASE_URL", "")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
INTEL_BOT_INSTANCE = os.getenv("INTEL_BOT_INSTANCE", "intel-bot")
INTEL_API_URL = os.getenv("INTEL_API_URL", "https://intel.almeida-prado.com")
WORKER_SECRET = os.getenv("WORKER_SECRET", "intel-audio-2026")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "audio-transcriber"}


@app.get("/debug-db")
async def debug_db():
    """Test database connectivity."""
    results = {}
    # Test INTEL DB
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) as total FROM contacts")
        results["intel"] = {"ok": True, "contacts": cursor.fetchone()["total"]}
        conn.close()
    except Exception as e:
        results["intel"] = {"ok": False, "error": str(e), "url_prefix": DATABASE_URL[:50] if DATABASE_URL else "EMPTY"}

    # Test ConselhoOS DB
    try:
        conn = psycopg.connect(CONSELHOOS_DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) as total FROM empresas")
        results["conselhoos"] = {"ok": True, "empresas": cursor.fetchone()["total"]}
        conn.close()
    except Exception as e:
        results["conselhoos"] = {"ok": False, "error": str(e)}

    return results


@app.post("/organize-empresa")
async def organize_empresa(request: Request):
    """
    Organize Drive folder + extract empresa data. No timeout limit.
    Called by ConselhoOS or directly.
    """
    data = await request.json()
    if data.get("secret") != WORKER_SECRET:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    empresa_id = data.get("empresa_id", "")
    folder_id = data.get("folder_id", "")
    access_token = data.get("access_token", "")

    if not folder_id:
        return JSONResponse(status_code=400, content={"error": "folder_id required"})

    logger.info(f"Organizing empresa {empresa_id}, folder {folder_id}")

    results = {"subfolders_created": [], "files_moved": [], "docs_read": 0, "extracted": None}

    try:
        # 1. List all items in folder
        headers = {"Authorization": f"Bearer {access_token}"}

        resp = await _drive_list(folder_id, headers)
        items = resp.get("files", [])
        logger.info(f"Found {len(items)} items in folder")

        folders = {f["name"].lower(): f["id"] for f in items if "folder" in f.get("mimeType", "")}
        files = [f for f in items if "folder" not in f.get("mimeType", "")]

        # 2. Create standard subfolders
        standard = ["Atas", "Documentos", "RACI", "Pauta Anual", "Financeiro", "Preparação"]
        for name in standard:
            if name.lower() not in folders:
                created = await _drive_create_folder(name, folder_id, headers)
                if created:
                    folders[name.lower()] = created["id"]
                    results["subfolders_created"].append(name)

        # 3. Move loose files to correct subfolders
        for f in files:
            name_lower = f["name"].lower()
            target = None

            if any(k in name_lower for k in ["ata", "minuta", "acta"]):
                target = folders.get("atas")
            elif any(k in name_lower for k in ["raci", "ação", "acao"]):
                target = folders.get("raci")
            elif any(k in name_lower for k in ["pauta", "agenda"]):
                target = folders.get("pauta anual")
            elif any(k in name_lower for k in ["dfin", "financ", "balancete", "dre", "balanço", "receita", "orçamento"]):
                target = folders.get("financeiro")
            elif any(k in name_lower for k in ["briefing", "preparação", "preparacao"]):
                target = folders.get("preparação")

            if target:
                moved = await _drive_move_file(f["id"], target, headers)
                if moved:
                    target_name = next((n for n, fid in folders.items() if fid == target), "?")
                    results["files_moved"].append({"name": f["name"], "to": target_name})

        # 4. Read Google Docs content for enrichment
        readable = ["application/vnd.google-apps.document", "application/vnd.google-apps.spreadsheet",
                     "application/vnd.google-apps.presentation"]

        # Also try to export .docx and .pptx files
        all_readable = [f for f in items if f.get("mimeType", "") in readable]

        # Scan subfolders too
        for fname, fid in folders.items():
            try:
                sub_resp = await _drive_list(fid, headers)
                for sf in sub_resp.get("files", []):
                    if sf.get("mimeType", "") in readable:
                        all_readable.append(sf)
            except Exception:
                pass

        doc_contents = []
        for doc in all_readable[:15]:
            try:
                content = await _drive_export_text(doc["id"], headers)
                if content:
                    doc_contents.append({"name": doc["name"], "content": content[:3000]})
                    results["docs_read"] += 1
            except Exception:
                pass

        # Also use file names for context
        file_list = "\n".join([f"[{f.get('folder', 'raiz') if 'folder' in f else 'raiz'}] {f['name']}" for f in items])

        # 5. Claude enrichment
        if doc_contents or items:
            doc_texts = "\n".join([f"\n--- {d['name']} ---\n{d['content']}" for d in doc_contents])

            prompt = f"""Analise os documentos desta empresa e extraia TODAS as informações.

EMPRESA: {data.get('empresa_nome', 'desconhecida')}

ARQUIVOS ({len(items)}):
{file_list}

CONTEÚDO DOS DOCUMENTOS LIDOS ({len(doc_contents)}):
{doc_texts or '(nenhum documento Google Docs encontrado)'}

Extraia APENAS JSON (sem markdown):
{{
  "setor": "setor de atuação",
  "descricao": "descrição em 2-3 frases",
  "contexto_md": "contexto detalhado em markdown: histórico, missão, valores, posicionamento, desafios",
  "pessoas": [
    {{"nome": "Nome", "cargo": "Cargo", "tipo": "socio|conselheiro|executivo|funcionario"}}
  ],
  "riscos": ["risco 1", "risco 2"],
  "plano_estrategico": "resumo do plano estratégico",
  "insights": {{
    "governanca": "estrutura, maturidade",
    "mercado": "setor, posicionamento",
    "financeiro": "se disponível",
    "operacional": "estrutura, processos"
  }}
}}

Extraia APENAS do que está nos documentos. NÃO invente."""

            async with httpx.AsyncClient(timeout=60.0) as client:
                ai_resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000,
                          "messages": [{"role": "user", "content": prompt}]}
                )

            if ai_resp.status_code == 200:
                text = ai_resp.json().get("content", [{}])[0].get("text", "")
                js = text.find("{")
                je = text.rfind("}") + 1
                if js >= 0:
                    extracted = json.loads(text[js:je])
                    results["extracted"] = extracted

                    # Update empresa in ConselhoOS DB
                    if CONSELHOOS_DATABASE_URL and empresa_id:
                        conn = psycopg.connect(CONSELHOOS_DATABASE_URL, row_factory=dict_row)
                        cursor = conn.cursor()

                        updates = []
                        values = []
                        if extracted.get("setor"):
                            updates.append("setor = %s")
                            values.append(extracted["setor"])
                        if extracted.get("descricao"):
                            updates.append("descricao = %s")
                            values.append(extracted["descricao"])
                        if extracted.get("contexto_md"):
                            updates.append("contexto_md = %s")
                            values.append(extracted["contexto_md"])
                        if extracted.get("insights"):
                            updates.append("insights_json = %s")
                            values.append(json.dumps(extracted["insights"]))
                        if extracted.get("pessoas"):
                            updates.append("pessoas_chave = %s")
                            values.append(json.dumps(extracted["pessoas"]))

                        if updates:
                            updates.append("updated_at = NOW()")
                            values.append(empresa_id)
                            cursor.execute(f"UPDATE empresas SET {', '.join(updates)} WHERE id = %s", values)
                            conn.commit()

                        # Create pessoas records
                        for p in extracted.get("pessoas", []):
                            if not p.get("nome"):
                                continue
                            cursor.execute("SELECT id FROM pessoas WHERE empresa_id = %s AND nome = %s", (empresa_id, p["nome"]))
                            if not cursor.fetchone():
                                cursor.execute(
                                    "INSERT INTO pessoas (id, empresa_id, nome, cargo) VALUES (gen_random_uuid(), %s, %s, %s)",
                                    (empresa_id, p["nome"], p.get("cargo", ""))
                                )
                        conn.commit()
                        conn.close()

        return results

    except Exception as e:
        logger.error(f"Organize empresa error: {e}")
        return {"error": str(e)}


async def _drive_list(folder_id: str, headers: dict) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://www.googleapis.com/drive/v3/files?q='{folder_id}'+in+parents+and+trashed=false&fields=files(id,name,mimeType)&pageSize=100",
            headers=headers)
        return resp.json() if resp.status_code == 200 else {"files": []}


async def _drive_create_folder(name: str, parent_id: str, headers: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://www.googleapis.com/drive/v3/files",
            headers={**headers, "Content-Type": "application/json"},
            json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]})
        return resp.json() if resp.status_code == 200 else None


async def _drive_move_file(file_id: str, target_folder: str, headers: dict) -> bool:
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Get current parents
        resp = await client.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=parents", headers=headers)
        if resp.status_code != 200:
            return False
        parents = resp.json().get("parents", [])
        # Move
        resp = await client.patch(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?addParents={target_folder}&removeParents={','.join(parents)}",
            headers=headers)
        return resp.status_code == 200


async def _drive_export_text(file_id: str, headers: dict) -> str | None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/plain",
            headers=headers)
        if resp.status_code == 200:
            return resp.text
        # Fallback: direct download
        resp = await client.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", headers=headers)
        return resp.text if resp.status_code == 200 else None


@app.post("/process-message")
async def process_message(request: Request):
    """
    Process bot message directly on Railway with full DB access.
    No timeout limit. Has access to INTEL + ConselhoOS databases.
    """
    data = await request.json()
    if data.get("secret") != WORKER_SECRET:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    phone = data.get("phone", "")
    content = data.get("content", "")
    message_id = data.get("message_id", "")

    if not phone or not content:
        return JSONResponse(status_code=400, content={"error": "missing phone or content"})

    logger.info(f"Processing bot message for {phone}: {content[:80]}")

    try:
        response = await _run_bot(phone, content, message_id)
        if response:
            await _send_response(phone, response)
        return {"status": "success", "response_length": len(response or "")}
    except Exception as e:
        logger.error(f"Bot processing error: {e}")
        await _send_response(phone, "Desculpa, tive um erro. Tenta de novo?")
        return {"status": "error", "error": str(e)}


# ==================== BOT ENGINE (runs on Railway) ====================

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")

BOT_TOOLS = [
    {
        "name": "web_search",
        "description": "Pesquisa na internet. Use para buscar informacoes atuais, noticias, dados de empresas, pessoas, etc.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "Termo de busca"}}, "required": ["query"]}
    },
    {
        "name": "fetch_url",
        "description": "Busca conteudo de uma URL (artigo, pagina web). Retorna titulo + texto extraido. Use para ler artigos, noticias, documentos online.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "summarize": {"type": "boolean", "description": "Se true, resume com IA"}}, "required": ["url"]}
    },
    {
        "name": "query_intel",
        "description": "SELECT no banco INTEL (contatos, mensagens, projetos, tarefas, memorias). Apenas SELECT. LIMIT 20.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "query_conselhoos",
        "description": "SELECT no banco ConselhoOS (empresas, reunioes, raci_itens, decisoes, pessoas). Apenas SELECT.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "execute_conselhoos",
        "description": "INSERT/UPDATE/DELETE no ConselhoOS. IDs UUID (gen_random_uuid()). IMPORTANTE: ao criar empresas, SEMPRE inclua user_id='115322753506978752025'. Tabelas: empresas (id,nome,setor,descricao,user_id,cor_hex), reunioes, raci_itens, decisoes, pessoas, documentos.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "manage_email",
        "description": (
            "Gerencia emails do Gmail. Acoes:\n"
            "- archive_non_urgent: arquiva emails nao-urgentes do inbox (filtra newsletters, notificacoes, spam)\n"
            "- list_inbox: lista emails recentes do inbox (limit?)\n"
            "- archive_by_subject: arquiva emails com assunto especifico (subject_contains)\n"
            "Parametros: {action, account? 'professional'|'personal'|'both', subject_contains?, limit?}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"}
            },
            "required": ["action", "params"]
        }
    },
    {
        "name": "execute_intel",
        "description": (
            "Executa acao no INTEL:\n"
            "- create_task: {titulo, descricao?, project_id?, contact_id?, data_vencimento? YYYY-MM-DD}\n"
            "- complete_task: {task_id}\n"
            "- save_note: {project_id, titulo, conteudo}\n"
            "- save_memory: {contact_id, titulo, resumo, tipo?}\n"
            "- save_feedback: {conteudo, tipo? bug|melhoria|ideia}\n"
            "- save_article: {project_id, url} — busca artigo, resume com IA, salva no projeto"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"}
            },
            "required": ["action", "params"]
        }
    }
]


def _db_query(url: str, sql: str, write: bool = False) -> str:
    """Execute SQL on a database."""
    if not url:
        return json.dumps({"erro": "Database URL nao configurada"})

    sql = sql.strip().rstrip(";").strip()
    sql_upper = sql.upper()

    if not write:
        if not sql_upper.startswith("SELECT"):
            return json.dumps({"erro": "Apenas SELECT permitido"})
        if "LIMIT" not in sql_upper:
            sql += " LIMIT 20"

    try:
        logger.info(f"DB query: {sql[:150]}")
        conn = psycopg.connect(url, row_factory=dict_row)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            if write:
                result = f"{cursor.rowcount} registro(s) afetado(s)"
                try:
                    rows = cursor.fetchall()
                    if rows:
                        result += "\n" + " | ".join(f"{k}: {v}" for k, v in dict(rows[0]).items())
                except Exception:
                    pass
                conn.commit()
                return json.dumps({"sucesso": True, "resultado": result}, ensure_ascii=False)
            else:
                rows = [dict(r) for r in cursor.fetchall()]
                if not rows:
                    return "Nenhum resultado"
                lines = []
                for i, row in enumerate(rows):
                    parts = [f"{k}: {str(v)[:200]}" for k, v in row.items() if v is not None]
                    lines.append(f"[{i+1}] " + " | ".join(parts))
                return f"{len(rows)} resultados:\n" + "\n".join(lines)
        finally:
            conn.close()
    except Exception as e:
        return json.dumps({"erro": str(e)})


def _audit_log(
    action_type: str,
    category: str,
    title: str,
    scope_ref: dict = None,
    payload: dict = None,
    undo_hint: str = None,
) -> None:
    """Inline P3 audit log para acoes do bot worker.

    Why: bot worker nao importa de app/services/ — codigo duplicado
    intencional. Sem isso, _execute_intel_action e _manage_email
    fazem mudancas de estado (Gmail, INTEL DB) sem trilha.
    """
    if not DATABASE_URL:
        return
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO agent_actions
                (action_type, category, title, scope_ref, source, payload, undo_hint)
            VALUES (%s, %s, %s, %s, 'intel_bot.worker', %s, %s)
        """, (
            action_type,
            category,
            title,
            json.dumps(scope_ref or {}),
            json.dumps(payload) if payload else None,
            undo_hint,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"audit_log failed ({action_type}): {e}")


async def _execute_intel_action(action: str, params: dict) -> str:
    """Execute an INTEL CRM action."""
    if not DATABASE_URL:
        return json.dumps({"erro": "DATABASE_URL nao configurada"})

    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()

        if action == "create_task":
            dv = params.get("data_vencimento")
            if dv:
                try:
                    dv = datetime.strptime(str(dv)[:10], "%Y-%m-%d")
                except Exception:
                    dv = None
            if not dv and params.get("prazo_dias"):
                dv = (_now_sp() + timedelta(days=params["prazo_dias"])).replace(tzinfo=None)

            # Validate foreign keys
            contact_id = params.get("contact_id")
            if contact_id:
                cursor.execute("SELECT id FROM contacts WHERE id = %s", (contact_id,))
                if not cursor.fetchone():
                    contact_id = None  # Invalid, skip

            project_id = params.get("project_id")
            if project_id:
                cursor.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
                if not cursor.fetchone():
                    project_id = None

            cursor.execute("""
                INSERT INTO tasks (titulo, descricao, project_id, contact_id, data_vencimento,
                    prioridade, ai_generated, origem, status)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'intel_bot', 'pending') RETURNING id
            """, (params.get("titulo"), params.get("descricao", ""), project_id,
                  contact_id, dv, params.get("prioridade", 5)))
            tid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            _audit_log('task_created_by_bot', 'tasks',
                       f"Tarefa criada via bot: {params.get('titulo', '')[:80]}",
                       scope_ref={'task_id': tid, 'project_id': project_id, 'contact_id': contact_id},
                       payload={'prazo_dias': params.get('prazo_dias')},
                       undo_hint=f"DELETE FROM tasks WHERE id={tid};")
            return f"Tarefa #{tid} criada: {params.get('titulo')}"

        elif action == "complete_task":
            cursor.execute("UPDATE tasks SET status='completed', data_conclusao=NOW() WHERE id=%s RETURNING titulo",
                          (params["task_id"],))
            r = cursor.fetchone()
            conn.commit()
            conn.close()
            if r:
                _audit_log('task_completed_by_bot', 'tasks',
                           f"Tarefa concluida via bot: {r['titulo'][:80]}",
                           scope_ref={'task_id': params['task_id']},
                           undo_hint=f"UPDATE tasks SET status='pending', data_conclusao=NULL WHERE id={params['task_id']};")
            return f"Tarefa concluida: {r['titulo']}" if r else "Tarefa nao encontrada"

        elif action == "save_note":
            cursor.execute("INSERT INTO project_notes (project_id, titulo, conteudo, tipo, autor) VALUES (%s,%s,%s,%s,'INTEL Bot') RETURNING id",
                          (params.get("project_id"), params.get("titulo", ""), params.get("conteudo", ""), params.get("tipo", "nota")))
            nid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            _audit_log('note_saved_by_bot', 'system',
                       f"Nota em projeto: {params.get('titulo', '')[:80]}",
                       scope_ref={'note_id': nid, 'project_id': params.get('project_id')},
                       undo_hint=f"DELETE FROM project_notes WHERE id={nid};")
            return f"Nota #{nid} salva"

        elif action == "save_memory":
            cursor.execute("INSERT INTO contact_memories (contact_id, titulo, resumo, tipo) VALUES (%s,%s,%s,%s) RETURNING id",
                          (params["contact_id"], params.get("titulo", ""), params.get("resumo", ""), params.get("tipo", "nota")))
            mid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            _audit_log('memory_saved_by_bot', 'contacts',
                       f"Memoria de contato: {params.get('titulo', '')[:80]}",
                       scope_ref={'memory_id': mid, 'contact_id': params['contact_id']},
                       undo_hint=f"DELETE FROM contact_memories WHERE id={mid};")
            return f"Memoria #{mid} salva"

        elif action == "save_feedback":
            cursor.execute("INSERT INTO system_feedback (tipo, conteudo) VALUES (%s,%s) RETURNING id",
                          (params.get("tipo", "feedback"), params.get("conteudo", "")))
            fid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            _audit_log('feedback_saved_by_bot', 'system',
                       f"Feedback {params.get('tipo', 'feedback')}: {(params.get('conteudo', ''))[:80]}",
                       scope_ref={'feedback_id': fid},
                       undo_hint=f"DELETE FROM system_feedback WHERE id={fid};")
            return f"Feedback #{fid} registrado"

        elif action == "save_article":
            conn.close()
            # Call the INTEL API to fetch, summarize, and save
            project_id = params.get("project_id")
            url = params.get("url", "")
            if not project_id or not url:
                return "project_id e url obrigatorios"
            try:
                async_resp = await _save_article_via_api(project_id, url)
                return async_resp
            except Exception as e:
                return f"Erro ao salvar artigo: {e}"

        conn.close()
        return f"Acao desconhecida: {action}"
    except Exception as e:
        return f"Erro: {e}"


async def _web_search(query: str) -> str:
    """Search the web via Brave Search API."""
    if not BRAVE_API_KEY:
        return "Web search indisponivel (BRAVE_API_KEY nao configurada)"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
                params={"q": query, "count": 5}
            )
        if resp.status_code != 200:
            return f"Erro na busca: {resp.status_code}"
        data = resp.json()
        results = data.get("web", {}).get("results", [])
        if not results:
            return "Nenhum resultado encontrado"
        lines = []
        for r in results[:5]:
            lines.append(f"**{r.get('title','')}**\n{r.get('description','')}\nURL: {r.get('url','')}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"Erro: {e}"


async def _fetch_url(url: str, summarize: bool = False) -> str:
    """Fetch and extract content from a URL."""
    import re
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return f"Erro HTTP {resp.status_code}"

        html = resp.text
        # Extract title
        title_match = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        title = title_match.group(1) if title_match else ""
        if not title:
            title_match = re.search(r'<title>([^<]+)</title>', html)
            title = title_match.group(1) if title_match else url

        # Extract text
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        article = re.search(r'<article[^>]*>(.*?)</article>', text, flags=re.DOTALL)
        if article:
            text = article.group(1)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()[:4000]

        result = f"**{title}**\n\n{text[:2000]}"

        if summarize and ANTHROPIC_API_KEY:
            async with httpx.AsyncClient(timeout=15.0) as client:
                ai_resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                          "messages": [{"role": "user", "content": f"Resuma em 3-4 frases:\n\n{title}\n{text[:3000]}"}]}
                )
            if ai_resp.status_code == 200:
                summary = ai_resp.json()["content"][0]["text"]
                result = f"**{title}**\n\n{summary}\n\nFonte: {url}"

        return result
    except Exception as e:
        return f"Erro ao buscar URL: {e}"


def _run_tool(name: str, input_data: dict) -> str:
    """Execute a bot tool (sync only - DB queries)."""
    if name == "query_intel":
        return _db_query(DATABASE_URL, input_data["sql"])
    elif name == "query_conselhoos":
        return _db_query(CONSELHOOS_DATABASE_URL, input_data["sql"])
    elif name == "execute_conselhoos":
        result = _db_query(CONSELHOOS_DATABASE_URL, input_data["sql"], write=True)
        # Auto-create INTEL project when empresa is created
        sql_upper = input_data.get("sql", "").upper()
        if "INSERT" in sql_upper and "EMPRESAS" in sql_upper:
            _auto_create_project_for_empresa(input_data["sql"], result)
        return result
    return "Tool desconhecida"


async def _get_gmail_token(account_type: str = "professional") -> tuple[str | None, str | None]:
    """Get fresh Gmail access token for an account."""
    if not DATABASE_URL:
        return None, None
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    cursor = conn.cursor()
    cursor.execute("SELECT email, access_token, refresh_token, token_expiry FROM google_accounts WHERE tipo = %s AND conectado = TRUE LIMIT 1", (account_type,))
    account = cursor.fetchone()
    conn.close()
    if not account:
        return None, None

    # Check if token is fresh
    if account.get('token_expiry') and account['token_expiry'] > datetime.now():
        return account['access_token'], account['email']

    # Refresh
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if not account.get('refresh_token'):
        return None, None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": account['refresh_token'], "grant_type": "refresh_token"
            })
        if resp.status_code == 200:
            new_token = resp.json()["access_token"]
            conn = psycopg.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("UPDATE google_accounts SET access_token = %s, token_expiry = NOW() + INTERVAL '1 hour' WHERE email = %s",
                          (new_token, account['email']))
            conn.commit()
            conn.close()
            return new_token, account['email']
    except Exception as e:
        logger.error(f"Gmail token refresh: {e}")
    return None, None


async def _manage_email(action: str, params: dict) -> str:
    """Manage Gmail emails (archive, list, etc.)."""
    account_type = params.get("account", "both")
    accounts_to_check = ["professional", "personal"] if account_type == "both" else [account_type]

    if action == "archive_non_urgent":
        total_archived = 0
        details = []

        for acct in accounts_to_check:
            token, email = await _get_gmail_token(acct)
            if not token:
                details.append(f"{acct}: token indisponível")
                continue

            try:
                # List inbox messages
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=in:inbox&maxResults=50",
                        headers={"Authorization": f"Bearer {token}"})
                    if resp.status_code != 200:
                        details.append(f"{acct}: erro ao listar")
                        continue
                    messages = resp.json().get("messages", [])

                # Get details and classify
                non_urgent_ids = []
                for msg in messages:
                    try:
                        detail = await client.get(
                            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject",
                            headers={"Authorization": f"Bearer {token}"})
                        if detail.status_code != 200:
                            continue
                        d = detail.json()
                        headers = {h['name']: h['value'] for h in d.get('payload', {}).get('headers', [])}
                        subject = (headers.get('Subject', '') or '').lower()
                        sender = (headers.get('From', '') or '').lower()

                        # Non-urgent patterns
                        is_non_urgent = any(p in subject or p in sender for p in [
                            'newsletter', 'digest', 'weekly', 'update', 'notification',
                            'noreply', 'no-reply', 'mailer-daemon', 'unsubscribe',
                            'linkedin', 'github', 'slack', 'notion', 'calendar',
                            'promoção', 'desconto', 'oferta', 'fatura', 'nfe',
                            'nota fiscal', 'boleto', 'comprovante', 'recibo'
                        ])
                        if is_non_urgent:
                            non_urgent_ids.append(msg['id'])
                    except Exception:
                        continue

                # Archive (remove INBOX label)
                if non_urgent_ids:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(
                            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                            json={"ids": non_urgent_ids, "removeLabelIds": ["INBOX"]})
                        if resp.status_code == 204:
                            total_archived += len(non_urgent_ids)
                            details.append(f"{email}: {len(non_urgent_ids)} arquivados")
                            _audit_log(
                                action_type='gmail_archived_non_urgent',
                                category='email',
                                title=f"Gmail: {len(non_urgent_ids)} emails nao-urgentes arquivados em {email}",
                                scope_ref={'account': acct, 'email': email},
                                payload={'message_ids': non_urgent_ids[:50]},
                                undo_hint=f"Gmail batchModify addLabelIds=['INBOX'] no token de {email} para os {len(non_urgent_ids)} ids em payload.message_ids",
                            )
                        else:
                            details.append(f"{email}: erro ao arquivar ({resp.status_code})")
                else:
                    details.append(f"{email}: nenhum não-urgente encontrado")

            except Exception as e:
                details.append(f"{acct}: {e}")

        return f"Arquivados: {total_archived} emails\n" + "\n".join(details)

    elif action == "archive_by_subject":
        subject_filter = params.get("subject_contains", "")
        if not subject_filter:
            return "Parâmetro subject_contains obrigatório"

        total_archived = 0
        for acct in accounts_to_check:
            token, email = await _get_gmail_token(acct)
            if not token:
                continue
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q=in:inbox+subject:{subject_filter}&maxResults=20",
                        headers={"Authorization": f"Bearer {token}"})
                    messages = resp.json().get("messages", [])
                    if messages:
                        ids = [m['id'] for m in messages]
                        await client.post(
                            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                            json={"ids": ids, "removeLabelIds": ["INBOX"]})
                        total_archived += len(ids)
                        _audit_log(
                            action_type='gmail_archived_by_subject',
                            category='email',
                            title=f"Gmail: {len(ids)} emails arquivados por subject '{subject_filter[:40]}' em {email}",
                            scope_ref={'account': acct, 'email': email},
                            payload={'subject_filter': subject_filter, 'message_ids': ids[:50]},
                            undo_hint=f"Gmail batchModify addLabelIds=['INBOX'] no token de {email} para os {len(ids)} ids em payload.message_ids",
                        )
            except Exception:
                pass

        return f"Arquivados {total_archived} emails com '{subject_filter}'"

    elif action == "list_inbox":
        limit = params.get("limit", 10)
        results = []
        for acct in accounts_to_check:
            token, email = await _get_gmail_token(acct)
            if not token:
                continue
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q=in:inbox&maxResults={limit}",
                        headers={"Authorization": f"Bearer {token}"})
                    messages = resp.json().get("messages", [])
                    for msg in messages[:limit]:
                        detail = await client.get(
                            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject",
                            headers={"Authorization": f"Bearer {token}"})
                        if detail.status_code == 200:
                            d = detail.json()
                            hdrs = {h['name']: h['value'] for h in d.get('payload', {}).get('headers', [])}
                            results.append(f"[{acct}] {hdrs.get('Subject','?')} — {hdrs.get('From','?')[:40]}")
            except Exception:
                pass
        return f"Inbox ({len(results)}):\n" + "\n".join(results) if results else "Inbox vazio"

    return f"Ação desconhecida: {action}"


async def _save_article_via_api(project_id: int, url: str) -> str:
    """Fetch, summarize, and save article to project via INTEL API."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{INTEL_API_URL}/api/projects/{project_id}/save-article",
                headers={"Content-Type": "application/json"},
                json={"url": url}
            )
        if resp.status_code == 200:
            data = resp.json()
            return f"Artigo salvo no projeto: {data.get('title', url)}\nResumo: {data.get('summary', '')[:300]}"
        else:
            # Fallback: save directly
            return await _save_article_direct(project_id, url)
    except Exception:
        return await _save_article_direct(project_id, url)


async def _save_article_direct(project_id: int, url: str) -> str:
    """Save article directly from worker (fallback if API fails)."""
    try:
        # Fetch
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return f"Erro HTTP {resp.status_code} ao buscar artigo"

        html = resp.text
        import re
        title_match = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        title = title_match.group(1) if title_match else url

        # Strip HTML
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()[:3000]

        # Summarize with Claude
        async with httpx.AsyncClient(timeout=20.0) as client:
            ai_resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 400,
                      "messages": [{"role": "user", "content": f"Resuma este artigo em português, 3-4 frases + pontos-chave:\n\nTÍTULO: {title}\n\n{text}"}]}
            )
        summary = ai_resp.json()["content"][0]["text"] if ai_resp.status_code == 200 else text[:300]

        # Save
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor, metadata)
            VALUES (%s, 'article', %s, %s, 'INTEL Bot', %s) RETURNING id
        """, (project_id, title, summary, json.dumps({"url": url})))
        nid = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return f"Artigo #{nid} salvo: {title}\nResumo: {summary[:300]}"
    except Exception as e:
        return f"Erro: {e}"


def _auto_create_project_for_empresa(sql: str, result: str):
    """When a ConselhoOS empresa is created, auto-create INTEL project."""
    if not DATABASE_URL:
        return
    try:
        # Extract empresa name from SQL (between quotes after nome)
        import re
        match = re.search(r"'([^']+)'", sql.split("nome" if "nome" in sql.lower() else ",")[0] + sql)
        # Better: query the empresa we just created
        conn_cos = psycopg.connect(CONSELHOOS_DATABASE_URL, row_factory=dict_row)
        cursor_cos = conn_cos.cursor()
        cursor_cos.execute("SELECT nome, setor, descricao, drive_folder_id FROM empresas ORDER BY created_at DESC LIMIT 1")
        emp = cursor_cos.fetchone()
        conn_cos.close()

        if not emp:
            return

        # Check if project already exists in INTEL
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM projects WHERE LOWER(nome) = LOWER(%s) LIMIT 1", (emp['nome'],))
        if cursor.fetchone():
            conn.close()
            return

        cursor.execute("""
            INSERT INTO projects (nome, descricao, tipo, status, google_drive_folder_id)
            VALUES (%s, %s, 'conselho', 'ativo', %s) RETURNING id
        """, (emp['nome'], emp.get('descricao') or f"Conselho consultivo - {emp.get('setor', '')}", emp.get('drive_folder_id')))
        pid = cursor.fetchone()['id']
        conn.commit()
        conn.close()
        logger.info(f"Auto-created INTEL project #{pid} for empresa {emp['nome']}")
    except Exception as e:
        logger.error(f"Auto-create project error: {e}")


def _load_history(phone: str, limit: int = 15) -> list:
    """Load conversation history from INTEL DB."""
    if not DATABASE_URL:
        return []
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM bot_conversations WHERE phone=%s ORDER BY created_at DESC LIMIT %s", (phone, limit))
        rows = list(reversed([dict(r) for r in cursor.fetchall()]))
        conn.close()
        return rows
    except Exception:
        return []


def _save_msg(phone: str, role: str, content: str):
    """Save message to conversation history."""
    if not content or not content.strip():
        return
    garbage = ['demorou demais', 'Erro interno', '__IMAGE_PENDING__', '__AUDIO_PENDING__',
               'indisponível', 'indisponivel', 'Não consigo acessar']
    if any(g in content for g in garbage):
        return
    if not DATABASE_URL:
        return
    try:
        conn = psycopg.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO bot_conversations (phone, role, content) VALUES (%s,%s,%s)", (phone, role, content))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _build_snapshot_block() -> str:
    """Snapshot situacional do INTEL — bot entra na conversa sabendo do estado atual.

    Why: P2 Inteligencia Real — bot reativo demais sem contexto. Reduz tool calls
    obvias e elimina performance theater ("aguarde, vou buscar...").
    """
    sections = []
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT t.id, t.titulo, t.data_vencimento::date AS due, p.nome AS projeto
            FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.status = 'pending' AND t.data_vencimento IS NOT NULL
              AND t.data_vencimento::date <= CURRENT_DATE
            ORDER BY t.data_vencimento ASC, t.prioridade ASC
            LIMIT 5
        """)
        tasks = cursor.fetchall()
        if tasks:
            lines = []
            for t in tasks:
                proj = f" — {t['projeto']}" if t['projeto'] else ""
                lines.append(f"  - [{t['id']}] {t['titulo'][:70]} (venc {t['due']}){proj}")
            sections.append("**Tarefas urgentes (<=hoje):**\n" + "\n".join(lines))

        cursor.execute("""
            SELECT id, summary, start_datetime
            FROM calendar_events
            WHERE start_datetime::date = CURRENT_DATE
              AND end_datetime >= NOW()
            ORDER BY start_datetime ASC
            LIMIT 5
        """)
        events = cursor.fetchall()
        if events:
            lines = [f"  - {e['start_datetime'].strftime('%H:%M')} {e['summary'][:70]}" for e in events]
            sections.append("**Agenda restante hoje:**\n" + "\n".join(lines))
        else:
            sections.append("**Agenda restante hoje:** vazio")

        cursor.execute("""
            SELECT id, nome, circulo, health_score, ultimo_contato::date AS ultimo
            FROM contacts
            WHERE circulo <= 2
              AND health_score IS NOT NULL
              AND health_score < 50
            ORDER BY health_score ASC, ultimo_contato ASC NULLS FIRST
            LIMIT 5
        """)
        cooling = cursor.fetchall()
        if cooling:
            lines = []
            for c in cooling:
                health = c['health_score'] if c['health_score'] is not None else 0
                ult = c['ultimo'] or 'nunca'
                lines.append(f"  - [{c['id']}] {c['nome']} (C{c['circulo']}, health {health}, ult {ult})")
            sections.append("**Contatos esfriando (C1-C2):**\n" + "\n".join(lines))

        cursor.execute("""
            SELECT
                (SELECT COUNT(*) FROM editorial_posts WHERE status = 'scheduled') AS scheduled,
                (SELECT COUNT(*) FROM editorial_posts WHERE status = 'draft') AS drafts,
                (SELECT COUNT(*) FROM hot_takes WHERE status = 'draft') AS hot_drafts,
                (SELECT data_publicacao FROM editorial_posts WHERE status = 'scheduled' ORDER BY data_publicacao ASC LIMIT 1) AS proximo
        """)
        ed = cursor.fetchone()
        if ed and (ed['scheduled'] or ed['drafts'] or ed['hot_drafts']):
            line = f"**Editorial:** {ed['scheduled']} agendados, {ed['drafts']} drafts, {ed['hot_drafts']} hot takes"
            if ed['proximo']:
                line += f" — proximo: {ed['proximo'].strftime('%d/%m %H:%M')}"
            sections.append(line)

        cursor.execute("""
            SELECT id, title, urgency
            FROM action_proposals
            WHERE status = 'pending'
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY CASE urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, criado_em DESC
            LIMIT 3
        """)
        props = cursor.fetchall()
        if props:
            lines = [f"  - [{p['id']}] {p['title'][:80]} ({p['urgency']})" for p in props]
            sections.append(f"**Propostas pendentes ({len(props)}):**\n" + "\n".join(lines))

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM email_triage
            WHERE status = 'pending' AND needs_attention = true
        """)
        row = cursor.fetchone()
        email_pending = row['total'] if row else 0
        if email_pending:
            sections.append(f"**Emails pendentes:** {email_pending}")

        conn.close()
    except Exception as e:
        logger.error(f"Error building snapshot block: {e}")
        return ""

    if not sections:
        return ""

    return "## SITUACAO ATUAL (snapshot — voce JA SABE disso, NAO precisa de tool call pra coisas obvias)\n\n" + "\n\n".join(sections) + "\n\n"


async def _run_bot(phone: str, message: str, message_id: str) -> str:
    """Full bot processing with tool_use loop. Runs on Railway (no timeout)."""
    now = _now_sp()
    snapshot = _build_snapshot_block()

    system_prompt = f"""Voce e o INTEL Bot, assistente pessoal de Renato Almeida Prado (executivo, tecnologia e governanca).

{snapshot}

TABELAS INTEL (nomes reais, use EXATAMENTE estes nomes):
- contacts: id, nome, empresa, cargo, circulo, health_score, telefones, emails, linkedin, ultimo_contato, resumo_ai
- messages: id, conversation_id, contact_id, direcao ('incoming'/'outgoing'), conteudo, enviado_em
- conversations: id, contact_id, canal ('whatsapp'/'email'), ultimo_mensagem
- projects: id, nome, descricao, tipo, status, prioridade, data_previsao
- tasks: id, titulo, descricao, status ('pending'/'completed'), data_vencimento, project_id, contact_id, prioridade
- contact_memories: id, contact_id, titulo, resumo, tipo, data_ocorrencia
- contact_facts: id, contact_id, categoria, fato
- calendar_events: id, summary, start_datetime, end_datetime
- project_notes: id, project_id, titulo, conteudo, tipo, criado_em
- action_proposals: id, contact_id, title, description, urgency, status

TABELAS CONSELHOOS:
- empresas: id (uuid), nome, setor, descricao, user_id (SEMPRE '115322753506978752025')
- reunioes: id (uuid), empresa_id, titulo, data, status, ata_md
- raci_itens: id (uuid), empresa_id, area, acao, prazo, status, responsavel_r
- decisoes: id, empresa_id, reuniao_id, decisao, area
- pessoas: id (uuid), empresa_id, nome, cargo, email, intel_contact_id

TOOLS:
- web_search: pesquisar na internet (noticias, empresas, pessoas, qualquer coisa)
- fetch_url: buscar conteudo de uma URL (artigo, pagina). Use summarize=true para resumir.
- query_intel: SELECT no banco INTEL. SEMPRE use nomes de tabela acima.
- query_conselhoos: SELECT no ConselhoOS.
- execute_conselhoos: INSERT/UPDATE/DELETE no ConselhoOS. IDs UUID (gen_random_uuid()).
- execute_intel: criar tarefas, salvar notas, memorias, feedback, salvar artigos.
- manage_email: gerenciar Gmail (archive_non_urgent, list_inbox, archive_by_subject).

EXEMPLOS SQL:
- Tarefas pendentes: SELECT id, titulo, data_vencimento FROM tasks WHERE status = 'pending' ORDER BY data_vencimento
- Contato por nome: SELECT id, nome, empresa FROM contacts WHERE nome ILIKE '%termo%'
- Projetos ativos: SELECT id, nome, tipo FROM projects WHERE status = 'ativo'
- Eventos de uma data: SELECT summary, start_datetime FROM calendar_events WHERE start_datetime::date = '2026-04-28'

REGRAS:
- NUNCA invente informacoes. Consulte antes de afirmar.
- NUNCA diga "Intel indisponivel". Se query falhar, tente de novo com SQL corrigido.
- NUNCA narre o processo: NADA de "buscando...", "aguarde um momento", "deixa eu verificar", "vou consultar". Se precisa de tool, chame e responda DIRETO com o resultado. Se ja tem no snapshot acima, responda ja com a info.
- Se a pergunta e sobre algo que ja esta no snapshot (tarefas hoje, agenda hoje, propostas, contatos esfriando, editorial), responda DIRETO sem tool call.
- Quando pedir para CRIAR no ConselhoOS, use execute_conselhoos com INSERT.
- Responda em portugues, conciso (WhatsApp). Use *negrito* para destaques.
- Data atual: {_format_sp_datetime(now)} (fuso America/Sao_Paulo, sempre)
- Para "segunda", "2a feira" = proximo dia util. Calcule a data.
- Audios transcritos: "[Audio transcrito] texto"
- Imagens analisadas: "[Imagem analisada] descricao"
- Feedback do sistema: use execute_intel save_feedback"""

    # Load history
    history = _load_history(phone)
    _save_msg(phone, "user", message)
    messages = [{"role": r["role"], "content": r["content"]} for r in history] + [{"role": "user", "content": message}]

    # Tool loop
    async with httpx.AsyncClient(timeout=30.0) as client:
        for iteration in range(10):
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1000,
                      "system": system_prompt, "tools": BOT_TOOLS, "messages": messages}
            )

            if resp.status_code != 200:
                logger.error(f"Claude error: {resp.status_code}")
                return None

            result = resp.json()
            text_parts = []
            tool_uses = []

            for block in result.get("content", []):
                if block["type"] == "text":
                    text_parts.append(block["text"])
                elif block["type"] == "tool_use":
                    tool_uses.append(block)

            if result.get("stop_reason") == "end_turn" or not tool_uses:
                response = "\n".join(text_parts)
                _save_msg(phone, "assistant", response)
                return response

            # Execute tools
            messages.append({"role": "assistant", "content": result["content"]})
            tool_results = []
            for tool in tool_uses:
                logger.info(f"Tool: {tool['name']} input: {json.dumps(tool.get('input', {}))[:200]}")
                tool_name = tool["name"]
                tool_input = tool.get("input", {})
                # Route to correct handler
                if tool_name == "web_search":
                    output = await _web_search(tool_input.get("query", ""))
                elif tool_name == "fetch_url":
                    output = await _fetch_url(tool_input.get("url", ""), tool_input.get("summarize", False))
                elif tool_name == "manage_email":
                    output = await _manage_email(tool_input.get("action", ""), tool_input.get("params", {}))
                elif tool_name == "execute_intel":
                    output = await _execute_intel_action(tool_input.get("action", ""), tool_input.get("params", {}))
                else:
                    output = _run_tool(tool_name, tool_input)
                tool_results.append({"type": "tool_result", "tool_use_id": tool["id"], "content": output})
            messages.append({"role": "user", "content": tool_results})

    return None


@app.post("/generate-ata")
async def generate_ata_endpoint(request: Request):
    """
    Generate a comprehensive ata from meeting transcription using Claude.
    Returns immediately, processes in background on Railway (no timeout).
    Saves directly to ConselhoOS database when done.
    """
    import asyncio

    data = await request.json()
    reuniao_id = data.get("reuniao_id")
    transcricao = data.get("transcricao", "")
    empresa_nome = data.get("empresa_nome", "")
    data_reuniao = data.get("data_reuniao", "")
    pauta_md = data.get("pauta_md", "")
    conselhoos_db_url = data.get("conselhoos_db_url") or CONSELHOOS_DATABASE_URL
    participantes_info = data.get("participantes", "")

    if not transcricao or not reuniao_id:
        return JSONResponse({"error": "reuniao_id e transcricao obrigatorios"}, status_code=400)

    logger.info(f"Queuing ata generation for {empresa_nome}, reuniao {reuniao_id} ({len(transcricao)} chars)")
    logger.info(f"ConselhoOS DB URL: {conselhoos_db_url[:30]}..." if conselhoos_db_url else "NO CONSELHOOS_DB_URL!")

    # Fire background task using asyncio (Railway keeps running after response)
    asyncio.create_task(
        _generate_ata_background(
            reuniao_id, transcricao, empresa_nome, data_reuniao,
            pauta_md, conselhoos_db_url, participantes_info
        )
    )

    return {"status": "processing", "message": "Ata sendo gerada em background. Recarregue em ~60s."}


async def _generate_ata_background(
    reuniao_id, transcricao, empresa_nome, data_reuniao,
    pauta_md, conselhoos_db_url, participantes_info
):
    """Background task for ata generation."""
    logger.info(f"[ATA-BG] Starting generation for {empresa_nome} ({len(transcricao)} chars)")

    try:
        await _do_generate_ata(
            reuniao_id, transcricao, empresa_nome, data_reuniao,
            pauta_md, conselhoos_db_url, participantes_info
        )
    except Exception as e:
        logger.error(f"[ATA-BG] FATAL ERROR: {e}", exc_info=True)


async def _do_generate_ata(
    reuniao_id, transcricao, empresa_nome, data_reuniao,
    pauta_md, conselhoos_db_url, participantes_info
):
    """Actual ata generation logic."""

    # Fetch real participant data from ConselhoOS
    if not participantes_info and conselhoos_db_url:
        try:
            with psycopg.connect(conselhoos_db_url) as conn:
                rows = conn.execute("""
                    SELECT p.nome, p.cargo, p.papel
                    FROM pessoas p
                    JOIN reunioes r ON r.empresa_id = p.empresa_id
                    WHERE r.id = %s
                    ORDER BY p.nome
                """, (reuniao_id,)).fetchall()
                if rows:
                    parts = []
                    for r in rows:
                        cargo = r[1] or r[2] or ''
                        parts.append(f"{r[0]} ({cargo})" if cargo else r[0])
                    participantes_info = ", ".join(parts)
                    logger.info(f"[ATA-BG] Found {len(rows)} participants from ConselhoOS")
        except Exception as e:
            logger.warning(f"[ATA-BG] Error fetching participants: {e}")

    prompt = f"""Você é um secretário executivo de alto nível especializado em governança corporativa.
Analise a transcrição desta reunião de conselho e produza uma ATA COMPLETA E DETALHADA.

**Empresa:** {empresa_nome}
**Data:** {data_reuniao}
{f"**Participantes cadastrados (use estes nomes e cargos EXATOS):** {participantes_info}" if participantes_info else ""}
{f"**Pauta prevista:**\n{pauta_md}" if pauta_md else ""}

INSTRUÇÕES DE QUALIDADE:
1. A ata deve ter entre 8.000 e 15.000 caracteres — seja DETALHADO
2. Use tabelas Markdown para dados financeiros (faturamento, metas, indicadores)
3. Cada decisão deve ter: o que foi decidido, por quê, quem é responsável, prazo
4. Cada discussão deve ter: contexto, argumentos apresentados, conclusão
5. Identifique números, valores em R$, percentuais e datas mencionados
6. Capture nuances: preocupações expressas, ressalvas, condições
7. Distingua entre DECISÕES (aprovadas pelo conselho) e PENDÊNCIAS (a resolver)
8. Se houve divergência de opinião, registre ambos os lados
9. Use formatação profissional com cabeçalhos numerados (1., 1.1, 1.2...)

ESTRUTURA OBRIGATÓRIA:

# [EMPRESA] — Ata de Reunião de Conselho
**Data:** ... | **Duração:** ~Xh | **Participantes:** N presentes

## PARTICIPANTES
Lista com nome, cargo/papel e status (presente/ausente)

## 1. CONTEXTO E ABERTURA
Contexto da reunião, revisão de ata anterior se mencionada

## 2. [TEMA PRINCIPAL 1] — título descritivo
### 2.1 Subtema
Análise detalhada com números, tabelas se aplicável

## 3. [TEMA PRINCIPAL 2] — título descritivo
(continuar para cada tema relevante)

## DECISÕES APROVADAS
Lista numerada com responsável e prazo

## PENDÊNCIAS E PRÓXIMOS PASSOS
Lista com responsável e prazo

## PRÓXIMA REUNIÃO
Data se mencionada

---

**Transcrição:**
{transcricao[:80000]}

Produza a ata completa em Markdown.

REGRAS CRÍTICAS:
- NUNCA invente informações não presentes na transcrição
- Use os nomes e cargos EXATOS da lista de participantes cadastrados (se fornecida)
- Se não souber o cargo de alguém, deixe em branco — NÃO invente cargos como CEO, Sócio, etc."""

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 16000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if resp.status_code != 200:
            logger.error(f"Claude API error: {resp.status_code} {resp.text[:200]}")
            return JSONResponse({"error": f"Claude API error: {resp.status_code}"}, status_code=500)

        result = resp.json()
        ata_md = result["content"][0]["text"]
        logger.info(f"Ata generated: {len(ata_md)} chars")

        # Save to ConselhoOS database
        if conselhoos_db_url:
            try:
                with psycopg.connect(conselhoos_db_url) as conn:
                    conn.execute(
                        "UPDATE reunioes SET ata_md = %s, updated_at = NOW() WHERE id = %s",
                        (ata_md, reuniao_id)
                    )
                    conn.commit()
                logger.info(f"Ata saved to ConselhoOS for reuniao {reuniao_id}")
            except Exception as e:
                logger.error(f"Error saving ata to ConselhoOS: {e}")
                return JSONResponse({
                    "status": "generated_not_saved",
                    "error": str(e),
                    "ata_md": ata_md
                }, status_code=200)

        # Notify via WhatsApp
        try:
            await _send_response(
                os.getenv("RENATO_PHONE", "5511984153337"),
                f"✅ Ata gerada para {empresa_nome} ({data_reuniao}). {len(ata_md)} chars. Recarregue a página."
            )
        except Exception:
            pass

        return {"status": "success", "chars": len(ata_md), "ata_md": ata_md}

    except Exception as e:
        logger.error(f"Ata generation error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/transcribe")
async def transcribe_audio(request: Request):
    """
    Receive audio transcription request from Vercel webhook.
    Downloads audio, transcribes, processes bot message, sends response.
    """
    data = await request.json()

    # Verify secret
    if data.get("secret") != WORKER_SECRET:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    key = data.get("key", {})
    phone = data.get("phone", "")
    message_id = data.get("message_id", "")

    if not phone or not key:
        return JSONResponse(status_code=400, content={"error": "missing phone or key"})

    logger.info(f"Transcription request for {phone}")

    try:
        # Step 1: Download audio from Evolution API
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl_resp = await client.post(
                f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{INTEL_BOT_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"message": {"key": key}, "convertToMp4": False}
            )

        if dl_resp.status_code not in (200, 201):
            logger.error(f"Download failed: {dl_resp.status_code}")
            await _send_response(phone, "Nao consegui baixar o audio. Pode digitar?")
            return {"error": "download_failed"}

        dl_data = dl_resp.json()
        audio_b64 = dl_data.get("base64", "")
        mimetype = dl_data.get("mimetype", "audio/ogg")

        if not audio_b64:
            await _send_response(phone, "Audio vazio. Pode digitar?")
            return {"error": "empty_audio"}

        logger.info(f"Audio downloaded: {len(audio_b64)} chars, type={mimetype}")

        # Step 2: Transcribe with Groq Whisper (free, fast, supports ogg)
        import base64
        audio_bytes = base64.b64decode(audio_b64)

        # Determine file extension from mimetype
        ext_map = {"audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}
        clean_mime = mimetype.split(";")[0].strip() if mimetype else "audio/ogg"
        ext = ext_map.get(clean_mime, "ogg")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"audio.{ext}", audio_bytes, clean_mime)},
                data={"model": "whisper-large-v3-turbo", "language": "pt"}
            )

        if resp.status_code != 200:
            error_detail = resp.text[:500]
            logger.error(f"Groq transcription failed: {resp.status_code} - {error_detail}")
            await _send_response(phone, f"Erro na transcricao ({resp.status_code}). Pode digitar?")
            return {"error": f"transcription_failed: {resp.status_code}", "detail": error_detail}

        transcription = resp.json().get("text", "")
        if not transcription:
            await _send_response(phone, "Nao consegui entender o audio. Pode digitar?")
            return {"error": "empty_transcription"}

        logger.info(f"Transcribed: {transcription[:100]}")

        # Step 3: Send transcribed text to INTEL bot for processing
        content = f"[Audio transcrito] {transcription}"

        # Step 3: Send to intel-bot for full processing (has query_intel, save_memory, etc)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                bot_resp = await client.post(
                    f"{INTEL_API_URL}/api/webhooks/bot-message",
                    headers={"Content-Type": "application/json"},
                    json={"phone": phone, "content": content, "message_id": message_id,
                          "secret": WORKER_SECRET},
                    timeout=55.0
                )
            if bot_resp.status_code == 200:
                return {"status": "success", "transcription": transcription[:200]}
            else:
                logger.warning(f"Bot API failed: {bot_resp.status_code}")
                await _send_response(phone, f"Transcrevi seu audio:\n\n_{transcription}_")
                return {"status": "partial", "transcription": transcription[:200]}
        except httpx.TimeoutException:
            logger.warning("Bot API timeout - sending transcription directly")
            await _send_response(phone, f"Transcrevi seu audio:\n\n_{transcription}_")
            return {"status": "partial_timeout", "transcription": transcription[:200]}

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        await _send_response(phone, "Erro ao processar audio. Tenta digitar?")
        return {"error": str(e)}


@app.post("/analyze-image")
async def analyze_image(request: Request):
    """
    Receive image from WhatsApp, analyze with Claude Vision,
    send to intel-bot for processing.
    """
    import base64

    data = await request.json()
    if data.get("secret") != WORKER_SECRET:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    key = data.get("key", {})
    phone = data.get("phone", "")
    message_id = data.get("message_id", "")
    caption = data.get("caption", "")

    if not phone or not key:
        return JSONResponse(status_code=400, content={"error": "missing phone or key"})

    logger.info(f"Image analysis request for {phone}, caption: {caption[:50]}")

    try:
        # Step 1: Download image from Evolution API
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl_resp = await client.post(
                f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{INTEL_BOT_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"message": {"key": key}, "convertToMp4": False}
            )

        if dl_resp.status_code not in (200, 201):
            await _send_response(phone, "Nao consegui baixar a imagem.")
            return {"error": "download_failed"}

        dl_data = dl_resp.json()
        image_b64 = dl_data.get("base64", "")
        mimetype = dl_data.get("mimetype", "image/jpeg").split(";")[0].strip()

        if not image_b64:
            await _send_response(phone, "Imagem vazia.")
            return {"error": "empty_image"}

        logger.info(f"Image downloaded: {len(image_b64)} chars, type={mimetype}")

        # Step 2: Analyze with Claude Vision
        user_instruction = caption if caption else "Descreva o que voce ve nesta imagem. Se for uma tela do sistema, identifique o que pode ser melhorado. Se for uma mensagem, resuma o conteudo."

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mimetype,
                                    "data": image_b64
                                }
                            },
                            {
                                "type": "text",
                                "text": user_instruction
                            }
                        ]
                    }]
                }
            )

        if resp.status_code != 200:
            logger.error(f"Claude Vision failed: {resp.status_code} - {resp.text[:200]}")
            await _send_response(phone, "Erro ao analisar imagem.")
            return {"error": f"vision_failed: {resp.status_code}"}

        analysis = resp.json().get("content", [{}])[0].get("text", "")
        if not analysis:
            await _send_response(phone, "Nao consegui analisar a imagem.")
            return {"error": "empty_analysis"}

        logger.info(f"Image analyzed: {analysis[:100]}")

        # Step 3: Send to intel-bot for processing with CRM context
        content = f"[Imagem analisada] {caption + ': ' if caption else ''}{analysis}"

        async with httpx.AsyncClient(timeout=55.0) as client:
            bot_resp = await client.post(
                f"{INTEL_API_URL}/api/webhooks/bot-message",
                headers={"Content-Type": "application/json"},
                json={"phone": phone, "content": content, "message_id": message_id,
                      "secret": WORKER_SECRET},
                timeout=55.0
            )

        if bot_resp.status_code == 200:
            return {"status": "success", "analysis": analysis[:200]}
        else:
            # Fallback: send analysis directly
            await _send_response(phone, f"📸 *Analise da imagem:*\n\n{analysis}")
            return {"status": "partial"}

    except Exception as e:
        logger.error(f"Image analysis error: {e}")
        await _send_response(phone, "Erro ao processar imagem.")
        return {"error": str(e)}


async def _send_response(phone: str, message: str):
    """Send WhatsApp message via intel-bot instance."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{EVOLUTION_API_URL}/message/sendText/{INTEL_BOT_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"number": phone, "text": message}
            )
    except Exception as e:
        logger.error(f"Failed to send response: {e}")


# ============== GMAIL SYNC JOB ==============
# Migrated from Vercel cron (services/gmail_sync.py) — was timing out at 300s
# because of O(N×M) loop over 3.5k contacts × 2 accounts × 3 emails.

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"


async def _gmail_list_messages(access_token: str, query: str, max_results: int = 100) -> dict:
    """List Gmail messages via REST API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": query, "maxResults": max_results, "includeSpamTrash": False},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code != 200:
            return {"error": resp.text[:200]}
        return resp.json()


async def _gmail_get_message_metadata(access_token: str, message_id: str) -> dict:
    """Fetch single message metadata."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "metadata"},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code != 200:
            return {"error": resp.text[:200]}
        return resp.json()


def _parse_gmail_date(date_str: str):
    """Parse RFC 2822 Gmail date header to datetime."""
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str)
    except Exception:
        return None


def _parse_gmail_headers(message: dict) -> dict:
    """Extract from/to/date headers from message payload."""
    headers = {}
    for h in message.get("payload", {}).get("headers", []):
        name = (h.get("name") or "").lower()
        if name in ("from", "to", "cc", "bcc", "subject", "date", "message-id"):
            headers[name] = h.get("value", "")
    return headers


async def _refresh_gmail_token_full(account: dict) -> tuple[str | None, str | None]:
    """Refresh access token from refresh_token. Returns (token, error_reason)."""
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        return None, "no_refresh_token"
    client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        return None, "missing_google_client_env"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            err = f"http_{resp.status_code}: {resp.text[:120]}"
            logger.error(f"Gmail token refresh failed: {err}")
            return None, err
        return resp.json().get("access_token"), None
    except Exception as e:
        logger.error(f"Gmail token refresh exception: {e}")
        return None, f"exception: {str(e)[:120]}"


async def _count_messages_for_email(access_token: str, email: str, months_back: int) -> dict:
    """Returns {count, latest_date, error?}."""
    query = f"(from:{email} OR to:{email})"
    if months_back:
        date_after = (datetime.now() - timedelta(days=months_back * 30)).strftime("%Y/%m/%d")
        query += f" after:{date_after}"

    response = await _gmail_list_messages(access_token, query, max_results=100)
    if "error" in response:
        return {"count": 0, "latest_date": None, "error": response["error"]}

    messages = response.get("messages", []) or []
    result = {"count": len(messages), "latest_date": None}

    if messages:
        msg_detail = await _gmail_get_message_metadata(access_token, messages[0]["id"])
        if "error" not in msg_detail:
            headers = _parse_gmail_headers(msg_detail)
            date_str = headers.get("date", "")
            if date_str:
                result["latest_date"] = _parse_gmail_date(date_str)
    return result


async def _sync_contact_emails_worker(
    contact_id: int, email: str, access_token: str, months_back: int
) -> dict:
    """Sync one contact's emails. Updates contacts.total_interacoes/ultimo_contato."""
    res = {"success": False, "count": 0, "updated": False}
    try:
        msg_result = await _count_messages_for_email(access_token, email, months_back)
        if msg_result.get("error") == "token_expired":
            return {"success": False, "error": "token_expired"}
        res["count"] = msg_result["count"]
        if msg_result["count"] <= 0:
            res["success"] = True
            return res

        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT total_interacoes, ultimo_contato FROM contacts WHERE id = %s",
                (contact_id,),
            )
            current = cursor.fetchone()
            if not current:
                return res

            current_interactions = current.get("total_interacoes") or 0
            current_ultimo = current.get("ultimo_contato")
            new_interactions = max(current_interactions, msg_result["count"])
            new_ultimo = msg_result["latest_date"]

            if current_ultimo and msg_result["latest_date"]:
                try:
                    cur_naive = current_ultimo.replace(tzinfo=None) if current_ultimo.tzinfo else current_ultimo
                    lat_naive = msg_result["latest_date"].replace(tzinfo=None) if msg_result["latest_date"].tzinfo else msg_result["latest_date"]
                    new_ultimo = msg_result["latest_date"] if lat_naive > cur_naive else current_ultimo
                except Exception:
                    new_ultimo = msg_result["latest_date"] or current_ultimo
            elif current_ultimo:
                new_ultimo = current_ultimo

            cursor.execute(
                "UPDATE contacts SET total_interacoes = %s, ultimo_contato = %s WHERE id = %s",
                (new_interactions, new_ultimo, contact_id),
            )
            conn.commit()
            res["updated"] = True

        res["success"] = True
    except Exception as e:
        logger.error(f"sync_contact {contact_id} error: {e}")
        res["error"] = str(e)
    return res


async def _run_gmail_sync(job_id: int, months_back: int = 1):
    """Process gmail sync job. Updates background_jobs row with progress."""
    import asyncio as _aio

    logger.info(f"[GmailSync job={job_id}] starting (months_back={months_back})")
    stats = {"imported": 0, "updated": 0, "errors": 0, "processed": 0, "accounts": 0,
             "error_samples": []}

    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE background_jobs SET status='running', started_at=NOW() WHERE id = %s",
                (job_id,),
            )
            conn.commit()

            cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE")
            accounts = cursor.fetchall()
            cursor.execute(
                "SELECT id, nome, emails FROM contacts "
                "WHERE emails IS NOT NULL AND emails::text != '[]' ORDER BY id"
            )
            contacts = cursor.fetchall()

            cursor.execute(
                "UPDATE background_jobs SET total_items = %s WHERE id = %s",
                (len(contacts) * len(accounts), job_id),
            )
            conn.commit()

        if not accounts:
            with psycopg.connect(DATABASE_URL) as conn:
                conn.execute(
                    "UPDATE background_jobs SET status='error', error=%s, completed_at=NOW() WHERE id=%s",
                    ("Nenhuma conta Gmail conectada", job_id),
                )
                conn.commit()
            return

        stats["accounts"] = len(accounts)
        total_processed = 0

        # Heartbeat task: atualiza last_progress a cada 15s pra detectar
        # se o worker morreu silenciosamente (Railway restart, OOM, etc).
        # Tambem loga memoria pra debug.
        async def _heartbeat():
            import resource
            while True:
                try:
                    mem_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    mem_mb = mem_kb / 1024 if sys.platform == "linux" else mem_kb / (1024*1024)
                    logger.info(f"[GmailSync job={job_id}] heartbeat: processed={total_processed} mem_mb={mem_mb:.1f}")
                except Exception:
                    pass
                await _aio.sleep(15)

        heartbeat_task = _aio.create_task(_heartbeat())

        for account in accounts:
            account_email = account.get("email", "")
            access_token, refresh_err = await _refresh_gmail_token_full(account)
            if not access_token:
                stats["errors"] += 1
                sample = f"{account_email}: {refresh_err}"
                if len(stats["error_samples"]) < 5:
                    stats["error_samples"].append(sample)
                logger.warning(f"[GmailSync job={job_id}] No token for {account_email}: {refresh_err}")
                continue

            for contact in contacts:
                contact_id = contact["id"]
                emails_data = contact.get("emails")
                email_list = []
                if isinstance(emails_data, str):
                    try:
                        email_list = json.loads(emails_data)
                    except Exception:
                        email_list = [{"email": emails_data}]
                elif isinstance(emails_data, list):
                    email_list = emails_data

                for email_obj in email_list[:3]:
                    email = email_obj.get("email", "") if isinstance(email_obj, dict) else str(email_obj)
                    if not email or email == account_email:
                        continue

                    result = await _sync_contact_emails_worker(
                        contact_id, email.lower(), access_token, months_back
                    )

                    if result.get("error") == "token_expired":
                        access_token, _ = await _refresh_gmail_token_full(account)
                        if not access_token:
                            break
                    if result.get("updated"):
                        stats["updated"] += 1
                    if result.get("error") and result.get("error") != "token_expired":
                        stats["errors"] += 1

                    await _aio.sleep(0.1)  # rate limit

                stats["processed"] += 1
                total_processed += 1

                # Checkpoint every 50 contacts
                if total_processed % 50 == 0:
                    try:
                        with psycopg.connect(DATABASE_URL) as conn:
                            conn.execute(
                                "UPDATE background_jobs SET processed_items=%s, success_count=%s, "
                                "failed_count=%s, result=%s WHERE id=%s",
                                (total_processed, stats["updated"], stats["errors"],
                                 json.dumps(stats), job_id),
                            )
                            conn.commit()
                    except Exception as ce:
                        logger.warning(f"[GmailSync job={job_id}] checkpoint failed: {ce}")

            # Update last_sync timestamp on account
            try:
                with psycopg.connect(DATABASE_URL) as conn:
                    conn.execute(
                        "UPDATE google_accounts SET ultima_sync = CURRENT_TIMESTAMP WHERE id = %s",
                        (account["id"],),
                    )
                    conn.commit()
            except Exception as ue:
                logger.warning(f"[GmailSync job={job_id}] account ts update failed: {ue}")

        heartbeat_task.cancel()
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE background_jobs SET status='completed', processed_items=%s, "
                "success_count=%s, failed_count=%s, result=%s, completed_at=NOW() WHERE id=%s",
                (total_processed, stats["updated"], stats["errors"], json.dumps(stats), job_id),
            )
            conn.commit()
        logger.info(f"[GmailSync job={job_id}] completed: {stats}")

    except Exception as e:
        try:
            heartbeat_task.cancel()
        except Exception:
            pass
        logger.exception(f"[GmailSync job={job_id}] fatal error")
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                conn.execute(
                    "UPDATE background_jobs SET status='error', error=%s, "
                    "result=%s, completed_at=NOW() WHERE id=%s",
                    (str(e)[:500], json.dumps(stats), job_id),
                )
                conn.commit()
        except Exception:
            pass


@app.post("/sync-gmail")
async def sync_gmail(request: Request, background_tasks: BackgroundTasks):
    """
    Receive gmail-sync job from Vercel cron.
    Validates secret + idempotency, then runs in background.
    """
    data = await request.json()
    if data.get("secret") != WORKER_SECRET:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    job_id = data.get("job_id")
    months_back = int(data.get("months_back", 1))

    if not job_id:
        return JSONResponse(status_code=400, content={"error": "job_id required"})

    # Idempotency: bail if (a) another non-stale gmail_sync job is running,
    # OR (b) THIS job_id was already picked up (claimed status='running').
    # Why (b): observed em prod que mesmo POST chega duplicado (LB retry, etc).
    # Sem isso 2 tasks paralelas escrevem na mesma row e o progresso oscila.
    # Solucao: claim atomico via UPDATE WHERE status='queued' RETURNING.
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM background_jobs "
                "WHERE job_type = 'gmail_sync' AND status = 'running' "
                "AND started_at > NOW() - INTERVAL '1 hour' AND id <> %s LIMIT 1",
                (job_id,),
            )
            existing = cursor.fetchone()
            if existing:
                logger.warning(f"[GmailSync] aborting job {job_id}: job {existing['id']} already running")
                cursor.execute(
                    "UPDATE background_jobs SET status='skipped', "
                    "error=%s, completed_at=NOW() WHERE id=%s",
                    (f"another job ({existing['id']}) already running", job_id),
                )
                conn.commit()
                return JSONResponse(
                    status_code=202,
                    content={"status": "skipped", "reason": "already_running",
                             "running_job_id": existing["id"]},
                )

            # Claim this job atomically: only proceed if status=queued (not yet picked up).
            # Idempotente contra POSTs duplicados pro mesmo job_id.
            cursor.execute(
                "UPDATE background_jobs SET status='running', started_at=NOW() "
                "WHERE id=%s AND status='queued' RETURNING id",
                (job_id,),
            )
            claimed = cursor.fetchone()
            conn.commit()
            if not claimed:
                logger.warning(f"[GmailSync] job {job_id} ja foi reclamado (POST duplicado?)")
                return JSONResponse(
                    status_code=202,
                    content={"status": "skipped", "reason": "already_claimed",
                             "job_id": job_id},
                )
    except Exception as e:
        logger.error(f"[GmailSync] idempotency check failed: {e}")

    background_tasks.add_task(_run_gmail_sync, job_id, months_back)
    return JSONResponse(status_code=202, content={"status": "accepted", "job_id": job_id})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
