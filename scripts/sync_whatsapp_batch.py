#!/usr/bin/env python3
"""
WhatsApp Sync em Batches
Sincroniza chats do WhatsApp com contatos em batches para não travar
"""
import os
import sys
import re
import json
import asyncio
from datetime import datetime
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                value = value.strip('"').strip("'")
                os.environ.setdefault(key, value)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db
from integrations.whatsapp import WhatsAppIntegration

# Config
BATCH_SIZE = 50
DELAY_BETWEEN_BATCHES = 2  # seconds
DELAY_BETWEEN_CHATS = 0.2  # seconds

wa = WhatsAppIntegration()


def normalize_phone(phone: str) -> str:
    return re.sub(r'\D', '', str(phone))


def find_contact_by_phone(phone: str, contacts_cache: dict) -> dict:
    """Busca contato pelo telefone usando cache."""
    phone_digits = normalize_phone(phone)
    if not phone_digits or len(phone_digits) < 8:
        return None

    # Tentar match exato
    if phone_digits in contacts_cache:
        return contacts_cache[phone_digits]

    # Tentar match parcial (ultimos 9 digitos)
    phone_suffix = phone_digits[-9:]
    for key, contact in contacts_cache.items():
        if key.endswith(phone_suffix):
            return contact

    # Tentar match parcial (ultimos 8 digitos)
    phone_suffix = phone_digits[-8:]
    for key, contact in contacts_cache.items():
        if key.endswith(phone_suffix):
            return contact

    return None


def build_contacts_cache() -> dict:
    """Constroi cache de contatos indexado por telefone."""
    cache = {}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, telefones
            FROM contacts
            WHERE telefones IS NOT NULL AND telefones::text != '[]'
        """)
        contacts = cursor.fetchall()

    for contact in contacts:
        telefones = contact["telefones"]
        if isinstance(telefones, str):
            try:
                telefones = json.loads(telefones)
            except:
                continue

        for tel in telefones:
            if isinstance(tel, dict):
                tel_number = tel.get("number", "") or tel.get("phone", "")
            else:
                tel_number = str(tel)

            tel_digits = normalize_phone(tel_number)
            if tel_digits and len(tel_digits) >= 8:
                cache[tel_digits] = dict(contact)

    return cache


def update_contact_interaction(contact_id: int, msg_count: int, latest_date: datetime):
    """Atualiza interacoes do contato."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT total_interacoes, ultimo_contato FROM contacts WHERE id = %s",
                (contact_id,)
            )
            current = cursor.fetchone()
            if not current:
                return

            current_interactions = current["total_interacoes"] or 0
            current_ultimo = current["ultimo_contato"]

            new_interactions = current_interactions + msg_count
            new_ultimo = latest_date

            if current_ultimo:
                try:
                    current_naive = current_ultimo.replace(tzinfo=None) if hasattr(current_ultimo, 'tzinfo') and current_ultimo.tzinfo else current_ultimo
                    latest_naive = latest_date.replace(tzinfo=None) if hasattr(latest_date, 'tzinfo') and latest_date.tzinfo else latest_date
                    new_ultimo = latest_date if latest_naive > current_naive else current_ultimo
                except:
                    pass

            cursor.execute("""
                UPDATE contacts
                SET total_interacoes = %s, ultimo_contato = %s
                WHERE id = %s
            """, (new_interactions, new_ultimo, contact_id))
            conn.commit()

    except Exception as e:
        print(f"    Erro ao atualizar contato {contact_id}: {e}", flush=True)


async def process_chat(chat: dict, contacts_cache: dict) -> dict:
    """Processa um chat individual."""
    result = {"linked": False, "messages": 0, "contact_id": None}

    phone = chat.get("_phone")
    if not phone:
        return result

    contact = find_contact_by_phone(phone, contacts_cache)
    if not contact:
        return result

    contact_id = contact["id"]
    result["contact_id"] = contact_id

    try:
        messages = await wa.get_messages_for_chat(phone, limit=50)
        result["messages"] = len(messages)

        if messages:
            latest_date = None
            for msg in messages:
                parsed = wa.parse_stored_message(msg)
                if parsed and parsed.get("timestamp"):
                    if latest_date is None or parsed["timestamp"] > latest_date:
                        latest_date = parsed["timestamp"]

            if latest_date:
                update_contact_interaction(contact_id, len(messages), latest_date)
                result["linked"] = True

    except Exception as e:
        result["error"] = str(e)

    return result


async def main():
    print("=" * 60, flush=True)
    print("SYNC WHATSAPP -> CONTATOS (Batches)", flush=True)
    print("=" * 60, flush=True)

    # Verificar conexao
    if not wa.base_url or not wa.api_key:
        print("ERRO: Evolution API nao configurada!", flush=True)
        print(f"  URL: {wa.base_url}", flush=True)
        print(f"  Key: {'***' if wa.api_key else 'VAZIO'}", flush=True)
        return

    print(f"Evolution API: {wa.base_url}", flush=True)
    print(f"Instancia: {wa.instance}", flush=True)

    # Build contacts cache
    print("\nCarregando contatos com telefone...", flush=True)
    contacts_cache = build_contacts_cache()
    print(f"  {len(contacts_cache)} telefones indexados", flush=True)

    # Fetch all chats
    print("\nBuscando chats do WhatsApp...", flush=True)
    chats = await wa.get_all_chats(include_groups=False)
    print(f"  {len(chats)} chats encontrados", flush=True)

    # Stats
    stats = {
        "total": len(chats),
        "processed": 0,
        "linked": 0,
        "messages": 0,
        "errors": 0
    }

    # Process in batches
    total_batches = (len(chats) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\nProcessando em {total_batches} batches de {BATCH_SIZE}...", flush=True)

    for batch_num in range(total_batches):
        start_idx = batch_num * BATCH_SIZE
        end_idx = min(start_idx + BATCH_SIZE, len(chats))
        batch = chats[start_idx:end_idx]

        batch_linked = 0
        batch_messages = 0

        for chat in batch:
            result = await process_chat(chat, contacts_cache)
            stats["processed"] += 1

            if result.get("linked"):
                stats["linked"] += 1
                batch_linked += 1

            stats["messages"] += result.get("messages", 0)
            batch_messages += result.get("messages", 0)

            if result.get("error"):
                stats["errors"] += 1

            await asyncio.sleep(DELAY_BETWEEN_CHATS)

        # Progress
        pct = (stats["processed"] / stats["total"]) * 100
        print(f"  Batch {batch_num + 1}/{total_batches}: {stats['processed']}/{stats['total']} ({pct:.0f}%) - Linked: {stats['linked']}, Msgs: {stats['messages']}", flush=True)

        if batch_num < total_batches - 1:
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    # Final report
    print("\n" + "=" * 60, flush=True)
    print("RESULTADO", flush=True)
    print("=" * 60, flush=True)
    print(f"Chats processados: {stats['processed']}", flush=True)
    print(f"Contatos vinculados: {stats['linked']}", flush=True)
    print(f"Mensagens contadas: {stats['messages']}", flush=True)
    print(f"Erros: {stats['errors']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
