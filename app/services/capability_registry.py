"""F-E — Capability Registry v0 (base de dados da retro PDCA quinzenal).

Camada que INTERPRETA a telemetria crua por CAPACIDADE. Hoje custo/uso/valor
vivem espalhados em tabelas de eventos (signals, cron_runs, action_proposals,
tonia_llm_usage, platform_costs); nenhuma responde a pergunta que a retro faz:
"por capacidade, quanto custa, quanto e usada, e quando dispara alguem AGE ou
IGNORA?". Este service agrega essas 3 dimensoes numa lista uniforme.

4 tipos de capacidade:
  - detector       (signals)          — value = resolved vs expired/dismissed
  - cron           (cron_runs)        — value = success vs error/timeout (=confiabilidade)
  - llm_function   (tonia_llm_usage
                    + platform_costs) — custo por funcao; sem proxy de valor
  - proposal_source(action_proposals) — value = executed/resolved vs rejected/dismissed/expired

Read-only sobre toda a telemetria (nenhuma escrita aqui — quem persiste o
snapshot e o cron em main.py via persist_snapshot). Cada secao degrada gracioso:
fonte ausente ou vazia -> secao vazia com nota, nunca crash. Onde nao ha proxy
de valor mensuravel, value_ratio = None e o motivo fica em `extra` (honestidade
> metrica-teatro — o Renato odeia numero inventado).

Convencao TZ: tudo UTC tz-aware via services.tz. Colunas do DB sao TIMESTAMP
naive (UTC por convencao); as janelas usam intervalos relativos no SQL (NOW() -
interval) pra nao depender de comparar naive vs aware no Python.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import now_utc

logger = logging.getLogger(__name__)


def _clamp_days(days: int) -> int:
    """Janela sensata: 1..90 dias."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 14
    return max(1, min(days, 90))


def _ratio(acted: int, ignored: int) -> Optional[float]:
    """value_ratio = acted / (acted + ignored). None quando o denominador e 0
    (nenhum sinal RESOLVIDO/IGNORADO ainda — tudo pendente/aberto): sem base pra
    medir, devolvemos None em vez de 0.0 (que mentiria dizendo "valor zero")."""
    denom = acted + ignored
    if denom <= 0:
        return None
    return round(acted / denom, 4)


# ---------------------------------------------------------------------------
# 1) DETECTORES — via signals
# ---------------------------------------------------------------------------
def _detectors(cursor, days: int) -> List[Dict[str, Any]]:
    """Valor por DETECTOR: emitido=volume; resolved=acionado;
    expired+dismissed=ruido/ignorado. value_ratio = resolved/(resolved+ruido).

    Sem custo isolado por detector (rodam dentro do cron detectors-run) ->
    cost_usd = None."""
    cursor.execute(
        """
        SELECT detector,
               COUNT(*)                                              AS invocations,
               COUNT(*) FILTER (WHERE status = 'resolved')           AS resolved,
               COUNT(*) FILTER (WHERE status = 'expired')            AS expired,
               COUNT(*) FILTER (WHERE status = 'dismissed')          AS dismissed,
               COUNT(*) FILTER (WHERE status = 'open')               AS still_open,
               ROUND(AVG(urgencia)::numeric, 1)                      AS avg_urgencia
        FROM signals
        WHERE criado_em > NOW() - (%s || ' days')::interval
        GROUP BY detector
        ORDER BY invocations DESC
        """,
        (days,),
    )
    out: List[Dict[str, Any]] = []
    for r in cursor.fetchall():
        r = dict(r)
        acted = int(r["resolved"])
        ignored = int(r["expired"]) + int(r["dismissed"])
        ratio = _ratio(acted, ignored)
        extra: Dict[str, Any] = {
            "still_open": int(r["still_open"]),
            "resolved": acted,
            "expired": int(r["expired"]),
            "dismissed": int(r["dismissed"]),
            "avg_urgencia": float(r["avg_urgencia"]) if r["avg_urgencia"] is not None else None,
            "cost_note": "detector nao tem custo isolado (roda dentro de detectors-run)",
        }
        if ratio is None:
            extra["value_note"] = (
                "sem sinal de valor ainda — todos open/pendentes na janela"
            )
        out.append({
            "capability_key": f"detector:{r['detector']}",
            "capability_type": "detector",
            "cost_usd": None,
            "invocations": int(r["invocations"]),
            "value_acted": acted,
            "value_ignored": ignored,
            "value_ratio": ratio,
            "extra": extra,
        })
    return out


