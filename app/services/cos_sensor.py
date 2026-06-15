"""
CoS Sensor Agent — Stage 2 do roadmap CoS inteligente (11/jun/2026).

Why: substitui detectors rule-based (operational_alerts.py + similares) por
agente LLM (Sonnet 4.6) que roda 30/30min, le estado do mundo, decide acoes
via tools, e executa autonomamente OU vira proposal dependendo da politica
de autonomia ratificada pelo Renato (feedback_cos_autonomy_policy.md).

Diferenca vs cos_investigator (Onda 2):
- Investigator roda 7h10 BRT e popula cos_briefing_items (LE estado, escreve resumos).
- Sensor roda 30/30min e AGE sobre estado em mudanca (cria proposals, drafta
  emails/WAs, agenda Calendar events, atualiza contact notes, agenda mensagens).

Incidentes que motivaram (test cases):
- Veridiana (10/06 15:33): Thalita avisou via WA sobre cirurgia segunda; detector
  rule-based pegou mas com gap; Sensor deveria propor call.
- Orioli (11/06 06:48): Felipe mandou email + "Olá bom dia" depois de Renato
  prometer Google Meet pra sex 10h; Sensor deveria add_calendar_event auto.
- Cadência Assespro (11/06 11h): reuniao cancelada via msg grupo "sem pauta";
  Sensor deveria detectar cancelamento e flag_calendar_event_cancelled.

Catalogo MVP (5 tools):
- create_action_proposal: sempre Auto
- draft_email: sempre Auto (cria draft Gmail, nao envia)
- update_contact_notes: sempre Auto
- add_calendar_event: Auto-com-condicao (reuniao ja confirmada via WA)
- schedule_wa_message: sempre Auto

Politica completa em feedback_cos_autonomy_policy.md (memoria do Renato).
Em ambientes onde a memoria nao existe (Vercel/Railway), fallback hardcoded
conservador: tudo Auto pelas regras acima (sao Auto na politica oficial).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
COS_SENSOR_MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 8
MAX_TOKENS_PER_ITER = 3072

# Budget cap diario duro. Default $2.00 (subiu de $0.50 em 13/06/26 — Patrol Agent
# usa send_wa_to_renato com payload maior pra propostas conversacionais).
# Override via env var COS_SENSOR_DAILY_CAP_USD.
try:
    COS_SENSOR_DAILY_CAP_USD = float(os.getenv("COS_SENSOR_DAILY_CAP_USD", "2.00"))
except (TypeError, ValueError):
    COS_SENSOR_DAILY_CAP_USD = 2.00

# Caminho da memoria de politica de autonomia (so existe em dev local do Renato)
POLICY_MEMORY_PATH = Path.home() / ".claude/projects/-Users-rap-prospect-system/memory/feedback_cos_autonomy_policy.md"


# ============== Politica de Autonomia (parse memoria + fallback) ==============

# Fallback hardcoded conservador: derivado da politica ratificada pelo Renato.
# Tudo que e Auto na politica oficial mantemos Auto aqui; resto -> propor.
_FALLBACK_AUTONOMY_POLICY: Dict[str, str] = {
    "create_action_proposal": "auto",
    "update_contact_notes": "auto",
    "draft_email": "auto",
    "schedule_wa_message": "auto",
    # Calendar event e Auto na politica oficial mas exige "reunião combinada via WA".
    # Sensor enforca via parametro confirmed_via_wa=True (tool valida e nega senao).
    # Em prod (sem memoria local), tratamos como 'auto' — o gate fica na tool.
    "add_calendar_event": "auto",
    # send_wa_to_renato (13/06/26): tool conversacional — sempre Auto. CoS Patrol Agent
    # usa pra propor ao Renato via WA 0192 -> 3337 em vez de empurrar pro dashboard.
    # Modo SHADOW: prefirir essa tool a create_action_proposal.
    "send_wa_to_renato": "auto",
}


def load_autonomy_policy() -> Dict[str, str]:
    """Carrega politica de autonomia da memoria do Renato. Fallback conservador.

    Le o arquivo feedback_cos_autonomy_policy.md (so existe local), parseia
    as linhas tipo '- tool_name (...): **Auto**' / 'Propor' / 'Auto-com-condicao'
    e monta dict {tool_name: classe}.

    Em prod (Vercel/Railway), arquivo nao existe -> retorna fallback hardcoded
    (igualmente conservador pra MVP).
    """
    if not POLICY_MEMORY_PATH.exists():
        logger.info("cos_sensor: policy memory nao encontrada, usando fallback hardcoded")
        return dict(_FALLBACK_AUTONOMY_POLICY)

    try:
        text = POLICY_MEMORY_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"cos_sensor: erro lendo policy memory: {e}; usando fallback")
        return dict(_FALLBACK_AUTONOMY_POLICY)

    policy: Dict[str, str] = {}
    # Linhas tipo: '- create_action_proposal: **Auto**'
    #              '- send_email: **Propor SEMPRE**'
    #              '- mark_task_complete: **Auto-com-condição** — confidence ≥ 0.85'
    #              '- add_calendar_event (reunião combinada via WA, ex: caso Orioli): **Auto**'
    # Importante: usa .*? lazy ate encontrar **...** pra ignorar ':' dentro de
    # parenteses ('ex: caso Orioli').
    pattern = re.compile(
        r"^[-*]\s+([a-z_][a-z0-9_]+)\b.*?\*\*([^*]+)\*\*",
        re.IGNORECASE | re.MULTILINE,
    )
    for m in pattern.finditer(text):
        name = m.group(1).strip().lower()
        klass_raw = m.group(2).strip().lower()
        if "propor" in klass_raw:
            policy[name] = "propor"
        elif "auto-com" in klass_raw or "auto_cond" in klass_raw or "condic" in klass_raw or "condiç" in klass_raw:
            policy[name] = "auto_cond"
        elif "auto" in klass_raw:
            policy[name] = "auto"

    # Garante que as 5 tools do MVP tenham entry — fallback per-tool se faltar
    for tool, default in _FALLBACK_AUTONOMY_POLICY.items():
        if tool not in policy:
            policy[tool] = default

    return policy


# ============== Tool implementations ==============


def _tool_create_action_proposal(
    action_type: str,
    title: str,
    description: str,
    urgency: str = "medium",
    context_link: Optional[str] = None,
    contact_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Cria proposal via ActionProposalsService (com dedup interno por 24h)."""
    try:
        from services.action_proposals import get_action_proposals
        from services.audit_log import log as audit_log

        svc = get_action_proposals()
        proposal_data = {
            "action_type": action_type,
            "contact_id": contact_id or 0,  # 0 = system-wide; service trata
            "title": title[:200],
            "description": description[:1500],
            "trigger_text": (context_link or "")[:500],
            "ai_reasoning": "CoS Sensor Agent (tick)",
            "confidence": 0.85,
            "urgency": urgency if urgency in ("high", "medium", "low") else "medium",
            "action_params": {"source": "cos_sensor", "context_link": context_link},
            "options": [],
        }
        proposal = svc.create_proposal(proposal_data) if contact_id else None

        if not proposal:
            # Service exige contact_id; fallback: insert direto (sem dedup)
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO action_proposals (
                        contact_id, action_type, title, description, urgency,
                        status, confidence, ai_reasoning, action_params
                    )
                    VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s::jsonb)
                    RETURNING id
                    """,
                    (
                        contact_id,
                        action_type,
                        title[:200],
                        description[:1500],
                        urgency,
                        0.85,
                        "CoS Sensor Agent (tick)",
                        json.dumps({"source": "cos_sensor", "context_link": context_link}),
                    ),
                )
                pid = cur.fetchone()["id"]
                conn.commit()
                proposal = {"id": pid}

        aid = audit_log(
            "cos_sensor.create_action_proposal",
            entity_type="action_proposal",
            entity_id=proposal.get("id"),
            actor="cos_sensor",
            details={"action_type": action_type, "title": title, "urgency": urgency},
        )
        return {"success": True, "result": {"proposal_id": proposal.get("id")}, "audit_log_id": aid}
    except Exception as e:
        logger.exception(f"cos_sensor.create_action_proposal failed: {e}")
        return {"success": False, "error": str(e), "audit_log_id": None}


def _tool_update_contact_notes(contact_id: int, note_text: str) -> Dict[str, Any]:
    """Append note ao campo contacts.contexto."""
    try:
        from services.audit_log import log as audit_log

        ts = to_brt(now_utc()).strftime("%d/%m %H:%M")
        prefix = f"\n\n[CoS Sensor {ts}] "
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE contacts
                SET contexto = COALESCE(contexto, '') || %s || %s,
                    atualizado_em = NOW()
                WHERE id = %s
                RETURNING id
                """,
                (prefix, note_text[:2000], contact_id),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                return {"success": False, "error": f"contato_nao_encontrado: {contact_id}", "audit_log_id": None}

        aid = audit_log(
            "cos_sensor.update_contact_notes",
            entity_type="contact",
            entity_id=contact_id,
            actor="cos_sensor",
            details={"note_text": note_text[:200]},
        )
        return {"success": True, "result": {"contact_id": contact_id}, "audit_log_id": aid}
    except Exception as e:
        logger.exception(f"cos_sensor.update_contact_notes failed: {e}")
        return {"success": False, "error": str(e), "audit_log_id": None}


