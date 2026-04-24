"""
Intel Bot - Conversational WhatsApp Bot with Claude Tool Use

Full LLM chat with conversation memory. Uses Claude's function calling
to dynamically decide when to query the CRM, create tasks, send messages, etc.
No rigid intent classification — Claude decides what tools to use.
"""
import os
import re
import json
import httpx
import logging
import asyncio
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Any

from database import get_db

logger = logging.getLogger(__name__)

# Config
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
INTEL_BOT_INSTANCE = os.getenv("INTEL_BOT_INSTANCE", "intel-bot")
INTEL_BOT_NUMBER = os.getenv("INTEL_BOT_NUMBER", "5511915020192")
RENATO_PHONE = "5511984153337"
RENATO_PHONE_SUFFIXES = ["11984153337", "984153337"]
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOOL_ITERATIONS = 5

# Rate limit: skip trivial messages
SKIP_PATTERNS = re.compile(
    r'^(ok|👍|👌|🙏|❤️|😀|😂|🤣|😊|👏|🔥|✅|sim|nao|não|obrigado|valeu|top|show|beleza|blz|tmj)$',
    re.IGNORECASE
)


def _is_renato(phone: str) -> bool:
    """Check if the phone belongs to Renato."""
    clean = ''.join(filter(str.isdigit, phone))
    if clean == RENATO_PHONE:
        return True
    for suffix in RENATO_PHONE_SUFFIXES:
        if clean.endswith(suffix):
            return True
    return False


# ==================== TOOL DEFINITIONS (3 meta-tools) ====================

TOOLS = [
    {
        "name": "query_intel",
        "description": (
            "Executa uma query SQL READ-ONLY no banco de dados do INTEL. "
            "Use para buscar QUALQUER informacao: contatos, mensagens, projetos, tarefas, "
            "memorias, calendario, editorial, etc. Apenas SELECT e permitido. "
            "Resultados limitados a 20 linhas. Use ILIKE para buscas case-insensitive. "
            "Para datas relativas use CURRENT_DATE, CURRENT_TIMESTAMP, INTERVAL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Query SQL SELECT. Ex: SELECT id, nome FROM contacts WHERE nome ILIKE '%joao%' LIMIT 10"
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "execute_action",
        "description": (
            "Executa uma acao no sistema INTEL. Acoes disponiveis:\n"
            "- create_task: cria tarefa (titulo, descricao?, project_id?, contact_id?, prazo_dias?, prioridade?)\n"
            "- complete_task: conclui tarefa (task_id)\n"
            "- save_note: salva nota em projeto (project_id, titulo, conteudo, tipo?)\n"
            "- save_memory: salva memoria de contato (contact_id, titulo, resumo, conteudo_completo?, tipo?)\n"
            "- schedule_meeting: cria evento (titulo, data_hora ISO, duracao_min?, contact_id?, local?, descricao?)\n"
            "- send_whatsapp: envia WhatsApp via rap-whatsapp (contact_id, message)\n"
            "- enrich_contact: enriquece contato com IA (contact_id)\n"
            "- update_contact: atualiza campos do contato (contact_id, fields: {campo: valor})"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Nome da acao",
                    "enum": [
                        "create_task", "complete_task", "save_note", "save_memory",
                        "schedule_meeting", "send_whatsapp", "enrich_contact", "update_contact"
                    ]
                },
                "params": {
                    "type": "object",
                    "description": "Parametros da acao (variam por acao)"
                }
            },
            "required": ["action", "params"]
        }
    },
    {
        "name": "query_conselhoos",
        "description": (
            "Executa uma query SQL READ-ONLY no banco de dados do ConselhoOS (sistema de governanca corporativa). "
            "Use para buscar dados de empresas assessoradas, reunioes de conselho, atas, transcricoes, "
            "tarefas RACI, decisoes, pautas e documentos. Apenas SELECT e permitido. "
            "Resultados limitados a 20 linhas. Use ILIKE para buscas case-insensitive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Query SQL SELECT. Ex: SELECT e.nome, e.setor FROM empresas e LIMIT 10"
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "draft_message",
        "description": "Gera um rascunho de mensagem personalizada para um contato, usando contexto completo: mensagens recentes, memorias, LinkedIn, fatos e emails.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "integer",
                    "description": "ID do contato"
                },
                "context": {
                    "type": "string",
                    "description": "Contexto/objetivo da mensagem (ex: 'follow up da reuniao', 'parabenizar aniversario')"
                }
            },
            "required": ["contact_id", "context"]
        }
    },
    {
        "name": "project_chat",
        "description": (
            "Conversa com o assistente dedicado de um projeto. "
            "O assistente tem contexto completo: tarefas, membros, notas, pareceres, mensagens. "
            "Pode consultar dados e executar acoes (criar tarefas, salvar notas, etc). "
            "Use quando o usuario perguntar sobre um projeto especifico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "ID do projeto (busque antes com query_intel se nao souber)"
                },
                "message": {
                    "type": "string",
                    "description": "Pergunta ou instrucao sobre o projeto"
                }
            },
            "required": ["project_id", "message"]
        }
    },
]


