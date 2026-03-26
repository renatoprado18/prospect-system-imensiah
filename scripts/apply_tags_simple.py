#!/usr/bin/env python3
"""Aplica tags automaticas - versao simples e rapida."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db
import json
import re

# Regras simplificadas de tags
EMPRESA_TAGS = {
    "financeiro": ["banco", "bank", "itau", "bradesco", "santander", "btg", "xp", "nubank", "inter", "c6", "safra", "credit", "capital", "asset", "invest"],
    "tecnologia": ["tech", "software", "google", "microsoft", "amazon", "aws", "ibm", "oracle", "sap", "totvs", "startup"],
    "consultoria": ["consult", "mckinsey", "bain", "bcg", "deloitte", "pwc", "kpmg", "ey ", "ernst", "accenture"],
    "juridico": ["advogad", "law", "legal", "escritorio", "machado meyer", "mattos filho", "pinheiro neto"],
    "saude": ["hospital", "clinic", "medic", "farma", "laboratorio", "dasa", "fleury", "hapvida", "notredame"],
}

CARGO_TAGS = {
    "c-level": ["ceo", "cfo", "cto", "coo", "cio", "chief", "presidente", "diretor geral"],
    "diretor": ["diretor", "director", "vp ", "vice-presidente"],
    "gerente": ["gerente", "manager", "head ", "coordenador"],
    "conselheiro": ["conselheiro", "conselho", "board", "advisory"],
}

def normalize(text):
    if not text:
        return ""
    return re.sub(r'[^\w\s]', ' ', str(text).lower())

def detect_tags(empresa, cargo):
    tags = set()
    empresa_norm = normalize(empresa)
    cargo_norm = normalize(cargo)

    # Empresa tags
    for tag, keywords in EMPRESA_TAGS.items():
        if any(kw in empresa_norm for kw in keywords):
            tags.add(tag)

    # Cargo tags
    for tag, keywords in CARGO_TAGS.items():
        if any(kw in cargo_norm for kw in keywords):
            tags.add(tag)

    return list(tags)

print("=" * 50, flush=True)
print("TAGS AUTOMATICAS (SIMPLES)", flush=True)
print("=" * 50, flush=True)

BATCH_SIZE = 200
stats = {"processados": 0, "com_tags": 0, "total_tags": 0, "por_tag": {}}

# Fetch all contacts in one query
print("Buscando contatos...", flush=True)
with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, empresa, cargo, tags FROM contacts ORDER BY id")
    contacts = cursor.fetchall()

total = len(contacts)
print(f"Total: {total} contatos", flush=True)

# Process in batches
for i in range(0, len(contacts), BATCH_SIZE):
    batch = contacts[i:i+BATCH_SIZE]

    with get_db() as conn:
        cursor = conn.cursor()

        for contact in batch:
            contact_id = contact["id"]
            empresa = contact["empresa"] or ""
            cargo = contact["cargo"] or ""

            # Get existing tags
            existing_tags = []
            try:
                existing_tags = json.loads(contact["tags"] or "[]")
            except:
                pass
            existing_set = set(existing_tags)

            # Detect new tags
            detected = detect_tags(empresa, cargo)
            new_tags = [t for t in detected if t not in existing_set]

            if new_tags:
                # Merge tags
                all_tags = list(existing_set | set(new_tags))
                cursor.execute(
                    "UPDATE contacts SET tags = %s WHERE id = %s",
                    (json.dumps(all_tags), contact_id)
                )
                stats["com_tags"] += 1
                stats["total_tags"] += len(new_tags)

                for tag in new_tags:
                    stats["por_tag"][tag] = stats["por_tag"].get(tag, 0) + 1

            stats["processados"] += 1

        conn.commit()

    # Progress
    batch_num = i // BATCH_SIZE + 1
    if batch_num % 5 == 0 or (i + len(batch)) >= len(contacts):
        progresso = (i + len(batch)) / len(contacts) * 100
        print(f"Batch {batch_num}: {stats['processados']}/{total} ({progresso:.1f}%) - Com tags: {stats['com_tags']}", flush=True)

print("\n" + "=" * 50, flush=True)
print("RESULTADO", flush=True)
print("=" * 50, flush=True)
print(f"Total: {stats['processados']}", flush=True)
print(f"Com novas tags: {stats['com_tags']}", flush=True)
print(f"Tags aplicadas: {stats['total_tags']}", flush=True)

if stats["por_tag"]:
    print("\nPor tag:", flush=True)
    for tag, count in sorted(stats["por_tag"].items(), key=lambda x: x[1], reverse=True):
        print(f"  {tag}: {count}", flush=True)
