#!/usr/bin/env python3
"""Verifica duplicados de contatos."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db
import json

print("=" * 50, flush=True)
print("VERIFICACAO DE DUPLICADOS", flush=True)
print("=" * 50, flush=True)

# Check email duplicates
print("\n1. Duplicados por EMAIL:", flush=True)
with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT emails::text, COUNT(*) as cnt, array_agg(id) as ids, array_agg(nome) as nomes
        FROM contacts
        WHERE emails IS NOT NULL AND emails::text != '[]' AND emails::text != '' AND emails::text != 'null'
        GROUP BY emails::text
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
        LIMIT 20
    """)
    email_dups = cursor.fetchall()

    if email_dups:
        total_email_dups = sum(row["cnt"] - 1 for row in email_dups)
        print(f"   Encontrados: {len(email_dups)} grupos ({total_email_dups} duplicados)")
        for row in email_dups[:10]:
            print(f"   - {row['cnt']}x: {row['nomes'][:3]}")
    else:
        print("   Nenhum duplicado por email")

# Check phone duplicates
print("\n2. Duplicados por TELEFONE:", flush=True)
with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT telefones::text, COUNT(*) as cnt, array_agg(id) as ids, array_agg(nome) as nomes
        FROM contacts
        WHERE telefones IS NOT NULL AND telefones::text != '[]' AND telefones::text != '' AND telefones::text != 'null'
        GROUP BY telefones::text
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
        LIMIT 20
    """)
    phone_dups = cursor.fetchall()

    if phone_dups:
        total_phone_dups = sum(row["cnt"] - 1 for row in phone_dups)
        print(f"   Encontrados: {len(phone_dups)} grupos ({total_phone_dups} duplicados)")
        for row in phone_dups[:10]:
            print(f"   - {row['cnt']}x: {row['nomes'][:3]}")
    else:
        print("   Nenhum duplicado por telefone")

# Check name duplicates (exact match)
print("\n3. Duplicados por NOME EXATO:", flush=True)
with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT LOWER(TRIM(nome)) as nome_norm, COUNT(*) as cnt, array_agg(id) as ids
        FROM contacts
        WHERE nome IS NOT NULL AND nome != ''
        GROUP BY LOWER(TRIM(nome))
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
        LIMIT 20
    """)
    name_dups = cursor.fetchall()

    if name_dups:
        total_name_dups = sum(row["cnt"] - 1 for row in name_dups)
        print(f"   Encontrados: {len(name_dups)} grupos ({total_name_dups} duplicados)")
        for row in name_dups[:10]:
            print(f"   - {row['cnt']}x: '{row['nome_norm']}'")
    else:
        print("   Nenhum duplicado por nome")

print("\n" + "=" * 50, flush=True)
print("RESUMO", flush=True)
print("=" * 50, flush=True)
print(f"Grupos duplicados por email: {len(email_dups) if email_dups else 0}")
print(f"Grupos duplicados por telefone: {len(phone_dups) if phone_dups else 0}")
print(f"Grupos duplicados por nome: {len(name_dups) if name_dups else 0}")
