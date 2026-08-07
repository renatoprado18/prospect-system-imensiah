#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Backfill INTEL → Google Contacts: as fichas antigas que nunca subiram.

POR QUE EXISTE (07/08/2026). O write-back entrou no ar de manhã com duas travas
deliberadas — teto de 20 por rodada e **janela de 30 dias** — porque escrever na
agenda pessoal de alguém em lote é o tipo de coisa que se descobre tarde demais.
A consequência aceita na hora: o histórico inteiro ficou de fora. Este script é a
segunda metade, feita com o Renato mandando, e o cron de hora em hora continua
cuidando do fluxo novo.

O NÚMERO QUE O BOARD REGISTRAVA ERA OUTRO. "199 fichas só no INTEL" veio do
`paridade_contatos.py`, que conta quem **não casa por telefone** com a agenda. O
critério de quem precisa subir é diferente — `google_contact_id IS NULL` — e dá
**1.425**. A diferença não é erro de ninguém: são ~1.200 fichas que EXISTEM nas
duas pontas e nunca tiveram o vínculo gravado. Para elas o certo não é criar, é
vincular. Medir antes de agir mudou o trabalho de "subir 199" para "vincular
1.200 e subir 199" — e vincular não escreve nada no Google.

TRÊS GUARDAS, porque o custo do erro é assimétrico (duplicata na agenda dele é
cara de desfazer, ficha que não subiu hoje sobe amanhã):

  1. ÍNDICE ANTES DE TUDO. Uma listagem da agenda inteira, e se ela falhar nada
     acontece — senão "não achei" significaria só "não olhei"
     ([[reference_contatos_paridade_google]]).
  2. SÓ CRIA O QUE DEU PRA PROCURAR. Ficha cujos telefones não têm DDD não é
     deduplicável: `_chave_tel` exige 10 dígitos. Criar às cegas duplicaria quem
     já está lá com o número completo. São 39 fichas — a guarda mora no
     `google_push.push_contato`, aqui só se conta.
  3. DRY-RUN É O PADRÃO. `--aplicar` é explícito.

CONEXÃO ÚNICA COM `keepalives`, e commit por item. O `push_varios` abre uma
conexão por ficha; em 1.425 fichas o Neon derruba o socket no meio e o processo
fica VIVO pendurado, sem query, sem erro e sem progresso — aconteceu duas vezes
em 05/08 ([[project_dev_backlog]], monitoramento técnico). Por isso o laço é
daqui e não do `push_varios`, reusando dele as regras (`_chave_tel`, o índice) em
vez de reescrevê-las.

Uso:
    scripts/backfill_google_contacts.py                 # dry-run de tudo
    scripts/backfill_google_contacts.py --aplicar       # vale
    scripts/backfill_google_contacts.py --aplicar --limite 50
"""
import argparse
import asyncio
import os
import sys

import psycopg2
import psycopg2.errors
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
sys.path.insert(0, f"{ROOT}/app")

CONTA = "renato@almeida-prado.com"

# Órfã cujo telefone bate com o de uma ficha JÁ vinculada é a mesma pessoa
# cadastrada duas vezes AQUI — não é ficha faltando no Google. Vinculá-la só
# esbarraria no índice único, e criá-la duplicaria a pessoa lá. São 1.195 das
# 1.425 (medido em 07/08): sem este recorte o backfill gastaria 85% do lote
# batendo em duplicata interna e devolveria um relatório de erro que parece
# defeito e é achado. O merge é decisão do Renato ([[reference_contact_merge_fix]]:
# migrar FKs antes do DELETE), nunca efeito colateral de um backfill.
SQL_CANDIDATAS = """
    WITH tel AS (
        SELECT c.id, c.google_contact_id,
               right(regexp_replace(t->>'number', '[^0-9]', '', 'g'), 8) AS k,
               length(regexp_replace(t->>'number', '[^0-9]', '', 'g')) AS n
          FROM contacts c, jsonb_array_elements(c.telefones) t
         WHERE c.telefones IS NOT NULL AND jsonb_typeof(c.telefones) = 'array'
    ),
    ja_no_google AS (
        SELECT DISTINCT k FROM tel WHERE google_contact_id IS NOT NULL AND n >= 10
    )
    SELECT c.id, c.nome, c.telefones, c.emails, c.empresa, c.cargo, c.contexto, c.criado_em
      FROM contacts c
     WHERE c.google_contact_id IS NULL
       AND c.telefones IS NOT NULL AND jsonb_typeof(c.telefones) = 'array'
       AND jsonb_array_length(c.telefones) > 0
       AND c.nome IS NOT NULL AND c.nome NOT LIKE 'Desconhecido%%'
       AND NOT EXISTS (
             SELECT 1 FROM tel t JOIN ja_no_google g ON g.k = t.k
              WHERE t.id = c.id AND t.n >= 10
       )
     ORDER BY c.criado_em DESC
