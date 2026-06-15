"""
CoS PORTFOLIO Agent — drift detector + revisão estratégica (15/06/26).

Specialist #2 do swarm CoS. Roda 24h, lê estado de TODOS os projetos INTEL
ativos, cruza com pesos das 5 frentes ratificadas (memory cos_config) +
gates ativos (Alba kill 30/09), e propõe via WA: cobrar/snooze/kill/promover.

Diferença vs CoS Conselheiro:
- CONSELHEIRO cobre 4 conselhos (Vallen, Alba, Despertar, Assespro) — leitura
  vertical profunda (RACI, atas, pauta, dossiê).
- PORTFOLIO cobre TODOS os projetos ativos — leitura horizontal de drift.
  Pra os 4 projetos de conselho, NAO duplica análise — só sinaliza "essa
  análise pertence ao CONSELHEIRO".

Política de autonomia:
- Apenas leitura + propor via send_wa_to_renato. Sem Auto.
- Cada proposta DEVE ter options concretas (regra herdada do CONSELHEIRO).

Budget cap: $0.50/dia.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 6
MAX_TOKENS_PER_ITER = 2500
DAILY_CAP_USD = float(os.getenv("COS_PORTFOLIO_DAILY_CAP_USD", "0.50"))

# Projetos que pertencem ao CONSELHEIRO — PORTFOLIO sinaliza mas nao analisa profundo
CONSELHEIRO_PROJECT_IDS = {24, 26, 25, 12}  # Vallen, Alba, Despertar, Assespro

# Frentes ratificadas (memory project_business_landscape + cos_config v5 11/06)
FRENTES_INFO = """
Frentes estratégicas (peso ratificado 11/06):
- Frente 1 (30%): imensIAH (Assespro canal, ICP founder PME) — aposta principal
- Frente 2 (20%): ConselhoOS (Vallen pagante + Wadhwani canal aquisição)
- Frente 3: Wadhwani Foundation (canal indireto, decisão Venture Partner pendente)
- Frente 4 (15%): Vallen Clinic direto (operacional contínuo)
- Frente 5 (5%): Despertar + Villela (capital relacional Itaúsa)

