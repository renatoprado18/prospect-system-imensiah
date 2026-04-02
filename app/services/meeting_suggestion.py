"""
Meeting Suggestion Service
Analisa mensagens WhatsApp/Email e sugere criacao de eventos no calendario

Funcionalidades:
- Detecta discussoes sobre reunioes/encontros nas mensagens
- Extrai data, hora, local e proposito usando IA
- Busca endereco da empresa (banco, website, Google Maps)
- Retorna sugestao estruturada para criacao de evento

Autor: INTEL
Data: 2026-04-02
"""
import os
import re
import json
import httpx
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from zoneinfo import ZoneInfo

from database import get_db

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")


async def analyze_messages_for_meeting(
    messages: List[Dict],
    contact_name: str,
    contact_empresa: str = None
) -> Optional[Dict[str, Any]]:
    """
    Analisa mensagens e detecta se ha uma reuniao sendo discutida.

    Args:
        messages: Lista de mensagens (com conteudo, direcao, enviado_em)
        contact_name: Nome do contato
        contact_empresa: Empresa do contato (opcional)

    Returns:
        Sugestao de evento ou None se nao houver reuniao detectada
    """
    if not messages or not ANTHROPIC_API_KEY:
        return None

    # Formatar mensagens para analise
    messages_text = "\n".join([
        f"[{m.get('direcao', 'incoming').upper()}] {m.get('enviado_em', '')}: {m.get('conteudo', '')}"
        for m in messages
    ])

    # Data atual para referencia
    sp_tz = ZoneInfo("America/Sao_Paulo")
    now = datetime.now(sp_tz)
    today_str = now.strftime("%Y-%m-%d")
    weekday_names = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]
    current_weekday = weekday_names[now.weekday()]

    prompt = f"""Analise as mensagens abaixo entre o usuario (OUTGOING) e {contact_name} (INCOMING).

MENSAGENS:
{messages_text}

DATA ATUAL: {today_str} ({current_weekday}-feira)
EMPRESA DO CONTATO: {contact_empresa or 'Nao informada'}

TAREFA: Identifique se ha uma reuniao/encontro sendo combinado ou mencionado nas mensagens.

Se houver uma reuniao sendo discutida, extraia:
1. Data e horario (se mencionados ou inferidos)
2. Local (se mencionado - pode ser escritorio, restaurante, empresa, etc)
3. Proposito/assunto da reuniao
4. Se e presencial ou virtual

IMPORTANTE:
- Se a data for relativa (amanha, proxima semana, etc), calcule a data real baseado em {today_str}
- Se nao houver reuniao sendo discutida, retorne null
- Considere termos como "almocar", "cafe", "encontrar", "reunir", "passar na", "visitar" como indicadores de reuniao

Responda APENAS com JSON valido (sem markdown):
{{
    "detected": true/false,
    "meeting": {{
        "title": "Titulo sugerido para o evento",
        "date": "YYYY-MM-DD",
        "time": "HH:MM",
        "duration_minutes": 60,
        "location": "Local mencionado ou null",
        "location_type": "office|restaurant|virtual|other",
        "purpose": "Descricao breve do proposito",
        "is_confirmed": true/false,
        "confidence": "high|medium|low",
        "notes": "Observacoes adicionais"
    }}
}}

Se nao houver reuniao:
{{"detected": false, "reason": "motivo"}}
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
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

            if response.status_code != 200:
                logger.error(f"Erro na API Claude: {response.status_code}")
                return None

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "")

            # Parse JSON response
            try:
                data = json.loads(content)
                if not data.get("detected"):
                    return None
                return data.get("meeting")
            except json.JSONDecodeError:
                logger.error(f"Erro ao parsear resposta: {content[:200]}")
                return None

    except Exception as e:
        logger.error(f"Erro ao analisar mensagens: {e}")
        return None


async def find_company_address(
    company_name: str,
    contact_id: int = None,
    company_website: str = None
) -> Optional[Dict[str, str]]:
    """
    Busca endereco da empresa em multiplas fontes.

    Args:
        company_name: Nome da empresa
        contact_id: ID do contato (para buscar no banco)
        company_website: Website da empresa (opcional)

    Returns:
        Dict com endereco ou None
    """
    address_info = None

    # 1. Buscar no banco de dados (empresa_dados do contato)
    if contact_id:
        address_info = _find_address_in_database(contact_id, company_name)
        if address_info:
            return address_info

    # 2. Buscar no website da empresa
    if company_website:
        address_info = await _find_address_on_website(company_website, company_name)
        if address_info:
            return address_info

    # 3. Buscar no Google Maps/Places API
    if GOOGLE_MAPS_API_KEY and company_name:
        address_info = await _find_address_google_maps(company_name)
        if address_info:
            return address_info

    # 4. Fallback: busca web generica
    if company_name:
        address_info = await _search_company_address_web(company_name)
        if address_info:
            return address_info

    return None


def _find_address_in_database(contact_id: int, company_name: str) -> Optional[Dict[str, str]]:
    """Busca endereco nos dados do contato no banco."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar empresa_dados do contato
            cursor.execute("""
                SELECT empresa, empresa_dados, company_website
                FROM contacts
                WHERE id = %s
            """, (contact_id,))
            row = cursor.fetchone()

            if not row:
                return None

            empresa_dados = row.get("empresa_dados") or {}
            if isinstance(empresa_dados, str):
                try:
                    empresa_dados = json.loads(empresa_dados)
                except:
                    empresa_dados = {}

            # Verificar se tem endereco nos dados da empresa
            address = empresa_dados.get("address") or empresa_dados.get("endereco")
            if address:
                return {
                    "address": address,
                    "source": "database",
                    "company": company_name or row.get("empresa")
                }

            # Verificar em localizacao
            location = empresa_dados.get("location") or empresa_dados.get("localizacao")
            if location:
                return {
                    "address": location,
                    "source": "database",
                    "company": company_name or row.get("empresa")
                }

            return None

    except Exception as e:
        logger.error(f"Erro ao buscar endereco no banco: {e}")
        return None


