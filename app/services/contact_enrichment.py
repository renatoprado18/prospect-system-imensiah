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

    # Build prompt with business context
    prompt = f"""Voce e um assistente de relacionamento profissional para Renato Almeida Prado.

## SOBRE RENATO (para contexto)

Renato e fundador da **ImensIAH**, uma plataforma de Governanca Estrategica que ajuda empresas com:
- Gestao de conselhos (administrativo, consultivo, fiscal)
- Governanca corporativa
- Planejamento estrategico com IA

Renato tambem atua como:
- Conselheiro em diversas empresas
- Mentor de startups e scale-ups
- Investidor anjo
- Advisor estrategico

**Objetivos de Renato**:
1. Identificar empresas que precisam de governanca/conselhos
2. Expandir rede de relacionamentos estrategicos
3. Encontrar oportunidades de negocio (consultoria, advisory, investimento)
4. Manter relacionamentos aquecidos com decisores

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

Analise PROFUNDAMENTE as conversas e gere insights ACIONAVEIS:

1. **RESUMO** (2-3 paragrafos): Perfil estrategico da pessoa:
   - Quem e profissionalmente (cargo, empresa, influencia)
   - Natureza do relacionamento com Renato
   - POTENCIAL para negocios/parcerias/conselhos
   - Sinais de oportunidade nas conversas

2. **NOVOS FATOS** (lista): Extraia informacoes valiosas:
   - categoria: "professional", "personal", "preference", "relationship", "opportunity"
   - fato: informacao especifica e util
   - confianca: 0.5 a 1.0

3. **INSIGHTS** (objeto JSON):
   - forca_relacionamento: "forte", "medio", "fraco"
   - sentimento_geral: "positivo", "neutro", "negativo"
   - topicos_frequentes: assuntos discutidos
   - ultima_interacao_relevante: resumo breve
   - potencial_negocio: "alto", "medio", "baixo", "nenhum" (para ImensIAH/consultoria)
   - perfil_decisor: true/false (e tomador de decisao na empresa?)
   - conexoes_estrategicas: lista de pessoas/empresas mencionadas que podem ser uteis

4. **OPORTUNIDADES** (lista): Identifique oportunidades CONCRETAS:
   - tipo: "conselho", "consultoria", "investimento", "parceria", "indicacao", "networking"
   - descricao: oportunidade especifica identificada
   - evidencia: onde voce viu isso na conversa
   - proximo_passo: acao concreta para Renato

5. **SUGESTOES** (lista): Acoes para AGORA:
   - tipo: "follow_up", "agendar_reuniao", "enviar_proposta", "reconexao", "aniversario", "apresentar_imensiah"
   - descricao: acao especifica e contextualizada
   - prioridade: "alta", "media", "baixa"
   - timing: "imediato", "esta_semana", "este_mes", "trimestre"

Responda APENAS com JSON valido:
{{
    "resumo": "...",
    "fatos": [
        {{"categoria": "...", "fato": "...", "confianca": 0.8}}
    ],
    "insights": {{
        "forca_relacionamento": "...",
        "sentimento_geral": "...",
        "topicos_frequentes": [...],
        "ultima_interacao_relevante": "...",
        "potencial_negocio": "...",
        "perfil_decisor": true/false,
        "conexoes_estrategicas": [...]
    }},
    "oportunidades": [
        {{"tipo": "...", "descricao": "...", "evidencia": "...", "proximo_passo": "..."}}
    ],
    "sugestoes": [
        {{"tipo": "...", "descricao": "...", "prioridade": "...", "timing": "..."}}
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
            # Combine insights with oportunidades and sugestoes for storage
            full_insights = enrichment.get("insights", {})
            full_insights["oportunidades"] = enrichment.get("oportunidades", [])
            full_insights["sugestoes"] = enrichment.get("sugestoes", [])

            cursor.execute("""
                UPDATE contacts
                SET resumo_ai = %s,
                    insights_ai = %s,
                    ultimo_enriquecimento = CURRENT_TIMESTAMP,
                    enriquecimento_status = 'complete'
                WHERE id = %s
            """, (
                enrichment["resumo"],
                json.dumps(full_insights),
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


# ============================================================
# MANUAL ENRICHMENT - Dados fornecidos pelo usuario
# ============================================================

async def update_manual_enrichment(
    contact_id: int,
    db_connection,
    relationship_context: str = None,
    linkedin_url: str = None,
    company_website: str = None,
    empresa: str = None,
    cargo: str = None,
    manual_notes: str = None
) -> Dict[str, Any]:
    """
    Atualiza dados manuais do contato fornecidos pelo usuario.
    Estes dados serao usados no enriquecimento com AI.
    """
    cursor = db_connection.cursor()

    updates = []
    values = []

    if relationship_context is not None:
        updates.append("relationship_context = %s")
        values.append(relationship_context)

    if linkedin_url is not None:
        updates.append("linkedin = %s")
        values.append(linkedin_url)

    if company_website is not None:
        updates.append("company_website = %s")
        values.append(company_website)

    if empresa is not None:
        updates.append("empresa = %s")
        values.append(empresa)

    if cargo is not None:
        updates.append("cargo = %s")
        values.append(cargo)

    if manual_notes is not None:
        updates.append("manual_notes = %s")
        values.append(manual_notes)

    if not updates:
        return {"status": "error", "error": "Nenhum dado para atualizar"}

    updates.append("atualizado_em = CURRENT_TIMESTAMP")
    values.append(contact_id)

    try:
        cursor.execute(f"""
            UPDATE contacts
            SET {', '.join(updates)}
            WHERE id = %s
        """, values)
        db_connection.commit()

        return {
            "status": "success",
            "updated_fields": [u.split(" = ")[0] for u in updates[:-1]],
            "contact_id": contact_id
        }
    except Exception as e:
        db_connection.rollback()
        return {"status": "error", "error": str(e)}


def lookup_company_from_email_domain(email: str) -> Dict[str, Any]:
    """
    Identifica se o email e corporativo e extrai o dominio.
    """
    if not email:
        return {"type": "unknown"}

    domain = email.split('@')[-1].lower()

    personal_domains = [
        'gmail.com', 'hotmail.com', 'outlook.com', 'yahoo.com',
        'icloud.com', 'live.com', 'msn.com', 'uol.com.br',
        'bol.com.br', 'terra.com.br', 'ig.com.br', 'globo.com',
        'yahoo.com.br', 'outlook.com.br'
    ]

    if domain in personal_domains:
        return {"type": "personal", "domain": domain}

    return {
        "type": "corporate",
        "domain": domain,
        "suggested_website": f"https://www.{domain}"
    }


async def enrich_with_context(
    contact_id: int,
    db_connection,
    relationship_context: str = None
) -> Dict[str, Any]:
    """
    Enriquece contato incluindo o contexto do relacionamento informado pelo usuario.

    Exemplo de relationship_context:
    "Participa comigo do Conselho Consultivo da Associacao Despertar.
     Conheci em 2023. Interesse em governanca corporativa."
    """
    cursor = db_connection.cursor()

    # Se foi fornecido contexto, salvar primeiro
    if relationship_context:
        cursor.execute("""
            UPDATE contacts
            SET relationship_context = %s, atualizado_em = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (relationship_context, contact_id))
        db_connection.commit()

    # Buscar dados atualizados do contato
    cursor.execute("""
        SELECT id, nome, apelido, empresa, cargo, emails, telefones,
               linkedin, linkedin_headline, contexto, resumo_ai,
               ultimo_contato, total_interacoes, relationship_context,
               company_website, manual_notes, circulo
        FROM contacts WHERE id = %s
    """, (contact_id,))
    contact = cursor.fetchone()

    if not contact:
        return {"status": "error", "error": "Contato nao encontrado"}

    contact = dict(contact)
    contact_name = contact.get("nome", "Contato")

    # Buscar contexto completo (mensagens, fatos, etc)
    context = await get_contact_context(contact_id, db_connection)

    whatsapp_text = format_messages_for_ai(
        context.get("whatsapp_messages", []),
        contact_name,
        "WhatsApp"
    )

    email_text = format_messages_for_ai(
        context.get("email_messages", []),
        contact_name,
        "Email"
    )

    existing_facts_text = ""
    if context.get("existing_facts"):
        existing_facts_text = "\n".join([
            f"- [{f['categoria']}] {f['fato']}"
            for f in context["existing_facts"]
        ])
    else:
        existing_facts_text = "Nenhum fato registrado anteriormente."

    # Verificar email corporativo
    emails = contact.get("emails", [])
    email_domain_info = ""
    if emails:
        first_email = emails[0].get('email', '') if isinstance(emails[0], dict) else emails[0]
        domain_check = lookup_company_from_email_domain(first_email)
        if domain_check.get("type") == "corporate":
            email_domain_info = f"Email corporativo detectado: {domain_check.get('domain')}"

    # PROMPT APRIMORADO COM CONTEXTO DO RELACIONAMENTO
    prompt = f"""Voce e um assistente de relacionamento profissional para Renato Almeida Prado.

## SOBRE RENATO (para contexto)

Renato e fundador da **ImensIAH**, uma plataforma de Governanca Estrategica que ajuda empresas com:
- Gestao de conselhos (administrativo, consultivo, fiscal)
- Governanca corporativa
- Planejamento estrategico com IA

Renato tambem atua como:
- Conselheiro em diversas empresas
- Mentor de startups e scale-ups
- Investidor anjo
- Advisor estrategico

## DADOS DO CONTATO

Nome: {contact_name}
Apelido: {contact.get('apelido') or 'N/A'}
Empresa: {contact.get('empresa') or 'N/A'}
Cargo: {contact.get('cargo') or 'N/A'}
LinkedIn: {contact.get('linkedin') or 'N/A'}
LinkedIn Headline: {contact.get('linkedin_headline') or 'N/A'}
Website Empresa: {contact.get('company_website') or 'N/A'}
Contexto: {contact.get('contexto') or 'N/A'}
Circulo: {contact.get('circulo', 5)} (1=Intimo, 2=Proximo, 3=Ativo, 4=Conhecido, 5=Arquivo)
Total de interacoes: {contact.get('total_interacoes') or 0}
Ultimo contato: {contact.get('ultimo_contato') or 'N/A'}
{email_domain_info}

## CONTEXTO DO RELACIONAMENTO (INFORMADO POR RENATO)

{contact.get('relationship_context') or 'Nenhum contexto adicional informado.'}

## NOTAS MANUAIS

{contact.get('manual_notes') or 'Nenhuma nota adicional.'}

## FATOS JA CONHECIDOS

{existing_facts_text}

## MENSAGENS WHATSAPP (mais recentes)

{whatsapp_text}

## MENSAGENS EMAIL (mais recentes)

{email_text}

## TAREFA

Analise TODAS as informacoes disponiveis e gere insights ACIONAVEIS.

**IMPORTANTE**: Use o CONTEXTO DO RELACIONAMENTO informado por Renato como base principal.
Se Renato disse que conhece a pessoa de algum lugar especifico, use isso!

1. **RESUMO** (2-3 paragrafos): Perfil estrategico da pessoa:
   - Quem e profissionalmente (cargo, empresa, influencia)
   - Natureza do relacionamento com Renato (use o contexto informado!)
   - POTENCIAL para negocios/parcerias/conselhos
   - Sinais de oportunidade nas conversas

2. **NOVOS FATOS** (lista): Extraia informacoes valiosas:
   - categoria: "professional", "personal", "preference", "relationship", "opportunity"
   - fato: informacao especifica e util
   - confianca: 0.5 a 1.0

3. **INSIGHTS** (objeto JSON):
   - forca_relacionamento: "forte", "medio", "fraco"
   - sentimento_geral: "positivo", "neutro", "negativo"
   - topicos_frequentes: assuntos discutidos
   - ultima_interacao_relevante: resumo breve
   - potencial_negocio: "alto", "medio", "baixo", "nenhum"
   - perfil_decisor: true/false
   - conexoes_estrategicas: pessoas/empresas mencionadas

4. **SUGESTOES** (lista): Acoes para AGORA:
   - tipo: "follow_up", "agendar_reuniao", "enviar_proposta", "reconexao", "apresentar_imensiah"
   - descricao: acao especifica e contextualizada
   - prioridade: "alta", "media", "baixa"
   - timing: "imediato", "esta_semana", "este_mes"

Responda APENAS com JSON valido:
{{
    "resumo": "...",
    "fatos": [...],
    "insights": {{...}},
    "sugestoes": [...]
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
                    "error": f"API error: {response.status_code}"
                }

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "")

            # Parse JSON response
            try:
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

                # Salvar resultados
                save_result = await save_enrichment_results(contact_id, enrichment, db_connection)

                return {
                    "status": "success",
                    "enrichment": enrichment,
                    "save_stats": save_result
                }

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


async def auto_enrich_priority_contacts(
    db_connection,
    circulo_max: int = 2,
    limit: int = 10
) -> Dict[str, Any]:
    """
    Enriquece automaticamente contatos prioritarios (circulos 1 e 2)
    que ainda nao tem resumo_ai ou tem resumo antigo (>30 dias).
    """
    cursor = db_connection.cursor()

    cursor.execute("""
        SELECT id, nome, empresa, circulo
        FROM contacts
        WHERE circulo <= %s
          AND (
              resumo_ai IS NULL
              OR resumo_ai = ''
              OR ultimo_enriquecimento < NOW() - INTERVAL '30 days'
          )
        ORDER BY circulo ASC, ultimo_contato DESC NULLS LAST
        LIMIT %s
    """, (circulo_max, limit))

    contacts = cursor.fetchall()

    results = []
    for contact in contacts:
        try:
            result = await enrich_with_context(contact['id'], db_connection)
            results.append({
                "id": contact['id'],
                "nome": contact['nome'],
                "circulo": contact['circulo'],
                "success": result.get('status') == 'success'
            })
        except Exception as e:
            results.append({
                "id": contact['id'],
                "nome": contact['nome'],
                "success": False,
                "error": str(e)
            })

    return {
        "enriched_count": len([r for r in results if r.get('success')]),
        "total_processed": len(results),
        "results": results
    }