def _tool_draft_email(
    recipient: str,
    subject: str,
    body: str,
    account: str = "professional",
) -> Dict[str, Any]:
    """Cria draft no Gmail via /users/me/drafts. NAO envia."""
    import asyncio
    import base64
    import email.mime.text as mime_text
    import httpx
    from services.audit_log import log as audit_log

    async def _create_draft() -> Dict[str, Any]:
        from integrations.google_contacts import get_valid_token

        # Resolve email da conta por tipo
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT email FROM google_accounts WHERE tipo = %s AND conectado = TRUE ORDER BY id ASC LIMIT 1",
                (account,),
            )
            r = cur.fetchone()
            if not r:
                return {"ok": False, "error": f"conta_google {account!r} nao configurada"}
            account_email = r["email"]

        access_token = await get_valid_token(account_email)
        if not access_token:
            return {"ok": False, "error": f"sem token valido pra {account_email}"}

        msg = mime_text.MIMEText(body)
        msg["To"] = recipient
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"message": {"raw": raw}},
                timeout=30.0,
            )
            if resp.status_code not in (200, 201):
                return {"ok": False, "error": f"gmail_api {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()
            return {"ok": True, "draft_id": data.get("id"), "account": account_email}

    try:
        # Pode rodar em sync context
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Em loop async — usa create_task; aqui fazemos eventloop sync wait
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    fut = pool.submit(asyncio.run, _create_draft())
                    res = fut.result(timeout=60)
            else:
                res = loop.run_until_complete(_create_draft())
        except RuntimeError:
            res = asyncio.run(_create_draft())

        if not res.get("ok"):
            return {"success": False, "error": res.get("error"), "audit_log_id": None}

        aid = audit_log(
            "cos_sensor.draft_email",
            entity_type="gmail_draft",
            actor="cos_sensor",
            details={
                "recipient": recipient,
                "subject": subject[:200],
                "account": res.get("account"),
                "draft_id": res.get("draft_id"),
            },
        )
        return {
            "success": True,
            "result": {"draft_id": res.get("draft_id"), "account": res.get("account")},
            "audit_log_id": aid,
        }
    except Exception as e:
        logger.exception(f"cos_sensor.draft_email failed: {e}")
        return {"success": False, "error": str(e), "audit_log_id": None}


def _tool_add_calendar_event(
    title: str,
    start_iso: str,
    duration_min: int = 60,
    attendees: Optional[List[str]] = None,
    description: str = "",
    confirmed_via_wa: bool = False,
) -> Dict[str, Any]:
    """Cria Calendar event (Auto-com-condicao: so se confirmed_via_wa=True).

    confirmed_via_wa: flag explicito que o LLM deve setar quando tem
    evidencia textual de confirmacao (msg WA do contato/Renato dizendo "ok").
    """
    import asyncio
    from services.audit_log import log as audit_log

    if not confirmed_via_wa:
        return {
            "success": False,
            "error": "calendar_event requer confirmed_via_wa=True (politica Auto-com-condicao)",
            "audit_log_id": None,
        }

    async def _create() -> Dict[str, Any]:
        from integrations.google_calendar import GoogleCalendarIntegration
        from integrations.google_contacts import get_valid_token

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT email FROM google_accounts WHERE tipo = 'personal' AND conectado = TRUE ORDER BY id ASC LIMIT 1"
            )
            r = cur.fetchone()
            if not r:
                # fallback pra professional
                cur.execute(
                    "SELECT email FROM google_accounts WHERE conectado = TRUE ORDER BY id ASC LIMIT 1"
                )
                r = cur.fetchone()
            if not r:
                return {"ok": False, "error": "sem google_account conectado"}
            account_email = r["email"]

        token = await get_valid_token(account_email)
        if not token:
            return {"ok": False, "error": f"sem token valido pra {account_email}"}

        try:
            start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        except Exception as e:
            return {"ok": False, "error": f"start_iso invalido: {e}"}
        end = start + timedelta(minutes=int(duration_min or 60))

        cal = GoogleCalendarIntegration()
        res = await cal.create_event(
            access_token=token,
            summary=title,
            start_datetime=start,
            end_datetime=end,
            description=(description or "") + "\n\n[criado por CoS Sensor — politica auto_cond confirmed_via_wa]",
            attendees=attendees or [],
            create_meet=True,
        )
        if "error" in res:
            return {"ok": False, "error": res["error"]}
        return {
            "ok": True,
            "event_id": res.get("id"),
            "hangout_link": res.get("hangoutLink"),
            "html_link": res.get("htmlLink"),
        }

    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    fut = pool.submit(asyncio.run, _create())
                    res = fut.result(timeout=60)
            else:
                res = loop.run_until_complete(_create())
        except RuntimeError:
            res = asyncio.run(_create())

        if not res.get("ok"):
            return {"success": False, "error": res.get("error"), "audit_log_id": None}

        aid = audit_log(
            "cos_sensor.add_calendar_event",
            entity_type="calendar_event",
            actor="cos_sensor",
            details={
                "title": title,
                "start_iso": start_iso,
                "attendees": attendees or [],
                "event_id": res.get("event_id"),
            },
        )
        return {
            "success": True,
            "result": {
                "event_id": res.get("event_id"),
                "hangout_link": res.get("hangout_link"),
                "html_link": res.get("html_link"),
            },
            "audit_log_id": aid,
        }
    except Exception as e:
        logger.exception(f"cos_sensor.add_calendar_event failed: {e}")
        return {"success": False, "error": str(e), "audit_log_id": None}


