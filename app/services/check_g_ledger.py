"""Ledger do check G — registra o inbound triado E o que foi descartado.

POR QUE EXISTE (06/08/2026). O check G mostra ~5 contatos por dia e o resto
some sem deixar linha. Em 05/08 descobrimos POR MEDIÇÃO MANUAL que um regex de
vocabulário matava **83%** do que tinha a bola com o Renato — o pedido do
Halfeld sobre o Judô, a consulta trabalhista da Glaucia, o Julio Andery. O
regex saiu; a cegueira não. Continuávamos sem saber o que os outros gates matam.

O funil, medido em 06/08 numa janela de 4 dias:

    328 incoming
    ├─  97 sem contact_id ......... quem não tem ficha NUNCA aparece no check
    ├─ 127 bola com o terceiro
    ├─   1 ruído comercial
    └─  62 sobrevivem, em 20 contatos → é isto que a /cos vê

266 mensagens morreram no caminho, e nenhuma deixou registro. "Alta precisão"
dito sobre as 20 é afirmação sobre o que se vê, nunca sobre o que se perdeu.

Este módulo faz duas coisas, e só:

  registrar()  — classifica cada incoming da janela e grava decisão + gate.
                 Idempotente por message_id: rodar de novo não duplica, e a
                 primeira decisão é a que fica (reclassificar por gate que mudou
                 apagaria justamente a evidência de que ele mudou).

  medir()      — cruza o SUPRIMIDO com o que aconteceu depois (`acao_do_renato`,
                 tasks do contato). Suprimido + ação depois = **o gate errou**.
                 É assim que o falso negativo deixa de ser opinião.

O que ele NÃO faz: não notifica, não cria proposta, não age. Respeita a decisão
"ii" do Renato — zero notificação nova. É memória pra medir, não alarme.

Rodar de fora:
    from services.check_g_ledger import registrar, medir
"""
from __future__ import annotations

import logging
from typing import Dict, List

from database import get_db

logger = logging.getLogger(__name__)

# Os gates, na ordem em que matam. A ordem importa: uma mensagem sem ficha e
# fora da janela é registrada pelo PRIMEIRO motivo, e é esse que se conta.
GATE_SEM_FICHA   = "sem_ficha"          # contact_id NULL — nem entra na query da /cos
GATE_BOLA_DELE   = "bola_com_terceiro"  # o Renato já respondeu por último
GATE_RUIDO       = "ruido_comercial"
GATE_JA_TASK     = "ja_virou_task"
GATE_JA_EVENTO   = "ja_virou_evento"
GATE_JA_RESOLVIDA = "ja_resolvida"

# A classificação espelha os gates do check G (`.claude/skills/cos/SKILL.md`).
# ⚠️ Se o check mudar de gate e isto não mudar junto, o ledger mede um funil que
# não existe mais — e um medidor errado é pior que nenhum, porque tem a cara de
# quem sabe. Ao mexer num, releia o outro.
_SQL_CLASSIFICA = """
WITH lastdir AS (
    SELECT DISTINCT ON (contact_id) contact_id, direcao
      FROM messages
     WHERE contact_id IS NOT NULL AND criado_em > now() - interval '14 days'
     ORDER BY contact_id, criado_em DESC
)
SELECT
    m.id, m.external_id, m.contact_id,
    COALESCE(m.metadata->>'phone', '') AS telefone,
    m.criado_em,
    left(m.conteudo, 160) AS trecho,
    CASE
        WHEN m.contact_id IS NULL THEN %(sem_ficha)s
        WHEN COALESCE(l.direcao, 'outgoing') <> 'incoming' THEN %(bola_dele)s
        WHEN length(m.conteudo) <= 600 AND m.conteudo ~*
             'desconto|promo[çc]|black friday|imperd|clique aqui|link na bio|inscri[çc]|newsletter|cancelar|no-?reply|unsubscribe'
             THEN %(ruido)s
        WHEN EXISTS (SELECT 1 FROM tasks t
                      WHERE t.contact_id = m.contact_id AND t.data_criacao > m.criado_em)
             THEN %(ja_task)s
        WHEN EXISTS (SELECT 1 FROM calendar_events e
                      WHERE e.contact_id = m.contact_id AND e.criado_em > m.criado_em)
             THEN %(ja_evento)s
        WHEN EXISTS (SELECT 1 FROM tasks t
                      WHERE t.contact_id = m.contact_id AND t.status = 'completed'
                        AND t.atualizado_em > m.criado_em)
             THEN %(ja_resolvida)s
        ELSE NULL
    END AS gate
FROM messages m
LEFT JOIN lastdir l ON l.contact_id = m.contact_id
WHERE m.direcao = 'incoming'
  AND m.criado_em > now() - make_interval(days => %(dias)s)
"""

