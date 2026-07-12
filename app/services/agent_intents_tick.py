"""
Agent Intents Tick — P6 Diligente (Fase 2)

Cron 30min que tenta progredir intents abertos sozinho, sem esperar mensagem
do Renato. Pra cada intent open/in_progress:
- Pega contexto (intent + ultimas msgs do bot_conversation associado).
- Roda Claude com system focado em "voce tem 1 intent aberto: progride, trava
  com motivo, ou cancela se nao faz mais sentido".
- Se Claude chama tool de write -> intent vira in_progress + step.
- Se Claude responde texto sem tool -> marca como blocked com motivo.

Idempotencia: pula intents atualizados nos ultimos 10min (evita re-trabalho
quando bot acabou de fazer um turn).

Audit obrigatorio (per AUTONOMY_POLICY.md): cada tick chama log_action.

Memoria: project_inteligencia_real.md (P6 Diligente Fase 2).
"""
import json
from services import llm
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from database import get_db
from services.agent_intents import (
    escalate_blocked_intents,
    get_open_intents,
    update_intent,
    append_step,
    cancel_intent,
)

logger = logging.getLogger(__name__)


# ==================== CONFIG ====================

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
# Mesmo modelo do intel_bot pra consistencia. Haiku barato; tick e high-frequency.
CLAUDE_MODEL = os.getenv("INTEL_BOT_MODEL", llm.FAST)
# Loop limitado: max iteracoes por intent (1 tool call -> 1 follow-up text -> stop).
# Mantem token cost previsivel. 3 e suficiente: tool, observa resultado, decide.
MAX_TICK_ITERATIONS = 3
# Cap de intents por rodada (Vercel timeout 300s — cada tick ~5-10s c/ Claude).
# 20 cobre folga ate 100s. Renato pode subir se nunca atingir.
MAX_INTENTS_PER_RUN = 20
# Skip threshold: intent atualizado < N min atras = bot acabou de mexer; nao re-trabalha.
SKIP_IF_UPDATED_WITHIN_MINUTES = 10


# Tool subset que o tick pode chamar. NAO inclui send_email/send_whatsapp pra
# evitar bot mandar mensagem sem aprovacao explicita do Renato. Foco em
# progresso seguro: tarefas, notas, memorias, contatos.
_TICK_SAFE_ACTIONS = {
    "create_task", "complete_task", "update_task", "postpone_tasks",
    "save_note", "save_memory",
    "save_system_memory",
    "update_contact",
}


# ==================== CONTEXT BUILDER ====================

