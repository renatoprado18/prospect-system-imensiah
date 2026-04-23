"""
Intel Bot - Conversational WhatsApp Bot via intel-bot instance

Receives commands from Renato via WhatsApp, processes with Claude AI,
and executes actions in the CRM (create tasks, schedule meetings,
save insights, query data, send messages).
"""
import os
import re
import json
import httpx
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)

# Config
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
INTEL_BOT_INSTANCE = os.getenv("INTEL_BOT_INSTANCE", "intel-bot")
INTEL_BOT_NUMBER = os.getenv("INTEL_BOT_NUMBER", "5511915020192")
RENATO_PHONE = "5511984153337"
RENATO_PHONE_SUFFIXES = ["11984153337", "984153337"]

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


def _get_crm_context() -> str:
    """Build CRM context string for the Claude system prompt."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Active projects
            cursor.execute("""
                SELECT id, nome, tipo, status FROM projects
                WHERE status = 'ativo'
                ORDER BY prioridade ASC
                LIMIT 20
            """)
            projects = [dict(r) for r in cursor.fetchall()]

            # Pending tasks count
            cursor.execute("""
                SELECT COUNT(*) as total FROM tasks WHERE status = 'pending'
            """)
            pending_tasks = cursor.fetchone()["total"]

            # Overdue tasks count
            cursor.execute("""
                SELECT COUNT(*) as total FROM tasks
                WHERE status = 'pending' AND data_vencimento IS NOT NULL
                AND data_vencimento < CURRENT_DATE
            """)
            overdue_tasks = cursor.fetchone()["total"]

            # Today's events
            cursor.execute("""
                SELECT summary, start_datetime, contact_id
                FROM calendar_events
                WHERE start_datetime::date = CURRENT_DATE
                ORDER BY start_datetime ASC
                LIMIT 10
            """)
            today_events = [dict(r) for r in cursor.fetchall()]

            projects_str = "\n".join(
                f"  - [{p['id']}] {p['nome']} ({p['tipo']}, {p['status']})"
                for p in projects
            ) or "  Nenhum projeto ativo"

            events_str = "\n".join(
                f"  - {e['summary']} as {e['start_datetime'].strftime('%H:%M') if hasattr(e['start_datetime'], 'strftime') else e['start_datetime']}"
                for e in today_events
            ) or "  Nenhum evento hoje"

            return f"""PROJETOS ATIVOS:
{projects_str}

TAREFAS: {pending_tasks} pendentes, {overdue_tasks} vencidas

EVENTOS HOJE:
{events_str}"""

    except Exception as e:
        logger.error(f"Error building CRM context: {e}")
        return "Erro ao carregar contexto do CRM."


async def _call_claude(system_prompt: str, user_message: str, max_tokens: int = 500) -> str:
    """Call Claude API and return text response."""
    if not ANTHROPIC_API_KEY:
        return "Erro: ANTHROPIC_API_KEY nao configurada."

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                },
            )

            if response.status_code != 200:
                logger.error(f"Claude API error: {response.status_code} - {response.text}")
                return "Desculpa, tive um erro ao processar. Tenta de novo?"

            result = response.json()
            return result.get("content", [{}])[0].get("text", "").strip()

    except Exception as e:
        logger.error(f"Claude API exception: {e}")
        return "Erro de conexao com a IA. Tenta de novo em instantes."


async def _classify_intent(message: str) -> Dict:
    """Use Claude to classify the user's intent and extract parameters."""
    today = datetime.now().strftime("%Y-%m-%d %A")

    system = f"""Voce e um assistente de classificacao de intencoes. Hoje e {today}.
O usuario e Renato, CEO e consultor de governanca corporativa.

Classifique a mensagem em UMA das categorias e extraia parametros relevantes.
Responda APENAS com JSON valido, sem markdown.

Categorias:
- create_task: criar tarefa/lembrete. Params: titulo, descricao, project_id (se mencionado), data_vencimento (ISO), prioridade (1-10)
- schedule_meeting: agendar reuniao. Params: titulo, data (ISO datetime), duracao_min, descricao, participantes (lista nomes)
- save_insight: salvar nota/insight. Params: conteudo, project_id (se mencionado), contact_name (se mencionado), tipo (nota|insight|decisao)
- ask_question: pergunta sobre dados do CRM. Params: query (a pergunta original)
- send_message: enviar WhatsApp para alguem. Params: destinatario (nome), mensagem (conteudo a enviar)
- general: conversa geral. Params: {{}}

Para datas relativas, use {today} como referencia:
- "amanha" = dia seguinte
- "sexta" = proxima sexta-feira
- "semana que vem" = proxima segunda

JSON: {{"intent": "<categoria>", "params": {{...}}, "confidence": 0.0-1.0}}"""

    response = await _call_claude(system, message, max_tokens=300)

    try:
        # Clean potential markdown wrapping
        text = response.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        if text.endswith("```"):
            text = text[:-3]

        return json.loads(text.strip())
    except (json.JSONDecodeError, IndexError):
        logger.warning(f"Failed to parse intent JSON: {response}")
        return {"intent": "general", "params": {}, "confidence": 0.5}


