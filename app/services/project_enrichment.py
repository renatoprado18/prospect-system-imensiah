"""
Project Enrichment Service

Uses AI to analyze project descriptions and gather context from:
- Database contacts
- Gmail messages
- WhatsApp messages
- Web search for companies

Generates suggestions for:
- Project name, type, priority
- Related contacts
- Milestones and tasks
"""
import os
import json
import httpx
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

from database import get_connection
from integrations.gmail import GmailIntegration

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


async def extract_entities_with_ai(descricao: str) -> Dict[str, Any]:
    """
    Use Claude to extract entities from the project description.

    Returns:
        {
            "pessoas": ["Emma Sakamoto", "..."],
            "empresas": ["Fictor", "..."],
            "temas": ["recuperação judicial", "..."],
            "tipo_inferido": "pessoal" | "negocio" | "patrimonio" | "conselho",
            "urgencia": "alta" | "media" | "baixa"
        }
    """
    prompt = f"""Analise esta descrição de projeto e extraia as entidades principais.

DESCRIÇÃO:
{descricao}

Extraia e retorne APENAS JSON válido:
{{
    "pessoas": ["lista de nomes de pessoas mencionadas"],
    "empresas": ["lista de empresas/organizações mencionadas"],
    "temas": ["lista de temas/assuntos principais"],
    "tipo_inferido": "pessoal|negocio|patrimonio|conselho",
    "urgencia": "alta|media|baixa"
}}

Regras para tipo_inferido:
- "pessoal": assuntos familiares, relacionamentos, saúde pessoal
- "negocio": oportunidades comerciais, parcerias, clientes
- "patrimonio": investimentos, imóveis, finanças, recuperação de valores
- "conselho": participação em conselhos, governança corporativa
"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

            if response.status_code != 200:
                return {"pessoas": [], "empresas": [], "temas": [], "tipo_inferido": "pessoal", "urgencia": "media"}

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "")

            # Clean up JSON
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            return json.loads(content.strip())

    except Exception as e:
        print(f"Error extracting entities: {e}")
        return {"pessoas": [], "empresas": [], "temas": [], "tipo_inferido": "pessoal", "urgencia": "media"}


def search_related_contacts(pessoas: List[str]) -> List[Dict]:
    """
    Search for contacts in the database that match the mentioned people.
    Uses fuzzy matching on name.
    """
    if not pessoas:
        return []

    contacts = []

    with get_connection() as conn:
        with conn.cursor() as cursor:
            for pessoa in pessoas:
                # Search by name similarity
                search_terms = pessoa.lower().split()
                if not search_terms:
                    continue

                # Build search condition
                conditions = []
                params = []
                for term in search_terms:
                    conditions.append("LOWER(nome) LIKE %s")
                    params.append(f"%{term}%")

                query = f"""
                    SELECT id, nome, empresa, cargo, circulo, foto_url
                    FROM contacts
                    WHERE {' AND '.join(conditions)}
                    LIMIT 5
                """

                cursor.execute(query, params)
                for row in cursor.fetchall():
                    contact = dict(row)
                    if not any(c['id'] == contact['id'] for c in contacts):
                        contacts.append(contact)

    return contacts


async def search_relevant_emails(entities: Dict, contacts: List[Dict]) -> List[Dict]:
    """
    Search Gmail for emails related to the project entities.
    Returns summarized email content.
    """
    emails = []

    with get_connection() as conn:
        with conn.cursor() as cursor:
            # Get connected Gmail accounts
            cursor.execute("""
                SELECT id, email, access_token, refresh_token, token_expiry
                FROM google_accounts
                WHERE conectado = true
                LIMIT 1
            """)
            account = cursor.fetchone()

            if not account:
                return []

            account = dict(account)

    gmail = GmailIntegration()
    access_token = account['access_token']

    # Check if token needs refresh
    if account['token_expiry'] and datetime.now() > account['token_expiry']:
        token_response = await gmail.refresh_access_token(account['refresh_token'])
        if 'access_token' in token_response:
            access_token = token_response['access_token']
            # Update token in database
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE google_accounts
                        SET access_token = %s, token_expiry = %s
                        WHERE id = %s
                    """, (access_token, datetime.now() + timedelta(hours=1), account['id']))
                    conn.commit()

    # Build search queries
    queries = []

    # Search by company names
    for empresa in entities.get('empresas', []):
        queries.append(f"subject:{empresa}")
        queries.append(f"{empresa}")

    # Search by contact emails
    for contact in contacts[:3]:  # Limit to first 3 contacts
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT emails FROM contacts WHERE id = %s", (contact['id'],))
                row = cursor.fetchone()
                if row and row['emails']:
                    email_list = row['emails'] if isinstance(row['emails'], list) else []
                    for email_obj in email_list[:2]:
                        email = email_obj.get('email') if isinstance(email_obj, dict) else email_obj
                        if email:
                            queries.append(f"from:{email} OR to:{email}")

    # Execute searches
    seen_ids = set()
    for query in queries[:5]:  # Limit to 5 queries
        try:
            result = await gmail.list_messages(access_token, query=query, max_results=5)

            if 'error' in result:
                continue

            for msg_meta in result.get('messages', [])[:3]:
                msg_id = msg_meta['id']
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)

                # Get full message
                full_msg = await gmail.get_message(access_token, msg_id, format='full')
                if 'error' in full_msg:
                    continue

                # Parse headers
                headers = {h['name']: h['value'] for h in full_msg.get('payload', {}).get('headers', [])}

                # Get body preview
                snippet = full_msg.get('snippet', '')

                emails.append({
                    'id': msg_id,
                    'from': headers.get('From', ''),
                    'to': headers.get('To', ''),
                    'subject': headers.get('Subject', ''),
                    'date': headers.get('Date', ''),
                    'snippet': snippet[:300]
                })

                if len(emails) >= 10:
                    break

        except Exception as e:
            print(f"Error searching emails: {e}")
            continue

        if len(emails) >= 10:
            break

    return emails


