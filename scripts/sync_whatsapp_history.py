#!/usr/bin/env python3
"""
Sync WhatsApp message history from Evolution API for contacts that have
a phone number but no conversation in the database.

Usage:
    python scripts/sync_whatsapp_history.py              # Default: 50 contacts
    python scripts/sync_whatsapp_history.py --limit 100  # Process 100 contacts
    python scripts/sync_whatsapp_history.py --dry-run    # Show what would be synced
    python scripts/sync_whatsapp_history.py --remote     # Use production DB
"""
import os
import sys
import json
import time
import argparse
import asyncio
from datetime import datetime
from pathlib import Path

# --- env loading (must be before importing app modules) ---
PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_DIR / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'").rstrip("\\n"))

# Default to local DB unless --remote is passed
if "--remote" not in sys.argv:
    os.environ["USE_LOCAL_DB"] = "1"
sys.path.insert(0, str(PROJECT_DIR / "app"))

from database import get_db
from integrations.whatsapp import WhatsAppIntegration


async def find_contacts_without_history(limit: int = 50) -> list:
    """
    Find contacts that have a phone number but no WhatsApp conversation in the DB.
    Returns list of dicts: {id, nome, phone}
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.id, c.nome, c.telefones
            FROM contacts c
            WHERE c.telefones IS NOT NULL
              AND c.telefones::text != '[]'
              AND NOT EXISTS (
                  SELECT 1 FROM conversations conv
                  WHERE conv.contact_id = c.id AND conv.canal = 'whatsapp'
              )
            ORDER BY c.ultimo_contato DESC NULLS LAST
            LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()

    results = []
    for row in rows:
        phones = row['telefones'] if isinstance(row['telefones'], list) else []
        for p in phones:
            phone_num = p.get('number', '') if isinstance(p, dict) else str(p)
            digits = ''.join(filter(str.isdigit, phone_num))
            if len(digits) >= 8:
                results.append({
                    'id': row['id'],
                    'nome': row['nome'],
                    'phone': digits,
                })
                break  # Use first valid phone
    return results


async def sync_contact_history(
    whatsapp: WhatsAppIntegration,
    contact_id: int,
    contact_name: str,
    phone: str,
    dry_run: bool = False,
) -> dict:
    """
    Fetch WhatsApp messages for a single contact from Evolution API
    and store them in the database.

    Returns dict with stats.
    """
    result = {"contact_id": contact_id, "name": contact_name, "phone": phone,
              "messages_fetched": 0, "messages_inserted": 0, "error": None}

    try:
        messages = await whatsapp.get_messages_for_chat(phone, limit=100)
        result["messages_fetched"] = len(messages)

        if not messages:
            return result

        if dry_run:
            return result

        with get_db() as conn:
            cursor = conn.cursor()

            # Create conversation
            cursor.execute("""
                INSERT INTO conversations (contact_id, canal, status, criado_em, atualizado_em)
                VALUES (%s, 'whatsapp', 'open', NOW(), NOW())
                RETURNING id
            """, (contact_id,))
            conversation_id = cursor.fetchone()["id"]

            # Pre-load existing external IDs for this contact to avoid duplicates
            cursor.execute("""
                SELECT external_id FROM messages
                WHERE contact_id = %s AND external_id IS NOT NULL
            """, (contact_id,))
            existing_ids = {row['external_id'] for row in cursor.fetchall()}

            # Also check metadata->message_id
            cursor.execute("""
                SELECT metadata->>'message_id' as msg_id FROM messages
                WHERE contact_id = %s AND metadata->>'message_id' IS NOT NULL
            """, (contact_id,))
            existing_ids.update(row['msg_id'] for row in cursor.fetchall())

            inserted = 0
            latest_timestamp = None

            for msg in messages:
                parsed = whatsapp.parse_stored_message(msg)
                if not parsed:
                    continue

                message_id_ext = parsed.get("message_id")

                # Skip duplicates
                if message_id_ext and message_id_ext in existing_ids:
                    continue

                timestamp = parsed.get("timestamp")
                content = parsed.get("content")
                direction = parsed.get("direction")

                cursor.execute("""
                    INSERT INTO messages
                    (conversation_id, contact_id, external_id, direcao, conteudo, enviado_em, metadata, criado_em)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    conversation_id,
                    contact_id,
                    message_id_ext,
                    direction,
                    content,
                    timestamp,
                    json.dumps({
                        "phone": phone,
                        "push_name": parsed.get("push_name"),
                        "message_id": message_id_ext,
                        "message_type": parsed.get("message_type"),
                        "is_group": False,
                        "source": "history_sync",
                    })
                ))
                inserted += 1
                if message_id_ext:
                    existing_ids.add(message_id_ext)

                if timestamp and (latest_timestamp is None or timestamp > latest_timestamp):
                    latest_timestamp = timestamp

            # Update conversation stats
            if inserted > 0:
                cursor.execute("""
                    UPDATE conversations
                    SET total_mensagens = %s,
                        ultimo_mensagem = %s,
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (inserted, latest_timestamp, conversation_id))

                # Update contact ultimo_contato
                if latest_timestamp:
                    cursor.execute("""
                        UPDATE contacts
                        SET ultimo_contato = GREATEST(COALESCE(ultimo_contato, '1970-01-01'), %s),
                            total_interacoes = COALESCE(total_interacoes, 0) + %s
                        WHERE id = %s
                    """, (latest_timestamp, inserted, contact_id))
            else:
                # No messages inserted, remove empty conversation
                cursor.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))

            conn.commit()
            result["messages_inserted"] = inserted

    except Exception as e:
        result["error"] = str(e)

    return result


async def run_sync(limit: int = 50, dry_run: bool = False, delay: float = 1.0):
    """
    Main sync loop: find contacts without history and fetch their messages.
    """
    print(f"{'[DRY RUN] ' if dry_run else ''}Finding contacts without WhatsApp history...")
    contacts = await find_contacts_without_history(limit)
    print(f"Found {len(contacts)} contacts to sync\n")

    if not contacts:
        print("Nothing to sync.")
        return {"synced": 0, "messages": 0, "errors": 0}

    whatsapp = WhatsAppIntegration()
    if not whatsapp.base_url or not whatsapp.api_key:
        print("ERROR: EVOLUTION_API_URL and EVOLUTION_API_KEY must be set in .env")
        return {"synced": 0, "messages": 0, "errors": 0}

    total_synced = 0
    total_messages = 0
    total_errors = 0

    for i, contact in enumerate(contacts, 1):
        print(f"[{i}/{len(contacts)}] {contact['nome']} ({contact['phone']})...", end=" ", flush=True)

        result = await sync_contact_history(
            whatsapp, contact['id'], contact['nome'], contact['phone'], dry_run=dry_run
        )

        if result["error"]:
            print(f"ERROR: {result['error']}")
            total_errors += 1
        elif result["messages_fetched"] == 0:
            print("no messages in Evolution API")
        else:
            action = "would insert" if dry_run else "inserted"
            print(f"fetched {result['messages_fetched']}, {action} {result['messages_inserted']}")
            if result["messages_inserted"] > 0 or (dry_run and result["messages_fetched"] > 0):
                total_synced += 1
                total_messages += result["messages_inserted"] if not dry_run else result["messages_fetched"]

        # Rate limit: wait between API calls
        if i < len(contacts):
            time.sleep(delay)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Summary:")
    print(f"  Contacts synced: {total_synced}")
    print(f"  Messages {'would be ' if dry_run else ''}imported: {total_messages}")
    print(f"  Errors: {total_errors}")

    return {"synced": total_synced, "messages": total_messages, "errors": total_errors}


def main():
    parser = argparse.ArgumentParser(description="Sync WhatsApp history from Evolution API")
    parser.add_argument("--limit", type=int, default=50, help="Max contacts to process (default: 50)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced without writing")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between API calls in seconds (default: 1.0)")
    parser.add_argument("--remote", action="store_true", help="Use production (remote) database")
    args = parser.parse_args()

    print(f"Database: {'REMOTE (production)' if args.remote else 'LOCAL'}")
    print(f"Limit: {args.limit} contacts")
    print(f"Rate limit: {args.delay}s between requests\n")

    asyncio.run(run_sync(limit=args.limit, dry_run=args.dry_run, delay=args.delay))


if __name__ == "__main__":
    main()
