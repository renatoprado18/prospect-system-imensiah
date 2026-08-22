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
               ) AS virou_contato
          FROM alertados a
         ORDER BY a.pushed_at DESC
        """,
        (a.dias, JANELA_CONTATO_DIAS),
    )
    linhas = cur.fetchall()
    com = sum(1 for x in linhas if x["virou_contato"])

    print(f"\n  5. seguidos de CONTATO em {JANELA_CONTATO_DIAS}d .. {com:5} de {len(linhas)}"
          f"   ← a régua do valor")
    if linhas:
        taxa = 100.0 * com / len(linhas)
        print(f"     taxa: {taxa:.0f}%")
    print("\n  ⚠️ correlação, não causa: o script não sabe se o contato foi POR CAUSA")
    print("     do alerta. Serve pra comparar watchers entre si e ao longo do tempo.")

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
