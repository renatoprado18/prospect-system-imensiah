"""Coleta + alerta de custos da plataforma (Cost Tracker - Blocos A+B).

Responsabilidades:
- auto_snapshot_month(period_start): preenche providers automatizaveis
- get_active_alerts(months): retorna saltos suspeitos (>25% E >$5)
- check_and_notify_alerts(): get + WhatsApp se houver alerta novo

Providers automaticos:
- vercel, google, github: $0 (free tiers conhecidos)
- linkdapi: ledger interno linkdapi_usage / 120 ($1=120 creditos)
- anthropic: /v1/organizations/cost_report (Bloco B, lag ~24h)
- railway: GraphQL usage + precos Hobby (Bloco B, projetos deletados subestimam)

Provider manual: neon (no API publica viavel; manual via POST).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from database import get_db

logger = logging.getLogger(__name__)

# Providers cobertos por auto-fill no Bloco A
FREE_TIER_PROVIDERS = ["vercel", "google", "github"]
LINKDAPI_USD_PER_CREDIT = Decimal("1") / Decimal("120")  # $10 = 1200 creditos

# Heuristica de alerta (motivada pelo incidente LibreChat)
ALERT_PCT_THRESHOLD = 25.0
ALERT_DELTA_USD_THRESHOLD = 5.0


def _last_completed_month(today: Optional[date] = None) -> date:
    """Primeiro dia do mes anterior. Ex: hoje=2026-05-07 -> 2026-04-01."""
    today = today or date.today()
    first_of_this = today.replace(day=1)
    return (first_of_this - timedelta(days=1)).replace(day=1)


def _last_day_of_month(d: date) -> date:
    if d.month == 12:
        return d.replace(day=31)
    next_first = d.replace(month=d.month + 1, day=1)
    return next_first - timedelta(days=1)


def _is_manual_row(cursor, provider: str, period_start: date) -> bool:
    """True se ja existe row e NAO foi auto-filled (i.e., POST manual).

    Auto rows tagueiam usage_metrics.auto_filled=true. Auto-fill respeita
    entrada manual pra nao sobrescrever numeros precisos do invoice (caso
    Railway: API subestima por nao ver projetos deletados)."""
    cursor.execute(
        """
        SELECT usage_metrics FROM platform_costs
        WHERE provider = %s AND period_start = %s::date
        """,
        (provider, period_start),
    )
    row = cursor.fetchone()
    if not row:
        return False
    metrics = row.get("usage_metrics") or {}
    return not metrics.get("auto_filled", False)


def _upsert_cost(
    cursor, provider: str, period_start: date, amount_usd: float,
    notes: str, usage_metrics: Optional[dict] = None,
) -> dict:
    """UPSERT idempotente. Retorna row inserido/atualizado.
    Marca auto_filled=true em usage_metrics pra distinguir de POSTs manuais."""
    import json
    period_end = _last_day_of_month(period_start)
    final_metrics = dict(usage_metrics) if usage_metrics else {}
    final_metrics["auto_filled"] = True
    metrics_json = json.dumps(final_metrics)
    cursor.execute(
        """
        INSERT INTO platform_costs
            (provider, period_start, period_end, amount_usd, usage_metrics, notes)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (provider, period_start) DO UPDATE SET
            period_end = EXCLUDED.period_end,
            amount_usd = EXCLUDED.amount_usd,
            usage_metrics = COALESCE(EXCLUDED.usage_metrics, '{}'::jsonb)
                            || (CASE WHEN platform_costs.usage_metrics ? 'alerted_at'
                                     THEN jsonb_build_object('alerted_at',
                                          platform_costs.usage_metrics->'alerted_at')
                                     ELSE '{}'::jsonb END),
            notes = EXCLUDED.notes,
            fetched_at = NOW()
        WHERE COALESCE((platform_costs.usage_metrics->>'auto_filled')::boolean, TRUE) = TRUE
        RETURNING id, provider, period_start, amount_usd
        """,
        (provider, period_start, period_end, amount_usd, metrics_json, notes),
    )
    row = cursor.fetchone()
    if row is None:
        # Manual row existe — nao toca; retorna o existente
        cursor.execute(
            """
            SELECT id, provider, period_start, amount_usd
            FROM platform_costs WHERE provider = %s AND period_start = %s::date
            """,
            (provider, period_start),
        )
        row = cursor.fetchone()
    return dict(row)


def _linkdapi_cost_for_period(cursor, period_start: date, period_end: date) -> Dict:
    """Calcula custo LinkdAPI consumido no periodo."""
    cursor.execute(
        """
        SELECT
            COALESCE(SUM(ABS(credits_delta)) FILTER (WHERE credits_delta < 0), 0) AS consumido,
            COALESCE(SUM(credits_delta) FILTER (WHERE credits_delta > 0), 0) AS refills,
            COUNT(*) FILTER (WHERE credits_delta < 0) AS calls
        FROM linkdapi_usage
        WHERE called_at >= %s AND called_at < %s::date + INTERVAL '1 day'
        """,
        (period_start, period_end),
    )
    row = cursor.fetchone() or {}
    consumido = int(row.get("consumido") or 0)
    refills = int(row.get("refills") or 0)
    calls = int(row.get("calls") or 0)
    cost_usd = float((Decimal(consumido) * LINKDAPI_USD_PER_CREDIT).quantize(Decimal("0.01")))
    return {
        "amount_usd": cost_usd,
        "metrics": {
            "credits_consumed": consumido,
            "credits_refilled": refills,
            "api_calls": calls,
            "rate_usd_per_credit": float(LINKDAPI_USD_PER_CREDIT),
        },
    }


def auto_snapshot_month(period_start: Optional[date] = None) -> Dict:
    """Preenche providers automatizaveis pra um mes especifico.

    Default: mes anterior completo. Idempotente via UPSERT.
    Cada integracao externa eh independente — falha de uma nao bloqueia outras.
    Provider manual unico: neon (sem API publica; via POST).
    """
    period_start = period_start or _last_completed_month()
    if period_start.day != 1:
        period_start = period_start.replace(day=1)

    period_end = _last_day_of_month(period_start)
    period_label = period_start.strftime("%Y-%m")
    inserted = []
    failed: List[Dict] = []

    with get_db() as conn:
        cursor = conn.cursor()

        # Free tiers conhecidos (google/github sao realmente $0;
        # vercel pode ser substituido pela integracao API abaixo)
        for prov in ["google", "github"]:
            note_map = {
                "google": "Free tier permanente (auto)",
                "github": "Free private repos / Actions free tier (auto)",
            }
            row = _upsert_cost(
                cursor, prov, period_start, 0.00, note_map[prov],
            )
            inserted.append({"provider": prov, "amount_usd": 0.00, "id": row["id"]})

        # LinkdAPI via ledger interno
        link_data = _linkdapi_cost_for_period(cursor, period_start, period_end)
        row = _upsert_cost(
            cursor, "linkdapi", period_start, link_data["amount_usd"],
            f"Auto via linkdapi_usage ledger — {link_data['metrics']['credits_consumed']} "
            f"creditos / {link_data['metrics']['api_calls']} calls",
            usage_metrics=link_data["metrics"],
        )
        inserted.append({
            "provider": "linkdapi",
            "amount_usd": link_data["amount_usd"],
            "id": row["id"],
            "metrics": link_data["metrics"],
        })

        # === Bloco B — integracoes externas ===
        # Cada provedor independente; falha de uma nao bloqueia outras.
        from services.platform_costs_integrations import (
            fetch_anthropic_cost, fetch_railway_cost, fetch_vercel_cost,
        )

        for prov, fetch_fn in [
            ("vercel", fetch_vercel_cost),
            ("anthropic", fetch_anthropic_cost),
            ("railway", fetch_railway_cost),
        ]:
            try:
                d = fetch_fn(period_start, period_end)
                row = _upsert_cost(
                    cursor, prov, period_start, d["amount_usd"],
                    d["notes"], usage_metrics=d.get("metrics"),
                )
                final_amount = float(row["amount_usd"])
                kept_manual = (abs(final_amount - d["amount_usd"]) > 0.005)
                entry = {
                    "provider": prov,
                    "amount_usd": final_amount,
                    "id": row["id"],
                    "metrics": d.get("metrics"),
                }
                if kept_manual:
                    entry["computed_usd"] = d["amount_usd"]
                    entry["kept_manual"] = True
                inserted.append(entry)
            except Exception as e:
                logger.warning(f"auto_snapshot {prov} falhou: {e}")
                failed.append({"provider": prov, "error": str(e)[:200]})

        conn.commit()

    return {
        "period": period_label,
        "auto_filled": inserted,
        "failed": failed,
        "manual_pending": ["neon"],
    }


def get_active_alerts(months: int = 12, include_acknowledged: bool = False) -> List[Dict]:
    """Retorna saltos suspeitos (>25% E >$5) nos ultimos N meses.

    Mesma logica do GET /api/admin/platform-costs — extraida pra reuso
    no dashboard pill e no cron de notificacao.

    include_acknowledged=False (default): filtra alertas que ja foram marcados
    como "OK ciente" pelo admin (stash em usage_metrics.acknowledged_at na
    row do to_period). Cada alerta retornado inclui campo acknowledged: bool.
    """
    months = max(1, min(int(months), 36))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT provider, period_start, amount_usd, usage_metrics
            FROM platform_costs
            WHERE period_start >= (DATE_TRUNC('month', CURRENT_DATE) - (%s || ' months')::interval)::date
            ORDER BY provider ASC, period_start ASC
            """,
            (months,),
        )
        rows = [dict(r) for r in cursor.fetchall()]

    by_provider: Dict[str, List[Dict]] = {}
    for r in rows:
        metrics = r.get("usage_metrics") or {}
        by_provider.setdefault(r["provider"], []).append({
            "period_start": r["period_start"],
            "amount_usd": float(r["amount_usd"] or 0),
            "acknowledged_at": metrics.get("acknowledged_at"),
        })

    alerts = []
    for prov, entries in by_provider.items():
        for i in range(1, len(entries)):
            prev_v = entries[i - 1]["amount_usd"]
            curr_v = entries[i]["amount_usd"]
            delta = curr_v - prev_v
            pct = (delta / prev_v * 100.0) if prev_v > 0 else 0.0
            if pct > ALERT_PCT_THRESHOLD and delta > ALERT_DELTA_USD_THRESHOLD:
                ack = bool(entries[i].get("acknowledged_at"))
                if ack and not include_acknowledged:
                    continue
                alerts.append({
                    "provider": prov,
                    "from_period": entries[i - 1]["period_start"].isoformat(),
                    "to_period": entries[i]["period_start"].isoformat(),
                    "from_usd": round(prev_v, 2),
                    "to_usd": round(curr_v, 2),
                    "delta_usd": round(delta, 2),
                    "delta_pct": round(pct, 1),
                    "acknowledged": ack,
                    "acknowledged_at": entries[i].get("acknowledged_at") if ack else None,
                })
    return alerts


