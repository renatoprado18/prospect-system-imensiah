#!/usr/bin/env python3
"""
Reprocessa os alertas processuais arquivados e os entrega AGRUPADOS POR PROCESSO.

A R3.7 (`2ce6ce8`) conserta a classificacao dos alertas NOVOS. Os que ja estavam
gravados seguem `archive_proposed` — a resolucao acontece uma vez, na entrada.
Este script reavalia os existentes.

⚠️ AGRUPA POR PROCESSO, e isso nao e cosmetica: os 30 alertas dos ultimos 30
dias tratam de **5 processos**. "Alexandro Bereska x NK Parking S/A" sozinho
responde por **12** deles. Reabrir 30 itens numa fila que o Renato le a mao seria
trocar um defeito (nunca ve) por outro (ve 30 vezes a mesma coisa) — e o board
e explicito: ferramenta nao vira tarefa dele.

Desenho: TODOS os processuais viram `must_read` + `needs_attention`, mas so o
alerta MAIS RECENTE de cada processo fica `pending` (a fila que ele le). Os
demais ficam `dismissed` com razao dizendo que foram **agrupados** — o que e
diferente de descartados por irrelevancia, e deixa o rastro auditavel.

    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/reprocessa_processual.py
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/reprocessa_processual.py --apply

Reversivel: grava o estado anterior em JSON antes de escrever (`--backup`).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from services.processo_agrupador import agrupar  # noqa: E402

REMETENTES = ("%jusbrasil%", "%aurum%")

# ⚠️ `email_triage.classification_reasons` e **jsonb**, nao `text[]`: passar lista
# Python direto levanta `DatatypeMismatch`. Mesmo tropeco que a CoS registrou em
# 10/10 sobre `email_drafts.to_emails`. Vai `json.dumps(...) + ::jsonb`.


def _conn():
    """Alvo declarado E conferido contra a URL resolvida."""
    from database import _get_conn_string  # noqa: PLC0415

    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo not in ("local", "prod"):
        sys.exit("DB_TARGET nao declarado (local|prod)")
    url = _get_conn_string()
    print(f"[DB] {(url.split('@')[-1].split('/')[0])[:48]} (target={alvo})")
    return psycopg2.connect(url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--backup", default=None, help="arquivo JSON do estado anterior")
    args = ap.parse_args()

    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT e.message_id, e.subject, e.content, e.timestamp,
               t.id AS triage_id, t.status, t.classification,
               t.needs_attention, t.priority
          FROM copilot.emails e
          JOIN email_triage t ON t.message_id = e.message_id
         WHERE (e.from_email ILIKE %s OR e.from_email ILIKE %s)
           AND e.timestamp >= now() - (%s || ' days')::interval
         ORDER BY e.timestamp DESC
        """,
        (*REMETENTES, args.dias),
    )
    rows = [dict(r) for r in cur.fetchall()]
    grupos, fora = agrupar(rows)

    print(f"\n╔═ REPROCESSAMENTO DE ALERTAS PROCESSUAIS ({args.dias}d) ═╗")
    print(f"  {len(rows)} alertas → {len(grupos)} PROCESSOS "
          f"· {len(fora)} fora (nao tratam de processo)\n")

    destaques, agrupadas = [], []
    for g in grupos:
        g["alertas"].sort(key=lambda a: a["timestamp"], reverse=True)
        destaques.append((g, g["alertas"][0]))
        agrupadas.extend(g["alertas"][1:])
        print(f"  📌 {g['rotulo'][:54]:54} {g['n']:2} alerta(s) · "
              f"último {str(g['ultimo'])[:16]}")
        for a in g["alertas"][:3]:
            print(f"       · {str(a['timestamp'])[:16]} {str(a['subject'])[:52]}")
        if g["n"] > 3:
            print(f"       … e mais {g['n'] - 3}")

    print(f"\n  ⇒ a fila do Renato passaria a ter {len(destaques)} item(ns), "
          f"não {len(rows)}")
    print(f"  ⇒ {len(agrupadas)} alerta(s) ficam como AGRUPADOS (contexto, "
          f"não descarte)")
    if fora:
        print(f"\n  os {len(fora)} de fora seguem intocados (são marketing):")
        for r in fora:
            print(f"       · {str(r['subject'])[:56]}")

    if not args.apply:
        print("\n  — SECO, nada escrito. Rode com --apply. —")
        return

    # Backup ANTES de escrever: o estado anterior nao e derivavel depois.
    backup = args.backup or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        "reprocessa_processual_backup.json")
    with open(backup, "w") as fh:
        json.dump([{k: (str(v) if k == "timestamp" else v)
                    for k, v in r.items() if k != "content"}
                   for r in rows], fh, indent=2, default=str)
    print(f"\n  💾 estado anterior salvo em {backup}")

    for g, topo in destaques:
        cur.execute(
            """
            UPDATE email_triage
               SET classification = 'must_read', priority = 9,
                   needs_attention = TRUE, status = 'pending',
                   classification_reasons = %s::jsonb
             WHERE id = %s
            """,
            (json.dumps([
                f"Alerta processual — processo: {g['rotulo']}",
                f"Agrupa {g['n']} alerta(s) do mesmo processo "
                f"(R3.7, reprocessado 19/10)"]),
             topo["triage_id"]),
        )
    conn.commit()

    for a in agrupadas:
        ident = next((g for g, _ in destaques if a in g["alertas"]), None)
        rot = ident["rotulo"] if ident else "?"
        cur.execute(
            """
            UPDATE email_triage
               SET classification = 'must_read', priority = 9,
                   needs_attention = FALSE, status = 'dismissed',
                   classification_reasons = %s::jsonb
             WHERE id = %s
            """,
            (json.dumps([
                f"Alerta processual AGRUPADO sob: {rot}",
                "Nao e descarte por irrelevancia — o alerta mais recente do "
                "mesmo processo esta pending"]),
             a["triage_id"]),
        )
    conn.commit()

    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE t.status = 'pending') AS pend,
               COUNT(*) FILTER (WHERE t.needs_attention) AS att,
               COUNT(*) AS tot
          FROM copilot.emails e JOIN email_triage t ON t.message_id = e.message_id
         WHERE (e.from_email ILIKE %s OR e.from_email ILIKE %s)
           AND e.timestamp >= now() - (%s || ' days')::interval
        """,
        (*REMETENTES, args.dias),
    )
    d = cur.fetchone()
    print(f"\n  ✍️  depois: pending={d['pend']} · needs_attention={d['att']} "
          f"· total={d['tot']}")

    if d["pend"] != len(destaques):
        print(f"  🔴 esperava {len(destaques)} pending, achei {d['pend']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