def _fetch_recent_bot_messages(related_message_id: Optional[int], limit: int = 5) -> List[Dict[str, Any]]:
    """Pega as 5 msgs ao redor do related_message_id pra dar contexto ao Claude.

    Se nao houver related_message_id, retorna [].
    """
    if not related_message_id:
        return []
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # Pega mensagens da MESMA conversa (mesmo phone), ordem cronologica,
            # incluindo ate 5 antes do related + ate 5 depois.
            cursor.execute(
                """
                SELECT phone FROM bot_conversations WHERE id = %s
                """,
                (related_message_id,),
            )
            row = cursor.fetchone()
            if not row:
                return []
            phone = row.get("phone") if isinstance(row, dict) else row[0]

            cursor.execute(
                """
                SELECT id, role, content, created_at
                FROM bot_conversations
                WHERE phone = %s AND id <= %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (phone, related_message_id, limit),
            )
            rows = cursor.fetchall() or []
        msgs = [dict(r) for r in rows]
        msgs.reverse()  # cronologico
        return msgs
    except Exception as e:
        logger.warning(f"_fetch_recent_bot_messages error id={related_message_id}: {e}")
        return []


def _format_context(intent: Dict[str, Any], recent_msgs: List[Dict[str, Any]]) -> str:
    """Constroi prompt textual do contexto do intent."""
    lines = [
        f"INTENT #{intent['id']} (status={intent.get('status')}):",
        f"  texto: {intent.get('intent_text', '')[:300]}",
    ]
    hint = intent.get("next_step_hint")
    blocker = intent.get("blocker")
    if hint:
        lines.append(f"  proximo passo sugerido: {hint[:200]}")
    if blocker:
        lines.append(f"  blocker atual: {blocker[:200]}")

    steps = intent.get("steps_done")
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception:
            steps = []
    if steps:
        lines.append(f"  passos ja feitos ({len(steps)}):")
        for s in steps[-5:]:
            if isinstance(s, dict):
                lines.append(f"    - {json.dumps(s, ensure_ascii=False)[:120]}")
            else:
                lines.append(f"    - {str(s)[:120]}")

    if recent_msgs:
        lines.append("\nULTIMAS MSGS DA CONVERSA:")
        for m in recent_msgs:
            role = m.get("role", "?")
            content = (m.get("content") or "").strip().replace("\n", " ")[:200]
            if content:
                lines.append(f"  [{role}] {content}")

    return "\n".join(lines)


# ==================== TOOL DEFS (subset) ====================

# Subset minimo das tools do intel_bot. Reaproveita _tool_execute_action
# direto pra evitar duplicar logica de write.
_TICK_TOOLS = [
    {
        "name": "execute_action",
        "description": (
            "Executa acao no INTEL pra progredir o intent. Acoes seguras: "
            "create_task, complete_task, update_task, postpone_tasks, "
            "save_note, save_memory, save_system_memory, update_contact. "
            "NAO ha send_email nem send_whatsapp aqui — esses precisam aprovacao do user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_TICK_SAFE_ACTIONS),
                },
                "params": {
                    "type": "object",
                    "description": "Parametros da acao (variam por acao)",
                },
            },
            "required": ["action", "params"],
        },
    },
    {
        "name": "manage_intent",
        "description": (
            "Gerencia o estado do intent atual. Use quando: progresso real (mark_step), "
            "travado por motivo concreto (mark_blocked), tudo concluido (mark_completed), "
            "ou intent perdeu sentido (cancel)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["mark_step", "mark_blocked", "mark_completed", "cancel"],
                },
                "details": {
                    "type": "string",
                    "description": "Descricao do passo / motivo do bloqueio. Obrigatorio pra mark_step e mark_blocked.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "query_intel",
        "description": "SQL READ-ONLY no INTEL pra entender estado antes de agir (max 20 linhas).",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
            },
            "required": ["sql"],
        },
    },
]


# ==================== CLAUDE CALL ====================

async def _call_claude_with_tools(
    system_prompt: str,
    user_prompt: str,
    intent_id: int,
) -> Dict[str, Any]:
    """Chama Claude com tools. Retorna {tool_calls: [...], final_text: str, stop_reason}.

    Loop interno: ate MAX_TICK_ITERATIONS. Cada tool_use e resolvido + result
    devolvido pro Claude.
    """
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY nao configurada", "tool_calls": [], "final_text": ""}

    # Imports locais — evita ciclo + lazy loading.
    from services.intel_bot import _tool_execute_action, _tool_query_intel  # noqa: WPS433

    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    tool_calls_log: List[Dict[str, Any]] = []
    final_text = ""
    stop_reason = None
    manage_intent_request: Optional[Dict[str, Any]] = None
    turn_text = ""  # acumulado do ultimo turn — usado se loop esgotar sem stop natural

    try:
        async with httpx.AsyncClient(timeout=55.0) as client:
            for iteration in range(MAX_TICK_ITERATIONS):
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": 1024,
                        "system": system_prompt,
                        "tools": _TICK_TOOLS,
                        "messages": messages,
                    },
                )
                if response.status_code != 200:
                    err = response.text[:500]
                    logger.error(f"tick claude call failed intent={intent_id} status={response.status_code}: {err}")
                    return {"error": f"claude_http_{response.status_code}", "tool_calls": tool_calls_log, "final_text": ""}

                data = response.json()
                stop_reason = data.get("stop_reason")
                content_blocks = data.get("content") or []

                # Coleta texto + tool_uses (reset turn_text a cada iteracao)
                tool_uses = []
                turn_text = ""  # noqa: F823 — reset proposital pra contexto do block
                for block in content_blocks:
                    btype = block.get("type")
                    if btype == "text":
                        turn_text += block.get("text", "")
                    elif btype == "tool_use":
                        tool_uses.append(block)

                if not tool_uses:
                    final_text = turn_text.strip()
                    break

                # Resolve tool_uses + manda resultado de volta
                # Adiciona assistant turn (com tool_use blocks brutos) na conversa
                messages.append({"role": "assistant", "content": content_blocks})
                tool_results = []
                for tu in tool_uses:
                    name = tu.get("name")
                    tu_input = tu.get("input") or {}
                    tu_id = tu.get("id")
                    result_str = ""
                    try:
                        if name == "execute_action":
                            action = tu_input.get("action", "")
                            if action not in _TICK_SAFE_ACTIONS:
                                result_str = json.dumps({"erro": f"acao '{action}' nao permitida no tick (use manage_intent ou query_intel)"}, ensure_ascii=False)
                            else:
                                result_str = await _tool_execute_action(action, tu_input.get("params", {}))
                        elif name == "manage_intent":
                            # Capturado: o tick decide o que fazer com base nisso DEPOIS do loop
                            # (e mais barato gerenciar o intent fora pra reusar audit).
                            manage_intent_request = {
                                "action": tu_input.get("action"),
                                "details": tu_input.get("details") or "",
                            }
                            result_str = json.dumps({"sucesso": True, "mensagem": "intent management registrado"}, ensure_ascii=False)
                        elif name == "query_intel":
                            result_str = _tool_query_intel(tu_input.get("sql", ""))
                        else:
                            result_str = json.dumps({"erro": f"tool desconhecida: {name}"})
                    except Exception as e:
                        logger.error(f"tick tool {name} error intent={intent_id}: {e}")
                        result_str = json.dumps({"erro": str(e)}, ensure_ascii=False)

                    tool_calls_log.append({"name": name, "input": tu_input, "result": result_str[:500]})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu_id,
                        "content": result_str[:2000],
                    })

                messages.append({"role": "user", "content": tool_results})
                # Continua loop ate Claude parar de chamar tools

            else:
                # Loop esgotou sem stop natural — pega texto da ultima iter
                final_text = (turn_text or "").strip()

    except httpx.TimeoutException:
        logger.error(f"tick claude timeout intent={intent_id}")
        return {"error": "timeout", "tool_calls": tool_calls_log, "final_text": ""}
    except Exception as e:
        logger.error(f"tick claude exception intent={intent_id}: {e}")
        return {"error": str(e), "tool_calls": tool_calls_log, "final_text": ""}

    return {
        "tool_calls": tool_calls_log,
        "final_text": final_text,
        "stop_reason": stop_reason,
        "manage_intent_request": manage_intent_request,
    }


# ==================== TICK CORE ====================

def _was_recently_updated(intent: Dict[str, Any]) -> bool:
    """Idempotencia: skip se intent foi atualizado nos ultimos N min."""
    from datetime import datetime, timezone, timedelta
    ts = intent.get("updated_at")
    if not ts:
        return False
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return False
    # updated_at vem em UTC (default do schema). Compara em UTC naive.
    if ts.tzinfo is None:
        ts_utc = ts
        now_utc = datetime.utcnow()
    else:
        ts_utc = ts.astimezone(timezone.utc).replace(tzinfo=None)
        now_utc = datetime.utcnow()
    delta = now_utc - ts_utc
    return delta < timedelta(minutes=SKIP_IF_UPDATED_WITHIN_MINUTES)


async def tick_one(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Processa 1 intent. Retorna resumo do que foi feito.

    Casos:
    - Intent ja blocked -> noop (escalation cuida)
    - Recently updated -> skip
    - Claude chama tool de write -> append_step com summary
    - Claude chama manage_intent -> aplica direto
    - Claude responde texto sem tool -> marca como blocked com motivo
    """
    intent_id = intent.get("id")
    status = intent.get("status")

    # Skip ja resolvidos (defensivo — get_open_intents ja filtra)
    if status not in ("open", "in_progress"):
        return {"intent_id": intent_id, "action_taken": "noop", "success": True, "reason": f"status={status}"}

    if _was_recently_updated(intent):
        return {"intent_id": intent_id, "action_taken": "skip", "success": True, "reason": "updated_recently"}

    # Audit ja na entrada (per AUTONOMY_POLICY.md)
    try:
        from services.agent_actions import log_action
        log_action(
            action_type="agent_intent.tick",
            category="system",
            title=f"Tick em intent #{intent_id}",
            details=f"Status: {status}. Texto: {(intent.get('intent_text') or '')[:120]}",
            scope_ref={"intent_id": intent_id},
            source="agent_intents_tick",
        )
    except Exception as e:
        logger.warning(f"tick log_action failed: {e}")

    recent = _fetch_recent_bot_messages(intent.get("related_message_id"))
    context_block = _format_context(intent, recent)

    system_prompt = (
        "Voce e o INTEL — agente que cumpre palavra (P6 Diligente). Voce tem UM intent aberto. "
        "Sua missao agora: tentar progredir esse intent autonomamente.\n\n"
        "Regras:\n"
        "1. Se ainda da pra avancar, chame execute_action com a tool apropriada (so create_task, "
        "complete_task, update_task, postpone_tasks, save_note, save_memory, save_system_memory, update_contact).\n"
        "2. Se voce avancou, chame manage_intent action='mark_step' com details descrevendo o passo.\n"
        "3. Se voce TERMINOU tudo, chame manage_intent action='mark_completed'.\n"
        "4. Se NAO consegue avancar (falta dado, falta tool, depende do user), chame manage_intent "
        "action='mark_blocked' com details explicando o motivo concreto.\n"
        "5. Se o intent perdeu sentido (ja foi feito por outra via, ou esta velho demais), chame "
        "manage_intent action='cancel'.\n"
        "6. NAO faca pergunta ao user — voce esta rodando em background sem ele.\n"
        "7. Use query_intel se precisar conferir estado antes de agir.\n"
        "8. Se nao tiver certeza do que fazer, prefira mark_blocked com motivo a inventar acao."
    )

    user_prompt = f"{context_block}\n\nProgrida ou marque o estado do intent."

    result = await _call_claude_with_tools(system_prompt, user_prompt, intent_id)
    if result.get("error"):
        return {
            "intent_id": intent_id,
            "action_taken": "error",
            "success": False,
            "message": result.get("error"),
        }

    tool_calls = result.get("tool_calls") or []
    manage_req = result.get("manage_intent_request")
    final_text = result.get("final_text") or ""

    # Decide o efeito final no intent baseado em manage_intent + tool_calls.
    if manage_req:
        m_action = manage_req.get("action")
        m_details = (manage_req.get("details") or "").strip()
        try:
            if m_action == "mark_step":
                step = {"kind": "auto_tick_step", "details": m_details[:300] or "(sem detalhes)"}
                append_step(intent_id, step, status="in_progress")
                return {"intent_id": intent_id, "action_taken": "mark_step", "success": True, "message": m_details[:120]}
            elif m_action == "mark_blocked":
                update_intent(intent_id, status="blocked", blocker=m_details[:300] or "(sem motivo)")
                return {"intent_id": intent_id, "action_taken": "mark_blocked", "success": True, "message": m_details[:120]}
            elif m_action == "mark_completed":
                update_intent(intent_id, status="completed")
                return {"intent_id": intent_id, "action_taken": "mark_completed", "success": True, "message": m_details[:120] or "completo"}
            elif m_action == "cancel":
                cancel_intent(intent_id)
                return {"intent_id": intent_id, "action_taken": "cancel", "success": True, "message": m_details[:120] or "cancelado"}
        except Exception as e:
            logger.error(f"tick manage_intent apply failed id={intent_id}: {e}")
            return {"intent_id": intent_id, "action_taken": "error", "success": False, "message": str(e)}

    # Sem manage_intent: ve se houve write tool com sucesso pra registrar como step
    write_calls = [
        tc for tc in tool_calls
        if tc.get("name") == "execute_action"
        and (tc.get("input") or {}).get("action") in _TICK_SAFE_ACTIONS
        and "erro" not in (tc.get("result") or "").lower()[:50]  # heuristica
    ]
    if write_calls:
        # Registra como step
        try:
            actions = [(tc.get("input") or {}).get("action") for tc in write_calls]
            step = {"kind": "auto_tick_action", "actions": actions, "result_excerpt": (write_calls[0].get("result") or "")[:200]}
            append_step(intent_id, step, status="in_progress")
            return {
                "intent_id": intent_id,
                "action_taken": "auto_progress",
                "success": True,
                "message": f"executou {len(write_calls)} acao(es): {', '.join(actions)}",
            }
        except Exception as e:
            logger.error(f"tick append_step failed id={intent_id}: {e}")

    # Nem manage_intent nem write -> bot so respondeu texto. Marca blocked.
    blocker_text = final_text[:300].strip() or "Bot nao chamou tool nem mark_blocked — resposta vazia."
    try:
        update_intent(intent_id, status="blocked", blocker=blocker_text)
        return {
            "intent_id": intent_id,
            "action_taken": "auto_blocked",
            "success": True,
            "message": blocker_text[:120],
        }
    except Exception as e:
        logger.error(f"tick auto_blocked update failed id={intent_id}: {e}")
        return {"intent_id": intent_id, "action_taken": "error", "success": False, "message": str(e)}


async def tick_all(max_intents: int = MAX_INTENTS_PER_RUN) -> Dict[str, Any]:
    """Orquestrador: pega intents abertos + tick_one em sequencia + escala blocked.

    Sequencial (nao paralelo): economiza tokens + evita race em intent.
    """
    intents = get_open_intents(limit=max_intents)
    results: List[Dict[str, Any]] = []
    for intent in intents:
        try:
            r = await tick_one(intent)
        except Exception as e:
            logger.error(f"tick_all tick_one exception id={intent.get('id')}: {e}")
            r = {"intent_id": intent.get("id"), "action_taken": "error", "success": False, "message": str(e)}
        results.append(r)

    # Escala blocked velhos depois de tentar progredir
    try:
        escalated = await escalate_blocked_intents()
    except Exception as e:
        logger.error(f"tick_all escalate_blocked_intents exception: {e}")
        escalated = []

    # Sumario por action_taken
    summary: Dict[str, int] = {}
    for r in results:
        a = r.get("action_taken", "unknown")
        summary[a] = summary.get(a, 0) + 1

    return {
        "ticked": len(results),
        "summary": summary,
        "escalated_count": len(escalated),
        "escalated_ids": [e["id"] for e in escalated],
        "results": results[:10],  # sample pro response
    }
