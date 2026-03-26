#!/usr/bin/env python3
"""
Script INTEL: Recalcular circulos, aplicar tags e verificar duplicados.

Uso: cd /Users/rap/prospect-system && python scripts/intel_recalc_all.py
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
from services.auto_tags import (
    analisar_contato_para_tags,
    aplicar_tags_contato
)
from services.duplicados import (
    encontrar_duplicados,
    get_duplicate_statistics
)


def recalcular_circulos(batch_size: int = 500):
    """Tarefa 1: Recalcula circulos de todos os contatos."""

    print("\n" + "=" * 60)
    print("TAREFA 1: RECALCULO DE CIRCULOS")
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
            # Buscar lote (excluindo circulo_manual)
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
            print(f"  Processados: {stats['processados']}/{total} ({progresso:.1f}%)")

    print(f"\n  Total processados: {stats['processados']}")
    print(f"  Mudaram de circulo: {stats['mudaram']}")
    print("\n  Distribuicao:")
    for c in range(1, 6):
        config = CIRCULO_CONFIG.get(c, {})
        nome = config.get("nome", f"Circulo {c}")
        print(f"    C{c} ({nome}): {stats['por_circulo'][c]}")

    return stats


def aplicar_auto_tags(batch_size: int = 500):
    """Tarefa 2: Aplica tags automaticas baseadas em empresa, cargo, etc."""

    print("\n" + "=" * 60)
    print("TAREFA 2: APLICAR TAGS AUTOMATICAS")
    print("=" * 60)

    with get_db() as conn:
        cursor = conn.cursor()

        # Contar total
        cursor.execute("SELECT COUNT(*) as count FROM contacts")
        total = cursor.fetchone()["count"]
        print(f"Total de contatos: {total}")

        stats = {
            "processados": 0,
            "com_novas_tags": 0,
            "total_tags_aplicadas": 0
        }

        offset = 0
        while offset < total:
            cursor.execute("""
                SELECT id, nome, empresa, cargo, emails, tags
                FROM contacts
                ORDER BY id
                LIMIT %s OFFSET %s
            """, (batch_size, offset))

            contacts = cursor.fetchall()
            if not contacts:
                break

            for row in contacts:
                contact = dict(row)
                contact_id = contact["id"]

                # Analisar e aplicar tags
                resultado = analisar_contato_para_tags(contact_id)
                tags_novas = resultado.get("tags_novas", [])

                if tags_novas:
                    aplicar_tags_contato(contact_id, tags_novas)
                    stats["com_novas_tags"] += 1
                    stats["total_tags_aplicadas"] += len(tags_novas)

                stats["processados"] += 1

            offset += len(contacts)
            progresso = (offset / total) * 100
            print(f"  Processados: {stats['processados']}/{total} ({progresso:.1f}%)")

    print(f"\n  Total processados: {stats['processados']}")
    print(f"  Contatos com novas tags: {stats['com_novas_tags']}")
    print(f"  Total tags aplicadas: {stats['total_tags_aplicadas']}")

    return stats


def verificar_duplicados():
    """Tarefa 3: Verifica duplicados no sistema."""

    print("\n" + "=" * 60)
    print("TAREFA 3: VERIFICAR DUPLICADOS")
    print("=" * 60)

    # Buscar duplicados com threshold 0.6
    result = encontrar_duplicados(threshold=0.6, limit=50)

    print(f"\n  Total de pares duplicados encontrados: {result['total']}")

    if result['duplicates']:
        print("\n  Top 10 duplicados mais provaveis:")
        for i, dup in enumerate(result['duplicates'][:10], 1):
            c1 = dup['contact1']
            c2 = dup['contact2']
            score = dup['score']
            reasons = ", ".join(dup['reasons'][:2])
            print(f"    {i}. Score {score:.0%}: {c1['nome']} <-> {c2['nome']}")
            print(f"       Motivo: {reasons}")

    return result


def verificar_distribuicao_final():
    """Tarefa 4: Verifica distribuicao final dos circulos."""

    print("\n" + "=" * 60)
    print("TAREFA 4: DISTRIBUICAO FINAL")
    print("=" * 60)

    with get_db() as conn:
        cursor = conn.cursor()

        # Distribuicao por circulo
        cursor.execute("""
            SELECT COALESCE(circulo, 5) as circulo, COUNT(*) as total
            FROM contacts
            GROUP BY COALESCE(circulo, 5)
            ORDER BY circulo
        """)

        print("\n  Distribuicao por circulo:")
        total_geral = 0
        for row in cursor.fetchall():
            c = row["circulo"]
            total = row["total"]
            total_geral += total
            config = CIRCULO_CONFIG.get(c, {})
            nome = config.get("nome", f"Circulo {c}")
            print(f"    Circulo {c} ({nome}): {total}")

        print(f"\n  Total geral: {total_geral}")

        # Tags mais comuns
        cursor.execute("SELECT tags FROM contacts WHERE tags IS NOT NULL AND tags != '[]'")
        tag_counts = {}
        for row in cursor.fetchall():
            try:
                import json
                tags = json.loads(row["tags"] or "[]")
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            except:
                pass

        if tag_counts:
            print("\n  Top 10 tags mais comuns:")
            sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            for tag, count in sorted_tags:
                print(f"    {tag}: {count}")


def main():
    """Executa todas as tarefas."""

    print("\n" + "#" * 60)
    print("# INTEL - RECALCULO E ANALISE COMPLETA")
    print("#" * 60)

    # Tarefa 1: Recalcular circulos
    circulos_stats = recalcular_circulos()

    # Tarefa 2: Aplicar tags
    tags_stats = aplicar_auto_tags()

    # Tarefa 3: Verificar duplicados
    dup_stats = verificar_duplicados()

    # Tarefa 4: Distribuicao final
    verificar_distribuicao_final()

    # Resumo final
    print("\n" + "#" * 60)
    print("# RESUMO FINAL")
    print("#" * 60)
    print(f"""
  Circulos:
    - Processados: {circulos_stats['processados']}
    - Mudaram: {circulos_stats['mudaram']}

  Tags:
    - Contatos atualizados: {tags_stats['com_novas_tags']}
    - Tags aplicadas: {tags_stats['total_tags_aplicadas']}

  Duplicados:
    - Pares encontrados: {dup_stats['total']}
""")

    print("=" * 60)
    print("CONCLUIDO!")
    print("=" * 60)


if __name__ == "__main__":
    main()
