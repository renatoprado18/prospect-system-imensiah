"""
scheduled_actions — primitivo CoS pra agendar acoes futuras com confirmacao ativa.

Why: GH Actions one-shot pra agendamentos discretos (envio WA pra X em Y data) e
band-aid. Erros de aritmetica de data ficam silenciosos, nao tem auditabilidade, e
o usuario precisa monitorar manualmente. Incidente Marcos Tanaka 07/06/26
(agendado pra 09/06 quando deveria ser 08/06) motivou esse refactor.

V0:
- Schedule via schedule_wa(...)
- Processamento via cron a cada 5min em /api/cron/process-scheduled-actions
- Idempotencia via dedup_key UNIQUE
- Notificacao ativa pos-execucao via WA (send_intel_notification)
- UI admin em /admin/scheduled-actions

V1 (pendente):
- Ping T-15min "vou mandar em 15min, responde NAO pra cancelar"
- 2-way cancel via WA reply
- Suporte a action_type='email', 'create_task', etc.
- Retry policy com backoff exponencial

Suporta apenas wa_send em V0. action_type extensivel via payload JSONB.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import now_utc, to_utc

logger = logging.getLogger(__name__)

# Tipos suportados em V0
SUPPORTED_ACTION_TYPES = {"wa_send"}


def _normalize_dt(dt: datetime) -> datetime:
    """Garante tz-aware UTC, depois retorna naive UTC pra storage TIMESTAMP."""
    aware = to_utc(dt)
    if aware is None:
        raise ValueError("scheduled_for nao pode ser None")
    return aware.replace(tzinfo=None)


def schedule_wa(
    instance: str,
    number: str,
    text: str,
    scheduled_for: datetime,
    source: str,
    dedup_key: Optional[str] = None,
    created_by: str = "cos",
    max_attempts: int = 3,
) -> int:
    """
    Agenda envio WA. Retorna id da row.

    Se dedup_key existir, retorna id existente sem duplicar (idempotency).

    Args:
        instance: 'rap-whatsapp' ou 'intel-bot' (nome de instancia Evolution).
        number: telefone no formato '5511984153337' (sera normalizado no envio).
        text: corpo da mensagem.
        scheduled_for: datetime quando deve disparar (UTC tz-aware preferencialmente).
        source: contexto livre pra audit log ("sessao 08/06/26 retomada FUP Marcos").
        dedup_key: chave UNIQUE pra idempotency (opcional).
        created_by: quem criou ('cos' / 'user_renato' / 'system' / nome do script).
        max_attempts: limite de tentativas antes de marcar status='failed'.

    Returns:
        id da row scheduled_actions.
    """
    if not instance or not number or not text:
        raise ValueError("schedule_wa requer instance, number e text nao-vazios")

    payload = {
        "instance": instance,
        "number": number,
        "text": text,
    }
    scheduled_for_naive = _normalize_dt(scheduled_for)

    with get_db() as conn:
        cursor = conn.cursor()

        # Idempotency check
        if dedup_key:
            cursor.execute(
                "SELECT id FROM scheduled_actions WHERE dedup_key = %s",
                (dedup_key,),
            )
            existing = cursor.fetchone()
            if existing:
                logger.info(
                    f"scheduled_actions: dedup_key={dedup_key} ja existe (id={existing['id']}), nao duplica"
                )
                return existing["id"]

        cursor.execute(
            """
            INSERT INTO scheduled_actions (
                action_type, payload, scheduled_for, status,
                attempts, max_attempts, created_by, source, dedup_key
            )
            VALUES (%s, %s::jsonb, %s, 'pending', 0, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                "wa_send",
                json.dumps(payload),
                scheduled_for_naive,
                max_attempts,
                created_by,
                source,
                dedup_key,
            ),
        )
        new_id = cursor.fetchone()["id"]
        conn.commit()

    logger.info(
        f"scheduled_actions: created id={new_id} type=wa_send for {scheduled_for_naive} "
        f"(source='{source}', dedup_key={dedup_key})"
    )
    return new_id


