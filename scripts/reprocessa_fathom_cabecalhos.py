#!/usr/bin/env python3
"""Reprocessa reunioes do Fathom afetadas pelo defeito do cabecalho de dono.

DRY-RUN POR PADRAO. Sem `--aplicar` nao escreve nada.

POR QUE UM SCRIPT E NAO O `/api/fathom/sync` (31/08/26): o sync so alcanca 48h,
e as reunioes afetadas sao de 25/08 e 29/08. Pior — reprocessar cegamente
DUPLICARIA: o dedup do importador casa por (source_id, titulo), e o conserto
MUDA o titulo dos itens que ficavam sob um cabecalho (ganham o prefixo do dono,
`Sandra: enviar a DRE`). O que ja nasceu sem prefixo nao casaria com o novo, e a
mesma tarefa entraria duas vezes.

Entao este script compara: o que o parser NOVO produz x o que ja existe no banco
pra aquele recording_id, e propoe **so o que falta** — que e exatamente o item
que sumiu pelo cap de 10. Cabecalho antigo (task fantasma ainda pending) sai na
lista de CANCELAR, nunca deletado.

Uso:
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/reprocessa_fathom_cabecalhos.py
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/reprocessa_fathom_cabecalhos.py --aplicar
"""
import argparse
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

# Recordings afetados, apurados em 31/08 pelas tasks-cabecalho ainda no banco.
RECORDINGS = [176475053, 178044531]


async def _carrega(rec_id):
    from integrations.fathom import FathomIntegration, _adapt_meeting_to_summary
    # Tenta as duas contas: a reuniao pode ter sido gravada em qualquer uma, e
    # "nao achei na profissional" nao e "nao existe".
    for conta in ("profissional", "pessoal", None):
        try:
            client = FathomIntegration(account=conta)
        except Exception:
            continue
        if not client.api_key:
            continue
        try:
            det = await client.get_meeting_details(rec_id)
        except Exception as e:
            print(f"  ⚠️  conta={conta}: {e.__class__.__name__}")
            continue
        if det:
            return _adapt_meeting_to_summary(det)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true",
                    help="escreve no banco (default: dry-run)")
    args = ap.parse_args()

    from database import get_db
    from integrations.fathom import separar_cabecalhos, _proximos_passos_do_resumo

    print("╔═ REPROCESSAMENTO FATHOM — cabecalho de dono ═╗")
    print(f"  modo: {'APLICAR (escreve)' if args.aplicar else 'DRY-RUN (nao escreve)'}\n")

    total_novas = total_cancelar = 0
    for rec_id in RECORDINGS:
        print(f"── recording {rec_id}")
        adapted = asyncio.run(_carrega(rec_id))
        if not adapted:
            print("   ⚠️  nao consegui carregar do Fathom — PULANDO (nao concluir "
                  "'sem itens' de uma leitura que falhou)\n")
            continue

        brutos = adapted.get("action_items") or []
        origem = "action_items"
        if not brutos:
            brutos = _proximos_passos_do_resumo(adapted.get("summary") or "")
            origem = "proximos_passos_md"
        itens, cabecalhos = separar_cabecalhos(brutos)
        print(f"   origem={origem} brutos={len(brutos)} itens={len(itens)} "
              f"cabecalhos={len(cabecalhos)}")
        if cabecalhos:
            print(f"   cabecalhos: {cabecalhos}")
        if len(itens) > 10:
            print(f"   ⚠️  {len(itens) - 10} item(ns) AINDA acima do cap de 10")

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, titulo, status FROM tasks "
                "WHERE source_table='fathom' AND source_id=%s", (rec_id,))
            existentes = [dict(r) for r in cur.fetchall()]

        # Fantasmas ainda vivos
        fantasmas = [t for t in existentes
                     if t["titulo"].strip().endswith(":")
                     and t["status"] not in ("cancelled", "completed")]
        # CASAR POR TEXTO NAO SOBREVIVE A TITULO REESCRITO — e a CoS reescreve.
        # Medido em 31/08 no recording 176475053: os 4 itens ja existiam como
        # `[Assespro #12] ELIANE: concluir o diagnostico entrevistando 6
        # associados NAO diretores` etc., um deles ja `completed`. A comparacao
        # literal os deu como inexistentes e o script propos CRIAR os 4 —
        # duplicaria trabalho feito, e ressuscitaria uma tarefa concluida.
        #
        # Entao a regra e de CONTAGEM, nao de texto, e o criterio e conservador:
        # so ha lacuna se o recording tem MENOS tasks reais que itens. Empate ou
        # sobra => alguem ja tratou; o script mostra e NAO propoe nada. Um
        # reprocessador que erra pra mais e pior que um que erra pra menos.
        reais = [t for t in existentes if not t["titulo"].strip().endswith(":")]
        lacuna = max(0, len(itens[:10]) - len(reais))

        print(f"   tasks no banco: {len(existentes)} ({len(reais)} reais) | "
              f"fantasmas vivos: {len(fantasmas)} | lacuna: {lacuna}")
        for t in fantasmas:
            print(f"     ✖ CANCELAR #{t['id']} {t['titulo']!r}")
        if lacuna:
            print(f"     ⚠️  {lacuna} item(ns) do recap sem task correspondente. "
                  f"NAO crio automaticamente — confira lado a lado:")
            for it in itens[:10]:
                print(f"        recap: [{it.get('_dono_da_secao') or '-'}] "
                      f"{(it.get('description') or '')[:80]!r}")
            for t in reais:
                print(f"        banco: #{t['id']} {t['titulo'][:80]!r}")
        elif len(reais) >= len(itens[:10]):
            print("     ✅ todos os itens do recap ja tem task (podem estar "
                  "reescritas) — nada a criar")
        total_novas += lacuna
        total_cancelar += len(fantasmas)

        # `--aplicar` SO CANCELA FANTASMA. Criar task fica de fora de proposito:
        # sem casamento confiavel (ver acima), inserir em lote produziria
        # duplicata e ressuscitaria concluida. A lacuna, quando houver, sai
        # impressa pra decisao humana.
        if args.aplicar and fantasmas:
            with get_db() as conn:
                cur = conn.cursor()
                for t in fantasmas:
                    cur.execute(
                        "UPDATE tasks SET status='cancelled', "
                        "descricao = COALESCE(descricao,'') || %s, "
                        "atualizado_em=NOW() WHERE id=%s",
                        ("\n\n[31/08 reprocessamento] Task fantasma do cabecalho "
                         "de dono; importador corrigido em 6d01bd4.", t["id"]))
                    print(f"     ✅ cancelada #{t['id']}")
                conn.commit()
        print()

    print(f"═ TOTAL: {total_cancelar} a cancelar · {total_novas} a criar")
    if not args.aplicar:
        print("  (dry-run — rode com --aplicar para escrever)")


if __name__ == "__main__":
    main()