# ==================== TOOL IMPLEMENTATIONS ====================

def _tool_query_intel(sql: str) -> str:
    """Execute a read-only SQL query against the INTEL database."""
    # Security: only allow SELECT statements
    sql_stripped = sql.strip().rstrip(";").strip()
    sql_upper = sql_stripped.upper()

    # Reject non-SELECT queries
    if not sql_upper.startswith("SELECT"):
        return json.dumps({"erro": "Apenas queries SELECT sao permitidas. INSERT/UPDATE/DELETE nao sao aceitos nesta tool. Use execute_action para modificar dados."})

    # Reject dangerous keywords even in subqueries
    dangerous = ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "CREATE ", "GRANT ", "REVOKE "]
    for kw in dangerous:
        if kw in sql_upper:
            return json.dumps({"erro": f"Query contem operacao proibida: {kw.strip()}"})

    # Ensure LIMIT exists (add LIMIT 20 if missing)
    if "LIMIT" not in sql_upper:
        sql_stripped = sql_stripped + " LIMIT 20"

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql_stripped)
            rows = cursor.fetchall()

        if not rows:
            return json.dumps({"resultado": "Nenhum registro encontrado.", "query": sql_stripped})

        # Format as readable text
        results = [dict(r) for r in rows]
        lines = []
        for i, row in enumerate(results):
            parts = []
            for key, value in row.items():
                if value is not None:
                    # Truncate long values
                    str_val = str(value)
                    if len(str_val) > 200:
                        str_val = str_val[:200] + "..."
                    parts.append(f"{key}: {str_val}")
            lines.append(f"[{i+1}] " + " | ".join(parts))

        return f"Encontrados {len(results)} registros:\n" + "\n".join(lines)

    except Exception as e:
        logger.error(f"query_intel error: {e}")
        return json.dumps({"erro": f"Erro SQL: {str(e)}", "query": sql_stripped})


def _tool_query_conselhoos(sql: str) -> str:
    """Execute a read-only SQL query against the ConselhoOS database."""
    # Security: only allow SELECT statements
    sql_stripped = sql.strip().rstrip(";").strip()
    sql_upper = sql_stripped.upper()

    if not sql_upper.startswith("SELECT"):
        return json.dumps({"erro": "Apenas queries SELECT sao permitidas no ConselhoOS."})

    dangerous = ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "CREATE ", "GRANT ", "REVOKE "]
    for kw in dangerous:
        if kw in sql_upper:
            return json.dumps({"erro": f"Query contem operacao proibida: {kw.strip()}"})

    if "LIMIT" not in sql_upper:
        sql_stripped = sql_stripped + " LIMIT 20"

    conselhoos_url = os.getenv("CONSELHOOS_DATABASE_URL")
    if not conselhoos_url:
        return json.dumps({"erro": "CONSELHOOS_DATABASE_URL nao configurada"})

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(conselhoos_url, cursor_factory=RealDictCursor)
        try:
            cursor = conn.cursor()
            cursor.execute(sql_stripped)
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            return json.dumps({"resultado": "Nenhum registro encontrado.", "query": sql_stripped})

        results = [dict(r) for r in rows]
        lines = []
        for i, row in enumerate(results):
            parts = []
            for key, value in row.items():
                if value is not None:
                    str_val = str(value)
                    if len(str_val) > 200:
                        str_val = str_val[:200] + "..."
                    parts.append(f"{key}: {str_val}")
            lines.append(f"[{i+1}] " + " | ".join(parts))

        return f"Encontrados {len(results)} registros:\n" + "\n".join(lines)

    except Exception as e:
        logger.error(f"query_conselhoos error: {e}")
        return json.dumps({"erro": f"Erro SQL ConselhoOS: {str(e)}", "query": sql_stripped})


