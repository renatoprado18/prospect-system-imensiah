"""
CoS CONSELHEIRO Agent — especialista em conselhos (14/06/26).

Stage 3 do roadmap CoS multi-agent: agente IA dedicado a operação dos 4
conselhos do Renato (Vallen, Alba, Despertar, Assespro), seguindo modelo
ConselhoOS (atas + RACI + pauta + dossiê + análise documentos + matriz risco).

Diferença vs cos_sensor (generalista):
- sensor: monitora tudo (msgs, calendar, propostas) e age via tools 5
- CONSELHEIRO: focado em ciclo de conselhos. Lê estado dos 3 (Vallen, Alba,
  Despertar) no ConselhoOS + INTEL projects pros 4 + WA groups quando ligados.
  Cobertura Assespro reduzida (sem RACI no ConselhoOS — só drift de tasks INTEL).

Roda 1x/dia (manhã cedo BRT). Política de autonomia:
- atualizar RACI status com evidência textual → Auto
- pauta/dossiê quando reunião ≤7d → propor via send_wa_to_renato
- alertar drift RACI quando >2 itens vencidos por conselho → propor

MVP v0.1: Atas + RACI tracking.
v0.2 pendente: pauta+dossiê automatizado, análise documentos (via gsk),
matriz de risco.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 6
MAX_TOKENS_PER_ITER = 2500

# Budget cap diario duro pra o conselheiro
DAILY_CAP_USD = float(os.getenv("COS_CONSELHEIRO_DAILY_CAP_USD", "0.75"))

# Mapeamento dos 4 conselhos: INTEL project_id <-> ConselhoOS empresa_id <-> WA group
CONSELHOS: List[Dict[str, Any]] = [
    {
        "slug": "vallen",
        "label": "Vallen Clinic",
        "intel_project_id": 24,
        "conselhoos_empresa_id": "9c246a3a-3e7e-4472-9854-ce83f6e22e14",
        "wa_group_jid": "120363408325592607@g.us",
        "is_pagante": True,
        "papel_renato": "Conselheiro independente",
    },
    {
        "slug": "alba",
        "label": "Alba Consultoria",
        "intel_project_id": 26,
        "conselhoos_empresa_id": "205cf5a1-471b-4024-a1e8-5a90285d9df8",
        "wa_group_jid": "120363280545683241@g.us",
        "is_pagante": False,
        "papel_renato": "Conselheiro consultivo (gate kill 30/09)",
    },
    {
        "slug": "despertar",
        "label": "Associação Despertar",
        "intel_project_id": 25,
        "conselhoos_empresa_id": "813709bd-276c-4820-918e-2e9ce6d34e70",
        "wa_group_jid": "5511995982736-1542899989@g.us",  # Conselho Consultivo Despertar (linkado 15/06)
        "is_pagante": False,
        "papel_renato": "Conselheiro pro-bono (capital relacional Villela/Itaúsa)",
    },
    {
        "slug": "assespro",
        "label": "Assespro-SP — ImensIAH",
        "intel_project_id": 12,
        "conselhoos_empresa_id": None,  # Assespro nao esta no ConselhoOS
        "wa_group_jid": None,
        "is_pagante": False,
        "papel_renato": "Canal imensIAH (não conselho formal — observador)",
    },
]


def _conselhoos_connect():
    """Connect ao ConselhoOS DB separado."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    url = (os.getenv("CONSELHOOS_DATABASE_URL") or "").strip()
    if not url:
        return None
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def _load_conselho_state(conselho: Dict[str, Any]) -> Dict[str, Any]:
    """Carrega estado vivo de 1 conselho — RACI + reunioes + grupo WA + projeto."""
    state = {
        "slug": conselho["slug"],
        "label": conselho["label"],
        "papel_renato": conselho["papel_renato"],
        "is_pagante": conselho["is_pagante"],
        "raci_summary": None,
        "raci_overdue": [],
        "proxima_reuniao": None,
        "ultima_reuniao": None,
        "wa_msgs_72h": [],
        "intel_project": None,
        "intel_tasks_overdue": [],
    }

    # ConselhoOS RACI + reunioes
    if conselho.get("conselhoos_empresa_id"):
        try:
            co_conn = _conselhoos_connect()
            if co_conn:
                cur = co_conn.cursor()
                cur.execute(
                    """
                    SELECT status, COUNT(*) as count
                    FROM raci_itens WHERE empresa_id = %s GROUP BY status
                    """,
                    (conselho["conselhoos_empresa_id"],),
                )
                state["raci_summary"] = {r["status"]: r["count"] for r in cur.fetchall()}

                cur.execute(
                    """
                    SELECT id::text, area, acao, prazo, status,
                           responsavel_r, responsavel_a
                    FROM raci_itens
                    WHERE empresa_id = %s
                      AND status IN ('pendente', 'em_andamento', 'atrasado')
                      AND prazo IS NOT NULL AND prazo < CURRENT_DATE
                    ORDER BY prazo ASC LIMIT 8
                    """,
                    (conselho["conselhoos_empresa_id"],),
                )
                state["raci_overdue"] = [
                    {
                        "id": r["id"],
                        "area": r["area"],
                        "acao": (r["acao"] or "")[:120],
                        "prazo": r["prazo"].isoformat() if r["prazo"] else None,
                        "status": r["status"],
                        "r": r["responsavel_r"],
                        "a": r["responsavel_a"],
                        "dias_atraso": (datetime.now().date() - r["prazo"]).days if r["prazo"] else None,
                    }
                    for r in cur.fetchall()
                ]

                cur.execute(
                    """
                    SELECT id::text, data, status, pauta_md IS NOT NULL as tem_pauta,
                           ata_md IS NOT NULL as tem_ata
                    FROM reunioes WHERE empresa_id = %s
                      AND data > NOW()
                      AND status NOT IN ('cancelada', 'concluida')
                      ORDER BY data ASC LIMIT 1
                    """,
                    (conselho["conselhoos_empresa_id"],),
                )
                r = cur.fetchone()
                if r:
                    state["proxima_reuniao"] = {
                        "id": r["id"],
                        "data_iso": r["data"].isoformat() if r["data"] else None,
                        "dias_ate": (r["data"].date() - datetime.now().date()).days if r["data"] else None,
                        "tem_pauta": r["tem_pauta"],
                        "tem_ata": r["tem_ata"],
                    }

                cur.execute(
                    """
                    SELECT id::text, data, ata_md IS NOT NULL as tem_ata
                    FROM reunioes WHERE empresa_id = %s
                    ORDER BY data DESC LIMIT 1
                    """,
                    (conselho["conselhoos_empresa_id"],),
                )
                r = cur.fetchone()
                if r:
                    state["ultima_reuniao"] = {
                        "id": r["id"],
                        "data_iso": r["data"].isoformat() if r["data"] else None,
                        "dias_desde": (datetime.now().date() - r["data"].date()).days if r["data"] else None,
                        "tem_ata": r["tem_ata"],
                    }
                co_conn.close()
        except Exception as e:
            logger.warning(f"cos_conselheiro: ConselhoOS load falhou pra {conselho['slug']}: {e}")
            state["erro_conselhoos"] = str(e)[:200]

    # INTEL: tasks vencidas + msgs recentes do grupo WA
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, nome, status, prioridade, atualizado_em::date
                FROM projects WHERE id = %s
                """,
                (conselho["intel_project_id"],),
            )
            r = cur.fetchone()
            if r:
                state["intel_project"] = dict(r)

            cur.execute(
                """
                SELECT id, titulo, data_vencimento::date as venc, status, prioridade
                FROM tasks
                WHERE project_id = %s AND status IN ('pending', 'delegated')
                  AND data_vencimento IS NOT NULL AND data_vencimento < NOW()
                ORDER BY data_vencimento ASC LIMIT 6
                """,
                (conselho["intel_project_id"],),
            )
            state["intel_tasks_overdue"] = [dict(r) for r in cur.fetchall()]

            # Msgs do WA group ultimas 72h
            if conselho.get("wa_group_jid"):
                since = datetime.now() - timedelta(hours=72)
                cur.execute(
                    """
                    SELECT id, sender_name, content, timestamp
                    FROM group_messages
                    WHERE group_jid = %s AND timestamp >= %s
                      AND from_me = FALSE
                    ORDER BY timestamp DESC LIMIT 20
                    """,
                    (conselho["wa_group_jid"], since),
                )
                state["wa_msgs_72h"] = [
                    {
                        "sender": r["sender_name"],
                        "content": (r["content"] or "")[:200],
                        "ts": r["timestamp"].isoformat() if r["timestamp"] else None,
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        logger.warning(f"cos_conselheiro: INTEL load falhou pra {conselho['slug']}: {e}")
        state["erro_intel"] = str(e)[:200]

    return state


def _check_budget() -> Dict[str, Any]:
    today_iso = to_brt(now_utc()).date().isoformat()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COALESCE(SUM((result_json->>'cost_usd')::float), 0) AS sum_usd
                FROM cron_runs
                WHERE path = '/api/cron/cos-conselheiro-tick'
                  AND started_at >= %s::date
                  AND started_at <  (%s::date + INTERVAL '1 day')
                  AND result_json ? 'cost_usd'
                """,
                (today_iso, today_iso),
            )
            r = cur.fetchone()
            today_usd = float((r or {}).get("sum_usd") or 0)
    except Exception:
        today_usd = 0.0
    return {"today_usd": today_usd, "abort": today_usd > DAILY_CAP_USD}


