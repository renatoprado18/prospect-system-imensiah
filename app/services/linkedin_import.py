"""
LinkedIn Connections Importer

Importa conexões do LinkedIn a partir do CSV exportado.
Faz match com contatos existentes e atualiza/cria conforme necessário.

Formato do CSV do LinkedIn (Connections.csv):
- First Name
- Last Name
- Email Address (pode estar vazio)
- Company
- Position
- Connected On
"""

import csv
import io
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from difflib import SequenceMatcher
import unicodedata
import re


def normalize_name(name: str) -> str:
    """Normaliza nome para comparação."""
    if not name:
        return ""

    # Remove acentos
    name = unicodedata.normalize('NFD', name)
    name = ''.join(c for c in name if unicodedata.category(c) != 'Mn')

    # Lowercase e remove espaços extras
    name = name.lower().strip()
    name = re.sub(r'\s+', ' ', name)

    return name


def name_similarity(name1: str, name2: str) -> float:
    """Calcula similaridade entre dois nomes (0-1)."""
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)

    if not n1 or not n2:
        return 0.0

    # Match exato
    if n1 == n2:
        return 1.0

    # Um contém o outro
    if n1 in n2 or n2 in n1:
        return 0.9

    # Similaridade de sequência
    return SequenceMatcher(None, n1, n2).ratio()


def parse_linkedin_date(date_str: str) -> Optional[datetime]:
    """Parseia data do LinkedIn (formato: '01 Jan 2024')."""
    if not date_str:
        return None

    try:
        # Formato inglês: "01 Jan 2024"
        return datetime.strptime(date_str.strip(), "%d %b %Y")
    except ValueError:
        pass

    try:
        # Formato alternativo: "2024-01-01"
        return datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except ValueError:
        pass

    return None


def parse_linkedin_csv(csv_content: str) -> List[Dict]:
    """
    Parseia CSV de conexões do LinkedIn.

    Returns:
        Lista de dicts com dados das conexões
    """
    connections = []

    # Detectar encoding e parsear
    reader = csv.DictReader(io.StringIO(csv_content))

    for row in reader:
        # Campos podem variar dependendo do idioma do LinkedIn
        first_name = row.get('First Name', row.get('Nome', '')).strip()
        last_name = row.get('Last Name', row.get('Sobrenome', '')).strip()
        email = row.get('Email Address', row.get('Email', row.get('Endereço de email', ''))).strip()
        company = row.get('Company', row.get('Empresa', '')).strip()
        position = row.get('Position', row.get('Cargo', row.get('Título', ''))).strip()
        connected_on = row.get('Connected On', row.get('Conectado em', '')).strip()
        url = row.get('URL', row.get('Profile URL', '')).strip()

        if not first_name and not last_name:
            continue

        full_name = f"{first_name} {last_name}".strip()

        connections.append({
            'first_name': first_name,
            'last_name': last_name,
            'full_name': full_name,
            'email': email if email and '@' in email else None,
            'company': company,
            'position': position,
            'connected_on': parse_linkedin_date(connected_on),
            'linkedin_url': url if url else None,
            'normalized_name': normalize_name(full_name)
        })

    return connections


def find_matching_contact(
    linkedin_connection: Dict,
    existing_contacts: List[Dict],
    threshold: float = 0.85
) -> Tuple[Optional[Dict], float, str]:
    """
    Encontra contato existente que corresponde à conexão do LinkedIn.

    Returns:
        Tuple (contato_match, score, match_type)
        match_type: 'email', 'name_exact', 'name_similar', 'none'
    """
    conn_email = linkedin_connection.get('email')
    conn_name = linkedin_connection.get('full_name', '')
    conn_normalized = linkedin_connection.get('normalized_name', '')

    best_match = None
    best_score = 0.0
    match_type = 'none'

    for contact in existing_contacts:
        # 1. Match por email (mais confiável)
        if conn_email:
            contact_emails = contact.get('emails', [])
            if isinstance(contact_emails, list):
                for email_obj in contact_emails:
                    email = email_obj.get('email', '') if isinstance(email_obj, dict) else email_obj
                    if email and email.lower() == conn_email.lower():
                        return (contact, 1.0, 'email')

        # 2. Match por nome
        contact_name = contact.get('nome', '')
        if contact_name and conn_name:
            score = name_similarity(conn_name, contact_name)

            if score > best_score:
                best_score = score
                best_match = contact

                if score == 1.0:
                    match_type = 'name_exact'
                elif score >= threshold:
                    match_type = 'name_similar'

    if best_score >= threshold:
        return (best_match, best_score, match_type)

    return (None, best_score, 'none')