async def _execute_wa_send(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executa um wa_send. Retorna dict {ok: bool, msg_id, status, error}.

    NAO atualiza DB nem notifica — isso fica em process_due.
    """
    from integrations.evolution_api import EvolutionAPIClient

    payload = row["payload"] or {}
    if isinstance(payload, str):
        # Defensive: alguns drivers retornam jsonb como string
        try:
            payload = json.loads(payload)
        except Exception:
            return {"ok": False, "error": f"payload nao parseavel: {payload!r}"}

    instance = payload.get("instance")
    number = payload.get("number")
    text = payload.get("text")

    if not instance or not number or not text:
        return {"ok": False, "error": f"payload incompleto: {payload!r}"}

    try:
        client = EvolutionAPIClient(instance_name=instance)
        if not client.is_configured:
            return {"ok": False, "error": "Evolution API nao configurada (EVOLUTION_API_URL/KEY)"}

        result = await client.send_text(number, text, instance_name=instance)

        if "error" in result:
            return {
                "ok": False,
                "error": str(result.get("error"))[:500],
            }

        # Evolution v2 retorna key.id no top-level ou aninhado
        msg_id = (
            (result.get("key") or {}).get("id")
            or result.get("id")
            or ""
        )
        status = result.get("status") or "PENDING"

        return {
            "ok": True,
            "msg_id": msg_id,
            "status": status,
        }

    except Exception as e:
        logger.exception(f"scheduled_actions: _execute_wa_send id={row['id']} crashed: {e}")
        return {"ok": False, "error": f"exception: {e}"}


async def _notify_renato(success: bool, row: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Confirmacao ativa pos-execucao. Falha silenciosa (best-effort)."""
    try:
        from services.intel_bot import send_intel_notification

        payload = row["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        number = payload.get("number", "?")
        text = (payload.get("text") or "")[:80]
        source = row.get("source") or ""

        if success:
            msg_id = result.get("msg_id", "?")
            note = (
                f"CoS enviou WA pra {number}: \"{text}{'...' if len(payload.get('text', '')) > 80 else ''}\""
                f" - msg id {msg_id}"
            )
            if source:
                note += f" [{source}]"
        else:
            err = result.get("error", "erro desconhecido")[:200]
            attempts = row.get("attempts", 0) + 1
            max_a = row.get("max_attempts", 3)
            terminal = attempts >= max_a
            prefix = "CoS DESISTIU envio WA" if terminal else "CoS falhou envio WA (vai retentar)"
            note = f"{prefix} pra {number}: {err} (tentativa {attempts}/{max_a})"
            if source:
                note += f" [{source}]"

        await send_intel_notification(note)
    except Exception as e:
        logger.warning(f"scheduled_actions: _notify_renato falhou silenciosamente: {e}")


async def process_due(now: Optional[datetime] = None, limit: int = 50) -> Dict[str, int]:
    """
    Processa rows com status=pending AND scheduled_for <= now AND attempts < max_attempts.

    Idempotente: row e atualizada com lock SELECT FOR UPDATE SKIP LOCKED, e a
    transicao pending->sent/failed acontece numa transacao por row. Mesmo cron
    rodando 2x simultaneamente nao duplica envio.

    Returns:
        {"processed": N, "sent": M, "failed": K, "skipped": L}
    """
    if now is None:
        now = now_utc()
    now_naive = _normalize_dt(now)

    stats = {"processed": 0, "sent": 0, "failed": 0, "skipped": 0}

    # Pega lista de IDs prontos primeiro (snapshot leve)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM scheduled_actions
            WHERE status = 'pending'
              AND scheduled_for <= %s
              AND attempts < max_attempts
            ORDER BY scheduled_for ASC
            LIMIT %s
            """,
            (now_naive, limit),
        )
        ids = [r["id"] for r in cursor.fetchall()]

    if not ids:
        return stats

    logger.info(f"scheduled_actions: process_due encontrou {len(ids)} rows prontas")

    for row_id in ids:
        try:
            # Lock + reread row atomicamente
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, action_type, payload, scheduled_for, status,
                           attempts, max_attempts, created_by, source, dedup_key
                    FROM scheduled_actions
                    WHERE id = %s AND status = 'pending'
                    FOR UPDATE SKIP LOCKED
                    """,
                    (row_id,),
                )
                row = cursor.fetchone()
                if not row:
                    stats["skipped"] += 1
                    conn.commit()
                    continue

                # Bump attempts dentro da mesma tx (mesmo se crashar depois,
                # a row nao fica em loop infinito porque attempts ja subiu).
                cursor.execute(
                    "UPDATE scheduled_actions SET attempts = attempts + 1 WHERE id = %s",
                    (row_id,),
                )
                conn.commit()
                row = dict(row)
                row["attempts"] = row["attempts"] + 1  # refletir incremento na cabeca

            stats["processed"] += 1

            # Despacha por tipo
            action_type = row["action_type"]
            if action_type not in SUPPORTED_ACTION_TYPES:
                logger.error(f"scheduled_actions: id={row_id} action_type={action_type!r} nao suportado em V0")
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        UPDATE scheduled_actions
                        SET status='failed', last_error=%s, executed_at=NOW()
                        WHERE id=%s
                        """,
                        (f"action_type {action_type!r} nao suportado em V0", row_id),
                    )
                    conn.commit()
                stats["failed"] += 1
                continue

            # action_type == 'wa_send'
            result = await _execute_wa_send(row)

            if result.get("ok"):
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        UPDATE scheduled_actions
                        SET status='sent', executed_at=NOW(),
                            result_msg_id=%s, result_status=%s, last_error=NULL
                        WHERE id=%s
                        """,
                        (result.get("msg_id"), result.get("status"), row_id),
                    )
                    conn.commit()
                stats["sent"] += 1
                await _notify_renato(True, row, result)
            else:
                # Falha. Se atingiu max_attempts -> status='failed'. Senao fica pending pra retry.
                terminal = row["attempts"] >= row["max_attempts"]
                new_status = "failed" if terminal else "pending"
                err_msg = (result.get("error") or "")[:1000]
                with get_db() as conn:
                    cursor = conn.cursor()
                    if terminal:
                        cursor.execute(
                            """
                            UPDATE scheduled_actions
                            SET status='failed', last_error=%s, executed_at=NOW()
                            WHERE id=%s
                            """,
                            (err_msg, row_id),
                        )
                    else:
                        cursor.execute(
                            "UPDATE scheduled_actions SET last_error=%s WHERE id=%s",
                            (err_msg, row_id),
                        )
                    conn.commit()
                stats["failed"] += 1
                # So notifica em falha terminal (evita ruido em retries)
                if terminal:
                    await _notify_renato(False, row, result)

        except Exception as e:
            logger.exception(f"scheduled_actions: process_due id={row_id} crashed: {e}")
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE scheduled_actions SET last_error=%s WHERE id=%s",
                        (f"crash: {e}"[:1000], row_id),
                    )
                    conn.commit()
            except Exception:
                pass
            stats["failed"] += 1

    logger.info(f"scheduled_actions: process_due result={stats}")
    return stats


