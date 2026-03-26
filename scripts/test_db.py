#!/usr/bin/env python3
"""Test database connection and recalc functions."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

print("Testing database connection...", flush=True)

from database import get_db
from services.circulos import calcular_score_circulo, calcular_health_score

print("Imports OK", flush=True)

try:
    with get_db() as conn:
        print("Connection OK", flush=True)
        cursor = conn.cursor()

        # Get count
        cursor.execute("SELECT COUNT(*) as count FROM contacts")
        total = cursor.fetchone()["count"]
        print(f"Total contacts: {total}", flush=True)

        # Get one contact to test
        cursor.execute("""
            SELECT id, nome, tags, total_interacoes, ultimo_contato,
                   aniversario, linkedin, empresa, cargo, foto_url,
                   contexto, score, circulo, frequencia_ideal_dias
            FROM contacts LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            contact = dict(row)
            print(f"Test contact: {contact['nome']}", flush=True)

            circulo, score, reasons = calcular_score_circulo(contact)
            print(f"Circle calc: circulo={circulo}, score={score}", flush=True)

            health = calcular_health_score(contact, circulo)
            print(f"Health: {health}", flush=True)

        print("All tests passed!", flush=True)

except Exception as e:
    print(f"Error: {e}", flush=True)
    import traceback
    traceback.print_exc()
