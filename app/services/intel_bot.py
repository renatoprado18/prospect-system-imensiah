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
MAX_TOOL_ITERATIONS = 3

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


# ==================== TOOL DEFINITIONS ====================

TOOLS = [
    {
        "name": "search_contact",
        "description": "Busca contatos pelo nome. Retorna id, nome, empresa, cargo, telefone e data da ultima mensagem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Nome ou parte do nome do contato"
                }
            },
            "required": ["name"]
        }
    },
    {
        "name": "get_contact_detail",
        "description": "Retorna informacoes completas de um contato: dados, ultimas 5 mensagens e ultimas 3 memorias.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "integer",
                    "description": "ID do contato"
                }
            },
            "required": ["contact_id"]
        }
    },
    {
        "name": "create_task",
        "description": "Cria uma tarefa no CRM. Pode vincular a um projeto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {
                    "type": "string",
                    "description": "Titulo da tarefa"
                },
                "descricao": {
                    "type": "string",
                    "description": "Descricao opcional"
                },
                "project_id": {
                    "type": "integer",
                    "description": "ID do projeto (opcional)"
                },
                "prazo_dias": {
                    "type": "integer",
                    "description": "Prazo em dias a partir de hoje (ex: 1 = amanha, 7 = semana que vem)"
                }
            },
            "required": ["titulo"]
        }
    },
    {
        "name": "complete_task",
        "description": "Marca uma tarefa como concluida.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "ID da tarefa a concluir"
                }
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "get_overdue_tasks",
        "description": "Retorna todas as tarefas pendentes com data de vencimento anterior a hoje.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_project_status",
        "description": "Retorna informacoes de um projeto: dados, tarefas pendentes e notas recentes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "integer",
                    "description": "ID do projeto"
                }
            },
            "required": ["project_id"]
        }
    },
    {
        "name": "save_insight",
        "description": "Salva um insight/nota no CRM. Pode vincular a um contato ou projeto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Conteudo do insight/nota"
                },
                "contact_id": {
                    "type": "integer",
                    "description": "ID do contato (opcional)"
                },
                "project_id": {
                    "type": "integer",
                    "description": "ID do projeto (opcional)"
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "schedule_meeting",
        "description": "Cria um evento no calendario (Google Calendar).",
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {
                    "type": "string",
                    "description": "Titulo do evento"
                },
                "data_hora": {
                    "type": "string",
                    "description": "Data e hora no formato ISO 8601 (ex: 2026-04-15T10:00:00)"
                },
                "duracao_min": {
                    "type": "integer",
                    "description": "Duracao em minutos (padrao: 60)"
                },
                "contact_id": {
                    "type": "integer",
                    "description": "ID do contato participante (opcional)"
                },
                "local": {
                    "type": "string",
                    "description": "Local do evento (opcional)"
                }
            },
            "required": ["titulo", "data_hora"]
        }
    },
    {
        "name": "send_whatsapp",
        "description": "Envia mensagem WhatsApp para um contato via instancia principal (rap-whatsapp). Use para enviar mensagens a terceiros, NAO para responder ao Renato.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "integer",
                    "description": "ID do contato destinatario"
                },
                "message": {
                    "type": "string",
                    "description": "Texto da mensagem a enviar"
                }
            },
            "required": ["contact_id", "message"]
        }
    },
    {
        "name": "get_calendar_today",
        "description": "Retorna os eventos de hoje no calendario.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "search_projects",
        "description": "Busca projetos pelo nome.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Termo de busca"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "draft_message",
        "description": "Gera um rascunho de mensagem personalizada para um contato, usando contexto de mensagens recentes, memorias e dados do LinkedIn.",
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
]


# ==================== TOOL IMPLEMENTATIONS ====================

