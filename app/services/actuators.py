"""
Catálogo de atuadores v0 (F-2 Passo C) — dá à Tônia poder de AGIR no INTEL.

A Tônia (julgador L2) chama estes atuadores SOB COMANDO do Renato — nunca
proativo. O gen-1 proativo (propostas automáticas a cada mensagem) virou ruído
net-negative e foi desligado (ver memo feedback_gen1_ruido_desligado); o gatilho
aqui é o mesmo da delegação de dev: COMANDO explícito no chat.

INTEL é o sistema de registro que EXECUTA; a Tônia DECIDE e chama via
/api/actuators/execute (auth X-API-Key == INTEL_API_KEY). Toda atuação audita em
cos_actions_log (source_table='tonia_actuator').

v0 minimalista (2 atuadores):
  - create_task  — reversível → executa direto (task nasce em 'pending').
  - schedule_wa  — agenda envio WA na fila durável scheduled_actions
                   (dedup_key UNIQUE + retry); o disparo em si é revisável.

Extensível: novo atuador = nova entrada em ALLOWED_ACTIONS + handler. NUNCA
levanta pro chamador do jeito errado — devolve dict {ok|error}.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from database import get_db
from services.tz import now_utc, parse_iso

log = logging.getLogger("actuators")

ALLOWED_ACTIONS = {"create_task", "schedule_wa"}


def _audit_open(cur, action: str, params: Dict[str, Any], source: str) -> Optional[int]:
    """Abre uma linha de audit em cos_actions_log (status='running'). Best-effort."""
    try:
        # cos_actions_log nasceu pro /papel-cos (sweep de triagem) e exige
        # sweep_id (UUID) + source_id NOT NULL. Atuadores da Tônia não vêm de
        # sweep nem de uma source row: cada atuação é um "sweep de 1 ação" (uuid4
        # próprio), source_id=0 sentinela, source_table='tonia_actuator'.
        cur.execute(
            """INSERT INTO cos_actions_log
                   (sweep_id, source_table, source_id, source_summary, bucket,
                    action_type, action_params, status)
               VALUES (%s, 'tonia_actuator', 0, %s, 'auto',
                       %s, %s::jsonb, 'running')
               RETURNING id""",
            (str(uuid.uuid4()), source[:200], action,
             json.dumps(params, ensure_ascii=False, default=str)),
        )
        return int(cur.fetchone()["id"])
    except Exception:
        log.exception("actuators: falha abrindo audit action=%s", action)
        return None


def _audit_close(cur, audit_id: Optional[int], status: str,
                 result: Optional[Dict] = None, error: Optional[str] = None) -> None:
    if audit_id is None:
        return
    try:
        cur.execute(
            """UPDATE cos_actions_log
                  SET status = %s,
                      result = COALESCE(%s, result),
                      error = COALESCE(%s, error),
                      finished_em = NOW()
                WHERE id = %s""",
            (status, json.dumps(result, ensure_ascii=False, default=str) if result else None,
             error, audit_id),
        )
    except Exception:
        log.exception("actuators: falha fechando audit id=%s", audit_id)


def _do_create_task(cur, payload: Dict[str, Any]) -> Dict[str, Any]:
    titulo = (payload.get("titulo") or payload.get("title") or "").strip()
    if not titulo:
        raise ValueError("create_task requer 'titulo'")
    descricao = payload.get("descricao") or payload.get("notes")
    prioridade = payload.get("prioridade", 5)
    project_id = payload.get("project_id")
    contact_id = payload.get("contact_id")

    # prazo: data_vencimento ISO OU prazo_dias (a partir de hoje BRT, meia-noite)
    data_venc = None
    if payload.get("data_vencimento"):
        try:
            data_venc = parse_iso(payload["data_vencimento"]).replace(tzinfo=None)
        except Exception:
            data_venc = None
    elif payload.get("prazo_dias") is not None:
        data_venc = (now_utc().replace(tzinfo=None)
                     + timedelta(days=int(payload["prazo_dias"]))
                     ).replace(hour=0, minute=0, second=0, microsecond=0)

    cur.execute(
        """INSERT INTO tasks
               (titulo, descricao, project_id, contact_id, data_vencimento,
                prioridade, ai_generated, status)
           VALUES (%s, %s, %s, %s, %s, %s, true, 'pending')
           RETURNING id""",
        (titulo, descricao, project_id, contact_id, data_venc, prioridade),
    )
    tid = int(cur.fetchone()["id"])
    return {"task_id": tid, "titulo": titulo,
            "data_vencimento": data_venc.isoformat() if data_venc else None}


def _do_schedule_wa(payload: Dict[str, Any], source: str) -> Dict[str, Any]:
    from services.scheduled_actions import schedule_wa
    number = (payload.get("number") or "").strip()
    text = (payload.get("text") or "").strip()
    if not number or not text:
        raise ValueError("schedule_wa requer 'number' e 'text'")
    when = payload.get("scheduled_for")
    if not when:
        raise ValueError("schedule_wa requer 'scheduled_for' (ISO-8601)")
    scheduled_for = parse_iso(when)
    instance = (payload.get("instance") or "rap-whatsapp").strip()
    dedup_key = payload.get("dedup_key")
    sid = schedule_wa(
        instance=instance, number=number, text=text, scheduled_for=scheduled_for,
        source=source, dedup_key=dedup_key, created_by="tonia",
    )
    return {"scheduled_action_id": sid, "scheduled_for": scheduled_for.isoformat(),
            "number": number}


def execute_actuator(action: str, payload: Dict[str, Any], source: str = "tonia") -> Dict[str, Any]:
    """
    Executa um atuador do catálogo v0. Audita em cos_actions_log.
    Retorna {"ok": True, "action": ..., ...result} ou {"error": ...}.
    NUNCA levanta.
    """
    action = (action or "").strip()
    payload = payload or {}
    if action not in ALLOWED_ACTIONS:
        return {"error": f"action '{action}' não permitida (v0: {sorted(ALLOWED_ACTIONS)})"}

    try:
        with get_db() as conn:
            cur = conn.cursor()
            audit_id = _audit_open(cur, action, payload, source)
            conn.commit()
            try:
                if action == "create_task":
                    result = _do_create_task(cur, payload)
                elif action == "schedule_wa":
                    result = _do_schedule_wa(payload, source)
                _audit_close(cur, audit_id, "success", result=result)
                conn.commit()
                log.info("actuators: %s ok source=%s result=%s", action, source, result)
                return {"ok": True, "action": action, **result}
            except Exception as e:
                _audit_close(cur, audit_id, "error", error=f"{type(e).__name__}: {e}")
                conn.commit()
                log.warning("actuators: %s falhou: %s", action, e)
                return {"error": f"{type(e).__name__}: {e}", "action": action}
    except Exception as e:
        log.exception("actuators: erro de conexão/execução action=%s", action)
        return {"error": f"{type(e).__name__}: {e}", "action": action}
