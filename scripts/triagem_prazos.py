#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Triagem de PRAZO das tarefas atrasadas — separa "o prazo morreu" de "a tarefa morreu".

POR QUE EXISTE, com o número e a data. Em 23/11/26 o Renato disse: *"tem um monte
de tarefas desatualizadas, de sistema... nem um pouco funcional"*. A medição
contrariou a premissa, e é por isso que este script classifica em vez de limpar:

  572 pendentes · 381 atrasadas (67%) · 285 vencidas há 30-90 dias
  327 das 381 (86%) sem NENHUM movimento próprio — só o sync do Google as toca

Peguei as 12 mais velhas e atrasadas das origens de automação esperando entulho.
Vieram `[Penhora #39] Alinhar estratégia com advogado` (que era o 🔴🔴 do dia
seguinte no board executivo), `[ConselhoOS #65] Preço`, `[EUDR #63] pitch deck da
AgriSpectre`, `[Reorg 7] multa da DCTF`. **Não era lixo: era trabalho vivo cujo
PRAZO morreu.** `origem=mcp_copilot` é tarefa que o próprio Renato criou
conversando, não robô. Fechar essas em lote seria apagar a agenda dele.

O QUE ESTE SCRIPT ACRESCENTA AOS DOIS MECANISMOS QUE JÁ EXISTEM. O
`task_reconciler` (ligado desde 23/08) fecha com EVIDÊNCIA de resolução — 18
fechamentos em 30 dias, conservador por desenho e certo nisso. Mas as 327 não têm
evidência nenhuma chegando: não estão resolvidas, estão **paradas**, e para elas o
reconciler não tem o que ler. A skill `/papel-cos` tria em 3 baldes, mas é manual,
usa `LIMIT 10` e a própria descrição diz *"sem auto-dismiss stale"*: mostra 10 e
omite 371. A lacuna entre os dois é exatamente "vencida, viva e sem dono de
prazo", e é só ela que este script endereça.

🔴 ESTE SCRIPT NÃO ESCREVE NADA. Nem UPDATE, nem fechamento, nem remarcação. É
deliberado e não é timidez: **100% das 640 tarefas pendentes têm
`google_task_id`**, ou seja, toda ação em lote propaga para o Google Tasks no
celular do Renato. A régua vem antes do gesto, e o gesto é dele
([[feedback_medir_antes_de_construir]]).

🔑 O BALDE MAIS IMPORTANTE É O "NÃO CLASSIFICÁVEL". Tarefa sem `project_id` e sem
`contact_id` não tem como ter movimento medido. A tentação é empurrá-la para
"parada" — e aí o lote de arquivamento levaria junto o que eu só não consegui
medir. Fonte que não pode ser medida sai como NÃO MEDIDA, nunca no balde de
descarte; é o mesmo contrato do `verifica_frescor`
([[feedback_guarda_abstencao_vira_fabrica]] é o risco oposto, e por isso o balde
aparece no relatório em vez de ficar calado).

Uso:  DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 scripts/triagem_prazos.py
      ... scripts/triagem_prazos.py --quiet            # só o placar
      ... scripts/triagem_prazos.py --balde parada     # lista um balde
      ... scripts/triagem_prazos.py --balde viva --limite 40
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from database import get_connection  # noqa: E402

# Janela que define "movimento recente". 14 dias porque a cadência de mesa/frente
# do Renato é quinzenal a mensal: 7 dias classificaria como "parada" uma frente
# que só se mexe a cada duas semanas, que é o normal dele, não a exceção.
JANELA_MOVIMENTO_D = 14

# Só acima disto "atrasada" vira "parada". Abaixo é atraso corriqueiro — e o board
# executivo repactua isso sozinho na semana.
VENCIDA_PARADA_D = 30

STATUS_ABERTOS = ("pending", "in_progress")
STATUS_FRENTE_MORTA = ("concluido", "arquivado")

