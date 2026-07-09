"""
WA Ingest — endpoint persist-only de ingestão WhatsApp (instância da Tonia).

Contexto (08/07/2026): a conversa Renato↔Tonia acontece na instância Evolution
`intel-bot-v2`, cujo webhook aponta pro repo da Tonia. Desde 04/07 o INTEL
não via mais essas mensagens (a Tonia parou de repassar por causa de um bug
de resposta dupla). Fix estrutural: a Tonia repassa TODOS os upserts da
instância dela pra cá — e este módulo APENAS PERSISTE.

PROIBIDO por construção (é o que torna resposta dupla impossível):
  - qualquer chamada a intel_bot / handle_bot_message
  - realtime_analyzer / wa_triage / action_proposals
  - dispatch pra worker, LLM, ou qualquer roteamento
Só parse + INSERT em messages + telemetria em webhook_audit.

Semântica de direção NESTA instância (intel-bot-v2):
  - fromMe=true  → direction='outgoing' = a TONIA enviou (não o Renato!)
  - fromMe=false → direction='incoming' = o contato (Renato) enviou
O metadata da mensagem registra {"instance": ..., "source": "wa_ingest"}
pra desambiguar de mensagens da instância pessoal (rap-whatsapp).

Auth: header X-Ingest-Secret == env WA_INGEST_SECRET (strip(), SEM fallback
hardcoded — mesmo padrão de services/worker_secret.py). Env ausente => 401.
"""
import json
import logging
import os
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

INGEST_SOURCE = "wa_ingest"


# ==================== AUTH ====================

def get_ingest_secret() -> str:
    """Lê WA_INGEST_SECRET do env, com strip() (Vercel cola \\n em env vars).

    Retorna "" se ausente — sem fallback. Callers tratam "" como
    misconfiguração (rejeitar, nunca aceitar default).
    """
    return os.environ.get("WA_INGEST_SECRET", "").strip()


def check_ingest_secret(provided) -> bool:
    """Valida o header X-Ingest-Secret. Env ausente => False (vira 401)."""
    expected = get_ingest_secret()
    if not expected:
        logger.error("WA_INGEST_SECRET não configurado — rejeitando request (401)")
        return False
    return bool(provided) and str(provided).strip() == expected


# ==================== INGEST ====================

def ingest_evolution_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Persiste (e SÓ persiste) um payload cru de webhook Evolution.

    Retorna sempre {"stored": bool, "reason": str} — idempotente: repetir o
    mesmo payload devolve stored=false/reason=duplicate sem efeito colateral.

    Sync (psycopg2) — caller async deve envolver em asyncio.to_thread.
    """
    started = time.monotonic()

    if not isinstance(payload, dict):
        return {"stored": False, "reason": "invalid_payload"}

    # Evolution varia a grafia do evento entre versões (messages.upsert vs
    # MESSAGES_UPSERT). Normaliza antes de qualquer decisão.
    event = str(payload.get("event") or "").strip().lower().replace("_", ".")
    instance = str(payload.get("instance") or "").strip()
    data = payload.get("data") or {}
    key = data.get("key") if isinstance(data, dict) else {}
    key = key or {}
    remote_jid = key.get("remoteJid") or ""
    from_me = key.get("fromMe")
    wa_message_id = key.get("id") or ""

    def _audit(reason: str, resulting_message_id: int = None) -> None:
        # Telemetria defensiva — nunca falha o ingest (padrão evolution_api).
        try:
            from integrations.evolution_api import _record_webhook_audit
            _record_webhook_audit(
                source=INGEST_SOURCE,
                event_type=event or None,
                instance=instance or None,
                remote_jid=remote_jid or None,
                from_me=from_me if isinstance(from_me, bool) else None,
                message_id=wa_message_id or None,
                decision="ingest_only",
                decision_reason=reason,
                resulting_message_id=resulting_message_id,
                payload=payload,
                processing_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as e:
            logger.warning(f"wa_ingest: webhook_audit falhou: {e}")

    # Aceita upsert (mensagem recebida) e send.message (mensagem enviada via
    # API — é assim que a resposta da Tonia chega: Evolution NÃO emite
    # messages.upsert com fromMe pra sends via API, só SEND_MESSAGE).
    # Demais eventos (connection.update, messages.update etc.): ignora.
    if event not in ("messages.upsert", "send.message"):
        _audit("ignored_event")
        return {"stored": False, "reason": "ignored_event"}

    try:
        return _ingest_upsert(payload, event, instance, wa_message_id, _audit)
    except Exception as e:
        logger.error(f"wa_ingest: erro ao persistir: {e}")
        _audit(f"error: {e}")
        # 200 idempotente mesmo em erro interno — a Tonia não deve retryar
        # em loop nem tratar o INTEL como dependência crítica do send dela.
        return {"stored": False, "reason": "error"}


def _resolve_contact_id(phone: str) -> int:
    """
    Resolve contato por telefone — matching digit-normalizado (mesma
    semântica de WhatsAppSyncService._find_contact_by_phone: exato,
    últimos-9, últimos-8), mas DETERMINÍSTICO em caso de contatos
    duplicados com o mesmo número: prefere o contato que já tem a conversa
    WhatsApp estabelecida (mais mensagens), depois menor id.

    Why: existem duplicatas reais (ex.: Renato #23419 e #25613 com o mesmo
    número) e _find_contact_by_phone devolve "o primeiro que vier" — sem
    isso o ingest fragmentaria a thread canônica (conv 590 / #25613).
    Retorna 0 se não achou.
    """
    import re

    from database import get_db

    phone_digits = re.sub(r"\D", "", str(phone or ""))
    if not phone_digits or len(phone_digits) < 8:
        return 0

    candidates = []  # contact_id -> melhor rank (0=exato, 1=last9, 2=last8)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, telefones FROM contacts
            WHERE telefones IS NOT NULL AND telefones::text != '[]'
        """)
        for contact in cursor.fetchall():
            telefones = contact["telefones"]
            if isinstance(telefones, str):
                try:
                    telefones = json.loads(telefones)
                except Exception:
                    telefones = []
            best = None
            for tel in telefones or []:
                if isinstance(tel, dict):
                    tel_number = tel.get("number", "") or tel.get("phone", "")
                else:
                    tel_number = str(tel)
                tel_digits = re.sub(r"\D", "", tel_number)
                if not tel_digits or len(tel_digits) < 8:
                    continue
                if tel_digits == phone_digits:
                    rank = 0
                elif tel_digits[-9:] == phone_digits[-9:]:
                    rank = 1
                elif tel_digits[-8:] == phone_digits[-8:]:
                    rank = 2
                else:
                    continue
                best = rank if best is None else min(best, rank)
            if best is not None:
                candidates.append((contact["id"], best))

        if not candidates:
            return 0
        if len(candidates) == 1:
            return candidates[0][0]

        # Desempate: volume de mensagens na conversa WhatsApp existente.
        ids = tuple(c[0] for c in candidates)
        cursor.execute("""
            SELECT conv.contact_id, COUNT(m.id) AS msg_count
            FROM conversations conv
            LEFT JOIN messages m ON m.conversation_id = conv.id
            WHERE conv.contact_id IN %s AND conv.canal = 'whatsapp'
            GROUP BY conv.contact_id
        """, (ids,))
        msg_counts = {r["contact_id"]: r["msg_count"] for r in cursor.fetchall()}

    candidates.sort(key=lambda c: (c[1], -msg_counts.get(c[0], 0), c[0]))
    return candidates[0][0]