async def _execute_create_task(params: Dict) -> str:
    """Create a task in the DB."""
    titulo = params.get("titulo", "Nova tarefa")
    descricao = params.get("descricao", "")
    project_id = params.get("project_id")
    prioridade = params.get("prioridade", 5)

    # Parse due date
    data_vencimento = None
    if params.get("data_vencimento"):
        try:
            data_vencimento = datetime.fromisoformat(params["data_vencimento"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tasks (
                    titulo, descricao, project_id, contact_id,
                    data_vencimento, prioridade, ai_generated, origem, status
                ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'intel_bot', 'pending')
                RETURNING id
            """, (
                titulo, descricao, project_id, 14911,  # Renato contact_id
                data_vencimento, prioridade
            ))
            task = cursor.fetchone()
            conn.commit()

        date_str = f" para {data_vencimento.strftime('%d/%m %H:%M')}" if data_vencimento else ""
        project_str = f" no projeto #{project_id}" if project_id else ""
        return f"Tarefa criada (#{task['id']}): *{titulo}*{project_str}{date_str}"

    except Exception as e:
        logger.error(f"Error creating task: {e}")
        return f"Erro ao criar tarefa: {e}"


async def _execute_schedule_meeting(params: Dict) -> str:
    """Create a calendar event."""
    from services.calendar_events import get_calendar_events

    titulo = params.get("titulo", "Reuniao")
    duracao = params.get("duracao_min", 60)
    descricao = params.get("descricao", "")

    # Parse date
    start_dt = None
    if params.get("data"):
        try:
            start_dt = datetime.fromisoformat(params["data"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return "Nao consegui entender a data. Tenta algo como 'sexta 15h' ou '2026-04-15 10:00'."

    if not start_dt:
        return "Preciso de uma data para agendar. Ex: 'Agendar call com Joao sexta 15h'"

    end_dt = start_dt + timedelta(minutes=duracao)

    try:
        cal = get_calendar_events()
        event = cal.create_event(
            summary=titulo,
            start_datetime=start_dt,
            end_datetime=end_dt,
            description=descricao,
            create_in_google=True
        )

        date_str = start_dt.strftime("%d/%m %H:%M")
        return f"Evento criado: *{titulo}* em {date_str} ({duracao}min)"

    except Exception as e:
        logger.error(f"Error scheduling meeting: {e}")
        return f"Erro ao agendar: {e}"


async def _execute_save_insight(params: Dict) -> str:
    """Save insight as project note or contact memory."""
    from services.projects import add_project_note

    conteudo = params.get("conteudo", "")
    project_id = params.get("project_id")
    contact_name = params.get("contact_name")
    tipo = params.get("tipo", "nota")

    if not conteudo:
        return "Conteudo vazio. Manda de novo com mais detalhes."

    try:
        # If linked to a project, save as project note
        if project_id:
            note = add_project_note(project_id, {
                "tipo": tipo,
                "titulo": conteudo[:80],
                "conteudo": conteudo,
                "autor": "Renato (via Bot)"
            })
            return f"Nota salva no projeto #{project_id}: *{conteudo[:60]}...*"

        # If linked to a contact, save as contact memory
        if contact_name:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, nome FROM contacts
                    WHERE nome ILIKE %s
                    LIMIT 1
                """, (f"%{contact_name}%",))
                contact = cursor.fetchone()

                if contact:
                    cursor.execute("""
                        INSERT INTO contact_memories (contact_id, tipo, titulo, resumo, conteudo_completo, data_ocorrencia)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        RETURNING id
                    """, (contact["id"], tipo, conteudo[:80], conteudo[:200], conteudo))
                    mem = cursor.fetchone()
                    conn.commit()
                    return f"Memoria salva para *{contact['nome']}*: {conteudo[:60]}..."
                else:
                    # Save as general note in a default project
                    pass

        # Fallback: save as general project note (project 0 or first active)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor)
                VALUES (
                    (SELECT id FROM projects WHERE status = 'ativo' ORDER BY prioridade ASC LIMIT 1),
                    %s, %s, %s, 'Renato (via Bot)'
                )
                RETURNING id, project_id
            """, (tipo, conteudo[:80], conteudo))
            note = cursor.fetchone()
            conn.commit()
            return f"Insight salvo (nota #{note['id']}): *{conteudo[:60]}...*"

    except Exception as e:
        logger.error(f"Error saving insight: {e}")
        return f"Erro ao salvar: {e}"


async def _execute_ask_question(params: Dict, crm_context: str) -> str:
    """Answer a CRM question using Claude with DB context."""
    query = params.get("query", "")

    # Get extra data based on likely query type
    extra_context = ""

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Overdue tasks detail
            if any(w in query.lower() for w in ["vencid", "atrasad", "overdue", "pendente"]):
                cursor.execute("""
                    SELECT t.id, t.titulo, t.data_vencimento, p.nome as projeto
                    FROM tasks t
                    LEFT JOIN projects p ON p.id = t.project_id
                    WHERE t.status = 'pending'
                    AND t.data_vencimento IS NOT NULL
                    AND t.data_vencimento < CURRENT_DATE
                    ORDER BY t.data_vencimento ASC
                    LIMIT 15
                """)
                tasks = [dict(r) for r in cursor.fetchall()]
                if tasks:
                    extra_context += "\nTAREFAS VENCIDAS:\n"
                    for t in tasks:
                        dv = t['data_vencimento'].strftime('%d/%m') if hasattr(t['data_vencimento'], 'strftime') else str(t['data_vencimento'])
                        extra_context += f"  - [{t['id']}] {t['titulo']} (venceu {dv}) - {t.get('projeto', 'sem projeto')}\n"

            # Project specific query
            if any(w in query.lower() for w in ["projeto", "project"]):
                cursor.execute("""
                    SELECT p.id, p.nome, p.status, p.tipo,
                        (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'pending') as tasks_pendentes
                    FROM projects p
                    WHERE p.status = 'ativo'
                    ORDER BY p.prioridade ASC
                    LIMIT 20
                """)
                projects = [dict(r) for r in cursor.fetchall()]
                if projects:
                    extra_context += "\nPROJETOS DETALHADOS:\n"
                    for p in projects:
                        extra_context += f"  - [{p['id']}] {p['nome']} ({p['tipo']}) - {p['tasks_pendentes']} tarefas pendentes\n"

            # Contact query
            if any(w in query.lower() for w in ["contato", "contact", "briefing", "quem"]):
                # Try to find a contact name in the query
                cursor.execute("""
                    SELECT id, nome, empresa, cargo, ultimo_contato
                    FROM contacts
                    WHERE nome ILIKE %s
                    ORDER BY ultimo_contato DESC NULLS LAST
                    LIMIT 5
                """, (f"%{query.split()[-1]}%",))
                contacts = [dict(r) for r in cursor.fetchall()]
                if contacts:
                    extra_context += "\nCONTATOS ENCONTRADOS:\n"
                    for c in contacts:
                        uc = c['ultimo_contato'].strftime('%d/%m/%Y') if c.get('ultimo_contato') and hasattr(c['ultimo_contato'], 'strftime') else "nunca"
                        extra_context += f"  - [{c['id']}] {c['nome']} - {c.get('cargo', '')} @ {c.get('empresa', '')} (ultimo contato: {uc})\n"

            # Upcoming events
            if any(w in query.lower() for w in ["agenda", "calendario", "reuniao", "reunioes", "evento", "semana"]):
                cursor.execute("""
                    SELECT e.summary, e.start_datetime, c.nome as contact_name
                    FROM calendar_events e
                    LEFT JOIN contacts c ON c.id = e.contact_id
                    WHERE e.start_datetime >= NOW()
                    AND e.start_datetime <= NOW() + INTERVAL '7 days'
                    ORDER BY e.start_datetime ASC
                    LIMIT 15
                """)
                events = [dict(r) for r in cursor.fetchall()]
                if events:
                    extra_context += "\nPROXIMOS EVENTOS (7 dias):\n"
                    for e in events:
                        dt = e['start_datetime'].strftime('%d/%m %H:%M') if hasattr(e['start_datetime'], 'strftime') else str(e['start_datetime'])
                        extra_context += f"  - {e['summary']} em {dt}"
                        if e.get('contact_name'):
                            extra_context += f" (com {e['contact_name']})"
                        extra_context += "\n"

    except Exception as e:
        logger.error(f"Error fetching extra context: {e}")

    today = datetime.now().strftime("%Y-%m-%d %A %H:%M")
    system = f"""Voce e o assistente pessoal de Renato Prado, CEO e consultor de governanca corporativa, cofundador do ImenSIAH.
Hoje: {today}. Responda de forma concisa e direta, como um briefing executivo.
Use os dados do CRM abaixo para responder.

{crm_context}
{extra_context}"""

    return await _call_claude(system, query, max_tokens=500)


async def _execute_send_message(params: Dict) -> str:
    """Send a WhatsApp message to a contact via the main rap-whatsapp instance."""
    from integrations.evolution_api import get_evolution_client

    destinatario = params.get("destinatario", "")
    mensagem = params.get("mensagem", "")

    if not destinatario or not mensagem:
        return "Preciso do nome do destinatario e da mensagem. Ex: 'Manda pro Rogerio: vamos marcar aquela call?'"

    try:
        # Find contact phone
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, telefones FROM contacts
                WHERE nome ILIKE %s
                LIMIT 3
            """, (f"%{destinatario}%",))
            contacts = [dict(r) for r in cursor.fetchall()]

        if not contacts:
            return f"Nao encontrei contato com nome '{destinatario}'. Tenta com o nome completo."

        if len(contacts) > 1:
            names = ", ".join(c["nome"] for c in contacts)
            return f"Encontrei varios contatos: {names}. Especifica melhor qual."

        contact = contacts[0]
        phones = contact.get("telefones")
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except (json.JSONDecodeError, TypeError):
                phones = []

        if not phones:
            return f"O contato {contact['nome']} nao tem telefone cadastrado."

        phone = phones[0] if isinstance(phones, list) else str(phones)
        phone_clean = ''.join(filter(str.isdigit, str(phone)))

        # Send via main instance (rap-whatsapp)
        client = get_evolution_client()
        result = await client.send_text(phone_clean, mensagem, instance_name="rap-whatsapp")

        if "error" not in result:
            return f"Mensagem enviada para *{contact['nome']}*: \"{mensagem[:80]}...\""
        else:
            return f"Erro ao enviar: {result.get('error', 'desconhecido')}"

    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return f"Erro ao enviar mensagem: {e}"


