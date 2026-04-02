"""
Contact Intelligence Service

Provides AI-powered intelligence for contacts:
- Chat with AI about the contact (answer questions)
- Generate contextual insights
- Suggest responses

Uses all available data:
- WhatsApp/Email messages
- Facts, Projects, Tasks
- LinkedIn data
- Relationship history
"""
import os
import json
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
import logging

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


async def gather_contact_context(contact_id: int, db_connection) -> Dict[str, Any]:
    """
    Gather comprehensive context about a contact.

    Returns all relevant data for AI analysis.
    """
    cursor = db_connection.cursor()

    # Get contact info
    cursor.execute("""
        SELECT id, nome, apelido, empresa, cargo, emails, telefones,
               linkedin, linkedin_headline, contexto, resumo_ai,
               ultimo_contato, total_interacoes, health_score,
               circulo_pessoal, circulo_profissional, aniversario,
               insights_ai, relationship_context
        FROM contacts WHERE id = %s
    """, (contact_id,))
    contact = cursor.fetchone()

    if not contact:
        return {"error": "Contact not found"}

    contact = dict(contact)
    contact_name = contact.get("nome", "Contato")

    # Get recent messages (WhatsApp + Email)
    cursor.execute("""
        SELECT m.direcao, m.conteudo, m.enviado_em, c.canal
        FROM messages m
        JOIN conversations c ON m.conversation_id = c.id
        WHERE m.contact_id = %s
        AND m.conteudo IS NOT NULL
        ORDER BY m.enviado_em DESC
        LIMIT 50
    """, (contact_id,))
    messages = [dict(row) for row in cursor.fetchall()]

    # Get facts
    cursor.execute("""
        SELECT categoria, fato, fonte, criado_em
        FROM contact_facts
        WHERE contact_id = %s
        ORDER BY criado_em DESC
        LIMIT 30
    """, (contact_id,))
    facts = [dict(row) for row in cursor.fetchall()]

    # Get projects
    cursor.execute("""
        SELECT p.nome, p.tipo, p.descricao, p.status, pc.papel
        FROM projects p
        JOIN project_contacts pc ON p.id = pc.project_id
        WHERE pc.contact_id = %s AND p.status = 'active'
    """, (contact_id,))
    projects = [dict(row) for row in cursor.fetchall()]

    # Get tasks
    cursor.execute("""
        SELECT titulo, descricao, status, data_vencimento
        FROM tasks
        WHERE contact_id = %s
        ORDER BY created_at DESC
        LIMIT 10
    """, (contact_id,))
    tasks = [dict(row) for row in cursor.fetchall()]

    # Get interactions (timeline)
    cursor.execute("""
        SELECT tipo, titulo, descricao, data_interacao
        FROM contact_interactions
        WHERE contact_id = %s
        ORDER BY data_interacao DESC
        LIMIT 20
    """, (contact_id,))
    interactions = [dict(row) for row in cursor.fetchall()]

    return {
        "contact": contact,
        "messages": messages,
        "facts": facts,
        "projects": projects,
        "tasks": tasks,
        "interactions": interactions
    }


