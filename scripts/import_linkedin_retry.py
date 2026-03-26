#!/usr/bin/env python3
"""
Reprocess LinkedIn import for failed batches (2500-4000)
"""
import csv
import sys
import os
from difflib import SequenceMatcher
from dotenv import load_dotenv

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables
load_dotenv()

from app.database import get_db

CSV_PATH = "/Users/rap/Downloads/Basic_LinkedInDataExport_03-25-2026.zip/Connections.csv"
START_INDEX = 2500
END_INDEX = 4000

def parse_linkedin_csv(file_path):
    """Parse LinkedIn connections CSV."""
    connections = []
    with open(file_path, 'r', encoding='utf-8') as f:
        # Skip the first 3 lines (Notes header + explanation + empty line)
        for _ in range(3):
            next(f)

        reader = csv.DictReader(f)
        for row in reader:
            first_name = row.get('First Name', '').strip()
            last_name = row.get('Last Name', '').strip()
            nome = f"{first_name} {last_name}".strip()

            connections.append({
                'nome': nome,
                'email': row.get('Email Address', '').strip().lower() if row.get('Email Address') else None,
                'empresa': row.get('Company', '').strip() if row.get('Company') else None,
                'cargo': row.get('Position', '').strip() if row.get('Position') else None,
                'linkedin_url': row.get('URL', '').strip() if row.get('URL') else None,
            })
    return connections

def find_contact_by_email(cursor, email):
    """Find contact by email."""
    if not email:
        return None
    cursor.execute("""
        SELECT id, nome, linkedin, empresa, cargo, tags
        FROM contacts
        WHERE emails::text ILIKE %s
    """, (f'%{email}%',))
    return cursor.fetchone()

def find_contact_by_name(cursor, nome):
    """Find contact by fuzzy name match."""
    if not nome or len(nome) < 3:
        return None

    # Exact match first
    cursor.execute("""
        SELECT id, nome, linkedin, empresa, cargo, tags
        FROM contacts
        WHERE LOWER(nome) = LOWER(%s)
    """, (nome,))
    result = cursor.fetchone()
    if result:
        return result

    # Fuzzy match
    parts = nome.lower().split()
    if len(parts) >= 2:
        cursor.execute("""
            SELECT id, nome, linkedin, empresa, cargo, tags
            FROM contacts
            WHERE LOWER(nome) LIKE %s AND LOWER(nome) LIKE %s
            LIMIT 10
        """, (f'%{parts[0]}%', f'%{parts[-1]}%'))
        candidates = cursor.fetchall()

        best_match = None
        best_ratio = 0.7
        for candidate in candidates:
            ratio = SequenceMatcher(None, nome.lower(), candidate['nome'].lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = candidate
        return best_match

    return None

def update_contact_linkedin(cursor, contact_id, linkedin_data, existing):
    """Update contact with LinkedIn data."""
    updates = []
    params = []

    if linkedin_data.get('linkedin_url') and not existing.get('linkedin'):
        updates.append("linkedin = %s")
        params.append(linkedin_data['linkedin_url'])

    if linkedin_data.get('empresa') and not existing.get('empresa'):
        updates.append("empresa = %s")
        params.append(linkedin_data['empresa'])

    if linkedin_data.get('cargo') and not existing.get('cargo'):
        updates.append("cargo = %s")
        params.append(linkedin_data['cargo'])

    # Add linkedin tag
    import json
    current_tags = existing.get('tags') or []
    if 'linkedin' not in current_tags:
        new_tags = current_tags + ['linkedin']
        updates.append("tags = %s::jsonb")
        params.append(json.dumps(new_tags))

    if updates:
        params.append(contact_id)
        cursor.execute(f"""
            UPDATE contacts
            SET {', '.join(updates)}, atualizado_em = NOW()
            WHERE id = %s
        """, params)
        return True
    return False

def main():
    print("=" * 60)
    print("REPROCESSANDO BATCHES FALHADOS (2500-4000)")
    print("=" * 60)
    print()

    print("1. Lendo arquivo CSV...")
    sys.stdout.flush()
    connections = parse_linkedin_csv(CSV_PATH)
    print(f"   Conexoes no range: {END_INDEX - START_INDEX}")
    sys.stdout.flush()

    # Get only the failed range
    batch = connections[START_INDEX:END_INDEX]

    stats = {
        'total': len(batch),
        'email_match': 0,
        'name_match': 0,
        'updated': 0,
        'not_found': 0,
    }

    print()
    print("2. Processando conexoes...")
    sys.stdout.flush()

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            for i, linkedin in enumerate(batch):
                contact = None
                match_type = None

                # Try email match first
                if linkedin.get('email'):
                    contact = find_contact_by_email(cursor, linkedin['email'])
                    if contact:
                        match_type = 'email'
                        stats['email_match'] += 1

                # Try name match
                if not contact and linkedin.get('nome'):
                    contact = find_contact_by_name(cursor, linkedin['nome'])
                    if contact:
                        match_type = 'name'
                        stats['name_match'] += 1

                # Update if found
                if contact:
                    if update_contact_linkedin(cursor, contact['id'], linkedin, contact):
                        stats['updated'] += 1
                else:
                    stats['not_found'] += 1

                if (i + 1) % 100 == 0:
                    print(f"   Processados: {i + 1}/{len(batch)}")
                    sys.stdout.flush()

            conn.commit()
            print(f"   Batch {START_INDEX}-{END_INDEX} concluido")
            sys.stdout.flush()

    except Exception as e:
        print(f"   Erro: {e}")
        sys.stdout.flush()

    print()
    print("=" * 60)
    print("RESULTADO RETRY")
    print("=" * 60)
    print(f"Total processados: {stats['total']}")
    print(f"Match por email: {stats['email_match']}")
    print(f"Match por nome: {stats['name_match']}")
    print(f"Total matches: {stats['email_match'] + stats['name_match']}")
    print(f"Contatos atualizados: {stats['updated']}")
    print(f"Nao encontrados: {stats['not_found']}")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
