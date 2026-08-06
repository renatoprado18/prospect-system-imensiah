"""
Gmail Outbound Sync — captura emails enviados pelo Renato.

Why: Gmail sync padrao (gmail_sync.py) trabalha incoming-first (busca por
email do contato em from: OR to:). Action blindness 13/06/26: Renato mandou
email pra Cecilia com draft de outra sessao; INTEL nao soube; CoS propos
draft duplicado. Caso confirmado em feedback_cos_action_blindness.

Este service varre o folder Sent de cada conta Gmail conectada (newer_than:1d),
extrai destinatarios, e registra como `messages` direcao='outgoing' canal=email.
Permite:
- dismiss_stale_on_reply auto-fechar propostas pendentes
- _get_active_cos_proposal (worker + Vercel) ocultar propostas onde ja houve
  outgoing posterior
- cos_sensor enxergar acoes do Renato fora do bot
"""
from __future__ import annotations

import os
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from database import get_db
from integrations.gmail import GmailIntegration, parse_gmail_date

logger = logging.getLogger(__name__)


EMAIL_REGEX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_emails(header_value: Optional[str]) -> List[str]:
    """Extrai emails de header tipo 'To'/'Cc' (formato 'Nome <email>, email2')."""
    if not header_value:
        return []
    return [m.lower() for m in EMAIL_REGEX.findall(header_value)]


def _find_contact_by_email(cursor, email: str) -> Optional[int]:
    """Acha contact_id cujo jsonb emails contem o email dado (lowercase)."""
    email = email.lower().strip()
    cursor.execute(
        """
        SELECT id FROM contacts
        WHERE EXISTS (
            SELECT 1 FROM jsonb_array_elements(COALESCE(emails, '[]'::jsonb)) e
            WHERE LOWER(e->>'email') = %s
        )
        LIMIT 1
        """,
        (email,),
    )
    row = cursor.fetchone()
    return row["id"] if row else None


def _upsert_outbound_message(
    cursor,
    contact_id: int,
    gmail_id: str,
    subject: str,
    body_text: str,
    sent_at: Optional[datetime],
    to_emails: List[str],
    account_email: str,
    thread_id: Optional[str] = None,
) -> Optional[int]:
    """Insert message direcao='outgoing' se nao existir (external_id dedup).

    Cria conversation canal='email' se necessario.
    """
    cursor.execute("SELECT id, metadata FROM messages WHERE external_id = %s LIMIT 1", (gmail_id,))
    existing = cursor.fetchone()
    if existing:
        # Backfill do thread_id nas linhas gravadas antes de 04/08/2026, quando o
        # sync lia o `id` da mensagem e descartava o `threadId`. Sem isto, todo
        # e-mail ja capturado na janela de 24h ficaria sem a unica ancora que
        # casa rascunho com enviado, e o loop so fecharia pro que viesse depois.
        meta = existing.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if thread_id and not meta.get("thread_id"):
            meta["thread_id"] = thread_id
            cursor.execute(
                "UPDATE messages SET metadata = %s WHERE id = %s",
                (json.dumps(meta), existing["id"]),
            )
        return existing["id"]

    cursor.execute(
        """
        SELECT id FROM conversations
        WHERE contact_id = %s AND canal = 'email'
        ORDER BY id ASC LIMIT 1
        """,
        (contact_id,),
    )
    conv = cursor.fetchone()
    if conv:
        conversation_id = conv["id"]
    else:
        cursor.execute(
            """
            INSERT INTO conversations (contact_id, canal, assunto, status)
            VALUES (%s, 'email', %s, 'active')
            RETURNING id
            """,
            (contact_id, (subject or "")[:200]),
        )
        conversation_id = cursor.fetchone()["id"]

    metadata = {
        "account": account_email,
        "to": to_emails,
        "subject": subject,
        "source": "gmail_outbound_sync",
        # `thread_id` e a ancora que fecha o loop DRAFT -> ENVIADO. O message id
        # do rascunho NAO e o da mensagem enviada (o Gmail cria outro ao
        # despachar); a thread e a mesma. Vinha na resposta do list e do get
        # desde sempre e era descartado aqui.
        "thread_id": thread_id,
    }
    cursor.execute(
        """
        INSERT INTO messages (
            conversation_id, contact_id, external_id, direcao,
            conteudo, metadata, enviado_em
        ) VALUES (%s, %s, %s, 'outgoing', %s, %s, %s)
        RETURNING id
        """,
        (
            conversation_id,
            contact_id,
            gmail_id,
            (body_text or "")[:5000],
            json.dumps(metadata),
            sent_at,
        ),
    )
    return cursor.fetchone()["id"]


