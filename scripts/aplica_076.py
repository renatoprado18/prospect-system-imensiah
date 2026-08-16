#!/usr/bin/env python3
"""Aplica a 076 (`jabo_group_reads`) — o registro do caminho que atualiza as
tasks do #28 a partir do grupo Governança Jabô.

DDL pura: a tabela nasce vazia de propósito, ao contrário da 074. Aqui não há o
que backfillar — o histórico do grupo não deve virar atualização retroativa. Foi
exatamente isso que produziu o descarte em massa de 22/07 no shadow do
ConselhoOS (42 de 72 propostas eram reprocessamento de evidência velha, e a CoS
descartou 43 à mão). O caminho novo começa do zero e olha só pra frente: a
janela de leitura é ancorada na hora da aplicação, não no início do grupo.

Uso:
  set -a && source .env && set +a && \
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/aplica_076.py [--dry-run]
"""
import os
import sys
import pathlib

import psycopg2
from psycopg2.extras import RealDictCursor

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SQL = (RAIZ / "scripts" / "migrations" / "076_jabo_group_reads.sql").read_text(encoding="utf-8")


def main() -> int:
    dry = "--dry-run" in sys.argv
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL ausente", file=sys.stderr)
        return 1
    alvo = os.getenv("DB_TARGET", "(não declarado)")
    print(f"[076] alvo={alvo} host={url.split('@')[-1][:52]}")
    if alvo == "(não declarado)":
        # `reference_db_target_protocol`: alvo declarado, nunca deduzido.
        print("[076] recuse-se a adivinhar: declare DB_TARGET.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.jabo_group_reads') AS t")
        se_existe = cur.fetchone()["t"]
        print(f"[076] tabela antes: {se_existe or 'ausente'}")

        if dry:
            print("[076] dry-run — nada aplicado")
            return 0

        cur.execute(SQL)
        conn.commit()

        cur.execute("SELECT to_regclass('public.jabo_group_reads') AS t")
        print(f"[076] tabela depois: {cur.fetchone()['t']}")
        cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='jabo_group_reads'
                        ORDER BY ordinal_position""")
        for c in cur.fetchall():
            print(f"        {c['column_name']:18s} {c['data_type']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
