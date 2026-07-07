"""
WA Backfill 1:1 — puxa DMs diretas do WhatsApp pessoal pra whatsapp_messages.

Contexto (formação S08 da Tonia): o webhook Evolution ao vivo só persiste
mensagens de GRUPO (group_messages). Conversas 1:1 nunca entram — a tabela
whatsapp_messages só tinha um backfill/export únicos, congelados em 13/06/26.
Resultado: qualquer raciocínio da Tonia sobre relação conduzida por DM 1:1
ficava cego (ex.: sondagem à Juliana em 28/06 sobre a demanda Eslovênia/300t
existia no Evolution mas não no INTEL).

Este módulo fecha o gap via pull agendado (não toca o webhook vivo):
findMessages por contato relevante → upsert idempotente (ON CONFLICT message_id).

Escopo por rodada: contatos com telefone whatsapp:true E (círculo <= 2 OU
vinculados a projeto ativo). ~55 contatos hoje — exatamente quem a Tonia
raciocina. Cap por rodada via max_contacts.

NÃO substitui um webhook 1:1 ao vivo (evolução futura). É catch-up com lag <=1d.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from database import get_db
from integrations.evolution_api import get_evolution_client

log = logging.getLogger("intel.wa_backfill")

IMPORTED_FROM = "evolution_backfill_cron"
DEFAULT_INSTANCE = "rap-whatsapp"  # linha pessoal do Renato (não o intel-bot)


# Contatos relevantes: whatsapp:true E (círculo<=2 OU projeto ativo).
# Ordena por prioridade (círculo asc, depois id) pra que o cap respeite o topo.
_SELECT_CONTACTS = """
    SELECT DISTINCT c.id, c.nome, c.telefones, COALESCE(c.circulo, 9) AS circulo
      FROM contacts c
     WHERE c.telefones::text LIKE '%%"whatsapp": true%%'
       AND (
             COALESCE(c.circulo, 9) <= 2
          OR EXISTS (
               SELECT 1 FROM tasks t
                 JOIN projects p ON p.id = t.project_id
                WHERE t.contact_id = c.id AND p.status = 'ativo'
             )
           )
  ORDER BY circulo ASC, c.id ASC
     LIMIT %s
"""

_UPSERT = """
    INSERT INTO whatsapp_messages
        (contact_id, phone, message_id, direction, content, message_type,
         message_date, imported_from, criado_em)
    VALUES (%(contact_id)s, %(phone)s, %(message_id)s, %(direction)s,
            %(content)s, %(message_type)s, %(message_date)s, %(imported_from)s, NOW())
    ON CONFLICT (message_id) DO NOTHING
"""

# Resolve contato por sufixo do telefone + flag de relevância (mesma política
# do backfill: círculo<=2 OU projeto ativo). Usado pela persistência ao vivo.
_RESOLVE_CONTACT = """
    SELECT c.id, c.nome,
           (COALESCE(c.circulo, 9) <= 2 OR EXISTS (
               SELECT 1 FROM tasks t JOIN projects p ON p.id = t.project_id
                WHERE t.contact_id = c.id AND p.status = 'ativo')) AS relevant
      FROM contacts c
     WHERE c.telefones::text LIKE %s
     LIMIT 5
