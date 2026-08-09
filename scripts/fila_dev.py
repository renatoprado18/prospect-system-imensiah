#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Fila de demandas de código — o que a abertura `/dev` lê no começo da sessão.

POR QUE EXISTE. A migration 068 (08/08/26) criou o projeto **"Sistema / Dev"**
para receber demanda de código de qualquer origem (o Renato, a sessão CoS, a
tonIAH), e a justificativa dela dizia, com todas as letras: *"a abertura /dev lê
a fila e promove o que cabe na sessão"*. Só que ninguém escreveu esse leitor — a
fila nasceu com duas tarefas dentro e **nenhum consumidor**, no mesmo dia. É o
padrão do [[feedback_consumidor_morto_wiring]]: "wirado" não é "vivo", e uma
fila que ninguém lê é pior que fila nenhuma, porque quem escreve nela acha que
entregou o recado.

FILA ≠ BOARD, e a distinção é o que evita duplicar: aqui é a caixa de ENTRADA
(qualquer um joga demanda); o `project_dev_backlog.md` continua sendo o board da
sessão, com o que está em curso e as prioridades. Uma tarefa promovida sai da
fila e vira linha do board.

O projeto é resolvido pelo NOME, nunca por id fixo — id de banco muda entre
local e prod, e hardcodar transforma um script em mentira silenciosa no outro
alvo ([[feedback_no_hardcoded_contact_ids]]).

Uso:  ./fila_dev.py                 # a fila
      ./fila_dev.py --quiet         # só se houver algo (abertura de sessão)
      DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 ./fila_dev.py   # contra o Neon
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from database import get_connection  # noqa: E402

PROJETO = "Sistema / Dev"
ABERTAS = ("pending", "in_progress", "on_hold")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="silencia quando a fila está vazia")
    a = ap.parse_args()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM projects WHERE nome = %s", (PROJETO,))
    proj = cur.fetchone()
    if not proj:
        # Ausência do projeto não é fila vazia — é o alvo errado ou a 068 não
        # aplicada. Dizer "nada na fila" aqui seria mentir por omissão.
        print(f"⚠️  projeto {PROJETO!r} não existe neste alvo — a 068 rodou aqui?")
        conn.close()
        return 2

    cur.execute(
        """
        SELECT t.id, t.titulo, t.status, t.prioridade, t.data_criacao,
               c.nome AS accountable
          FROM tasks t
          LEFT JOIN contacts c ON c.id = t.accountable_id
         WHERE t.project_id = %s AND t.status = ANY(%s)
         ORDER BY t.prioridade ASC NULLS LAST, t.data_criacao ASC
        """,
        (proj["id"], list(ABERTAS)),
    )
    linhas = cur.fetchall()
    conn.close()

    if not linhas:
        if not a.quiet:
            print(f"╔═ FILA DEV ({PROJETO}) ═╗\n  🟢 vazia — nada esperando promoção.")
        return 0

    print(f"╔═ FILA DEV ({PROJETO}) — {len(linhas)} esperando ═╗")
    for t in linhas:
        prio = f"P{t['prioridade']}" if t["prioridade"] is not None else "P?"
        dono = f" · A: {t['accountable']}" if t.get("accountable") else ""
        idade = f" · desde {str(t['data_criacao'])[:10]}" if t.get("data_criacao") else ""
        print(f"  [{prio}] #{t['id']} {t['titulo'][:74]}")
        print(f"        {t['status']}{dono}{idade}")
    print("\n  Promover = escolher uma, pegar os locks e levá-la pro board.")
    print("  Não promover também é decisão — mas então diga ao Renato por que ficou.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