async def _execute_general(message: str, crm_context: str) -> str:
    """Handle general conversation with CRM context."""
    today = datetime.now().strftime("%Y-%m-%d %A %H:%M")
    system = f"""Voce e o INTEL Bot, assistente pessoal de Renato Prado no WhatsApp.
Renato e CEO, consultor de governanca corporativa e cofundador do ImenSIAH.
Hoje: {today}.

Responda de forma concisa, direta e util. Use o contexto do CRM quando relevante.
Se a pergunta parecer algo que voce poderia executar (criar tarefa, agendar, etc),
sugira o formato correto. Ex: "Quer que eu crie uma tarefa? Me manda: Tarefa: [descricao]"

{crm_context}"""

    return await _call_claude(system, message, max_tokens=500)


# ==================== MAIN HANDLER ====================

async def handle_bot_message(phone: str, message: str, message_id: str) -> str:
    """
    Main entry point for bot messages from intel-bot instance.

    Args:
        phone: Sender phone number
        message: Message text
        message_id: Unique message ID

    Returns:
        Response text to send back
    """
    # 1. Verify sender is Renato
    if not _is_renato(phone):
        logger.warning(f"Unauthorized bot message from {phone}")
        return "Este bot e de uso exclusivo. Acesso nao autorizado."

    # 2. Skip trivial messages
    if SKIP_PATTERNS.match(message.strip()):
        logger.debug(f"Skipping trivial message: {message}")
        return ""  # Empty = don't respond

    # 3. Get CRM context
    crm_context = _get_crm_context()

    # 4. Classify intent
    classification = await _classify_intent(message)
    intent = classification.get("intent", "general")
    params = classification.get("params", {})

    logger.info(f"Bot intent: {intent} (confidence: {classification.get('confidence', '?')})")

    # 5. Execute action
    try:
        if intent == "create_task":
            return await _execute_create_task(params)
        elif intent == "schedule_meeting":
            return await _execute_schedule_meeting(params)
        elif intent == "save_insight":
            return await _execute_save_insight(params)
        elif intent == "ask_question":
            return await _execute_ask_question(params, crm_context)
        elif intent == "send_message":
            return await _execute_send_message(params)
        else:
            return await _execute_general(message, crm_context)
    except Exception as e:
        logger.error(f"Error executing bot action {intent}: {e}")
        return f"Ops, deu um erro: {e}. Tenta de novo?"


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
