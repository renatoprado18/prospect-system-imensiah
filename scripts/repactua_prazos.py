#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Repactua o PRAZO das tarefas vivas — o gesto que o `triagem_prazos.py` só mediu.

POR QUE ESTE SCRIPT MEXE SÓ NA DATA. O passo 1 (23/11/26) mediu: das 381
atrasadas, **204 estão VIVAS** (movimento ≤14d) e exatamente **UMA** aponta para
frente encerrada. Não havia entulho a limpar; o que morreu foi o prazo, nunca
repactuado. Logo o alívio vem de **remarcar as 204**, não de arquivar as 155 —
e a assimetria é de risco, não de gosto: **mexer em data é reversível e não perde
informação; arquivar perde.** Nenhuma tarefa é fechada, cancelada ou apagada aqui.

🔴 ELE NÃO TOCA O BALDE `sem_sinal`, NEM O `nao_classificavel`. O 🟡 é um TETO,
não um veredito: só 8 de 55 frentes ativas têm nota ≤14d, porque o trabalho do
Renato é registrado nos boards de memória e no WhatsApp, não em `project_notes` —
o medidor subestima movimento. E o 🔵 é imensurável por falta de âncora. Agir
sobre qualquer um dos dois seria agir sobre a própria cegueira.

AS TRÊS ARMADILHAS DO BANCO, e como cada uma é tratada:

1. **`atualizado_em` NÃO é cosmético — é a guarda contra o pull.**
   `tasks_sync._update_from_google` tem `if local_atualizado > local_synced: return
   "skipped"`. Mudar `data_vencimento` SEM carimbar `atualizado_em=NOW()` deixa a
   data ser **sobrescrita de volta** pelo próximo pull do Google. Mesma classe do
   defeito de 22/11, em que o pull regravou por cima o texto que o push cortara.
2. **`sync_status='pending_push'`** — é o que `push_all_pending` seleciona
   (`WHERE sync_status IN ('local_only','pending_push')`). Sem isso a data nova
   fica só no INTEL e o celular dele segue mostrando a antiga.
3. **NÃO CRIAR UM PICO NOVO.** Jogar 204 tarefas para "hoje+30d" recria o
   problema 30 dias depois — e já há precedente no banco: 33 tarefas vencem todas
   em 28/08, 30 em 25/08. Por isso as datas são **escalonadas por FRENTE**, em
   round-robin sobre as semanas: tarefas da mesma frente vencem juntas (é como as
   mesas acontecem) e as frentes se espalham.

DRY-RUN É O DEFAULT. Sem `--aplicar` nada é escrito. Cada tarefa alterada ganha
`agent_actions` com `undo_hint` que restaura a data EXATA anterior, e o `desfaz N`
do WhatsApp já sabe executá-lo.

⚠️ DÉBITO CONHECIDO, declarado porque me afeta: carimbar `atualizado_em` faz a
tarefa marcar `mov_proprio=True` na triagem até o próximo push mover
`last_synced_at`. Ou seja, **a repactuação se parece com movimento próprio por
alguns dias** — o medidor vê o próprio gesto. Não corrompe decisão (o balde já era
`viva`), mas se o passo 3 usar `mov_proprio` para medir adoção, é preciso excluir
o que tem `agent_actions.action_type='task_rescheduled'` desta `source`.

Uso:  DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 scripts/repactua_prazos.py
      ...                                     --limite 25          # lote menor
      ...                                     --task 999653        # uma só
      ...                                     --aplicar            # ESCREVE
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_RAIZ, "app"))
sys.path.insert(0, os.path.join(_RAIZ, "scripts"))

from database import get_connection  # noqa: E402

# UMA RÉGUA SÓ. A classificação é importada do passo 1 em vez de reescrita: duas
# cópias da mesma regra divergem na primeira correção, e aí o gesto agiria sobre
# um balde que o relatório não mostra ([[feedback_fonte_unica_de_fatos]]).
from triagem_prazos import (  # noqa: E402
    JANELA_MOVIMENTO_D,
    SQL as SQL_TRIAGEM,
    STATUS_ABERTOS,
    STATUS_FRENTE_MORTA,
    VENCIDA_PARADA_D,
)

# Em quantas semanas espalhar as frentes. 6 porque 55 frentes ativas / 6 dá ~9
# frentes por semana, e o volume medido (204 tarefas) cai em ~34/semana — perto da
# cadência real dele e longe do pico de 33-numa-só-data que já existe no banco.
SEMANAS = 6

# Movimento nos últimos 3 dias = assunto EM CURSO. Estas não são espalhadas: vão
# para a próxima segunda. 3 dias porque cobre o fim de semana — algo que se moveu
# na sexta ainda está quente na segunda.
DIAS_QUENTE = 3

