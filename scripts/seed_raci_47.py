#!/usr/bin/env python3
"""
Seed do RACI do projeto #47 (reorg das 7 empresas) — de TEXTO pra linhas.

Por que existe: a matriz do #47 vivia dentro da `project_note` #268, em prosa
("• Regularização contábil (DFs+ECF): R=Priscila · A=Renato · C=Piccino ·
I=Gustavo"). Legível por humano, inútil pra máquina — não ordena por prazo,
não filtra por status, não imprime. Este script transcreve as 8 linhas da nota
pra `raci_itens`, que é o 1º caso de uso do RACI genérico (task #999703).

TRANSCRIÇÃO, NÃO INTERPRETAÇÃO: papéis e ações saem literais da nota. Prazo
NÃO é inventado — a nota tem um "PAINEL DE PRAZOS" que aponta pra tasks, e
amarrar prazo de task a linha de RACI aqui seria adivinhar qual task pertence
a qual linha. As linhas nascem sem prazo (responsabilidade permanente) e o
Renato põe data onde fizer sentido, na própria tela.

Idempotente: identifica pela `origem` ('nota:268') + ação. Rodar de novo não
duplica.

Uso:
    DB_TARGET=local  python3 scripts/seed_raci_47.py
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/seed_raci_47.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from database import get_db  # noqa: E402

PROJECT_ID = 47
ORIGEM = "nota:268"

# area, acao, R, A, C, I
ITENS = [
    ("Contábil", "Regularização contábil (DFs + ECF)",
     "Priscila", "Renato", "Piccino", "Gustavo"),
    ("Tributário", "CDAs federais — decidir prescrever × transacionar",
     "Piccino (+Andressa levanta)", "Renato", "Priscila", "Gustavo"),
    ("Trabalhista", "Processos trabalhistas (10)",
     "Wanelise", "Renato", "Priscila (provisão)", "Gustavo"),
    ("Cível", "Processos cíveis (13, incl. Makerfest)",
     "Piccino", "Renato", None, "Gustavo"),
    ("Societário", "Incorporação / fusão das empresas",
     "Piccino", "Renato", "Wanelise (sucessão) + Gustavo (sócio)", None),
    ("Financeiro", "Financiamento da reorg",
     "Renato", "Renato", "Orestes (pagador)", "Priscila"),
    ("Patrimonial", "Firewall herança / holding",
     "Renato", "Renato", "Piccino", "Orestes"),
    ("Coordenação", "Coordenação e cadência (grupo RENATAO)",
     "Renato + Andressa", "Renato", "Piccino", "Gustavo / Priscila"),
]


def main():
    criados, existentes = 0, 0
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT nome FROM projects WHERE id = %s", (PROJECT_ID,))
        row = cursor.fetchone()
        if not row:
            print(f"projeto #{PROJECT_ID} não existe neste banco — abortando")
            return 1
        print(f"projeto #{PROJECT_ID}: {row['nome']}")

        for area, acao, r, a, c, i in ITENS:
            cursor.execute("""
                SELECT id FROM raci_itens
                 WHERE project_id = %s AND origem = %s AND acao = %s
            """, (PROJECT_ID, ORIGEM, acao))
            if cursor.fetchone():
                existentes += 1
                continue
            cursor.execute("""
                INSERT INTO raci_itens
                    (project_id, area, acao, responsavel_r, responsavel_a,
                     responsavel_c, responsavel_i, status, origem)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pendente', %s)
            """, (PROJECT_ID, area, acao, r, a, c, i, ORIGEM))
            criados += 1
        conn.commit()

    print(f"criados: {criados} · já existiam: {existentes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