def list_pending(limit: int = 50) -> List[Dict[str, Any]]:
    """Lista acoes pendentes (status='pending') ordenadas por scheduled_for ASC."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, action_type, payload, scheduled_for, status,
                       attempts, max_attempts, last_error, created_by, source,
                       dedup_key, result_msg_id, result_status, created_at, executed_at
                FROM scheduled_actions
                WHERE status = 'pending'
                ORDER BY scheduled_for ASC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"scheduled_actions: list_pending error: {e}")
        return []


def cancel(action_id: int) -> bool:
    """Cancela uma acao pendente. Retorna True se cancelada, False se nao era pending."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE scheduled_actions
                SET status='cancelled', executed_at=NOW()
                WHERE id = %s AND status = 'pending'
                RETURNING id
                """,
                (action_id,),
            )
            row = cursor.fetchone()
            conn.commit()
            return row is not None
    except Exception as e:
        logger.error(f"scheduled_actions: cancel({action_id}) error: {e}")
        return False


def get_audit_log(limit: int = 100, days: int = 7) -> List[Dict[str, Any]]:
    """
    Audit log das ultimas N acoes (qualquer status) dos ultimos `days` dias.
    Usado pela UI /admin/scheduled-actions.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, action_type, payload, scheduled_for, status,
                       attempts, max_attempts, last_error, created_by, source,
                       dedup_key, result_msg_id, result_status, created_at, executed_at
                FROM scheduled_actions
                WHERE created_at > NOW() - (%s * INTERVAL '1 day')
                ORDER BY
                    CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                    scheduled_for ASC,
                    created_at DESC
                LIMIT %s
                """,
                (days, limit),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"scheduled_actions: get_audit_log error: {e}")
        return []
