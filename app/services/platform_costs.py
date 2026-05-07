"""Coleta + alerta de custos da plataforma (Cost Tracker - Bloco A).

Responsabilidades:
- auto_snapshot_month(period_start): preenche providers triviais (free tiers + LinkdAPI)
- get_active_alerts(months): retorna saltos suspeitos (>25% E >$5)
- check_and_notify_alerts(): get + WhatsApp se houver alerta novo

Providers automaticos (Bloco A):
- vercel, google, github: $0 (free tiers conhecidos)
- linkdapi: SUM(ABS(credits_delta) WHERE delta<0) / 120 (taxa $1=120 creditos)

Providers que continuam manuais (Bloco B vai automatizar via API):
- railway, neon, anthropic
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


def _upsert_cost(
    cursor, provider: str, period_start: date, amount_usd: float,
    notes: str, usage_metrics: Optional[dict] = None,
) -> dict:
    """UPSERT idempotente. Retorna row inserido/atualizado."""
    import json
    period_end = _last_day_of_month(period_start)
    metrics_json = json.dumps(usage_metrics) if usage_metrics else None
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
        RETURNING id, provider, period_start, amount_usd
        """,
        (provider, period_start, period_end, amount_usd, metrics_json, notes),
    )
    return dict(cursor.fetchone())


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
    Nao toca em providers manuais (railway/neon/anthropic) — esses
    continuam vindo via POST /api/admin/platform-costs.
    """
    period_start = period_start or _last_completed_month()
    if period_start.day != 1:
        period_start = period_start.replace(day=1)

    period_end = _last_day_of_month(period_start)
    period_label = period_start.strftime("%Y-%m")
    inserted = []

    with get_db() as conn:
        cursor = conn.cursor()

        # Free tiers conhecidos
        for prov in FREE_TIER_PROVIDERS:
            note_map = {
                "vercel": "Hobby plan free tier (auto)",
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

        conn.commit()

    return {
        "period": period_label,
        "auto_filled": inserted,
        "manual_pending": ["railway", "neon", "anthropic"],
    }


def get_active_alerts(months: int = 12) -> List[Dict]:
    """Retorna saltos suspeitos (>25% E >$5) nos ultimos N meses.

    Mesma logica do GET /api/admin/platform-costs — extraida pra reuso
    no dashboard pill e no cron de notificacao.
    """
    months = max(1, min(int(months), 36))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT provider, period_start, amount_usd
            FROM platform_costs
            WHERE period_start >= (DATE_TRUNC('month', CURRENT_DATE) - (%s || ' months')::interval)::date
            ORDER BY provider ASC, period_start ASC
            """,
            (months,),
        )
        rows = [dict(r) for r in cursor.fetchall()]

    by_provider: Dict[str, List[Dict]] = {}
    for r in rows:
        by_provider.setdefault(r["provider"], []).append({
            "period_start": r["period_start"],
            "amount_usd": float(r["amount_usd"] or 0),
        })

    alerts = []
    for prov, entries in by_provider.items():
        for i in range(1, len(entries)):
            prev_v = entries[i - 1]["amount_usd"]
            curr_v = entries[i]["amount_usd"]
            delta = curr_v - prev_v
            pct = (delta / prev_v * 100.0) if prev_v > 0 else 0.0
            if pct > ALERT_PCT_THRESHOLD and delta > ALERT_DELTA_USD_THRESHOLD:
                alerts.append({
                    "provider": prov,
                    "from_period": entries[i - 1]["period_start"].isoformat(),
                    "to_period": entries[i]["period_start"].isoformat(),
                    "from_usd": round(prev_v, 2),
                    "to_usd": round(curr_v, 2),
                    "delta_usd": round(delta, 2),
                    "delta_pct": round(pct, 1),
                })
    return alerts


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
    alerts = get_active_alerts(months=12)
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