def search_relevant_whatsapp(entities: Dict, contacts: List[Dict]) -> List[Dict]:
    """
    Search WhatsApp messages related to the project.
    """
    messages = []

    if not contacts:
        return []

    contact_ids = [c['id'] for c in contacts]

    with get_connection() as conn:
        with conn.cursor() as cursor:
            # Search messages from related contacts
            cursor.execute("""
                SELECT m.direcao, m.conteudo, m.enviado_em, c.nome as contact_nome
                FROM messages m
                JOIN contacts c ON c.id = m.contact_id
                JOIN conversations conv ON conv.id = m.conversation_id
                WHERE m.contact_id = ANY(%s)
                AND conv.canal = 'whatsapp'
                AND m.conteudo IS NOT NULL
                AND m.conteudo != ''
                ORDER BY m.enviado_em DESC
                LIMIT 30
            """, (contact_ids,))

            for row in cursor.fetchall():
                msg = dict(row)
                # Filter by themes if possible
                content = msg.get('conteudo', '').lower()
                temas = entities.get('temas', [])
                empresas = entities.get('empresas', [])

                # Check if message is relevant
                relevant = not temas and not empresas  # Include all if no filters
                for tema in temas:
                    if tema.lower() in content:
                        relevant = True
                        break
                for empresa in empresas:
                    if empresa.lower() in content:
                        relevant = True
                        break

                if relevant:
                    messages.append({
                        'direcao': msg['direcao'],
                        'conteudo': msg['conteudo'][:200],
                        'data': str(msg['enviado_em']) if msg['enviado_em'] else '',
                        'contato': msg['contact_nome']
                    })

                    if len(messages) >= 20:
                        break

    return messages


async def search_company_web_info(empresas: List[str]) -> Dict[str, Any]:
    """
    Search web for information about mentioned companies.
    """
    if not empresas:
        return {}

    web_info = {}

    for empresa in empresas[:2]:  # Limit to 2 companies
        try:
            # Try to find company website
            search_url = f"https://www.google.com/search?q={empresa}+site+oficial"

            async with httpx.AsyncClient(timeout=15.0) as client:
                # Search for company info using Claude's knowledge
                prompt = f"""O que você sabe sobre a empresa "{empresa}"?

Forneça informações concisas sobre:
1. O que a empresa faz
2. Setor de atuação
3. Situação atual (se houver notícias relevantes como recuperação judicial, fusão, etc)
4. Informações úteis para alguém que precisa lidar com essa empresa

Responda em 2-3 parágrafos, de forma objetiva."""

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
                        "messages": [{"role": "user", "content": prompt}]
                    }
                )

                if response.status_code == 200:
                    result = response.json()
                    content = result.get("content", [{}])[0].get("text", "")
                    web_info[empresa] = content

        except Exception as e:
            print(f"Error searching web for {empresa}: {e}")
            continue

    return web_info