# A classificação inteira em UMA query, e de propósito: cada sinal é um fato do
# banco (data > data), nunca um juízo. Sem LLM não há o que alucinar, e o
# resultado é reproduzível — duas rodadas no mesmo minuto dão o mesmo balde.
SQL = """
WITH base AS (
    SELECT t.id, t.titulo, t.origem, t.project_id, t.contact_id, t.status,
           t.data_vencimento, t.data_criacao,
           (t.google_task_id IS NOT NULL) AS tem_google,
           EXTRACT(EPOCH FROM (now() - t.data_vencimento)) / 86400 AS dias_vencida,
           -- MOVIMENTO PRÓPRIO: `atualizado_em` sozinho não serve — o sync do
           -- Google o carimba em toda run, então ele mede o nosso cron, não a
           -- tarefa. O critério é ser mais novo que o último sync.
           (t.atualizado_em IS NOT NULL
            AND t.atualizado_em > now() - make_interval(days => %(janela)s)
            AND (t.last_synced_at IS NULL OR t.atualizado_em > t.last_synced_at)
           ) AS mov_proprio,
           p.status AS frente_status,
           p.nome   AS frente_nome
      FROM tasks t
      LEFT JOIN projects p ON p.id = t.project_id
     WHERE t.status = ANY(%(abertos)s)
       AND t.data_vencimento IS NOT NULL
       AND t.data_vencimento < now()
),
sinais AS (
    SELECT b.*,
           -- MOVIMENTO DA FRENTE: nota nova na frente OU outra tarefa da mesma
           -- frente concluída na janela. As duas são prova de que o assunto anda.
           (b.project_id IS NOT NULL AND (
              EXISTS (SELECT 1 FROM project_notes n
                       WHERE n.project_id = b.project_id
                         AND n.criado_em > now() - make_interval(days => %(janela)s))
              OR EXISTS (SELECT 1 FROM tasks t2
                       WHERE t2.project_id = b.project_id
                         AND t2.data_conclusao > now() - make_interval(days => %(janela)s))
           )) AS mov_frente,
           -- MOVIMENTO DO CONTATO: qualquer canal serve; aqui o sinal é "o
           -- relacionamento está vivo", não "este assunto andou".
           (b.contact_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM messages m
               WHERE m.contact_id = b.contact_id
                 AND coalesce(m.enviado_em, m.criado_em) > now() - make_interval(days => %(janela)s)
           )) AS mov_contato,
           -- MENSURABILIDADE: sem frente e sem contato não há o que medir. Este
           -- flag é o que impede o balde de descarte de engolir o imensurável.
           (b.project_id IS NOT NULL OR b.contact_id IS NOT NULL) AS mensuravel
      FROM base b
)
SELECT id, titulo, origem, project_id, contact_id, dias_vencida, tem_google,
       mov_proprio, mov_frente, mov_contato, mensuravel, frente_status, frente_nome,
       CASE
         WHEN frente_status = ANY(%(frente_morta)s)            THEN 'fantasma'
         WHEN mov_proprio OR mov_frente OR mov_contato         THEN 'viva'
         WHEN NOT mensuravel                                   THEN 'nao_classificavel'
         WHEN dias_vencida > %(parada)s                        THEN 'sem_sinal'
         ELSE 'recente'
       END AS balde
  FROM sinais
 ORDER BY dias_vencida DESC
"""