def format_context_for_prompt(context: Dict[str, Any]) -> str:
    """Format all context data into a readable prompt section."""
    contact = context["contact"]

    # Basic info
    info_parts = []
    info_parts.append(f"Nome: {contact.get('nome', 'N/A')}")
    if contact.get('apelido'):
        info_parts.append(f"Apelido: {contact['apelido']}")
    if contact.get('empresa'):
        info_parts.append(f"Empresa: {contact['empresa']}")
    if contact.get('cargo'):
        info_parts.append(f"Cargo: {contact['cargo']}")
    if contact.get('linkedin_headline'):
        info_parts.append(f"LinkedIn: {contact['linkedin_headline']}")

    # Circles
    if contact.get('circulo_pessoal'):
        circulos = {1: 'Núcleo', 2: 'Próximo', 3: 'Relacionamento', 4: 'Ocasional', 5: 'Distante'}
        info_parts.append(f"Círculo Pessoal: P{contact['circulo_pessoal']} ({circulos.get(contact['circulo_pessoal'], '')})")
    if contact.get('circulo_profissional'):
        circulos_prof = {1: 'Core', 2: 'Estratégico', 3: 'Networking', 4: 'Ocasional', 5: 'Arquivo'}
        info_parts.append(f"Círculo Profissional: R{contact['circulo_profissional']} ({circulos_prof.get(contact['circulo_profissional'], '')})")

    if contact.get('health_score'):
        info_parts.append(f"Health Score: {contact['health_score']}%")
    if contact.get('total_interacoes'):
        info_parts.append(f"Total de interações: {contact['total_interacoes']}")
    if contact.get('aniversario'):
        info_parts.append(f"Aniversário: {contact['aniversario']}")

    # AI Summary if available
    resumo_section = ""
    if contact.get('resumo_ai'):
        resumo_section = f"\n\n## RESUMO DO RELACIONAMENTO (gerado por IA)\n\n{contact['resumo_ai']}"

    # Format messages
    messages_text = ""
    if context["messages"]:
        msg_lines = []
        for msg in context["messages"][:30]:
            direction = "Renato" if msg.get("direcao") == "outgoing" else contact.get('nome', 'Contato')
            date = ""
            if msg.get("enviado_em"):
                if isinstance(msg["enviado_em"], str):
                    date = msg["enviado_em"][:10]
                else:
                    date = msg["enviado_em"].strftime("%d/%m/%Y")
            content = (msg.get("conteudo") or "")[:300]
            canal = msg.get("canal", "")
            if content:
                msg_lines.append(f"[{date}] [{canal}] {direction}: {content}")
        messages_text = "\n".join(msg_lines)
    else:
        messages_text = "Nenhuma mensagem registrada."

    # Format facts
    facts_text = ""
    if context["facts"]:
        facts_text = "\n".join([f"- [{f['categoria']}] {f['fato']}" for f in context["facts"]])
    else:
        facts_text = "Nenhum fato registrado."

    # Format projects
    projects_text = ""
    if context["projects"]:
        projects_text = "\n".join([
            f"- {p['nome']} ({p['tipo']}) - Papel: {p.get('papel', 'Participante')}"
            for p in context["projects"]
        ])
    else:
        projects_text = "Nenhum projeto em comum."

    # Format tasks
    tasks_text = ""
    if context["tasks"]:
        tasks_text = "\n".join([
            f"- [{t['status']}] {t['titulo']}" + (f" (vence: {t['data_vencimento']})" if t.get('data_vencimento') else "")
            for t in context["tasks"]
        ])
    else:
        tasks_text = "Nenhuma tarefa."

    # Format interactions
    interactions_text = ""
    if context["interactions"]:
        interactions_text = "\n".join([
            f"- [{i.get('data_interacao', 'N/A')}] {i['tipo']}: {i.get('titulo', '')} {i.get('descricao', '')[:100]}"
            for i in context["interactions"][:10]
        ])
    else:
        interactions_text = "Nenhuma interação registrada."

    return f"""## INFORMAÇÕES DO CONTATO

{chr(10).join(info_parts)}
{resumo_section}

## FATOS CONHECIDOS

{facts_text}

## PROJETOS EM COMUM

{projects_text}

## TAREFAS RELACIONADAS

{tasks_text}

## HISTÓRICO DE INTERAÇÕES

{interactions_text}

## MENSAGENS RECENTES (WhatsApp/Email)

{messages_text}
"""


async def chat_about_contact(
    contact_id: int,
    question: str,
    db_connection
) -> Dict[str, Any]:
    """
    Answer questions about a contact using AI.

    Args:
        contact_id: ID of the contact
        question: User's question
        db_connection: Database connection

    Returns:
        {
            "answer": "AI response",
            "sources": ["where the info came from"],
            "suggestions": ["follow-up actions"],
            "status": "success" | "error"
        }
    """
    if not ANTHROPIC_API_KEY:
        return {"status": "error", "error": "ANTHROPIC_API_KEY not configured"}

    # Gather context
    context = await gather_contact_context(contact_id, db_connection)

    if "error" in context:
        return {"status": "error", "error": context["error"]}

    contact_name = context["contact"].get("nome", "Contato")
    formatted_context = format_context_for_prompt(context)

    prompt = f"""Você é o assistente de relacionamento INTEL de Renato Almeida Prado.

Renato é fundador da ImensIAH (plataforma de governança estratégica) e atua como conselheiro, mentor e investidor.

## CONTEXTO DO CONTATO: {contact_name}

{formatted_context}

## PERGUNTA DO RENATO

"{question}"

## INSTRUÇÕES

1. Responda a pergunta de forma DIRETA e ÚTIL
2. Use APENAS informações do contexto fornecido - não invente dados
3. Se não houver informação suficiente, diga claramente
4. Seja conciso mas completo
5. Se identificar oportunidades ou ações sugeridas, mencione
6. Sempre que possível, cite a fonte (mensagem, fato, etc.)

Responda em JSON:
{{
    "answer": "Sua resposta aqui (pode ser longa e detalhada)",
    "confidence": "high" | "medium" | "low",
    "sources": ["lista de fontes usadas: mensagem, fato, projeto, etc."],
    "suggestions": ["ações sugeridas baseadas na pergunta, se houver"]
}}
"""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1500,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ]
                }
            )

            if response.status_code != 200:
                logger.error(f"Claude API error: {response.status_code} - {response.text}")
                return {"status": "error", "error": f"API error: {response.status_code}"}

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "")

            # Parse JSON response
            try:
                # Clean the response (remove markdown code blocks if present)
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content.rsplit("```", 1)[0]
                content = content.strip()

                parsed = json.loads(content)
                return {
                    "status": "success",
                    "answer": parsed.get("answer", content),
                    "confidence": parsed.get("confidence", "medium"),
                    "sources": parsed.get("sources", []),
                    "suggestions": parsed.get("suggestions", []),
                    "contact_name": contact_name
                }
            except json.JSONDecodeError:
                # If not valid JSON, return raw text
                return {
                    "status": "success",
                    "answer": content,
                    "confidence": "medium",
                    "sources": [],
                    "suggestions": [],
                    "contact_name": contact_name
                }

    except httpx.TimeoutException:
        return {"status": "error", "error": "Timeout ao chamar IA"}
    except Exception as e:
        logger.error(f"Error in chat_about_contact: {str(e)}")
        return {"status": "error", "error": str(e)}


