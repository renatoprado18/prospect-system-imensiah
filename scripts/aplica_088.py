#!/usr/bin/env python3
"""Aplica a 088 no INTEL e propaga o que o ConselhoOS já relatou.

POR QUE UM SCRIPT e não `psql < 088.sql`. O backfill tem duas partes e só uma
cabe num arquivo SQL. A parte (b) — "concluído há mais de 7 dias conta como já
relatado" — é uma decisão por data e roda contra um banco só. A parte (a) é a
que precisa das DUAS conexões: o ConselhoOS tem 10 itens com
`concluido_relatado_em` preenchido (medido 27/10) e essa informação viaja pelo
ponteiro `conselhoos_raci_id` da 084. Re-relatar ao cliente o que ele já viu é
o erro mais visível que esta migração poderia cometer, e é justamente o que a
parte (a) evita.

A ORDEM IMPORTA: (a) antes de (b). A parte (a) carimba a data REAL em que o item
saiu num relatório; a (b) é um default seguro por idade. Rodando (b) primeiro,
os 10 itens de (a) já estariam carimbados com a data de fechamento e a
informação precisa seria perdida — sem erro nenhum, só menos exata.

O TESTE DEPOIS DA APLICAÇÃO NÃO É ENFEITE. Coluna criada não prova que o filtro
do relatório funciona: prova que o ALTER rodou. Aqui o controle positivo é um
item de teste que percorre o ciclo real — concluído e não relatado APARECE,
depois de `mark_reported` DESAPARECE — com a transação revertida no fim. Sem
isso, "aplicado com sucesso" significa apenas que o SQL não deu erro
([[feedback_controle_positivo_pega_o_furo_real]]).

Uso:
  set -a && source .env && set +a && \\
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/aplica_088.py [--dry-run]
"""
import argparse
import os
import pathlib
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SQL = (RAIZ / "scripts" / "migrations" / "088_raci_concluido_relatado.sql").read_text(
    encoding="utf-8"
)


def _url(nome: str) -> str:
    v = (os.getenv(nome) or "").strip()
    if not v:
        print(f"❌ {nome} ausente ou vazia")
        sys.exit(1)
    return v


def coluna_existe(cur) -> bool:
    cur.execute(
        """SELECT 1 FROM information_schema.columns
            WHERE table_name = 'raci_itens' AND column_name = 'concluido_relatado_em'"""
    )
    return cur.fetchone() is not None


def propaga_relatados(intel_cur, cos_cur) -> int:
    """Parte (a): o que o ConselhoOS já relatou, pelo ponteiro da 084."""
    cos_cur.execute(
        """SELECT id, concluido_relatado_em FROM raci_itens
            WHERE concluido_relatado_em IS NOT NULL"""
    )
    pares = [(r["id"], r["concluido_relatado_em"]) for r in cos_cur.fetchall()]
    if not pares:
        print("   (a) ConselhoOS não tem nenhum item marcado como relatado — nada a propagar")
        return 0

    n = 0
    for uuid_item, quando in pares:
        intel_cur.execute(
            """UPDATE raci_itens SET concluido_relatado_em = %s
                WHERE conselhoos_raci_id = %s
                  AND status = 'concluido'
                  AND concluido_relatado_em IS NULL""",
            (quando, uuid_item),
        )
        n += intel_cur.rowcount
    print(f"   (a) {len(pares)} relatados no ConselhoOS → {n} propagados no INTEL")
    return n


