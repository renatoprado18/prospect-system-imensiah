"""
Cron Telemetry — wrapper que loga cada execucao de cron job.

Uso:
    from services.cron_telemetry import track_cron_run

    @app.get("/api/cron/foo")
    @track_cron_run
    async def cron_foo(request: Request):
        ...
        return {"job": "foo", "status": "success", "rows": 42}

Em cada invocacao:
- INSERT em cron_runs com status='running'
- Apos retorno bem-sucedido: UPDATE com status='success', duration_ms, result_json,
  rows_affected (extraido heuristicamente do dict de retorno)
- Em exception: UPDATE com status='error', error_message — e re-raise

Falhas na telemetria NUNCA quebram o cron (try/except externo + logger.warning).

Why: ate hoje os crons rodavam "as cegas" — diagnostico era feito por evidencias
indiretas (atualizado_em em contacts, etc). Cron_runs centraliza historico
real, possibilita /api/admin/cron-health e widget no dashboard.
"""
import functools
import json
import logging
import time
from typing import Any, Optional

from fastapi import Request
from database import get_db

logger = logging.getLogger(__name__)


# Chaves comuns no retorno dos crons que indicam "quantas linhas/itens foram afetados"
_ROWS_KEYS = (
    "rows_affected", "rows", "count", "total",
    "contacts_updated", "updated", "imported", "deleted",
    "processed", "synced", "created", "sent",
    "items", "tasks_created", "tasks_resolved",
    "messages_processed", "events_synced", "documents_indexed",
)


def _extract_rows_affected(result: Any) -> Optional[int]:
    """Tenta heuristicamente extrair um inteiro de 'linhas afetadas' do retorno."""
    if not isinstance(result, dict):
        return None
    for key in _ROWS_KEYS:
        v = result.get(key)
        if isinstance(v, int):
            return v
    # total_changes / total_X
    for k, v in result.items():
        if isinstance(v, int) and ("total" in k.lower() or "count" in k.lower()):
            return v
    return None


def _safe_json(obj: Any) -> Optional[str]:
    """Serializa pra JSON com fallback — nunca levanta."""
    try:
        return json.dumps(obj, default=str)[:200000]  # cap em ~200KB
    except Exception:
        try:
            return json.dumps({"_repr": repr(obj)[:1000]})
        except Exception:
            return None


def _insert_run(path: str) -> Optional[int]:
    """Insere row inicial com status='running'. Retorna id ou None em caso de falha."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cron_runs (path, status, started_at)
                VALUES (%s, 'running', NOW())
                RETURNING id
                """,
                (path,),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["id"] if row else None
    except Exception:
        logger.warning("cron_telemetry: insert running row failed", exc_info=True)
        return None


def _finalize_run(
    run_id: Optional[int],
    *,
    status: str,
    duration_ms: int,
    result: Any = None,
    error_message: Optional[str] = None,
    http_status: Optional[int] = None,
):
    """Update final da row (success ou error). Nunca levanta."""
    if run_id is None:
        return
    try:
        rows_affected = _extract_rows_affected(result)
        result_json = _safe_json(result) if result is not None else None
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE cron_runs
                SET status = %s,
                    finished_at = NOW(),
                    duration_ms = %s,
                    rows_affected = %s,
                    result_json = %s::jsonb,
                    error_message = %s,
                    http_status = %s
                WHERE id = %s
                """,
                (
                    status,
                    duration_ms,
                    rows_affected,
                    result_json,
                    (error_message[:4000] if error_message else None),
                    http_status,
                    run_id,
                ),
            )
            conn.commit()
    except Exception:
        logger.warning("cron_telemetry: finalize run %s failed", run_id, exc_info=True)


def track_cron_run(handler):
    """
    Decorator pra cron endpoints. Garante que cada invocacao seja registrada
    em cron_runs com status, duration e payload.

    Espera que o handler seja `async def handler(request: Request, ...)`.
    """
    @functools.wraps(handler)
    async def wrapper(request: Request, *args, **kwargs):
        # Path pra dedup/agrupamento — usar request.url.path (nao inclui query string)
        try:
            path = request.url.path
        except Exception:
            path = getattr(request, "scope", {}).get("path", "/api/cron/unknown")

        run_id = _insert_run(path)
        started = time.time()

        try:
            result = await handler(request, *args, **kwargs)
            duration_ms = int((time.time() - started) * 1000)
            _finalize_run(
                run_id,
                status="success",
                duration_ms=duration_ms,
                result=result,
                http_status=200,
            )
            return result
        except Exception as e:
            duration_ms = int((time.time() - started) * 1000)
            # Status HTTP heuristico — se for HTTPException usa o code, senao 500
            try:
                from fastapi import HTTPException
                http_status = e.status_code if isinstance(e, HTTPException) else 500
            except Exception:
                http_status = 500
            _finalize_run(
                run_id,
                status="error",
                duration_ms=duration_ms,
                error_message=f"{type(e).__name__}: {e}",
                http_status=http_status,
            )
            raise

    return wrapper
