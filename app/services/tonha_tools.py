"""
Tonha Tools — Fase 2 da rebuild.

Catalogo de tools que a Tonha (Sonnet 4.6) pode chamar.
5 tools minimos viaveis. Pode expandir depois conforme estabilizar.

Todos os tools respeitam TONHA_SHADOW_MODE=1 (default ON em Fase 2A):
- send_message: salva como draft, NAO envia
- update_record: loga + retorna sucesso, NAO executa UPDATE
- delegate: cria row em delegations + draft mensagem, NAO envia
- search_context + decide_and_log + resolve_signal: sempre executam (sao read/log)

Ver docs/ARCHITECTURE_REBUILD.md sec 4 (camada 3).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import now_utc

logger = logging.getLogger(__name__)


def _shadow_mode() -> bool:
    return (os.getenv("TONHA_SHADOW_MODE") or "1").strip() != "0"


# ============================================================================
# Tool schemas (formato Anthropic tools)
# ============================================================================

TOOLS = [
    {
        "name": "search_context",
        "description": (
            "Busca contexto no INTEL CRM: contacts, projects, tasks, signals abertos. "
            "Use quando precisa saber QUEM e algum contato (cargo, empresa, tier), "
            "QUE projeto esta ativo, QUAIS signals relacionados a um topico, "
            "ou ESTADO atual de algo. "
            "Retorna estruturado. NAO inventa — se nao achar, retorna empty."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["contacts", "projects", "tasks", "signals", "delegations", "calendar", "whatsapp", "all"],
                    "description": "O que buscar. 'calendar' = eventos proximos 14d. 'whatsapp' = msgs WA (DM + grupos) ultimos 30d. 'all' = broad.",
                },
                "query": {
                    "type": "string",
                    "description": "Termo de busca. Aceita nome, palavra-chave, ID numerico, ou tipo de signal.",
                },
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["scope", "query"],
        },
    },
    {
        "name": "send_message",
        "description": (
            "Manda mensagem por WA ou email. Em SHADOW MODE (default Fase 2A), salva como "
            "draft em vez de enviar — Renato revisa depois. Use pra: cobrar pendencia de delegado, "
            "rascunhar resposta a email, mensagem proativa pra contato esfriando. "
            "Lembre: regra de ouro do Renato e ser util sem ruido — so manda se realmente faz diferenca."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["whatsapp", "email"]},
                "target": {
                    "type": "string",
                    "description": "WA: contact_id (int) ou group_jid. Email: email address.",
                },
                "subject": {"type": "string", "description": "So pra email."},
                "content": {"type": "string"},
                "force_send": {
                    "type": "boolean",
                    "default": False,
                    "description": "Bypass shadow mode. Use SO em emergencia clara (financial alerta 95%+, conflito de agenda urgente). Auditado.",
                },
            },
            "required": ["channel", "target", "content"],
        },
    },
    {
        "name": "update_record",
        "description": (
            "Atualiza campo de tabela. Em SHADOW MODE, loga sem executar. Use pra: "
            "marcar task como completed quando ja foi feita, ajustar prioridade de projeto, "
            "atualizar status de delegation. Whitelist de tabelas: tasks, projects, "
            "delegations, signals, weekly_raci_renato."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "enum": ["tasks", "projects", "delegations", "signals", "weekly_raci_renato"],
                },
                "id": {"type": "integer"},
                "fields": {
                    "type": "object",
                    "description": "Dict com campos a atualizar. Ex: {'status': 'completed', 'data_conclusao': '2026-06-15'}",
                },
            },
            "required": ["table", "id", "fields"],
        },
    },
    {
        "name": "delegate",
        "description": (
            "Delega tarefa pra time humano (Andressa, Joao Piccino advogado, Priscila contadora) "
            "ou interno (dev=Claude Code, evaluator=auto-review, collector=auto-cobranca). "
            "Cria row em `delegations` + rascunha mensagem de delegacao. Em SHADOW, msg vira draft. "
            "Use pra delegar AGORA — se nao tem deadline, nao delega."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "enum": ["andressa", "joao_piccino", "priscila_contadora", "dev", "evaluator", "collector"],
                },
                "task_summary": {"type": "string", "description": "Uma linha do que precisa ser feito."},
                "task_full": {"type": "string", "description": "Contexto completo + instrucao."},
                "deadline": {"type": "string", "description": "ISO date YYYY-MM-DD. Obrigatorio."},
                "contact_id": {"type": "integer", "description": "Quando aplicavel."},
                "signal_id": {"type": "integer"},
            },
            "required": ["to", "task_summary", "task_full", "deadline"],
        },
    },
    {
        "name": "manage_calendar_event",
        "description": (
            "Cancela ou apaga um evento do calendar. Em SHADOW MODE, salva como draft de cancelamento "
            "(nao apaga real). Real: chama Google Calendar API + remove da tabela local. "
            "Use quando Renato dizer 'deleta a reuniao X', 'cancela tal evento', 'nao vou participar'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "id local do calendar_events"},
                "action": {
                    "type": "string",
                    "enum": ["delete", "cancel"],
                    "description": "'delete' apaga do Google + local. 'cancel' marca status='cancelled' apenas (mantem registro).",
                },
                "scope": {
                    "type": "string",
                    "enum": ["single", "future", "all"],
                    "default": "single",
                    "description": "Pra eventos recorrentes. Default 'single'.",
                },
                "reason": {"type": "string", "description": "Por que cancelou — vai no audit log."},
            },
            "required": ["event_id", "action"],
        },
    },
    {
        "name": "decide_and_log",
        "description": (
            "MANDATORIO em modo autonomous — registra cada decisao tomada sobre um signal. "
            "Tipos: auto_execute (agi sozinho), draft_and_send (rascunhei e enviei/draftei), "
            "escalate (mando msg pro Renato), silence (decidi nao fazer nada — ruido). "
            "Apos decide_and_log, marca o signal como resolved/dismissed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signal_id": {"type": "integer"},
                "decision_type": {
                    "type": "string",
                    "enum": ["auto_execute", "draft_and_send", "escalate", "silence", "delegate"],
                },
                "summary": {"type": "string", "description": "Uma linha do que decidi."},
                "reasoning": {"type": "string", "description": "Pensamento que levou a decisao (300 chars)."},
                "action_taken": {
                    "type": "object",
                    "description": "Ex: {'tool': 'send_message', 'draft_id': 42}",
                },
                "new_signal_status": {
                    "type": "string",
                    "enum": ["resolved", "dismissed"],
                    "default": "resolved",
                },
            },
            "required": ["signal_id", "decision_type", "summary"],
        },
    },
]


# ============================================================================
# Tool dispatchers
# ============================================================================

def dispatch(name: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Roteia chamada de tool. ctx tem mode/triggered_by/iteration."""
    try:
        if name == "search_context":
            return _tool_search_context(**params)
        if name == "send_message":
            return _tool_send_message(ctx=ctx, **params)
        if name == "update_record":
            return _tool_update_record(ctx=ctx, **params)
        if name == "delegate":
            return _tool_delegate(ctx=ctx, **params)
        if name == "decide_and_log":
            return _tool_decide_and_log(ctx=ctx, **params)
        if name == "manage_calendar_event":
            return _tool_manage_calendar_event(ctx=ctx, **params)
        return {"ok": False, "error": f"tool '{name}' nao reconhecida"}
    except Exception as e:
        logger.exception(f"tool {name} crashed")
        return {"ok": False, "error": str(e)[:300]}