def acknowledge_alert(provider: str, to_period: str, ack: bool = True) -> bool:
    """Marca/desmarca um alerta como reconhecido (stash em usage_metrics).

    Idempotente. Retorna True se a row existia, False caso contrario.
    Usa to_period (period_start do mes que disparou o alerta).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if ack:
            cursor.execute(
                """
                UPDATE platform_costs
                SET usage_metrics = COALESCE(usage_metrics, '{}'::jsonb)
                                    || jsonb_build_object('acknowledged_at', NOW()::text)
                WHERE provider = %s AND period_start = %s::date
                """,
                (provider, to_period),
            )
        else:
            cursor.execute(
                """
                UPDATE platform_costs
                SET usage_metrics = COALESCE(usage_metrics, '{}'::jsonb) - 'acknowledged_at'
                WHERE provider = %s AND period_start = %s::date
                """,
                (provider, to_period),
            )
        affected = cursor.rowcount
        conn.commit()
    return affected > 0


def _alert_signature(alert: Dict) -> str:
    """Hash estavel pra dedupe — provider+to_period (so notifica 1x por salto)."""
    return f"{alert['provider']}:{alert['to_period']}"


def _was_alerted_recently(cursor, provider: str, to_period: str) -> bool:
    """Checa se a row do to_period ja tem flag alerted_at nos ultimos 60 dias.
    Stash fica em usage_metrics->>'alerted_at' (no schema change)."""
    cursor.execute(
        """
        SELECT (usage_metrics->>'alerted_at')::timestamp AS alerted_at
        FROM platform_costs
        WHERE provider = %s AND period_start = %s::date
        """,
        (provider, to_period),
    )
    row = cursor.fetchone()
    if not row or not row.get("alerted_at"):
        return False
    alerted_at = row["alerted_at"]
    return alerted_at > datetime.now() - timedelta(days=60)


def _mark_alert_sent(cursor, provider: str, to_period: str) -> None:
    """Stamp usage_metrics.alerted_at na row que disparou o alerta."""
    cursor.execute(
        """
        UPDATE platform_costs
        SET usage_metrics = COALESCE(usage_metrics, '{}'::jsonb)
                            || jsonb_build_object('alerted_at', NOW()::text)
        WHERE provider = %s AND period_start = %s::date
        """,
        (provider, to_period),
    )


async def check_and_notify_alerts() -> Dict:
    """Verifica alerts ativos e manda WhatsApp se algum for novo.

    Dedupe por (provider, to_period) — mesmo salto nao alerta 2x em 60 dias.
    Mensagem agrupa todos os alertas novos em 1 unica notificacao.
    """
    # include_acknowledged=False: alertas reconhecidos pelo admin nao re-notificam
    alerts = get_active_alerts(months=12, include_acknowledged=False)
    if not alerts:
        return {"alerted": False, "alerts_total": 0, "alerts_new": 0}

    new_alerts = []
    with get_db() as conn:
        cursor = conn.cursor()
        for a in alerts:
            if not _was_alerted_recently(cursor, a["provider"], a["to_period"]):
                new_alerts.append(a)

    if not new_alerts:
        return {"alerted": False, "alerts_total": len(alerts), "alerts_new": 0,
                "reason": "all_alerts_already_sent"}

    # Compoe mensagem consolidada
    lines = ["💸 *Cost Tracker — aumento suspeito detectado*", ""]
    for a in new_alerts:
        lines.append(
            f"• *{a['provider']}*: ${a['from_usd']:.2f} → ${a['to_usd']:.2f} "
            f"(+{a['delta_pct']:.0f}%, +${a['delta_usd']:.2f})"
        )
        lines.append(f"  {a['from_period'][:7]} → {a['to_period'][:7]}")
    lines.append("")
    lines.append(f"Threshold: >{int(ALERT_PCT_THRESHOLD)}% E >${int(ALERT_DELTA_USD_THRESHOLD)}")
    lines.append("Detalhes: /api/admin/platform-costs")
    msg = "\n".join(lines)

    sent = False
    try:
        from services.intel_bot import send_intel_notification
        sent = await send_intel_notification(msg)
    except Exception as e:
        logger.warning(f"check_and_notify_alerts: send failed: {e}")

    # Stamp dedupe na row de cada alerta novo (mesmo se WhatsApp falhou —
    # evita spam infinito por falhas transitorias). Stash em usage_metrics.
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            for a in new_alerts:
                _mark_alert_sent(cursor, a["provider"], a["to_period"])
            conn.commit()
    except Exception:
        logger.exception("check_and_notify_alerts: falha ao gravar dedupe")

    return {
        "alerted": sent,
        "alerts_total": len(alerts),
        "alerts_new": len(new_alerts),
        "new_alert_signatures": [_alert_signature(a) for a in new_alerts],
    }