def testa_filtro(cur) -> list:
    """Controle positivo: o filtro do relatório de fato esconde o já-relatado."""
    falhas = []
    cur.execute("SELECT id FROM projects ORDER BY id LIMIT 1")
    proj = cur.fetchone()
    if not proj:
        return ["sem projeto para o teste — filtro NÃO verificado"]
    pid = proj["id"]

    cur.execute(
        """INSERT INTO raci_itens (project_id, acao, status, origem, concluido_em,
                                   sem_par_declarado_em)
           VALUES (%s, '[teste 088] apagar se aparecer', 'concluido', 'manual',
                   NOW(), NOW())
           RETURNING id""",
        (pid,),
    )
    item_id = cur.fetchone()["id"]

    def visivel() -> bool:
        cur.execute(
            """SELECT 1 FROM raci_itens
                WHERE id = %s
                  AND (status != 'concluido' OR concluido_relatado_em IS NULL)""",
            (item_id,),
        )
        return cur.fetchone() is not None

    if not visivel():
        falhas.append("concluído e NÃO relatado não apareceu — o relatório nunca o mostraria")

    cur.execute(
        """UPDATE raci_itens SET concluido_relatado_em = NOW()
            WHERE project_id = %s AND status = 'concluido'
              AND concluido_relatado_em IS NULL""",
        (pid,),
    )
    if visivel():
        falhas.append("após marcar como relatado ele CONTINUOU aparecendo — "
                      "a seção Concluídas vira lista histórica")
    return falhas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="roda tudo e dá ROLLBACK no fim")
    args = ap.parse_args()

    intel = psycopg2.connect(_url("DATABASE_URL"))
    cos = psycopg2.connect(_url("CONSELHOOS_DATABASE_URL"))
    intel.autocommit = False
    icur = intel.cursor(cursor_factory=RealDictCursor)
    ccur = cos.cursor(cursor_factory=RealDictCursor)

    try:
        print(f"→ INTEL: coluna já existe? {coluna_existe(icur)}")

        # 1. DDL + parte (b) do backfill vêm do arquivo SQL. O `IF NOT EXISTS`
        #    torna a reaplicação inofensiva; a parte (b) é idempotente pelo
        #    `concluido_relatado_em IS NULL`.
        #    ⚠️ Executado ANTES de (a) só no que é DDL não dá — o arquivo tem os
        #    dois. Então (a) roda depois e o `IS NULL` dela deixaria de casar nos
        #    itens que (b) já carimbou. Por isso (a) vem primeiro, contra a
        #    coluna recém-criada: o DDL é separado aqui.
        icur.execute("ALTER TABLE raci_itens "
                     "ADD COLUMN IF NOT EXISTS concluido_relatado_em TIMESTAMP")
        icur.execute(
            """CREATE INDEX IF NOT EXISTS idx_raci_concluido_nao_relatado
                 ON raci_itens (project_id)
                WHERE status = 'concluido' AND concluido_relatado_em IS NULL"""
        )
        print("→ DDL aplicado (coluna + índice parcial)")

        propaga_relatados(icur, ccur)

        icur.execute(
            """UPDATE raci_itens
                  SET concluido_relatado_em = COALESCE(concluido_em, NOW())
                WHERE status = 'concluido'
                  AND concluido_relatado_em IS NULL
                  AND concluido_em IS NOT NULL
                  AND concluido_em < (NOW() - INTERVAL '7 days')"""
        )
        print(f"   (b) {icur.rowcount} concluídos há +7d carimbados por data")

        icur.execute(
            """SELECT count(*) AS abertos_na_janela FROM raci_itens
                WHERE status = 'concluido' AND concluido_relatado_em IS NULL"""
        )
        janela = icur.fetchone()["abertos_na_janela"]
        print(f"→ ficam {janela} concluídos para o PRÓXIMO relatório "
              "(fecharam dentro da janela de 7 dias — é o que tem de sair)")

        falhas = testa_filtro(icur)
        if falhas:
            print("\n❌ CONTROLE POSITIVO FALHOU:")
            for f in falhas:
                print(f"   · {f}")
            intel.rollback()
            return 1
        print("→ controle positivo OK: aparece quando não relatado, some depois de marcado")

        # O item de teste e o UPDATE do teste NUNCA ficam. Só o backfill real
        # sobrevive, e por isso ele é refeito numa transação limpa.
        intel.rollback()

        if args.dry_run:
            print("\n🟡 DRY-RUN: nada persistido.")
            return 0

        icur.execute("ALTER TABLE raci_itens "
                     "ADD COLUMN IF NOT EXISTS concluido_relatado_em TIMESTAMP")
        icur.execute(
            """CREATE INDEX IF NOT EXISTS idx_raci_concluido_nao_relatado
                 ON raci_itens (project_id)
                WHERE status = 'concluido' AND concluido_relatado_em IS NULL"""
        )
        a = propaga_relatados(icur, ccur)
        icur.execute(
            """UPDATE raci_itens
                  SET concluido_relatado_em = COALESCE(concluido_em, NOW())
                WHERE status = 'concluido'
                  AND concluido_relatado_em IS NULL
                  AND concluido_em IS NOT NULL
                  AND concluido_em < (NOW() - INTERVAL '7 days')"""
        )
        b = icur.rowcount
        intel.commit()
        print(f"\n✅ 088 aplicada. Backfill: {a} por ponteiro + {b} por data.")
        print("   Reversível: DROP COLUMN raci_itens.concluido_relatado_em;")
        return 0
    except Exception as e:
        intel.rollback()
        print(f"\n❌ {type(e).__name__}: {e}")
        return 1
    finally:
        intel.close()
        cos.close()


if __name__ == "__main__":
    sys.exit(main())