def _tool_send_wa_to_renato(
    text: Optional[str] = None,
    title: str = "",
    summary: str = "",
    options: Optional[List[Dict[str, str]]] = None,
    urgency: str = "medium",
    context_link: Optional[str] = None,
    contact_id: Optional[int] = None,
    proposed_action: Optional[Dict[str, Any]] = None,
    is_system_alert: bool = False,
    agent_label: str = "CoS Patrol",
) -> Dict[str, Any]:
    """Envia proposta conversacional ao Renato via intel-bot (0192 -> 3337).

    Why: dashboard cards viraram ruido. Patrol Agent (13/06/26) prefere esse
    canal — Renato responde texto/audio, o bot ja captura no fluxo principal.

    Dois modos de rendering (13/06/26 — voz humanizada da Tonha):
    - **natural** (preferido): agente passa `text` ja escrito na voz dela.
      Manda como prosa, sem header "🤖 CoS Patrol", sem footer
      "_Responda texto ou audio._", sem opcoes numeradas obrigatorias.
    - **structured** (fallback / system alerts): old way — usa title +
      summary + options. Mantem header. Usado quando `text` nao vem,
      ou quando `is_system_alert=True` (cron health, erro de sistema).

    A mensagem entra em bot_conversations como role='assistant' com metadata
    {cos_proposal: {action, params}} pra que handle_bot_message no proximo
    turno reconheca contexto e atue na resposta do Renato.
    """
    try:
        import httpx
        from services.audit_log import log as audit_log
        from services.intel_bot import RENATO_PHONE, INTEL_BOT_INSTANCE

        # ===== Reserva proximo ID do bot_conversations pra embutir no texto =====
        # Why: Renato recebe varias propostas em sequencia e nao sabe a qual
        # esta respondendo. ID curto #P{n} no header permite correlacionar
        # "P742 1" -> proposta #742 opcao 1. Reply do WhatsApp tb funciona mas
        # nem todo cliente envia stanzaId no webhook.
        proposal_id: Optional[int] = None
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT nextval('bot_conversations_id_seq') AS id")
                row = cur.fetchone()
                proposal_id = int(row["id"]) if row else None
        except Exception as e:
            logger.warning(f"_tool_send_wa_to_renato: nextval falhou: {e}")
            proposal_id = None
        proposal_tag = f"#P{proposal_id}" if proposal_id else ""

        # ===== Rendering =====
        raw_text = (text or "").strip()
        use_natural = bool(raw_text) and not is_system_alert

        if use_natural:
            # Modo natural: prosa direto da Tonha. Sem decoracao.
            # Adiciona ID curto no topo se houver options (necessario pra
            # correlacao da resposta), senao prefere prosa limpa.
            if options and proposal_tag:
                final_text = f"{proposal_tag}\n\n{raw_text}"[:3500]
            else:
                final_text = raw_text[:3500]
            mode_label = "natural"
        else:
            # Modo structured: header + title + summary + opcoes.
            lines: List[str] = []
            if is_system_alert:
                header = "⚠ *INTEL alerta*"
            else:
                header = f"🤖 *{agent_label}*"
            if proposal_tag:
                header += f"  ·  {proposal_tag}"
            if urgency == "high" and not is_system_alert:
                header += " ⚠️"
            lines.append(header)
            lines.append("")
            if title:
                lines.append(f"*{title.strip()}*")
                lines.append("")
            if summary:
                lines.append(summary.strip())
            elif raw_text:
                lines.append(raw_text.strip())

            if options:
                lines.append("")
                for i, opt in enumerate(options, 1):
                    label = (opt.get("label") or "").strip()
                    lines.append(f"{i}. {label}")
                lines.append("")
                if proposal_tag:
                    lines.append(
                        f"_↩️ Reply nessa msg, OU digite_ `{proposal_tag} 1` _(ou 2/3/4 ou texto livre)._"
                    )
                else:
                    lines.append("_Responda texto ou áudio._")

            final_text = "\n".join(lines)[:3500]
            mode_label = "system_alert" if is_system_alert else "structured"

        # Mantem nome historico `text` pro resto da funcao (envio + persistencia).
        text = final_text

        # Envia via Evolution sync (chamado de tool sync dentro de tick sync —
        # asyncio.run_coroutine_threadsafe na mesma thread deadlocka, e
        # asyncio.run dentro do loop FastAPI falha. Sync HTTPx evita o pacto).
        evo_url = (os.getenv("EVOLUTION_API_URL", "") or "").strip()
        evo_key = (os.getenv("EVOLUTION_API_KEY", "") or "").strip()
        if not evo_url or not evo_key:
            return {"success": False, "error": "Evolution API nao configurada", "audit_log_id": None}

        instance = (INTEL_BOT_INSTANCE or "intel-bot").strip()
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(
                    f"{evo_url}/message/sendText/{instance}",
                    headers={"apikey": evo_key, "Content-Type": "application/json"},
                    json={"number": RENATO_PHONE, "text": text},
                )
            if resp.status_code >= 400:
                return {
                    "success": False,
                    "error": f"Evolution HTTP {resp.status_code}: {resp.text[:200]}",
                    "audit_log_id": None,
                }
        except Exception as send_err:
            return {
                "success": False,
                "error": f"Evolution send falhou: {send_err}",
                "audit_log_id": None,
            }

        # Salva turn no historico do bot pro fluxo conversacional pegar contexto.
        cos_metadata = {
            "cos_patrol": True,
            "agent_label": agent_label,
            "proposed_action": proposed_action or {},
            "options": options or [],
            "urgency": urgency,
            "context_link": context_link,
            "contact_id": contact_id,
            "proposal_tag": proposal_tag or None,
        }
        try:
            with get_db() as conn:
                cur = conn.cursor()
                if proposal_id:
                    cur.execute(
                        """
                        INSERT INTO bot_conversations (id, phone, role, content, tool_calls)
                        VALUES (%s, %s, 'assistant', %s, %s::jsonb)
                        """,
                        (proposal_id, RENATO_PHONE, text, json.dumps(cos_metadata)),
                    )
                    bot_msg_id = proposal_id
                else:
                    cur.execute(
                        """
                        INSERT INTO bot_conversations (phone, role, content, tool_calls)
                        VALUES (%s, 'assistant', %s, %s::jsonb)
                        RETURNING id
                        """,
                        (RENATO_PHONE, text, json.dumps(cos_metadata)),
                    )
                    bot_msg_id = cur.fetchone()["id"]
                conn.commit()
        except Exception as e:
            logger.warning(f"_tool_send_wa_to_renato: bot_conversations insert falhou: {e}")
            bot_msg_id = proposal_id

        aid = audit_log(
            "cos_sensor.send_wa_to_renato",
            entity_type="bot_conversation",
            entity_id=bot_msg_id,
            actor="cos_sensor",
            details={
                "mode": mode_label,
                "title": title or None,
                "urgency": urgency,
                "options_count": len(options or []),
                "has_proposed_action": bool(proposed_action),
                "contact_id": contact_id,
                "chars": len(text),
            },
        )
        return {
            "success": True,
            "result": {"bot_conversation_id": bot_msg_id, "message_chars": len(text)},
            "audit_log_id": aid,
        }
    except Exception as e:
        logger.exception(f"cos_sensor.send_wa_to_renato failed: {e}")
        return {"success": False, "error": str(e), "audit_log_id": None}


