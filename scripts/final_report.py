#!/usr/bin/env python3
"""Relatorio final de distribuicao."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db
import json

print("=" * 60, flush=True)
print("RELATORIO FINAL - DISTRIBUICAO DE CONTATOS", flush=True)
print("=" * 60, flush=True)

with get_db() as conn:
    cursor = conn.cursor()

    # Total contacts
    cursor.execute("SELECT COUNT(*) as total FROM contacts")
    total = cursor.fetchone()["total"]
    print(f"\nTotal de contatos: {total}", flush=True)

    # Distribution by circle
    print("\n1. DISTRIBUICAO POR CIRCULO:", flush=True)
    cursor.execute("""
        SELECT COALESCE(circulo, 5) as circulo, COUNT(*) as total
        FROM contacts
        GROUP BY COALESCE(circulo, 5)
        ORDER BY circulo
    """)
    for row in cursor.fetchall():
        pct = row["total"] / total * 100
        print(f"   Circulo {row['circulo']}: {row['total']} ({pct:.1f}%)", flush=True)

    # Manual vs automatic
    print("\n2. CIRCULOS MANUAIS vs AUTOMATICOS:", flush=True)
    cursor.execute("""
        SELECT circulo_manual, COUNT(*) as total
        FROM contacts
        GROUP BY circulo_manual
    """)
    for row in cursor.fetchall():
        tipo = "Manual" if row["circulo_manual"] else "Automatico"
        print(f"   {tipo}: {row['total']}", flush=True)

    # Health score distribution
    print("\n3. DISTRIBUICAO POR HEALTH SCORE:", flush=True)
    cursor.execute("""
        SELECT
            CASE
                WHEN health_score >= 80 THEN 'Excelente (80-100)'
                WHEN health_score >= 60 THEN 'Bom (60-79)'
                WHEN health_score >= 40 THEN 'Regular (40-59)'
                WHEN health_score >= 20 THEN 'Baixo (20-39)'
                ELSE 'Critico (0-19)'
            END as faixa,
            COUNT(*) as total
        FROM contacts
        GROUP BY 1
        ORDER BY 1
    """)
    for row in cursor.fetchall():
        print(f"   {row['faixa']}: {row['total']}", flush=True)

    # Top tags
    print("\n4. TOP 15 TAGS:", flush=True)
    cursor.execute("SELECT tags FROM contacts WHERE tags IS NOT NULL AND tags::text != '[]'")
    tag_counts = {}
    for row in cursor.fetchall():
        try:
            tags = json.loads(row["tags"] or "[]") if isinstance(row["tags"], str) else (row["tags"] or [])
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        except:
            pass

    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    for tag, count in sorted_tags:
        print(f"   {tag}: {count}", flush=True)

    # Contacts with interactions
    print("\n5. INTERACOES:", flush=True)
    cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE total_interacoes > 0")
    with_interactions = cursor.fetchone()["total"]
    print(f"   Com interacoes: {with_interactions}", flush=True)
    print(f"   Sem interacoes: {total - with_interactions}", flush=True)

    # Contacts with recent activity
    print("\n6. ATIVIDADE RECENTE:", flush=True)
    cursor.execute("""
        SELECT
            COUNT(*) FILTER (WHERE ultimo_contato >= CURRENT_DATE - INTERVAL '30 days') as ultimos_30d,
            COUNT(*) FILTER (WHERE ultimo_contato >= CURRENT_DATE - INTERVAL '90 days') as ultimos_90d,
            COUNT(*) FILTER (WHERE ultimo_contato >= CURRENT_DATE - INTERVAL '365 days') as ultimo_ano
        FROM contacts
    """)
    activity = cursor.fetchone()
    print(f"   Ultimos 30 dias: {activity['ultimos_30d']}", flush=True)
    print(f"   Ultimos 90 dias: {activity['ultimos_90d']}", flush=True)
    print(f"   Ultimo ano: {activity['ultimo_ano']}", flush=True)

print("\n" + "=" * 60, flush=True)
print("FIM DO RELATORIO", flush=True)
print("=" * 60, flush=True)
