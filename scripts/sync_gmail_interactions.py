#!/usr/bin/env python3
"""
Sincroniza interacoes do Gmail para atualizar contatos.
Busca emails de/para cada contato e atualiza ultimo_contato e total_interacoes.
"""
import os
import sys
import asyncio
import json
from datetime import datetime

# Load .env file
from pathlib import Path
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
from integrations.gmail import GmailIntegration, parse_gmail_date

BATCH_SIZE = 50
MAX_MESSAGES_PER_CONTACT = 100

gmail = GmailIntegration()

async def get_valid_token(account):
    """Get valid access token, refreshing if needed."""
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        return None

    # Always refresh to get a fresh token
    result = await gmail.refresh_access_token(refresh_token)
    if "error" in result:
        print(f"  Erro ao refresh token: {result['error']}", flush=True)
        return None

    return result.get("access_token")


async def count_messages_for_email(access_token: str, email: str) -> dict:
    """
    Count messages from/to an email address and get latest date.
    Returns: {count: int, latest_date: datetime or None}
    """
    result = {"count": 0, "latest_date": None}

    # Search for messages involving this email
    query = f"from:{email} OR to:{email}"

    try:
        response = await gmail.list_messages(
            access_token=access_token,
            query=query,
            max_results=MAX_MESSAGES_PER_CONTACT
        )

        if "error" in response:
            return result

        messages = response.get("messages", [])
        result["count"] = len(messages)

        # Get the first (most recent) message to extract date
        if messages:
            msg_id = messages[0]["id"]
            msg_detail = await gmail.get_message(
                access_token=access_token,
                message_id=msg_id,
                format="metadata"
            )

            if "error" not in msg_detail:
                headers = gmail.parse_message_headers(msg_detail)
                date_str = headers.get("date", "")
                if date_str:
                    result["latest_date"] = parse_gmail_date(date_str)

    except Exception as e:
        print(f"    Erro: {e}", flush=True)

    return result


async def sync_account(account: dict, contacts: list) -> dict:
    """Sync interactions for one Gmail account."""
    stats = {"processados": 0, "atualizados": 0, "total_interacoes": 0}

    email_account = account["email"]
    print(f"\nSincronizando conta: {email_account}", flush=True)

    access_token = await get_valid_token(account)
    if not access_token:
        print(f"  ERRO: Nao foi possivel obter token valido!", flush=True)
        return stats

    print(f"  Token obtido com sucesso", flush=True)

    for i, contact in enumerate(contacts):
        contact_id = contact["id"]
        contact_name = contact["nome"]
        contact_emails = contact["emails"]

        # Parse emails
        email_list = []
        if isinstance(contact_emails, str):
            try:
                email_list = json.loads(contact_emails)
            except:
                email_list = [{"email": contact_emails}]
        elif isinstance(contact_emails, list):
            email_list = contact_emails

        if not email_list:
            continue

        # Get all email addresses for this contact
        addresses = []
        for e in email_list:
            if isinstance(e, dict):
                addr = e.get("email", "")
            else:
                addr = str(e)
            if addr and addr != email_account:  # Skip own email
                addresses.append(addr.lower())

        if not addresses:
            continue

        # Count messages for each email address
        total_count = 0
        latest_date = None

        for addr in addresses[:3]:  # Max 3 emails per contact
            result = await count_messages_for_email(access_token, addr)
            total_count += result["count"]

            if result["latest_date"]:
                if latest_date is None or result["latest_date"] > latest_date:
                    latest_date = result["latest_date"]

            # Rate limit
            await asyncio.sleep(0.1)

        stats["processados"] += 1
        stats["total_interacoes"] += total_count

        if total_count > 0:
            stats["atualizados"] += 1

            # Update contact in database
            with get_db() as conn:
                cursor = conn.cursor()

                # Get current values
                cursor.execute(
                    "SELECT total_interacoes, ultimo_contato FROM contacts WHERE id = %s",
                    (contact_id,)
                )
                current = cursor.fetchone()
                current_interactions = current["total_interacoes"] or 0
                current_ultimo = current["ultimo_contato"]

                # Merge values (keep higher interaction count, more recent date)
                new_interactions = max(current_interactions, total_count)
                new_ultimo = latest_date

                # Handle timezone-aware vs naive datetime comparison
                if current_ultimo and latest_date:
                    # Make both naive for comparison
                    current_naive = current_ultimo.replace(tzinfo=None) if hasattr(current_ultimo, 'tzinfo') and current_ultimo.tzinfo else current_ultimo
                    latest_naive = latest_date.replace(tzinfo=None) if hasattr(latest_date, 'tzinfo') and latest_date.tzinfo else latest_date
                    new_ultimo = latest_date if latest_naive > current_naive else current_ultimo
                elif current_ultimo:
                    new_ultimo = current_ultimo

                cursor.execute("""
                    UPDATE contacts
                    SET total_interacoes = %s, ultimo_contato = %s
                    WHERE id = %s
                """, (new_interactions, new_ultimo, contact_id))
                conn.commit()

        # Progress
        if (i + 1) % 100 == 0:
            print(f"  Progresso: {i+1}/{len(contacts)} - Atualizados: {stats['atualizados']}", flush=True)

    return stats


async def main():
    print("=" * 60, flush=True)
    print("SINCRONIZACAO GMAIL -> CONTATOS", flush=True)
    print("=" * 60, flush=True)

    # Get Gmail accounts
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE")
        accounts = cursor.fetchall()

    if not accounts:
        print("ERRO: Nenhuma conta Gmail conectada!", flush=True)
        return

    print(f"Contas conectadas: {len(accounts)}", flush=True)

    # Get contacts with emails (in batches)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, emails
            FROM contacts
            WHERE emails IS NOT NULL AND emails::text != '[]'
            ORDER BY id
        """)
        contacts = cursor.fetchall()

    print(f"Contatos com email: {len(contacts)}", flush=True)

    # Process each account
    total_stats = {"processados": 0, "atualizados": 0, "total_interacoes": 0}

    for account in accounts:
        stats = await sync_account(dict(account), [dict(c) for c in contacts])
        total_stats["processados"] += stats["processados"]
        total_stats["atualizados"] += stats["atualizados"]
        total_stats["total_interacoes"] += stats["total_interacoes"]

        # Update last sync time
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE google_accounts SET ultima_sync = CURRENT_TIMESTAMP WHERE id = %s",
                (account["id"],)
            )
            conn.commit()

    print("\n" + "=" * 60, flush=True)
    print("RESULTADO", flush=True)
    print("=" * 60, flush=True)
    print(f"Contatos processados: {total_stats['processados']}", flush=True)
    print(f"Contatos atualizados: {total_stats['atualizados']}", flush=True)
    print(f"Total interacoes encontradas: {total_stats['total_interacoes']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