async def _tool_execute_action(action: str, params: Dict) -> str:
    """Execute a write action on the INTEL system."""
    try:
        if action == "create_task":
            titulo = params.get("titulo")
            if not titulo:
                return json.dumps({"erro": "titulo e obrigatorio"})
            descricao = params.get("descricao", "")
            project_id = params.get("project_id")
            contact_id = params.get("contact_id")
            prazo_dias = params.get("prazo_dias")
            prioridade = params.get("prioridade", 5)

            data_vencimento = None
            if prazo_dias is not None:
                data_vencimento = datetime.now() + timedelta(days=prazo_dias)

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO tasks (
                        titulo, descricao, project_id, contact_id,
                        data_vencimento, prioridade, ai_generated, origem, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'intel_bot', 'pending')
                    RETURNING id
                """, (titulo, descricao, project_id, contact_id, data_vencimento, prioridade))
                task = cursor.fetchone()
                conn.commit()

            date_str = f" para {data_vencimento.strftime('%d/%m %H:%M')}" if data_vencimento else ""
            proj_str = f" no projeto #{project_id}" if project_id else ""
            return json.dumps({
                "sucesso": True,
                "task_id": task["id"],
                "mensagem": f"Tarefa #{task['id']} criada: {titulo}{proj_str}{date_str}"
            }, ensure_ascii=False)

        elif action == "complete_task":
            task_id = params.get("task_id")
            if not task_id:
                return json.dumps({"erro": "task_id e obrigatorio"})

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE tasks SET status = 'completed', data_conclusao = NOW()
                    WHERE id = %s AND status = 'pending'
                    RETURNING id, titulo
                """, (task_id,))
                task = cursor.fetchone()
                conn.commit()

            if not task:
                return json.dumps({"erro": f"Tarefa #{task_id} nao encontrada ou ja concluida"})
            return json.dumps({
                "sucesso": True,
                "mensagem": f"Tarefa #{task['id']} concluida: {task['titulo']}"
            }, ensure_ascii=False)

        elif action == "save_note":
            project_id = params.get("project_id")
            titulo = params.get("titulo", "Nota via Bot")
            conteudo = params.get("conteudo", "")
            tipo = params.get("tipo", "insight")

            if not project_id:
                # Use first active project
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT id FROM projects WHERE status = 'ativo' ORDER BY prioridade ASC LIMIT 1")
                    row = cursor.fetchone()
                    project_id = row["id"] if row else None

            if not project_id:
                return json.dumps({"erro": "Nenhum projeto ativo encontrado"})

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor)
                    VALUES (%s, %s, %s, %s, 'Renato (via Bot)')
                    RETURNING id
                """, (project_id, tipo, titulo, conteudo))
                note = cursor.fetchone()
                conn.commit()

            return json.dumps({
                "sucesso": True,
                "note_id": note["id"],
                "mensagem": f"Nota '{titulo}' salva no projeto #{project_id}"
            }, ensure_ascii=False)

        elif action == "save_memory":
            contact_id = params.get("contact_id")
            if not contact_id:
                return json.dumps({"erro": "contact_id e obrigatorio"})
            titulo = params.get("titulo", "Memoria via Bot")
            resumo = params.get("resumo", "")
            conteudo_completo = params.get("conteudo_completo", resumo)
            tipo = params.get("tipo", "insight")

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO contact_memories (contact_id, tipo, titulo, resumo, conteudo_completo, data_ocorrencia)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    RETURNING id
                """, (contact_id, tipo, titulo, resumo, conteudo_completo))
                mem = cursor.fetchone()
                conn.commit()

            return json.dumps({
                "sucesso": True,
                "memory_id": mem["id"],
                "mensagem": f"Memoria '{titulo}' salva para contato #{contact_id}"
            }, ensure_ascii=False)

        elif action == "schedule_meeting":
            titulo = params.get("titulo")
            data_hora = params.get("data_hora")
            if not titulo or not data_hora:
                return json.dumps({"erro": "titulo e data_hora sao obrigatorios"})

            duracao_min = params.get("duracao_min", 60)
            contact_id = params.get("contact_id")
            local = params.get("local")

            try:
                start_dt = datetime.fromisoformat(data_hora.replace("Z", "+00:00"))
            except ValueError:
                return json.dumps({"erro": f"Formato de data invalido: {data_hora}. Use ISO 8601 (ex: 2026-04-15T10:00:00)"})

            end_dt = start_dt + timedelta(minutes=duracao_min)

            from services.calendar_events import get_calendar_events
            cal = get_calendar_events()
            event = cal.create_event(
                summary=titulo,
                start_datetime=start_dt,
                end_datetime=end_dt,
                location=local,
                contact_id=contact_id,
                create_in_google=True
            )

            return json.dumps({
                "sucesso": True,
                "event_id": event.get("id"),
                "mensagem": f"Evento '{titulo}' criado em {start_dt.strftime('%d/%m %H:%M')} ({duracao_min}min)"
            }, ensure_ascii=False)

        elif action == "send_whatsapp":
            contact_id = params.get("contact_id")
            message = params.get("message")
            if not contact_id or not message:
                return json.dumps({"erro": "contact_id e message sao obrigatorios"})

            from integrations.evolution_api import get_evolution_client

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, nome, telefones FROM contacts WHERE id = %s", (contact_id,))
                contact = cursor.fetchone()

            if not contact:
                return json.dumps({"erro": f"Contato #{contact_id} nao encontrado"})

            contact = dict(contact)
            phones = contact.get("telefones")
            if isinstance(phones, str):
                try:
                    phones = json.loads(phones)
                except:
                    phones = []

            if not phones or (isinstance(phones, list) and not phones):
                return json.dumps({"erro": f"Contato {contact['nome']} nao tem telefone"})

            phone = phones[0] if isinstance(phones, list) else str(phones)
            phone_clean = ''.join(filter(str.isdigit, str(phone)))

            client = get_evolution_client()
            result = await client.send_text(phone_clean, message, instance_name="rap-whatsapp")

            if "error" not in result:
                return json.dumps({
                    "sucesso": True,
                    "mensagem": f"Mensagem enviada para {contact['nome']}"
                }, ensure_ascii=False)
            else:
                return json.dumps({"erro": f"Falha ao enviar: {result.get('error', 'desconhecido')}"})

        elif action == "enrich_contact":
            contact_id = params.get("contact_id")
            if not contact_id:
                return json.dumps({"erro": "contact_id e obrigatorio"})

            from services.contact_enrichment import enrich_contact_with_ai

            with get_db() as conn:
                result = await enrich_contact_with_ai(contact_id, conn)

            return json.dumps({
                "sucesso": True,
                "mensagem": f"Contato #{contact_id} enriquecido com IA",
                "resultado": {k: str(v)[:100] for k, v in result.items()} if isinstance(result, dict) else str(result)[:200]
            }, ensure_ascii=False)

        elif action == "update_contact":
            contact_id = params.get("contact_id")
            fields = params.get("fields", {})
            if not contact_id or not fields:
                return json.dumps({"erro": "contact_id e fields sao obrigatorios"})

            # Whitelist of updatable fields
            allowed = {
                "nome", "apelido", "empresa", "cargo", "emails", "telefones",
                "linkedin", "circulo", "relationship_context", "manual_notes",
                "company_website", "contexto"
            }
            safe_fields = {k: v for k, v in fields.items() if k in allowed}
            if not safe_fields:
                return json.dumps({"erro": f"Nenhum campo permitido. Campos validos: {', '.join(sorted(allowed))}"})

            set_clauses = []
            values = []
            for k, v in safe_fields.items():
                set_clauses.append(f"{k} = %s")
                values.append(v)
            values.append(contact_id)

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"UPDATE contacts SET {', '.join(set_clauses)}, atualizado_em = CURRENT_TIMESTAMP WHERE id = %s RETURNING id, nome",
                    values
                )
                updated = cursor.fetchone()
                conn.commit()

            if not updated:
                return json.dumps({"erro": f"Contato #{contact_id} nao encontrado"})

            return json.dumps({
                "sucesso": True,
                "mensagem": f"Contato {updated['nome']} atualizado: {', '.join(safe_fields.keys())}"
            }, ensure_ascii=False)

        else:
            return json.dumps({"erro": f"Acao desconhecida: {action}. Acoes validas: create_task, complete_task, save_note, save_memory, schedule_meeting, send_whatsapp, enrich_contact, update_contact"})

    except Exception as e:
        logger.error(f"execute_action error ({action}): {e}")
        return json.dumps({"erro": str(e)})