def _tool_search_context(scope: str, query: str, limit: int = 10) -> Dict[str, Any]:
    out: Dict[str, Any] = {"scope": scope, "query": query, "results": {}}
    with get_db() as conn:
        cur = conn.cursor()
        if scope in ("contacts", "all"):
            cur.execute("""
                SELECT id, nome, empresa, cargo, contexto, tags
                FROM contacts
                WHERE nome ILIKE %s OR empresa ILIKE %s OR apelido ILIKE %s
                ORDER BY ultimo_enriquecimento DESC NULLS LAST
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", f"%{query}%", limit))
            out["results"]["contacts"] = [dict(r) for r in cur.fetchall()]

        if scope in ("projects", "all"):
            cur.execute("""
                SELECT id, nome, tipo, status, prioridade, data_previsao, descricao
                FROM projects
                WHERE nome ILIKE %s OR descricao ILIKE %s
                ORDER BY prioridade ASC NULLS LAST, atualizado_em DESC
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", limit))
            out["results"]["projects"] = [dict(r) for r in cur.fetchall()]

        if scope in ("tasks", "all"):
            cur.execute("""
                SELECT id, titulo, status, prioridade, data_vencimento, project_id
                FROM tasks
                WHERE titulo ILIKE %s
                  AND status IN ('pending', 'in_progress')
                ORDER BY data_vencimento ASC NULLS LAST
                LIMIT %s
            """, (f"%{query}%", limit))
            out["results"]["tasks"] = [dict(r) for r in cur.fetchall()]

        if scope in ("signals", "all"):
            cur.execute("""
                SELECT id, tipo, urgencia, detector, contexto, criado_em
                FROM signals
                WHERE status = 'open'
                  AND (tipo ILIKE %s OR contexto::text ILIKE %s)
                ORDER BY urgencia DESC, criado_em DESC
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", limit))
            out["results"]["signals"] = [dict(r) for r in cur.fetchall()]

        if scope in ("delegations", "all"):
            cur.execute("""
                SELECT id, delegated_to, task_summary, deadline, status, contact_id
                FROM delegations
                WHERE status IN ('open', 'in_progress')
                  AND (task_summary ILIKE %s OR task_full ILIKE %s)
                ORDER BY deadline ASC NULLS LAST
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", limit))
            out["results"]["delegations"] = [dict(r) for r in cur.fetchall()]

        if scope in ("whatsapp", "all"):
            # DMs (whatsapp_messages) + grupos (group_messages) ultimos 30d.
            # Query pode bater no nome do contato OU no conteudo da msg.
            cur.execute("""
                SELECT 'dm' AS kind, wm.id, wm.contact_id, ct.nome AS contato_nome,
                       wm.direction, wm.content, wm.message_date AS ts
                FROM whatsapp_messages wm
                LEFT JOIN contacts ct ON ct.id = wm.contact_id
                WHERE wm.message_date > NOW() - INTERVAL '30 days'
                  AND (wm.content ILIKE %s OR ct.nome ILIKE %s)
                ORDER BY wm.message_date DESC
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", limit))
            dms = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT 'group' AS kind, gm.id, gm.group_jid, gm.sender_name,
                       gm.contact_id, gm.from_me, gm.content, gm.timestamp AS ts,
                       pwg.group_name
                FROM group_messages gm
                LEFT JOIN project_whatsapp_groups pwg ON pwg.group_jid = gm.group_jid
                WHERE gm.timestamp > NOW() - INTERVAL '30 days'
                  AND (gm.content ILIKE %s OR gm.sender_name ILIKE %s OR pwg.group_name ILIKE %s)
                ORDER BY gm.timestamp DESC
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", f"%{query}%", limit))
            groups = [dict(r) for r in cur.fetchall()]

            out["results"]["whatsapp"] = {"dms": dms, "groups": groups}

        if scope in ("calendar", "all"):
            # IMPORTANTE: calendar_events armazena datetime na timezone do campo
            # 'timezone' (geralmente America/Sao_Paulo direto, NAO em UTC). NAO
            # converter — retorna raw + label timezone pra Brain interpretar.
            cur.execute("""
                SELECT id, summary, location,
                       start_datetime AS start_raw,
                       end_datetime   AS end_raw,
                       timezone, all_day, status, conference_url, contact_id
                FROM calendar_events
                WHERE start_datetime > NOW() - INTERVAL '1 hour'
                  AND start_datetime < NOW() + INTERVAL '14 days'
                  AND status IN ('confirmed', 'tentative')
                  AND (summary ILIKE %s OR description ILIKE %s OR location ILIKE %s OR %s = '')
                ORDER BY start_datetime ASC
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", f"%{query}%", query, limit))
            out["results"]["calendar"] = [dict(r) for r in cur.fetchall()]
            # Inclui nota interpretativa pra Brain
            out["calendar_note"] = (
                "start_raw/end_raw estao na timezone do campo timezone (geralmente "
                "America/Sao_Paulo = BRT). NAO converter. Mostrar como-eh."
            )

    return {"ok": True, **out}


def _tool_send_message(
    *, channel: str, target: str, content: str, subject: str = "",
    force_send: bool = False, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    shadow = _shadow_mode() and not force_send
    record = {
        "channel": channel,
        "target": target,
        "subject": subject,
        "content": content[:3000],
        "shadow": shadow,
        "ctx_mode": ctx.get("mode"),
        "criado_em": now_utc().isoformat(),
    }
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tonha_decisions (decision_type, decision_summary, action_taken, mode, triggered_by)
            VALUES ('draft_and_send', %s, %s::jsonb, %s, %s)
            RETURNING id
        """, (
            f"{'shadow_draft' if shadow else 'sent'}: {channel} -> {target}",
            json.dumps(record),
            ctx.get("mode", "autonomous"),
            ctx.get("triggered_by", "cron_loop"),
        ))
        draft_id = cur.fetchone()["id"]
        conn.commit()

    if shadow:
        return {"ok": True, "shadow": True, "draft_id": draft_id, "message": "Salvo como draft. Renato revisa."}

    # Real send (so quando force_send ou shadow off)
    if channel == "whatsapp":
        from services.cos_tools import send_whatsapp as _send_wa
        sent = _send_wa(target, content)
        return {"ok": True, "shadow": False, "draft_id": draft_id, "send_result": sent}
    if channel == "email":
        return {"ok": False, "error": "email send ainda nao implementado fora de shadow"}

    return {"ok": False, "error": f"channel {channel} desconhecido"}


def _tool_update_record(
    *, table: str, id: int, fields: Dict[str, Any], ctx: Dict[str, Any]
) -> Dict[str, Any]:
    allowed = {"tasks", "projects", "delegations", "signals", "weekly_raci_renato"}
    if table not in allowed:
        return {"ok": False, "error": f"table {table} nao permitida"}
    shadow = _shadow_mode()
    record = {"table": table, "id": id, "fields": fields, "shadow": shadow}

    with get_db() as conn:
        cur = conn.cursor()
        if not shadow:
            cols = ", ".join(f"{k} = %s" for k in fields.keys())
            vals = list(fields.values()) + [id]
            cur.execute(f"UPDATE {table} SET {cols} WHERE id = %s", vals)
            record["rowcount"] = cur.rowcount

        cur.execute("""
            INSERT INTO tonha_decisions (decision_type, decision_summary, action_taken, mode, triggered_by)
            VALUES ('auto_execute', %s, %s::jsonb, %s, %s)
            RETURNING id
        """, (
            f"{'shadow_update' if shadow else 'updated'} {table}#{id}",
            json.dumps(record, default=str),
            ctx.get("mode", "autonomous"),
            ctx.get("triggered_by", "cron_loop"),
        ))
        decision_id = cur.fetchone()["id"]
        conn.commit()

    return {"ok": True, "shadow": shadow, "decision_id": decision_id, **record}


def _tool_delegate(
    *, to: str, task_summary: str, task_full: str, deadline: str,
    contact_id: Optional[int] = None, signal_id: Optional[int] = None,
    ctx: Dict[str, Any]
) -> Dict[str, Any]:
    shadow = _shadow_mode()
    try:
        deadline_d = datetime.strptime(deadline, "%Y-%m-%d").date()
    except Exception:
        return {"ok": False, "error": f"deadline invalido: {deadline}"}

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO delegations (
                delegated_to, contact_id, task_summary, task_full, deadline,
                signal_id, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'open')
            RETURNING id
        """, (to, contact_id, task_summary, task_full, deadline_d, signal_id))
        delegation_id = cur.fetchone()["id"]

        cur.execute("""
            INSERT INTO tonha_decisions (
                signal_id, decision_type, decision_summary, action_taken, mode, triggered_by
            )
            VALUES (%s, 'delegate', %s, %s::jsonb, %s, %s)
            RETURNING id
        """, (
            signal_id,
            f"delegated to {to}: {task_summary[:100]}",
            json.dumps({
                "delegation_id": delegation_id,
                "to": to,
                "task_summary": task_summary,
                "deadline": deadline,
                "shadow": shadow,
            }),
            ctx.get("mode", "autonomous"),
            ctx.get("triggered_by", "cron_loop"),
        ))
        decision_id = cur.fetchone()["id"]
        conn.commit()

    return {
        "ok": True,
        "shadow": shadow,
        "delegation_id": delegation_id,
        "decision_id": decision_id,
        "message": f"Delegation criada pra {to}. Em SHADOW: mensagem nao foi enviada — proximo loop do collector cobra.",
    }


def _tool_manage_calendar_event(
    *, event_id: int, action: str, scope: str = "single",
    reason: str = "", ctx: Dict[str, Any]
) -> Dict[str, Any]:
    shadow = _shadow_mode()
    record = {
        "event_id": event_id, "action": action, "scope": scope,
        "reason": reason[:300], "shadow": shadow,
    }

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, summary, start_datetime, status, google_event_id FROM calendar_events WHERE id = %s",
            (event_id,),
        )
        ev = cur.fetchone()
        if not ev:
            return {"ok": False, "error": f"calendar_event #{event_id} nao encontrado"}
        record["summary"] = ev["summary"]
        record["previous_status"] = ev["status"]

        if not shadow:
            if action == "cancel":
                cur.execute(
                    "UPDATE calendar_events SET status='cancelled', atualizado_em=NOW() WHERE id = %s",
                    (event_id,),
                )
                record["local_rowcount"] = cur.rowcount
            elif action == "delete":
                # Google delete e async, precisa await fora desta call.
                # Marca pra batch async ou retorna instrucao.
                try:
                    import asyncio
                    from services.calendar_events import get_calendar_events
                    cal = get_calendar_events()
                    ok = asyncio.run(cal.delete_event(event_id, delete_from_google=True, scope=scope))
                    record["google_delete_ok"] = ok
                except RuntimeError:
                    record["google_delete_ok"] = "skipped_event_loop_running"
                    cur.execute("DELETE FROM calendar_events WHERE id = %s", (event_id,))
                except Exception as e:
                    record["google_delete_error"] = str(e)[:200]

        cur.execute("""
            INSERT INTO tonha_decisions (decision_type, decision_summary, action_taken, mode, triggered_by)
            VALUES ('auto_execute', %s, %s::jsonb, %s, %s)
            RETURNING id
        """, (
            f"{'shadow_' if shadow else ''}{action} calendar #{event_id} ({ev['summary'][:60]})",
            json.dumps(record, default=str),
            ctx.get("mode", "autonomous"),
            ctx.get("triggered_by", "cron_loop"),
        ))
        decision_id = cur.fetchone()["id"]
        conn.commit()

    return {"ok": True, "shadow": shadow, "decision_id": decision_id, **record}


def _tool_decide_and_log(
    *, signal_id: int, decision_type: str, summary: str,
    reasoning: str = "", action_taken: Optional[Dict] = None,
    new_signal_status: str = "resolved",
    ctx: Dict[str, Any]
) -> Dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tonha_decisions (
                signal_id, decision_type, decision_summary, reasoning,
                action_taken, mode, triggered_by
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING id
        """, (
            signal_id,
            decision_type,
            summary[:500],
            reasoning[:500],
            json.dumps(action_taken or {}, default=str),
            ctx.get("mode", "autonomous"),
            ctx.get("triggered_by", "cron_loop"),
        ))
        decision_id = cur.fetchone()["id"]

        if new_signal_status in ("resolved", "dismissed"):
            cur.execute("""
                UPDATE signals
                SET status = %s,
                    resolved_at = NOW(),
                    resolved_by = 'tonha_brain',
                    decision_id = %s
                WHERE id = %s
            """, (new_signal_status, decision_id, signal_id))
        conn.commit()

    return {"ok": True, "decision_id": decision_id, "signal_marked": new_signal_status}
