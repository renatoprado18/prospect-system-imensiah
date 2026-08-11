#!/usr/bin/env python3
"""Aplica a 073 nos DOIS bancos e prova que o trigger funciona em cada um.

POR QUE UM SCRIPT, e não `psql < arquivo.sql` duas vezes. A 073 é a primeira
migration que atravessa os dois Neons — INTEL (us-east) e ConselhoOS (sa-east) —
e eles divergem no que importa para ela: a PK é integer de um lado e uuid do
outro, e `status` é `text` num e enum no outro. Aplicar na mão convida a rodar
num banco e esquecer o outro, que é exatamente como a métrica nasceria pela
metade sem ninguém perceber ([[feedback_intel_conselhoos_sync_lacuna]]).

O TESTE DEPOIS DA APLICAÇÃO NÃO É ENFEITE. Coluna criada não prova trigger
disparando: prova que o ALTER rodou. Aqui cada banco recebe um item de teste que
percorre o ciclo real — nasce pendente, vira concluído, é reaberto — e a
transação é revertida no fim. Sem esse controle positivo, "aplicado com sucesso"
significaria apenas que o SQL não deu erro ([[feedback_controle_positivo_pega_o_furo_real]]).

Uso:  set -a && source .env && set +a && python3 scripts/aplica_073.py [--dry-run]
"""
import os
import sys
import pathlib

import psycopg2
from psycopg2.extras import RealDictCursor

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SQL = (RAIZ / "scripts" / "migrations" / "073_raci_concluido_em.sql").read_text(encoding="utf-8")

BANCOS = [("INTEL", "DATABASE_URL"), ("ConselhoOS", "CONSELHOOS_DATABASE_URL")]


def _projeto_para_teste(cur, banco: str):
    """Um alvo válido para a FK, sem criar lixo — a transação é revertida."""
    if banco == "INTEL":
        cur.execute("SELECT id FROM projects ORDER BY id LIMIT 1")
        r = cur.fetchone()
        return ("project_id", r["id"]) if r else None
    cur.execute("SELECT empresa_id FROM raci_itens LIMIT 1")
    r = cur.fetchone()
    return ("empresa_id", r["empresa_id"]) if r else None


def testa_trigger(conn, banco: str) -> list:
    """Percorre o ciclo real do item. Devolve as falhas encontradas."""
    falhas = []
    cur = conn.cursor(cursor_factory=RealDictCursor)
    alvo = _projeto_para_teste(cur, banco)
    if not alvo:
        return [f"{banco}: sem linha de referência para o teste — trigger NÃO verificado"]
    col, val = alvo

    cur.execute("SAVEPOINT teste_073")
    try:
        cur.execute(
            f"""INSERT INTO raci_itens ({col}, area, acao, prazo, status)
                VALUES (%s, 'TESTE', 'item de verificação da 073', CURRENT_DATE, 'pendente')
                RETURNING id, concluido_em""", (val,))
        item = cur.fetchone()
        if item["concluido_em"] is not None:
            falhas.append(f"{banco}: item PENDENTE nasceu com concluido_em preenchido")

        cur.execute("UPDATE raci_itens SET status='concluido' WHERE id=%s "
                    "RETURNING concluido_em, concluido_em_fonte", (item["id"],))
        r = cur.fetchone()
        if r["concluido_em"] is None:
            falhas.append(f"{banco}: virou concluído e o trigger NÃO carimbou a data")
        elif r["concluido_em_fonte"] != "gatilho":
            falhas.append(f"{banco}: fonte gravada como {r['concluido_em_fonte']!r}, esperava 'gatilho'")

        # Editar sem mexer no status não pode reescrever a data — é o caso do
        # sync que regrava a linha inteira a cada rodada.
        antes = r["concluido_em"]
        cur.execute("UPDATE raci_itens SET notas='mexi na nota' WHERE id=%s "
                    "RETURNING concluido_em", (item["id"],))
        if cur.fetchone()["concluido_em"] != antes:
            falhas.append(f"{banco}: editar a nota REESCREVEU a data de conclusão")

        cur.execute("UPDATE raci_itens SET status='pendente' WHERE id=%s "
                    "RETURNING concluido_em", (item["id"],))
        if cur.fetchone()["concluido_em"] is not None:
            falhas.append(f"{banco}: item REABERTO manteve a data — 'fechou no prazo' ficaria falso")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT teste_073")
    return falhas


def main() -> int:
    dry = "--dry-run" in sys.argv
    problemas = []

    for banco, env in BANCOS:
        url = (os.getenv(env) or "").strip()
        if not url:
            problemas.append(f"{banco}: {env} ausente — migration NÃO aplicada neste banco")
            print(f"❌ {banco}: {env} ausente")
            continue

        print(f"\n═══ {banco} · {url.split('@')[-1][:46]}")
        conn = psycopg2.connect(url, keepalives=1)
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            if dry:
                cur.execute("""SELECT count(*) n FROM information_schema.columns
                                WHERE table_name='raci_itens' AND column_name='concluido_em'""")
                print(f"   [dry-run] concluido_em já existe: {cur.fetchone()['n'] == 1}")
                continue

            cur.execute(SQL)
            conn.commit()
            print("   SQL aplicado")

            falhas = testa_trigger(conn, banco)
            conn.commit()
            if falhas:
                problemas += falhas
                for f in falhas:
                    print(f"   ❌ {f}")
            else:
                print("   ✅ trigger verificado: carimba, preserva na edição, zera na reabertura")

            cur.execute("""SELECT count(*) FILTER (WHERE status::text='concluido') concluidos,
                                  count(*) FILTER (WHERE concluido_em IS NOT NULL) com_data,
                                  count(*) FILTER (WHERE concluido_em_fonte='relato') via_relato
                             FROM raci_itens""")
            r = cur.fetchone()
            cob = (r["com_data"] / r["concluidos"] * 100) if r["concluidos"] else 0
            print(f"   cobertura: {r['com_data']}/{r['concluidos']} concluídos com data "
                  f"({cob:.0f}%) · {r['via_relato']} vieram do backfill por relato")
        finally:
            conn.close()

    print()
    if problemas:
        print("╔═ A 073 NÃO ESTÁ COMPLETA ═╗")
        for p in problemas:
            print(f"  ❌ {p}")
        return 1
    print("🟢 073 aplicada e verificada nos dois bancos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
