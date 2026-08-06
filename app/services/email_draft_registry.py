"""
Registro de rascunhos de e-mail — fecha o loop DRAFT → ENVIADO.

POR QUE ESTE MÓDULO EXISTE. 04/08/2026: a camada montou um e-mail para o pai do
Renato, deixou no rascunho do Gmail e reportou "pronto no rascunho". Ele já
tinha enviado às 16h29. O sistema sabia o que ele mesmo CRIAVA e era cego ao
que o Renato FAZIA — irmã do caso de 13/06 (feedback_cos_action_blindness), só
que do lado do rascunho.

A causa era simples: `admin_gmail_proxy` action=create_draft devolvia o
{id, message:{id, threadId}} do Gmail para o caller HTTP e jogava fora. Sem
linha no banco não há o que reconciliar quando o e-mail aparece em `in:sent`.

⚠️ REGRA CENTRAL: o message id MUDA quando o rascunho é enviado. O rascunho tem
um message id; ao despachar, o Gmail cria OUTRO. Casar por message id não casa
NUNCA — e falharia calado (zero linhas, zero erro), a classe de defeito de
feedback_filtro_vocabulario_errado_falha_calado. `thread_id` é a única âncora
estável: sobrevive ao envio, à edição do rascunho e à resposta do outro lado.

Duas metades:
  1. `register_draft` — o proxy chama depois de criar o rascunho no Gmail.
  2. `reconcile_thread` / `reconcile_sent_threads` — o gmail_outbound_sync chama
     com as threads que viu em `in:sent`; rascunho pendente naquela thread vira
     'sent'.

A decisão de casar mora em Python (`draft_matches_sent`), não no WHERE do SQL:
é ela que precisa ser provada em teste, e uma regra escondida num WHERE não se
testa sem reimplementar SQL no teste.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from services.tz import now_utc, to_utc

logger = logging.getLogger(__name__)


# Tolerância para o envio parecer anterior à criação do rascunho.
#
# A comparação é entre dois relógios diferentes: `criado_em` vem do nosso
# processo (now_utc) e `sent_at` vem do header Date do Gmail, escrito pelo
# cliente que despachou. O envio real acontece SEMPRE depois da criação, mas
# alguns segundos de desvio entre os relógios não podem custar a
# reconciliação — no caso do pai foram 40 minutos, e mesmo assim a margem
# protege o cenário "o Renato mandou 20 segundos depois de eu criar".
CLOCK_SKEW = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Escrita: o que o create_draft devolveu
# ---------------------------------------------------------------------------

def extract_ids(gmail_response: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Tira {draft_id, thread_id, draft_message_id} da resposta do drafts.create.

    Formato do Gmail: {"id": "r-123", "message": {"id": "18f...", "threadId": "18e..."}}.
    Devolve dict com None onde faltar em vez de estourar — resposta de erro da
    API não pode derrubar o proxy que acabou de criar o rascunho de verdade.
    """
    resp = gmail_response or {}
    message = resp.get("message") or {}
    return {
        "draft_id": resp.get("id"),
        "thread_id": message.get("threadId"),
        "draft_message_id": message.get("id"),
    }


def register_draft(
    cursor,
    gmail_response: Optional[Dict[str, Any]],
    *,
    account_email: Optional[str] = None,
    to_emails: Optional[List[str]] = None,
    subject: Optional[str] = None,
    source: Optional[str] = None,
    contact_id: Optional[int] = None,
) -> Optional[int]:
    """Grava (ou atualiza) a linha do rascunho. Devolve o id, ou None se não deu.

    Sem `draft_id` não há o que registrar. Sem `thread_id` a linha ainda é
    gravada — serve de rastro de auditoria —, mas nunca vai reconciliar, e é
    melhor ter a linha órfã visível do que não ter nada.

    ON CONFLICT atualiza: chamar o proxy duas vezes na mesma thread não deve
    render duas linhas concorrendo pela mesma reconciliação.
    """
    ids = extract_ids(gmail_response)
    if not ids["draft_id"]:
        return None

    cursor.execute(
        """
        INSERT INTO email_drafts (
            draft_id, thread_id, draft_message_id,
            account_email, to_emails, subject, contact_id, source,
            status, criado_em
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
        ON CONFLICT (draft_id) DO UPDATE SET
            thread_id        = EXCLUDED.thread_id,
            draft_message_id = EXCLUDED.draft_message_id,
            to_emails        = EXCLUDED.to_emails,
            subject          = EXCLUDED.subject
        RETURNING id
        """,
        (
            ids["draft_id"],
            ids["thread_id"],
            ids["draft_message_id"],
            account_email,
            json.dumps(to_emails or []),
            (subject or "")[:500] or None,
            contact_id,
            source,
            now_utc(),
        ),
    )
    row = cursor.fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# Leitura: casar rascunho com enviado
