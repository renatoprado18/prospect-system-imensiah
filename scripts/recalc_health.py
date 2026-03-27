#!/usr/bin/env python3
"""
Recalcula health scores de todos os contatos.

O health score indica a "saude" do relacionamento:
- 100: Contato recente, dentro da frequencia ideal
- 50: Contato moderado, precisa de atencao
- 0: Muito tempo sem contato, relacionamento esfriando

Formula:
health = max(0, min(100, 100 - (dias_sem_contato / frequencia_ideal * 50)))

Uso: python scripts/recalc_health.py
"""
import os
import sys

# Adicionar o diretorio app ao path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from dotenv import load_dotenv
load_dotenv()

from database import get_db
from datetime import datetime


# Frequencia ideal por circulo (dias)
FREQUENCIA_POR_CIRCULO = {
    1: 7,    # Circulo intimo: semanal
    2: 14,   # Circulo proximo: quinzenal
    3: 30,   # Networking: mensal
    4: 90,   # Conhecidos: trimestral
    5: 365   # Arquivo: anual
}


def calcular_health(dias_sem_contato: int, frequencia_dias: int) -> int:
    """
    Calcula health score baseado em dias sem contato.

    Args:
        dias_sem_contato: Dias desde o ultimo contato
        frequencia_dias: Frequencia ideal em dias

    Returns:
        Health score de 0 a 100
    """
    if frequencia_dias <= 0:
        frequencia_dias = 30

    # Quanto mais dias sem contato, menor o health
    # Se dias_sem_contato == frequencia_dias, health = 50
    # Se dias_sem_contato == 0, health = 100
    # Se dias_sem_contato == 2*frequencia_dias, health = 0

    health = 100 - (dias_sem_contato / frequencia_dias * 50)
    return max(0, min(100, int(health)))


def recalc_health():
    """Recalcula health scores de todos os contatos."""

    print("=" * 60)
    print("RECALCULO DE HEALTH SCORES")
    print("=" * 60)
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar todos os contatos dos circulos 1-4
        cursor.execute("""
            SELECT id, nome, circulo, ultimo_contato, frequencia_ideal_dias, health_score
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 4
            ORDER BY circulo, nome
        """)

        contacts = cursor.fetchall()
        print(f"Total de contatos ativos (circulos 1-4): {len(contacts)}")
        print()

        updated = 0
        stats = {1: [], 2: [], 3: [], 4: []}

        for row in contacts:
            contact_id = row["id"]
            nome = row["nome"]
            circulo = row["circulo"] or 5
            ultimo = row["ultimo_contato"]
            freq_custom = row["frequencia_ideal_dias"]
            health_atual = row["health_score"]

            # Usar frequencia customizada ou padrao do circulo
            freq = freq_custom or FREQUENCIA_POR_CIRCULO.get(circulo, 30)

            # Calcular dias sem contato
            if ultimo:
                dias = (datetime.now() - ultimo).days
            else:
                dias = 365  # Sem contato registrado = 1 ano

            # Calcular novo health
            novo_health = calcular_health(dias, freq)

            # Atualizar se diferente
            if novo_health != health_atual:
                cursor.execute("""
                    UPDATE contacts SET health_score = %s WHERE id = %s
                """, (novo_health, contact_id))
                updated += 1

            # Estatisticas
            if circulo in stats:
                stats[circulo].append(novo_health)

        conn.commit()

        # Imprimir estatisticas
        print("ESTATISTICAS POR CIRCULO:")
        print("-" * 40)
        for c in range(1, 5):
            if stats[c]:
                avg = sum(stats[c]) / len(stats[c])
                low = len([h for h in stats[c] if h < 30])
                medium = len([h for h in stats[c] if 30 <= h < 70])
                high = len([h for h in stats[c] if h >= 70])
                print(f"  Circulo {c}:")
                print(f"    Total: {len(stats[c])}")
                print(f"    Health medio: {avg:.1f}%")
                print(f"    Critico (<30): {low}")
                print(f"    Atencao (30-70): {medium}")
                print(f"    Bom (>=70): {high}")
                print()

        print("-" * 40)
        print(f"Health scores atualizados: {updated} contatos")
        print("=" * 60)

        return updated


if __name__ == "__main__":
    recalc_health()