_SYSTEM_PROMPT = """Voce e o CONSELHEIRO IA do Renato Almeida Prado — especialista em operacao de conselhos.

Renato participa de 4 conselhos:
- **Vallen Clinic** (pagante, conselheiro independente)
- **Alba Consultoria** (pro-bono, gate kill 30/09 — exige 1 acao/mes)
- **Associacao Despertar** (pro-bono, capital relacional Villela/Itausa)
- **Assespro-SP / ImensIAH** (canal imensIAH, papel observador — SEM RACI ConselhoOS)

Voce roda 1x/dia (manha). Lendo estado de cada conselho:
- RACI vencidos (ConselhoOS)
- Proxima/ultima reuniao
- Msgs WA grupo nas 72h
- Tasks INTEL vencidas

Sua MISSAO neste tick:
1. Identificar sinais que demandam acao OU dossie OU pauta OU alerta.
2. Quando proxima reuniao <=7d e sem pauta -> propor preparar pauta+dossie.
3. Quando >=3 RACI vencidos no mesmo conselho -> propor consolidacao/cobranca.
4. Quando ultima reuniao >60d sem proxima agendada -> alertar drift, propor agendar.
5. Quando msgs WA grupo trazem decisao/compromisso novo -> sugerir capturar em RACI/ata.

NUNCA invente sinais. Se nao ha nada novo, responda em texto curto (1-2 linhas)
sem chamar tool. Silencio e OK.

Quando precisar propor algo pro Renato, use **send_wa_to_renato** (tool) com:
- title curto e factual ("Alba — reuniao segunda 16/06, sem pauta")
- summary objetivo com numeros (quantos RACI, dias desde reuniao, etc)
- **options CONCRETAS, NUNCA genéricas** — diga a ACAO especifica em cada label:
  - RUIM: ["Aprovar", "Modificar", "Snooze", "Ignorar"]
  - BOM:  ["Manda WA pra Sandra propondo reagendar 23/06",
           "Cobra os 8 RACI vencidos no grupo Alba",
           "Snooze ate quarta",
           "Descartar"]
  - Renato precisa entender ANTES de clicar o que cada opcao faz.
  - Maximo 4 opcoes. Ultima sempre "Descartar" (catch-all silenciar).
- proposed_action {action, params}: JSON estruturado do que sera executado
  se Renato aprovar a opcao 1 (default).

Restricoes:
- **NAO PUBLIQUE pauta/dossie/ata sem revisao do Renato.** Sempre propor via WA.
- **Foque em conselhos onde voce TEM dados** (ConselhoOS empresa_id presente).
  Despertar tem 1 reuniao historica + sem grupo WA — atue mais conservador.
  Assespro nao tem RACI ConselhoOS — so olhe tasks INTEL.
- **Consolide** — se 2 conselhos precisam alerta semelhante, mande 1 msg unica.
- **Politica Domingo Sagrado**: nao propor acoes pra domingo, nao alertar no domingo.
"""