# Só o balde `viva` é elegível. Lista explícita (e não "tudo menos X") para que
# balde novo no passo 1 não passe a ser alvo por omissão.
BALDES_ELEGIVEIS = ("viva",)


def _proxima_segunda(hoje):
    """Segunda-feira seguinte. Vencimento em dia útil porque a revisão é dele, e
    tarefa que vence sábado é tarefa que nasce atrasada."""
    return hoje + timedelta(days=(7 - hoje.weekday()) or 7)


def _planejar(rows):
    """Atribui a data nova. Determinístico: mesma entrada ⇒ mesma saída.

    🔴 AS QUENTES VÊM PRIMEIRO, e esta regra nasceu de um furo real. A primeira
    versão só escalonava por frente e mandou a **#999653 (Penhora/Pitangueiras)
    para 07/12** — ela tinha mensagem do contato naquele mesmo dia e gate no dia
    seguinte, marcada 🔴🔴 no board executivo. Espalhar para não criar pico é
    correto; espalhar **ignorando urgência** esconde o que está acontecendo agora,
    e o preço é pior que o pico. `prioridade` não resolveria: vale 5 tanto na
    Pitangueiras quanto em "acompanhar lead alemã", e 98 das 381 têm o mesmo 5.
    O sinal que funciona é a RECÊNCIA do movimento.

    Depois das quentes, round-robin das FRENTES por volume (maior primeiro).
    Ordenar por volume é o que balanceia: `project_id % SEMANAS` pareceria
    equivalente e jogaria duas frentes grandes na mesma semana por azar do id.
    """
    base = _proxima_segunda(datetime.now().date())
    plano = []

    # TRÊS grupos, e o terceiro é o que me obriga a não decidir. Histórico do
    # desenho, porque cada versão errou de um jeito diferente:
    #   v1 — só escalonava por frente ⇒ mandou a Pitangueiras (gate no dia
    #        seguinte) para 07/12;
    #   v2 — "quente" por qualquer movimento ⇒ 155 das 204 quentes e o pico de
    #        volta, porque com o WhatsApp ativo todo contato falou;
    #   v3 (esta) — quente só por movimento DIRETO (tarefa/frente). E aí a
    #        Pitangueiras voltou a cair em 07/12, pois seu movimento direto é de
    #        25/09: o que a torna urgente é o gate de amanhã, que vive no BOARD
    #        EXECUTIVO e não existe em nenhuma tabela.
    # 🔑 A conclusão é que a urgência real está FORA do INTEL, e um gesto
    # automático não pode inventá-la. Então a máquina repactua onde tem sinal e
    # ABSTÉM-SE onde não tem — mas abstenção LISTADA, nunca calada, senão viraria
    # a fábrica de silêncio de [[feedback_guarda_abstencao_vira_fabrica]].
    quentes, frias, indecidiveis = [], [], []
    for r in rows:
        direto = r.get("dias_mov_direto")
        qualquer = r.get("dias_movimento")
        if direto is not None and direto <= DIAS_QUENTE:
            quentes.append(r)          # a TAREFA ou a FRENTE andou: urgência medida
        elif qualquer is not None and qualquer <= DIAS_QUENTE:
            indecidiveis.append(r)     # só o contato falou: pode ser gate ou conversa
        else:
            frias.append(r)            # o volume, e o lote seguro

    # Quente fica na PRÓXIMA SEMANA (o assunto está em curso; a data existe para
    # ser olhada, não para arquivar), mas espalhada pelos DIAS ÚTEIS dela — não
    # todas na segunda. Motivo medido: `max(data_conclusao)` da frente torna
    # quente a frente INTEIRA quando uma única tarefa dela fecha, e isso punha 96
    # tarefas no mesmo dia. Seg-sex por frente dá ~19/dia.
    quentes_por_frente = defaultdict(list)
    for r in quentes:
        quentes_por_frente[r["project_id"] or f"task:{r['id']}"].append(r)
    ordem_q = sorted(quentes_por_frente.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    for i, (_frente, tarefas) in enumerate(ordem_q):
        nova = base + timedelta(days=i % 5)   # 5 = seg a sex
        for t in tarefas:
            plano.append((t, datetime.combine(nova, datetime.min.time())))

    por_frente = defaultdict(list)
    for r in frias:
        # Sem frente, a própria task é o grupo — não há mesa que a agrupe.
        por_frente[r["project_id"] or f"task:{r['id']}"].append(r)

    ordem = sorted(por_frente.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    for i, (_frente, tarefas) in enumerate(ordem):
        # +1 semana: a primeira segunda é das quentes, e misturar as duas
        # populações ali devolveria o pico que o escalonamento evita.
        nova = base + timedelta(weeks=1 + (i % SEMANAS))
        for t in tarefas:
            plano.append((t, datetime.combine(nova, datetime.min.time())))

    return plano, indecidiveis
    # Mais vencida primeiro: se o lote for cortado por --limite, o corte leva o
    # que está pior, não o que o ORDER BY calhou de trazer.
    plano.sort(key=lambda p: -p[0]["dias_vencida"])
    return plano


def _desfazer_lote(aplicar: bool) -> int:
    """Reverte todas as repactuações pendentes, pelo caminho OFICIAL de undo.

    Usa `run_smart_undo` (o mesmo que o `desfaz N` do WhatsApp chama) em vez de
    rodar o SQL na mão: ele executa o `undo_hint` E marca a ação como `undone`.
    Escrever o UPDATE direto restauraria a data e deixaria a trilha dizendo que a
    repactuação ainda está de pé — trilha que mente é pior que trilha ausente.
    """
    from services.email_triage import run_smart_undo

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, (scope_ref->>'task_id')::int AS task_id, title,
                  payload->>'data_antiga' AS antiga, payload->>'data_nova' AS nova
             FROM agent_actions
            WHERE source = 'repactua_prazos' AND action_type = 'task_rescheduled'
              AND status = 'done' AND undo_hint IS NOT NULL
            ORDER BY id""")
    pend = [dict(r) for r in cur.fetchall()]
    conn.close()

    print(f"╔═ DESFAZER REPACTUAÇÕES — {'APLICANDO' if aplicar else 'DRY-RUN'} ═╗")
    print(f"  {len(pend)} repactuações de pé.")
    for p in pend[:10]:
        print(f"   #{p['task_id']:<8} {str(p['nova'])[:10]} → {str(p['antiga'])[:10]}  {(p['title'] or '')[:46]}")
    if len(pend) > 10:
        print(f"   … e outras {len(pend) - 10}")
    if not aplicar:
        print("\n  ⚪ DRY-RUN. Para reverter de verdade: --desfazer --aplicar")
        return 0

    ok, falhas = 0, []
    for p in pend:
        try:
            r = run_smart_undo(p["task_id"])
            if r.get("undone"):
                ok += 1
            else:
                falhas.append((p["task_id"], r.get("reason", "?")))
        except Exception as e:
            falhas.append((p["task_id"], f"{type(e).__name__}: {e}"))
    print(f"\n  ✅ {ok} revertidas.")
    if falhas:
        print(f"  🟡 {len(falhas)} não revertidas:")
        for tid, m in falhas[:10]:
            print(f"     #{tid}: {m}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="ESCREVE (default: dry-run)")
    ap.add_argument("--limite", type=int, default=0, help="máximo de tarefas (0 = todas)")
    ap.add_argument("--task", type=int, help="repactua uma só (para provar o undo)")
    # A contrapartida de um lote de 145: desfazer de uma vez. `desfaz N` no
    # WhatsApp resolve UMA tarefa, e exigir 145 comandos para reverter um gesto
    # que levou um comando não é reversibilidade, é teatro.
    ap.add_argument("--desfazer", action="store_true",
                    help="reverte TODAS as repactuações ainda não desfeitas deste script")
    a = ap.parse_args()

    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo != "prod":
        print("╔═ REPACTUAÇÃO DE PRAZOS ═╗")
        print(f"  ⚪ NADA FEITO — DB_TARGET={alvo or '(vazio)'}. As tasks de verdade estão em prod.")
        return 0

    if a.desfazer:
        return _desfazer_lote(aplicar=a.aplicar)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(SQL_TRIAGEM, {
        "janela": JANELA_MOVIMENTO_D,
        "parada": VENCIDA_PARADA_D,
        "abertos": list(STATUS_ABERTOS),
        "frente_morta": list(STATUS_FRENTE_MORTA),
    })
    rows = [dict(r) for r in cur.fetchall()]
    elegiveis = [r for r in rows if r["balde"] in BALDES_ELEGIVEIS]
    if a.task:
        elegiveis = [r for r in elegiveis if r["id"] == a.task]
        if not elegiveis:
            alvo_row = next((r for r in rows if r["id"] == a.task), None)
            print("╔═ REPACTUAÇÃO DE PRAZOS ═╗")
            print(f"  🔴 #{a.task} não é elegível"
                  + (f" — está no balde '{alvo_row['balde']}', e só {BALDES_ELEGIVEIS} entram."
                     if alvo_row else " — não está entre as atrasadas abertas."))
            conn.close()
            return 1

    plano, indecidiveis = _planejar(elegiveis)
    if a.limite:
        plano = plano[: a.limite]

    modo = "APLICANDO" if a.aplicar else "DRY-RUN (nada escrito)"
    print(f"╔═ REPACTUAÇÃO DE PRAZOS — {modo} ═╗")
    print(f"  elegíveis: {len(elegiveis)} de {len(rows)} atrasadas (só balde {BALDES_ELEGIVEIS[0]})")
    print(f"  neste lote: {len(plano)}")
    if not plano:
        print("  nada a fazer.")
        conn.close()
        return 0

    por_data = defaultdict(int)
    for _t, nova in plano:
        por_data[nova.date()] += 1
    print(f"  distribuição nas próximas {SEMANAS} semanas (evita recriar pico):")
    for d in sorted(por_data):
        print(f"    {d:%d/%m (%a)}  {por_data[d]:>3} tarefas  {'▇' * min(por_data[d], 40)}")
    print()

    for t, nova in plano[:12]:
        print(f"   #{t['id']:<8} {int(t['dias_vencida']):>4}d vencida  →  {nova:%d/%m/%Y}  "
              f"{(t['titulo'] or '')[:52]}")
    if len(plano) > 12:
        print(f"   … e outras {len(plano) - 12}")

    if indecidiveis:
        print()
        print(f"  ✋ {len(indecidiveis)} FORA DO LOTE — só o CONTATO se moveu, e isso não diz")
        print("     se é gate de amanhã ou conversa social. O INTEL não sabe, então não decido:")
        for t in sorted(indecidiveis, key=lambda r: -r["dias_vencida"])[:10]:
            print(f"       #{t['id']:<8} {int(t['dias_vencida']):>4}d  {(t['titulo'] or '')[:56]}")
        if len(indecidiveis) > 10:
            print(f"       … e outras {len(indecidiveis) - 10}")
        print("     ⇒ decida a data destas à mão (ex.: `--task <id>` não as alcança de propósito).")
        print("     🔑 Caso que ensinou: a #999653 (Pitangueiras) tem gate amanhã e movimento")
        print("        direto de 25/09 — a urgência dela vive no BOARD, não no banco.")

    google = sum(1 for t, _ in plano if t["tem_google"])
    print()
    print(f"  ⚠️ {google} das {len(plano)} vão propagar ao Google Tasks no próximo push"
          f" — aparecem no celular dele com a data nova.")

    if not a.aplicar:
        print()
        print("  ⚪ DRY-RUN: nenhum UPDATE foi executado. Para aplicar, repita com --aplicar.")
        conn.close()
        return 0

    feitas, erros = 0, []
    from services.agent_actions import log_action
    for t, nova in plano:
        antiga = t["data_vencimento"]
        try:
            # `AND data_vencimento = %s` é trava otimista: se outra sessão (ou o
            # pull do Google) mexeu na data desde a leitura, este UPDATE não casa
            # e a tarefa é contada como conflito em vez de sobrescrita cega.
            cur.execute(
                """UPDATE tasks
                      SET data_vencimento = %s,
                          atualizado_em   = NOW(),
                          sync_status     = 'pending_push'
                    WHERE id = %s
                      AND status = ANY(%s)
                      AND data_vencimento = %s""",
                (nova, t["id"], list(STATUS_ABERTOS), antiga),
            )
            if cur.rowcount != 1:
                erros.append((t["id"], "data mudou desde a leitura — não tocada"))
                conn.rollback()
                continue
            conn.commit()
        except Exception as e:
            conn.rollback()
            erros.append((t["id"], f"{type(e).__name__}: {e}"))
            continue

        log_action(
            action_type="task_rescheduled",
            category="tasks",
            title=f"Prazo repactuado: {t['titulo']}",
            details=(
                f"Vencida há {int(t['dias_vencida'])}d, mas com movimento nos últimos "
                f"{JANELA_MOVIMENTO_D}d (próprio={bool(t['mov_proprio'])}, "
                f"frente={bool(t['mov_frente'])}, contato={bool(t['mov_contato'])}). "
                f"O prazo estava errado, não a tarefa. {antiga:%d/%m/%Y} → {nova:%d/%m/%Y}."
            ),
            scope_ref={"task_id": t["id"], "contact_id": t.get("contact_id"),
                       "project_id": t.get("project_id")},
            source="repactua_prazos",
            payload={"data_antiga": antiga.isoformat(), "data_nova": nova.isoformat(),
                     "dias_vencida": int(t["dias_vencida"]), "balde": t["balde"]},
            # Restaura a data EXATA e devolve ao push, senão o undo deixaria o
            # Google com a data nova e o INTEL com a antiga.
            undo_hint=(
                f"UPDATE tasks SET data_vencimento='{antiga.isoformat()}', "
                f"atualizado_em=NOW(), sync_status='pending_push' WHERE id={t['id']}"
            ),
        )
        feitas += 1

    conn.close()
    print()
    print(f"  ✅ {feitas} repactuadas, cada uma com undo (`desfaz <id>` no WhatsApp).")
    if erros:
        print(f"  🟡 {len(erros)} não tocadas:")
        for tid, motivo in erros[:10]:
            print(f"     #{tid}: {motivo}")
    print("  ⏭️ A data nova chega ao Google no próximo `push_all_pending`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
