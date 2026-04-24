"""
Group Message Sync - Sincroniza mensagens de grupos WhatsApp marcados para sync.

Busca mensagens novas dos grupos com sync_enabled=TRUE e salva na tabela group_messages.
Cruza remetentes com contatos INTEL pelo telefone.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def sync_group_messages(limit_per_group: int = 50) -> Dict:
    """
    Sincroniza mensagens dos grupos marcados para sync.
    Chamado pelo cron daily-sync.
    """
    from services.social_groups import get_sync_enabled_groups

    groups = get_sync_enabled_groups()
    if not groups:
        return {"skipped": "no groups with sync enabled"}

    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return {"error": "Evolution API not configured"}

    results = {"groups_synced": 0, "messages_saved": 0, "errors": 0}

    async with httpx.AsyncClient(timeout=60.0) as client:
        for group in groups:
            jid = group['group_jid']
            name = group['group_name']

            try:
                saved = await _sync_single_group(
                    client, base_url, api_key, instance,
                    jid, name, limit_per_group
                )
                results["messages_saved"] += saved
                results["groups_synced"] += 1
            except Exception as e:
                logger.error(f"Error syncing group {name}: {e}")
                results["errors"] += 1

    return results


async def _sync_single_group(
    client: httpx.AsyncClient,
    base_url: str, api_key: str, instance: str,
    group_jid: str, group_name: str,
    limit: int
) -> int:
    """Sincroniza mensagens de um grupo especifico. Retorna quantidade salva."""

    # Fetch messages from Evolution API
    resp = await client.post(
        f"{base_url}/chat/findMessages/{instance}",
        headers={'apikey': api_key, 'Content-Type': 'application/json'},
        json={"where": {"key": {"remoteJid": group_jid}}, "limit": limit}
    )

    if resp.status_code != 200:
        logger.warning(f"Failed to fetch messages for {group_name}: {resp.status_code}")
        return 0

    resp_data = resp.json()
    msgs_container = resp_data.get('messages', resp_data)
    if isinstance(msgs_container, dict):
        raw_msgs = msgs_container.get('records', [])
    elif isinstance(msgs_container, list):
        raw_msgs = msgs_container
    else:
        raw_msgs = []

    if not raw_msgs:
        return 0

    # Parse and save messages
    saved = 0
    with get_db() as conn:
        cursor = conn.cursor()

        for m in raw_msgs:
            msg_data = m.get('message') or {}
            key = m.get('key') or {}
            msg_type = m.get('messageType', '')
            msg_id = key.get('id', '')

            # Skip reactions, stickers, protocol messages
            if msg_type in ('reactionMessage', 'stickerMessage', 'protocolMessage',
                            'senderKeyDistributionMessage', 'messageContextInfo'):
                continue

            # Extract content
            content = ''
            if 'conversation' in msg_data:
                content = msg_data['conversation']
            elif 'extendedTextMessage' in msg_data:
                content = msg_data['extendedTextMessage'].get('text', '')
            elif 'documentMessage' in msg_data:
                doc_name = msg_data['documentMessage'].get('fileName', 'documento')
                content = msg_data['documentMessage'].get('caption', f'[Documento: {doc_name}]')
                msg_type = 'document'
            elif 'imageMessage' in msg_data:
                content = msg_data['imageMessage'].get('caption', '[Imagem]')
                msg_type = 'image'
            elif 'videoMessage' in msg_data:
                content = msg_data['videoMessage'].get('caption', '[Video]')
                msg_type = 'video'
            elif 'audioMessage' in msg_data:
                content = '[Audio]'
                msg_type = 'audio'

            if not content or len(content) < 2:
                continue

            # Sender info
            sender_phone = (key.get('participantAlt') or key.get('participant') or '') \
                .replace('@s.whatsapp.net', '').replace('@lid', '')
            sender_name = m.get('pushName', '')
            from_me = key.get('fromMe', False)
            if from_me:
                sender_name = 'Renato'

            # Timestamp
            timestamp = m.get('messageTimestamp', 0)
            if isinstance(timestamp, (int, float)) and timestamp > 0:
                try:
                    timestamp = datetime.fromtimestamp(int(timestamp))
                except Exception:
                    continue
            else:
                continue

            # Cross sender_phone with contacts
            contact_id = _find_contact_by_phone(cursor, sender_phone) if sender_phone and len(sender_phone) > 8 else None

            # Insert with dedup by message_id
            try:
                cursor.execute("""
                    INSERT INTO group_messages (group_jid, message_id, sender_phone, sender_name,
                        contact_id, content, message_type, timestamp, from_me)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (message_id) DO NOTHING
                """, (
                    group_jid, msg_id, sender_phone, sender_name,
                    contact_id, content, msg_type, timestamp, from_me
                ))
                if cursor.rowcount > 0:
                    saved += 1
            except Exception as e:
                logger.debug(f"Skip message {msg_id}: {e}")

        # Update last sync timestamp
        cursor.execute("""
            UPDATE social_groups_cache SET last_message_sync = NOW()
            WHERE group_jid = %s
        """, (group_jid,))

        conn.commit()

    logger.info(f"Group {group_name}: {saved} new messages saved")
    return saved


def _find_contact_by_phone(cursor, phone: str):
    """Find contact_id by phone number (last 8 digits match)."""
    if not phone or len(phone) < 8:
        return None
    last_digits = phone[-8:]
    cursor.execute("""
        SELECT id FROM contacts WHERE telefones::text LIKE %s LIMIT 1
    """, (f'%{last_digits}%',))
    row = cursor.fetchone()
    return row['id'] if row else None


def get_group_messages(group_jid: str, limit: int = 50, since: datetime = None) -> List[Dict]:
    """Retorna mensagens de um grupo do cache local."""
    with get_db() as conn:
        cursor = conn.cursor()
        if since:
            cursor.execute("""
                SELECT gm.*, c.nome as contact_nome
                FROM group_messages gm
                LEFT JOIN contacts c ON c.id = gm.contact_id
                WHERE gm.group_jid = %s AND gm.timestamp >= %s
                ORDER BY gm.timestamp DESC LIMIT %s
            """, (group_jid, since, limit))
        else:
            cursor.execute("""
                SELECT gm.*, c.nome as contact_nome
                FROM group_messages gm
                LEFT JOIN contacts c ON c.id = gm.contact_id
                WHERE gm.group_jid = %s
                ORDER BY gm.timestamp DESC LIMIT %s
            """, (group_jid, limit))

        rows = []
        for r in cursor.fetchall():
            row = dict(r)
            for key in ('timestamp', 'criado_em'):
                if row.get(key) and hasattr(row[key], 'isoformat'):
                    row[key] = row[key].isoformat()
            rows.append(row)
        return rows
