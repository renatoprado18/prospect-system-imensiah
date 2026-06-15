"""
detector_financial — substitui cos_financial.

Sinais:
- financial_custo_acima_baseline  — provider com custo do mes >130% media 3 meses
- financial_alerta_anthropic      — Anthropic >= 80% do limite spend (config externo)
- financial_alerta_railway        — Railway >= 80% limite mensal
"""
from __future__ import annotations

import os
from typing import List

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_financial"

# Limites configuraveis via env (USD)
ANTHROPIC_LIMIT = float(os.getenv("ANTHROPIC_MONTHLY_LIMIT", "200").strip())
RAILWAY_LIMIT = float(os.getenv("RAILWAY_MONTHLY_LIMIT", "20").strip())


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    cur = conn.cursor()

    # ----- 1. Custo do mes vs baseline 3 meses -----
    try:
        with savepoint(conn, "baseline"):
            cur.execute("""
                WITH mes_atual AS (
                    SELECT provider, SUM(amount_usd) AS atual
                    FROM platform_costs
                    WHERE period_start >= DATE_TRUNC('month', CURRENT_DATE)
                    GROUP BY provider
                ),
                baseline AS (
                    SELECT provider, AVG(monthly) AS media
                    FROM (
                        SELECT provider, DATE_TRUNC('month', period_start) AS mes, SUM(amount_usd) AS monthly
                        FROM platform_costs
                        WHERE period_start >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '3 months')
                          AND period_start <  DATE_TRUNC('month', CURRENT_DATE)
                        GROUP BY provider, DATE_TRUNC('month', period_start)
                    ) sub
                    GROUP BY provider
                )
                SELECT m.provider, m.atual, b.media
                FROM mes_atual m
                JOIN baseline b ON b.provider = m.provider
                WHERE b.media > 1
                  AND m.atual > b.media * 1.30
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("financial_custo_acima_baseline", r["provider"], "mes_atual")
                current_hashes.append(sh)
                atual = float(r["atual"] or 0)
                media = float(r["media"] or 0)
                pct = ((atual / media) - 1) * 100 if media else 0
                urg = 5 if pct < 50 else (7 if pct < 100 else 9)
                ctx = {
                    "provider": r["provider"],
                    "custo_mes_atual_usd": round(atual, 2),
                    "media_3meses_usd": round(media, 2),
                    "aumento_pct": round(pct, 1),
                }
                _bump(res, emit_signal(conn, tipo="financial_custo_acima_baseline", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"baseline: {str(e)[:200]}")

    # ----- 2. Anthropic >= 80% limite -----
    try:
        with savepoint(conn, "anthropic"):
            cur.execute("""
                SELECT SUM(amount_usd) AS total
                FROM platform_costs
                WHERE provider IN ('Anthropic', 'anthropic')
                  AND period_start >= DATE_TRUNC('month', CURRENT_DATE)
            """)
            row = cur.fetchone()
            total = float(row["total"] or 0) if row else 0
            if total >= ANTHROPIC_LIMIT * 0.80:
                sh = make_signal_hash("financial_alerta_anthropic", "mes_atual")
                current_hashes.append(sh)
                pct = (total / ANTHROPIC_LIMIT) * 100
                urg = 8 if pct < 95 else 10
                ctx = {
                    "provider": "Anthropic",
                    "gasto_usd": round(total, 2),
                    "limite_usd": ANTHROPIC_LIMIT,
                    "pct_consumido": round(pct, 1),
                }
                _bump(res, emit_signal(conn, tipo="financial_alerta_anthropic", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"anthropic: {str(e)[:200]}")

    # ----- 3. Railway >= 80% limite -----
    try:
        with savepoint(conn, "railway"):
            cur.execute("""
                SELECT SUM(amount_usd) AS total
                FROM platform_costs
                WHERE provider IN ('Railway', 'railway')
                  AND period_start >= DATE_TRUNC('month', CURRENT_DATE)
            """)
            row = cur.fetchone()
            total = float(row["total"] or 0) if row else 0
            if total >= RAILWAY_LIMIT * 0.80:
                sh = make_signal_hash("financial_alerta_railway", "mes_atual")
                current_hashes.append(sh)
                pct = (total / RAILWAY_LIMIT) * 100
                urg = 7 if pct < 95 else 9
                ctx = {
                    "provider": "Railway",
                    "gasto_usd": round(total, 2),
                    "limite_usd": RAILWAY_LIMIT,
                    "pct_consumido": round(pct, 1),
                }
                _bump(res, emit_signal(conn, tipo="financial_alerta_railway", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"railway: {str(e)[:200]}")

    res.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)
    return res


def _bump(res: DetectorRun, result: str) -> None:
    if result == "emitted":
        res.emitted += 1
    elif result == "updated":
        res.updated += 1
    else:
        res.skipped += 1
