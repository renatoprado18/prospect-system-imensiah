#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Placar dos watchers de notícia — o funil inteiro, até a ação.

POR QUE EXISTE (22/08/2026). O Renato perguntou "como medimos o valor?" depois de
descobrir que um artigo do Gui capturado em 10/08 nunca chegou nele. A resposta
honesta exigia medir cada degrau, porque o valor some em qualquer um deles:

    capturado → alertado → apresentado → LEU → virou contato

Os três primeiros o sistema já sabia contar. O quarto e o quinto, não — e é
justamente aí que a pergunta dele mora. Contar "117 propostas criadas" e chamar
isso de resultado é o erro que este arquivo existe pra não repetir: produção não é
consumo ([[feedback_medir_o_consumidor_certo]]).

A RÉGUA DO VALOR É O CONTATO, não a leitura. Um alerta sobre a Westwing só vale
se ele falar com alguém do grupo — foi o objetivo declarado ao criar o watcher
("é demo sim, mas com o objetivo de me aproximar do grupo"). Então o último
degrau mede mensagem OUTBOUND para membro do projeto depois do alerta. É a mesma
lógica de [[feedback_notificacao_valor_medido]]: notificação que não muda o que
ele faz é ruído com boa intenção.

⚠️ CORRELAÇÃO, NÃO CAUSA. Se ele falou com a pessoa depois do alerta, o script
não sabe se foi POR CAUSA dele. A janela curta (7 dias) e o contraste com a taxa
de base é o que torna o número interpretável — não uma prova. Um medidor que se
apresenta como prova é pior que nenhum.

Uso:  ./news_placar.py            # funil dos últimos 30 dias
      ./news_placar.py --dias 60
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from database import get_connection  # noqa: E402

JANELA_CONTATO_DIAS = 7

# Diferença mínima, em pontos percentuais, para dizer que o alerta mudou algo.
# Abaixo disso as duas janelas são a mesma coisa e o dado honesto é "não separa".
MARGEM_PP = 5.0


