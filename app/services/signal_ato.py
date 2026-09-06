"""Fechamento de signal por EVIDENCIA DE ATO — o numerador que faltava.

POR QUE EXISTE (06/09/2026, task #999981). `signals.status` nunca chegava a
`resolved` para nenhum detector da geracao de agosto. Dos 168 `resolved` da
historia, 157 tinham `resolved_by='tonha_brain'` e todos caiam entre 15/06 e
27/06: os detectores novos foram ligados sem herdar quem fechava o ciclo. O
efeito medido em 06/09: de 206 signals de `frente_review.alertar_portoes` em 45
dias, **zero** resolvidos — 186 `detector_expired` e 20 abertos.

ISSO ENVENENAVA A MEDICAO INTEIRA, e nao so a linha do detector:

  - `capability_registry._detectors` calcula value_ratio = resolved/(resolved+
    ignorado). Sem escritor de `resolved`, o numerador e' estruturalmente zero.
    O conserto de 31/08 (`5ca471c`) tirou os inconclusivos do denominador, e o
    ratio passou de um **0.0 mentiroso** para um **NULL honesto** — correto, mas
    ainda sem medir nada. Trocar a mentira pela abstencao era metade do caminho;
    a outra metade e ter numerador. [[feedback_medidor_que_nao_mede_a_si_mesmo]]
  - `emit_signal` ja CONSOME `resolved` (devolve 'skipped' quando o portao ja
    foi fechado, pra nao reabrir o que ele resolveu). O consumidor existia e o
    escritor nao — o portao voltava a cobrar o que ja tinha sido feito.

A EVIDENCIA DE ATO JA ESTAVA GRAVADA, so nao era lida. Cada signal de portao
cita no texto as tasks que o portao carrega ("a task #999919, vence amanha") e
essas tasks sao concluidas pelo Renato no Google Tasks. Medido em prod nos
mesmos 45 dias: **18 signals** tiveram uma task citada concluida DEPOIS da
emissao, dentro de 72h. Nao e telemetria nova — e leitura de telemetria que ja
existia. [[feedback_medir_antes_de_construir]]

NAO E ECO. A camada que escreve tem 5 operacoes e nenhuma conclui task (167
escritas em `tasks`, todas `criar_task_followup`); as 17 tasks concluidas do
levantamento tem todas `google_task_id`, isto e, o fechamento veio de fora do
sistema. O detector nao se declara util sozinho — quem fecha e o ato do Renato.
Se um dia a camada ganhar `concluir_task`, a guarda `_CONCLUSAO_DE_AGENTE`
abaixo precisa passar a valer, senao isto vira a maquina ouvindo o proprio eco.
[[feedback_maquina_ouve_o_proprio_eco]]

POR QUE O EXTRATOR NAO FILTRA POR PROJETO. A primeira versao exigia que a task
citada fosse do mesmo `project_id` do signal. Parecia prudente e descartava ato
REAL: o portao do projeto 47 cita legitimamente a task `[Carambola #40]` porque
depende dela, e a #999984 tem `project_id` NULL, o que reprovava sempre — guarda
que exige campo ausente falha calada. Os 18 candidatos foram auditados um a um
com o texto em volta: todos vem precedidos de "task"/"Task" ou de contexto
inequivoco. Zero falso positivo. A guarda certa e' textual (nao colar em nome de
tabela conhecido), nao relacional. [[feedback_task_nao_materializa_campo_ausente]]
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Janela entre a emissao do signal e a conclusao da task. 72h cobre o fim de
# semana (portao emitido na sexta, feito na segunda) sem esticar tanto a ponto
# de colher qualquer conclusao posterior como se fosse resposta ao alerta.
JANELA_ATO_HORAS = 72

# O texto dos signals cita varias tabelas no formato `tabela#N`. Sem esta lista,
# `messages#28762` viraria "task 28762" — e um ID de mensagem que por acaso
# exista como task fecharia um portao que ninguem cumpriu.
TABELAS_CITAVEIS = frozenset({
    "messages", "calendar_events", "project_notes", "contact_facts", "contacts",
    "group_messages", "wa_attachments", "signals", "emails", "prospects",
    "action_proposals", "agent_writes", "raci_itens", "board_hunt_frentes",
})

# Agentes cuja escrita NAO conta como ato do Renato. Hoje nenhum conclui task
# (a lista de 5 operacoes so cria), mas a guarda fica ligada de proposito: no
# dia em que `concluir_task` entrar na camada, o fechador nao pode passar a se
# auto-alimentar em silencio.
_CONCLUSAO_DE_AGENTE = ("cos_agent", "cos_agent_local", "camada_agente")

_CITACAO = re.compile(r"(?:([A-Za-z_]+)\s*)?#(\d{5,7})")

MOTIVO = "ato:task_concluida"


def extrair_tasks_citadas(texto: Optional[str]) -> Set[int]:
    """IDs de task citados no texto — `#N` NAO colado a nome de tabela conhecido.

    `task #999919` e `(#999872)` contam; `messages#28762` nao. O prefixo e' lido
    com `\\s*` de proposito: o texto real traz as duas formas ("task #999931" e
    "project_notes#968").
    """
    if not texto:
        return set()
    achados: Set[int] = set()
    for m in _CITACAO.finditer(texto):
        prefixo = (m.group(1) or "").lower()
        if prefixo in TABELAS_CITAVEIS:
            continue
        achados.add(int(m.group(2)))
    return achados


def _contexto_dict(contexto: Any) -> Dict[str, Any]:
    """`contexto` vem dict (psycopg2 jsonb) ou str conforme o driver/cursor."""
    if isinstance(contexto, dict):
        return contexto
    if isinstance(contexto, str):
        try:
            return json.loads(contexto)
        except (ValueError, TypeError):
            return {}
    return {}


def fechar_signals_por_ato(
    conn,
    detector: Optional[str] = None,
    dias: int = 45,
    janela_horas: int = JANELA_ATO_HORAS,
    dry_run: bool = True,
    incluir_expirados: bool = False,
) -> Dict[str, Any]:
    """Marca `resolved` os signals cuja task citada foi concluida apos a emissao.

    `dry_run=True` por padrao: quem muda estado em producao diz que quer mudar.
    `incluir_expirados=False` por padrao — reclassificar signal ja fechado e'
    reescrever historico e fica atras de uma flag explicita; sem ela, so `open`.

    Idempotente: um signal ja `resolved` nao e' candidato, entao rodar duas vezes
    nao muda nada na segunda.
    """
    cur = conn.cursor()

    filtro_status = (
        "(status = 'open' OR (status = 'expired' "
        " AND COALESCE(resolved_by,'') LIKE 'detector_expired%%'))"
        if incluir_expirados else "status = 'open'"
    )
    params: List[Any] = [dias]
    sql_detector = ""
    if detector:
        sql_detector = "AND detector = %s"
        params.append(detector)

    cur.execute(
        f"""
        SELECT id, detector, status, resolved_by, criado_em, contexto
          FROM signals
         WHERE {filtro_status}
           AND criado_em > NOW() - (%s || ' days')::interval
           {sql_detector}
         ORDER BY criado_em
        """,
        params,
    )
    candidatos = cur.fetchall()

    pares: List[tuple] = []
    for s in candidatos:
        ctx = _contexto_dict(s["contexto"])
        for tid in extrair_tasks_citadas(ctx.get("o_que")):
            pares.append((s, tid))

    stats: Dict[str, Any] = {
        "signals_examinados": len(candidatos),
        "citacoes": len(pares),
        "fechados": 0,
        "dry_run": dry_run,
        "evidencias": [],
        "descartados_por_agente": 0,
    }
    if not pares:
        return stats

    ids = sorted({tid for _, tid in pares})
    cur.execute(
        """
        SELECT id, status, data_conclusao, titulo
          FROM tasks
         WHERE id = ANY(%s) AND status = 'completed' AND data_conclusao IS NOT NULL
        """,
        (ids,),
    )
    concluidas = {r["id"]: r for r in cur.fetchall()}
    if not concluidas:
        return stats

    # Guarda anti-eco: conclusao feita pela propria camada nao e ato do Renato.
    # Hoje devolve vazio (nenhuma operacao conclui task); existe para o dia em
    # que deixar de devolver.
    cur.execute(
        """
        SELECT DISTINCT registro_id
          FROM agent_writes
         WHERE tabela = 'tasks'
           AND registro_id = ANY(%s)
           AND agente = ANY(%s)
           AND operacao ILIKE %s
           AND desfeito_em IS NULL
        """,
        (sorted(concluidas.keys()), list(_CONCLUSAO_DE_AGENTE), "%conclu%"),
    )
    por_agente = {r["registro_id"] for r in cur.fetchall()}

    # Um signal pode citar varias tasks; a PRIMEIRA conclusao dentro da janela
    # fecha. Guardar so uma evidencia por signal mantem a procedencia legivel.
    ja_fechados: Set[int] = set()
    for s, tid in pares:
        if s["id"] in ja_fechados:
            continue
        t = concluidas.get(tid)
        if not t:
            continue
        if tid in por_agente:
            stats["descartados_por_agente"] += 1
            continue
        delta_h = (t["data_conclusao"] - s["criado_em"]).total_seconds() / 3600.0
        if not (0 < delta_h <= janela_horas):
            continue

        ja_fechados.add(s["id"])
        stats["fechados"] += 1
        stats["evidencias"].append({
            "signal_id": s["id"],
            "detector": s["detector"],
            "status_anterior": s["status"],
            "task_id": tid,
            "task_titulo": t["titulo"][:80],
            "horas": round(delta_h, 1),
        })

        if dry_run:
            continue

        # A evidencia vai pro proprio contexto do signal: sem procedencia, o
        # `resolved` vira afirmacao sem lastro e ninguem consegue auditar depois
        # por que aquele portao consta como cumprido.
        ctx = _contexto_dict(s["contexto"])
        ctx["ato_evidencia"] = {
            "task_id": tid,
            "concluida_em": t["data_conclusao"].isoformat(),
            "horas_apos_signal": round(delta_h, 1),
            "regra": MOTIVO,
        }
        cur.execute(
            """
            UPDATE signals
               SET status = 'resolved',
                   resolved_at = %s,
                   resolved_by = %s,
                   contexto = %s,
                   atualizado_em = NOW()
             WHERE id = %s AND status <> 'resolved'
            """,
            (t["data_conclusao"], MOTIVO, json.dumps(ctx, ensure_ascii=False), s["id"]),
        )

    logger.info(
        "signal_ato: %s signal(is) examinado(s), %s citacao(oes), %s fechado(s)%s",
        stats["signals_examinados"], stats["citacoes"], stats["fechados"],
        " (dry-run)" if dry_run else "",
    )
    return stats