SENSOR_TOOLS = [
    {
        "name": "send_wa_to_renato",
        "description": (
            "Manda proposta conversacional pro Renato via WA (0192 -> 3337). "
            "Use quando detectar sinal que precisa decisao/acao. Sempre inclui "
            "options [Aprovar/Modificar/Snooze/Ignorar] e proposed_action JSON."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Titulo curto factual."},
                "summary": {"type": "string", "description": "Contexto factual com numeros, evidencia, sem especular."},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "action_hint": {"type": "string"},
                        },
                        "required": ["label"],
                    },
                },
                "urgency": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "default": "medium",
                },
                "proposed_action": {
                    "type": "object",
                    "description": "JSON da acao concreta caso Renato aprovar.",
                },
                "conselho_slug": {
                    "type": "string",
                    "enum": ["vallen", "alba", "despertar", "assespro"],
                },
            },
            "required": ["title", "summary"],
        },
    },
]


def _exec_tool(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if name != "send_wa_to_renato":
        return {"success": False, "error": f"tool desconhecida: {name}"}
    # Reusa o tool do cos_sensor — mesmo formato bot_conversations cos_patrol
    try:
        from services.cos_sensor import _tool_send_wa_to_renato
        # Marca origem CONSELHEIRO no proposed_action
        pa = dict(params.get("proposed_action") or {})
        pa.setdefault("agent", "cos_conselheiro")
        if params.get("conselho_slug"):
            pa["conselho_slug"] = params["conselho_slug"]
        return _tool_send_wa_to_renato(
            title=params.get("title", ""),
            summary=params.get("summary", ""),
            options=params.get("options"),
            urgency=params.get("urgency", "medium"),
            contact_id=None,
            context_link=f"conselheiro:{params.get('conselho_slug','')}",
            proposed_action=pa,
            agent_label="CoS Conselheiro",
        )
    except Exception as e:
        logger.exception(f"_exec_tool send_wa_to_renato falhou: {e}")
        return {"success": False, "error": str(e)}


def tick() -> Dict[str, Any]:
    """1 tick do CONSELHEIRO."""
    started = time.time()
    budget = _check_budget()
    if budget["abort"]:
        return {"status": "aborted_budget", "today_usd": budget["today_usd"]}

    if not ANTHROPIC_API_KEY:
        return {"status": "skipped", "reason": "no_api_key"}
    try:
        import anthropic
    except ImportError:
        return {"status": "error", "reason": "anthropic_sdk_missing"}

    # Carrega estado de cada conselho
    estados = []
    for c in CONSELHOS:
        try:
            estados.append(_load_conselho_state(c))
        except Exception as e:
            logger.exception(f"load_conselho_state falhou {c['slug']}: {e}")

    now_brt_iso = to_brt(now_utc()).isoformat()

    user_prompt = (
        f"Tick CONSELHEIRO em {now_brt_iso}.\n\n"
        f"Estado dos 4 conselhos:\n```json\n"
        f"{json.dumps(estados, default=str, ensure_ascii=False, indent=2)[:12000]}\n"
        f"```\n\n"
        f"Analise e decida: ha sinal acionavel HOJE? Se sim, chame send_wa_to_renato. "
        f"Se nao, responda 1-2 linhas indicando que esta tudo OK."
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_param = [
        {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    total_input = total_output = cache_creation = cache_read = 0
    tool_calls_log: List[Dict[str, Any]] = []
    final_text = ""
    last_stop_reason = None
    error_msg = None

    try:
        for it in range(MAX_ITERATIONS):
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS_PER_ITER,
                system=system_param,
                tools=SENSOR_TOOLS,
                messages=messages,
            )
            usage = resp.usage
            total_input += getattr(usage, "input_tokens", 0) or 0
            total_output += getattr(usage, "output_tokens", 0) or 0
            cache_creation += getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            last_stop_reason = resp.stop_reason

            messages.append({"role": "assistant", "content": resp.content})

            tu_blocks = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    txt = (getattr(b, "text", "") or "").strip()
                    if txt:
                        final_text = txt

            if resp.stop_reason != "tool_use" or not tu_blocks:
                break

            tool_results = []
            for tu in tu_blocks:
                tname = getattr(tu, "name", "")
                tin = dict(getattr(tu, "input", {}) or {})
                tid = getattr(tu, "id", "")
                tres = _exec_tool(tname, tin)
                tool_calls_log.append({"tool": tname, "input": tin, "result": tres})
                result_str = json.dumps(tres, default=str, ensure_ascii=False)[:3000]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": result_str,
                })
            messages.append({"role": "user", "content": tool_results})

    except Exception as e:
        error_msg = f"loop_failed: {e}"
        logger.exception("cos_conselheiro.tick loop falhou")

    duration_ms = int((time.time() - started) * 1000)
    cost_usd = (
        (total_input * 3.0 / 1_000_000)
        + (total_output * 15.0 / 1_000_000)
        + (cache_creation * 3.75 / 1_000_000)
        + (cache_read * 0.30 / 1_000_000)
    )

    return {
        "status": "error" if error_msg and not tool_calls_log else ("success" if not error_msg else "partial"),
        "tool_count": len(tool_calls_log),
        "tool_calls": tool_calls_log,
        "conselhos_analisados": [e["slug"] for e in estados],
        "tokens": {
            "input": total_input, "output": total_output,
            "cache_creation": cache_creation, "cache_read": cache_read,
        },
        "cost_usd": round(cost_usd, 4),
        "duration_ms": duration_ms,
        "last_stop_reason": last_stop_reason,
        "final_text": (final_text or "")[:500],
        "error_message": error_msg,
    }


def tick_safe() -> Dict[str, Any]:
    import traceback
    try:
        from services.audit_log import log as audit_log
    except Exception:
        audit_log = None

    try:
        result = tick()
        if audit_log:
            audit_log(
                "cos_conselheiro.tick",
                actor="cos_conselheiro",
                details={
                    "status": result.get("status"),
                    "tool_count": result.get("tool_count"),
                    "cost_usd": result.get("cost_usd"),
                    "conselhos": result.get("conselhos_analisados"),
                    "error": result.get("error_message"),
                },
            )
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception(f"cos_conselheiro.tick_safe crashed: {e}")
        if audit_log:
            audit_log(
                "cos_conselheiro.tick_error",
                actor="cos_conselheiro",
                details={"error": str(e), "type": type(e).__name__, "traceback": tb[:3000]},
            )
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": tb.splitlines()[-12:],
        }