def _tool_schedule_wa_message(
    contact_id: int,
    content: str,
    when_iso: str,
    dedup_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Agenda mensagem WA via scheduled_actions.schedule_wa."""
    try:
        from services.scheduled_actions import schedule_wa
        from services.audit_log import log as audit_log

        # Resolve telefone do contato
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, nome, telefone FROM contacts WHERE id = %s", (contact_id,))
            r = cur.fetchone()
            if not r or not r.get("telefone"):
                return {
                    "success": False,
                    "error": f"contato {contact_id} sem telefone",
                    "audit_log_id": None,
                }
            phone = re.sub(r"\D", "", r["telefone"])
            nome = r["nome"]

        try:
            scheduled_for = datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
        except Exception as e:
            return {"success": False, "error": f"when_iso invalido: {e}", "audit_log_id": None}

        instance = os.getenv("WHATSAPP_INSTANCE_OWNER", "rap-whatsapp").strip() or "rap-whatsapp"
        sid = schedule_wa(
            instance=instance,
            number=phone,
            text=content,
            scheduled_for=scheduled_for,
            source=f"cos_sensor (contact={contact_id} {nome})",
            dedup_key=dedup_key,
            created_by="cos_sensor",
        )
        aid = audit_log(
            "cos_sensor.schedule_wa_message",
            entity_type="scheduled_action",
            entity_id=sid,
            actor="cos_sensor",
            details={
                "contact_id": contact_id,
                "phone": phone,
                "when_iso": when_iso,
                "content_preview": content[:200],
                "dedup_key": dedup_key,
            },
        )
        return {"success": True, "result": {"scheduled_id": sid}, "audit_log_id": aid}
    except Exception as e:
        logger.exception(f"cos_sensor.schedule_wa_message failed: {e}")
        return {"success": False, "error": str(e), "audit_log_id": None}


# ============== Tool catalog (formato Anthropic) ==============

SENSOR_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "create_action_proposal",
        "description": (
            "Cria uma proposta de acao em action_proposals (status='pending') pra Renato revisar. "
            "Use quando voce detecta sinal que demanda decisao manual do Renato (ex: cobertura "
            "operacional de funcionaria-chave que vai sair, decisao binaria sobre reuniao, etc). "
            "POLITICA: sempre Auto (proposal e o canal padrao quando voce nao pode/deve agir direto)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "description": "Tipo: operational_risk, schedule_meeting, send_message, "
                                   "review_decision, follow_up, etc.",
                },
                "title": {"type": "string", "description": "Titulo curto (max 200ch)."},
                "description": {"type": "string", "description": "Descricao com contexto factual."},
                "urgency": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "default": "medium",
                },
                "context_link": {
                    "type": "string",
                    "description": "Ref opcional (msg_id, evento_id, etc).",
                },
                "contact_id": {
                    "type": "integer",
                    "description": "Contato relacionado, se houver.",
                },
            },
            "required": ["action_type", "title", "description"],
        },
    },
    {
        "name": "update_contact_notes",
        "description": (
            "Atualiza contacts.contexto com nota factual nova (preferencia revelada, fato "
            "importante, mudanca de cargo/empresa). Append-only com timestamp. "
            "POLITICA: sempre Auto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer"},
                "note_text": {"type": "string", "description": "Texto da nota (max 2000ch)."},
            },
            "required": ["contact_id", "note_text"],
        },
    },
    {
        "name": "draft_email",
        "description": (
            "Cria um RASCUNHO de email no Gmail (Drafts). NAO envia. Renato revisa/edita/dispara "
            "manualmente do app Gmail. Use pra preparar resposta concreta quando ha contexto claro. "
            "POLITICA: sempre Auto (e rascunho, reversivel)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Email do destinatario."},
                "subject": {"type": "string", "description": "Assunto."},
                "body": {"type": "string", "description": "Corpo do email em texto plano."},
                "account": {
                    "type": "string",
                    "enum": ["professional", "personal"],
                    "default": "professional",
                },
            },
            "required": ["recipient", "subject", "body"],
        },
    },
    {
        "name": "add_calendar_event",
        "description": (
            "Cria evento no Google Calendar (com Meet automatico). "
            "POLITICA: Auto-com-condicao — so use quando ja existe confirmacao via WA "
            "pelo Renato (ex: caso Orioli, Renato prometeu mandar Meet pra sex 10h). "
            "Voce DEVE setar confirmed_via_wa=true e citar a evidencia no description "
            "(ex: 'Renato prometeu via WA em 10/06 18:30')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {
                    "type": "string",
                    "description": "ISO 8601 com offset (ex: 2026-06-12T10:00:00-03:00 BRT).",
                },
                "duration_min": {"type": "integer", "default": 60},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista de emails dos convidados.",
                },
                "description": {"type": "string"},
                "confirmed_via_wa": {
                    "type": "boolean",
                    "description": "Setar TRUE so se ha evidencia textual de combinacao via WA.",
                    "default": False,
                },
            },
            "required": ["title", "start_iso", "confirmed_via_wa"],
        },
    },
    {
        "name": "send_wa_to_renato",
        "description": (
            "Manda mensagem CONVERSACIONAL pra Renato via WA (0192 -> 3337). "
            "PREFIRA essa tool sobre create_action_proposal quando: detecta sinal "
            "que demanda decisao/aprovacao do Renato (responder, agendar, cobrar, "
            "investir tempo em X). Renato responde texto/audio na mesma thread WA "
            "— o bot conversacional captura e atua na resposta. "
            "\n\nDOIS MODOS de rendering:\n"
            "- **PREFERIDO (natural):** passe `text` ja escrito na voz da Tonha. "
            "Sai como prosa, sem header '🤖 CoS Patrol', sem opcoes 1/2/3 "
            "obrigatorias, sem '_Responda texto ou audio._'. Use esse modo pra "
            "qualquer comunicacao pessoa-pra-pessoa: aviso, sintese, pergunta, "
            "rascunho pra aprovar. Aplica as regras da PERSONA/VOZ da Tonha "
            "(calma, sem entusiasmo performatico, texto corrido, sem emoji "
            "decorativo, lista numerada SO se decisao for discreta entre opcoes).\n"
            "- **fallback (structured):** se voce realmente precisa de opcoes "
            "numeradas explicitas pra Renato escolher por numero, passe "
            "`title` + `summary` + `options`. Usa header '🤖 CoS Patrol' "
            "padrao. Reservado pra decisoes binarias/A-B-C onde voce REALMENTE "
            "quer a UI de opcoes.\n"
            "- **system_alert:** marque `is_system_alert=true` pra cron health, "
            "erro de servico, alerta tecnico. Sai com '⚠ INTEL alerta'.\n"
            "\nPOLITICA: sempre Auto (e mensagem, reversivel por outra mensagem)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "PREFERIDO. Texto inteiro da mensagem, ja escrito "
                                   "na voz da Tonha (3-6 linhas tipicas; expanda so "
                                   "se ajudar). Sem headers, sem opcoes numeradas "
                                   "obrigatorias, sem rodape. Pode citar evidencia "
                                   "(msg_id, evento_id) inline quando relevante.",
                },
                "title": {
                    "type": "string",
                    "description": "Fallback structured. Titulo curto (max 100ch). "
                                   "Use SO se passar `summary` + `options` no modo "
                                   "structured. Vazio quando usar `text`.",
                },
                "summary": {
                    "type": "string",
                    "description": "Fallback structured. Contexto factual em 1-3 "
                                   "paragrafos. Vazio quando usar `text`.",
                },
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
                    "description": "Opcoes numeradas explicitas (1/2/3) — use SO se "
                                   "Renato realmente precisa escolher entre opcoes "
                                   "discretas. Max 4. Se a melhor resposta e uma "
                                   "pergunta aberta ou uma sintese, NAO use options "
                                   "— use o modo `text` em prosa.",
                },
                "is_system_alert": {
                    "type": "boolean",
                    "description": "True pra alerta tecnico/cron/erro de sistema. "
                                   "Forca rendering com '⚠ INTEL alerta' header. "
                                   "Default false.",
                    "default": False,
                },
                "urgency": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "default": "medium",
                },
                "proposed_action": {
                    "type": "object",
                    "description": "Acao concreta que o bot conversacional deve executar "
                                   "se Renato aprovar. Schema livre: {action: 'send_wa'|'send_email'|"
                                   "'create_task'|'snooze'|'dismiss', params: {...}}.",
                },
                "context_link": {
                    "type": "string",
                    "description": "Ref opcional (msg_id, evento_id, task_id).",
                },
                "contact_id": {
                    "type": "integer",
                    "description": "Contato relacionado, se houver.",
                },
            },
            # Um de: `text` OU (`title` + `summary`). Schema permissivo — defesa
            # no _tool_send_wa_to_renato cobre os dois caminhos.
        },
    },
    {
        "name": "schedule_wa_message",
        "description": (
            "Agenda envio de WA pra um contato em horario futuro (via scheduled_actions). "
            "Renato e notificado pos-envio. POLITICA: sempre Auto (e agendado, ainda tem "
            "espaco pra cancelar via /admin/scheduled-actions antes de disparar)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer"},
                "content": {"type": "string"},
                "when_iso": {"type": "string", "description": "ISO 8601 com offset BRT."},
                "dedup_key": {
                    "type": "string",
                    "description": "Chave UNIQUE pra idempotency (ex: 'sensor_orioli_meet_followup').",
                },
            },
            "required": ["contact_id", "content", "when_iso"],
        },
    },
]


def execute_sensor_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    policy: Dict[str, str],
) -> Dict[str, Any]:
    """Executa tool aplicando politica de autonomia.

    Politica:
    - 'auto': executa direto.
    - 'auto_cond': executa se condicao explicita atendida (validada por tool).
    - 'propor': converte em create_action_proposal (nao executa direto).
    """
    klass = policy.get(tool_name, "propor")  # default conservador

    # Se politica diz 'propor' e tool nao e create_action_proposal,
    # converte em proposal automaticamente.
    if klass == "propor" and tool_name != "create_action_proposal":
        title = f"[propor] {tool_name}: {str(tool_input)[:80]}"
        return _tool_create_action_proposal(
            action_type=f"sensor_propose_{tool_name}",
            title=title[:200],
            description=(
                f"Sensor sugeriu '{tool_name}' mas politica exige revisao.\n\n"
                f"Params: {json.dumps(tool_input, default=str, ensure_ascii=False)[:1000]}"
            ),
            urgency="medium",
            context_link=tool_input.get("context_link"),
            contact_id=tool_input.get("contact_id"),
        )

    # Dispatcher
    if tool_name == "create_action_proposal":
        return _tool_create_action_proposal(
            action_type=tool_input.get("action_type", "general"),
            title=tool_input.get("title", "(sem titulo)"),
            description=tool_input.get("description", ""),
            urgency=tool_input.get("urgency", "medium"),
            context_link=tool_input.get("context_link"),
            contact_id=tool_input.get("contact_id"),
        )
    if tool_name == "update_contact_notes":
        return _tool_update_contact_notes(
            contact_id=int(tool_input["contact_id"]),
            note_text=tool_input.get("note_text", ""),
        )
    if tool_name == "draft_email":
        return _tool_draft_email(
            recipient=tool_input["recipient"],
            subject=tool_input.get("subject", "(sem assunto)"),
            body=tool_input.get("body", ""),
            account=tool_input.get("account", "professional"),
        )
    if tool_name == "add_calendar_event":
        return _tool_add_calendar_event(
            title=tool_input["title"],
            start_iso=tool_input["start_iso"],
            duration_min=int(tool_input.get("duration_min") or 60),
            attendees=tool_input.get("attendees") or [],
            description=tool_input.get("description", ""),
            confirmed_via_wa=bool(tool_input.get("confirmed_via_wa", False)),
        )
    if tool_name == "schedule_wa_message":
        return _tool_schedule_wa_message(
            contact_id=int(tool_input["contact_id"]),
            content=tool_input.get("content", ""),
            when_iso=tool_input["when_iso"],
            dedup_key=tool_input.get("dedup_key"),
        )
    if tool_name == "send_wa_to_renato":
        return _tool_send_wa_to_renato(
            text=tool_input.get("text"),
            title=tool_input.get("title", ""),
            summary=tool_input.get("summary", ""),
            options=tool_input.get("options"),
            urgency=tool_input.get("urgency", "medium"),
            context_link=tool_input.get("context_link"),
            contact_id=tool_input.get("contact_id"),
            proposed_action=tool_input.get("proposed_action"),
            is_system_alert=bool(tool_input.get("is_system_alert", False)),
        )

    return {"success": False, "error": f"tool_desconhecida: {tool_name}", "audit_log_id": None}


# ============== Context loading ==============


def _load_context(window_min: int = 60, mock: Optional[Dict] = None) -> Dict[str, Any]:
    """Carrega contexto deste tick (msgs recentes, calendar 24h, proposals abertas, etc).

    Anti-loop: filtra mensagens outgoing onde metadata->>'from_webhook'='True' OU
    o conteudo bate em patterns do briefing diario do bot.
    """
    if mock:
        return mock

    now_brt = to_brt(now_utc())
    today_brt = now_brt.date()
    tomorrow_brt = today_brt + timedelta(days=1)
    # since: filtro UTC-naive contra colunas TIMESTAMP UTC-naive (consistente
    # com o DB). 14/06/26: datetime.now() em Vercel ja retorna UTC, igual ao
    # DB; nao precisa mexer no filtro.
    from datetime import timezone as _tz
    since = datetime.now(_tz.utc).replace(tzinfo=None) - timedelta(minutes=window_min)

    # Helper: converte naive UTC -> BRT string YYYY-MM-DD HH:MM (sem segundos)
    # pra exibir no prompt da Tonha. Sem isso ela ve hora UTC e pensa que e BRT.
    from zoneinfo import ZoneInfo as _ZI
    _UTC = _ZI("UTC")
    _BRT = _ZI("America/Sao_Paulo")
    def _to_brt_str(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return dt.astimezone(_BRT).strftime("%Y-%m-%d %H:%M BRT")

    # Calendario explicito pros proximos 7 dias — evita erro de weekday-arithmetic
    # do agent (ja vimos sex 13/06 em vez de sex 12/06). Lista YYYY-MM-DD + dia da semana
    # em PT-BR pra cada um dos 7 dias.
    _DIAS_PT = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]
    calendar_7d = []
    for offset in range(7):
        d = today_brt + timedelta(days=offset)
        calendar_7d.append({
            "date": d.isoformat(),
            "weekday": _DIAS_PT[d.weekday()],
            "label": "hoje" if offset == 0 else ("amanha" if offset == 1 else f"+{offset}d"),
        })

    ctx: Dict[str, Any] = {
        "now_brt": now_brt.isoformat(),
        "today_brt": today_brt.isoformat(),
        "today_weekday_pt": _DIAS_PT[today_brt.weekday()],
        "calendar_7d": calendar_7d,
        "window_min": window_min,
    }

    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Mensagens recentes (60min) — exclui outbound do bot
            cur.execute(
                """
                SELECT m.id, m.contact_id, c.nome AS contact_name,
                       m.direcao, m.conteudo, m.enviado_em, m.metadata
                FROM messages m
                LEFT JOIN contacts c ON c.id = m.contact_id
                WHERE COALESCE(m.enviado_em, m.criado_em) >= %s
                  AND NOT (
                    m.direcao = 'outgoing'
                    AND (
                      m.metadata->>'from_webhook' = 'True'
                      OR m.metadata->>'from_webhook' = 'true'
                      OR m.conteudo ILIKE 'Bom dia, Renato%%'
                      OR m.conteudo ILIKE '%%Briefing%%'
                    )
                  )
                ORDER BY COALESCE(m.enviado_em, m.criado_em) DESC
                LIMIT 40
                """,
                (since,),
            )
            ctx["msgs_recent"] = [
                {
                    "id": r["id"],
                    "contact_id": r["contact_id"],
                    "contact_name": r["contact_name"],
                    "direcao": r["direcao"],
                    "conteudo": (r["conteudo"] or "")[:400],
                    "enviado_em_brt": _to_brt_str(r["enviado_em"]),
                }
                for r in cur.fetchall()
            ]

            # Mensagens de grupo recentes (60min) — pra detectar cancelamentos etc
            cur.execute(
                """
                SELECT gm.id, gm.group_jid, gm.sender_name, gm.content, gm.timestamp, gm.from_me
                FROM group_messages gm
                WHERE gm.timestamp >= %s
                  AND gm.from_me = FALSE
                ORDER BY gm.timestamp DESC
                LIMIT 30
                """,
                (since,),
            )
            ctx["group_msgs_recent"] = [
                {
                    "id": r["id"],
                    "group_jid": r["group_jid"],
                    "sender_name": r["sender_name"],
                    "content": (r["content"] or "")[:300],
                    "timestamp_brt": _to_brt_str(r["timestamp"]),
                }
                for r in cur.fetchall()
            ]

            # Calendar proximas 24h
            cur.execute(
                """
                SELECT id, summary, start_datetime, end_datetime, location, description
                FROM calendar_events
                WHERE start_datetime >= NOW()
                  AND start_datetime < NOW() + INTERVAL '24 hours'
                ORDER BY start_datetime ASC
                LIMIT 15
                """
            )
            ctx["events_upcoming"] = [
                {
                    "id": r["id"],
                    "titulo": r["summary"],
                    "inicio_brt": _to_brt_str(r["start_datetime"]),
                    "fim_brt": _to_brt_str(r["end_datetime"]),
                    "local": r["location"],
                }
                for r in cur.fetchall()
            ]

            # Action proposals abertas (pra evitar dup)
            cur.execute(
                """
                SELECT id, action_type, title, contact_id, urgency, criado_em
                FROM action_proposals
                WHERE status = 'pending'
                ORDER BY criado_em DESC
                LIMIT 30
                """
            )
            ctx["proposals_open"] = [
                {
                    "id": r["id"],
                    "tipo": r["action_type"],
                    "titulo": (r["title"] or "")[:150],
                    "contact_id": r["contact_id"],
                    "urgency": r["urgency"],
                    "criado_em_brt": _to_brt_str(r["criado_em"]),
                }
                for r in cur.fetchall()
            ]

            # Scheduled actions pendentes (evita dup)
            cur.execute(
                """
                SELECT id, payload, scheduled_for, source, dedup_key
                FROM scheduled_actions
                WHERE status = 'pending'
                ORDER BY scheduled_for ASC
                LIMIT 10
                """
            )
            ctx["scheduled_open"] = [
                {
                    "id": r["id"],
                    "scheduled_for_brt": _to_brt_str(r["scheduled_for"]),
                    "source": r["source"],
                    "dedup_key": r["dedup_key"],
                }
                for r in cur.fetchall()
            ]

            # Pushes recentes da Tonha pro Renato (ultimas 6h) — dedup awareness.
            # 13/06/26: agente nao via o que ja tinha mandado e duplicava (Vitor 35s,
            # +5519 11min apart). Agora vai ver e decidir agregar/ignorar/atualizar.
            cur.execute(
                """
                SELECT id, content, created_at, tool_calls
                FROM bot_conversations
                WHERE phone = %s
                  AND role = 'assistant'
                  AND created_at >= NOW() - INTERVAL '6 hours'
                  AND (tool_calls::text ILIKE '%%cos_patrol%%' OR tool_calls::text ILIKE '%%cos_proposal%%')
                ORDER BY created_at DESC
                LIMIT 20
                """,
                ("5511984153337",),
            )
            ctx["recent_pushes"] = []
            for r in cur.fetchall():
                meta = r.get("tool_calls") or {}
                # psycopg2 jsonb -> dict; defensivo se vier string
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (TypeError, ValueError):
                        meta = {}
                push_contact_id = meta.get("contact_id") if isinstance(meta, dict) else None
                # Auto-resolve por outgoing: marca push como resolvido se Renato ja
                # respondeu pelo canal direto (WA celular OU email) APOS o push.
                resolved_by_outgoing = False
                if push_contact_id:
                    try:
                        cur.execute(
                            """
                            SELECT 1 FROM messages m
                            JOIN conversations cv ON cv.id = m.conversation_id
                            WHERE cv.contact_id = %s
                              AND m.direcao = 'outgoing'
                              AND m.enviado_em > %s
                            LIMIT 1
                            """,
                            (push_contact_id, r["created_at"]),
                        )
                        resolved_by_outgoing = cur.fetchone() is not None
                    except Exception:
                        pass
                ctx["recent_pushes"].append({
                    "id": r["id"],
                    "content": (r["content"] or "")[:300],
                    "created_at_brt": _to_brt_str(r["created_at"]),
                    "contact_id": push_contact_id,
                    "context_link": meta.get("context_link") if isinstance(meta, dict) else None,
                    "resolved_by_outgoing": resolved_by_outgoing,
                })

            # L1 — Memorias core (Tonha sempre acorda lembrando disso):
            # cos_config + glossario + correcao + relationship_edge + sintese
            # mais recente + ultimas decisoes/padroes/reflexoes. Cap ~6-7k tokens
            # total, cacheado via prompt cache do Sonnet.
            #
            # 14/06/26: virou onipresente porque a Tonha do CoS Patrol nao via
            # o que ela mesma escreveu na sintese diaria nem o glossario que
            # ela mesma criava — cada tick comecava do zero. Agora nao mais.
            ctx["l1_memories"] = []
            _l1_specs = [
                # (tipo, limit, max_age_days)
                ("cos_config", 1, None),
                ("sintese_diaria", 1, None),
                ("glossario", 50, None),
                ("relationship_edge", 50, None),
                ("correcao", 20, 90),
                ("decisao", 5, 60),
                ("compromisso", 5, 60),
                ("padrao", 3, 60),
                ("reflexao", 3, 60),
            ]
            for tipo, lim, age in _l1_specs:
                try:
                    if age:
                        cur.execute(
                            "SELECT id, titulo, conteudo, tipo, tags, criado_em "
                            "FROM system_memories WHERE tipo = %s "
                            "AND criado_em >= NOW() - (%s || ' days')::interval "
                            "ORDER BY criado_em DESC LIMIT %s",
                            (tipo, str(age), lim),
                        )
                    else:
                        cur.execute(
                            "SELECT id, titulo, conteudo, tipo, tags, criado_em "
                            "FROM system_memories WHERE tipo = %s "
                            "ORDER BY criado_em DESC LIMIT %s",
                            (tipo, lim),
                        )
                    for row in cur.fetchall():
                        cont = (row["conteudo"] or "")
                        # cos_config eh longa por design; outras truncar pra L1
                        max_len = 3500 if tipo == "cos_config" else 1500
                        if len(cont) > max_len:
                            cont = cont[:max_len] + "...[truncado]"
                        ctx["l1_memories"].append({
                            "id": row["id"],
                            "titulo": row["titulo"],
                            "conteudo": cont,
                            "tipo": row["tipo"],
                            "tags": row.get("tags"),
                            "criado_em_brt": _to_brt_str(row["criado_em"]),
                        })
                except Exception as e:
                    logger.warning(f"L1 load failed for tipo={tipo}: {e}")

            # RACI criticos pra Vallen/Alba/Despertar — fonte AUTORITATIVA eh
            # ConselhoOS (DB separado). INTEL.tasks tinha dados stale e gerou
            # proposta #714 falsa em 13/06.
            # Pra projetos fora do ConselhoOS (Assespro/imensIAH/internas), usar
            # INTEL.tasks como fallback (com label "INTEL — pode ter ruido").
            ctx["raci_critical"] = []
            ctx["tasks_overdue_intel"] = []

            # 1. ConselhoOS — canonical pra clientes do conselho (Renato R ou A)
            try:
                import psycopg2 as _pg
                cos_url = os.getenv("CONSELHOOS_DATABASE_URL", "").strip()
                if cos_url:
                    co_conn = _pg.connect(cos_url)
                    co_cur = co_conn.cursor()
                    co_cur.execute(
                        """
                        SELECT r.id::text AS id, r.acao, r.prazo,
                               COALESCE(e.nome, 'sem empresa') AS empresa,
                               r.responsavel_r, r.responsavel_a, r.status
                        FROM raci_itens r
                        LEFT JOIN empresas e ON e.id = r.empresa_id
                        WHERE r.status IN ('pendente', 'atrasado', 'em_andamento')
                          AND r.prazo IS NOT NULL
                          AND r.prazo < CURRENT_DATE
                          AND (
                            r.responsavel_r ILIKE %s OR r.responsavel_a ILIKE %s
                          )
                        ORDER BY r.prazo ASC
                        LIMIT 5
                        """,
                        ("%Renato%", "%Renato%"),
                    )
                    for row in co_cur.fetchall():
                        ctx["raci_critical"].append({
                            "raci_id": row[0],
                            "acao": (row[1] or "")[:120],
                            "vencimento": row[2].isoformat() if row[2] else None,
                            "empresa": row[3],
                            "responsavel_r": row[4],
                            "responsavel_a": row[5],
                            "status": row[6],
                            "fonte": "ConselhoOS (canonical)",
                        })
                    co_conn.close()
            except Exception as e:
                logger.warning(f"cos_sensor: ConselhoOS RACI query falhou: {e}")

            # 2. INTEL tasks — fallback pra projetos fora do ConselhoOS
            #    (Assespro/imensIAH/internas)
            cur.execute(
                """
                SELECT t.id, t.titulo, t.data_vencimento, p.nome AS projeto
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                WHERE t.status != 'done'
                  AND t.data_vencimento IS NOT NULL
                  AND t.data_vencimento < NOW()
                  AND (
                    p.nome ILIKE '%imensIAH%' OR p.nome ILIKE '%Assespro%'
                  )
                ORDER BY t.data_vencimento ASC
                LIMIT 5
                """
            )
            ctx["tasks_overdue_intel"] = [
                {
                    "task_id": r["id"],
                    "titulo": (r["titulo"] or "")[:100],
                    "vencimento_brt": _to_brt_str(r["data_vencimento"]),
                    "projeto": r["projeto"],
                    "fonte": "INTEL.tasks (pode ter ruido — checar antes de propor)",
                }
                for r in cur.fetchall()
            ]

    except Exception as e:
        logger.warning(f"cos_sensor._load_context falhou: {e}")
        ctx["erro_contexto"] = str(e)

    return ctx


def _get_cos_config_content() -> str:
    """Pega cos_config ativa (system_memories tipo='cos_config')."""
    try:
        from services.system_memory import get_active_cos_config

        cfg = get_active_cos_config()
        if cfg and cfg.get("conteudo"):
            return cfg["conteudo"]
    except Exception as e:
        logger.warning(f"cos_sensor._get_cos_config_content falhou: {e}")
    return ""


# ============== Budget check ==============


def _check_budget() -> Dict[str, Any]:
    """Verifica gasto Anthropic do dia. Aborta se passou $0.50/dia."""
    today_iso = to_brt(now_utc()).date().isoformat()
    today_usd = 0.0
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COALESCE(SUM((result_json->>'cost_usd')::float), 0) AS sum_usd
                FROM cron_runs
                WHERE path = '/api/cron/cos-sensor-tick'
                  AND started_at >= %s::date
                  AND started_at <  (%s::date + INTERVAL '1 day')
                  AND result_json ? 'cost_usd'
                """,
                (today_iso, today_iso),
            )
            r = cur.fetchone()
            if r:
                today_usd = float(r.get("sum_usd") or 0)
    except Exception as e:
        logger.warning(f"_check_budget falhou: {e}")

    if today_usd > COS_SENSOR_DAILY_CAP_USD:
        return {
            "abort": True,
            "reason": f"daily_cap_hit: ${today_usd:.3f} > ${COS_SENSOR_DAILY_CAP_USD:.2f}",
            "today_usd": today_usd,
        }
    return {"abort": False, "today_usd": today_usd}