async def _tool_draft_message(contact_id: int, context: str) -> str:
    """Draft a personalized message using full AI-enriched contact context."""
    try:
        from services.contact_enrichment import get_contact_context, format_messages_for_ai

        with get_db() as conn:
            cursor = conn.cursor()

            # Contact info
            cursor.execute("""
                SELECT id, nome, empresa, cargo, linkedin_headline, linkedin_about,
                       linkedin_location, linkedin_experience, relationship_context,
                       resumo_ai, ultimo_contato, circulo
                FROM contacts WHERE id = %s
            """, (contact_id,))
            contact = cursor.fetchone()
            if not contact:
                return json.dumps({"erro": f"Contato #{contact_id} nao encontrado"})
            contact = dict(contact)

            # Full enriched context (same as "Enriquecer com IA" button)
            full_context = await get_contact_context(contact_id, conn)

            # Format all data sources
            whatsapp_text = format_messages_for_ai(
                full_context.get("whatsapp_messages", []), contact["nome"], "WhatsApp"
            ) or "Sem mensagens WhatsApp"

            email_text = format_messages_for_ai(
                full_context.get("email_messages", []), contact["nome"], "Email"
            ) or "Sem emails"

            facts_text = "\n".join(
                f"- [{f.get('categoria', '?')}] {f.get('fato', '')}"
                for f in full_context.get("existing_facts", [])
            ) or "Sem fatos registrados"

            memories_text = "\n".join(
                f"- {m.get('titulo', '?')}: {m.get('resumo', '')[:100]}"
                for m in full_context.get("memories", [])
            ) or "Sem memorias"

        # Build rich context
        contact_ctx = f"Nome: {contact['nome']}, Empresa: {contact.get('empresa', '?')}, Cargo: {contact.get('cargo', '?')}"
        if contact.get("linkedin_headline"):
            contact_ctx += f"\nLinkedIn: {contact['linkedin_headline']}"
        if contact.get("linkedin_about"):
            contact_ctx += f"\nSobre: {contact['linkedin_about'][:200]}"
        if contact.get("relationship_context"):
            contact_ctx += f"\nContexto do relacionamento: {contact['relationship_context']}"
        if contact.get("resumo_ai"):
            contact_ctx += f"\nResumo IA: {contact['resumo_ai'][:200]}"
        if contact.get("ultimo_contato"):
            contact_ctx += f"\nUltimo contato: {contact['ultimo_contato']}"

        system = f"""Voce e o assistente de Renato Prado. Escreva um rascunho de mensagem WhatsApp para o contato abaixo.
A mensagem deve ser natural, no tom do Renato (profissional mas cordial), em portugues.
Use o contexto completo do relacionamento para personalizar.

CONTATO:
{contact_ctx}

WHATSAPP (historico):
{whatsapp_text[:500]}

EMAILS:
{email_text[:300]}

FATOS CONHECIDOS:
{facts_text}

MEMORIAS:
{memories_text}

OBJETIVO: {context}

REGRAS CRITICAS:
- NUNCA invente fatos. Se nao sabe se a pessoa curtiu, comentou ou fez algo, NAO mencione.
- Use APENAS informacoes que estao nos dados acima.
- Se o objetivo menciona "meu post", inclua o link se disponivel no contexto.
- Escreva no tom do Renato: profissional, cordial, direto.

Escreva APENAS a mensagem, pronta para enviar. Sem explicacoes."""

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 300,
                    "system": system,
                    "messages": [{"role": "user", "content": f"Escreva a mensagem para {contact['nome']}: {context}"}],
                },
            )

            if response.status_code != 200:
                return json.dumps({"erro": "Falha ao gerar rascunho"})

            result = response.json()
            draft = result.get("content", [{}])[0].get("text", "").strip()

        return json.dumps({
            "rascunho": draft,
            "contato": contact["nome"],
            "contact_id": contact_id
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"draft_message error: {e}")
        return json.dumps({"erro": str(e)})


async def _execute_tool(name: str, input_data: Dict) -> str:
    """Execute a tool by name and return the result as string."""
    try:
        if name == "query_intel":
            return _tool_query_intel(input_data["sql"])
        elif name == "query_conselhoos":
            return _tool_query_conselhoos(input_data["sql"])
        elif name == "execute_action":
            return await _tool_execute_action(input_data["action"], input_data.get("params", {}))
        elif name == "draft_message":
            return await _tool_draft_message(input_data["contact_id"], input_data["context"])
        elif name == "project_chat":
            from services.project_assistant import chat as project_chat
            result = await project_chat(input_data["project_id"], input_data["message"])
            return result.get("response", result.get("error", "Sem resposta"))
        else:
            return json.dumps({"erro": f"Tool desconhecida: {name}"})
    except Exception as e:
        logger.error(f"Tool execution error ({name}): {e}")
        return json.dumps({"erro": str(e)})


# ==================== CONVERSATION MEMORY ====================

def _load_conversation_history(phone: str, limit: int = 20) -> List[Dict]:
    """Load recent conversation messages for this phone."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT role, content, tool_calls, tool_results
                FROM bot_conversations
                WHERE phone = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (phone, limit))
            rows = [dict(r) for r in cursor.fetchall()]

        # Reverse to chronological order
        rows.reverse()
        return rows
    except Exception as e:
        logger.error(f"Error loading conversation history: {e}")
        return []


def _save_conversation_message(phone: str, role: str, content: str,
                                tool_calls: Any = None, tool_results: Any = None):
    """Save a single message to conversation history."""
    try:
        tc_json = json.dumps(tool_calls) if tool_calls else None
        tr_json = json.dumps(tool_results) if tool_results else None

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO bot_conversations (phone, role, content, tool_calls, tool_results)
                VALUES (%s, %s, %s, %s, %s)
            """, (phone, role, content, tc_json, tr_json))
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving conversation message: {e}")


