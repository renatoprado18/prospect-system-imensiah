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

ALLOWED_ACTIONS = {"create_task", "schedule_wa",
                   "send_push", "send_email", "create_calendar_event",
                   "triage_inbox"}


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


def _do_send_push(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Dispara push notification (Web Push/VAPID) pros subscribers. Sem
    subscription específica = broadcast pra todos. Reusa o PushNotificationService
    (não reimplementa envio). Retorna {sent, failed}."""
    from services.push_notifications import get_push_service
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("send_push requer 'title'")
    body = (payload.get("body") or "").strip()
    res = get_push_service().send_notification(
        title=title,
        body=body,
        data=payload.get("data"),
        tag=payload.get("tag"),
        urgent=bool(payload.get("urgent", False)),
    )
    # success=False COM errors = problema real (não-configurado, 4xx do push).
    # success=False SEM errors = simplesmente não há subscriber → sent=0, ok.
    if not res.get("success") and res.get("errors"):
        raise RuntimeError("; ".join(str(e) for e in res["errors"]))
    return {"sent": res.get("sent", 0), "failed": res.get("failed", 0)}


async def _do_send_email(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Envia email via Gmail API. Resolve a conta (personal|professional) em
    google_accounts e pega token válido (refresh automático via get_valid_token).
    Espelha o padrão da tool send_email do intel_bot. Retorna {message_id}."""
    to = (payload.get("to") or "").strip()
    if not to or "@" not in to:
        raise ValueError("send_email requer 'to' (email válido)")
    subject = (payload.get("subject") or "").strip()
    if not subject:
        raise ValueError("send_email requer 'subject'")
    body = payload.get("body") or ""

    account_alias = (payload.get("account") or "professional").lower()
    account_email = None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT email FROM google_accounts WHERE conectado=TRUE AND tipo=%s LIMIT 1",
            (account_alias,),
        )
        row = cur.fetchone()
        if row:
            account_email = row["email"]
    if not account_email:
        raise RuntimeError(f"conta Gmail '{account_alias}' não conectada")

    from integrations.google_contacts import get_valid_token
    from integrations.gmail import GmailIntegration
    token = await get_valid_token(account_email)
    if not token:
        raise RuntimeError(f"falha ao obter token Gmail da conta {account_email}")

    result = await GmailIntegration().send_message(
        access_token=token,
        to=to,
        subject=subject,
        body=body,
        html_body=payload.get("html_body"),
    )
    if "error" in result:
        raise RuntimeError(f"Gmail send falhou: {result.get('error')}")
    return {"message_id": result.get("id"), "from_account": account_email}


async def _do_create_calendar_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Cria evento no Google Calendar via CalendarEventsService (resolve
    conta/token internamente — não pedir access_token). start/end em ISO-8601.
    Retorna {event_id, google_event_id, conference_url}.

    Obs: CalendarEventsService não expõe o htmlLink do Google (push_local_event
    não o persiste), então devolvemos os IDs + conference_url (link Meet, se houver)
    em vez de html_link."""
    summary = (payload.get("summary") or "").strip()
    if not summary:
        raise ValueError("create_calendar_event requer 'summary'")
    start_raw = payload.get("start_datetime")
    end_raw = payload.get("end_datetime")
    if not start_raw or not end_raw:
        raise ValueError("create_calendar_event requer 'start_datetime' e 'end_datetime' (ISO-8601)")
    # Mesmo parsing da tool schedule_meeting do intel_bot (preserva o horário BRT
    # que o Renato passa — calendar_events guarda naive BRT, não converter).
    try:
        start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"data inválida (use ISO-8601): {e}")

    # attendees aceita ["a@b.com"] ou [{"email": "a@b.com"}]
    attendees = payload.get("attendees")
    if isinstance(attendees, list) and attendees and isinstance(attendees[0], str):
        attendees = [{"email": a} for a in attendees]

    from services.calendar_events import get_calendar_events
    event = await get_calendar_events().create_event(
        summary=summary,
        start_datetime=start_dt,
        end_datetime=end_dt,
        description=payload.get("description"),
        location=payload.get("location"),
        attendees=attendees,
        create_in_google=True,
    )
    if not event:
        raise RuntimeError("create_event não retornou evento")
    return {
        "event_id": event.get("id"),
        "google_event_id": event.get("google_event_id"),
        "conference_url": event.get("conference_url"),
    }


async def _do_triage_inbox(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Varre o INBOX atual do Renato e organiza nos 4 buckets
    (!!Renato/!Andressa/Financeiro/Arquivar/!!Deletar). Reusa
    apply_triage_to_inbox do email_triage (mesma logica/helpers do sweep) — NAO
    reimplementa roteamento. Params:
      - account: 'professional'|'personal' OU email direto (opcional; default 2 contas).
      - dry_run: se True, so preview (nao age). Default False.
      - limit: max de emails por conta (default 40).
    Retorna o stats dict (processed, by_bucket, acted, dry_run, per_email...)."""
    from services.email_triage import apply_triage_to_inbox

    account_raw = (payload.get("account") or "").strip()
    account_email = None
    if account_raw:
        if "@" in account_raw:
            account_email = account_raw
        else:
            # alias professional|personal -> resolve email da conta conectada
            alias = account_raw.lower()
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT email FROM google_accounts WHERE conectado=TRUE AND tipo=%s LIMIT 1",
                    (alias,),
                )
                row = cur.fetchone()
                if row:
                    account_email = row["email"]
            if not account_email:
                raise RuntimeError(f"conta Gmail '{account_raw}' nao conectada")

    dry_run = bool(payload.get("dry_run", False))
    try:
        limit = int(payload.get("limit", 40))
    except (TypeError, ValueError):
        limit = 40

    result = await apply_triage_to_inbox(
        account_email=account_email,
        limit=limit,
        dry_run=dry_run,
    )
    if not result.get("ok", True):
        raise RuntimeError("; ".join(str(e) for e in result.get("errors", [])) or "triage_inbox falhou")
    return result


async def execute_actuator(action: str, payload: Dict[str, Any], source: str = "tonia") -> Dict[str, Any]:
    """
    Executa um atuador do catálogo. Audita em cos_actions_log.
    Retorna {"ok": True, "action": ..., ...result} ou {"error": ...}.
    NUNCA levanta.

    Async: send_email e create_calendar_event precisam de await (Gmail/Calendar
    são async). create_task/schedule_wa/send_push seguem síncronos. Chamado do
    endpoint async main.py:/api/actuators/execute.
    """
    action = (action or "").strip()
    payload = payload or {}
    if action not in ALLOWED_ACTIONS:
        return {"error": f"action '{action}' não permitida ({sorted(ALLOWED_ACTIONS)})"}

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
                elif action == "send_push":
                    result = _do_send_push(payload)
                elif action == "send_email":
                    result = await _do_send_email(payload)
                elif action == "create_calendar_event":
                    result = await _do_create_calendar_event(payload)
                elif action == "triage_inbox":
                    result = await _do_triage_inbox(payload)
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
