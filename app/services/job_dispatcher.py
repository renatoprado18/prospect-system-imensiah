"""
Job Dispatcher
Cria registros em background_jobs e dispara endpoints do Railway worker
em fire-and-forget pattern (timeout curto). O worker processa async e
atualiza o registro do job.

Pattern usado por:
- /api/cron/sync-gmail (services/gmail_sync.py migrado pra Railway)
- step_gmail dentro do daily-sync (mesmo motivo)

Why this exists: Vercel mata funcoes em 300s. Loops grandes (gmail sync,
audio transcribe, image analyze) precisam ir pro Railway worker.

Autor: INTEL
Data: 2026-05-02
"""
from __future__ import annotations
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

import httpx

from database import get_db

logger = logging.getLogger(__name__)


def _worker_url() -> str:
    """Mantem o nome AUDIO_WORKER_URL por compatibilidade — mesma URL serve
    todos os jobs do worker (transcribe, sync-gmail, analyze-image, etc)."""
    return (os.getenv("AUDIO_WORKER_URL") or "").strip()


def _worker_secret() -> str:
    return (os.getenv("WORKER_SECRET") or "intel-audio-2026").strip()


async def enqueue_job(
    job_type: str,
    payload: Dict[str, Any],
    dispatch_path: str,
    dispatch_timeout: float = 8.0,
) -> Tuple[Optional[int], bool, Optional[str]]:
    """
    Cria registro em background_jobs e dispara POST fire-and-forget pro worker.

    Args:
        job_type: identificador do tipo de job (ex.: 'gmail_sync')
        payload: dict serializavel (vira JSON enviado pro worker, alem de
                 ir pro campo result inicial pra debugging)
        dispatch_path: rota no worker (ex.: '/sync-gmail')
        dispatch_timeout: segundos pra esperar o worker aceitar o request

    Returns:
        (job_id, dispatched, error)
        - job_id: ID criado em background_jobs (ou None se INSERT falhou)
        - dispatched: True se worker aceitou o request (HTTP 2xx) ou nao
                      bloqueia (timeout) — semantica fire-and-forget
        - error: mensagem de erro se houver, senao None
    """
    job_id = None
    error = None

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO background_jobs (job_type, status, result, started_at)
                VALUES (%s, 'queued', %s, NOW())
                RETURNING id
                """,
                (job_type, json.dumps({"payload": payload})),
            )
            row = cursor.fetchone()
            job_id = row["id"] if isinstance(row, dict) else row[0]
            conn.commit()
    except Exception as e:
        logger.exception(f"enqueue_job: failed to INSERT job_type={job_type}")
        return None, False, f"insert_failed: {e}"

    url = _worker_url()
    if not url:
        # Marca falha imediata pra ficar claro no banco
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE background_jobs SET status='error', error=%s, "
                    "completed_at=NOW() WHERE id=%s",
                    ("AUDIO_WORKER_URL not configured", job_id),
                )
                conn.commit()
        except Exception:
            pass
        return job_id, False, "worker_url_missing"

    body = {**payload, "job_id": job_id, "secret": _worker_secret()}
    try:
        async with httpx.AsyncClient(timeout=dispatch_timeout) as client:
            resp = await client.post(f"{url}{dispatch_path}", json=body)
        if resp.status_code in (200, 202):
            logger.info(f"enqueue_job: dispatched job_id={job_id} type={job_type} -> {resp.status_code}")
            return job_id, True, None
        error = f"worker_status_{resp.status_code}"
        logger.warning(f"enqueue_job: worker returned {resp.status_code} for job_id={job_id}: {resp.text[:200]}")
    except httpx.TimeoutException:
        # Em fire-and-forget, timeout no dispatch nao significa falha do job —
        # so significa que o worker demorou pra responder. O job ainda vai rodar.
        logger.info(f"enqueue_job: dispatch timeout (ok) job_id={job_id}")
        return job_id, True, None
    except Exception as e:
        error = f"dispatch_error: {e}"
        logger.exception(f"enqueue_job: dispatch failed job_id={job_id}")

    # Marca como erro se nao dispatchou bem
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE background_jobs SET status='error', error=%s, completed_at=NOW() WHERE id=%s",
                (error or "dispatch_failed", job_id),
            )
            conn.commit()
    except Exception:
        pass
    return job_id, False, error


def get_job_status(job_id: int) -> Optional[Dict[str, Any]]:
    """Retorna status do job pra visibilidade no /api/jobs/{id}."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, job_type, status, total_items, processed_items, "
                "success_count, failed_count, skipped_count, result, error, "
                "started_at, completed_at FROM background_jobs WHERE id = %s",
                (job_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            # Datetimes -> ISO
            for k in ("started_at", "completed_at"):
                if d.get(k):
                    d[k] = d[k].isoformat()
            return d
    except Exception as e:
        logger.exception(f"get_job_status({job_id}) failed")
        return {"error": str(e)}