def _build_messages_from_history(history: List[Dict]) -> List[Dict]:
    """Convert DB history rows to Claude messages format."""
    messages = []
    for row in history:
        role = row["role"]
        content = row["content"]

        if role == "user":
            messages.append({"role": "user", "content": content})
        elif role == "assistant":
            # If there were tool calls, reconstruct the multi-block content
            if row.get("tool_calls"):
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                tool_calls = row["tool_calls"]
                if isinstance(tool_calls, str):
                    tool_calls = json.loads(tool_calls)
                for tc in tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["input"]
                    })
                messages.append({"role": "assistant", "content": blocks})

                # Add tool results
                if row.get("tool_results"):
                    tool_results = row["tool_results"]
                    if isinstance(tool_results, str):
                        tool_results = json.loads(tool_results)
                    result_blocks = []
                    for tr in tool_results:
                        result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tr["tool_use_id"],
                            "content": tr["content"]
                        })
                    messages.append({"role": "user", "content": result_blocks})
            else:
                messages.append({"role": "assistant", "content": content})

    return messages


# ==================== SYSTEM PROMPT ====================

def _build_system_prompt() -> str:
    """Build the rich system prompt with CRM context."""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d %A %H:%M")

    # Get active projects summary
    projects_str = ""
    overdue_count = 0
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, nome, status FROM projects
                WHERE status = 'ativo'
                ORDER BY prioridade ASC
                LIMIT 15
            """)
            projects = [dict(r) for r in cursor.fetchall()]
            projects_str = "\n".join(f"  - [{p['id']}] {p['nome']}" for p in projects) or "  Nenhum"

            cursor.execute("""
                SELECT COUNT(*) as total FROM tasks
                WHERE status = 'pending' AND data_vencimento IS NOT NULL
                AND data_vencimento < CURRENT_DATE
            """)
            overdue_count = cursor.fetchone()["total"]
    except Exception as e:
        logger.error(f"Error building system prompt context: {e}")

    return f"""Voce e o INTEL Bot, assistente pessoal de Renato Prado no WhatsApp.