Gates ativos:
- Alba Consultoria: kill date 30/09 — exige 1 ação proativa/mês ate la
- imensIAH tier gratuito: decisão estratégica em drift (>17d)
- Wadhwani: decisão Venture Partner pendente (>17d)
"""


def _load_portfolio_state() -> Dict[str, Any]:
    """Carrega estado de todos os projetos ativos + pausados."""
    state: Dict[str, Any] = {"ativos": [], "pausados": [], "summary": {}}
    now = datetime.now()
    cutoff_30d = now - timedelta(days=30)
    cutoff_14d = now - timedelta(days=14)

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT p.id, p.nome, p.tipo, p.status, p.prioridade,
                       p.atualizado_em::date AS proj_atualizado,
                       p.owner_contact_id, c.nome as owner_nome,
                       (SELECT MAX(atualizado_em)::date FROM tasks WHERE project_id=p.id) AS ultima_task_update,
                       (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND status IN ('pending','delegated')) AS tasks_abertas,
                       (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND status='pending' AND data_vencimento < NOW()) AS tasks_vencidas,
                       (SELECT MIN(data_vencimento)::date FROM tasks WHERE project_id=p.id AND status='pending' AND data_vencimento < NOW()) AS task_mais_vencida
                FROM projects p
                LEFT JOIN contacts c ON c.id = p.owner_contact_id
                WHERE p.status IN ('ativo', 'pausado')
                ORDER BY p.status, p.prioridade NULLS LAST, p.id
                """
            )
            rows = cur.fetchall()
            for r in rows:
                pid = r["id"]
                is_conselheiro = pid in CONSELHEIRO_PROJECT_IDS
                proj = {
                    "id": pid,
                    "nome": r["nome"],
                    "tipo": r["tipo"],
                    "status": r["status"],
                    "prioridade": r["prioridade"],
                    "owner_contact_id": r["owner_contact_id"],
                    "owner_nome": r["owner_nome"],
                    "proj_atualizado": r["proj_atualizado"].isoformat() if r["proj_atualizado"] else None,
                    "dias_sem_atualizar": (now.date() - r["proj_atualizado"]).days if r["proj_atualizado"] else None,
                    "ultima_task_update": r["ultima_task_update"].isoformat() if r["ultima_task_update"] else None,
                    "tasks_abertas": r["tasks_abertas"],
                    "tasks_vencidas": r["tasks_vencidas"],
                    "task_mais_vencida": r["task_mais_vencida"].isoformat() if r["task_mais_vencida"] else None,
                    "is_coberto_conselheiro": is_conselheiro,
                }
                if r["status"] == "ativo":
                    state["ativos"].append(proj)
                else:
                    state["pausados"].append(proj)

            # Drift snapshot: projetos que precisam atenção
            drift_alta = [
                p for p in state["ativos"]
                if not p["is_coberto_conselheiro"] and (
                    (p["tasks_vencidas"] or 0) > 0
                    or (p["dias_sem_atualizar"] or 0) > 30
                )
            ]
            sem_tasks = [
                p for p in state["ativos"]
                if not p["is_coberto_conselheiro"] and (p["tasks_abertas"] or 0) == 0
            ]
            sobrecarga = [
                p for p in state["ativos"]
                if (p["tasks_abertas"] or 0) >= 15
            ]
            state["summary"] = {
                "total_ativos": len(state["ativos"]),
                "total_pausados": len(state["pausados"]),
                "drift_alta_count": len(drift_alta),
                "drift_alta": drift_alta[:10],
                "sem_tasks_abertas_count": len(sem_tasks),
                "sem_tasks_abertas": [{"id": p["id"], "nome": p["nome"]} for p in sem_tasks[:8]],
                "sobrecarga_count": len(sobrecarga),
                "sobrecarga": [{"id": p["id"], "nome": p["nome"], "tasks": p["tasks_abertas"]} for p in sobrecarga],
            }

            # Últimas propostas Portfolio nas 48h pra dedup
            cur.execute(
                """
                SELECT id, content, tool_calls, created_at
                FROM bot_conversations
                WHERE role='assistant'
                  AND tool_calls->>'agent_label' = 'CoS Portfolio'
                  AND created_at > NOW() - INTERVAL '48 hours'
                ORDER BY created_at DESC LIMIT 10
                """
            )
            state["recent_portfolio_pushes"] = [
                {
                    "id": r["id"],
                    "content_preview": (r["content"] or "")[:300],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in cur.fetchall()
            ]
    except Exception as e:
        logger.exception(f"_load_portfolio_state falhou: {e}")
        state["erro"] = str(e)[:300]

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
                WHERE path = '/api/cron/cos-portfolio-tick'
                  AND started_at >= %s::date
                  AND started_at < (%s::date + INTERVAL '1 day')
                  AND result_json ? 'cost_usd'
                """,
                (today_iso, today_iso),
            )
            r = cur.fetchone()
            today_usd = float((r or {}).get("sum_usd") or 0)
    except Exception:
        today_usd = 0.0
    return {"today_usd": today_usd, "abort": today_usd > DAILY_CAP_USD}


_SYSTEM_PROMPT = """Voce e o CoS Portfolio do Renato Almeida Prado — specialist de drift detector e revisao estrategica de portfolio.

Voce roda 24h. Le estado de TODOS os projetos INTEL ativos + pausados.

==== FRENTES ESTRATEGICAS ====

""" + FRENTES_INFO + """

==== ESCOPO ====

Voce cobre TODOS os projetos ativos do INTEL — visao horizontal.

NAO DUPLICA com cos_conselheiro:
- Vallen Clinic (24), Alba Consultoria (26), Associação Despertar (25),
  Assespro-SP (12) sao cobertos pelo CONSELHEIRO. Voce SO sinaliza
  "esse pertence ao CONSELHEIRO" — nao analisa RACI/atas/pauta deles.

Foco em:
- Projetos com >30d sem atualizado_em -> propor: ainda ativo? snooze/kill/cobrar
- Projetos com tasks vencidas (não-conselho) -> propor: cobrar quem, mover prazo, criar follow-up
- Projetos com 0 tasks abertas (não-conselho) -> alerta orfao: ainda ativo?
- Projetos com >=15 tasks abertas -> alerta sobrecarga: priorizar / dividir
- Gate Alba 30/09 — verificar se houve 1 acao registrada no mes
- Pausados — sugerir reativar se virou prioridade ou matar definitivamente

==== POLITICA DE PROPOSTAS ====

Use **send_wa_to_renato** com:
- title curto factual
- summary objetivo com numeros
- **options CONCRETAS** (NAO genericas):
  RUIM: ["Aprovar", "Modificar", "Snooze", "Ignorar"]
  BOM:  ["Cobrar DAP por status do tier gratuito",
         "Mover prazo da #398 pra 30/06",
         "Snooze ate quarta",
         "Descartar"]
- Maximo 4 opcoes. Ultima sempre catch-all silenciar.
- proposed_action {action, params}: JSON do que sera executado se opcao 1 aprovada

DEDUP:
- Veja recent_portfolio_pushes — se ja avisou nas 48h, NAO repita o mesmo sinal.
- Consolide: 2-3 projetos com drift similar viram UMA mensagem.
- Maximo 2 mensagens por tick (rajada irrita).

POLITICA DOMINGO: nao propor acoes pra domingo (C2 sagrado).

==== FECHAMENTO ====

Se nada novo/relevante, responda em texto curto (1-2 linhas) sem chamar tool.
Silencio e OK.
"""


SENSOR_TOOLS = [
    {
        "name": "send_wa_to_renato",
        "description": (
            "Manda proposta conversacional pro Renato via WA (0192 -> 3337). "
            "Use quando detectar sinal drift acionavel. SEMPRE inclui options "
            "CONCRETAS (nao genericas) e proposed_action JSON."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
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
                    "description": "JSON da acao a executar se Renato aprovar opcao 1",
                },
                "project_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "IDs dos projetos INTEL envolvidos na proposta",
                },
            },
            "required": ["title", "summary"],
        },
    },
]


def _exec_tool(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if name != "send_wa_to_renato":
        return {"success": False, "error": f"tool desconhecida: {name}"}
    try:
        from services.cos_sensor import _tool_send_wa_to_renato
        pa = dict(params.get("proposed_action") or {})
        pa.setdefault("agent", "cos_portfolio")
        if params.get("project_ids"):
            pa["project_ids"] = params["project_ids"]
        return _tool_send_wa_to_renato(
            title=params.get("title", ""),
            summary=params.get("summary", ""),
            options=params.get("options"),
            urgency=params.get("urgency", "medium"),
            contact_id=None,
            context_link="portfolio",
            proposed_action=pa,
            agent_label="CoS Portfolio",
        )
    except Exception as e:
        logger.exception(f"_exec_tool send_wa_to_renato failed: {e}")
        return {"success": False, "error": str(e)}


def tick() -> Dict[str, Any]:
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

    state = _load_portfolio_state()
    now_brt_iso = to_brt(now_utc()).isoformat()

    user_prompt = (
        f"Tick CoS Portfolio em {now_brt_iso}.\n\n"
        f"Estado do portfolio (resumo + drift candidates):\n```json\n"
        f"{json.dumps(state, default=str, ensure_ascii=False, indent=2)[:14000]}\n"
        f"```\n\n"
        f"Analise drift e proponha acao SE ha sinal novo. Lembre dedup contra recent_portfolio_pushes. "
        f"Max 2 mensagens. Se nada acionavel, responda 1-2 linhas."
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
        for _ in range(MAX_ITERATIONS):
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
        logger.exception("cos_portfolio.tick loop falhou")

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
        "portfolio_summary": state.get("summary"),
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
                "cos_portfolio.tick",
                actor="cos_portfolio",
                details={
                    "status": result.get("status"),
                    "tool_count": result.get("tool_count"),
                    "cost_usd": result.get("cost_usd"),
                    "summary": result.get("portfolio_summary"),
                    "error": result.get("error_message"),
                },
            )
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception(f"cos_portfolio.tick_safe crashed: {e}")
        if audit_log:
            audit_log(
                "cos_portfolio.tick_error",
                actor="cos_portfolio",
                details={"error": str(e), "type": type(e).__name__, "traceback": tb[:3000]},
            )
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": tb.splitlines()[-12:],
        }
