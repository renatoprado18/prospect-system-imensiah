#!/usr/bin/env python3
"""Verifica contas Gmail conectadas."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db

print("=" * 50, flush=True)
print("VERIFICACAO DE CONTAS GMAIL", flush=True)
print("=" * 50, flush=True)

with get_db() as conn:
    cursor = conn.cursor()

    # Check if table exists
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'google_accounts'
        )
    """)
    table_exists = cursor.fetchone()["exists"]

    if not table_exists:
        print("\nTabela google_accounts NAO existe!", flush=True)
        print("Precisa rodar init_db() primeiro.", flush=True)
    else:
        cursor.execute("SELECT * FROM google_accounts")
        accounts = cursor.fetchall()

        if accounts:
            print(f"\nContas encontradas: {len(accounts)}", flush=True)
            for acc in accounts:
                print(f"\n  Email: {acc['email']}", flush=True)
                print(f"  Tipo: {acc['tipo']}", flush=True)
                print(f"  Conectado: {acc['conectado']}", flush=True)
                print(f"  Ultima sync: {acc['ultima_sync']}", flush=True)
                has_token = "Sim" if acc.get('refresh_token') else "Nao"
                print(f"  Tem refresh_token: {has_token}", flush=True)
        else:
            print("\nNenhuma conta Gmail conectada!", flush=True)
            print("\nPara conectar, acesse:", flush=True)
            print("  https://intel.almeida-prado.com/api/gmail/auth?type=professional", flush=True)
            print("  ou", flush=True)
            print("  https://intel.almeida-prado.com/api/gmail/auth?type=personal", flush=True)

    # Check contacts with emails
    cursor.execute("""
        SELECT COUNT(*) as total FROM contacts
        WHERE emails IS NOT NULL AND emails::text != '[]'
    """)
    with_emails = cursor.fetchone()["total"]
    print(f"\nContatos com email: {with_emails}", flush=True)

    # Check contacts with interactions
    cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE total_interacoes > 0")
    with_interactions = cursor.fetchone()["total"]
    print(f"Contatos com interacoes: {with_interactions}", flush=True)