async def _find_address_on_website(website_url: str, company_name: str) -> Optional[Dict[str, str]]:
    """Busca endereco no website da empresa usando IA."""
    if not ANTHROPIC_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Tentar buscar pagina de contato
            contact_urls = [
                f"{website_url}/contato",
                f"{website_url}/contact",
                f"{website_url}/fale-conosco",
                f"{website_url}/sobre",
                f"{website_url}/about"
            ]

            page_content = None
            for url in contact_urls:
                try:
                    resp = await client.get(url, follow_redirects=True, timeout=10.0)
                    if resp.status_code == 200:
                        page_content = resp.text[:10000]  # Limitar tamanho
                        break
                except:
                    continue

            # Se nao encontrou pagina de contato, tentar home
            if not page_content:
                try:
                    resp = await client.get(website_url, follow_redirects=True, timeout=10.0)
                    if resp.status_code == 200:
                        page_content = resp.text[:10000]
                except:
                    return None

            if not page_content:
                return None

            # Usar Claude para extrair endereco
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 300,
                    "messages": [{
                        "role": "user",
                        "content": f"""Extraia o endereco fisico da empresa {company_name} do HTML abaixo.

HTML:
{page_content}

Responda APENAS com JSON:
{{"found": true, "address": "Endereco completo"}}
ou
{{"found": false}}
"""
                    }]
                }
            )

            if response.status_code == 200:
                result = response.json()
                content = result.get("content", [{}])[0].get("text", "")
                try:
                    data = json.loads(content)
                    if data.get("found") and data.get("address"):
                        return {
                            "address": data["address"],
                            "source": "website",
                            "company": company_name
                        }
                except:
                    pass

    except Exception as e:
        logger.error(f"Erro ao buscar endereco no website: {e}")

    return None


