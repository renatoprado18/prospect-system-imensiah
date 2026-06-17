"""
Consumer cron pra delegations(delegated_to='dev').

Tonha (brain) chama tool delegate(to='dev', task=...) -> INSERT em delegations
status='open'. Sem esse cron, a row fica orfa indefinidamente — Tonha re-detecta
delegacao_vencida e re-delegate pra dev de novo, infinito.

Esse modulo:
  1. SELECT delegations open dev (com filtros conservadores)
  2. POST pro worker claude-code-delegator (Railway, Node SDK headless)
  3. Grava response na delegations + cria signal pra Tonha surfacear
  4. Log telemetria em dev_delegation_runs (audit + cap de custo)

Guard-rails primeira versao (shadow default):
  - DEV_DELEGATION_SHADOW=1 (default) — so loga payload, nao chama worker
  - DEV_DELEGATION_MAX_USD_DAY=5 — cap diario
  - DEV_DELEGATION_MAX_PER_CYCLE=3 — cap por chamada do cron
  - Janela 9-22 BRT — nao roda madrugada
  - Skip task_summary que comeca com 'Follow-up #' — Tonha gera esses sozinha
    cobrando delegations dev stale. Picking them up =  ela cobra a propria
    cobranca, loop sem ganho.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from database import get_db
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)


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


def _in_window_brt(now_brt: datetime, start_hour: int = 9, end_hour: int = 22) -> bool:
    return start_hour <= now_brt.hour < end_hour


def _today_cost_usd() -> float:
    """Soma cost_usd de hoje (BRT day) — pra cap diario. Comparacao contra
    started_at TIMESTAMP (naive UTC) feita em UTC pra compatibilidade do
    storage, mas baseline e meianoite BRT pra evitar reset cedo as 21h BRT
    (P2 do code review 17/06)."""
    midnight_brt = to_brt(now_utc()).replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_brt.astimezone(now_utc().tzinfo).replace(tzinfo=None)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS total
            FROM dev_delegation_runs
            WHERE started_at >= %s AND status='success'
            """,
            (midnight_utc,),
        )
        row = cur.fetchone()
        return float(row["total"] or 0)


def _fetch_open_dev(limit: int) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.cursor()
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


def _insert_run(
    *, delegation_id: int, shadow: bool, mode: str, payload: Dict[str, Any]
) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dev_delegation_runs (delegation_id, shadow, mode, request_payload, status)
            VALUES (%s, %s, %s, %s::jsonb, 'running')
            RETURNING id
            """,
            (delegation_id, shadow, mode, json.dumps(payload)),
        )
        return cur.fetchone()["id"]


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
    with get_db() as conn:
        cur = conn.cursor()
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


def _close_delegation(*, delegation_id: int, response: str) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE delegations
            SET status='completed', response=%s, response_at=NOW()
            WHERE id=%s
            """,
            (response, delegation_id),
        )


def _emit_concluida_signal(*, delegation_id: int, summary: str, contact_id: Optional[int]) -> None:
    """Cria signal 'delegacao_dev_concluida' pra Tonha surfacear pro Renato."""
    payload = {
        "delegation_id": delegation_id,
        "contact_id": contact_id,
        "summary": summary[:500],
    }
    signal_hash = f"deleg_dev_done:{delegation_id}"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO signals (signal_hash, tipo, urgencia, contexto, detector, status)
            VALUES (%s, 'delegacao_dev_concluida', %s, %s::jsonb, 'dev_delegation_pickup', 'open')
            ON CONFLICT (signal_hash) DO NOTHING
            """,
            (signal_hash, 5, json.dumps(payload)),
        )


async def _call_delegator(
    task: str, context: str, mode: str = "investigate", timeout: float = 350.0
) -> Dict[str, Any]:
    url = (os.getenv("CLAUDE_CODE_DELEGATOR_URL") or "").strip()
    secret = (os.getenv("WORKER_SECRET") or "intel-audio-2026").strip()
    if not url:
        return {"_error": "CLAUDE_CODE_DELEGATOR_URL ausente"}
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
    """Entry point do cron. Returns summary dict pra cron_runs.result_json."""
    shadow = _env_bool("DEV_DELEGATION_SHADOW", "1")
    cap_usd = _env_float("DEV_DELEGATION_MAX_USD_DAY", 5.0)
    # Shadow runs sao rapidos (so loga). Real call ao delegator pode demorar
    # 30-350s. Vercel mata em 300s — 3 em serie passa do limite. Quando
    # shadow=off, cap_cycle baixa pra 1 (default override) pra caber.
    # TODO pre-cutover: migrar endpoint pro Railway worker (sem Vercel limit).
    cap_cycle_default = 3 if shadow else 1
    cap_cycle = _env_int("DEV_DELEGATION_MAX_PER_CYCLE", cap_cycle_default)
    mode = (os.getenv("DEV_DELEGATION_MODE") or "investigate").strip()

    now_brt = to_brt(now_utc())
    if not _in_window_brt(now_brt):
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

    run_id = _insert_run(
        delegation_id=deleg_id, shadow=shadow, mode=mode, payload=payload
    )

    if shadow:
        _finalize_run(
            run_id=run_id,
            status="skipped",
            response_summary="SHADOW: payload registrado, worker nao chamado",
        )
        results.append({"delegation_id": deleg_id, "status": "shadow_skipped"})
        return

    started = now_utc()
    data = await _call_delegator(task=task, context=ctx_text, mode=mode)
    duration_ms = int((now_utc() - started).total_seconds() * 1000)

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
    _emit_concluida_signal(
        delegation_id=deleg_id, summary=summary, contact_id=contact_id
    )
    results.append(
        {
            "delegation_id": deleg_id,
            "status": "success",
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
        }
    )