def _ingest_upsert(payload, event, instance, wa_message_id, _audit) -> Dict[str, Any]:
    from database import get_db
    from integrations.whatsapp import parse_webhook_message
    from services.whatsapp_sync import WhatsAppSyncService

    # parse_webhook_message espera event exatamente "messages.upsert" —
    # passa cópia rasa com o evento já normalizado.
    normalized = dict(payload)
    normalized["event"] = "messages.upsert"

    parsed = parse_webhook_message(normalized)
    if not parsed:
        # Grupo (@g.us), status broadcast, tipo sem conteúdo extraível etc.
        _audit("not_parsed")
        return {"stored": False, "reason": "not_parsed"}

    content = (parsed.get("content") or "").strip()
    if not content:
        _audit("empty_content")
        return {"stored": False, "reason": "empty_content"}

    # remoteJid é sempre o counterparty do chat — vale pra fromMe=true
    # (Tonia respondendo) e fromMe=false (contato mandando). Ou seja, o
    # contato resolvido é o interlocutor humano em ambas as direções.
    phone = parsed.get("phone") or ""
    direction = parsed.get("direction") or "incoming"
    timestamp = parsed.get("timestamp")
    msg_id = parsed.get("message_id") or wa_message_id or ""

    contact_id = _resolve_contact_id(phone)
    if not contact_id:
        _audit(f"contact_not_found: {phone}")
        return {"stored": False, "reason": "contact_not_found"}

    conversation_id = WhatsAppSyncService()._get_or_create_conversation(contact_id, phone)
    if not conversation_id:
        _audit("no_conversation")
        return {"stored": False, "reason": "no_conversation"}

    with get_db() as conn:
        cursor = conn.cursor()

        # Dedup pelo padrão existente (metadata->>'message_id').
        if msg_id:
            cursor.execute(
                """
                SELECT id FROM messages
                WHERE conversation_id = %s AND metadata->>'message_id' = %s
                LIMIT 1
                """,
                (conversation_id, msg_id),
            )
        else:
            cursor.execute(
                """
                SELECT id FROM messages
                WHERE conversation_id = %s AND conteudo = %s AND enviado_em = %s
                LIMIT 1
                """,
                (conversation_id, content, timestamp),
            )

        if cursor.fetchone():
            _audit("duplicate")
            return {"stored": False, "reason": "duplicate"}

        metadata = json.dumps({
            "message_id": msg_id,
            "source": INGEST_SOURCE,
            "instance": instance or None,
            "push_name": parsed.get("push_name"),
        })

        cursor.execute(
            """
            INSERT INTO messages (conversation_id, contact_id, direcao, conteudo, enviado_em, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (conversation_id, contact_id, direction, content, timestamp, metadata),
        )
        new_id = cursor.fetchone()["id"]

        cursor.execute(
            """
            UPDATE conversations
            SET total_mensagens = total_mensagens + 1, ultimo_mensagem = %s
            WHERE id = %s
            """,
            (timestamp, conversation_id),
        )
        conn.commit()

    # NOTA: intencionalmente NÃO chama _update_contact_interaction nem
    # dismiss_stale_on_reply — "outgoing" aqui é a Tonia, não o Renato;
    # atualizar interação/propostas com base nisso poluiria os sinais.
    logger.info(
        f"wa_ingest: msg {new_id} persistida (contato {contact_id}, "
        f"direcao {direction}, instance {instance or '?'})"
    )
    _audit("stored", resulting_message_id=new_id)
    return {"stored": True, "reason": "stored"}