# ---------------------------------------------------------------------------
# 2) CRONS — via cron_runs
# ---------------------------------------------------------------------------
def _crons(cursor, days: int) -> List[Dict[str, Any]]:
    """Uso + CONFIABILIDADE por cron. Aqui "valor" nao e acionado-vs-ignorado
    (crons nao tem esse proxy) — usamos success-vs-falha como proxy honesto:
    value_acted = runs success, value_ignored = error+timeout, value_ratio =
    taxa de sucesso. `extra.value_note` deixa claro que e CONFIABILIDADE, nao
    valor-de-negocio (um cron 100% confiavel pode ser inutil — a retro decide
    isso cruzando com o resto)."""
    cursor.execute(
        """
        SELECT path,
               COUNT(*)                                        AS invocations,
               COUNT(*) FILTER (WHERE status = 'success')      AS success,
               COUNT(*) FILTER (WHERE status = 'error')        AS errors,
               COUNT(*) FILTER (WHERE status = 'timeout')      AS timeouts,
               COUNT(*) FILTER (WHERE status = 'running')      AS running,
               ROUND(AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL))  AS avg_ms,
               MAX(started_at)                                 AS last_run
        FROM cron_runs
        WHERE started_at > NOW() - (%s || ' days')::interval
        GROUP BY path
        ORDER BY invocations DESC
        """,
        (days,),
    )
    out: List[Dict[str, Any]] = []
    for r in cursor.fetchall():
        r = dict(r)
        acted = int(r["success"])
        ignored = int(r["errors"]) + int(r["timeouts"])
        ratio = _ratio(acted, ignored)
        out.append({
            "capability_key": f"cron:{r['path']}",
            "capability_type": "cron",
            "cost_usd": None,
            "invocations": int(r["invocations"]),
            "value_acted": acted,
            "value_ignored": ignored,
            "value_ratio": ratio,
            "extra": {
                "success": acted,
                "errors": int(r["errors"]),
                "timeouts": int(r["timeouts"]),
                "running": int(r["running"]),
                "avg_duration_ms": int(r["avg_ms"]) if r["avg_ms"] is not None else None,
                "last_run": r["last_run"].isoformat() if r["last_run"] else None,
                "value_note": "value_ratio = taxa de sucesso (CONFIABILIDADE, "
                              "nao valor-de-negocio)",
                "cost_note": "sem custo por-cron rastreado (compute Railway/Vercel "
                             "e fixo mensal, ver platform_costs)",
            },
        })
    return out