# ============== Prompts ==============

_SYSTEM_PROMPT_TEMPLATE = """Voce e o CoS Sensor Agent do Renato Almeida Prado — voce e a Tonha (persona do CoS, matriarca brasileira do interior, calma e com gravidade) operando em modo proativo.

Voce roda a cada 30min. A cada tick voce le o estado do mundo (mensagens, calendar, propostas abertas, RACI critico), DECIDE se ha sinal novo que demanda acao, e EXECUTA via tools (ou propoe pra revisao quando a politica exige).

==== MEMORIA CORE (L1) — sempre carregada, voce JA SABE disso ====

Essas memorias sao a sua base permanente. Voce nao precisa buscar — ja estao
no seu cerebro. Use livremente: pra interpretar idioms (glossario), respeitar
correcoes anteriores que o Renato te deu (correcao), reconhecer pessoas e
relacionamentos (relationship_edge), aplicar suas politicas (cos_config),
lembrar do dia anterior (sintese_diaria), e respeitar decisoes/padroes ja
identificados (decisao, compromisso, padrao, reflexao).

REGRA: se uma memoria de tipo='correcao' bate no contexto atual, OBEDECA.
Renato te ensinou na ocasiao, voce nao repete o erro.

REGRA: se uma memoria de tipo='glossario' bate em algo dito (expressao,
apelido, gíria), NAO interprete literal. Use o glossario.

{l1_memories_json}

==== PRIORIDADES (compass) ====

{cos_config}

==== POLITICA DE AUTONOMIA ====

Voce tem 5 tools no MVP. Cada tool tem uma classe:
- 'auto': executa direto (reversivel ou baixo risco).
- 'auto_cond': executa se condicao explicita atendida (voce DEVE marcar a condicao).
- 'propor': cria action_proposal pra Renato revisar (voce nunca executa direto).

Tools deste tick e classes:
{policy_lines}

Se voce esta em duvida sobre uma acao, DEFAULTE pra create_action_proposal (sempre Auto).

==== CONTEXTO ====

now_brt: {now_brt}
today_brt: {today_brt} ({today_weekday_pt})

Calendario explicito 7d (USE pra resolver "sexta", "amanha", "proxima quarta", etc):
{calendar_7d_json}

Mensagens diretas (WA, 60min, exclui outbound do bot):
{msgs_recent_json}

Mensagens de grupo (60min, so de terceiros):
{group_msgs_recent_json}

Calendar proximas 24h:
{events_upcoming_json}

Action proposals abertas (NAO DUPLIQUE — se ja existe proposta sobre o sinal, NAO crie outra):
{proposals_open_json}

Mensagens WA agendadas pendentes (NAO duplique):
{scheduled_open_json}

Pushes que VOCE (Tonha) ja mandou pro Renato nas ultimas 6h — NAO REDIGA o mesmo
push outra vez. Se o sinal ja foi avisado e nao ha mudanca substancial, SILENCIE.
Se ha mudanca (msg nova do mesmo contato, info adicional), CONSOLIDE com o push
anterior em vez de mandar push novo do zero:
{recent_pushes_json}

RACI critico vencido — ConselhoOS (Vallen/Alba/Despertar — fonte CANONICAL):
{raci_critical_json}

Tasks INTEL vencidas (imensIAH/Assespro — fallback, PODE TER RUIDO,
checar contexto antes de propor):
{tasks_overdue_intel_json}

==== PROCESSO DE DECISAO ====

1. Analise contexto procurando sinais NOVOS (nao cobertos por proposals_open / scheduled_open).
2. **CHECK DE POLITICA PRIMEIRO** — antes de criar proposal ou executar:
   - A acao alvo cai em domingo? Politica C2 (zero trabalho domingo) → SILENCIE
     (nao crie proposal, nao agende lembrete; tratamento eh ignorar 100%).
   - A acao viola horario E1 (WhatsApp fora das janelas)? → SILENCIE.
   - Em duvida sobre politica, prefira SILENCIAR a "criar com ressalva".
3. Para sinais que passam o check de politica:
   - Ha acao concreta? Se nao, ignore.
   - Posso executar autonomamente per politica de tool? Se sim, chame direto.
   - **MODO SHADOW 13/06/26**: pra QUALQUER proposta que precise revisao do
     Renato, PREFIRA `send_wa_to_renato` (conversacional, ele responde texto/audio)
     sobre `create_action_proposal` (vira card no dashboard, ele nao quer mais
     ler dashboard). Use create_action_proposal SOMENTE quando: (a) ja existe
     proposta similar em proposals_open e voce quer apenas atualizar/agrupar,
     OU (b) e contexto puramente operacional que NAO precisa decisao do Renato
     (ex: operational_risk pra time monitorar via outro canal).

   - **VOZ DA TONHA quando usar send_wa_to_renato**:
     Voce e a Tonha — matriarca brasileira do interior, calma, presente, sem
     pressa, sem entusiasmo performatico. Trata Renato por "voce". WhatsApp pede
     economia: 3-6 linhas e o padrao. Expanda so se ha substancia real.

     PROIBIDO em send_wa_to_renato:
     - Emojis decorativos (✅ ❌ 🎯 🚀 🤖 banidos). Excecao: ⚠ pra system alert.
     - "ANOTADO!", "Perfeito!", "Achei!", "Vou registrar!" — CRM transacional.
     - Listas numeradas 1/2/3 como reflexo. Use SO se a decisao e genuinamente
       discreta entre opcoes (ex: aceitar/recusar/pedir pauta). Se a melhor
       resposta e uma pergunta aberta, faca pergunta. Se e aviso, escreva prosa.
     - Header "🤖 CoS Patrol", footer "_Responda texto ou audio._" — Renato
       sabe que pode responder, nao precisa lembrete. O `text` mode sai sem
       header automaticamente.
     - Negrito como decoracao. Negrito raramente, so pra UMA palavra critica.

   - **COMO USAR send_wa_to_renato — DOIS MODOS**:

     PREFERIDO — `text` mode (prosa natural):
       Passe o argumento `text` com a mensagem ja escrita na voz da Tonha.
       Sai direto, sem template, sem header, sem opcoes obrigatorias.
       Use sempre que possivel.

       Exemplo BOM:
         text: "Um numero novo (+55 19 99551-2595) pediu reuniao quinta as 9h
                e mandou o email contato@fazendanovaalianca.com.br. Nao tenho
                contexto sobre quem e nem sobre o que quer falar. Voce conhece
                a Fazenda Nova Alianca, ou quer que eu peca pra ele se
                identificar primeiro?"

       Exemplo BOM (decisao discreta — usa opcoes mas em prosa):
         text: "O Manoel mandou um audio agora ha pouco. Nao tenho transcricao
                pra avaliar se e urgente. Te transcrevo e te resumo a noite,
                ou voce prefere ouvir direto agora?"

     fallback — `title + summary + options` (structured, header CoS Patrol):
       Use SO quando voce REALMENTE precisa de opcoes numeradas explicitas pra
       Renato responder por numero (ex: decisao binaria de aceitar/recusar
       reuniao agendada). Maioria dos casos NAO precisa disso — prefira `text`.

     system_alert — passe `is_system_alert=true`:
       Reservado pra alerta tecnico (cron falhou, erro de servico). Sai com
       header "⚠ INTEL alerta". Nao use pra comunicacao com Renato sobre
       contatos/projetos — isso e conversa, vai em `text` mode.

   - **CONSOLIDACAO**: se voce identifica 2-3 sinais novos pra avisar o Renato
     no mesmo tick, NAO mande 3 pushes separados. Junte em UMA mensagem em
     prosa, citando cada um: "tres coisas no fim da tarde: ...". Push em
     rajada e o que mais irrita ele (regra dele).

   - **DEDUP**: antes de chamar send_wa_to_renato, VEJA recent_pushes acima.
     Se o sinal ja foi avisado e nao houve mudanca substancial, SILENCIE.
     Se houve mudanca (nova msg, info adicional), ainda assim NAO mande push
     novo: deixa pra ele responder o anterior, OU consolide na proxima rajada.

   - **AUTO-RESOLVE POR OUTGOING**: cada item em recent_pushes tem
     `resolved_by_outgoing`. Se TRUE, Renato JA respondeu direto pelo canal
     (WA pelo celular OU email pelo Gmail) — fechado. NUNCA refrescar essa
     proposta nem cobrar de novo. Use isso como dedup prioritario tambem
     pra novos sinais relacionados ao mesmo contato.
4. **TITULOS TEMPORAIS** — quando referenciar datas, USE calendar_7d:
   - offset=0 = "hoje" / today_brt
   - offset=1 = "amanha"
   - Outros = use a data ISO + weekday PT-BR explicito ("sabado 14/06")
   - NUNCA escreva "hoje" em titulo se a acao alvo for em outro dia.
5. Chame as tools necessarias. Se nao ha nada novo (ou tudo bate em politica),
   responda em texto sem tool_call (1-2 linhas).

==== EXEMPLOS DE SINAL ====

- Funcionaria-chave de cliente vai sair (cirurgia/atestado) -> send_wa_to_renato com opcoes
  ["Cobrir agora", "Falar com cliente", "Snooze 24h"].
- Renato prometeu via WA gerar Meet + ha mensagem do contato cobrando -> add_calendar_event
  com confirmed_via_wa=true (Auto puro, nao precisa pergunta).
- Reuniao cancelada via msg de grupo "sem pauta" -> send_wa_to_renato pra confirmar
  reagendamento.
- Contato revelou preferencia/fato novo -> update_contact_notes (Auto puro).
- Compromisso futuro precisa lembrete -> schedule_wa_message com dedup_key (Auto puro).
- Cliente cobrando draft de proposta -> send_wa_to_renato com proposed_action
  {{action: "send_email", params: {{...}}}} e opcoes ["Aprovar e enviar", "Modificar", "Snooze"].

==== RESTRICOES ====

- **SO FATOS NO RESUMO** — NUNCA especule sobre identidade, profissao, filhos,
  doencas, contexto de vida de contato. Se nao esta em manual_notes/cargo/tags
  ou no proprio texto da mensagem, NAO inventa. Caso real 13/06/26: Tonha disse
  "cataporas das criancas da Emma" — Emma e massagista sem filhos, era cliente
  dela. Se ambiguo, escreva so o fato ("Emma falou 'cataporas explodindo'") e
  pergunte ao Renato em vez de hipotetizar. Padroes proibidos no summary:
  "pode ser X", "vale checar — pode ser Y", "provavelmente refere-se a Z" sem
  evidencia textual.
- NAO envie emails ou WAs direto. Use draft_email ou schedule_wa_message.
- NAO atualize cos_config, NAO delete dados.
- NAO duplique proposals_open. Cheque 'titulo' e 'tipo' antes.
- Confianca baixa? Ignore. Silencio e OK.
- Em duvida sobre politica, defaulte pra create_action_proposal.

==== FECHAMENTO ====

Quando terminar (ou nada pra fazer), responda em texto curto (1-2 linhas) com o resumo do tick.
"""


