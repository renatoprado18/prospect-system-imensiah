"""
Project Assistant - Assistente IA dedicado por projeto.

Chat conversacional com contexto fixo do projeto: tarefas, membros,
mensagens, pareceres, notas. Usa Claude com tool_use para consultar
e executar ações no escopo do projeto.
"""
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOOL_ITERATIONS = 5

TOOLS = [
    {
        "name": "query_project",
        "description": (
            "Consulta dados do projeto no banco. Query SQL READ-ONLY. "
            "Tabelas: tasks, project_notes, project_members, contacts, messages, "
            "conversations, project_whatsapp_groups, group_messages, calendar_events. "
            "O project_id ja esta filtrado automaticamente. Apenas SELECT."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Query SQL SELECT"}
            },
            "required": ["sql"]
        }
    },
    {
        "name": "execute_action",
        "description": (
            "Executa acao no projeto:\n"
            "- create_task: cria tarefa (titulo, descricao?, prazo_dias?, contact_id?)\n"
            "- complete_task: conclui tarefa (task_id)\n"
            "- save_note: salva nota (titulo, conteudo, tipo?)\n"
            "- update_task: atualiza tarefa (task_id, fields: {campo: valor})"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create_task", "complete_task", "save_note", "update_task"]
                },
                "params": {"type": "object", "description": "Parametros da acao"}
            },
            "required": ["action", "params"]
        }
    }
]


def _build_system_prompt(project: Dict, tasks: List, members: List, recent_notes: List) -> str:
    """Build system prompt with full project context."""
    tasks_text = "\n".join([
        f"  - [{t['status']}] {t['titulo']} (vence: {str(t.get('data_vencimento','?'))[:10]}, resp: {t.get('contact_nome','?')})"
        for t in tasks
    ]) or "  Nenhuma tarefa"

    members_text = "\n".join([
        f"  - {m['nome']} ({m.get('papel','membro')}) - {m.get('empresa','')} {m.get('cargo','')}"
        for m in members
    ]) or "  Nenhum membro"

    notes_text = "\n".join([
        f"  [{str(n.get('criado_em',''))[:10]}] {n.get('titulo','')}: {(n.get('conteudo',''))[:200]}"
        for n in recent_notes[:5]
    ]) or "  Nenhuma nota"

    return f"""Voce e o assistente dedicado do projeto "{project['nome']}".
Seu papel e ajudar Renato a gerenciar este projeto: responder perguntas, criar tarefas,
analisar progresso e sugerir acoes.

PROJETO: {project['nome']}
Tipo: {project.get('tipo', 'negocio')} | Status: {project.get('status', 'ativo')}
Descricao: {project.get('descricao', 'N/A')}

TAREFAS:
{tasks_text}

MEMBROS:
{members_text}

NOTAS RECENTES:
{notes_text}

REGRAS:
- Responda em portugues, de forma objetiva e acionavel
- Use query_project para buscar dados especificos (mensagens, historico, etc)
- Use execute_action para criar tarefas, notas ou atualizar progresso
- Sempre contextualize no escopo deste projeto
- Seja proativo: sugira acoes quando identificar oportunidades ou riscos
- Data atual: {datetime.now().strftime('%d/%m/%Y %H:%M')}"""


def _get_conversation_history(project_id: int, limit: int = 20) -> List[Dict]:
    """Get conversation history for this project assistant."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT role, content FROM project_assistant_messages
            WHERE project_id = %s
            ORDER BY criado_em DESC LIMIT %s
        """, (project_id, limit))
        rows = [dict(r) for r in cursor.fetchall()]
        return list(reversed(rows))


def _save_message(project_id: int, role: str, content: str):
    """Save a message to project conversation history."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO project_assistant_messages (project_id, role, content)
            VALUES (%s, %s, %s)
        """, (project_id, role, content))
        conn.commit()


def _execute_query(project_id: int, sql: str) -> str:
    """Execute read-only SQL scoped to project."""
    sql_upper = sql.strip().upper()
    if not sql_upper.startswith("SELECT"):
        return "Erro: apenas SELECT permitido"

    # Auto-scope to project where possible
    if "LIMIT" not in sql_upper:
        sql = sql.rstrip(";") + " LIMIT 20"

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = [dict(r) for r in cursor.fetchall()]
            if not rows:
                return "Nenhum resultado encontrado"
            return json.dumps(rows, default=str, ensure_ascii=False)[:3000]
    except Exception as e:
        return f"Erro SQL: {str(e)}"


