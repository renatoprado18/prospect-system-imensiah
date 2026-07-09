"""
Variant 2 (19/06/26): dev_delegation_pickup portado pro Railway worker.

Motivacao: cron Vercel /api/cron/dev-delegation-pickup chamava _call_delegator
com timeout 350s, mas Vercel mata em 300s — runs ficavam stuck em status=running
sem finished_at. Diagnostico monitor 19/06 detectou 2 runs stuck (id 15, 16).

Esse modulo espelha app/services/dev_delegation_pickup.py mas com psycopg
direto + zoneinfo (sem importar app.database / services.tz). Roda in-process
no APScheduler do worker — sem teto de timeout.

Guard-rails identicos ao original:
  - DEV_DELEGATION_SHADOW=1 (default seguro): so loga payload
  - DEV_DELEGATION_MAX_USD_DAY=5: cap diario (BRT day)
  - DEV_DELEGATION_MAX_PER_CYCLE: 1 quando shadow=off, 3 quando shadow=on
  - Janela 9-22 BRT
  - Skip task_summary 'Follow-up #%' (Tonha gera, ela mesma cobraria)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

SP_TZ = ZoneInfo("America/Sao_Paulo")
UTC = timezone.utc


def _env_bool(key: str, default: str = "1") -> bool:
    return (os.getenv(key, default) or default).strip() in ("1", "true", "TRUE", "yes")


def _env_float(key: str, default: float) -> float:
    try:
        return float((os.getenv(key, "") or "").strip() or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int((os.getenv(key, "") or "").strip() or default)
    except ValueError:
        return default


def _conn():
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL nao setado no worker")
    return psycopg.connect(url, row_factory=dict_row)


def _today_cost_usd() -> float:
    """Soma cost_usd de hoje (BRT day). Comparacao em UTC naive — mesma
    semantica do original em app/services/dev_delegation_pickup.py."""
    now_utc_aware = datetime.now(UTC)
    midnight_brt = now_utc_aware.astimezone(SP_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    midnight_utc_naive = midnight_brt.astimezone(UTC).replace(tzinfo=None)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS total
                FROM dev_delegation_runs
                WHERE started_at >= %s AND status='success'
                """,
                (midnight_utc_naive,),
            )
            row = cur.fetchone()
            return float(row["total"] or 0)


def _fetch_open_dev(limit: int) -> List[Dict[str, Any]]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, task_summary, task_full, contact_id, signal_id, deadline
                FROM delegations
                WHERE delegated_to='dev'
                  AND status='open'
                  AND response IS NULL
                  AND task_summary NOT LIKE 'Follow-up #%%'
                ORDER BY deadline ASC NULLS LAST, criado_em ASC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]


def _insert_run(*, delegation_id: int, shadow: bool, mode: str, payload: Dict[str, Any]) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dev_delegation_runs (delegation_id, shadow, mode, request_payload, status, created_by)
                VALUES (%s, %s, %s, %s::jsonb, 'running', 'railway-worker')
                RETURNING id
                """,
                (delegation_id, shadow, mode, json.dumps(payload)),
            )
            row = cur.fetchone()
            conn.commit()
            return row["id"]


def _finalize_run(
    *,
    run_id: int,
    status: str,
    response_text: Optional[str] = None,
    response_summary: Optional[str] = None,
    cost_usd: Optional[float] = None,
    turn_count: Optional[int] = None,
    tools_used: Optional[List[str]] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dev_delegation_runs
                SET finished_at = NOW(),
                    duration_ms = %s,
                    status = %s,
                    response_text = %s,
                    response_summary = %s,
                    cost_usd = %s,
                    turn_count = %s,
                    tools_used = %s::jsonb,
                    error_message = %s
                WHERE id = %s
                """,
                (
                    duration_ms,
                    status,
                    response_text,
                    response_summary,
                    cost_usd,
                    turn_count,
                    json.dumps(tools_used) if tools_used is not None else None,
                    error_message,
                    run_id,
                ),
            )
            conn.commit()


def _close_delegation(*, delegation_id: int, response: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE delegations
                SET status='completed', response=%s, response_at=NOW()
                WHERE id=%s
                """,
                (response, delegation_id),
            )
            conn.commit()


def _emit_concluida_signal(*, delegation_id: int, summary: str, contact_id: Optional[int]) -> None:
    payload = {
        "delegation_id": delegation_id,
        "contact_id": contact_id,
        "summary": summary[:500],
    }
    signal_hash = f"deleg_dev_done:{delegation_id}"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals (signal_hash, tipo, urgencia, contexto, detector, status)
                VALUES (%s, 'delegacao_dev_concluida', %s, %s::jsonb, 'dev_delegation_pickup', 'open')
                ON CONFLICT (signal_hash) DO NOTHING
                """,
                (signal_hash, 5, json.dumps(payload)),
            )
            conn.commit()


