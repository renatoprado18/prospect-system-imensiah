"""
detector_governanca_pessoal — sec 4.6 do ARCHITECTURE_REBUILD.

Olha proativamente pra carteira de projetos do Renato. Sinais que ele
nunca pediria, mas Tonha (CoS) precisa puxar.

Sinais:
- gov_projeto_drift           — projeto ativo sem milestone proximo + sem task pending
- gov_task_orfa               — tasks ai_generated=TRUE sem project_id ha +14d
- gov_projetos_duplicados     — 2+ projects com nome similar (>0.7 sim)
- gov_carteira_overload       — >5 projects 'ativo' com prioridade<=3
"""
from __future__ import annotations

from typing import List

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_governanca_pessoal"


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    cur = conn.cursor()

    # ----- 1. Projeto ativo c/ drift (sem milestone proximo + sem task pending) -----
    try:
        with savepoint(conn, "projeto_drift"):
            cur.execute("""
                SELECT p.id, p.nome, p.prioridade, p.atualizado_em
                FROM projects p
                WHERE p.status = 'ativo'
                  AND NOT EXISTS (
                    SELECT 1 FROM project_milestones m
                    WHERE m.project_id = p.id
                      AND m.status IN ('pendente', 'em_andamento')
                      AND (m.data_prevista IS NULL OR m.data_prevista > CURRENT_DATE - INTERVAL '30 days')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM tasks t
                    WHERE t.project_id = p.id
                      AND t.status IN ('pending', 'in_progress')
                  )
                ORDER BY p.prioridade ASC NULLS LAST, p.atualizado_em ASC
                LIMIT 20
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("gov_projeto_drift", r["id"])
                current_hashes.append(sh)
                prio = r["prioridade"] or 5
                urg = max(3, min(7, 7 - prio))
                ctx = {
                    "project_id": r["id"],
                    "nome": r["nome"],
                    "prioridade": prio,
                    "atualizado_em": r["atualizado_em"].isoformat() if r["atualizado_em"] else None,
                    "razao": "sem milestone proximo + sem task pending",
                }
                _bump(res, emit_signal(conn, tipo="gov_projeto_drift", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"projeto_drift: {str(e)[:200]}")

    # ----- 2. Tasks orfas (ai_generated, sem project, ha +14d) -----
    try:
        with savepoint(conn, "task_orfa"):
            cur.execute("""
                SELECT id, titulo, data_criacao, prioridade, contexto, origem
                FROM tasks
                WHERE status IN ('pending', 'in_progress')
                  AND ai_generated = TRUE
                  AND project_id IS NULL
                  AND contact_id IS NULL
                  AND data_criacao < NOW() - INTERVAL '14 days'
                ORDER BY data_criacao ASC
                LIMIT 30
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("gov_task_orfa", r["id"])
                current_hashes.append(sh)
                ctx = {
                    "task_id": r["id"],
                    "titulo": r["titulo"],
                    "data_criacao": r["data_criacao"].isoformat() if r["data_criacao"] else None,
                    "prioridade": r["prioridade"],
                    "contexto": r["contexto"],
                    "origem": r["origem"],
                }
                _bump(res, emit_signal(conn, tipo="gov_task_orfa", signal_hash=sh, urgencia=3, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"task_orfa: {str(e)[:200]}")

    # ----- 3. Projetos com nome similar (potencial duplicacao) -----
    try:
        with savepoint(conn, "duplicados"):
            cur.execute("""
                SELECT a.id AS a_id, a.nome AS a_nome, b.id AS b_id, b.nome AS b_nome,
                       SIMILARITY(LOWER(a.nome), LOWER(b.nome)) AS sim
                FROM projects a
                JOIN projects b ON a.id < b.id
                WHERE a.status = 'ativo' AND b.status = 'ativo'
                  AND SIMILARITY(LOWER(a.nome), LOWER(b.nome)) > 0.70
                ORDER BY sim DESC
                LIMIT 10
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("gov_projetos_duplicados", r["a_id"], r["b_id"])
                current_hashes.append(sh)
                ctx = {
                    "projeto_a": {"id": r["a_id"], "nome": r["a_nome"]},
                    "projeto_b": {"id": r["b_id"], "nome": r["b_nome"]},
                    "similaridade": round(float(r["sim"]), 3),
                }
                _bump(res, emit_signal(conn, tipo="gov_projetos_duplicados", signal_hash=sh, urgencia=4, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        # pg_trgm pode nao estar habilitado — silencioso
        res.errors.append(f"duplicados: {str(e)[:120]}")

    # ----- 4. Carteira overload -----
    try:
        with savepoint(conn, "overload"):
            cur.execute("""
                SELECT COUNT(*) AS n
                FROM projects
                WHERE status = 'ativo' AND prioridade <= 3
            """)
            row = cur.fetchone()
            n = row["n"] if row else 0
            if n > 5:
                sh = make_signal_hash("gov_carteira_overload", "ativos_alta_prio")
                current_hashes.append(sh)
                urg = 5 if n <= 8 else 7
                ctx = {"projetos_alta_prioridade_ativos": n, "target": 5}
                _bump(res, emit_signal(conn, tipo="gov_carteira_overload", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"overload: {str(e)[:200]}")

    res.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)
    return res


def _bump(res: DetectorRun, result: str) -> None:
    if result == "emitted":
        res.emitted += 1
    elif result == "updated":
        res.updated += 1
    else:
        res.skipped += 1