Voce tem acesso TOTAL ao sistema INTEL via SQL e acoes. Pode consultar QUALQUER dado e executar QUALQUER acao.

SOBRE RENATO:
- CEO e consultor de governanca corporativa
- Cofundador do ImenSIAH (instituto de mentoria para conselheiros)
- Atua com conselhos de administracao, family offices, governanca

CONTEXTO ATUAL:
- Data/hora: {today_str}
- Projetos ativos:
{projects_str}
- Tarefas vencidas: {overdue_count}

## SCHEMA DO BANCO (tabelas principais para query_intel):

contacts: id, nome, apelido, empresa, cargo, emails (jsonb), telefones (jsonb), linkedin, linkedin_url, linkedin_headline, linkedin_about, linkedin_experience, linkedin_skills, linkedin_location, circulo (C1-C5), health_score, ultimo_contato, resumo_ai, relationship_context, manual_notes, foto_url, company_website, contexto, total_interacoes, criado_em, atualizado_em

messages: id, conversation_id, contact_id, direcao (incoming/outgoing), conteudo, tipo, enviado_em, lido

conversations: id, contact_id, canal (whatsapp/email), ultimo_mensagem, total_mensagens

contact_memories: id, contact_id, tipo (insight/reuniao/fato/relato), titulo, resumo, conteudo_completo, data_ocorrencia, fonte, criado_em

contact_facts: id, contact_id, categoria, fato, fonte, confianca, criado_em

projects: id, nome, tipo (negocio/patrimonio/pessoal/conselho), status (ativo/pausado/concluido), descricao, prioridade, data_previsao, criado_em

project_members: project_id, contact_id, papel

project_notes: id, project_id, tipo, titulo, conteudo, autor, criado_em

tasks: id, titulo, descricao, status (pending/completed), project_id, contact_id, data_vencimento, data_conclusao, prioridade (1-10), ai_generated, origem, data_criacao

calendar_events: id, summary, start_datetime, end_datetime, contact_id, location, description, google_event_id

editorial_posts: id, article_title, tipo, status, data_publicacao, linkedin_impressoes, linkedin_reacoes, linkedin_comentarios, linkedin_compartilhamentos

hot_takes: id, news_title, hook, body, status, published_at, criado_em

action_proposals: id, action_type, title, description, status, contact_id, urgency, criado_em

campaigns: id, nome, tipo, status, descricao
campaign_enrollments: id, campaign_id, contact_id, status

