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


def _has_embedded_errors(result: Any) -> tuple:
    """
    Detecta se um cron retornou HTTP 200 mas com falha embutida no payload.

    Why: muitos crons capturam exceptions internamente e retornam um dict tipo
    {"errors": 2604, "contacts_updated": 0} ou {"status": "error", "error": ...}
    com HTTP 200. O decorator antes marcava como 'success' (so olhava se levantava
    exception). Resultado: /api/admin/cron-health mostrava verde pra crons que
    efetivamente falharam.

    Retorna (has_errors: bool, summary: str). Heuristica conservadora — so marca
    erro se houver evidencia clara no payload.
    """
    if not isinstance(result, dict):
        return False, ""

    # 1. errors: int > 0
    err_field = result.get("errors")
    if isinstance(err_field, int) and err_field > 0:
        return True, f"{err_field} errors reported"
    # 2. errors: lista nao-vazia
    if isinstance(err_field, list) and err_field:
        return True, f"{len(err_field)} errors reported"

    # 3. status == 'error' no top-level
    if result.get("status") == "error":
        err_msg = result.get("error") or "status=error"
        return True, str(err_msg)[:500]

    # 4. campo error truthy (mas evita string vazia)
    err_msg = result.get("error")
    if err_msg and isinstance(err_msg, (str, dict, list)):
        return True, str(err_msg)[:500]

    # 5. results: lista de sub-resultados onde algum tem status=error ou error truthy
    sub_results = result.get("results")
    if isinstance(sub_results, list) and sub_results:
        bad = [
            r for r in sub_results
            if isinstance(r, dict) and (
                r.get("status") == "error" or
                (r.get("error") and not isinstance(r.get("error"), bool))
            )
        ]
        if bad:
            # Resumo dos primeiros 3 sub-erros pra preservar contexto
            details = []
            for r in bad[:3]:
                acc = r.get("account") or r.get("name") or r.get("id") or "?"
                msg = str(r.get("error") or r.get("status") or "error")[:120]
                details.append(f"{acc}: {msg}")
            return True, f"{len(bad)}/{len(sub_results)} sub-errors: " + "; ".join(details)

    # 6. steps: dict de sub-resultados (padrao do daily-sync e crons aggregators).
    # Estrutura: {"steps": {"step_name": {"status": "error"|"timeout", "error": "..."}}}.
    # Sem isso, daily-sync com 1+ steps falhando ficava marcado 'success' porque
    # o top-level so tem started_at/completed_at/steps.
    steps = result.get("steps")
    if isinstance(steps, dict) and steps:
        bad_steps = [
            (name, sr) for name, sr in steps.items()
            if isinstance(sr, dict) and (
                sr.get("status") in ("error", "timeout") or
                (sr.get("error") and not isinstance(sr.get("error"), bool))
            )
        ]
        if bad_steps:
            details = []
            for name, sr in bad_steps[:3]:
                msg = str(sr.get("error") or sr.get("status") or "error")[:120]
                details.append(f"{name}: {msg}")
            return True, f"{len(bad_steps)}/{len(steps)} step-errors: " + "; ".join(details)

    return False, ""


# Sources validos pro header X-Cron-Source. Qualquer outro valor cai pra 'scheduled'.
#
# 25/07/26 — 'railway-worker' ACEITO (era descartado pro fallback 'scheduled').
# Motivo: o scheduler do worker Railway JA manda X-Cron-Source: railway-worker
# (workers/audio-transcriber/main.py:131), mas o valor nao estava na lista e
# degradava pra 'scheduled' — exatamente o mesmo carimbo de um curl ad-hoc (que
# nao manda header nenhum). Resultado: cron_runs ficou sem como distinguir run
# agendado de preview manual. Foi o que produziu a leitura de "duas series" de
# /api/cron/triage-inbox-scan em 25/07 (as dry_run=true das 12:26 e 14:10 BRT
# eram previews manuais da sessao Dev, nao um cron zumbi — nao ha segunda
# entrada registrada em _SCHEDULER_JOBS nem em cron_registry).
#
# Efeito: runs do worker passam a gravar 'railway-worker'; curl ad-hoc continua
# 'scheduled'. Pra jobs cujo unico agendador e o worker (inbox-zero-scan), a
# distincao vira exata. Vercel cron / GH Actions nao mandam header e seguem
# 'scheduled' (sem mudanca). Consumidores: o badge do dashboard so pinta
# 'manual'/'catch_up' (valor novo = sem badge) e a query de catch-up filtra
# 'catch_up' — nenhum quebra.
_VALID_TRIGGER_SOURCES = ("scheduled", "manual", "catch_up", "railway-worker")


