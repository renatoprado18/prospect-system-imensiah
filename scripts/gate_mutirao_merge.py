#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""GATE do mutirão de merge — os números que faltam para a decisão do Renato.

POR QUE EXISTE (23/08/2026). O `ensaio_merge_google.py` respondeu *"o caminho
cria duplicata no Google?"* — 12 CREATE → **0** em 120 grupos. Provou o CAMINHO.
Não respondeu a pergunta que o Renato precisa decidir, que é outra:

    soltar o mutirão em QUANTAS fichas, e o que se perde se estiver errado?

O board carregou "111 grupos" por semanas. Os 111 não existem: eram triagem
manual de 22/08, não critério de código. Este script mede o que o código de fato
faria HOJE, contra PROD, **sem escrever uma linha**.

O QUE ELE MEDE, e por que cada bloco está aqui:

1. **AUDITORIA DAS TABELAS** (o bloco que pode reprovar o mutirão sozinho).
   O merge reponta as FKs com `_FK_TABLES_TO_CONTACTS` — uma lista **escrita à
   mão**, auditada pela última vez em 25/07/26. O contrato do modelo foi de 165
   para 168 tabelas depois disso. Tabela com `contact_id` que não esteja na
   lista é uma de duas coisas, ambas caladas: se a FK é CASCADE, **o merge apaga
   o histórico dela** e termina 200; se não há FK, sobra órfã apontando pra
   linha inexistente. Em 1 merge isso é um bug; em 1.319 é um estrago sem volta.
   [[feedback_audit_link_tables_antes_de_bulk]] · [[feedback_cascade_chain_warning]]

2. **OS GRUPOS, POR TIPO DE CHAVE.** `find_duplicates` agrupa por telefone,
   e-mail **e nome**. O corte que o ensaio usa (nomes normalizados todos iguais)
   tira do caminho o balde do telefone de central — mas é cego para o inverso:
   um grupo formado SÓ pelo nome tem homônimo como hipótese principal, e passa
   no corte justamente porque os nomes são iguais. Fundir dois "José Carlos
   Silva" diferentes é o mesmo dano do balde, pela porta oposta. Por isso o
   placar sai **separado por chave**, nunca num total só.
   [[feedback_mesmo_telefone_nao_e_mesma_pessoa]]

3. **O PESO DO QUE SOME.** Ficha vazia fundida errado custa pouco; ficha com 400
   mensagens e 3 tasks fundida errado custa o histórico de uma relação. O placar
   conta o conteúdo das fichas **não-sobreviventes** — é essa a massa em risco,
   não o total de contatos.

4. **SOBREPOSIÇÃO.** O mesmo contato pode cair em vários grupos (por telefone e
   por nome). O mutirão roda grupo a grupo; o segundo grupo encontra um id que o
   primeiro já apagou. Quantos, e quais.

⚠️ LEITURA PURA. Nenhum INSERT/UPDATE/DELETE, nem local nem no Google. Roda
contra prod de propósito: é prod que o mutirão tocaria, e medir no local seria
medir outra base ([[feedback_medir_o_consumidor_certo]]).

Uso:
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/gate_mutirao_merge.py
    ... --amostra 15      # quantos exemplos imprimir por bucket (default 8)
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT / "scripts"))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db  # noqa: E402
from services.contact_dedup import (  # noqa: E402
    _FK_TABLES_TO_CONTACTS, find_duplicates, merge_contacts,
    normalize_name_for_dedup,
)

# O MESMO corte e a MESMA query que o ensaio de 22/08 exercitou. Importados, não
# recopiados: um gate que reimplementa o critério mede a si mesmo, e passaria a
# divergir do ensaio no dia em que um dos dois mudasse.
from ensaio_merge_google import COLUNAS_DA_ROTA, _grupos_fundiveis  # noqa: E402

# Tabelas onde "conteúdo" quer dizer histórico que dói perder. Não é a lista de
# FKs (essa é reparo referencial) — é a régua do CUSTO DE ERRAR.
TABELAS_DE_PESO = [
    ("whatsapp_messages", "contact_id", "msgs WA"),
    ("messages", "contact_id", "msgs"),
    ("group_messages", "contact_id", "msgs grupo"),
    ("tasks", "contact_id", "tasks"),
    ("calendar_events", "contact_id", "eventos"),
    ("contact_facts", "contact_id", "fatos"),
    ("email_triage", "contact_id", "e-mails"),
]


def _q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()


# ─────────────────────── 1. auditoria das tabelas ───────────────────────

