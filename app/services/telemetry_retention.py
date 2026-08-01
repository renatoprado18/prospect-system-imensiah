"""
Retencao da telemetria — poda o que so servia pra depurar ontem.

POR QUE EXISTE. Medido em 31/07/2026: o banco tinha 397 MB, dos quais 274 MB
(69%) eram telemetria. `webhook_audit` sozinha ocupava 218 MB — 55% do banco —
com 105.026 linhas crescendo ~2.059/dia e NENHUMA politica de retencao desde
que foi criada. O dado de negocio inteiro (22k mensagens, 12k contatos, 10k
mensagens de grupo) cabia em 65 MB. Ou seja: pagavamos armazenamento e backup
de um banco 4x maior do que o negocio precisa, pra guardar o corpo bruto de
webhooks de dois meses atras que ninguem jamais releu.

O QUE ESTE ARQUIVO NAO FAZ, e e o ponto principal. Ele NAO apaga tudo que passa
de 30 dias. Cada uma das tres tabelas tem um leitor com exigencia diferente, e
uma poda uniforme quebraria dois deles:

  webhook_audit    -> pode apagar a LINHA. O `wa_catchup` le com janela de 2h
                      (`catchup_recent_dms(hours=2)`) e contatos de 7 dias; a
                      rota `/api/admin/webhook-audit` e diagnostico de evento
                      recente. Ninguem consulta 30 dias atras.

  cron_runs        -> NAO pode apagar linha. `monitor-cron-health` faz
                      `MAX(started_at)` por job com lookback PROPORCIONAL ao
                      intervalo declarado (piso de 120 dias, main.py:29202), e
                      medimos 14 jobs cuja ultima execucao bem-sucedida ja e
                      mais velha que 30 dias. Apagar essas linhas os declararia
                      "nunca disparou" e dispararia alerta falso — exatamente o
                      defeito que o comentario de 27/07 em main.py:29199 conta
                      ter consertado uma vez ("um job trimestral/anual perderia
                      a propria evidencia pela borda"). Aqui so esvaziamos o
                      `result_json`, que e onde esta o peso.

  cron_heartbeats  -> `monitor-cron-health` roda `MAX(fired_at) GROUP BY job_id`
                      SEM janela nenhuma (main.py:29174). Apagar todas as linhas
                      de um job silencioso o removeria do radar. Preservamos
                      sempre a linha mais recente de cada `job_id`.

E POR QUE `result_json` DO `monitor-cron-health` FICA. Esse job e o unico cujo
payload e lido depois: main.py:29252 varre `result_json->'jobs'` com
`INTERVAL '400 days'` pra descobrir quando cada job foi avistado pela primeira
vez — e esse "primeiro avistamento" e o relogio que decide se um job mensal ja
teve tempo de rodar. Esvaziar esse campo cegaria o detector no ponto em que ele
e menos obvio: ele continuaria respondendo 200, so que com a conta errada.

Idempotente: rodar duas vezes seguidas nao muda nada na segunda.
"""
from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Dias mantidos. 30 e a janela em que a telemetria ainda responde "por que essa
# mensagem nao chegou?" — a pergunta que motivou `webhook_audit` existir. Alem
# disso ela vira arquivo morto: nenhum consumidor no repo olha tao longe.
RETENCAO_DIAS = 30

# O unico job cujo result_json e lido retroativamente (ate 400 dias). Ver o
# docstring. Se algum dia outro job ganhar leitura historica do payload, ele
# entra AQUI — e nao no esquecimento.
JOBS_COM_PAYLOAD_HISTORICO = ("/api/cron/monitor-cron-health",)


def podar(dias: int = RETENCAO_DIAS, dry_run: bool = False) -> Dict:
    """Aplica a retencao nas tres tabelas. Devolve o que saiu, por tabela.

    `dry_run=True` conta sem apagar — use pra conferir o tamanho do estrago
    antes da primeira execucao, que e de longe a maior.
    """
    from database import get_db

    resultado: Dict = {"dias": dias, "dry_run": dry_run, "tabelas": {}}

    with get_db() as conn:
        cur = conn.cursor()

        # ---- 1. webhook_audit: apaga a linha inteira -------------------------
        # A tabela e append-only e o unico leitor de janela larga e humano
        # (a rota de admin). Nenhum job depende de historico aqui.
        cur.execute(
            "SELECT COUNT(*) AS n FROM webhook_audit "
            "WHERE received_at < NOW() - make_interval(days => %s)",
            (dias,),
        )
        n_wa = cur.fetchone()["n"]
        if not dry_run and n_wa:
            cur.execute(
                "DELETE FROM webhook_audit "
                "WHERE received_at < NOW() - make_interval(days => %s)",
                (dias,),
            )
        resultado["tabelas"]["webhook_audit"] = {"linhas_removidas": n_wa}

        # ---- 2. cron_runs: esvazia o payload, PRESERVA a linha ---------------
        # A linha e o que prova que o job rodou; o payload e o detalhe que so
        # importa no dia. Excecao dos jobs com leitura historica no docstring.
        placeholders = ",".join(["%s"] * len(JOBS_COM_PAYLOAD_HISTORICO))
        cur.execute(
            f"""SELECT COUNT(*) AS n FROM cron_runs
                WHERE started_at < NOW() - make_interval(days => %s)
                  AND result_json IS NOT NULL
                  AND path NOT IN ({placeholders})""",
            (dias, *JOBS_COM_PAYLOAD_HISTORICO),
        )
        n_cr = cur.fetchone()["n"]
        if not dry_run and n_cr:
            cur.execute(
                f"""UPDATE cron_runs SET result_json = NULL
                    WHERE started_at < NOW() - make_interval(days => %s)
                      AND result_json IS NOT NULL
                      AND path NOT IN ({placeholders})""",
                (dias, *JOBS_COM_PAYLOAD_HISTORICO),
            )
        resultado["tabelas"]["cron_runs"] = {
            "payloads_esvaziados": n_cr, "linhas_removidas": 0,
        }

        # ---- 3. cron_heartbeats: apaga, menos o ultimo de cada job -----------
        # `NOT IN (ultimo de cada job)` e o que impede um job silencioso de
        # sumir do `MAX(fired_at) GROUP BY job_id`, que nao tem janela.
        cur.execute(
            """SELECT COUNT(*) AS n FROM cron_heartbeats
               WHERE fired_at < NOW() - make_interval(days => %s)
                 AND id NOT IN (
                     SELECT DISTINCT ON (job_id) id FROM cron_heartbeats
                     ORDER BY job_id, fired_at DESC)""",
            (dias,),
        )
        n_hb = cur.fetchone()["n"]
        if not dry_run and n_hb:
            cur.execute(
                """DELETE FROM cron_heartbeats
                   WHERE fired_at < NOW() - make_interval(days => %s)
                     AND id NOT IN (
                         SELECT DISTINCT ON (job_id) id FROM cron_heartbeats
                         ORDER BY job_id, fired_at DESC)""",
                (dias,),
            )
        resultado["tabelas"]["cron_heartbeats"] = {"linhas_removidas": n_hb}

        if not dry_run:
            conn.commit()

    total = n_wa + n_hb
    resultado["total_linhas_removidas"] = total
    resultado["total_payloads_esvaziados"] = n_cr
    logger.info(
        "telemetry_retention: %s linhas removidas, %s payloads esvaziados (dias=%s, dry_run=%s)",
        total, n_cr, dias, dry_run,
    )
    return resultado
