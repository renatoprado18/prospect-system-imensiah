#!/usr/bin/env python3
"""Recalcula circulos em batches pequenos com commits frequentes."""
import os
import sys
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from database import get_db
from services.circulos import calcular_score_circulo, calcular_health_score

BATCH_SIZE = 100
stats = {"processados": 0, "mudaram": 0, "por_circulo": {1:0, 2:0, 3:0, 4:0, 5:0}}

print("=" * 50)
print("RECALCULO DE CIRCULOS")
print("=" * 50)

with get_db() as conn:
    cursor = conn.cursor()

    # Get total
    cursor.execute("SELECT COUNT(*) as count FROM contacts WHERE circulo_manual IS NOT TRUE OR circulo_manual IS NULL")
    total = cursor.fetchone()["count"]
    print(f"Total a processar: {total}")

    # Get all IDs first
    cursor.execute("""
        SELECT id FROM contacts
        WHERE circulo_manual IS NOT TRUE OR circulo_manual IS NULL
        ORDER BY id
    """)
    all_ids = [row["id"] for row in cursor.fetchall()]

print(f"IDs carregados: {len(all_ids)}")

# Process in small batches with separate connections
for i in range(0, len(all_ids), BATCH_SIZE):
    batch_ids = all_ids[i:i+BATCH_SIZE]

    with get_db() as conn:
        cursor = conn.cursor()

        for contact_id in batch_ids:
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

        # Commit after each batch
        conn.commit()

    progresso = (i + len(batch_ids)) / len(all_ids) * 100
    print(f"Batch {i//BATCH_SIZE + 1}: {stats['processados']}/{len(all_ids)} ({progresso:.1f}%) - Mudaram: {stats['mudaram']}")
    time.sleep(0.1)  # Small pause to avoid overwhelming the DB

print("\n" + "=" * 50)
print("RESULTADO")
print("=" * 50)
print(f"Total: {stats['processados']}")
print(f"Mudaram: {stats['mudaram']}")
print("Por circulo:")
for c in range(1, 6):
    print(f"  C{c}: {stats['por_circulo'][c]}")