BALDES = {
    "fantasma": ("⚪", "FANTASMA — a frente está concluída/arquivada",
                 "a tarefa aponta para algo que já encerrou; é o descarte mais seguro"),
    "viva": ("🟢", "VIVA — o assunto andou nos últimos %dd" % JANELA_MOVIMENTO_D,
             "o PRAZO está errado, a tarefa não ⇒ repactuar data, nunca fechar"),
    # 🔑 "SEM SINAL", NUNCA "PARADA" — e o rótulo é o conserto de 23/11/26. Chamei
    # este balde de PARADA e o controle derrubou o nome na primeira tentativa: a
    # #999653 (Penhora/Pitangueiras), que era o 🔴🔴 do dia seguinte no board
    # executivo, só escapou dele por UM sinal (uma mensagem do contato naquele
    # dia) — sem ela, a tarefa mais urgente do Renato seria classificada como
    # parada. "Parada" afirma um fato sobre o mundo; "sem sinal" descreve o que
    # ESTE medidor conseguiu ver, que é tudo a que ele tem direito.
    "sem_sinal": ("🟡", "SEM SINAL DETECTÁVEL — vencida >%dd, nada que eu meça se moveu" % VENCIDA_PARADA_D,
                  "⚠️ TETO, não número: parte está viva FORA do INTEL ⇒ revisar, nunca arquivar em lote"),
    "recente": ("⚪", "RECENTE — vencida ≤%dd, sem movimento" % VENCIDA_PARADA_D,
                "atraso corriqueiro; o board da semana costuma absorver"),
    "nao_classificavel": ("🔵", "NÃO CLASSIFICÁVEL — sem frente e sem contato",
                          "NÃO É 'parada': é imensurável. Nunca entra em lote de descarte"),
}
ORDEM = ["sem_sinal", "viva", "fantasma", "recente", "nao_classificavel"]