def _tool_search_contact(name: str) -> str:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, c.cargo, c.telefones,
                    (SELECT m.conteudo FROM messages m
                     JOIN conversations cv ON cv.id = m.conversation_id
                     WHERE cv.contact_id = c.id
                     ORDER BY m.enviado_em DESC NULLS LAST LIMIT 1) as ultima_msg,
                    (SELECT m.enviado_em FROM messages m
                     JOIN conversations cv ON cv.id = m.conversation_id
                     WHERE cv.contact_id = c.id
                     ORDER BY m.enviado_em DESC NULLS LAST LIMIT 1) as data_ultima_msg
                FROM contacts c
                WHERE c.nome ILIKE %s
                ORDER BY c.ultimo_contato DESC NULLS LAST
                LIMIT 10
            """, (f"%{name}%",))
            contacts = [dict(r) for r in cursor.fetchall()]

        if not contacts:
            return json.dumps({"resultado": f"Nenhum contato encontrado com '{name}'"})

        results = []
        for c in contacts:
            phone = ""
            if c.get("telefones"):
                phones = c["telefones"]
                if isinstance(phones, str):
                    try:
                        phones = json.loads(phones)
                    except:
                        phones = []
                if isinstance(phones, list) and phones:
                    phone = str(phones[0])

            results.append({
                "id": c["id"],
                "nome": c["nome"],
                "empresa": c.get("empresa", ""),
                "cargo": c.get("cargo", ""),
                "telefone": phone,
                "ultima_msg": (c.get("ultima_msg") or "")[:100],
                "data_ultima_msg": str(c["data_ultima_msg"]) if c.get("data_ultima_msg") else None
            })

        return json.dumps({"contatos": results}, default=str, ensure_ascii=False)
    except Exception as e:
        logger.error(f"search_contact error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_get_contact_detail(contact_id: int) -> str:
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, nome, empresa, cargo, telefones, emails,
                       linkedin_url, circulo, health_score, ultimo_contato,
                       linkedin_headline, linkedin_about
                FROM contacts WHERE id = %s
            """, (contact_id,))
            contact = cursor.fetchone()
            if not contact:
                return json.dumps({"erro": f"Contato #{contact_id} nao encontrado"})
            contact = dict(contact)

            # Last 5 messages
            cursor.execute("""
                SELECT m.conteudo, m.direcao, m.enviado_em
                FROM messages m
                JOIN conversations cv ON cv.id = m.conversation_id
                WHERE cv.contact_id = %s
                ORDER BY m.enviado_em DESC NULLS LAST
                LIMIT 5
            """, (contact_id,))
            msgs = [dict(r) for r in cursor.fetchall()]

            # Last 3 memories
            cursor.execute("""
                SELECT tipo, titulo, resumo, data_ocorrencia
                FROM contact_memories
                WHERE contact_id = %s
                ORDER BY data_ocorrencia DESC
                LIMIT 3
            """, (contact_id,))
            mems = [dict(r) for r in cursor.fetchall()]

        return json.dumps({
            "contato": contact,
            "ultimas_mensagens": msgs,
            "memorias_recentes": mems
        }, default=str, ensure_ascii=False)
    except Exception as e:
        logger.error(f"get_contact_detail error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_create_task(titulo: str, descricao: str = "", project_id: int = None, prazo_dias: int = None) -> str:
    try:
        data_vencimento = None
        if prazo_dias is not None:
            data_vencimento = datetime.now() + timedelta(days=prazo_dias)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tasks (
                    titulo, descricao, project_id, contact_id,
                    data_vencimento, prioridade, ai_generated, origem, status
                ) VALUES (%s, %s, %s, %s, %s, 5, TRUE, 'intel_bot', 'pending')
                RETURNING id
            """, (titulo, descricao, project_id, 14911, data_vencimento))
            task = cursor.fetchone()
            conn.commit()

        date_str = f" para {data_vencimento.strftime('%d/%m %H:%M')}" if data_vencimento else ""
        proj_str = f" no projeto #{project_id}" if project_id else ""
        return json.dumps({
            "sucesso": True,
            "task_id": task["id"],
            "mensagem": f"Tarefa #{task['id']} criada: {titulo}{proj_str}{date_str}"
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"create_task error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_complete_task(task_id: int) -> str:
    try:
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
    except Exception as e:
        logger.error(f"complete_task error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_get_overdue_tasks() -> str:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT t.id, t.titulo, t.data_vencimento, t.prioridade,
                       p.nome as projeto
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                WHERE t.status = 'pending'
                AND t.data_vencimento IS NOT NULL
                AND t.data_vencimento < CURRENT_DATE
                ORDER BY t.data_vencimento ASC
                LIMIT 20
            """)
            tasks = [dict(r) for r in cursor.fetchall()]

        if not tasks:
            return json.dumps({"mensagem": "Nenhuma tarefa vencida!"})

        return json.dumps({"tarefas_vencidas": tasks, "total": len(tasks)}, default=str, ensure_ascii=False)
    except Exception as e:
        logger.error(f"get_overdue_tasks error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_get_project_status(project_id: int) -> str:
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, nome, descricao, tipo, status, prioridade, data_previsao
                FROM projects WHERE id = %s
            """, (project_id,))
            project = cursor.fetchone()
            if not project:
                return json.dumps({"erro": f"Projeto #{project_id} nao encontrado"})
            project = dict(project)

            # Pending tasks
            cursor.execute("""
                SELECT id, titulo, status, data_vencimento, prioridade
                FROM tasks WHERE project_id = %s AND status = 'pending'
                ORDER BY data_vencimento ASC NULLS LAST
                LIMIT 10
            """, (project_id,))
            tasks = [dict(r) for r in cursor.fetchall()]

            # Recent notes
            cursor.execute("""
                SELECT titulo, conteudo, tipo, criado_em
                FROM project_notes WHERE project_id = %s
                ORDER BY criado_em DESC LIMIT 5
            """, (project_id,))
            notes = [dict(r) for r in cursor.fetchall()]

        return json.dumps({
            "projeto": project,
            "tarefas_pendentes": tasks,
            "notas_recentes": notes
        }, default=str, ensure_ascii=False)
    except Exception as e:
        logger.error(f"get_project_status error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_save_insight(text: str, contact_id: int = None, project_id: int = None) -> str:
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            if contact_id:
                cursor.execute("""
                    INSERT INTO contact_memories (contact_id, tipo, titulo, resumo, conteudo_completo, data_ocorrencia)
                    VALUES (%s, 'insight', %s, %s, %s, NOW())
                    RETURNING id
                """, (contact_id, text[:80], text[:200], text))
                mem = cursor.fetchone()
                conn.commit()
                return json.dumps({
                    "sucesso": True,
                    "tipo": "contact_memory",
                    "id": mem["id"],
                    "mensagem": f"Insight salvo como memoria do contato #{contact_id}"
                }, ensure_ascii=False)

            if project_id:
                cursor.execute("""
                    INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor)
                    VALUES (%s, 'insight', %s, %s, 'Renato (via Bot)')
                    RETURNING id
                """, (project_id, text[:80], text))
                note = cursor.fetchone()
                conn.commit()
                return json.dumps({
                    "sucesso": True,
                    "tipo": "project_note",
                    "id": note["id"],
                    "mensagem": f"Insight salvo no projeto #{project_id}"
                }, ensure_ascii=False)

            # Fallback: save in first active project
            cursor.execute("""
                INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor)
                VALUES (
                    (SELECT id FROM projects WHERE status = 'ativo' ORDER BY prioridade ASC LIMIT 1),
                    'insight', %s, %s, 'Renato (via Bot)'
                )
                RETURNING id, project_id
            """, (text[:80], text))
            note = cursor.fetchone()
            conn.commit()
            return json.dumps({
                "sucesso": True,
                "tipo": "project_note",
                "id": note["id"],
                "project_id": note["project_id"],
                "mensagem": f"Insight salvo como nota geral"
            }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"save_insight error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_schedule_meeting(titulo: str, data_hora: str, duracao_min: int = 60,
                           contact_id: int = None, local: str = None) -> str:
    try:
        start_dt = datetime.fromisoformat(data_hora.replace("Z", "+00:00"))
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
    except ValueError:
        return json.dumps({"erro": f"Formato de data invalido: {data_hora}. Use ISO 8601 (ex: 2026-04-15T10:00:00)"})
    except Exception as e:
        logger.error(f"schedule_meeting error: {e}")
        return json.dumps({"erro": str(e)})


async def _tool_send_whatsapp(contact_id: int, message: str) -> str:
    try:
        from integrations.evolution_api import get_evolution_client

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, telefones FROM contacts WHERE id = %s
            """, (contact_id,))
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

    except Exception as e:
        logger.error(f"send_whatsapp error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_get_calendar_today() -> str:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.id, e.summary, e.start_datetime, e.end_datetime,
                       e.location, e.description, c.nome as contact_name
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                WHERE e.start_datetime::date = CURRENT_DATE
                ORDER BY e.start_datetime ASC
                LIMIT 15
            """)
            events = [dict(r) for r in cursor.fetchall()]

        if not events:
            return json.dumps({"mensagem": "Nenhum evento hoje."})

        return json.dumps({"eventos_hoje": events}, default=str, ensure_ascii=False)
    except Exception as e:
        logger.error(f"get_calendar_today error: {e}")
        return json.dumps({"erro": str(e)})


def _tool_search_projects(query: str) -> str:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.id, p.nome, p.tipo, p.status, p.prioridade,
                    (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'pending') as tasks_pendentes
                FROM projects p
                WHERE p.nome ILIKE %s
                ORDER BY p.prioridade ASC
                LIMIT 10
            """, (f"%{query}%",))
            projects = [dict(r) for r in cursor.fetchall()]

        if not projects:
            return json.dumps({"resultado": f"Nenhum projeto encontrado com '{query}'"})

        return json.dumps({"projetos": projects}, default=str, ensure_ascii=False)
    except Exception as e:
        logger.error(f"search_projects error: {e}")
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
        if name == "search_contact":
            return _tool_search_contact(input_data["name"])
        elif name == "get_contact_detail":
            return _tool_get_contact_detail(input_data["contact_id"])
        elif name == "create_task":
            return _tool_create_task(
                titulo=input_data["titulo"],
                descricao=input_data.get("descricao", ""),
                project_id=input_data.get("project_id"),
                prazo_dias=input_data.get("prazo_dias")
            )
        elif name == "complete_task":
            return _tool_complete_task(input_data["task_id"])
        elif name == "get_overdue_tasks":
            return _tool_get_overdue_tasks()
        elif name == "get_project_status":
            return _tool_get_project_status(input_data["project_id"])
        elif name == "save_insight":
            return _tool_save_insight(
                text=input_data["text"],
                contact_id=input_data.get("contact_id"),
                project_id=input_data.get("project_id")
            )
        elif name == "schedule_meeting":
            return _tool_schedule_meeting(
                titulo=input_data["titulo"],
                data_hora=input_data["data_hora"],
                duracao_min=input_data.get("duracao_min", 60),
                contact_id=input_data.get("contact_id"),
                local=input_data.get("local")
            )
        elif name == "send_whatsapp":
            return await _tool_send_whatsapp(input_data["contact_id"], input_data["message"])
        elif name == "get_calendar_today":
            return _tool_get_calendar_today()
        elif name == "search_projects":
            return _tool_search_projects(input_data["query"])
        elif name == "draft_message":
            return await _tool_draft_message(input_data["contact_id"], input_data["context"])
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

SOBRE RENATO:
- CEO e consultor de governanca corporativa
- Cofundador do ImenSIAH (instituto de mentoria para conselheiros)
- Atua com conselhos de administracao, family offices, governanca

CONTEXTO ATUAL:
- Data/hora: {today_str}
- Projetos ativos:
{projects_str}
- Tarefas vencidas: {overdue_count}

REGRAS:
- Responda SEMPRE em portugues
- Seja conciso e direto (sao mensagens WhatsApp)
- Use as ferramentas para consultar/criar dados no CRM
- Se o usuario perguntar sobre contatos, projetos ou tarefas, USE as ferramentas para buscar dados reais
- Se pedir para criar tarefa, agendar reuniao, etc., USE a ferramenta correspondente
- NAO invente dados — consulte o CRM
- Para datas relativas, use {now.strftime('%Y-%m-%d')} como referencia
- Formate respostas com *negrito* para destaques (WhatsApp markdown)
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
            # Max iterations reached
            if not final_text:
                final_text = "Processamento completo. Me manda mais detalhes se precisar."
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
