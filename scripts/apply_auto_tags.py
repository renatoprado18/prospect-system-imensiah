#!/usr/bin/env python3
"""Aplica tags automaticas em todos os contatos."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db
from services.auto_tags import analisar_contato_para_tags, aplicar_tags_contato
import json

BATCH_SIZE = 100

print("=" * 50, flush=True)
print("APLICACAO DE TAGS AUTOMATICAS", flush=True)
print("=" * 50, flush=True)

stats = {
    "processados": 0,
    "com_novas_tags": 0,
    "total_tags_aplicadas": 0,
    "tags_count": {}
}

# Get all contact IDs
print("Buscando contatos...", flush=True)
with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM contacts ORDER BY id")
    all_ids = [row["id"] for row in cursor.fetchall()]

total = len(all_ids)
print(f"Total: {total} contatos", flush=True)

# Process in batches
for batch_num, i in enumerate(range(0, len(all_ids), BATCH_SIZE)):
    batch_ids = all_ids[i:i+BATCH_SIZE]

    with get_db() as conn:
        cursor = conn.cursor()

        for contact_id in batch_ids:
            try:
                # Analyze contact for tags
                resultado = analisar_contato_para_tags(contact_id)
                tags_novas = resultado.get("tags_sugeridas", [])

                if tags_novas:
                    # Apply tags
                    aplicar_tags_contato(contact_id, tags_novas)
                    stats["com_novas_tags"] += 1
                    stats["total_tags_aplicadas"] += len(tags_novas)

                    # Count tags
                    for tag in tags_novas:
                        stats["tags_count"][tag] = stats["tags_count"].get(tag, 0) + 1

                stats["processados"] += 1

            except Exception as e:
                # Skip errors silently
                stats["processados"] += 1
                continue

        conn.commit()

    # Progress every 10 batches
    if batch_num % 10 == 0 or (i + len(batch_ids)) >= len(all_ids):
        progresso = (i + len(batch_ids)) / len(all_ids) * 100
        print(f"Batch {batch_num+1}: {stats['processados']}/{total} ({progresso:.1f}%) - Com tags: {stats['com_novas_tags']}", flush=True)

print("\n" + "=" * 50, flush=True)
print("RESULTADO", flush=True)
print("=" * 50, flush=True)
print(f"Total processados: {stats['processados']}", flush=True)
print(f"Com novas tags: {stats['com_novas_tags']}", flush=True)
print(f"Total tags aplicadas: {stats['total_tags_aplicadas']}", flush=True)

if stats["tags_count"]:
    print("\nTop tags aplicadas:", flush=True)
    sorted_tags = sorted(stats["tags_count"].items(), key=lambda x: x[1], reverse=True)[:15]
    for tag, count in sorted_tags:
        print(f"  {tag}: {count}", flush=True)