async def sync_account_outbound(
    account: Dict,
    gmail: GmailIntegration,
    hours: int = 24,
    max_pages: int = 1,
    post_process: bool = True,
) -> Dict[str, Any]:
    """Sync 1 conta — varre Sent newer_than:Nd e registra outgoing.

    `max_pages=1` (default) preserva o comportamento do cron de 30min: 1 página
    de 50 e-mails, custo previsivel. Backfill historico passa max_pages alto —
    a paginacao existe pra isso, nao pro caminho agendado.

    Returns: {emails_listed, processed, registered, contacts_resolved, errors}
    """
    stats = {
        "account": account.get("email"),
        "emails_listed": 0,
        "processed": 0,
        "registered": 0,
        "contacts_resolved": 0,
        "skipped_existing": 0,
        "pages": 0,
        "errors": 0,
        "error_samples": [],
    }

    # Refresh token
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        stats["errors"] += 1
        stats["error_samples"].append({"step": "refresh_token", "error": "no_refresh_token"})
        return stats

    refresh_result = await gmail.refresh_access_token(refresh_token)
    if "error" in refresh_result:
        stats["errors"] += 1
        stats["error_samples"].append({"step": "refresh_token", "error": refresh_result["error"]})
        return stats
    access_token = refresh_result.get("access_token")
    if not access_token:
        stats["errors"] += 1
        return stats

    days = max(1, hours // 24)
    query = f"in:sent newer_than:{days}d"

    messages: List[Dict] = []
    page_token = None
    for _ in range(max(1, max_pages)):
        list_result = await gmail.list_messages(
            access_token=access_token,
            query=query,
            max_results=50,
            page_token=page_token,
        )
        if not list_result or list_result.get("error"):
            stats["errors"] += 1
            stats["error_samples"].append({"step": "list", "error": (list_result or {}).get("error", "no_response")})
            # Pagina 1 falhou = nada a processar; falha numa pagina posterior
            # ainda deixa as anteriores utilizaveis.
            if not messages:
                return stats
            break
        stats["pages"] += 1
        messages.extend(list_result.get("messages", []) or [])
        page_token = list_result.get("nextPageToken")
        if not page_token:
            break

    stats["emails_listed"] = len(messages)

    contact_ids_touched: set = set()
    # Toda thread vista em `in:sent`, mesmo a de e-mail cujo destinatario nao
    # virou contato: o rascunho pendente existe independentemente de o outro
    # lado estar cadastrado, e o loop tem que fechar do mesmo jeito.
    threads_enviadas: List[Dict[str, Any]] = []

    with get_db() as conn:
        cursor = conn.cursor()

        for msg_summary in messages:
            stats["processed"] += 1
            gmail_id = msg_summary.get("id")
            if not gmail_id:
                continue

            try:
                msg = await gmail.get_message(access_token, gmail_id, format="full")
                if not msg or msg.get("error"):
                    stats["errors"] += 1
                    continue

                # O `threadId` vem tanto no list quanto no get; o get manda,
                # porque o summary pode ter vindo de pagina anterior.
                thread_id = msg.get("threadId") or msg_summary.get("threadId")

                headers = gmail.parse_message_headers(msg)
                to_emails = _extract_emails(headers.get("to"))
                cc_emails = _extract_emails(headers.get("cc"))
                recipient_emails = list(dict.fromkeys(to_emails + cc_emails))  # dedup ordenado
                # Skip Renato → Renato (cron, automacao)
                renato_emails = {"renato@almeida-prado.com", "renato.almeida.prado@gmail.com", "renatodaprado@gmail.com"}
                recipient_emails = [e for e in recipient_emails if e not in renato_emails]

                subject = headers.get("subject", "")[:200]
                date_str = headers.get("date", "")
                sent_at = None
                try:
                    sent_at = parse_gmail_date(date_str)
                except Exception:
                    pass

                if thread_id:
                    threads_enviadas.append(
                        {"thread_id": thread_id, "sent_at": sent_at, "message_id": gmail_id}
                    )

                # A partir daqui e sobre CONTATO; a thread ja foi anotada acima
                # justamente porque o loop do rascunho nao depende disso.
                if not recipient_emails:
                    continue

                body = gmail.parse_message_body(msg)
                body_text = (body.get("text", "") or "")[:5000]

                for email in recipient_emails:
                    contact_id = _find_contact_by_email(cursor, email)
                    if not contact_id:
                        continue
                    msg_id_inserted = _upsert_outbound_message(
                        cursor=cursor,
                        contact_id=contact_id,
                        gmail_id=gmail_id,
                        subject=subject,
                        body_text=body_text,
                        sent_at=sent_at,
                        to_emails=recipient_emails,
                        account_email=account.get("email", ""),
                        thread_id=thread_id,
                    )
                    if msg_id_inserted:
                        stats["registered"] += 1
                        stats["contacts_resolved"] += 1
                        contact_ids_touched.add(contact_id)
            except Exception as e:
                stats["errors"] += 1
                if len(stats["error_samples"]) < 5:
                    stats["error_samples"].append({"gmail_id": gmail_id, "error": str(e)[:200]})

        conn.commit()

    # Pos-processamento: auto-dismiss proposals pendentes pra cada contato tocado.
    #
    # `post_process=False` existe pro BACKFILL historico: um e-mail de meses atras
    # nao e evidencia de que o Renato ja respondeu a uma proposta aberta HOJE —
    # rodar o dismiss sobre corpus antigo fecharia propostas vivas em massa. O
    # caminho agendado (janela de 1 dia) segue com o dismiss ligado.
    if contact_ids_touched and post_process:
        try:
            from services.action_proposals import get_action_proposals
            svc = get_action_proposals()
            dismissed_total = 0
            for cid in contact_ids_touched:
                try:
                    dismissed_total += svc.dismiss_stale_on_reply(cid)
                except Exception as e:
                    logger.warning(f"dismiss_stale_on_reply failed for contact {cid}: {e}")
            stats["proposals_dismissed"] = dismissed_total
        except Exception as e:
            logger.warning(f"Failed to dismiss proposals after outbound sync: {e}")

    # Fechamento do loop DRAFT -> ENVIADO (04/08/2026): rascunho pendente numa
    # thread onde acabou de sair e-mail passa a 'sent'. Roda mesmo com
    # `post_process=False` — ao contrario do dismiss de propostas, aqui o
    # backfill historico e inofensivo: a propria regra de casamento exige que o
    # envio seja POSTERIOR a criacao do rascunho (draft_matches_sent), entao um
    # e-mail de meses atras nunca fecha um rascunho de hoje.
    if threads_enviadas:
        try:
            from services.email_draft_registry import reconcile_sent_threads
            recon = reconcile_sent_threads(threads_enviadas)
            stats["threads_seen"] = recon["threads"]
            stats["drafts_reconciled"] = recon["drafts_sent"]
        except Exception as e:
            logger.warning(f"Failed to reconcile email drafts after outbound sync: {e}")

    return stats


async def sync_all_outbound(hours: int = 24, max_pages: int = 1,
                            post_process: bool = True) -> Dict[str, Any]:
    """Itera google_accounts conectadas e roda sync_account_outbound."""
    out = {"accounts": [], "registered_total": 0, "errors_total": 0, "drafts_reconciled_total": 0}
    gmail = GmailIntegration()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, email, tipo, refresh_token FROM google_accounts WHERE conectado = true"
        )
        accounts = [dict(r) for r in cursor.fetchall()]

    for account in accounts:
        try:
            stats = await sync_account_outbound(account, gmail, hours=hours,
                                                max_pages=max_pages,
                                                post_process=post_process)
        except Exception as e:
            logger.exception(f"sync_account_outbound crash for {account.get('email')}: {e}")
            stats = {"account": account.get("email"), "errors": 1, "error_samples": [{"step": "crash", "error": str(e)[:200]}]}
        out["accounts"].append(stats)
        out["registered_total"] += stats.get("registered", 0)
        out["errors_total"] += stats.get("errors", 0)
        out["drafts_reconciled_total"] += stats.get("drafts_reconciled", 0)

    return out