def _insert_run(path: str, trigger_source: str = "scheduled") -> Optional[int]:
    """Insere row inicial com status='running'. Retorna id ou None em caso de falha."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cron_runs (path, status, started_at, trigger_source)
                VALUES (%s, 'running', NOW(), %s)
                RETURNING id
                """,
                (path, trigger_source),
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


def _discard_run(run_id: Optional[int]) -> None:
    """Apaga a row de uma invocacao que nunca chegou a executar o job.

    Existe por causa da ordem do decorator: `_insert_run` grava ANTES do handler,
    e a verificacao de credencial mora dentro dele. Sem isto, bater na porta sem
    a chave produz uma linha permanente numa tabela cujo unico proposito e medir
    se o job esta saudavel. Nunca levanta — telemetria nao pode derrubar request.
    """
    if run_id is None:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cron_runs WHERE id = %s", (run_id,))
            conn.commit()
    except Exception:
        logger.warning("cron_telemetry: discard run %s failed", run_id, exc_info=True)


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

        # X-Cron-Source: 'manual' = botao "Rodar" na UI, 'catch_up' = retry automatico,
        # default 'scheduled' (Vercel cron / GH Actions agendados ou ausencia do header).
        try:
            raw_source = (request.headers.get("x-cron-source") or "").strip().lower()
        except Exception:
            raw_source = ""
        trigger_source = raw_source if raw_source in _VALID_TRIGGER_SOURCES else "scheduled"

        run_id = _insert_run(path, trigger_source)
        started = time.time()

        try:
            result = await handler(request, *args, **kwargs)
            duration_ms = int((time.time() - started) * 1000)
            # Inspeciona o payload — crons que capturam exceptions internamente
            # podem retornar HTTP 200 com erros embutidos. Marca como 'error' nesses
            # casos pra que /api/admin/cron-health reflita o estado real.
            has_err, err_summary = _has_embedded_errors(result)
            _finalize_run(
                run_id,
                status=("error" if has_err else "success"),
                duration_ms=duration_ms,
                result=result,
                error_message=(err_summary if has_err else None),
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

            # 401/403 e falha do CHAMADOR, nao do job: a `verify_cron_auth` roda
            # DENTRO do handler, e o `_insert_run` acima ja gravou a linha antes
            # dela — entao toda requisicao nao-autenticada a /api/cron/* virava
            # `status='error'`, indistinguivel de job quebrado. O job nao chegou a
            # rodar: nada dele falhou, nao ha o que medir.
            #
            # Medido em 30/07: 4 linhas assim na base, DUAS produzidas por mim
            # tentando disparar um cron com a chave errada. Um scanner batendo na
            # porta encheria o `cron-health` de alarme sobre job saudavel.
            #
            # A linha e APAGADA em vez de receber um status proprio. Um status
            # novo ('unauthorized') parecia mais informativo, mas vazaria em tres
            # reguas de `_compute_cron_health` que nao o conhecem — e a pior
            # delas silenciosamente: o `error_streak` (main.py) percorre as runs
            # recentes e PARA no primeiro status que nao e 'error', entao uma
            # batida anonima no meio de uma sequencia de falhas reais zeraria o
            # streak e esconderia o job quebrado. Trocar falso-positivo por
            # falso-negativo e pior que o defeito original: alarme a toa incomoda,
            # alarme que nao dispara engana.
            #
            # O sinal nao se perde: vai pro log, que e onde se investiga acesso
            # indevido — e nao na tabela que mede saude de job.
            if http_status in (401, 403):
                logger.warning(
                    "cron_telemetry: requisicao NAO AUTENTICADA em %s (http=%s, source=%s) "
                    "— run descartada da telemetria (falha de chamador, nao do job)",
                    path, http_status, trigger_source,
                )
                _discard_run(run_id)
                raise

            _finalize_run(
                run_id,
                status="error",
                duration_ms=duration_ms,
                error_message=f"{type(e).__name__}: {e}",
                http_status=http_status,
            )
            raise

    return wrapper