"""

# As que o recorte acima deixa de fora, pra reportar em vez de esconder.
SQL_DUPLICATAS = """
    WITH tel AS (
        SELECT c.id, c.google_contact_id,
               right(regexp_replace(t->>'number', '[^0-9]', '', 'g'), 8) AS k,
               length(regexp_replace(t->>'number', '[^0-9]', '', 'g')) AS n
          FROM contacts c, jsonb_array_elements(c.telefones) t
         WHERE c.telefones IS NOT NULL AND jsonb_typeof(c.telefones) = 'array'
           AND c.nome IS NOT NULL AND c.nome NOT LIKE 'Desconhecido%%'
    ),
    ja_no_google AS (
        SELECT DISTINCT k FROM tel WHERE google_contact_id IS NOT NULL AND n >= 10
    )
    SELECT count(DISTINCT t.id) AS n
      FROM tel t JOIN ja_no_google g ON g.k = t.k
     WHERE t.google_contact_id IS NULL AND t.n >= 10
"""


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def conectar():
    """`keepalives` porque o lote é longo: sem eles o Neon derruba o socket e o
    script fica vivo sem trabalhar — processo vivo ≠ processo trabalhando.

    `autocommit` porque UMA falha não pode contaminar o lote. Na primeira
    execução (07/08, interrompida) duas fichas do INTEL casaram com o MESMO
    contato do Google e o segundo UPDATE bateu no índice único de
    `google_contact_id`. Com transação explícita, o erro deixa a conexão em
    `idle in transaction (aborted)` e **todo comando seguinte falha** — o lote
    inteiro vira erro por causa de uma linha, e ainda por cima em silêncio: o
    contador de sucessos já era > 0, então a guarda de aborto nunca disparou.
    Em autocommit cada UPDATE é a própria transação e a colisão só afeta a si
    mesma."""
    conn = psycopg2.connect(
        env("DATABASE_URL"), keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
        cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="escreve de verdade")
    ap.add_argument("--limite", type=int, default=0, help="0 = todas")
    args = ap.parse_args()

    os.environ.setdefault("DATABASE_URL", env("DATABASE_URL"))
    os.environ.setdefault("DB_TARGET", "prod")
    os.environ.setdefault("ALLOW_PROD_FROM_LOCAL", "1")

    import integrations.google_contacts as gc
    from services.google_push import _chave_tel, indice_telefones_todas

    conn = conectar()
    cur = conn.cursor()
    cur.execute(SQL_CANDIDATAS)
    fichas = [dict(r) for r in cur.fetchall()]
    if args.limite:
        fichas = fichas[:args.limite]
    cur.execute(SQL_DUPLICATAS)
    dups = cur.fetchone()["n"]
    print(f"candidatas: {len(fichas)}")
    print(f"fora do lote: {dups} órfãs que são duplicata interna (a pessoa já tem "
          f"outra ficha vinculada) — merge é decisão do Renato, não deste script")

    token = await gc.get_valid_token(CONTA)
    if not token:
        print(f"✗ sem token para {CONTA} — nada feito")
        return 1

    # Sem índice não se cria nada. Se a listagem falhar, o RuntimeError sobe e o
    # script morre aqui de propósito: é o ponto em que abortar é o certo.
    # AS DUAS AGENDAS: a primeira execução indexou só a profissional e criou 53
    # contatos que já estavam na pessoal — 53 de 53.
    idx = await indice_telefones_todas()
    print(f"índice do Google (todas as agendas): {len(idx)} telefones\n")

    contas = {"vinculada": 0, "criada": 0, "criaria": 0, "vincularia": 0,
              "sem_telefone_dedupavel": 0, "colisao_outra_ficha": 0, "erro": 0}
    erros = []
    seguidos = 0          # erros CONSECUTIVOS — ver a guarda no fim do laço

    for i, f in enumerate(fichas, 1):
        numeros = [t.get("number") for t in (f["telefones"] or [])
                   if isinstance(t, dict) and t.get("number")]
        chaves = [k for k in (_chave_tel(n) for n in numeros) if k]

        if not chaves:
            contas["sem_telefone_dedupavel"] += 1
            continue

        achado = next((idx[k] for k in chaves if k in idx), None)

        try:
            if achado:
                rid = (achado.get("resourceName") or "").replace("people/", "")
                if not rid:
                    contas["erro"] += 1
                    seguidos += 1
                    erros.append((f["id"], f["nome"], "achado sem resourceName"))
                    continue
                if args.aplicar:
                    try:
                        cur.execute("""UPDATE contacts SET google_contact_id = %s, atualizado_em = now()
                                        WHERE id = %s AND google_contact_id IS NULL""", (rid, f["id"]))
                        contas["vinculada"] += 1
                    except psycopg2.errors.UniqueViolation:
                        # Outra ficha do INTEL já aponta pra este contato do
                        # Google: as duas são a MESMA pessoa cadastrada duas
                        # vezes aqui. Não é falha do backfill — é duplicata
                        # interna que ele acabou de revelar, e o índice único é
                        # melhor detector disso que o dedup por telefone
                        # ([[project_dev_backlog]], 29 duplicatas em 07/08).
                        # Merge é decisão do Renato, nunca efeito colateral.
                        contas["colisao_outra_ficha"] += 1
                        erros.append((f["id"], f["nome"], f"já vinculado a outra ficha: {rid}"))
                else:
                    contas["vincularia"] += 1
            else:
                if args.aplicar:
                    novo = await gc.create_google_contact(token, f)
                    if not novo:
                        contas["erro"] += 1
                        seguidos += 1
                        erros.append((f["id"], f["nome"], "create_google_contact devolveu None"))
                        # 20 destes seguidos é a API dizendo não — parar aqui
                        # também, senão o lote insiste 1.400 vezes contra uma
                        # porta fechada.
                        if seguidos >= 20:
                            print("✗ 20 falhas seguidas ao criar — abortando", flush=True)
                            break
                        continue
                    cur.execute("""UPDATE contacts SET google_contact_id = %s, atualizado_em = now()
                                    WHERE id = %s""", (novo, f["id"]))
                    contas["criada"] += 1
                    print(f"  + criado no Google: {str(f['nome'])[:40]}", flush=True)
                    # O novo contato entra no índice: duas fichas do INTEL com o
                    # mesmo telefone (existem) criariam DOIS contatos no Google
                    # sem isto — a duplicata nascendo dentro do próprio backfill.
                    for k in chaves:
                        idx.setdefault(k, {"resourceName": f"people/{novo}"})
                else:
                    contas["criaria"] += 1
            seguidos = 0
        except Exception as e:
            contas["erro"] += 1
            seguidos += 1
            erros.append((f["id"], f["nome"], str(e)[:120]))
            # 20 erros CONSECUTIVOS derrubam o lote: aí o problema não é a
            # ficha, é o Google ou o banco. A versão anterior exigia "nenhum
            # sucesso até agora" e por isso NUNCA abortou — 16 vinculações no
            # começo bastaram pra desarmar a guarda enquanto o resto do lote
            # falhava em silêncio. Guarda que depende do placar acumulado não
            # protege de falha que começa no meio.
            if seguidos >= 20:
                print(f"✗ 20 erros seguidos (último: {str(e)[:90]}) — abortando", flush=True)
                break

        if args.aplicar and i % 100 == 0:
            print(f"  ... {i}/{len(fichas)}  {contas}", flush=True)

    print(f"\n{'APLICADO' if args.aplicar else 'DRY-RUN (nada foi escrito)'}:")
    for k, v in contas.items():
        if v:
            print(f"  {k:26} {v}")
    if erros:
        print(f"\n  primeiros erros ({len(erros)}):")
        for cid, nome, e in erros[:8]:
            print(f"    #{cid} {str(nome)[:34]:36} {e}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