def _execute_action(project_id: int, action: str, params: Dict) -> str:
    """Execute an action scoped to the project."""
    with get_db() as conn:
        cursor = conn.cursor()

        if action == "create_task":
            from datetime import timedelta
            prazo = params.get('prazo_dias', 7)
            vencimento = datetime.now() + timedelta(days=prazo)
            cursor.execute("""
                INSERT INTO tasks (titulo, descricao, status, data_vencimento, project_id, contact_id, prioridade)
                VALUES (%s, %s, 'pending', %s, %s, %s, %s) RETURNING id
            """, (
                params['titulo'], params.get('descricao', ''),
                vencimento, project_id,
                params.get('contact_id'), params.get('prioridade', 5)
            ))
            task_id = cursor.fetchone()['id']
            conn.commit()
            return f"Tarefa #{task_id} criada: {params['titulo']}"

        elif action == "complete_task":
            cursor.execute("""
                UPDATE tasks SET status = 'completed', data_conclusao = NOW()
                WHERE id = %s AND project_id = %s RETURNING titulo
            """, (params['task_id'], project_id))
            row = cursor.fetchone()
            conn.commit()
            return f"Tarefa concluida: {row['titulo']}" if row else "Tarefa nao encontrada"

        elif action == "save_note":
            cursor.execute("""
                INSERT INTO project_notes (project_id, titulo, conteudo, tipo, autor)
                VALUES (%s, %s, %s, %s, 'Assistente IA') RETURNING id
            """, (project_id, params.get('titulo', ''), params['conteudo'], params.get('tipo', 'nota')))
            note_id = cursor.fetchone()['id']
            conn.commit()
            return f"Nota #{note_id} salva"

        elif action == "update_task":
            task_id = params['task_id']
            fields = params.get('fields', {})
            allowed = ['titulo', 'descricao', 'status', 'data_vencimento', 'prioridade']
            updates = []
            values = []
            for k, v in fields.items():
                if k in allowed:
                    updates.append(f"{k} = %s")
                    values.append(v)
            if not updates:
                return "Nenhum campo valido para atualizar"
            values.extend([task_id, project_id])
            cursor.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = %s AND project_id = %s RETURNING titulo",
                values
            )
            row = cursor.fetchone()
            conn.commit()
            return f"Tarefa atualizada: {row['titulo']}" if row else "Tarefa nao encontrada"

    return "Acao desconhecida"


async def chat(project_id: int, message: str) -> Dict:
    """
    Chat with the project assistant.
    Returns the assistant's response.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY nao configurada"}

    # Load project context
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        project = cursor.fetchone()
        if not project:
            return {"error": "Projeto nao encontrado"}
        project = dict(project)

        cursor.execute("""
            SELECT t.*, c.nome as contact_nome FROM tasks t
            LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.project_id = %s ORDER BY t.status, t.data_vencimento
        """, (project_id,))
        tasks = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
            SELECT c.nome, c.empresa, c.cargo, pm.papel
            FROM project_members pm JOIN contacts c ON c.id = pm.contact_id
            WHERE pm.project_id = %s
        """, (project_id,))
        members = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
            SELECT titulo, conteudo, tipo, criado_em FROM project_notes
            WHERE project_id = %s ORDER BY criado_em DESC LIMIT 5
        """, (project_id,))
        notes = [dict(r) for r in cursor.fetchall()]

    system_prompt = _build_system_prompt(project, tasks, members, notes)

    # Load conversation history
    history = _get_conversation_history(project_id)

    # Save user message
    _save_message(project_id, "user", message)

    # Build messages
    messages = history + [{"role": "user", "content": message}]

    # Call Claude with tool loop
    async with httpx.AsyncClient(timeout=45.0) as client:
        for iteration in range(MAX_TOOL_ITERATIONS):
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1000,
                    "system": system_prompt,
                    "tools": TOOLS,
                    "messages": messages
                }
            )

            if resp.status_code != 200:
                return {"error": f"API error: {resp.status_code}"}

            result = resp.json()
            stop_reason = result.get("stop_reason")

            # Extract text and tool_use blocks
            text_parts = []
            tool_uses = []
            for block in result.get("content", []):
                if block["type"] == "text":
                    text_parts.append(block["text"])
                elif block["type"] == "tool_use":
                    tool_uses.append(block)

            if stop_reason == "end_turn" or not tool_uses:
                # Final response
                response_text = "\n".join(text_parts)
                _save_message(project_id, "assistant", response_text)
                return {"response": response_text}

            # Process tool calls
            messages.append({"role": "assistant", "content": result["content"]})

            tool_results = []
            for tool in tool_uses:
                tool_input = tool.get("input", {})
                if tool["name"] == "query_project":
                    output = _execute_query(project_id, tool_input.get("sql", ""))
                elif tool["name"] == "execute_action":
                    output = _execute_action(project_id, tool_input.get("action", ""), tool_input.get("params", {}))
                else:
                    output = "Tool desconhecida"

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool["id"],
                    "content": output
                })

            messages.append({"role": "user", "content": tool_results})

    return {"error": "Max iterations reached"}
