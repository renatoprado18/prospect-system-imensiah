#!/usr/bin/env python3
"""
DRY-RUN da cascata de identidade sobre os grupos de duplicatas JA existentes.

SOMENTE LEITURA. Este script nao escreve nada — nenhum INSERT/UPDATE/DELETE/DDL,
nenhum merge. A limpeza da base e decisao do Renato; aqui so respondemos
"quantos grupos a nova cascata reconheceria com seguranca, quantos ficariam de
fora pela guarda, e quais sao os casos ambiguos".

Uso:
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 .venv/bin/python \
        scripts/dryrun_google_identity_cascade.py
    # ou, contra a copia local:
    DB_TARGET=local .venv/bin/python scripts/dryrun_google_identity_cascade.py
"""
import os
import sys
from collections import defaultdict
from pathlib import Path

# Load .env (mesma convencao dos demais scripts/)
_ENV = Path(__file__).parent.parent / ".env"
if _ENV.exists():
    with open(_ENV) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from database import get_db  # noqa: E402
from services.contact_identity import (  # noqa: E402
    GOOGLE_IDS_COLUMN,
    SHARED_LINE_MAX_CONTACTS,
    contact_emails,
    contact_phones,
    names_match,
    phone_kind,
)


def components(ids, names, kind):
    """Particiona um grupo em componentes conexas por `names_match`."""
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            ia, ib = ids[a], ids[b]
            if names_match(names[ia], names[ib], kind):
                parent[find(ia)] = find(ib)

    buckets = defaultdict(list)
    for i in ids:
        buckets[find(i)].append(i)
    return list(buckets.values())


def main():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT id, nome, emails, telefones, origem, contexto,
                   google_contact_id, {GOOGLE_IDS_COLUMN}
            FROM contacts
        """)
        rows = [dict(r) for r in cur.fetchall()]

    by_id = {r["id"]: r for r in rows}
    by_phone = defaultdict(list)
    by_email = defaultdict(list)
    for r in rows:
        for p in contact_phones(r):
            by_phone[p].append(r["id"])
        for e in contact_emails(r):
            by_email[e].append(r["id"])

    names = {r["id"]: (r["nome"] or "") for r in rows}
    origens = {r["id"]: (r["origem"] or "") for r in rows}

    stats = {
        "grupos": 0, "colapsa_todo": 0, "colapsa_parcial": 0,
        "fora_guarda": 0, "ramal": 0,
        "fichas_em_grupos": 0, "fichas_reconhecidas": 0,
        "ja_pegos_por_email": 0,
        "uma_de_cada_conta": 0,
    }
    ambiguos = []
    parciais = []

    for phone, ids in sorted(by_phone.items()):
        ids = sorted(set(ids))
        if len(ids) < 2:
            continue

        stats["grupos"] += 1
        stats["fichas_em_grupos"] += len(ids)
        kind = phone_kind(phone)

        contas = {origens[i] for i in ids}
        if len(contas) > 1:
            stats["uma_de_cada_conta"] += 1

        # o tier (b) e-mail ja resolveria?
        emails_do_grupo = set()
        for i in ids:
            emails_do_grupo |= set(contact_emails(by_id[i]))
        if any(len(set(by_email[e]) & set(ids)) > 1 for e in emails_do_grupo):
            stats["ja_pegos_por_email"] += 1

        if len(ids) > SHARED_LINE_MAX_CONTACTS:
            stats["ramal"] += 1
            ambiguos.append(("ramal", phone, kind,
                             [names[i] for i in ids[:6]], len(ids)))
            continue

        comps = components(ids, names, kind)
        maiores = [c for c in comps if len(c) > 1]

        if len(comps) == 1:
            stats["colapsa_todo"] += 1
            stats["fichas_reconhecidas"] += len(ids) - 1
        elif maiores:
            stats["colapsa_parcial"] += 1
            stats["fichas_reconhecidas"] += sum(len(c) - 1 for c in maiores)
            parciais.append((phone, kind, [[names[i] for i in c] for c in comps]))
        else:
            stats["fora_guarda"] += 1
            ambiguos.append(("nomes_distintos", phone, kind,
                             [names[i] for i in ids], len(ids)))

    print("=" * 72)
    print("DRY-RUN — cascata de identidade sobre duplicatas por telefone")
    print(f"alvo: DB_TARGET={os.getenv('DB_TARGET')}   contatos: {len(rows)}")
    print("=" * 72)
    for k, v in stats.items():
        print(f"  {k:24} {v}")

    print("\n--- ambiguos: LINHA COMPARTILHADA (a guarda barrou) ---")
    fora = [a for a in ambiguos if a[0] == "nomes_distintos"]
    for _, phone, kind, nomes, n in fora[:40]:
        print(f"  {phone} [{kind}] n={n}: " + " | ".join(nomes[:5]))
    print(f"  ... {len(fora)} grupos nesta categoria")

    print("\n--- ambiguos: RAMAL / linha de muitos (> "
          f"{SHARED_LINE_MAX_CONTACTS} fichas, tier telefone nem tenta) ---")
    for _, phone, kind, nomes, n in [a for a in ambiguos if a[0] == "ramal"][:20]:
        print(f"  {phone} [{kind}] n={n}: " + " | ".join(nomes) + " ...")

    print("\n--- grupos que colapsam PARCIALMENTE (>=2 pessoas na mesma linha,"
          " cada uma com suas duplicatas) ---")
    for phone, kind, comps in parciais[:20]:
        print(f"  {phone} [{kind}]: " +
              "  ||  ".join("+".join(c) for c in comps))
    print(f"  ... {len(parciais)} grupos nesta categoria")


if __name__ == "__main__":
    main()