_PARAMS = {
    "sem_ficha": GATE_SEM_FICHA, "bola_dele": GATE_BOLA_DELE, "ruido": GATE_RUIDO,
    "ja_task": GATE_JA_TASK, "ja_evento": GATE_JA_EVENTO, "ja_resolvida": GATE_JA_RESOLVIDA,
}


def registrar(dias: int = 4) -> Dict:
    """Classifica o inbound da janela e grava no ledger. Idempotente."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_SQL_CLASSIFICA, {**_PARAMS, "dias": dias})
        linhas = cur.fetchall()

        gravadas = 0
        for r in linhas:
            gate = r["gate"]
            cur.execute(
                """
                INSERT INTO check_g_ledger
                    (message_id, external_id, contact_id, telefone, msg_em,
                     decisao, gate, trecho)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (message_id) DO NOTHING
                """,
                (r["id"], r["external_id"], r["contact_id"], r["telefone"],
                 r["criado_em"], "suprimido" if gate else "mostrado", gate, r["trecho"]),
            )
            gravadas += cur.rowcount
        conn.commit()

    por_gate: Dict[str, int] = {}
    for r in linhas:
        por_gate[r["gate"] or "mostrado"] = por_gate.get(r["gate"] or "mostrado", 0) + 1
    logger.info(f"check_g_ledger: {len(linhas)} avaliadas, {gravadas} novas — {por_gate}")
    return {"avaliadas": len(linhas), "gravadas": gravadas, "por_gate": por_gate}


# O que conta como ERRO depende do gate — e ignorar isso produz número bonito e
# falso. A primeira versão desta medição contou "o Renato agiu depois" como erro
# em todos os gates e cuspiu "59,5% de falso negativo". Era tautologia: o gate
# `bola_com_terceiro` suprime PORQUE ele já respondeu, então "agiu depois"
# aparecia em 125 de 127 casos. Medir sem pensar no que se mede é o defeito que
# esta tabela existe pra caçar.
_ERRO_POR_GATE = {
    # Ele respondeu — só é erro se, mesmo assim, precisou virar tarefa depois:
    # sinal de que a resposta não fechou o assunto.
    GATE_BOLA_DELE:    ("virou_task",),
    # Já havia task concluída do contato. Task NOVA depois = havia outra coisa.
    GATE_JA_RESOLVIDA: ("virou_task",),
    GATE_JA_TASK:      ("virou_task",),
    GATE_JA_EVENTO:    ("virou_task",),
    # Promoção não deveria gerar nem ação nem tarefa. Qualquer um dos dois acusa.
    GATE_RUIDO:        ("virou_task", "agiu"),
    # Sem ficha não tem como cruzar por contact_id — ver `nao_medido` abaixo.
    GATE_SEM_FICHA:    ("virou_ficha",),
}


def medir(dias: int = 14) -> Dict:
    """Falso negativo do check, gate a gate — com o que NÃO foi medido à vista.

    Cruza o suprimido com o que aconteceu depois: `acao_do_renato` (view 062) e
    tasks do contato. Mas o que caracteriza ERRO muda por gate (ver
    `_ERRO_POR_GATE`), porque cada gate suprime por um motivo diferente.

    E o buraco fica explícito em vez de virar zero: mensagem sem ficha não tem
    `contact_id` pra cruzar, então ela é marcada `nao_medido` — não `nada`. São
    97 das 328 da janela de 06/08 (30%). Contá-las como "sem erro" produziria um
    verde que significa apenas "não olhei" — exatamente o que derrubou a
    ingestão do WhatsApp e sumiu com 42% dos áudios.
    """
    with get_db() as conn:
        cur = conn.cursor()
        # (a) com ficha: cruzamento normal
        cur.execute(
            """
            UPDATE check_g_ledger l SET
                desfecho = CASE
                    WHEN EXISTS (SELECT 1 FROM tasks t
                                  WHERE t.contact_id = l.contact_id
                                    AND t.data_criacao > l.msg_em) THEN 'virou_task'
                    WHEN EXISTS (SELECT 1 FROM acao_do_renato a
                                  WHERE a.pessoa_id = l.contact_id
                                    AND a.quando > l.msg_em) THEN 'agiu'
                    ELSE 'nada' END,
                desfecho_em = now()
            WHERE l.msg_em > now() - make_interval(days => %s)
              AND l.contact_id IS NOT NULL
            """,
            (dias,),
        )
        # (b) sem ficha: o único desfecho verificável é o número ter virado
        # contato depois. O resto continua fora de alcance — e assumido.
        cur.execute(
            """
            UPDATE check_g_ledger l SET
                desfecho = CASE WHEN EXISTS (
                        SELECT 1 FROM contacts c
                         WHERE c.criado_em > l.msg_em
                           AND regexp_replace(c.telefones::text, '\\D', '', 'g')
                               LIKE '%%' || regexp_replace(l.telefone, '\\D', '', 'g') || '%%'
                           AND length(regexp_replace(l.telefone, '\\D', '', 'g')) >= 10
                    ) THEN 'virou_ficha' ELSE 'nao_medido' END,
                desfecho_em = now()
            WHERE l.msg_em > now() - make_interval(days => %s)
              AND l.contact_id IS NULL
            """,
            (dias,),
        )
        cur.execute(
            """
            SELECT COALESCE(gate, 'mostrado') AS gate, decisao, count(*) AS n,
                   count(*) FILTER (WHERE desfecho = 'virou_task')  AS virou_task,
                   count(*) FILTER (WHERE desfecho = 'agiu')        AS agiu,
                   count(*) FILTER (WHERE desfecho = 'virou_ficha') AS virou_ficha,
                   count(*) FILTER (WHERE desfecho = 'nao_medido')  AS nao_medido
              FROM check_g_ledger
             WHERE msg_em > now() - make_interval(days => %s)
             GROUP BY 1, 2 ORDER BY 3 DESC
            """,
            (dias,),
        )
        linhas = [dict(r) for r in cur.fetchall()]
        conn.commit()

    for r in linhas:
        criterio = _ERRO_POR_GATE.get(r["gate"], ())
        r["criterio_de_erro"] = " ou ".join(criterio) if criterio else "—"
        r["erros"] = sum(r.get(k, 0) for k in criterio)

    suprimidas = [r for r in linhas if r["decisao"] == "suprimido"]
    total_sup = sum(r["n"] for r in suprimidas)
    erros = sum(r["erros"] for r in suprimidas)
    nao_medido = sum(r["nao_medido"] for r in linhas)
    return {
        "janela_dias": dias,
        "por_gate": linhas,
        "suprimidas": total_sup,
        "erros_do_gate": erros,
        # A taxa é sobre o que deu pra medir. O denominador honesto exclui o que
        # não foi verificável — e o número desse buraco vai junto, sempre.
        "falso_negativo_pct": (round(100 * erros / (total_sup - nao_medido), 1)
                               if total_sup - nao_medido > 0 else None),
        "nao_medido": nao_medido,
        "nao_medido_pct": round(100 * nao_medido / total_sup, 1) if total_sup else None,
    }