async def suggest_response(
    contact_id: int,
    db_connection,
    context_type: str = "reply"
) -> Dict[str, Any]:
    """
    Suggest a response message for the contact.

    Args:
        contact_id: ID of the contact
        db_connection: Database connection
        context_type: "reply" (respond to last message) or "reconnect" (restart conversation)

    Returns:
        {
            "suggestion": "Suggested message text",
            "tone": "professional" | "friendly" | "casual",
            "status": "success" | "error"
        }
    """
    if not ANTHROPIC_API_KEY:
        return {"status": "error", "error": "ANTHROPIC_API_KEY not configured"}

    # Gather context
    context = await gather_contact_context(contact_id, db_connection)

    if "error" in context:
        return {"status": "error", "error": context["error"]}

    contact_name = context["contact"].get("nome", "Contato")
    first_name = contact_name.split()[0] if contact_name else "Contato"

    # Get last few messages for context
    recent_messages = []
    for msg in context["messages"][:10]:
        direction = "Renato" if msg.get("direcao") == "outgoing" else first_name
        content = (msg.get("conteudo") or "")[:200]
        if content:
            recent_messages.append(f"{direction}: {content}")

    messages_context = "\n".join(recent_messages) if recent_messages else "Nenhuma mensagem recente."

    # Get relationship info
    relationship_info = []
    if context["contact"].get("empresa"):
        relationship_info.append(f"Trabalha na {context['contact']['empresa']}")
    if context["contact"].get("cargo"):
        relationship_info.append(f"É {context['contact']['cargo']}")
    if context["contact"].get("circulo_pessoal") and context["contact"]["circulo_pessoal"] <= 2:
        relationship_info.append("Relacionamento pessoal próximo")
    if context["contact"].get("circulo_profissional") and context["contact"]["circulo_profissional"] <= 2:
        relationship_info.append("Relacionamento profissional estratégico")

    if context_type == "reply":
        instruction = "Sugira uma RESPOSTA para a última mensagem recebida. Continue a conversa de forma natural."
    else:
        instruction = "Sugira uma mensagem de RECONEXÃO para retomar o contato. Seja genuíno e interessado."

    prompt = f"""Você é assistente de comunicação de Renato Almeida Prado.

## CONTATO: {contact_name}

Contexto: {', '.join(relationship_info) if relationship_info else 'Contato profissional'}

## ÚLTIMAS MENSAGENS

{messages_context}

## TAREFA

{instruction}

A mensagem deve:
- Ser natural e pessoal (não robótica)
- Ter o tom apropriado para o relacionamento
- Ser em português brasileiro
- Ter 1-3 frases (para WhatsApp)

Responda em JSON:
{{
    "suggestion": "texto da mensagem sugerida",
    "tone": "professional" | "friendly" | "casual",
    "reasoning": "por que essa abordagem"
}}
"""

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 500,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ]
                }
            )

            if response.status_code != 200:
                return {"status": "error", "error": f"API error: {response.status_code}"}

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "")

            try:
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content.rsplit("```", 1)[0]

                parsed = json.loads(content.strip())
                return {
                    "status": "success",
                    "suggestion": parsed.get("suggestion", ""),
                    "tone": parsed.get("tone", "friendly"),
                    "reasoning": parsed.get("reasoning", ""),
                    "contact_name": contact_name
                }
            except json.JSONDecodeError:
                return {
                    "status": "success",
                    "suggestion": content,
                    "tone": "friendly",
                    "contact_name": contact_name
                }

    except Exception as e:
        logger.error(f"Error in suggest_response: {str(e)}")
        return {"status": "error", "error": str(e)}