async def _call_delegator(
    task: str, context: str, mode: str = "investigate", timeout: float = 350.0
) -> Dict[str, Any]:
    url = (os.getenv("CLAUDE_CODE_DELEGATOR_URL") or "").strip()
    secret = (os.getenv("WORKER_SECRET") or "").strip()
    if not url:
        return {"_error": "CLAUDE_CODE_DELEGATOR_URL ausente"}
    if not secret:
        logger.error("dev_delegation_pickup: WORKER_SECRET não configurado — call abortada (sem fallback)")
        return {"_error": "WORKER_SECRET ausente"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{url}/delegate",
                headers={
                    "x-delegator-secret": secret,
                    "content-type": "application/json",
                },
                json={
                    "task": task,
                    "context": context,
                    "mode": mode,
                    "requested_by": "tonha_cron",
                },
            )
            if resp.status_code != 200:
                return {"_error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
            return resp.json()
    except httpx.TimeoutException:
        return {"_error": "timeout (>350s)"}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


async def process_due() -> Dict[str, Any]:
    """Entry point chamado pelo APScheduler do worker. Returns summary dict."""
    shadow = _env_bool("DEV_DELEGATION_SHADOW", "1")
    cap_usd = _env_float("DEV_DELEGATION_MAX_USD_DAY", 5.0)
    # No worker nao tem Vercel 300s — podemos voltar pro default 3 mesmo
    # com shadow=off. Mas mantemos override por env pra flexibilidade.
    cap_cycle = _env_int("DEV_DELEGATION_MAX_PER_CYCLE", 3)
    mode = (os.getenv("DEV_DELEGATION_MODE") or "investigate").strip()

    now_brt = datetime.now(UTC).astimezone(SP_TZ)
    if not (9 <= now_brt.hour < 22):
        return {"skipped": "fora_janela_brt", "hour_brt": now_brt.hour}

    today_cost = _today_cost_usd()
    if today_cost >= cap_usd:
        logger.warning(
            f"dev_delegation_pickup: cap diario atingido ${today_cost:.2f}/${cap_usd:.2f}"
        )
        return {"skipped": "cap_diario", "today_cost_usd": today_cost, "cap_usd": cap_usd}

    rows = _fetch_open_dev(cap_cycle)
    if not rows:
        return {"processed": 0, "shadow": shadow, "today_cost_usd": today_cost}

    results: List[Dict[str, Any]] = []
    for row in rows:
        deleg_id = row["id"]
        try:
            await _process_one(row, shadow=shadow, mode=mode, results=results)
        except Exception as e:
            logger.exception(f"dev_delegation_pickup: row {deleg_id} crashed: {e}")
            results.append({"delegation_id": deleg_id, "status": "crashed", "error": str(e)[:300]})

    return {
        "processed": len(results),
        "shadow": shadow,
        "today_cost_usd": today_cost,
        "results": results,
    }


async def _process_one(
    row: Dict[str, Any], *, shadow: bool, mode: str, results: List[Dict[str, Any]]
) -> None:
    deleg_id = row["id"]
    task = row["task_summary"]
    full = row["task_full"]
    contact_id = row.get("contact_id")
    deadline = row.get("deadline")

    ctx_text = (
        f"delegation_id: {deleg_id}\n"
        f"deadline: {deadline}\n"
        f"contact_id: {contact_id}\n"
        f"\n# task_full\n{full or '(vazio)'}"
    )
    payload = {
        "delegation_id": deleg_id,
        "mode": mode,
        "task_summary": task,
        "task_full": full,
        "shadow": shadow,
    }

    run_id = _insert_run(delegation_id=deleg_id, shadow=shadow, mode=mode, payload=payload)

    if shadow:
        _finalize_run(
            run_id=run_id,
            status="skipped",
            response_summary="SHADOW: payload registrado, worker nao chamado",
        )
        results.append({"delegation_id": deleg_id, "status": "shadow_skipped"})
        return

    started = datetime.now(UTC)
    data = await _call_delegator(task=task, context=ctx_text, mode=mode)
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

    if "_error" in data:
        err = data["_error"]
        _finalize_run(
            run_id=run_id,
            status="error" if "timeout" not in err.lower() else "timeout",
            error_message=err,
            duration_ms=duration_ms,
        )
        results.append({"delegation_id": deleg_id, "status": "error", "error": err})
        return

    response_text = (data.get("result") or "").strip()
    cost_usd = data.get("cost_usd")
    turn_count = data.get("turn_count")
    tools_used = data.get("tools_used") or []
    summary = response_text[:300]

    _finalize_run(
        run_id=run_id,
        status="success",
        response_text=response_text,
        response_summary=summary,
        cost_usd=cost_usd,
        turn_count=turn_count,
        tools_used=tools_used,
        duration_ms=duration_ms,
    )
    _close_delegation(delegation_id=deleg_id, response=response_text)
    _emit_concluida_signal(delegation_id=deleg_id, summary=summary, contact_id=contact_id)
    results.append(
        {
            "delegation_id": deleg_id,
            "status": "success",
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
        }
    )
