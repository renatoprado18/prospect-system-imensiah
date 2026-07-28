#!/usr/bin/env python3
"""
Amarra as linhas do RACI do #47 às tasks que as governam.

O seed (`seed_raci_47.py`) criou as 8 linhas SEM prazo de propósito: a nota
#268 lista um "painel de prazos" que aponta pra tasks, e adivinhar qual task
pertence a qual linha na hora da transcrição seria inventar dado.

Feito o levantamento das 14 tasks abertas do #47, o mapeamento deixou de ser
adivinhação. REGRA: cada linha aponta pra task de prazo MAIS PRÓXIMO entre as
suas — é ela que governa a data da linha. As outras tasks da mesma linha
seguem no projeto; `raci_itens.task_id` é escalar por desenho (a linha de RACI
é a responsabilidade durável, a task é o passo do momento).

Duas linhas ficam SEM prazo: Financeiro e Patrimonial não têm task aberta.
Isso é o dado real, não lacuna a preencher.

Reversível: é UPDATE de prazo/task_id, sem apagar nada.

Uso:
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/raci_47_amarra_tasks.py [--executar]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from database import get_db  # noqa: E402

# acao (prefixo) -> task que governa o prazo, + as demais tasks da linha
MAPA = {
    "Regularização contábil": (999667, [999667]),
    "CDAs federais":          (999693, [999693, 999686, 999669]),
    "Processos trabalhistas": (999685, [999685, 999666]),
    "Processos cíveis":       (999687, [999687]),
    "Incorporação / fusão":   (999665, [999665, 999695, 999688, 999670]),
    "Coordenação e cadência": (999681, [999681, 999668]),
    # Financiamento da reorg e Firewall herança/holding: sem task aberta.
}


def main():
    executar = "--executar" in sys.argv
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, area, acao, prazo, task_id FROM raci_itens
             WHERE project_id = 47 ORDER BY id
        """)
        linhas = [dict(r) for r in cursor.fetchall()]

        print(f"{'EXECUTANDO' if executar else 'DRY-RUN'}\n")
        for linha in linhas:
            chave = next((k for k in MAPA if linha["acao"].startswith(k)), None)
            if not chave:
                print(f"  {linha['area']:12} {linha['acao'][:38]:40} "
                      f"— sem task aberta, fica sem prazo")
                continue

            task_id, todas = MAPA[chave]
            cursor.execute(
                "SELECT titulo, data_vencimento FROM tasks WHERE id = %s",
                (task_id,))
            task = cursor.fetchone()
            if not task:
                print(f"  ⚠️  task #{task_id} não existe — pulando {chave}")
                continue

            outras = [t for t in todas if t != task_id]
            sufixo = f"  (+{len(outras)} task(s) na mesma linha)" if outras else ""
            prazo = task["data_vencimento"]
            print(f"  {linha['area']:12} {linha['acao'][:38]:40} -> #{task_id} "
                  f"{prazo.strftime('%d/%m') if prazo else '—'}{sufixo}")

            if executar:
                cursor.execute("""
                    UPDATE raci_itens
                       SET prazo = %s, task_id = %s,
                           atualizado_em = CURRENT_TIMESTAMP
                     WHERE id = %s
                """, (prazo, task_id, linha["id"]))
        if executar:
            conn.commit()
            print("\naplicado.")
        else:
            print("\nNada foi escrito. Para aplicar: --executar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
