#!/usr/bin/env python3
"""
Merge das fichas que se duplicaram por causa do 9º dígito da Anatel.

CONTEXTO: até 2013-2016 o celular brasileiro tinha 8 dígitos de assinante; a
Anatel prefixou um 9. `35 9985-1122` e `35 99985-1122` são A MESMA LINHA. Como
`normalize_phone` não equiparava as duas eras, o índice `by_phone` da cascata
de identidade guardava a mesma linha sob DUAS chaves — o tier telefone+nome
nunca via os dois lados juntos e o sync criava ficha nova.

A fonte fechou no commit `94b2f5f` (`canonical_br_phone` + `contact_phones`
canônico). Isto aqui é o PASSIVO.

DIFERENÇA IMPORTANTE PRO LOTE DAS FANTASMAS: lá um lado era ficha-lixo
("Desconhecido +55...") e o outro era a ficha real — a escolha do primário era
óbvia. Aqui as DUAS são fichas reais, então o merge decide qual nome, empresa
e cargo sobrevivem. Por isso:

  - só entra grupo cujos nomes passam por `names_match` (o mesmo critério da
    cascata), com `kind` do telefone;
  - grupo que NÃO passa é listado à parte, nunca mergeado em lote — é onde
    mora o caso "Pousada Tatuapara × Ayrton Kacic", que pode ser o
    estabelecimento e o dono, duas entidades no mesmo celular;
  - o primário é a ficha de MENOR id (a mais antiga, onde está o histórico
    mais longo), e `merge_contacts` escolhe o melhor nome entre as duas por
    `get_name_score` — não é o nome do primário que vence automaticamente.

Uso:
    python3 scripts/merge_nono_digito.py                 # dry-run (padrão)
    python3 scripts/merge_nono_digito.py --executar

    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/merge_nono_digito.py
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from database import get_db  # noqa: E402
from services.contact_dedup import merge_duplicate_contacts  # noqa: E402
from services.contact_identity import (  # noqa: E402
    canonical_br_phone, names_match, phone_kind,
)

CAMPOS = ("id, nome, empresa, cargo, emails, telefones, foto_url, linkedin, "
          "contexto, google_contact_id, circulo, origem")


def levantar(cursor):
    """
    Grupos de fichas que compartilham o MESMO número canônico mas foram
    gravadas em eras diferentes da numeração. Read-only.

    O filtro `tem_antigo AND tem_novo` é o que restringe ao defeito do 9º
    dígito: duas fichas com o número gravado identicamente já eram vistas pela
    cascata e não são esta classe.
    """
    cursor.execute("""
        WITH nums AS (
          SELECT c.id,
                 regexp_replace(
                   jsonb_array_elements(
                     CASE WHEN jsonb_typeof(c.telefones) = 'array'
                          THEN c.telefones ELSE '[]'::jsonb END)->>'number',
                   '[^0-9]', '', 'g') AS d
            FROM contacts c WHERE c.telefones IS NOT NULL
        ),
        canon AS (
          SELECT id, d,
                 CASE
                   WHEN length(d) = 12 AND left(d, 2) = '55'
                        AND substr(d, 5, 1) IN ('6','7','8','9')
                        THEN substr(d, 1, 4) || '9' || substr(d, 5)
                   WHEN length(d) = 10 AND substr(d, 3, 1) IN ('6','7','8','9')
                        THEN substr(d, 1, 2) || '9' || substr(d, 3)
                   ELSE d END AS c,
                 (length(d) IN (10, 12)) AS era_antiga
            FROM nums WHERE length(d) >= 10
        )
        SELECT c, array_agg(DISTINCT id ORDER BY id) AS ids
          FROM canon
         GROUP BY c
        HAVING count(DISTINCT id) > 1
           AND bool_or(era_antiga) AND bool_or(NOT era_antiga)
         ORDER BY c
    """)
    return [(r["c"], r["ids"]) for r in cursor.fetchall()]


def main():
    executar = "--executar" in sys.argv

    with get_db() as conn:
        cursor = conn.cursor()
        grupos = levantar(cursor)

        print(f"{'EXECUTANDO' if executar else 'DRY-RUN'} — "
              f"{len(grupos)} grupo(s) partidos pelo 9º dígito\n")

        aptos, barrados = [], []
        for numero, ids in grupos:
            cursor.execute(
                f"SELECT {CAMPOS} FROM contacts WHERE id = ANY(%s) ORDER BY id",
                (ids,))
            fichas = [dict(r) for r in cursor.fetchall()]
            if len(fichas) < 2:
                continue

            kind = phone_kind(numero)
            base = fichas[0]["nome"] or ""
            # Todos têm que ser a mesma pessoa que o primário. Um grupo com uma
            # ficha divergente vai inteiro pra revisão — separar o "bom" do
            # "ruim" dentro do grupo é julgamento, não regra.
            ok = all(names_match(base, f["nome"] or "", kind) for f in fichas[1:])
            (aptos if ok else barrados).append((numero, fichas))

        for numero, fichas in aptos:
            msgs = []
            for f in fichas:
                cursor.execute("""
                    SELECT count(*) n FROM messages m
                      JOIN conversations cv ON cv.id = m.conversation_id
                     WHERE cv.contact_id = %s
                """, (f["id"],))
                msgs.append(cursor.fetchone()["n"])
            alvo = fichas[0]
            outros = " + ".join(f"#{f['id']}" for f in fichas[1:])
            print(f"  {numero:14} -> #{alvo['id']:6} {(alvo['nome'] or '')[:30]:32} "
                  f"absorve {outros}   ({sum(msgs)} msg no grupo)")
            for f, n in zip(fichas, msgs):
                print(f"       #{f['id']:6} {(f['nome'] or '')[:28]:30} "
                      f"{(f['origem'] or '')[:34]:36} {n:3} msg")
            if executar:
                r = merge_duplicate_contacts(fichas, conn)
                print(f"       -> {r['status']}, nome final: "
                      f"{r['merged_contact']['nome']!r}, apagadas: {r['deleted_ids']}")
            print()

        if barrados:
            print(f"\n⚠️  {len(barrados)} grupo(s) NÃO entram — nomes não batem "
                  f"(mesmo celular, possivelmente entidades distintas):")
            for numero, fichas in barrados:
                nomes = " × ".join(f"#{f['id']} {(f['nome'] or '')[:26]}"
                                   for f in fichas)
                print(f"  {numero:14} {nomes}")
            print("  Estes exigem julgamento — decidir um a um.")

        if not executar:
            print("\nNada foi escrito. Para aplicar: --executar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
