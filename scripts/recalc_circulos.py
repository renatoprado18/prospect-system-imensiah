#!/usr/bin/env python3
"""
Script para recalcular circulos de todos os contatos em lotes.
Executa diretamente no banco de dados.

Uso: python scripts/recalc_circulos.py
"""

import os
import sys

# Adicionar o diretorio app ao path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from database import get_db
from services.circulos import (
    calcular_score_circulo,
    calcular_health_score,
    CIRCULO_CONFIG
)

def recalcular_em_lotes(batch_size: int = 500):
    """Recalcula circulos em lotes para evitar timeout."""

    print("=" * 60)
    print("RECALCULO DE CIRCULOS")
    print("=" * 60)

    with get_db() as conn:
        cursor = conn.cursor()

        # Contar total
        cursor.execute("SELECT COUNT(*) as count FROM contacts")
        total = cursor.fetchone()["count"]
        print(f"Total de contatos: {total}")

        # Estatisticas
        stats = {
            "processados": 0,
            "mudaram": 0,
            "por_circulo": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        }

        offset = 0
        while offset < total:
            # Buscar lote
            cursor.execute("""
                SELECT id, nome, tags, total_interacoes, ultimo_contato,
                       aniversario, linkedin, empresa, cargo, foto_url,
                       contexto, score, circulo, circulo_manual,
                       frequencia_ideal_dias, health_score
                FROM contacts
                WHERE circulo_manual IS NOT TRUE OR circulo_manual IS NULL
                ORDER BY id
                LIMIT %s OFFSET %s
            """, (batch_size, offset))

            contacts = cursor.fetchall()
            if not contacts:
                break

            for row in contacts:
                contact = dict(row)
                circulo_anterior = contact.get("circulo") or 5

                # Calcular novo circulo e health
                circulo, score, reasons = calcular_score_circulo(contact)
                health = calcular_health_score(contact, circulo)

                # Atualizar no banco
                cursor.execute("""
                    UPDATE contacts
                    SET circulo = %s,
                        health_score = %s,
                        ultimo_calculo_circulo = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (circulo, health, contact["id"]))

                stats["processados"] += 1
                stats["por_circulo"][circulo] += 1

                if circulo != circulo_anterior:
                    stats["mudaram"] += 1

            offset += len(contacts)
            progresso = (offset / total) * 100
            print(f"Progresso: {offset}/{total} ({progresso:.1f}%) - Mudaram: {stats['mudaram']}")

        # Commit final
        conn.commit()

    print("\n" + "=" * 60)
    print("RESULTADO FINAL")
    print("=" * 60)
    print(f"Total processados: {stats['processados']}")
    print(f"Total que mudaram de circulo: {stats['mudaram']}")
    print("\nDistribuicao por circulo:")
    for c in range(1, 6):
        config = CIRCULO_CONFIG.get(c, {})
        nome = config.get("nome", f"Circulo {c}")
        print(f"  Circulo {c} ({nome}): {stats['por_circulo'][c]}")

    return stats


if __name__ == "__main__":
    recalcular_em_lotes(batch_size=500)
