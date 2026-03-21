"""
Contact Enrichment Service

Uses AI to analyze and enrich contact data from multiple sources:
- WhatsApp messages
- Gmail messages
- LinkedIn profile
- Existing facts

Generates:
- AI-powered summary/bio
- Key facts extraction
- Relationship insights
- Follow-up suggestions
"""
import os
import json
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


async def get_contact_context(contact_id: int, db_connection) -> Dict[str, Any]:
    """
    Gather all available context for a contact from multiple sources.

    Returns:
        {
            "contact": {...},
            "whatsapp_messages": [...],
            "email_messages": [...],
            "existing_facts": [...],
            "linkedin_data": {...}
        }
    """
    cursor = db_connection.cursor()

    # Get contact info
    cursor.execute("""
        SELECT id, nome, apelido, empresa, cargo, emails, telefones,
               linkedin, linkedin_headline, contexto, resumo_ai,
               ultimo_contato, total_interacoes
        FROM contacts WHERE id = %s
    """, (contact_id,))
    contact = cursor.fetchone()

    if not contact:
        return {"error": "Contact not found"}

    contact = dict(contact)

    # Get WhatsApp messages (last 50)
    cursor.execute("""
        SELECT direcao, conteudo, enviado_em, metadata
        FROM messages
        WHERE contact_id = %s
        AND conversation_id IN (SELECT id FROM conversations WHERE canal = 'whatsapp')
        ORDER BY enviado_em DESC
        LIMIT 50
    """, (contact_id,))
    whatsapp_messages = [dict(row) for row in cursor.fetchall()]

    # Get Email messages (last 30)
    cursor.execute("""
        SELECT direcao, conteudo, metadata, enviado_em
        FROM messages
        WHERE contact_id = %s
        AND conversation_id IN (SELECT id FROM conversations WHERE canal = 'email')
        ORDER BY enviado_em DESC
        LIMIT 30
    """, (contact_id,))
    email_messages = [dict(row) for row in cursor.fetchall()]

    # Get existing facts
    cursor.execute("""
        SELECT categoria, fato, fonte, confianca
        FROM contact_facts
        WHERE contact_id = %s
        ORDER BY criado_em DESC
    """, (contact_id,))
    existing_facts = [dict(row) for row in cursor.fetchall()]

    return {
        "contact": contact,
        "whatsapp_messages": whatsapp_messages,
        "email_messages": email_messages,
        "existing_facts": existing_facts
    }


def format_messages_for_ai(messages: List[Dict], contact_name: str, source: str) -> str:
    """Format messages for AI prompt."""
    if not messages:
        return f"Nenhuma mensagem de {source} disponivel."

    formatted = []
    for msg in messages[:30]:  # Limit to 30 most recent
        direction = "Eu" if msg.get("direcao") == "outgoing" else contact_name
        date = ""
        if msg.get("enviado_em"):
            if isinstance(msg["enviado_em"], str):
                date = msg["enviado_em"][:10]
            else:
                date = msg["enviado_em"].strftime("%d/%m/%Y")

        content = msg.get("conteudo", "")[:500]  # Limit content length
        if content:
            formatted.append(f"[{date}] {direction}: {content}")

    return "\n".join(formatted) if formatted else f"Nenhuma mensagem de {source} com conteudo."


