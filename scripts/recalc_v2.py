#!/usr/bin/env python3
"""Recalcula circulos - versao otimizada."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db
from services.circulos import calcular_score_circulo, calcular_health_score

BATCH_SIZE = 50
stats = {"processados": 0, "mudaram": 0, "por_circulo": {1:0, 2:0, 3:0, 4:0, 5:0}}

print("=" * 50, flush=True)
print("RECALCULO DE CIRCULOS v2", flush=True)
print("=" * 50, flush=True)

# Step 1: Get all IDs first (quick query)
print("Buscando IDs...", flush=True)
with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id FROM contacts
        WHERE circulo_manual IS NOT TRUE OR circulo_manual IS NULL
        ORDER BY id
    """)
    all_ids = [row["id"] for row in cursor.fetchall()]

total = len(all_ids)
print(f"Total: {total} contatos", flush=True)

# Step 2: Process in batches
for batch_num, i in enumerate(range(0, len(all_ids), BATCH_SIZE)):
    batch_ids = all_ids[i:i+BATCH_SIZE]

    with get_db() as conn:
        cursor = conn.cursor()

        for contact_id in batch_ids:
            # Get contact
            cursor.execute("""
                SELECT id, nome, tags, total_interacoes, ultimo_contato,
                       aniversario, linkedin, empresa, cargo, foto_url,
                       contexto, score, circulo, frequencia_ideal_dias
                FROM contacts WHERE id = %s
            """, (contact_id,))

            row = cursor.fetchone()
            if not row:
                continue

            contact = dict(row)
            circulo_anterior = contact.get("circulo") or 5

            # Calculate
            circulo, score, reasons = calcular_score_circulo(contact)
            health = calcular_health_score(contact, circulo)

            # Update
            cursor.execute("""
                UPDATE contacts
                SET circulo = %s, health_score = %s, ultimo_calculo_circulo = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (circulo, health, contact_id))

            stats["processados"] += 1
            stats["por_circulo"][circulo] += 1
            if circulo != circulo_anterior:
                stats["mudaram"] += 1

        conn.commit()

    # Progress every 10 batches
    if batch_num % 10 == 0 or (i + len(batch_ids)) >= len(all_ids):
        progresso = (i + len(batch_ids)) / len(all_ids) * 100
        print(f"Batch {batch_num+1}: {stats['processados']}/{total} ({progresso:.1f}%) - Mudaram: {stats['mudaram']}", flush=True)

print("\n" + "=" * 50, flush=True)
print("RESULTADO", flush=True)
print("=" * 50, flush=True)
print(f"Total: {stats['processados']}", flush=True)
print(f"Mudaram: {stats['mudaram']}", flush=True)
print("Por circulo:", flush=True)
for c in range(1, 6):
    print(f"  C{c}: {stats['por_circulo'][c]}", flush=True)