async def _find_address_google_maps(company_name: str) -> Optional[Dict[str, str]]:
    """Busca endereco no Google Places API."""
    if not GOOGLE_MAPS_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Text Search para encontrar a empresa
            response = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={
                    "query": f"{company_name} Sao Paulo Brasil",
                    "key": GOOGLE_MAPS_API_KEY,
                    "language": "pt-BR"
                }
            )

            if response.status_code != 200:
                return None

            data = response.json()
            results = data.get("results", [])

            if results:
                place = results[0]
                return {
                    "address": place.get("formatted_address", ""),
                    "source": "google_maps",
                    "company": place.get("name", company_name),
                    "place_id": place.get("place_id"),
                    "lat": place.get("geometry", {}).get("location", {}).get("lat"),
                    "lng": place.get("geometry", {}).get("location", {}).get("lng")
                }

    except Exception as e:
        logger.error(f"Erro ao buscar no Google Maps: {e}")

    return None


async def _search_company_address_web(company_name: str) -> Optional[Dict[str, str]]:
    """Busca generica de endereco na web usando IA."""
    if not ANTHROPIC_API_KEY:
        return None

    # Para evitar muitas chamadas, retornar None por enquanto
    # Essa funcao poderia usar web search + AI para encontrar o endereco
    return None


async def generate_event_suggestion(
    contact_id: int,
    messages: List[Dict] = None,
    limit: int = 10
) -> Optional[Dict[str, Any]]:
    """
    Gera sugestao completa de evento para um contato.

    Args:
        contact_id: ID do contato
        messages: Mensagens (opcional, busca do banco se nao fornecido)
        limit: Numero de mensagens a analisar

    Returns:
        Sugestao de evento completa com endereco
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar dados do contato
            cursor.execute("""
                SELECT id, nome, empresa, company_website, empresa_dados, emails
                FROM contacts
                WHERE id = %s
            """, (contact_id,))
            contact = cursor.fetchone()

            if not contact:
                return None

            contact_name = contact.get("nome", "Contato")
            contact_empresa = contact.get("empresa")
            company_website = contact.get("company_website")

            # Buscar mensagens se nao fornecidas
            if not messages:
                cursor.execute("""
                    SELECT m.conteudo, m.direcao, m.enviado_em, c.canal
                    FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.contact_id = %s
                    ORDER BY m.enviado_em DESC
                    LIMIT %s
                """, (contact_id, limit))
                messages = [dict(row) for row in cursor.fetchall()]
                # Reverter para ordem cronologica
                messages = list(reversed(messages))

            if not messages:
                return None

            # Analisar mensagens para detectar reuniao
            meeting = await analyze_messages_for_meeting(
                messages, contact_name, contact_empresa
            )

            if not meeting:
                return None

            # Buscar endereco se a reuniao for presencial e local for empresa
            address_info = None
            location = meeting.get("location")
            location_type = meeting.get("location_type")

            if location_type == "office" or (location and contact_empresa and contact_empresa.lower() in location.lower()):
                # Buscar endereco da empresa
                address_info = await find_company_address(
                    company_name=contact_empresa,
                    contact_id=contact_id,
                    company_website=company_website
                )

            # Extrair email do contato para convidar
            emails = contact.get("emails") or []
            if isinstance(emails, str):
                try:
                    emails = json.loads(emails)
                except:
                    emails = []

            contact_email = None
            if emails:
                if isinstance(emails[0], dict):
                    contact_email = emails[0].get("email") or emails[0].get("address")
                else:
                    contact_email = emails[0]

            # Montar sugestao completa
            suggestion = {
                "contact_id": contact_id,
                "contact_name": contact_name,
                "contact_email": contact_email,
                "contact_empresa": contact_empresa,
                "meeting": meeting,
                "address_info": address_info,
                "location_final": address_info.get("address") if address_info else meeting.get("location"),
                "created_at": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat()
            }

            return suggestion

    except Exception as e:
        logger.error(f"Erro ao gerar sugestao de evento: {e}")
        return None


# Singleton
_meeting_suggestion_service = None

def get_meeting_suggestion_service():
    """Retorna instancia do servico."""
    return None  # Servico e stateless, usar funcoes diretamente