# ---------------------------------------------------------------------------

def draft_matches_sent(
    draft: Dict[str, Any],
    sent_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> bool:
    """A regra de casamento, isolada para poder ser provada.

    O `thread_id` já foi casado por quem buscou a linha — aqui sobram as duas
    condições que sobram:

    (a) o rascunho ainda está pendente. Rascunho já 'sent' não re-reconcilia
        (senão toda run do sync de 30min reescreve sent_at e a data do ato do
        Renato vira a data da última varredura);

    (b) o envio é POSTERIOR à criação do rascunho. Sem isto, um backfill
        histórico (`max_pages` alto, corpus de meses) casaria um e-mail antigo
        da mesma thread com um rascunho de hoje e declararia enviado o que
        ninguém enviou — a mesma armadilha que fez `post_process=False` existir
        no gmail_outbound_sync.
    """
    if (draft.get("status") or "pending") != "pending":
        return False

    criado_em = to_utc(draft.get("criado_em"))
    if criado_em is None:
        # Linha sem carimbo de criação: não dá para afirmar a ordem dos fatos,
        # e afirmar "enviado" errado é pior que continuar pendente.
        return False

    momento = to_utc(sent_at) or (now or now_utc())
    return momento >= criado_em - CLOCK_SKEW


def reconcile_thread(
    cursor,
    thread_id: Optional[str],
    *,
    sent_at: Optional[datetime] = None,
    sent_message_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[int]:
    """Marca como enviado todo rascunho pendente da thread. Devolve os ids.

    O SELECT busca SÓ por thread_id — de propósito. Nenhuma coluna de message
    id entra na busca: o id do rascunho e o da mensagem enviada são diferentes
    por construção (ver docstring do módulo).
    """
    if not thread_id:
        return []

    cursor.execute(
        """
        SELECT id, status, criado_em
        FROM email_drafts
        WHERE thread_id = %s
        """,
        (thread_id,),
    )
    rows = cursor.fetchall() or []

    agora = now or now_utc()
    casados = [d["id"] for d in (dict(r) for r in rows) if draft_matches_sent(d, sent_at, now=agora)]
    if not casados:
        return []

    cursor.execute(
        """
        UPDATE email_drafts
        SET status = 'sent',
            sent_at = %s,
            sent_message_id = %s,
            reconciled_at = %s
        WHERE id = ANY(%s)
        """,
        (to_utc(sent_at) or agora, sent_message_id, agora, casados),
    )
    return casados


def reconcile_sent_threads(threads: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Roda a reconciliação para uma leva de mensagens enviadas.

    Cada item: {thread_id, sent_at, message_id}. Abre a própria conexão — é o
    ponto de entrada do gmail_outbound_sync, que já fechou a dele antes do
    pós-processamento.
    """
    from database import get_db

    itens = [t for t in threads if t.get("thread_id")]
    out = {"threads": len(itens), "drafts_sent": 0, "draft_ids": []}
    if not itens:
        return out

    with get_db() as conn:
        cursor = conn.cursor()
        for item in itens:
            try:
                ids = reconcile_thread(
                    cursor,
                    item.get("thread_id"),
                    sent_at=item.get("sent_at"),
                    sent_message_id=item.get("message_id"),
                )
            except Exception as e:
                # Reconciliação é observação, não ingestão: falhar aqui não pode
                # derrubar o sync que já gravou as mensagens.
                logger.warning(f"reconcile_thread falhou para {item.get('thread_id')}: {e}")
                continue
            out["drafts_sent"] += len(ids)
            out["draft_ids"].extend(ids)
        conn.commit()

    return out


def pending_drafts(cursor, limit: int = 50) -> List[Dict[str, Any]]:
    """Rascunhos que a camada criou e ninguém mandou ainda.

    Existe para que quem for relatar ("está pronto no rascunho") possa ler o
    estado em vez de supô-lo — foi supor que produziu o caso de 04/08.
    """
    cursor.execute(
        """
        SELECT id, draft_id, thread_id, account_email, to_emails,
               subject, contact_id, source, criado_em
        FROM email_drafts
        WHERE status = 'pending'
        ORDER BY criado_em DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [dict(r) for r in (cursor.fetchall() or [])]