contact_rodas: id, contact_id, roda_nome, data_inicio (rodas de networking)

## CONSELHOOS DATABASE (query_conselhoos):
O ConselhoOS e o sistema de governanca corporativa do Renato. Banco separado do INTEL.
Use a tool query_conselhoos para consultar.

empresas: id (uuid), nome, setor, descricao, created_at
reunioes: id (uuid), empresa_id (uuid), titulo, data (timestamp), status, pauta_md (text), transcricao (text), transcricao_resumo (text), ata_md (text), ata_docx_drive_id, dossie_md (text), fathom_recording_id, created_at
raci_itens: id (uuid), empresa_id (uuid), area, acao, prazo, status, responsavel_r
decisoes: id, empresa_id, reuniao_id, decisao, area
temas_reuniao: id, reuniao_id, titulo, ordem
pautas_anuais: id, empresa_id, titulo
documentos: id, empresa_id, titulo, tipo, url

### Exemplos ConselhoOS:
- Reunioes de empresa: SELECT r.titulo, r.data, r.status, LENGTH(r.ata_md) as ata_chars FROM reunioes r JOIN empresas e ON e.id = r.empresa_id WHERE e.nome ILIKE '%vallen%' ORDER BY r.data DESC
- Buscar ata: SELECT ata_md FROM reunioes WHERE id = 'uuid'
- Tarefas RACI pendentes: SELECT area, acao, prazo, status FROM raci_itens WHERE empresa_id = 'uuid' AND status IN ('pendente', 'em_andamento')
- Decisoes de reuniao: SELECT d.decisao, d.area FROM decisoes d WHERE d.reuniao_id = 'uuid'

## DICAS SQL:
- Buscar contato por nome: SELECT id, nome, empresa, cargo FROM contacts WHERE nome ILIKE '%termo%'
- Mensagens recentes de um contato: SELECT m.conteudo, m.direcao, m.enviado_em FROM messages m JOIN conversations cv ON cv.id = m.conversation_id WHERE cv.contact_id = X ORDER BY m.enviado_em DESC LIMIT 10
- Tarefas pendentes: SELECT id, titulo, data_vencimento FROM tasks WHERE status = 'pending' ORDER BY data_vencimento ASC NULLS LAST
- Projetos ativos: SELECT id, nome, tipo FROM projects WHERE status = 'ativo' ORDER BY prioridade ASC
- Eventos de hoje: SELECT summary, start_datetime, end_datetime FROM calendar_events WHERE start_datetime::date = CURRENT_DATE ORDER BY start_datetime
- Contatos por circulo: SELECT nome, empresa FROM contacts WHERE circulo = 'C1'
- Memorias de contato: SELECT titulo, resumo, data_ocorrencia FROM contact_memories WHERE contact_id = X ORDER BY data_ocorrencia DESC
- Fatos de contato: SELECT categoria, fato FROM contact_facts WHERE contact_id = X