# AUTOEXAME: a cobertura do sinal mais fraco. Medido em 23/11/26: só 8 de 55
# frentes ativas tinham nota ≤14d, 32 estavam com mais de 60 dias e 2 nunca
# receberam nota. O registro do trabalho do Renato vive nos boards de memória, no
# WhatsApp e no e-mail — não em `project_notes`. Então `mov_frente` subestima por
# construção, e um medidor que não declara isso vende o próprio ponto cego como
# conclusão ([[feedback_medidor_que_nao_mede_a_si_mesmo]]).
SQL_AUTOEXAME = """
SELECT count(*) FILTER (WHERE mx > now() - make_interval(days => %(janela)s)) AS frescas,
       count(*) AS ativas
  FROM (SELECT p.id, (SELECT max(n.criado_em) FROM project_notes n
                       WHERE n.project_id = p.id) AS mx
          FROM projects p WHERE p.status = 'ativo') x
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="só o placar")
    ap.add_argument("--balde", choices=list(BALDES), help="lista as tarefas de um balde")
    ap.add_argument("--limite", type=int, default=20, help="quantas listar (default 20)")
    a = ap.parse_args()

    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo != "prod":
        # Mesmo contrato do verifica_frescor: contra o local isto mediria a hora
        # do `./dev.sh sync`, e "rodou e passou" seria certificação vazia.
        print("╔═ TRIAGEM DE PRAZOS ═╗")
        print(f"  ⚪ NÃO MEDIDO — DB_TARGET={alvo or '(vazio)'}. Só tem sentido contra prod.")
        print("     Rode: DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 scripts/triagem_prazos.py")
        return 0

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(SQL, {
            "janela": JANELA_MOVIMENTO_D,
            "parada": VENCIDA_PARADA_D,
            "abertos": list(STATUS_ABERTOS),
            "frente_morta": list(STATUS_FRENTE_MORTA),
        })
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"╔═ TRIAGEM DE PRAZOS ═╗\n  🔴 SEM MEDIÇÃO: {e}")
        return 1

    # DENOMINADOR. Sem ele o placar é retórica: "285 paradas" não diz se é muito
    # ([[feedback_mecanismo_que_nao_mede_o_denominador]]).
    try:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE status = ANY(%s)", (list(STATUS_ABERTOS),)
        )
        total_abertas = dict(cur.fetchone())["n"]
        cur.execute(
            """SELECT count(*) AS n FROM tasks
                WHERE status NOT IN ('completed','concluida','cancelled','cancelada')
                  AND status <> ALL(%s)""", (list(STATUS_ABERTOS),)
        )
        com_gesto = dict(cur.fetchone())["n"]
    except Exception:
        total_abertas, com_gesto = None, None
    conn.close()

    contagem = {b: 0 for b in BALDES}
    for r in rows:
        contagem[r["balde"]] += 1
    atrasadas = len(rows)

    print("╔═ TRIAGEM DE PRAZOS — o prazo morreu ou a tarefa morreu? ═╗")
    if total_abertas:
        pct = round(atrasadas * 100 / total_abertas)
        print(f"  {atrasadas} atrasadas de {total_abertas} abertas ({pct}%)"
              + (f" · {com_gesto} com gesto explícito (delegated/on_hold), fora da conta" if com_gesto else ""))
        if pct >= 50:
            print("  ⚠️ Com mais da metade atrasada, \"atrasada\" deixou de ser um sinal:")
            print("     ele não distingue mais nada. É esse ruído que se sente como")
            print("     \"nem um pouco funcional\" — não o volume de tarefas.")
    print()

    for b in ORDEM:
        icone, titulo, nota = BALDES[b]
        n = contagem[b]
        if a.quiet and n == 0:
            continue
        print(f"  {icone} {n:>4}  {titulo}")
        if not a.quiet:
            print(f"         ↳ {nota}")

    # Quantas das "vivas" se seguram num fio só. 124 de 204 em 23/11 — e é esse
    # número que proíbe tratar o balde oposto como veredito.
    um_sinal = sum(
        1 for r in rows
        if r["balde"] == "viva"
        and (int(bool(r["mov_proprio"])) + int(bool(r["mov_frente"])) + int(bool(r["mov_contato"]))) == 1
    )
    if not a.quiet and contagem["viva"]:
        print()
        print(f"  🔎 AUTOEXAME — {um_sinal} das {contagem['viva']} vivas dependem de UM único sinal.")
        try:
            cur2 = get_connection().cursor()
            cur2.execute(SQL_AUTOEXAME, {"janela": JANELA_MOVIMENTO_D})
            ae = dict(cur2.fetchone())
            print(f"     E o sinal mais fraco é o da frente: só {ae['frescas']} de {ae['ativas']} frentes")
            print(f"     ativas têm nota ≤{JANELA_MOVIMENTO_D}d. O trabalho é registrado nos boards de")
            print("     memória e no WhatsApp, não em `project_notes` ⇒ eu SUBESTIMO movimento.")
            print("     Logo: o balde 🟡 é um TETO. Tratá-lo como veredito arquiva coisa viva.")
        except Exception as e:
            print(f"     ⚠️ autoexame não medido ({type(e).__name__}) — leia o 🟡 com ainda mais reserva.")

    google = sum(1 for r in rows if r["tem_google"])
    if google:
        print()
        print(f"  ⚠️ {google} das {atrasadas} sincronizam com o Google Tasks"
              f" ({round(google*100/atrasadas)}%) — toda ação em lote chega no celular dele.")
        print("     Nenhum gesto aqui: este script só mede. O gesto é do Renato.")

    if a.balde:
        sel = [r for r in rows if r["balde"] == a.balde][: a.limite]
        icone, titulo, _ = BALDES[a.balde]
        print()
        print(f"  ── {icone} {titulo} — {len(sel)} de {contagem[a.balde]} ──")
        for r in sel:
            sinais = "".join([
                "P" if r["mov_proprio"] else "·",
                "F" if r["mov_frente"] else "·",
                "C" if r["mov_contato"] else "·",
            ])
            frente = (r["frente_nome"] or "(sem frente)")[:26]
            print(f"   #{r['id']:<8} {int(r['dias_vencida']):>4}d  [{sinais}]  "
                  f"{(r['titulo'] or '')[:58]:<58}  {frente}")
        if not a.quiet and sel:
            print("     sinais: P=movimento próprio · F=da frente · C=do contato")

    # Exit 0 SEMPRE: isto é um retrato para decisão, não um alarme. Sair !=0 aqui
    # faria a abertura tratar backlog de prazo como incidente, e o /dev pararia
    # todo dia por uma condição que só o Renato resolve.
    return 0


if __name__ == "__main__":
    sys.exit(main())