"""

LIVE_IMPORTED_FROM = "evolution_live_1to1"


def _first_wa_phone(telefones: Any) -> Optional[str]:
    """Extrai o primeiro número marcado whatsapp:true. Retorna só dígitos."""
    if isinstance(telefones, str):
        try:
            telefones = json.loads(telefones)
        except Exception:
            return None
    if not isinstance(telefones, list):
        return None
    for tel in telefones:
        if isinstance(tel, dict) and tel.get("whatsapp") and tel.get("number"):
            digits = "".join(ch for ch in str(tel["number"]) if ch.isdigit())
            if len(digits) >= 8:
                return digits
    return None


def _extract_text(message_obj: Dict[str, Any]) -> Optional[str]:
    if not isinstance(message_obj, dict):
        return None
    if message_obj.get("conversation"):
        return message_obj["conversation"]
    ext = message_obj.get("extendedTextMessage") or {}
    if isinstance(ext, dict) and ext.get("text"):
        return ext["text"]
    return None


def _parse_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normaliza um record do findMessages. Retorna None se não for texto 1:1."""
    key = rec.get("key") or {}
    remote_jid = key.get("remoteJid", "") or ""
    if "@g.us" in remote_jid:  # defensivo: nunca deveria vir grupo aqui
        return None
    message_id = key.get("id")
    if not message_id:
        return None
    text = _extract_text(rec.get("message") or {})
    if not text:  # v1: só texto (media fica pra evolução futura)
        return None

    ts = rec.get("messageTimestamp")
    try:
        ts_int = int(ts)
        msg_date = datetime.fromtimestamp(ts_int, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        msg_date = None

    phone = "".join(ch for ch in remote_jid.split("@")[0] if ch.isdigit()) or None

    return {
        "message_id": str(message_id),
        "direction": "outbound" if key.get("fromMe") else "inbound",
        "content": text,
        "message_type": rec.get("messageType") or "conversation",
        "message_date": msg_date,
        "phone": phone,
    }


def _resolve_relevant_contact(phone: str) -> Optional[Dict[str, Any]]:
    """Acha contato relevante (círculo<=2 OU projeto ativo) por sufixo do
    telefone. Retorna {id, nome} ou None (desconhecido/irrelevante)."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) < 8:
        return None
    suffix = digits[-9:] if len(digits) >= 9 else digits[-8:]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_RESOLVE_CONTACT, (f"%{suffix}%",))
        for row in cur.fetchall():
            if row["relevant"]:
                return {"id": row["id"], "nome": row["nome"]}
    return None


def persist_live_direct_message(data: Dict[str, Any], shadow: bool = False) -> Dict[str, Any]:
    """
    Persiste UMA DM 1:1 vinda do webhook (payload['data']), se o contato for
    relevante. Chamado pelo webhook Evolution como side-effect. Idempotente
    (ON CONFLICT message_id). Reusa _parse_record/_UPSERT do backfill.

    shadow=True: resolve e diz o que FARIA, sem escrever (rollout seguro).
    """
    parsed = _parse_record(data)
    if not parsed:
        return {"skipped": "no_text"}
    if not parsed.get("phone"):
        return {"skipped": "no_phone"}

    contact = _resolve_relevant_contact(parsed["phone"])
    if not contact:
        return {"skipped": "unknown_or_irrelevant"}

    if shadow:
        return {"shadow_would_persist": contact["nome"], "dir": parsed["direction"]}

    parsed["contact_id"] = contact["id"]
    parsed["imported_from"] = LIVE_IMPORTED_FROM
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_UPSERT, parsed)
        conn.commit()
        return {"persisted": cur.rowcount, "contact": contact["nome"], "dir": parsed["direction"]}


async def backfill_direct_messages(
    max_contacts: int = 80,
    msg_limit: int = 50,
    instance: str = DEFAULT_INSTANCE,
) -> Dict[str, Any]:
    """
    Puxa DMs 1:1 recentes dos contatos relevantes e faz upsert idempotente.

    Args:
        max_contacts: teto de contatos processados por rodada.
        msg_limit: mensagens recentes buscadas por contato.
        instance: instância Evolution (linha pessoal).

    Returns:
        Sumário {contacts_scanned, messages_seen, inserted, skipped, errors, ...}.
    """
    evo = get_evolution_client()
    if not evo.is_configured:
        return {"error": "Evolution não configurado (EVOLUTION_API_URL/KEY)"}

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_SELECT_CONTACTS, (max_contacts,))
        contacts = [dict(r) for r in cur.fetchall()]

    stats = {
        "contacts_scanned": 0,
        "contacts_no_phone": 0,
        "contacts_errored": 0,
        "messages_seen": 0,
        "messages_text": 0,
        "inserted": 0,
        "instance": instance,
    }
    errored_names: List[str] = []

    # Uma conexão de escrita pra rodada inteira (evita 55 conexões curtas).
    with get_db() as wconn:
        wcur = wconn.cursor()
        for c in contacts:
            phone = _first_wa_phone(c.get("telefones"))
            if not phone:
                stats["contacts_no_phone"] += 1
                continue

            stats["contacts_scanned"] += 1
            try:
                resp = await evo.get_messages(phone, limit=msg_limit, instance_name=instance)
            except Exception as e:
                stats["contacts_errored"] += 1
                errored_names.append(f"{c['nome']}({type(e).__name__})")
                continue

            if not isinstance(resp, dict) or resp.get("error"):
                stats["contacts_errored"] += 1
                errored_names.append(f"{c['nome']}(api:{resp.get('status_code') if isinstance(resp, dict) else '?'})")
                continue

            records = ((resp.get("messages") or {}).get("records")) or []
            stats["messages_seen"] += len(records)

            inserted_c = 0
            for rec in records:
                parsed = _parse_record(rec)
                if not parsed:
                    continue
                parsed["contact_id"] = c["id"]
                parsed["imported_from"] = IMPORTED_FROM
                stats["messages_text"] += 1
                wcur.execute(_UPSERT, parsed)
                inserted_c += wcur.rowcount  # 1 se inseriu, 0 se ON CONFLICT
            stats["inserted"] += inserted_c
        wconn.commit()

    if errored_names:
        stats["errored_sample"] = errored_names[:8]

    log.info(
        "wa_backfill: scanned=%s inserted=%s seen=%s errored=%s",
        stats["contacts_scanned"], stats["inserted"],
        stats["messages_seen"], stats["contacts_errored"],
    )
    return {"job": "wa-backfill-1to1", **stats}
