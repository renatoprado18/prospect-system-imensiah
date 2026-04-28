"""
Group Digest Service - Resumo diário dos grupos WhatsApp.

Gera resumo IA das mensagens do dia para cada grupo com sync ativo.
Envia via WhatsApp (intel-bot) com destaques e pendências.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def generate_daily_group_digests(days: int = 1) -> Dict:
    """
    Generate digest for all sync-enabled groups.
    Called by cron daily (evening).
    """
    results = {"groups_processed": 0, "digests_sent": 0, "errors": []}

    with get_db() as conn:
        cursor = conn.cursor()

        # Get groups with sync enabled
        cursor.execute("""
            SELECT group_jid, group_name FROM social_groups_cache
            WHERE sync_enabled = TRUE AND group_name IS NOT NULL
        """)
        groups = [dict(r) for r in cursor.fetchall()]

    if not groups:
        return {"skipped": "no groups with sync enabled"}

    # First, sync messages for all groups
    try:
        from services.group_message_sync import sync_group_messages
        sync_result = await sync_group_messages(limit_per_group=100)
        logger.info(f"Group sync: {sync_result}")
    except Exception as e:
        logger.error(f"Group sync error: {e}")

    # Generate digest for each group
    digests = []
    for group in groups:
        try:
            digest = await _generate_group_digest(group['group_jid'], group['group_name'], days)
            if digest:
                digests.append(digest)
                results["groups_processed"] += 1
        except Exception as e:
            results["errors"].append(f"{group['group_name']}: {e}")

    # Send combined digest via WhatsApp
    if digests:
        wa_text = _format_digests_for_whatsapp(digests)
        try:
            from services.intel_bot import send_intel_notification
            await send_intel_notification(wa_text)
            results["digests_sent"] = 1
        except Exception as e:
            results["errors"].append(f"WhatsApp send: {e}")

    return results


async def _generate_group_digest(group_jid: str, group_name: str, days: int = 1) -> Dict | None:
    """Generate AI digest for a single group's messages from today."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get today's messages
        cursor.execute("""
            SELECT sender_name, content, message_type, timestamp, from_me
            FROM group_messages
            WHERE group_jid = %s AND timestamp >= CURRENT_DATE - INTERVAL '%s days'
            ORDER BY timestamp ASC
        """, (group_jid, days))
        messages = [dict(r) for r in cursor.fetchall()]

    if not messages or len(messages) < 3:
        return None  # Skip groups with < 3 messages

    # Build conversation text
    msgs_text = "\n".join([
        f"[{m['timestamp'].strftime('%H:%M') if hasattr(m['timestamp'], 'strftime') else '?'}] "
        f"{'Renato' if m['from_me'] else (m['sender_name'] or '?')}: "
        f"{m['content'][:300]}"
        for m in messages
    ])

    # Truncate if too long
    if len(msgs_text) > 4000:
        msgs_text = msgs_text[:4000] + "\n... (truncado)"

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"group": group_name, "summary": f"{len(messages)} mensagens", "messages": len(messages)}

    prompt = f"""Resuma as mensagens deste grupo de WhatsApp de hoje.

GRUPO: {group_name}
MENSAGENS ({len(messages)}):
{msgs_text}

Retorne um resumo CURTO (máx 3-4 linhas) com:
1. Tema principal discutido
2. Decisões ou conclusões (se houver)
3. Menções a Renato ou pendências para ele
4. Próximos passos mencionados

Português. Direto. Sem introdução."""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                      "messages": [{"role": "user", "content": prompt}]}
            )

        if resp.status_code == 200:
            summary = resp.json()["content"][0]["text"]
            return {
                "group": group_name,
                "summary": summary,
                "messages": len(messages),
            }
    except Exception as e:
        logger.error(f"Digest AI error for {group_name}: {e}")

    return {
        "group": group_name,
        "summary": f"{len(messages)} mensagens (resumo indisponível)",
        "messages": len(messages),
    }


def _format_digests_for_whatsapp(digests: List[Dict]) -> str:
    """Format all group digests into a single WhatsApp message."""
    total_msgs = sum(d["messages"] for d in digests)
    active = [d for d in digests if d["messages"] >= 3]

    text = f"📱 *Digest dos Grupos* ({len(active)} ativos, {total_msgs} msgs)\n"

    for d in sorted(active, key=lambda x: -x["messages"]):
        text += f"\n*{d['group']}* ({d['messages']} msgs)\n"
        text += f"{d['summary']}\n"

    return text
