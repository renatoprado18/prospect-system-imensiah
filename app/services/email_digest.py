"""
Email Digest Service - Resumo diário dos emails recebidos.

Busca emails das últimas 24h via Gmail API para ambas as contas
(pessoal e profissional), resume com IA, envia via WhatsApp.
"""
import os
import json
import logging
import base64
from datetime import datetime, timedelta
from typing import Dict, List
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def generate_email_digest(days: int = 1) -> Dict:
    """
    Generate digest of emails from the last N days.
    Fetches from both personal and professional Gmail accounts.
    """
    results = {"accounts_checked": 0, "emails_found": 0, "digest_sent": False, "errors": []}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT email, tipo, access_token, refresh_token, token_expiry
            FROM google_accounts WHERE conectado = TRUE
        """)
        accounts = [dict(r) for r in cursor.fetchall()]

    if not accounts:
        return {"skipped": "no connected accounts"}

    all_emails = []

    for account in accounts:
        try:
            # Refresh token if needed
            token = await _get_fresh_token(account)
            if not token:
                results["errors"].append(f"{account['email']}: token refresh failed")
                continue

            # Fetch recent emails
            emails = await _fetch_recent_emails(token, account['email'], account['tipo'], days)
            all_emails.extend(emails)
            results["accounts_checked"] += 1
            results["emails_found"] += len(emails)
        except Exception as e:
            results["errors"].append(f"{account['email']}: {e}")

    if not all_emails:
        return results

    # Generate AI digest
    digest_text = await _generate_ai_digest(all_emails)

    if digest_text:
        try:
            from services.intel_bot import send_intel_notification
            await send_intel_notification(digest_text)
            results["digest_sent"] = True
        except Exception as e:
            results["errors"].append(f"WhatsApp send: {e}")

    return results


async def _get_fresh_token(account: Dict) -> str | None:
    """Get or refresh Google access token."""
    token_expiry = account.get('token_expiry')
    if token_expiry and token_expiry > datetime.now():
        return account['access_token']

    # Refresh
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    refresh_token = account.get('refresh_token')

    if not refresh_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            })
        if resp.status_code == 200:
            new_token = resp.json()["access_token"]
            # Update in DB
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE google_accounts SET access_token = %s, token_expiry = NOW() + INTERVAL '1 hour'
                    WHERE email = %s
                """, (new_token, account['email']))
                conn.commit()
            return new_token
    except Exception as e:
        logger.error(f"Token refresh error: {e}")

    return None


async def _fetch_recent_emails(token: str, email: str, tipo: str, days: int) -> List[Dict]:
    """Fetch recent emails from Gmail API."""
    after = (datetime.now() - timedelta(days=days)).strftime('%Y/%m/%d')
    query = f"after:{after} in:inbox"

    emails = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # List messages
            resp = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}&maxResults=20",
                headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code != 200:
                return []

            messages = resp.json().get("messages", [])

            # Get details for each message
            for msg in messages[:15]:  # Limit to 15
                try:
                    detail_resp = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date",
                        headers={"Authorization": f"Bearer {token}"}
                    )
                    if detail_resp.status_code != 200:
                        continue

                    detail = detail_resp.json()
                    headers = {h['name']: h['value'] for h in detail.get('payload', {}).get('headers', [])}

                    # Get snippet
                    snippet = detail.get('snippet', '')

                    emails.append({
                        "from": headers.get("From", "?"),
                        "subject": headers.get("Subject", "(sem assunto)"),
                        "snippet": snippet[:200],
                        "date": headers.get("Date", ""),
                        "account": tipo,
                        "account_email": email,
                    })
                except Exception:
                    continue

    except Exception as e:
        logger.error(f"Gmail fetch error for {email}: {e}")

    return emails


async def _generate_ai_digest(emails: List[Dict]) -> str | None:
    """Generate AI summary of emails."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _format_simple_digest(emails)

    # Group by account
    personal = [e for e in emails if e['account'] == 'personal']
    professional = [e for e in emails if e['account'] == 'professional']

    def format_emails(email_list):
        return "\n".join([
            f"- De: {e['from'][:50]} | Assunto: {e['subject']} | {e['snippet'][:100]}"
            for e in email_list
        ])

    parts = []
    if professional:
        parts.append(f"PROFISSIONAL ({len(professional)} emails):\n{format_emails(professional)}")
    if personal:
        parts.append(f"PESSOAL ({len(personal)} emails):\n{format_emails(personal)}")

    email_text = "\n\n".join(parts)

    prompt = f"""Resuma os emails recebidos hoje de forma concisa e acionável.

{email_text}

FORMATO:
- Agrupe por prioridade (urgente, importante, informativo)
- Para cada email relevante: quem enviou, sobre o que, ação necessária
- Ignore newsletters, spam, notificações automáticas
- Destaque emails que precisam de resposta

Máximo 300 palavras. Português. Direto."""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
                      "messages": [{"role": "user", "content": prompt}]}
            )
        if resp.status_code == 200:
            summary = resp.json()["content"][0]["text"]
            total = len(emails)
            return f"📧 *Digest de Emails* ({total} recebidos)\n\n{summary}"
    except Exception as e:
        logger.error(f"Email digest AI error: {e}")

    return _format_simple_digest(emails)


def _format_simple_digest(emails: List[Dict]) -> str:
    """Simple digest without AI."""
    text = f"📧 *Digest de Emails* ({len(emails)} recebidos)\n"
    for e in emails[:10]:
        text += f"\n• *{e['subject'][:50]}*\n  De: {e['from'][:40]}\n  {e['snippet'][:80]}\n"
    return text