async def generate_project_suggestions(
    descricao: str,
    entities: Dict,
    contacts: List[Dict],
    emails: List[Dict],
    whatsapp: List[Dict],
    web_info: Dict
) -> Dict[str, Any]:
    """
    Use Claude to generate project suggestions based on all gathered context.
    """

    # Format contacts
    contacts_text = ""
    if contacts:
        contacts_text = "Contatos encontrados no CRM:\n"
        for c in contacts:
            contacts_text += f"- {c['nome']} (ID: {c['id']}, Empresa: {c.get('empresa', 'N/A')}, Círculo: {c.get('circulo', 'N/A')})\n"
    else:
        contacts_text = "Nenhum contato relacionado encontrado no CRM."

    # Format emails
    emails_text = ""
    if emails:
        emails_text = "Emails relevantes encontrados:\n"
        for e in emails[:5]:
            emails_text += f"- De: {e['from'][:50]} | Assunto: {e['subject'][:50]} | Preview: {e['snippet'][:100]}...\n"
    else:
        emails_text = "Nenhum email relevante encontrado."

    # Format WhatsApp
    whatsapp_text = ""
    if whatsapp:
        whatsapp_text = "Mensagens WhatsApp relevantes:\n"
        for m in whatsapp[:10]:
            direction = "→" if m['direcao'] == 'outgoing' else "←"
            whatsapp_text += f"- {direction} {m['contato']}: {m['conteudo'][:100]}...\n"
    else:
        whatsapp_text = "Nenhuma mensagem WhatsApp relevante encontrada."

    # Format web info
    web_text = ""
    if web_info:
        web_text = "Informações públicas sobre empresas:\n"
        for empresa, info in web_info.items():
            web_text += f"\n{empresa}:\n{info[:500]}\n"
    else:
        web_text = "Nenhuma informação pública adicional."

    prompt = f"""Você é um assistente de gestão de projetos pessoais e profissionais.

CONTEXTO:
O usuário Renato está criando um novo projeto no seu CRM pessoal (INTEL).
Ele forneceu a seguinte descrição:

"{descricao}"

ENTIDADES IDENTIFICADAS:
- Pessoas: {', '.join(entities.get('pessoas', [])) or 'Nenhuma'}
- Empresas: {', '.join(entities.get('empresas', [])) or 'Nenhuma'}
- Temas: {', '.join(entities.get('temas', [])) or 'Nenhum'}
- Tipo inferido: {entities.get('tipo_inferido', 'pessoal')}
- Urgência: {entities.get('urgencia', 'media')}

INFORMAÇÕES ENCONTRADAS:

{contacts_text}

{emails_text}

{whatsapp_text}

{web_text}

TAREFA:
Analise todas as informações e sugira um preenchimento completo para o projeto.
Seja ESPECÍFICO e ACIONÁVEL nas sugestões de marcos e tarefas.

Responda APENAS com JSON válido:
{{
    "nome_sugerido": "Nome conciso e descritivo do projeto",
    "tipo_sugerido": "negocio|pessoal|patrimonio|conselho",
    "prioridade_sugerida": 1-7 (1=urgente, 5=normal, 7=baixa),
    "descricao_expandida": "Descrição completa com contexto adicional identificado",
    "empresa_relacionada": "Nome da empresa principal ou null",
    "valor_estimado": null ou valor numérico em reais,
    "contatos_sugeridos": [
        {{"contact_id": ID, "nome": "Nome", "papel": "Responsável|Participante|Beneficiário|Consultor"}}
    ],
    "marcos_sugeridos": [
        {{"titulo": "Título do marco", "descricao": "Descrição", "data_prevista": "YYYY-MM-DD ou null"}}
    ],
    "tarefas_sugeridas": [
        {{"titulo": "Título da tarefa", "descricao": "Descrição", "prioridade": 1-7}}
    ],
    "insights": "Observações relevantes e recomendações estratégicas"
}}

IMPORTANTE:
- Sugira marcos realistas baseados no tipo de projeto
- Para projetos jurídicos, inclua marcos como "Protocolo de documentos", "Audiência", etc.
- Para investimentos, inclua marcos como "Análise de recuperação", "Contato com administrador", etc.
- As tarefas devem ser AÇÕES CONCRETAS que Renato pode executar
- Use as informações encontradas para personalizar as sugestões
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
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

            if response.status_code != 200:
                return {
                    "status": "error",
                    "error": f"API error: {response.status_code}"
                }

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "")

            # Clean up JSON
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            suggestions = json.loads(content.strip())
            suggestions["status"] = "success"
            return suggestions

    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "error": f"Failed to parse AI response: {str(e)}"
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"API call failed: {str(e)}"
        }


async def enrich_project_from_description(descricao: str) -> Dict[str, Any]:
    """
    Main function to enrich a project from its description.

    Args:
        descricao: Free-form project description from user

    Returns:
        {
            "status": "success" | "error",
            "nome_sugerido": "...",
            "tipo_sugerido": "...",
            "prioridade_sugerida": 5,
            "descricao_expandida": "...",
            "empresa_relacionada": "...",
            "valor_estimado": null,
            "contatos_sugeridos": [...],
            "marcos_sugeridos": [...],
            "tarefas_sugeridas": [...],
            "insights": "...",
            "contexto_encontrado": {
                "emails_count": 10,
                "whatsapp_count": 5,
                "contacts_count": 2,
                "empresas_pesquisadas": [...]
            }
        }
    """
    if not descricao or len(descricao.strip()) < 10:
        return {
            "status": "error",
            "error": "Descrição muito curta. Forneça mais detalhes sobre o projeto."
        }

    # Step 1: Extract entities
    entities = await extract_entities_with_ai(descricao)

    # Step 2: Search related contacts
    contacts = search_related_contacts(entities.get('pessoas', []))

    # Step 3: Search relevant emails
    emails = await search_relevant_emails(entities, contacts)

    # Step 4: Search WhatsApp messages
    whatsapp = search_relevant_whatsapp(entities, contacts)

    # Step 5: Search web for company info
    web_info = await search_company_web_info(entities.get('empresas', []))

    # Step 6: Generate suggestions
    suggestions = await generate_project_suggestions(
        descricao, entities, contacts, emails, whatsapp, web_info
    )

    # Add context info
    suggestions["contexto_encontrado"] = {
        "emails_count": len(emails),
        "whatsapp_count": len(whatsapp),
        "contacts_count": len(contacts),
        "empresas_pesquisadas": list(web_info.keys())
    }

    return suggestions