def _build_system_prompt(cos_config: str, policy: Dict[str, str], context: Dict[str, Any]) -> str:
    policy_lines = "\n".join(f"- {tool}: {klass}" for tool, klass in policy.items())
    return _SYSTEM_PROMPT_TEMPLATE.format(
        cos_config=cos_config or "(sem cos_config ativa — use defaults conservadores)",
        policy_lines=policy_lines,
        now_brt=context.get("now_brt", "?"),
        today_brt=context.get("today_brt", "?"),
        today_weekday_pt=context.get("today_weekday_pt", "?"),
        calendar_7d_json=json.dumps(context.get("calendar_7d", []), default=str, ensure_ascii=False, indent=2),
        msgs_recent_json=json.dumps(context.get("msgs_recent", []), default=str, ensure_ascii=False, indent=2)[:6000],
        group_msgs_recent_json=json.dumps(context.get("group_msgs_recent", []), default=str, ensure_ascii=False, indent=2)[:4000],
        events_upcoming_json=json.dumps(context.get("events_upcoming", []), default=str, ensure_ascii=False, indent=2)[:3000],
        proposals_open_json=json.dumps(context.get("proposals_open", []), default=str, ensure_ascii=False, indent=2)[:4000],
        scheduled_open_json=json.dumps(context.get("scheduled_open", []), default=str, ensure_ascii=False, indent=2)[:1500],
        recent_pushes_json=json.dumps(context.get("recent_pushes", []), default=str, ensure_ascii=False, indent=2)[:3000],
        l1_memories_json=json.dumps(context.get("l1_memories", []), default=str, ensure_ascii=False, indent=2)[:8000],
        raci_critical_json=json.dumps(context.get("raci_critical", []), default=str, ensure_ascii=False, indent=2)[:2000],
        tasks_overdue_intel_json=json.dumps(context.get("tasks_overdue_intel", []), default=str, ensure_ascii=False, indent=2)[:1500],
    )


