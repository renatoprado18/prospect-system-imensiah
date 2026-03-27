#!/usr/bin/env python3
"""
Daily Maintenance Cron Job

Executa tarefas de manutencao diaria:
- Recalcula circulos para contatos com interacoes recentes
- Atualiza health scores
- Gera alertas de contatos precisando atencao
- Limpa dados antigos/temporarios
"""
import os
import sys
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                value = value.strip('"').strip("'")
                os.environ.setdefault(key, value)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db


def recalculate_recent_circles():
    """Recalcula circulos para contatos com atividade recente"""
    print("  Recalculando circulos...", flush=True)

    with get_db() as conn:
        cursor = conn.cursor()

        # Find contacts with recent activity (last 7 days)
        cursor.execute("""
            SELECT id FROM contacts
            WHERE ultimo_contato > NOW() - INTERVAL '7 days'
            OR atualizado_em > NOW() - INTERVAL '7 days'
        """)
        contact_ids = [row["id"] for row in cursor.fetchall()]

        if not contact_ids:
            print(f"    Nenhum contato com atividade recente", flush=True)
            return 0

        # Import circle calculation function
        try:
            from services.circulos import calcular_score_circulo

            updated = 0
            for contact_id in contact_ids:
                try:
                    result = calcular_score_circulo(contact_id)
                    new_circulo = result[0] if isinstance(result, tuple) else result

                    cursor.execute("""
                        UPDATE contacts
                        SET circulo = %s, atualizado_em = NOW()
                        WHERE id = %s AND (circulo IS NULL OR circulo != %s)
                        AND COALESCE(circulo_manual, FALSE) = FALSE
                    """, (new_circulo, contact_id, new_circulo))

                    if cursor.rowcount > 0:
                        updated += 1
                except:
                    pass

            conn.commit()
            print(f"    {updated} circulos atualizados", flush=True)
            return updated

        except ImportError:
            print("    Modulo de circulos nao disponivel", flush=True)
            return 0


def update_health_scores():
    """Atualiza health scores para contatos importantes"""
    print("  Atualizando health scores...", flush=True)

    with get_db() as conn:
        cursor = conn.cursor()

        try:
            from services.circulos import calcular_health_score

            # Update health for circles 1-3
            cursor.execute("""
                SELECT id FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
            """)
            contact_ids = [row["id"] for row in cursor.fetchall()]

            updated = 0
            for contact_id in contact_ids:
                try:
                    health = calcular_health_score(contact_id)
                    cursor.execute("""
                        UPDATE contacts
                        SET health_score = %s
                        WHERE id = %s
                    """, (health, contact_id))
                    updated += 1
                except:
                    pass

            conn.commit()
            print(f"    {updated} health scores atualizados", flush=True)
            return updated

        except ImportError:
            print("    Modulo de circulos nao disponivel", flush=True)
            return 0


def generate_attention_alerts():
    """Gera alertas para contatos que precisam de atencao"""
    print("  Gerando alertas de atencao...", flush=True)

    with get_db() as conn:
        cursor = conn.cursor()

        alerts = []

        # Contacts without interaction for too long
        cursor.execute("""
            SELECT id, nome, circulo, ultimo_contato,
                   EXTRACT(DAYS FROM NOW() - ultimo_contato) as dias_sem_contato
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 2
            AND ultimo_contato < NOW() - INTERVAL '30 days'
            ORDER BY circulo, ultimo_contato ASC
            LIMIT 20
        """)

        for row in cursor.fetchall():
            alerts.append({
                "type": "stale_contact",
                "contact_id": row["id"],
                "contact_name": row["nome"],
                "circle": row["circulo"],
                "days_since_contact": int(row["dias_sem_contato"]) if row["dias_sem_contato"] else None
            })

        # Upcoming birthdays (next 7 days)
        cursor.execute("""
            SELECT id, nome, aniversario
            FROM contacts
            WHERE aniversario IS NOT NULL
            AND (
                (EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
                 AND EXTRACT(DAY FROM aniversario) BETWEEN EXTRACT(DAY FROM CURRENT_DATE)
                 AND EXTRACT(DAY FROM CURRENT_DATE) + 7)
                OR
                (EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE + INTERVAL '7 days')
                 AND EXTRACT(DAY FROM aniversario) <= EXTRACT(DAY FROM CURRENT_DATE + INTERVAL '7 days'))
            )
            LIMIT 20
        """)

        for row in cursor.fetchall():
            alerts.append({
                "type": "upcoming_birthday",
                "contact_id": row["id"],
                "contact_name": row["nome"],
                "birthday": row["aniversario"].strftime("%d/%m") if row["aniversario"] else None
            })

        # Low health score contacts
        cursor.execute("""
            SELECT id, nome, circulo, health_score
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 2
            AND health_score < 40
            ORDER BY health_score ASC
            LIMIT 10
        """)

        for row in cursor.fetchall():
            alerts.append({
                "type": "low_health",
                "contact_id": row["id"],
                "contact_name": row["nome"],
                "circle": row["circulo"],
                "health_score": row["health_score"]
            })

        print(f"    {len(alerts)} alertas gerados", flush=True)
        return alerts


