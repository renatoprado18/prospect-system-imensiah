#!/usr/bin/env python3
"""
Colhe o mapa LID -> telefone de todos os grupos conhecidos.

Fonte: `GET /group/findGroupInfos`, que devolve por participante o par
`{id: "<lid>@lid", phoneNumber: "<e164>@s.whatsapp.net"}`. E o unico caminho que
resolve LID em retrospecto — `findMessages` nao devolve `remoteJidAlt` em replay,
e foi por isso que o backlog registrava LID como irrecuperavel.

    # mede e NAO escreve (default)
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/wa_lid_colher.py

    # colhe e grava
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/wa_lid_colher.py --apply

Aditivo: nunca sobrescreve par existente (conflito e contado e logado, nao
aplicado). Reversivel por `DELETE FROM wa_lid_map`.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from services.wa_lid_map import (  # noqa: E402
    colher_grupo,
    cobertura,
    gravar_pares,
)


def _conn():
    """Conecta pelo resolvedor do app — o alvo declarado e CONFERIDO contra a
    URL resolvida. Ler `DATABASE_URL` cru imprime o rotulo e abre no outro
    banco ([[feedback_teste_com_ambiente_declarado_nao_aplicado]])."""
    from database import _get_conn_string  # noqa: PLC0415

    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo not in ("local", "prod"):
        sys.exit("DB_TARGET nao declarado (local|prod) — ver reference_db_target_protocol")
    url = _get_conn_string()
    print(f"[DB] {(url.split('@')[-1].split('/')[0])[:48]} (target={alvo})")
    return psycopg2.connect(url)


def _foto(cur, titulo):
    c = cobertura(cur)
    print(f"  {titulo}: mapa {c['pares_no_mapa']} pares · "
          f"LIDs cobertos {c['lids_cobertos']}/{c['lids_distintos']} "
          f"({c['pct_lids_cobertos']}%) · msgs cobertas "
          f"{c['msgs_cobertas']}/{c['msgs']}")
    return c


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="grava; sem isso so mede")
    args = ap.parse_args()

    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("╔═ COLHEITA DO MAPA LID -> TELEFONE ═╗")
    _foto(cur, "antes ")

    # Varre todo grupo de que ha mensagem OU que esta no cache: o LID de quem
    # fala num grupo pode ser o mesmo de uma DM, entao mapear grupo em que ele
    # nunca falou ainda rende.
    cur.execute("""
        SELECT DISTINCT group_jid FROM (
            SELECT group_jid FROM group_messages
            UNION
            SELECT group_jid FROM social_groups_cache
        ) t WHERE group_jid LIKE '%@g.us'
    """)
    jids = [r["group_jid"] for r in cur.fetchall()]
    print(f"\n  {len(jids)} grupos a varrer\n")

    tot = {"novos": 0, "conflitos": 0, "iguais": 0}
    pares_vistos = 0
    sem_resposta = []

    for i, jid in enumerate(jids, 1):
        pares = await colher_grupo(jid)
        if not pares:
            # Grupo que nao respondeu NAO e grupo sem LID: a diferenca importa,
            # e somar os dois faria a cobertura parecer completa.
            sem_resposta.append(jid)
            continue
        pares_vistos += len(pares)
        if args.apply:
            r = gravar_pares(cur, pares, origem_jid=jid)
            for k in tot:
                tot[k] += r[k]
            conn.commit()      # por grupo: o Neon derruba lote longo sem erro
        if i % 10 == 0:
            print(f"    … {i}/{len(jids)} grupos, {pares_vistos} pares vistos")

    print(f"\n  grupos varridos      : {len(jids) - len(sem_resposta)}")
    print(f"  grupos SEM resposta  : {len(sem_resposta)}"
          f"{' ⚠️ nao e o mesmo que sem LID' if sem_resposta else ''}")
    print(f"  pares (lid,fone)     : {pares_vistos}")
    if args.apply:
        print(f"  ✍️  novos no mapa     : {tot['novos']}")
        print(f"  = ja iguais          : {tot['iguais']}")
        print(f"  🔴 CONFLITOS         : {tot['conflitos']}"
              f"{'  (par antigo preservado — ver log)' if tot['conflitos'] else ''}")
        depois = _foto(cur, "depois")
        if depois["pares_no_mapa"] < 1 and pares_vistos > 0:
            print("\n  🔴 colheu pares e o mapa ficou vazio — algo nao gravou")
            sys.exit(1)
    else:
        print("\n  — SECO, nada escrito. Rode com --apply. —")

    if sem_resposta:
        print(f"\n  grupos sem resposta (primeiros 10): {sem_resposta[:10]}")


if __name__ == "__main__":
    asyncio.run(main())