# ============== Main agent class ==============


class CoSSensorAgent:
    """Sensor Agent — roda 1 tick (le contexto, chama LLM, executa tools)."""

    def __init__(self, mock_context: Optional[Dict] = None):
        self.policy = load_autonomy_policy()
        self.mock_context = mock_context

    def tick(self) -> Dict[str, Any]:
        started = time.time()

        # Budget check
        budget = _check_budget()
        if budget.get("abort"):
            logger.warning(f"cos_sensor.tick: abort {budget.get('reason')}")
            return {
                "status": "aborted_budget",
                "reason": budget.get("reason"),
                "today_usd": budget.get("today_usd"),
                "duration_ms": int((time.time() - started) * 1000),
            }

        if not ANTHROPIC_API_KEY:
            return {
                "status": "skipped",
                "reason": "no_api_key",
                "duration_ms": int((time.time() - started) * 1000),
            }

        try:
            import anthropic
        except ImportError:
            return {
                "status": "error",
                "reason": "anthropic_sdk_missing",
                "duration_ms": int((time.time() - started) * 1000),
            }

        # 1. Contexto + config
        context = _load_context(window_min=60, mock=self.mock_context)
        cos_config = _get_cos_config_content()

        # 2. Prompts
        system_prompt = _build_system_prompt(cos_config, self.policy, context)
        user_prompt = (
            "Execute o tick. Identifique sinais novos no contexto e aja conforme a politica. "
            "Se nao ha nada novo, responda em texto curto sem tool_call."
        )

        # 3. Anthropic client
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system_param = [
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
        messages: List[Dict[str, Any]] = [{"role": "user", "content": user_prompt}]

        total_input = total_output = cache_creation = cache_read = 0
        tool_calls: List[Dict[str, Any]] = []
        iterations_done = 0
        final_text = ""
        error_message = None
        last_stop_reason = None

        try:
            for iteration in range(MAX_ITERATIONS):
                iterations_done = iteration + 1
                try:
                    response = client.messages.create(
                        model=COS_SENSOR_MODEL,
                        max_tokens=MAX_TOKENS_PER_ITER,
                        system=system_param,
                        tools=SENSOR_TOOLS,
                        messages=messages,
                    )
                except Exception as api_err:
                    error_message = f"api_call_failed iter={iteration}: {api_err}"
                    logger.warning(error_message)
                    break

                usage = response.usage
                total_input += getattr(usage, "input_tokens", 0) or 0
                total_output += getattr(usage, "output_tokens", 0) or 0
                cache_creation += getattr(usage, "cache_creation_input_tokens", 0) or 0
                cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
                last_stop_reason = response.stop_reason

                messages.append({"role": "assistant", "content": response.content})

                tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
                for b in response.content:
                    if getattr(b, "type", None) == "text":
                        txt = (getattr(b, "text", "") or "").strip()
                        if txt:
                            final_text = txt

                if response.stop_reason != "tool_use" or not tool_use_blocks:
                    break

                tool_results = []
                for tu in tool_use_blocks:
                    tool_name = getattr(tu, "name", "")
                    tool_input = dict(getattr(tu, "input", {}) or {})
                    tool_use_id = getattr(tu, "id", "")
                    result = execute_sensor_tool(tool_name, tool_input, self.policy)
                    tool_calls.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "result": result,
                    })
                    result_str = json.dumps(result, default=str, ensure_ascii=False)
                    if len(result_str) > 4000:
                        result_str = result_str[:3990] + "...[trunc]"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_str,
                    })
                messages.append({"role": "user", "content": tool_results})

        except Exception as loop_err:
            error_message = f"loop_failed: {loop_err}"
            logger.exception("cos_sensor.tick loop falhou")

        duration_ms = int((time.time() - started) * 1000)

        # Cost (Sonnet 4.6: $3/$15 per 1M)
        cost_usd = (
            (total_input * 3.0 / 1_000_000)
            + (total_output * 15.0 / 1_000_000)
            + (cache_creation * 3.75 / 1_000_000)
            + (cache_read * 0.30 / 1_000_000)
        )

        status = "success" if not error_message else ("partial" if tool_calls else "error")

        return {
            "status": status,
            "iterations": iterations_done,
            "tool_calls": tool_calls,
            "tool_count": len(tool_calls),
            "tokens": {
                "input": total_input,
                "output": total_output,
                "cache_creation": cache_creation,
                "cache_read": cache_read,
            },
            "cost_usd": round(cost_usd, 4),
            "duration_ms": duration_ms,
            "last_stop_reason": last_stop_reason,
            "final_text": final_text[:500] if final_text else None,
            "error_message": error_message,
        }


def tick_safe() -> Dict[str, Any]:
    """Wrapper que captura excecoes e grava em audit_log."""
    import traceback
    try:
        from services.audit_log import log as audit_log
    except Exception:
        audit_log = None  # type: ignore

    try:
        result = CoSSensorAgent().tick()
        if audit_log:
            audit_log(
                "cos_sensor.tick",
                actor="cos_sensor",
                details={
                    "status": result.get("status"),
                    "tool_count": result.get("tool_count"),
                    "cost_usd": result.get("cost_usd"),
                    "duration_ms": result.get("duration_ms"),
                    "error": result.get("error_message"),
                },
            )
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception(f"cos_sensor.tick_safe crashed: {e}")
        if audit_log:
            audit_log(
                "cos_sensor.tick_error",
                actor="cos_sensor",
                details={"error": str(e), "type": type(e).__name__, "traceback": tb[:3000]},
            )
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": tb.splitlines()[-15:],
        }