async def analyze_linkedin_import(
    csv_content: str,
    existing_contacts: List[Dict]
) -> Dict:
    """
    Analisa CSV do LinkedIn e retorna preview do que será importado.

    Returns:
        Dict com estatísticas e preview das ações
    """
    connections = parse_linkedin_csv(csv_content)

    results = {
        'total_connections': len(connections),
        'matches': [],      # Contatos que serão atualizados
        'new_contacts': [], # Conexões que virarão novos contatos
        'skipped': [],      # Conexões sem dados suficientes
        'stats': {
            'email_matches': 0,
            'name_exact_matches': 0,
            'name_similar_matches': 0,
            'new': 0,
            'skipped': 0
        }
    }

    for conn in connections:
        match, score, match_type = find_matching_contact(conn, existing_contacts)

        if match:
            results['matches'].append({
                'linkedin': conn,
                'contact': {
                    'id': match.get('id'),
                    'nome': match.get('nome'),
                    'empresa': match.get('empresa'),
                    'cargo': match.get('cargo'),
                    'linkedin': match.get('linkedin')
                },
                'score': score,
                'match_type': match_type,
                'updates': get_updates_needed(conn, match)
            })

            if match_type == 'email':
                results['stats']['email_matches'] += 1
            elif match_type == 'name_exact':
                results['stats']['name_exact_matches'] += 1
            else:
                results['stats']['name_similar_matches'] += 1

        elif conn['full_name'] and (conn['company'] or conn['email']):
            # Tem dados suficientes para criar novo contato
            results['new_contacts'].append(conn)
            results['stats']['new'] += 1

        else:
            # Sem dados suficientes
            results['skipped'].append(conn)
            results['stats']['skipped'] += 1

    return results


def get_updates_needed(linkedin_conn: Dict, contact: Dict) -> List[Dict]:
    """Determina quais campos precisam ser atualizados."""
    updates = []

    # LinkedIn URL
    if linkedin_conn.get('linkedin_url') and not contact.get('linkedin'):
        updates.append({
            'field': 'linkedin',
            'old': None,
            'new': linkedin_conn['linkedin_url']
        })

    # Empresa (atualiza se LinkedIn tem e contato não tem)
    if linkedin_conn.get('company') and not contact.get('empresa'):
        updates.append({
            'field': 'empresa',
            'old': contact.get('empresa'),
            'new': linkedin_conn['company']
        })

    # Cargo (atualiza se LinkedIn tem e contato não tem)
    if linkedin_conn.get('position') and not contact.get('cargo'):
        updates.append({
            'field': 'cargo',
            'old': contact.get('cargo'),
            'new': linkedin_conn['position']
        })

    return updates


async def execute_linkedin_import(
    csv_content: str,
    existing_contacts: List[Dict],
    db_connection,
    update_existing: bool = True,
    create_new: bool = True,
    propagate_to_google: bool = False
) -> Dict:
    """
    Executa a importação do LinkedIn.

    Args:
        csv_content: Conteúdo do CSV
        existing_contacts: Lista de contatos existentes
        db_connection: Conexão com o banco
        update_existing: Se deve atualizar contatos existentes
        create_new: Se deve criar novos contatos
        propagate_to_google: Se deve sincronizar com Google Contacts

    Returns:
        Dict com resultados da importação
    """
    analysis = await analyze_linkedin_import(csv_content, existing_contacts)

    results = {
        'updated': 0,
        'created': 0,
        'errors': [],
        'details': []
    }

    # 1. Atualizar contatos existentes
    if update_existing:
        for match in analysis['matches']:
            if not match['updates']:
                continue

            try:
                contact_id = match['contact']['id']
                updates = {}

                for update in match['updates']:
                    field = update['field']
                    value = update['new']
                    updates[field] = value

                if updates:
                    # Atualizar no banco
                    set_clause = ", ".join([f"{k} = ${i+2}" for i, k in enumerate(updates.keys())])
                    values = [contact_id] + list(updates.values())

                    await db_connection.execute(
                        f"UPDATE contacts SET {set_clause} WHERE id = $1",
                        *values
                    )

                    results['updated'] += 1
                    results['details'].append({
                        'action': 'updated',
                        'contact_id': contact_id,
                        'name': match['contact']['nome'],
                        'updates': updates
                    })

            except Exception as e:
                results['errors'].append({
                    'contact': match['contact']['nome'],
                    'error': str(e)
                })

    # 2. Criar novos contatos
    if create_new:
        for conn in analysis['new_contacts']:
            try:
                # Preparar dados para inserção
                emails_json = []
                if conn.get('email'):
                    emails_json = [{'type': 'linkedin', 'email': conn['email'], 'primary': True}]

                result = await db_connection.fetchrow("""
                    INSERT INTO contacts (
                        nome, empresa, cargo, linkedin,
                        emails, origem, contexto, criado_em
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                    RETURNING id
                """,
                    conn['full_name'],
                    conn.get('company'),
                    conn.get('position'),
                    conn.get('linkedin_url'),
                    emails_json,
                    'linkedin',
                    'professional'
                )

                results['created'] += 1
                results['details'].append({
                    'action': 'created',
                    'contact_id': result['id'],
                    'name': conn['full_name']
                })

            except Exception as e:
                results['errors'].append({
                    'contact': conn['full_name'],
                    'error': str(e)
                })

    return results


# Função auxiliar para construir URL do LinkedIn a partir do nome
def build_linkedin_search_url(name: str) -> str:
    """Gera URL de busca no LinkedIn para um nome."""
    encoded_name = name.replace(' ', '%20')
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded_name}"
