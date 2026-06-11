"""
WhatsApp catchup — recupera mensagens DM perdidas pelo webhook.

Why: webhook Evolution as vezes perde mensagens silenciosamente (caso Felipe
Orioli +351938588722 / 197864965841105@lid em 11/jun/26). Esse modulo:
1. Lista contatos prioritarios (circulo<=4 OU msg nos ultimos 7d)
2. Pra cada um, fetch /chat/findMessages das ultimas 20 msgs
3. Compara contra messages.external_id (ja existem?)
4. Pras faltantes, simula payload de webhook e chama handler — assim a
   telemetria webhook_audit tambem grava com source='catchup'

Roda a cada 30min via Railway scheduler.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def _evolution_base() -> tuple[str, str, str]:
    url = os.getenv("EVOLUTION_API_URL", "").replace("\\n", "").replace("\\r", "").strip().rstrip("/")
    key = os.getenv("EVOLUTION_API_KEY", "").strip()
    instance = os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp").strip()
    return url, key, instance


async def _find_messages_for_jid(jid: str, page: int = 1, limit: int = 50) -> List[Dict]:
    """POST /chat/findMessages — retorna lista de records.

    Why: Evolution v2 retorna {messages: {records: [...]}} OU lista direta.
    Lidamos com ambos. Page-paginated (50/page hardcoded server-side).
    """
    url, key, instance = _evolution_base()
    if not url or not key:
        return []

    endpoint = f"{url}/chat/findMessages/{instance}"
    headers = {"apikey": key, "Content-Type": "application/json"}
    body = {"where": {"key": {"remoteJid": jid}}, "page": page, "offset": limit}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
            if resp.status_code not in (200, 201):
                logger.warning(f"findMessages {jid} status={resp.status_code}: {resp.text[:200]}")
                return []
            data = resp.json()
            # Formatos: list direta, {records: [...]}, {messages: {records: [...]}}
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                if "records" in data and isinstance(data["records"], list):
                    return data["records"]
                msgs = data.get("messages")
                if isinstance(msgs, dict) and isinstance(msgs.get("records"), list):
                    return msgs["records"]
                if isinstance(msgs, list):
                    return msgs
            return []
    except Exception as e:
        logger.warning(f"findMessages error {jid}: {e}")
        return []


def _build_jids_for_contact(contact: Dict) -> List[str]:
    """Constroi possiveis remoteJids pra um contato.

    Por telefone: <digits>@s.whatsapp.net (formato classico).
    Edge: nao temos o @lid nativamente — sera coberto via search por telefone.
    """
    import json as _json
    tels = contact.get("telefones")
    if isinstance(tels, str):
        try:
            tels = _json.loads(tels)
        except Exception:
            tels = []
    if not isinstance(tels, list):
        return []
    jids = []
    for t in tels:
        if isinstance(t, dict):
            num = t.get("numero") or t.get("number") or ""
        else:
            num = str(t)
        digits = "".join(c for c in num if c.isdigit())
        if len(digits) >= 8:
            jids.append(f"{digits}@s.whatsapp.net")
    return jids


def _list_priority_contacts(conn, days: int = 7) -> List[Dict]:
    """Contatos com msg recente OU circulo<=4 OU ultimo_contato<7d."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT c.id, c.nome, c.telefones, c.circulo, c.ultimo_contato
        FROM contacts c
        WHERE (
            COALESCE(c.circulo, 5) <= 4
            OR c.ultimo_contato > NOW() - INTERVAL '%s days'
            OR EXISTS (
                SELECT 1 FROM messages m
                WHERE m.contact_id = c.id
                  AND m.criado_em > NOW() - INTERVAL '%s days'
            )
        )
        AND c.telefones IS NOT NULL
        AND c.telefones::text != '[]'
        ORDER BY c.ultimo_contato DESC NULLS LAST
        LIMIT 200
        """ % (days, days)
    )
    return [dict(r) for r in cur.fetchall()]


def _existing_message_ids(conn, external_ids: List[str]) -> set:
    if not external_ids:
        return set()
    cur = conn.cursor()
    cur.execute(
        "SELECT external_id FROM messages WHERE external_id = ANY(%s)",
        (external_ids,),
    )
    return {r["external_id"] for r in cur.fetchall()}


async def _replay_through_webhook(record: Dict, instance: str) -> Optional[Dict]:
    """Simula payload de webhook e chama handler — garante mesma logica + audit."""
    from integrations.evolution_api import handle_evolution_webhook

    # Evolution armazena message records com shape: {key, message, messageTimestamp, ...}
    # Webhook payload espera: {event, instance, data: {key, message, ...}}
    data_block = {
        "key": record.get("key", {}),
        "message": record.get("message", {}),
        "messageTimestamp": record.get("messageTimestamp"),
        "pushName": record.get("pushName"),
        "instance": instance,
    }
    payload = {
        "event": "messages.upsert",
        "instance": instance,
        "data": data_block,
        "_catchup": True,
    }
    try:
        return await handle_evolution_webhook(payload)
    except Exception as e:
        logger.warning(f"catchup replay failed: {e}")
        return {"error": str(e)}


async def catchup_recent_dms(hours: int = 2, max_contacts: int = 50) -> Dict:
    """Itera contatos prioritarios, fetch ultimas msgs, replay faltantes."""
    from database import get_db

    _, _, instance = _evolution_base()

    stats = {
        "checked_contacts": 0,
        "evolution_msgs_seen": 0,
        "missing_found": 0,
        "recovered": 0,
        "errors": 0,
        "details": [],
    }

    with get_db() as conn:
        contacts = _list_priority_contacts(conn, days=7)

    contacts = contacts[:max_contacts]
    logger.info(f"catchup: checking {len(contacts)} priority contacts")

    for c in contacts:
        stats["checked_contacts"] += 1
        jids = _build_jids_for_contact(c)
        if not jids:
            continue

        for jid in jids:
            try:
                records = await _find_messages_for_jid(jid, page=1, limit=20)
                if not records:
                    continue
                stats["evolution_msgs_seen"] += len(records)

                # IDs externos
                ext_ids = []
                for r in records:
                    mid = (r.get("key") or {}).get("id")
                    if mid:
                        ext_ids.append(mid)

                with get_db() as conn:
                    existing = _existing_message_ids(conn, ext_ids)

                missing = [r for r in records
                           if (r.get("key") or {}).get("id") and (r.get("key") or {}).get("id") not in existing]
                if not missing:
                    continue

                stats["missing_found"] += len(missing)
                logger.info(f"catchup: {c.get('nome')} ({jid}) — {len(missing)} missing msgs")

                for rec in missing:
                    res = await _replay_through_webhook(rec, instance)
                    if res and res.get("processed"):
                        stats["recovered"] += 1
                    elif res and res.get("error"):
                        stats["errors"] += 1

                stats["details"].append({
                    "contact_id": c.get("id"),
                    "nome": c.get("nome"),
                    "jid": jid,
                    "evolution_count": len(records),
                    "missing": len(missing),
                })
            except Exception as e:
                logger.warning(f"catchup contact {c.get('id')} jid {jid} error: {e}")
                stats["errors"] += 1

    logger.info(f"catchup done: {stats}")
    return stats