def cleanup_old_data():
    """Limpa dados antigos/temporarios"""
    print("  Limpando dados antigos...", flush=True)

    with get_db() as conn:
        cursor = conn.cursor()
        deleted = 0

        # Delete old orphan messages (no contact)
        cursor.execute("""
            DELETE FROM messages
            WHERE contact_id IS NULL
            AND enviado_em < NOW() - INTERVAL '30 days'
        """)
        deleted += cursor.rowcount

        # Delete old temporary data if exists
        try:
            cursor.execute("""
                DELETE FROM contact_facts
                WHERE confianca < 0.3
                AND criado_em < NOW() - INTERVAL '90 days'
            """)
            deleted += cursor.rowcount
        except:
            pass

        conn.commit()
        print(f"    {deleted} registros removidos", flush=True)
        return deleted


def update_statistics():
    """Atualiza estatisticas do sistema"""
    print("  Atualizando estatisticas...", flush=True)

    with get_db() as conn:
        cursor = conn.cursor()

        # Count totals
        cursor.execute("SELECT COUNT(*) as total FROM contacts")
        total_contacts = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE total_interacoes > 0")
        with_interactions = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COALESCE(circulo, 5) as c, COUNT(*) as count
            FROM contacts
            GROUP BY COALESCE(circulo, 5)
            ORDER BY c
        """)
        circle_counts = {f"C{row['c']}": row["count"] for row in cursor.fetchall()}

        stats = {
            "date": datetime.now().isoformat(),
            "total_contacts": total_contacts,
            "with_interactions": with_interactions,
            "circles": circle_counts
        }

        print(f"    Total: {total_contacts}, Com interacoes: {with_interactions}", flush=True)
        return stats


async def run_maintenance(full: bool = False):
    """Executa manutencao completa"""
    print("=" * 60, flush=True)
    print("MANUTENCAO DIARIA", flush=True)
    print("=" * 60, flush=True)
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"Modo: {'COMPLETO' if full else 'RAPIDO'}", flush=True)
    print(flush=True)

    results = {
        "started_at": datetime.now().isoformat(),
        "tasks": {}
    }

    # 1. Recalculate circles
    print("[1/5] Circulos", flush=True)
    results["tasks"]["circles"] = recalculate_recent_circles()

    # 2. Update health scores
    print("[2/5] Health Scores", flush=True)
    results["tasks"]["health"] = update_health_scores()

    # 3. Generate alerts
    print("[3/5] Alertas", flush=True)
    results["tasks"]["alerts"] = generate_attention_alerts()

    # 4. Cleanup
    print("[4/5] Limpeza", flush=True)
    results["tasks"]["cleanup"] = cleanup_old_data()

    # 5. Statistics
    print("[5/5] Estatisticas", flush=True)
    results["tasks"]["stats"] = update_statistics()

    results["completed_at"] = datetime.now().isoformat()

    print(flush=True)
    print("=" * 60, flush=True)
    print("MANUTENCAO CONCLUIDA", flush=True)
    print("=" * 60, flush=True)

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Executa manutencao completa")
    args = parser.parse_args()

    asyncio.run(run_maintenance(full=args.full))