async def enrich_contact_with_ai(contact_id: int, db_connection) -> Dict[str, Any]:
    """
    Main enrichment function. Analyzes contact data and generates insights.

    Returns:
        {
            "resumo": "AI-generated summary",
            "fatos": [...],
            "insights": {...},
            "sugestoes": [...],
            "status": "success" | "error"
        }
    """
    if not ANTHROPIC_API_KEY:
        return {"status": "error", "error": "ANTHROPIC_API_KEY not configured"}

    # Gather context
    context = await get_contact_context(contact_id, db_connection)

    if "error" in context:
        return {"status": "error", "error": context["error"]}

    contact = context["contact"]
    contact_name = contact.get("nome", "Contato")

    # Format data for prompt
    whatsapp_text = format_messages_for_ai(
        context["whatsapp_messages"],
        contact_name,
        "WhatsApp"
    )

    email_text = format_messages_for_ai(
        context["email_messages"],
        contact_name,
        "Email"
    )

    existing_facts_text = ""
    if context["existing_facts"]:
        existing_facts_text = "\n".join([
            f"- [{f['categoria']}] {f['fato']}"
            for f in context["existing_facts"]
        ])
    else:
        existing_facts_text = "Nenhum fato registrado anteriormente."

    # Build prompt
    prompt = f"""Analise os dados deste contato e gere um enriquecimento completo.

## DADOS DO CONTATO

Nome: {contact_name}
Apelido: {contact.get('apelido') or 'N/A'}
Empresa: {contact.get('empresa') or 'N/A'}
Cargo: {contact.get('cargo') or 'N/A'}
LinkedIn: {contact.get('linkedin') or 'N/A'}
LinkedIn Headline: {contact.get('linkedin_headline') or 'N/A'}
Contexto: {contact.get('contexto') or 'N/A'}
Total de interacoes: {contact.get('total_interacoes') or 0}
Ultimo contato: {contact.get('ultimo_contato') or 'N/A'}

## FATOS JA CONHECIDOS

{existing_facts_text}

## MENSAGENS WHATSAPP (mais recentes)

{whatsapp_text}

## MENSAGENS EMAIL (mais recentes)

{email_text}

## TAREFA

Com base nos dados acima, gere:

1. **RESUMO** (2-3 paragrafos): Um perfil completo da pessoa, incluindo:
   - Quem ela e profissionalmente
   - Natureza do relacionamento comigo (Renato)
   - Historico de interacoes relevantes
   - Contexto pessoal que seja evidente nas conversas

2. **NOVOS FATOS** (lista): Fatos importantes extraidos das conversas que ainda nao estao nos "fatos conhecidos". Cada fato deve ter:
   - categoria: "professional", "personal", "preference", "relationship"
   - fato: descricao clara e objetiva
   - confianca: 0.5 a 1.0 (baseado na certeza)

3. **INSIGHTS** (objeto JSON):
   - forca_relacionamento: "forte", "medio", "fraco" (baseado na frequencia e tom)
   - sentimento_geral: "positivo", "neutro", "negativo"
   - topicos_frequentes: lista de assuntos mais discutidos
   - ultima_interacao_relevante: resumo breve

4. **SUGESTOES** (lista): Acoes sugeridas para fortalecer o relacionamento:
   - Tipo: "follow_up", "aniversario", "oportunidade", "reconexao"
   - Descricao da acao
   - Prioridade: "alta", "media", "baixa"

Responda APENAS com JSON valido no formato:
{{
    "resumo": "...",
    "fatos": [
        {{"categoria": "...", "fato": "...", "confianca": 0.8}}
    ],
    "insights": {{
        "forca_relacionamento": "...",
        "sentimento_geral": "...",
        "topicos_frequentes": [...],
        "ultima_interacao_relevante": "..."
    }},
    "sugestoes": [
        {{"tipo": "...", "descricao": "...", "prioridade": "..."}}
    ]
}}
"""

    # Call Claude API
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
                    "max_tokens": 2000,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ]
                }
            )

            if response.status_code != 200:
                return {
                    "status": "error",
                    "error": f"API error: {response.status_code} - {response.text}"
                }

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "")

            # Parse JSON response
            try:
                # Clean up response if needed
                content = content.strip()
                if content.startswith("```json"):
                    content = content[7:]
                if content.startswith("```"):
                    content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

                enrichment = json.loads(content)
                enrichment["status"] = "success"
                return enrichment

            except json.JSONDecodeError as e:
                return {
                    "status": "error",
                    "error": f"Failed to parse AI response: {str(e)}",
                    "raw_response": content[:500]
                }

    except Exception as e:
        return {
            "status": "error",
            "error": f"API call failed: {str(e)}"
        }


async def save_enrichment_results(
    contact_id: int,
    enrichment: Dict[str, Any],
    db_connection
) -> Dict[str, Any]:
    """
    Save enrichment results to database.
    Updates contact resumo_ai and inserts new facts.
    """
    if enrichment.get("status") != "success":
        return {"saved": False, "error": enrichment.get("error")}

    cursor = db_connection.cursor()
    stats = {"resumo_updated": False, "facts_added": 0, "insights_saved": False}

    try:
        # Update contact resumo_ai
        if enrichment.get("resumo"):
            cursor.execute("""
                UPDATE contacts
                SET resumo_ai = %s,
                    insights_ai = %s,
                    ultimo_enriquecimento = CURRENT_TIMESTAMP,
                    enriquecimento_status = 'complete'
                WHERE id = %s
            """, (
                enrichment["resumo"],
                json.dumps(enrichment.get("insights", {})),
                contact_id
            ))
            stats["resumo_updated"] = True
            stats["insights_saved"] = True

        # Insert new facts
        for fato in enrichment.get("fatos", []):
            cursor.execute("""
                INSERT INTO contact_facts (contact_id, categoria, fato, fonte, confianca)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                contact_id,
                fato.get("categoria", "professional"),
                fato.get("fato", ""),
                "ai_enrichment",
                fato.get("confianca", 0.8)
            ))
            stats["facts_added"] += 1

        db_connection.commit()
        stats["saved"] = True

    except Exception as e:
        db_connection.rollback()
        return {"saved": False, "error": str(e)}

    return stats


async def enrich_and_save(contact_id: int, db_connection) -> Dict[str, Any]:
    """
    Full enrichment pipeline: analyze and save results.
    """
    # Run enrichment
    enrichment = await enrich_contact_with_ai(contact_id, db_connection)

    if enrichment.get("status") != "success":
        return enrichment

    # Save results
    save_result = await save_enrichment_results(contact_id, enrichment, db_connection)

    return {
        "status": "success",
        "enrichment": enrichment,
        "save_stats": save_result
    }
