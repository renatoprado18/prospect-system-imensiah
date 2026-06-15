"""
detector_editorial — substitui cos_editorial.

Sinais:
- editorial_post_sem_imagem    — Post scheduled em <24h sem imagem
- editorial_hot_take_velho     — Hot take em 'aprovado' ha +7d sem virar post
- editorial_pipeline_seca      — < 3 posts scheduled nos proximos 7d
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_editorial"


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    cur = conn.cursor()

    # ----- 1. Posts scheduled sem imagem em <24h -----
    try:
        with savepoint(conn, "post_sem_imagem"):
            cur.execute("""
                SELECT id, conteudo_adaptado, data_publicacao, canal, imagem_url, article_title
                FROM editorial_posts
                WHERE status IN ('scheduled', 'agendado')
                  AND data_publicacao BETWEEN NOW() AND NOW() + INTERVAL '24 hours'
                  AND (imagem_url IS NULL OR imagem_url = '')
                LIMIT 20
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("editorial_post_sem_imagem", r["id"])
                current_hashes.append(sh)
                horas = max(0, int((r["data_publicacao"] - datetime.utcnow()).total_seconds() / 3600))
                urg = max(5, min(9, 10 - horas // 4))
                ctx = {
                    "post_id": r["id"],
                    "titulo": (r["article_title"] or "")[:120],
                    "preview": (r["conteudo_adaptado"] or "")[:200],
                    "data_publicacao": r["data_publicacao"].isoformat() if r["data_publicacao"] else None,
                    "canal": r["canal"],
                    "horas_ate": horas,
                }
                _bump(res, emit_signal(conn, tipo="editorial_post_sem_imagem", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"post_sem_imagem: {str(e)[:200]}")

    # ----- 2. Hot takes aprovados sem virar post -----
    try:
        with savepoint(conn, "hot_take_velho"):
            cur.execute("""
                SELECT id, news_title, body, linkedin_post, created_at
                FROM hot_takes
                WHERE status IN ('aprovado', 'approved')
                  AND editorial_post_id IS NULL
                  AND created_at < NOW() - INTERVAL '7 days'
                ORDER BY created_at ASC
                LIMIT 10
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("editorial_hot_take_velho", r["id"])
                current_hashes.append(sh)
                dias = (datetime.utcnow() - r["created_at"]).days if r["created_at"] else 0
                urg = max(3, min(7, 3 + dias // 7))
                ctx = {
                    "hot_take_id": r["id"],
                    "news_title": (r["news_title"] or "")[:200],
                    "preview": (r["linkedin_post"] or r["body"] or "")[:300],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "dias_parado": dias,
                }
                _bump(res, emit_signal(conn, tipo="editorial_hot_take_velho", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"hot_take_velho: {str(e)[:200]}")

    # ----- 3. Pipeline seca: <3 posts scheduled nos prox 7d -----
    try:
        with savepoint(conn, "pipeline_seca"):
            cur.execute("""
                SELECT COUNT(*) AS n
                FROM editorial_posts
                WHERE status IN ('scheduled', 'agendado')
                  AND data_publicacao BETWEEN NOW() AND NOW() + INTERVAL '7 days'
            """)
            row = cur.fetchone()
            n = row["n"] if row else 0
            if n < 3:
                sh = make_signal_hash("editorial_pipeline_seca", "7d")
                current_hashes.append(sh)
                urg = 6 if n == 0 else (5 if n == 1 else 4)
                ctx = {"scheduled_count_7d": n, "target": 3}
                _bump(res, emit_signal(conn, tipo="editorial_pipeline_seca", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"pipeline_seca: {str(e)[:200]}")

    res.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)
    return res


def _bump(res: DetectorRun, result: str) -> None:
    if result == "emitted":
        res.emitted += 1
    elif result == "updated":
        res.updated += 1
    else:
        res.skipped += 1