REGRAS CRITICAS:
- NUNCA invente, assuma ou fabrique informacoes. Se nao tem o dado no banco, NAO afirme. Diga "nao tenho essa informacao".
- NUNCA diga que alguem curtiu, comentou ou fez algo a menos que tenha EVIDENCIA no banco de dados.
- Quando Renato mencionar "meu post", consulte editorial_posts para pegar o link (url_publicado ou linkedin_post_url) e inclua na mensagem.
- Responda SEMPRE em portugues
- Seja conciso e direto (sao mensagens WhatsApp)
- Use query_intel para consultar QUALQUER dado — SEMPRE consulte antes de afirmar
- Use execute_action para criar/modificar dados
- Use draft_message para rascunhos personalizados (ele tambem segue estas regras)
- Para datas relativas, use {now.strftime('%Y-%m-%d')} como referencia
- Formate respostas com *negrito* para destaques (WhatsApp markdown)
- Voce pode fazer multiplas queries em sequencia para responder perguntas complexas
- Se nao souber algo, diga e sugira como ajudar"""


# ==================== MAIN HANDLER ====================

async def handle_bot_message(phone: str, message: str, message_id: str) -> str:
    """
    Main entry point for bot messages from intel-bot instance.
    Uses Claude tool_use for dynamic function calling with conversation memory.
    """
    # 1. Verify sender is Renato
    if not _is_renato(phone):
        logger.warning(f"Unauthorized bot message from {phone}")
        return "Este bot e de uso exclusivo. Acesso nao autorizado."

    # 2. Skip trivial messages
    if SKIP_PATTERNS.match(message.strip()):
        logger.debug(f"Skipping trivial message: {message}")
        return ""

    # 3. Save user message to history
    _save_conversation_message(phone, "user", message)

    # 4. Load conversation history
    history = _load_conversation_history(phone, limit=20)
    messages = _build_messages_from_history(history)

    # Ensure we have the current user message (if history didn't include it yet)
    if not messages or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": message})

    # 5. Build system prompt
    system_prompt = _build_system_prompt()

    # 6. Call Claude with tool_use in a loop
    if not ANTHROPIC_API_KEY:
        return "Erro: ANTHROPIC_API_KEY nao configurada."

    final_text = ""
    try:
        for iteration in range(MAX_TOOL_ITERATIONS):
            logger.info(f"Claude call iteration {iteration + 1}/{MAX_TOOL_ITERATIONS}")

            async with httpx.AsyncClient(timeout=25.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": 1000,
                        "system": system_prompt,
                        "tools": TOOLS,
                        "messages": messages,
                    },
                )

            if response.status_code != 200:
                logger.error(f"Claude API error: {response.status_code} - {response.text}")
                _save_conversation_message(phone, "assistant", "Erro ao processar. Tenta de novo?")
                return "Desculpa, tive um erro ao processar. Tenta de novo?"

            result = response.json()
            stop_reason = result.get("stop_reason", "")
            content_blocks = result.get("content", [])

            # Extract text and tool_use blocks
            text_parts = []
            tool_use_blocks = []

            for block in content_blocks:
                if block.get("type") == "text":
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_use_blocks.append(block)

            current_text = "\n".join(text_parts).strip()

            # If no tool calls, we're done
            if stop_reason != "tool_use" or not tool_use_blocks:
                final_text = current_text
                _save_conversation_message(phone, "assistant", final_text)
                break

            # Execute tool calls
            tool_calls_data = []
            tool_results_data = []

            # Add assistant message with tool calls to messages
            messages.append({"role": "assistant", "content": content_blocks})

            tool_result_blocks = []
            for tool_block in tool_use_blocks:
                tool_name = tool_block["name"]
                tool_input = tool_block["input"]
                tool_id = tool_block["id"]

                logger.info(f"Executing tool: {tool_name} with input: {json.dumps(tool_input, ensure_ascii=False)[:200]}")

                tool_result = await _execute_tool(tool_name, tool_input)

                tool_calls_data.append({
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input
                })
                tool_results_data.append({
                    "tool_use_id": tool_id,
                    "content": tool_result
                })
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": tool_result
                })

            # Add tool results to messages
            messages.append({"role": "user", "content": tool_result_blocks})

            # Save the tool interaction to history
            _save_conversation_message(
                phone, "assistant", current_text,
                tool_calls=tool_calls_data,
                tool_results=tool_results_data
            )

        else:
            # Max iterations reached — summarize what was found
            if not final_text:
                # Try to extract useful info from the last tool results
                last_results = [msg.get("content", "") for msg in messages if msg.get("role") == "user" and isinstance(msg.get("content"), list)]
                final_text = "Busquei no sistema mas não encontrei uma resposta definitiva. Pode reformular a pergunta ou dar mais detalhes?"
                _save_conversation_message(phone, "assistant", final_text)

    except httpx.TimeoutException:
        logger.error("Claude API timeout")
        final_text = "Desculpa, demorou demais para processar. Tenta de novo?"
        _save_conversation_message(phone, "assistant", final_text)
    except Exception as e:
        logger.error(f"handle_bot_message error: {e}", exc_info=True)
        final_text = f"Erro interno: {e}. Tenta de novo?"
        _save_conversation_message(phone, "assistant", final_text)

    return final_text


# ==================== NOTIFICATION HELPER ====================

async def send_intel_notification(text: str, phone: str = RENATO_PHONE) -> bool:
    """
    Send a notification message via the intel-bot WhatsApp instance.

    This is the standard way for the system to notify Renato:
    - Editorial PDCA weekly briefing
    - Task reminders
    - Action proposals
    - System alerts

    Args:
        text: Message text to send
        phone: Destination phone (default: Renato)

    Returns:
        True if sent successfully
    """
    from integrations.evolution_api import EvolutionAPIClient

    try:
        client = EvolutionAPIClient(instance_name=INTEL_BOT_INSTANCE)

        if not client.is_configured:
            logger.warning("Evolution API not configured, cannot send intel notification")
            return False

        result = await client.send_text(phone, text, instance_name=INTEL_BOT_INSTANCE)

        if "error" not in result:
            logger.info(f"Intel notification sent to {phone}: {text[:80]}...")
            return True
        else:
            logger.error(f"Intel notification failed: {result.get('error')}")
            return False

    except Exception as e:
        logger.error(f"Error sending intel notification: {e}")
        return False