# ---------------------------------------------------------------------------
# 3) FUNCOES LLM — via tonia_llm_usage (+ platform_costs anthropic)
# ---------------------------------------------------------------------------
def _llm_functions(cursor, days: int) -> List[Dict[str, Any]]:
    """Custo LLM por FUNCAO (endpoint em tonia_llm_usage, ex 'briefing.generate').
    Sem proxy de valor por-chamada -> value_ratio = None (explicado em extra).

    Alem das funcoes da Tonia, adiciona 1 capacidade agregada
    `llm:anthropic-platform` a partir de platform_costs (anthropic, ultimo
    periodo) — o custo LLM do INTEL que NAO passa pela Tonia (briefings antigos,
    OCR, Smart Update etc. ainda nao instrumentados por-funcao). Cobre o gap ate
    cada call ter seu proprio registro em tonia_llm_usage."""
    out: List[Dict[str, Any]] = []

    # 3a) funcoes instrumentadas (Tonia)
    try:
        cursor.execute(
            """
            SELECT endpoint,
                   COUNT(*)                 AS invocations,
                   SUM(cost_usd)            AS cost_usd,
                   SUM(input_tokens)        AS input_tokens,
                   SUM(output_tokens)       AS output_tokens,
                   SUM(cache_read_tokens)   AS cache_read_tokens,
                   SUM(cache_creation_tokens) AS cache_creation_tokens
            FROM tonia_llm_usage
            WHERE ts > NOW() - (%s || ' days')::interval
            GROUP BY endpoint
            ORDER BY cost_usd DESC
            """,
            (days,),
        )
        for r in cursor.fetchall():
            r = dict(r)
            out.append({
                "capability_key": f"llm:{r['endpoint']}",
                "capability_type": "llm_function",
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else 0.0,
                "invocations": int(r["invocations"]),
                "value_acted": None,
                "value_ignored": None,
                "value_ratio": None,
                "extra": {
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                    "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                    "source": "tonia_llm_usage",
                    "value_note": "sem proxy de valor por-chamada — funcao LLM nao "
                                  "'resolve/ignora'; o valor mede-se no consumidor "
                                  "(o detector/proposta que a chamada alimenta)",
                },
            })
    except Exception as e:  # tabela ausente no alvo -> segue sem essa fatia
        cursor.connection.rollback()
        logger.warning("capability_registry: tonia_llm_usage indisponivel: %s", e)

    # 3b) custo LLM agregado nao-instrumentado (platform_costs anthropic)
    try:
        cursor.execute(
            """
            SELECT amount_usd, period_start, period_end, usage_metrics
            FROM platform_costs
            WHERE provider = 'anthropic'
            ORDER BY period_start DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if row:
            row = dict(row)
            tonia_cost = sum(
                c["cost_usd"] for c in out if c.get("cost_usd")
            )
            platform_month_cost = float(row["amount_usd"]) if row["amount_usd"] is not None else 0.0

            # PRORATA: platform_costs e MENSAL (period_start=dia 1). As demais
            # capacidades respeitam a janela `days`; comparar o custo do mes
            # inteiro contra a soma da janela mistura escalas e inventa um "gap
            # cego" (ex: $37/mes - $14.85/14d = $22 fantasma). Prorateamos o
            # custo mensal pra janela, por dia DECORRIDO do periodo (o mes
            # corrente ainda nao acabou -> nao usar os 31 dias nominais).
            p_start = row["period_start"]
            p_end = row["period_end"]
            today = now_utc().date()
            elapsed_days = 1
            if p_start:
                ref_end = min(today, p_end) if p_end else today
                elapsed_days = max(1, (ref_end - p_start).days + 1)
            per_day = platform_month_cost / elapsed_days
            window_days = min(days, elapsed_days)
            platform_window_cost = round(per_day * window_days, 6)
            uninstrumented = round(max(0.0, platform_window_cost - tonia_cost), 6)
            coverage = (
                round(min(tonia_cost / platform_window_cost, 1.0), 4)
                if platform_window_cost > 0 else None
            )
            out.append({
                "capability_key": "llm:anthropic-platform",
                "capability_type": "llm_function",
                # custo prorateado A JANELA (comparavel c/ as funcoes acima),
                # nao o mes inteiro — o mensal bruto fica em extra.
                "cost_usd": platform_window_cost,
                "invocations": None,  # platform_costs nao conta chamadas
                "value_acted": None,
                "value_ignored": None,
                "value_ratio": None,
                "extra": {
                    "source": "platform_costs (provider=anthropic, ultimo periodo, prorateado a janela)",
                    "period_start": p_start.isoformat() if p_start else None,
                    "period_end": p_end.isoformat() if p_end else None,
                    "platform_month_usd": round(platform_month_cost, 6),
                    "platform_prorated_window_usd": platform_window_cost,
                    "period_elapsed_days": elapsed_days,
                    "prorated_over_days": window_days,
                    "tonia_instrumented_usd": round(tonia_cost, 6),
                    "uninstrumented_usd_estimate": uninstrumented,
                    "coverage_pct": coverage,
                    "value_note": "custo LLM total da conta Anthropic, PRORATEADO da "
                                  "linha mensal de platform_costs pra janela `days` "
                                  "(por dia decorrido do periodo) — assim compara com "
                                  "as funcoes acima na mesma escala. `platform_month_usd` "
                                  "= mensal bruto. RESSALVA: a instrumentacao por-funcao "
                                  "e recente (jul/2026); janelas que cruzam o periodo "
                                  "pre-instrumentacao SUBESTIMAM coverage_pct (o custo "
                                  "existiu, o registro por-funcao nao). Ler coverage numa "
                                  "janela curta pos-deploy pra medir a cobertura real.",
                },
            })
    except Exception as e:
        cursor.connection.rollback()
        logger.warning("capability_registry: platform_costs indisponivel: %s", e)

    return out


# ---------------------------------------------------------------------------
# 4) FONTES DE PROPOSTA — via action_proposals
# ---------------------------------------------------------------------------
def _proposal_sources(cursor, days: int) -> List[Dict[str, Any]]:
    """Valor por FONTE de proposta (action_type). executed+resolved = valor;
    rejected+dismissed+expired+cancelled = ruido. Pending nao conta em nenhum
    lado (ainda indeciso). value_ratio = valor/(valor+ruido); None se so pending."""
    cursor.execute(
        """
        SELECT action_type,
               COUNT(*)                                          AS invocations,
               COUNT(*) FILTER (WHERE status IN ('executed','resolved')) AS acted,
               COUNT(*) FILTER (WHERE status IN ('rejected','dismissed','expired','cancelled')) AS ignored,
               COUNT(*) FILTER (WHERE status = 'pending')        AS pending
        FROM action_proposals
        WHERE criado_em > NOW() - (%s || ' days')::interval
        GROUP BY action_type
        ORDER BY invocations DESC
        """,
        (days,),
    )
    out: List[Dict[str, Any]] = []
    for r in cursor.fetchall():
        r = dict(r)
        acted = int(r["acted"])
        ignored = int(r["ignored"])
        ratio = _ratio(acted, ignored)
        extra: Dict[str, Any] = {
            "pending": int(r["pending"]),
            "acted": acted,
            "ignored": ignored,
            "cost_note": "custo LLM da geracao esta agregado em llm:anthropic-platform",
        }
        if ratio is None:
            extra["value_note"] = "todas pending na janela — sem base de valor ainda"
        out.append({
            "capability_key": f"proposal:{r['action_type']}",
            "capability_type": "proposal_source",
            "cost_usd": None,
            "invocations": int(r["invocations"]),
            "value_acted": acted,
            "value_ignored": ignored,
            "value_ratio": ratio,
            "extra": extra,
        })
    return out


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------
_SECTION_BUILDERS = {
    "detectors": _detectors,
    "crons": _crons,
    "llm_functions": _llm_functions,
    "proposal_sources": _proposal_sources,
}


def build_registry(days: int = 14) -> Dict[str, Any]:
    """Rollup on-demand das 4 secoes. Cada secao degrada gracioso (se a fonte
    falhar, a secao vem vazia com `note` em vez de derrubar o resto).

    Retorna:
      {
        "generated_at": ISO-8601 UTC,
        "window_days": N,
        "sections": {detectors|crons|llm_functions|proposal_sources: [cap,...]},
        "counts": {section: n},
        "totals": {cost_usd, capabilities},
      }
    onde cada `cap` = {capability_key, capability_type, cost_usd, invocations,
    value_acted, value_ignored, value_ratio, extra}.
    """
    days = _clamp_days(days)
    sections: Dict[str, Any] = {}
    notes: Dict[str, str] = {}

    with get_db() as conn:
        cursor = conn.cursor()
        for name, builder in _SECTION_BUILDERS.items():
            try:
                sections[name] = builder(cursor, days)
            except Exception as e:  # fonte ausente/vazia -> secao vazia, nunca crash
                conn.rollback()
                logger.warning("capability_registry: secao %s falhou: %s", name, e)
                sections[name] = []
                notes[name] = f"indisponivel: {e.__class__.__name__}"

    total_cost = 0.0
    total_caps = 0
    for caps in sections.values():
        for c in caps:
            total_caps += 1
            if c.get("cost_usd"):
                total_cost += c["cost_usd"]

    result: Dict[str, Any] = {
        "generated_at": now_utc().isoformat(),
        "window_days": days,
        "sections": sections,
        "counts": {k: len(v) for k, v in sections.items()},
        "totals": {
            "cost_usd": round(total_cost, 6),
            "capabilities": total_caps,
        },
    }
    if notes:
        result["notes"] = notes
    return result


def flatten_registry(registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Achata as secoes numa lista de capacidades — 1 dict por capacidade,
    consumido pelo persistidor de snapshot (1 linha por capacidade por dia)."""
    flat: List[Dict[str, Any]] = []
    for caps in registry.get("sections", {}).values():
        flat.extend(caps)
    return flat


def persist_snapshot(days: int = 14, snapshot_date=None) -> Dict[str, Any]:
    """Constroi o registry e persiste 1 linha por capacidade em
    capability_snapshots, para a data de hoje (UTC). Idempotente por dia: re-run
    do mesmo dia faz UPSERT (ON CONFLICT snapshot_date, capability_key).

    Por que snapshot alem do rollup on-demand: custo/uso ja sao eventos
    timestampados (serie existe no cron_runs/tonia_llm_usage), MAS os proxies
    point-in-time (nº pending hoje, ratio aberto) NAO viram serie sozinhos. O
    snapshot diario os acumula pra retro ver TENDENCIA. Comeca a acumular ja."""
    import json
    from datetime import date as _date

    registry = build_registry(days)
    caps = flatten_registry(registry)
    snap_date = snapshot_date or now_utc().date()
    if isinstance(snap_date, str):
        snap_date = _date.fromisoformat(snap_date)

    upserted = 0
    with get_db() as conn:
        cursor = conn.cursor()
        for c in caps:
            cursor.execute(
                """
                INSERT INTO capability_snapshots
                    (snapshot_date, capability_key, capability_type, window_days,
                     cost_usd, invocations, value_acted, value_ignored, value_ratio, extra)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (snapshot_date, capability_key) DO UPDATE SET
                    capability_type = EXCLUDED.capability_type,
                    window_days     = EXCLUDED.window_days,
                    cost_usd        = EXCLUDED.cost_usd,
                    invocations     = EXCLUDED.invocations,
                    value_acted     = EXCLUDED.value_acted,
                    value_ignored   = EXCLUDED.value_ignored,
                    value_ratio     = EXCLUDED.value_ratio,
                    extra           = EXCLUDED.extra,
                    created_at      = NOW()
                """,
                (
                    snap_date,
                    c["capability_key"],
                    c["capability_type"],
                    registry["window_days"],
                    c.get("cost_usd"),
                    c.get("invocations"),
                    c.get("value_acted"),
                    c.get("value_ignored"),
                    c.get("value_ratio"),
                    json.dumps(c.get("extra") or {}),
                ),
            )
            upserted += 1
        conn.commit()

    return {
        "snapshot_date": snap_date.isoformat(),
        "window_days": registry["window_days"],
        "capabilities_persisted": upserted,
        "counts": registry["counts"],
        "total_cost_usd": registry["totals"]["cost_usd"],
    }


def snapshot_trend(days_back: int = 30, limit_per_cap: int = 10) -> Dict[str, Any]:
    """Tendencia: os ultimos snapshots por capacidade (pra retro ver a serie
    point-in-time acumulada). Degrada gracioso se a tabela ainda nao existe."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT capability_key, snapshot_date, capability_type,
                       cost_usd, invocations, value_acted, value_ignored, value_ratio
                FROM capability_snapshots
                WHERE snapshot_date > (CURRENT_DATE - %s)
                ORDER BY capability_key, snapshot_date DESC
                """,
                (days_back,),
            )
            for r in cursor.fetchall():
                r = dict(r)
                key = r["capability_key"]
                if key not in out:
                    out[key] = []
                if len(out[key]) >= limit_per_cap:
                    continue
                out[key].append({
                    "snapshot_date": r["snapshot_date"].isoformat(),
                    "capability_type": r["capability_type"],
                    "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                    "invocations": r["invocations"],
                    "value_acted": r["value_acted"],
                    "value_ignored": r["value_ignored"],
                    "value_ratio": float(r["value_ratio"]) if r["value_ratio"] is not None else None,
                })
        return {"available": True, "capabilities": out}
    except Exception as e:
        return {"available": False, "note": f"indisponivel: {e.__class__.__name__}",
                "capabilities": {}}