def veredito_do_lift(linhas):
    """Compara a janela DEPOIS do alerta com a MESMA regra ANTES dele.

    Recebe linhas com `virou_contato` (janela +7d) e `ja_havia_contato` (−7d).
    Devolve as duas taxas, o lift em ambas as direções e a leitura em uma frase.

    O lift é o que a taxa bruta esconde: um hit onde já havia conversa antes e
    continuou havendo depois não é alerta funcionando, é projeto vivo. Só conta
    como ganho o contato que NÃO existia antes e passou a existir.
    """
    n = len(linhas)
    if not n:
        return {"n": 0, "depois": 0, "antes": 0, "taxa_depois": 0.0, "taxa_antes": 0.0,
                "lift_positivo": 0, "lift_negativo": 0,
                "veredito": "sem hit alertado na janela — nada a medir"}

    depois = sum(1 for x in linhas if x["virou_contato"])
    antes = sum(1 for x in linhas if x["ja_havia_contato"])
    lift_pos = sum(1 for x in linhas if x["virou_contato"] and not x["ja_havia_contato"])
    lift_neg = sum(1 for x in linhas if x["ja_havia_contato"] and not x["virou_contato"])
    t_dep, t_ant = 100.0 * depois / n, 100.0 * antes / n
    delta = t_dep - t_ant

    if delta > MARGEM_PP:
        v = (f"o alerta ANTECEDE mais contato do que a base ({delta:+.0f} p.p.) — "
             f"{lift_pos} contato(s) novo(s)")
    elif delta < -MARGEM_PP:
        v = (f"a taxa CAI depois do alerta ({delta:+.0f} p.p.): a régua está medindo "
             f"o projeto estar vivo, não o efeito do alerta")
    else:
        v = (f"as duas janelas são iguais ({delta:+.0f} p.p.) — esta régua não separa "
             f"alerta de atividade de base, e não serve para decidir watcher")
    return {"n": n, "depois": depois, "antes": antes,
            "taxa_depois": t_dep, "taxa_antes": t_ant,
            "lift_positivo": lift_pos, "lift_negativo": lift_neg, "veredito": v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=30)
    a = ap.parse_args()

    cur = get_connection().cursor()

    # ---- degrau 1-3: o que o sistema já sabia contar --------------------
    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE h.hit_at > NOW() - (%s || ' days')::interval) capturados,
               COUNT(*) FILTER (WHERE h.pushed_at IS NOT NULL
                                  AND h.pushed_at > NOW() - (%s || ' days')::interval) alertados,
               COUNT(*) FILTER (WHERE h.archived_at IS NOT NULL
                                  AND h.pushed_at IS NULL) silenciados,
               COUNT(*) FILTER (WHERE h.pushed_at IS NULL AND h.archived_at IS NULL) na_fila
          FROM project_news_hits h
        """,
        (a.dias, a.dias),
    )
    f = cur.fetchone()

    cur.execute(
        """
        SELECT COUNT(DISTINCT s.id) emitidos,
               COUNT(DISTINCT ts.signal_id) apresentados
          FROM signals s
          LEFT JOIN tonia_seen_signals ts ON ts.signal_id = s.id
         WHERE s.tipo = 'news_pendente'
           AND s.criado_em > NOW() - (%s || ' days')::interval
        """,
        (a.dias,),
    )
    s = cur.fetchone()

    print(f"\n╔═ WATCHERS DE NOTÍCIA — últimos {a.dias} dias ═╗")
    print(f"  1. capturados ................ {f['capturados']:5}")
    print(f"  2. alertados (viraram signal)  {f['alertados']:5}")
    if f["alertados"] > f["capturados"]:
        # NÃO É FUNIL QUEBRADO: as duas linhas contam por datas diferentes —
        # `hit_at` na 1 e `pushed_at` na 2. Um hit capturado há 40 dias e
        # alertado há 10 entra só na segunda. Sem esta nota o leitor conclui que
        # o sistema alerta mais do que captura, que é impossível.
        print(f"     ↑ 2 > 1 é esperado: 1 conta por data de captura e 2 por data")
        print(f"       de alerta — hit antigo alertado agora entra só na linha 2")
    print(f"  3. signals emitidos .......... {s['emitidos']:5}")
    print(f"  4. apresentados pela Tônia ... {s['apresentados']:5}"
          f"   ← alcance real; abaixo disso ele nunca viu")
    print(f"  {'─'*54}")
    print(f"  na fila (esperando alerta) ... {f['na_fila']:5}")
    print(f"  silenciados (acervo/velhos) .. {f['silenciados']:5}")

    # ---- degrau 5: virou contato? ---------------------------------------
    # Para cada hit alertado, houve mensagem SAINDO pro membro do projeto
    # daquele watcher nos 7 dias seguintes? `outgoing` = Renato enviou
    # ([[CLAUDE.md]]: outgoing = Renato, incoming = contato).
    #
    # ⚠️ A MESMA REGRA RODA NA JANELA DE ANTES, e é isso que torna o número
    # legível. Até 06/09/26 este degrau imprimia só a taxa de depois — 93% — e
    # essa taxa foi lida como valor entregue. O placebo desmente: a taxa ANTES do
    # alerta é 98%, MAIOR que a de depois. O que a regra captura é o projeto ter
    # conversa em andamento (Emma: 56 de 56 nos dois lados), não o efeito do
    # alerta. O docstring já prometia "o contraste com a taxa de base" e o script
    # nunca o calculava: medidor que promete denominador e não entrega certifica
    # o que nunca mediu. [[feedback_mecanismo_que_nao_mede_o_denominador]]
    cur.execute(
        """
        WITH alertados AS (
            SELECT h.id, h.pushed_at, w.project_id, p.nome AS projeto, h.title
              FROM project_news_hits h
              JOIN project_news_watchers w ON w.id = h.watcher_id
              LEFT JOIN projects p ON p.id = w.project_id
             WHERE h.pushed_at IS NOT NULL
               AND h.pushed_at > NOW() - (%s || ' days')::interval
        )
        SELECT a.projeto, a.title, a.pushed_at,
               EXISTS (
                   SELECT 1
                     FROM messages m
                     JOIN project_members pm ON pm.contact_id = m.contact_id
                    WHERE pm.project_id = a.project_id
                      AND m.direcao = 'outgoing'
                      AND COALESCE(m.enviado_em, m.criado_em) BETWEEN a.pushed_at
                                          AND a.pushed_at + (%s || ' days')::interval
               ) AS virou_contato,
               EXISTS (
                   SELECT 1
                     FROM messages m
                     JOIN project_members pm ON pm.contact_id = m.contact_id
                    WHERE pm.project_id = a.project_id
                      AND m.direcao = 'outgoing'
                      AND COALESCE(m.enviado_em, m.criado_em) BETWEEN
                                          a.pushed_at - (%s || ' days')::interval
                                      AND a.pushed_at
               ) AS ja_havia_contato
          FROM alertados a
         ORDER BY a.pushed_at DESC
        """,
        (a.dias, JANELA_CONTATO_DIAS, JANELA_CONTATO_DIAS),
    )
    linhas = cur.fetchall()
    v = veredito_do_lift(linhas)

    print(f"\n  5. seguidos de CONTATO em {JANELA_CONTATO_DIAS}d .. {v['depois']:5} de {v['n']}"
          f"   ({v['taxa_depois']:.0f}%)")
    print(f"     a MESMA regra na janela de ANTES  {v['antes']:5} de {v['n']}"
          f"   ({v['taxa_antes']:.0f}%)   ← taxa de base")
    print(f"  {'─'*54}")
    print(f"     LIFT: contato NOVO depois do alerta .. {v['lift_positivo']:4}")
    print(f"           contato que existia e sumiu .... {v['lift_negativo']:4}")
    print(f"\n     → {v['veredito']}")
    print("\n  ⚠️ correlação, não causa — em nenhuma das duas janelas. O contraste")
    print("     com a taxa de base é o que torna o número interpretável; a taxa")
    print("     de depois, sozinha, mede o projeto estar vivo.")

    # ---- por watcher: quem entrega e quem faz barulho --------------------
    cur.execute(
        """
        SELECT w.id, COALESCE(p.nome, w.query) AS nome, w.query, w.active,
               COUNT(h.id) FILTER (WHERE h.hit_at > NOW() - (%s || ' days')::interval) hits,
               COUNT(h.id) FILTER (WHERE h.pushed_at IS NOT NULL) alertados
          FROM project_news_watchers w
          LEFT JOIN projects p ON p.id = w.project_id
          LEFT JOIN project_news_hits h ON h.watcher_id = w.id
         GROUP BY w.id, p.nome, w.query, w.active
         ORDER BY hits DESC, w.id
        """,
        (a.dias,),
    )
    print(f"\n  ── por watcher ({a.dias}d) ──")
    for w in cur.fetchall():
        marca = "  " if w["active"] else "off"
        print(f"   {marca} #{w['id']:2} {w['nome'][:34]:34} hits={w['hits']:3} alertados={w['alertados']:3}")

    print("\n  Como ler: watcher com muitos hits e nenhum contato é candidato a")
    print("  desligar ou afinar a query — cobertura sem uso é ruído com custo.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
