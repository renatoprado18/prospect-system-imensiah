"""
detector_delegacoes — sec 4.5 do ARCHITECTURE_REBUILD.

Sinais:
- delegacao_vencida              — delegation.status='open' + deadline < hoje
- delegacao_sem_followup         — open + criada ha +N dias sem followup (varia por delegated_to)
- delegacao_andressa_sem_resposta — andressa em particular, ha +24h sem resposta
"""
from __future__ import annotations

from datetime import date
from typing import List

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_delegacoes"

# Janelas de followup por delegate (em dias)
FOLLOWUP_WINDOWS = {
    "andressa": 1,             # diaria
    "joao_piccino": 3,
    "priscila_contadora": 3,
    "dev": 1,                  # Claude Code rapido
    "evaluator": 1,
    "collector": 7,
}


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    cur = conn.cursor()

    # ----- 1. Vencidas -----
    try:
        with savepoint(conn, "vencida"):
            cur.execute("""
                SELECT id, delegated_to, contact_id, task_summary, deadline,
                       criado_em, last_followup_at, followup_count
                FROM delegations
                WHERE status = 'open'
                  AND deadline IS NOT NULL
                  AND deadline < CURRENT_DATE
                ORDER BY deadline ASC
                LIMIT 30
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("delegacao_vencida", r["id"])
                current_hashes.append(sh)
                dias_atraso = (date.today() - r["deadline"]).days
                urg = max(5, min(9, 5 + dias_atraso // 2))
                ctx = {
                    "delegation_id": r["id"],
                    "delegated_to": r["delegated_to"],
                    "task_summary": r["task_summary"],
                    "deadline": r["deadline"].isoformat(),
                    "dias_atraso": dias_atraso,
                    "followup_count": r["followup_count"],
                    "contact_id": r["contact_id"],
                }
                _bump(res, emit_signal(conn, tipo="delegacao_vencida", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"vencida: {str(e)[:200]}")

    # ----- 2. Sem followup dentro da janela -----
    try:
        with savepoint(conn, "sem_followup"):
            cur.execute("""
                SELECT id, delegated_to, contact_id, task_summary, deadline,
                       criado_em, last_followup_at, followup_count
                FROM delegations
                WHERE status = 'open'
                ORDER BY criado_em ASC
                LIMIT 100
            """)
            for r in cur.fetchall():
                window_days = FOLLOWUP_WINDOWS.get(r["delegated_to"], 3)
                ref = r["last_followup_at"] or r["criado_em"]
                if not ref:
                    continue
                dias_desde = (date.today() - ref.date()).days
                if dias_desde < window_days:
                    continue
                sh = make_signal_hash("delegacao_sem_followup", r["id"])
                current_hashes.append(sh)
                urg = max(3, min(6, 3 + dias_desde // window_days))
                ctx = {
                    "delegation_id": r["id"],
                    "delegated_to": r["delegated_to"],
                    "task_summary": r["task_summary"],
                    "criado_em": r["criado_em"].isoformat() if r["criado_em"] else None,
                    "last_followup_at": r["last_followup_at"].isoformat() if r["last_followup_at"] else None,
                    "dias_desde_ultimo_contato": dias_desde,
                    "janela_dias": window_days,
                    "followup_count": r["followup_count"],
                    "contact_id": r["contact_id"],
                }
                _bump(res, emit_signal(conn, tipo="delegacao_sem_followup", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"sem_followup: {str(e)[:200]}")

    res.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)
    return res


def _bump(res: DetectorRun, result: str) -> None:
    if result == "emitted":
        res.emitted += 1
    elif result == "updated":
        res.updated += 1
    else:
        res.skipped += 1