def auditar_tabelas(cur) -> int:
    """Compara o banco REAL com a lista escrita à mão que o merge usa."""
    print("=" * 72)
    print("1. AUDITORIA — o merge conhece todas as tabelas que apontam pra contacts?")
    print("=" * 72)

    # Toda coluna chamada contact_id (ou owner_contact_id) no schema public.
    colunas = _q(cur, """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND column_name IN ('contact_id', 'owner_contact_id')
         ORDER BY table_name
    """)
    # Só TABELAS — view herda a coluna da tabela de baixo e não se reponta.
    tabelas_reais = _q(cur, """
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    base = {r["table_name"] for r in tabelas_reais}

    # Regra de deleção declarada em cada FK que aponta pra contacts.
    fks = _q(cur, """
        SELECT tc.table_name, kcu.column_name, rc.delete_rule
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_schema = kcu.table_schema
          JOIN information_schema.referential_constraints rc
            ON tc.constraint_name = rc.constraint_name
           AND tc.table_schema = rc.constraint_schema
          JOIN information_schema.constraint_column_usage ccu
            ON rc.unique_constraint_name = ccu.constraint_name
           AND rc.unique_constraint_schema = ccu.table_schema
         WHERE tc.table_schema = 'public'
           AND tc.constraint_type = 'FOREIGN KEY'
           AND ccu.table_name = 'contacts'
    """)
    regra = {(r["table_name"], r["column_name"]): r["delete_rule"] for r in fks}

    conhecidas = set(_FK_TABLES_TO_CONTACTS) | {"projects"}  # owner_contact_id à parte
    faltantes = []
    for r in colunas:
        t, c = r["table_name"], r["column_name"]
        if t not in base or t in conhecidas or t == "contacts":
            continue
        faltantes.append((t, c, regra.get((t, c), "SEM FK")))

    print(f"  tabelas com coluna de contato: {len([r for r in colunas if r['table_name'] in base])}")
    print(f"  na lista do merge:             {len(_FK_TABLES_TO_CONTACTS)} + projects.owner_contact_id")

    if not faltantes:
        print("\n  🟢 nenhuma tabela fora da lista — o merge reponta tudo que existe hoje.")
        return 0

    print(f"\n  🔴 {len(faltantes)} TABELA(S) FORA DA LISTA — o merge não as reponta:\n")
    grave = 0
    for t, c, dr in sorted(faltantes):
        linhas = _q(cur, f"SELECT COUNT(*) AS n FROM {t} WHERE {c} IS NOT NULL")[0]["n"]
        if dr == "CASCADE":
            marca, custo, grave = "🔴", "APAGA essas linhas", grave + 1
        elif dr == "SET NULL":
            marca, custo = "🟠", "perde o vínculo (vira NULL)"
        elif dr == "SEM FK":
            marca, custo = "🟠", "vira órfã apontando pra id inexistente"
        else:
            marca, custo = "🟠", f"DELETE em contacts FALHA ({dr})"
        print(f"     {marca} {t}.{c:18s} {dr:10s} {linhas:>7,} linhas — {custo}")

    print(f"\n  VEREDITO: {'🔴 NÃO soltar' if grave else '🟠 corrigir antes de soltar'}"
          f" — a lista está desatualizada em relação ao banco.")
    return 1 if grave else 0


# ─────────────────────── 2-4. os grupos ───────────────────────

def _tipo(chave: str) -> str:
    return chave.split(":", 1)[0]


def medir_grupos(cur, amostra: int) -> None:
    cur.execute(COLUNAS_DA_ROTA)
    contacts = [dict(r) for r in cur.fetchall()]
    fundiveis, balde = _grupos_fundiveis(contacts)

    print("\n" + "=" * 72)
    print("2. OS GRUPOS — quantos, de que tipo, e quais o código realmente fundiria")
    print("=" * 72)
    print(f"  contatos em prod:              {len(contacts):,}")
    print(f"  grupos de duplicata:           {len(fundiveis) + len(balde):,}")
    print(f"    fundíveis (nomes iguais):    {len(fundiveis):,}")
    print(f"    🔴 balde (nomes diferentes): {len(balde):,}  — fora do mutirão")

    por_tipo = Counter(_tipo(k) for k in fundiveis)
    fichas_por_tipo = Counter()
    for k, g in fundiveis.items():
        fichas_por_tipo[_tipo(k)] += len(g) - 1

    print("\n  FUNDÍVEIS POR CHAVE — o risco não é o mesmo em cada uma:")
    explica = {
        "phone": "telefone igual + nome igual — evidência forte, é o caso limpo",
        "email": "e-mail igual + nome igual — evidência forte",
        "name": "⚠️ SÓ o nome. Homônimo é a hipótese rival, e passa no corte",
    }
    for t in ("phone", "email", "name"):
        if por_tipo.get(t):
            print(f"    {t:6s} {por_tipo[t]:>5,} grupos  →  {fichas_por_tipo[t]:>5,} fichas somem"
                  f"   · {explica[t]}")

    total_somem = sum(len(g) - 1 for g in fundiveis.values())
    print(f"\n  fichas que o mutirão APAGARIA no total: {total_somem:,}")

    # ── sobreposição ──
    aparece_em = defaultdict(list)
    for k, g in fundiveis.items():
        for c in g:
            aparece_em[c["id"]].append(k)
    repetidos = {cid: ks for cid, ks in aparece_em.items() if len(ks) > 1}
    print(f"\n  contatos em MAIS DE UM grupo:          {len(repetidos):,}")
    if repetidos:
        print("    (o mutirão roda grupo a grupo; do 2º em diante o id já foi apagado —")
        print("     o merge pula ou explode, e o grupo seguinte não é fundido)")
        for cid, ks in list(repetidos.items())[:amostra]:
            nome = next((c["nome"] for c in fundiveis[ks[0]] if c["id"] == cid), "?")
            print(f"       #{cid} {str(nome)[:28]:28s} em {len(ks)}: {', '.join(k[:22] for k in ks[:3])}")

    # ── peso ──
    print("\n" + "=" * 72)
    print("3. O PESO — quanto histórico está nas fichas que sumiriam")
    print("=" * 72)
    condenados = []
    for g in fundiveis.values():
        primary = merge_contacts(g)["id"]
        condenados += [c["id"] for c in g if c["id"] != primary]
    print(f"  fichas não-sobreviventes: {len(condenados):,}")

    peso = defaultdict(Counter)
    for tabela, col, rotulo in TABELAS_DE_PESO:
        existe = _q(cur, """SELECT 1 FROM information_schema.tables
                             WHERE table_schema='public' AND table_name=%s""", (tabela,))
        if not existe:
            print(f"    {rotulo:12s} — tabela ausente neste alvo, pulada")
            continue
        rows = _q(cur, f"""SELECT {col} AS cid, COUNT(*) AS n FROM {tabela}
                            WHERE {col} = ANY(%s) GROUP BY {col}""", (condenados,))
        total = sum(r["n"] for r in rows)
        print(f"    {rotulo:12s} {total:>8,} linhas em {len(rows):>5,} fichas")
        for r in rows:
            peso[r["cid"]][rotulo] = r["n"]

    com_peso = sorted(peso.items(), key=lambda kv: -sum(kv[1].values()))
    print(f"\n  fichas condenadas COM histórico: {len(com_peso):,} de {len(condenados):,}"
          f"  ({100*len(com_peso)//max(len(condenados),1)}%)")
    print(f"  as {min(amostra, len(com_peso))} mais pesadas — se alguma dessas for merge errado, o dano é aqui:")
    nomes = {c["id"]: c["nome"] for g in fundiveis.values() for c in g}
    for cid, cnt in com_peso[:amostra]:
        detalhe = " · ".join(f"{v:,} {k}" for k, v in cnt.most_common(3))
        print(f"     #{cid} {str(nomes.get(cid))[:30]:30s} {detalhe}")

    # ── amostras ──
    print("\n" + "=" * 72)
    print("4. AMOSTRAS — o que a decisão está aprovando, em nomes")
    print("=" * 72)
    for t, titulo in (("phone", "fundíveis por TELEFONE (caso limpo)"),
                      ("email", "fundíveis por E-MAIL"),
                      ("name", "⚠️ fundíveis SÓ POR NOME — leia com atenção")):
        itens = [(k, g) for k, g in fundiveis.items() if _tipo(k) == t]
        if not itens:
            continue
        print(f"\n  {titulo} — {len(itens):,} grupos, mostrando {min(amostra, len(itens))}:")
        for k, g in itens[:amostra]:
            emp = {str(c.get("empresa") or "—") for c in g}
            print(f"     {len(g)} fichas · {str(g[0].get('nome'))[:34]:34s} "
                  f"ids {','.join(str(c['id']) for c in g[:4])}"
                  f"{'' if len(emp) == 1 else '  ⚠️ empresas diferentes: ' + ' / '.join(list(emp)[:2])}")

    print(f"\n  🔴 BALDE — {len(balde):,} grupos que juntam PESSOAS DIFERENTES "
          f"(ficam fora do mutirão; mostrando {min(amostra, len(balde))}):")
    for k, g in list(balde.items())[:amostra]:
        print(f"     {k[:30]:30s} {' / '.join(str(c.get('nome'))[:20] for c in g[:3])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amostra", type=int, default=8,
                    help="exemplos por bucket (default 8)")
    args = ap.parse_args()

    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    print(f"[gate] alvo={alvo!r} — LEITURA PURA, nada é escrito\n")

    conn = get_db()
    cur = conn.cursor()
    rc = auditar_tabelas(cur)
    medir_grupos(cur, args.amostra)

    print("\n" + "=" * 72)
    print("A DECISÃO (gate do Renato)")
    print("=" * 72)
    print("  (a) soltar nos fundíveis por telefone/e-mail — evidência forte")
    print("  (b) os fundíveis SÓ POR NOME entram junto, ou ficam pra revisão manual?")
    print("  (c) o que fazer com o balde: nada, ou revisar um a um?")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
