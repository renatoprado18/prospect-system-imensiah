#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Desfaz criações de contato no Google feitas por engano pelo backfill.

POR QUE EXISTE (07/08/2026). O backfill das fichas antigas indexou só a agenda
profissional e criou 53 contatos que já existiam na pessoal — 53 de 53. A causa
está consertada (`indice_telefones_todas`), mas o estrago já tinha sido feito, e
"deixa lá, é só duplicata" seria transformar um erro de 40 minutos em ruído
permanente na agenda dele.

DESFAZER É MAIS PERIGOSO QUE FAZER, então nada acontece sem as três guardas
baterem para cada contato, uma por uma:

  1. O contato a apagar tem que ser o que ESTE backfill criou: o
     `google_contact_id` da ficha, gravado na janela informada em `--minutos`.
  2. Tem que existir na agenda pessoal um contato com o MESMO telefone — é a
     prova de que a exclusão não perde nada, porque a pessoa continua lá.
  3. O alvo NÃO pode estar na agenda pessoal. Se estiver, é contato antigo dele e
     não se toca, por mais que os outros sinais batam.

Falhou qualquer uma, a ficha é pulada e reportada. Depois de apagar, a ficha do
INTEL passa a apontar para o contato da agenda PESSOAL — que é onde a pessoa
sempre esteve. Se outra ficha já apontar pra lá (índice único), volta a NULL: o
estado de antes, sem inventar vínculo.

Uso:
    scripts/desfaz_criacoes_google.py --nomes <arquivo>            # dry-run
    scripts/desfaz_criacoes_google.py --nomes <arquivo> --aplicar
"""
import argparse
import asyncio
import os
import re
import sys

import httpx
import psycopg2
import psycopg2.errors
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
sys.path.insert(0, f"{ROOT}/app")

CONTA_ALVO = "renato@almeida-prado.com"        # onde o backfill criou por engano
CONTA_PESSOAL = "renato.almeida.prado@gmail.com"


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def k8(v):
    d = re.sub(r"\D", "", v or "")
    return d[-8:] if len(d) >= 10 else ""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nomes", required=True, help="arquivo com um nome por linha")
    ap.add_argument("--minutos", type=int, default=90)
    ap.add_argument("--aplicar", action="store_true")
    args = ap.parse_args()

    os.environ.setdefault("DATABASE_URL", env("DATABASE_URL"))
    os.environ.setdefault("DB_TARGET", "prod")
    os.environ.setdefault("ALLOW_PROD_FROM_LOCAL", "1")
    import integrations.google_contacts as gc

    nomes = [l.strip() for l in open(args.nomes) if l.strip()]
    conn = psycopg2.connect(env("DATABASE_URL"), keepalives=1,
                            cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""SELECT id, nome, telefones, google_contact_id
                     FROM contacts
                    WHERE nome = ANY(%s)
                      AND google_contact_id IS NOT NULL
                      AND atualizado_em > now() - make_interval(mins => %s)""",
                (nomes, args.minutos))
    fichas = [dict(r) for r in cur.fetchall()]
    print(f"fichas candidatas a desfazer: {len(fichas)}")

    # Índice da agenda PESSOAL: é a prova de que a pessoa continua lá (guarda 2)
    # e a fonte do vínculo correto.
    tok_p = await gc.get_valid_token(CONTA_PESSOAL)
    if not tok_p:
        print("✗ sem token da conta pessoal — abortando: sem ela não dá pra provar "
              "que a exclusão não perde nada")
        return 1
    idx_pessoal, ids_pessoais = {}, set()
    for p in await gc.fetch_all_contacts(tok_p):
        rid = (p.get("resourceName") or "").replace("people/", "")
        ids_pessoais.add(rid)
        for pn in (p.get("phoneNumbers") or []):
            k = k8(pn.get("value"))
            if k:
                idx_pessoal.setdefault(k, rid)
    print(f"agenda pessoal: {len(idx_pessoal)} telefones\n")

    tok_a = await gc.get_valid_token(CONTA_ALVO)
    if not tok_a:
        print("✗ sem token da conta profissional")
        return 1

    contas = {"apagado": 0, "revinculado": 0, "voltou_null": 0,
              "pulado_sem_par": 0, "pulado_e_pessoal": 0, "erro": 0}

    async with httpx.AsyncClient(timeout=30) as cli:
        for f in fichas:
            gid = f["google_contact_id"]
            chaves = [k8(t.get("number")) for t in (f["telefones"] or [])
                      if isinstance(t, dict) and k8(t.get("number"))]

            # guarda 3 — o alvo é contato antigo da agenda pessoal? não se toca.
            if gid in ids_pessoais:
                contas["pulado_e_pessoal"] += 1
                continue

            # guarda 2 — existe a mesma pessoa na pessoal?
            par = next((idx_pessoal[k] for k in chaves if k in idx_pessoal), None)
            if not par:
                contas["pulado_sem_par"] += 1
                print(f"  · pulei #{f['id']} {str(f['nome'])[:34]} — sem par na pessoal")
                continue

            if not args.aplicar:
                contas["apagado"] += 1
                continue

            try:
                r = await cli.delete(
                    f"https://people.googleapis.com/v1/people/{gid}:deleteContact",
                    headers={"Authorization": f"Bearer {tok_a}"})
                if r.status_code not in (200, 204):
                    contas["erro"] += 1
                    print(f"  ✗ #{f['id']} HTTP {r.status_code}: {r.text[:90]}")
                    continue
                contas["apagado"] += 1
            except Exception as e:
                contas["erro"] += 1
                print(f"  ✗ #{f['id']} {str(e)[:90]}")
                continue

            try:
                cur.execute("""UPDATE contacts SET google_contact_id = %s, atualizado_em = now()
                                WHERE id = %s""", (par, f["id"]))
                contas["revinculado"] += 1
            except psycopg2.errors.UniqueViolation:
                # Outra ficha já aponta pro contato da pessoal. NULL é o estado
                # de antes do backfill — honesto, e o merge decide depois.
                cur.execute("""UPDATE contacts SET google_contact_id = NULL, atualizado_em = now()
                                WHERE id = %s""", (f["id"],))
                contas["voltou_null"] += 1

    print(f"\n{'APLICADO' if args.aplicar else 'DRY-RUN (nada apagado)'}:")
    for k, v in contas.items():
        if v:
            print(f"  {k:22} {v}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
