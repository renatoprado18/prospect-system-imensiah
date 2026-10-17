#!/usr/bin/env python3
"""
Atribui contato as mensagens de grupo orfas (`group_messages.contact_id IS NULL`).

Por que existe um script alem do cron: o backfill inicial sao 1.741 remetentes
e nao cabe na janela do `run-group-messages`. O cron leva um pedaco por rodada
(ver GROUP_ATTRIB_BATCH); este roda a fila inteira de uma vez.

    # mede e NAO escreve (default)
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/group_msg_attribution.py

    # aplica
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/group_msg_attribution.py --apply

⚠️ `--apply` escreve em `group_messages.contact_id` apenas onde ja era NULL.
Reversivel: nenhuma linha e apagada e nenhuma atribuicao existente e tocada.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import psycopg2
import psycopg2.extras

from services.group_msg_attribution import (  # noqa: E402
    ATRIBUIDA,
    ATRIBUIDA_FICHA_PLACEHOLDER,
    ATRIBUIDA_NOME_DIVERGE,
    ATRIBUIDA_VIA_LID,
    PULADA_FIXO_NOME_DIVERGE,
    PULADA_LID,
    PULADA_LID_SEM_FICHA,
    PULADA_SEM_FICHA,
    atribuir_orfas,
    orfandade,
    resolver_remetente,
)

ROTULO = {
    ATRIBUIDA: "✅ celular/fixo + nome bate",
    ATRIBUIDA_NOME_DIVERGE: "⚠️ celular, nome divergente (atribui)",
    ATRIBUIDA_FICHA_PLACEHOLDER: "🟡 ficha placeholder (atribui)",
    ATRIBUIDA_VIA_LID: "🆔 LID resolvido pelo mapa (atribui)",
    PULADA_LID: "🚫 LID fora do mapa (sem grupo em comum)",
    PULADA_LID_SEM_FICHA: "⬜ LID resolvido, telefone SEM ficha",
    PULADA_SEM_FICHA: "⬜ telefone valido sem ficha",
    PULADA_FIXO_NOME_DIVERGE: "🚫 fixo, nome divergente (linha compartilhada)",
}
# Rotulo faltando sairia como a chave crua — e foi o que aconteceu na 1a rodada
# com os baldes novos do LID. Falha barata, mas o relatorio e o que o Renato le.
assert set(ROTULO) >= {ATRIBUIDA, ATRIBUIDA_NOME_DIVERGE,
                       ATRIBUIDA_FICHA_PLACEHOLDER, ATRIBUIDA_VIA_LID,
                       PULADA_LID, PULADA_LID_SEM_FICHA, PULADA_SEM_FICHA,
                       PULADA_FIXO_NOME_DIVERGE}, "balde sem rotulo"


def _conn():
    """
    Conecta pelo resolvedor do app, NUNCA por `DATABASE_URL` cru.

    ⚠️ A primeira versao deste script lia `os.environ["DATABASE_URL"]` e
    imprimia o `DB_TARGET` ao lado — com `DB_TARGET=local` ele anunciou
    `target=local` e abriu conexao no **Neon**, porque o `.env` exporta a URL de
    producao e o rotulo nao era verificado contra ela. Rotulo nao e medida
    ([[feedback_teste_com_ambiente_declarado_nao_aplicado]]). `_get_conn_string`
    resolve o alvo declarado E confere a URL resultante (`_assert_coerente`),
    que e o que recusa justamente esse caso.
    """
    from database import _get_conn_string  # noqa: PLC0415 — sys.path montado acima

    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo not in ("local", "prod"):
        sys.exit("DB_TARGET nao declarado (local|prod) — alvo se declara, "
                 "nunca se deduz. Ver reference_db_target_protocol")
    url = _get_conn_string()
    print(f"[DB] {(url.split('@')[-1].split('/')[0])[:48]} (target={alvo})")
    return psycopg2.connect(url)


def _foto(cur, titulo):
    o = orfandade(cur)
    print(f"  {titulo}: {o['com_contato']}/{o['total']} com contato "
          f"({o['pct_com_contato']}%) · orfas {o['orfas']} · "
          f"grupos zerados {o['grupos_zerados']}")
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="escreve; sem isso so mede")
    ap.add_argument("--limite", type=int, default=None,
                    help="corta a fila nos N remetentes de maior volume")
    args = ap.parse_args()

    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("╔═ ATRIBUICAO DE CONTATO EM MENSAGENS DE GRUPO ═╗")
    antes = _foto(cur, "antes ")

    if not args.apply:
        # Dry-run: mesma decisao, sem UPDATE. Nao reaproveita `atribuir_orfas`
        # de proposito — um "modo seguro" dentro da funcao que escreve e um
        # if a mais entre a medida e o efeito.
        cur.execute(
            """
            SELECT sender_phone, MAX(sender_name) AS sender_name, COUNT(*) AS n
              FROM group_messages
             WHERE contact_id IS NULL AND NOT COALESCE(from_me, FALSE)
             GROUP BY sender_phone
             ORDER BY COUNT(*) DESC
            """
            + ("" if args.limite is None else f" LIMIT {int(args.limite)}")
        )
        motivos, msgs = {}, {}
        for r in cur.fetchall():
            _, motivo = resolver_remetente(cur, r["sender_phone"], r["sender_name"])
            motivos[motivo] = motivos.get(motivo, 0) + 1
            msgs[motivo] = msgs.get(motivo, 0) + int(r["n"])
        print("\n  — SECO, nada escrito —")
        for k in sorted(msgs, key=lambda x: -msgs[x]):
            print(f"  {ROTULO.get(k, k):46} {msgs[k]:6} msgs  ({motivos[k]:4} remet.)")
        ganho = sum(v for k, v in msgs.items() if k.startswith("atribuida"))
        novo = antes["com_contato"] + ganho
        print(f"\n  ⇒ atribuiria {ganho} msgs: "
              f"{antes['pct_com_contato']}% → {round(100.0*novo/antes['total'],1)}%")
        print("\n  Rode com --apply pra escrever.")
        return

    res = atribuir_orfas(cur, limite_remetentes=args.limite,
                         commit_cb=conn.commit)
    conn.commit()

    print(f"\n  remetentes examinados: {res['remetentes_examinados']}")
    for k in sorted(res["msgs_por_motivo"], key=lambda x: -res["msgs_por_motivo"][x]):
        print(f"  {ROTULO.get(k, k):46} {res['msgs_por_motivo'][k]:6} msgs  "
              f"({res['por_motivo'][k]:4} remet.)")
    print(f"\n  ✍️  msgs atribuidas: {res['msgs_atribuidas']}")

    if res["divergencias_de_nome"]:
        print(f"\n  ⚠️ {len(res['divergencias_de_nome'])} remetentes de CELULAR "
              f"atribuidos com nome divergente — conferir:")
        for d in res["divergencias_de_nome"][:15]:
            print(f"     {d['msgs']:5} msgs | {str(d['sender_name'])[:26]:26} "
                  f"-> #{d['contact_id']}")

    depois = _foto(cur, "depois")
    # A soma por motivo tem de fechar com a fila, senao um ramo saiu calado.
    soma = sum(res["por_motivo"].values())
    if soma != res["remetentes_examinados"]:
        print(f"\n  🔴 BALDES NAO FECHAM: {soma} != {res['remetentes_examinados']}")
        sys.exit(1)
    if depois["com_contato"] < antes["com_contato"]:
        print("\n  🔴 REGRESSAO: atribuicao DESCEU")
        sys.exit(1)


if __name__ == "__main__":
    main()
