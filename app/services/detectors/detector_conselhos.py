"""
detector_conselhos — substitui cos_conselheiro + cos_cs.

Sinais que emite:
- raci_vencido           — RACI no ConselhoOS com prazo passado e status != concluido
- raci_perto_vencer      — RACI vence em 3-7d
- reuniao_proxima_sem_dossie — Reuniao em 7d sem dossie gerado
- reuniao_proxima_sem_pauta — Reuniao em 3d sem pauta enviada
- grupo_wa_silencioso    — Grupo WA do conselho sem msg ha +7d
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Dict, List

import psycopg2
import psycopg2.extras

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_conselhos"
CONSELHOOS_URL = os.getenv("CONSELHOOS_DATABASE_URL", "").strip()


def run(conn) -> DetectorRun:
    run = DetectorRun(detector=DETECTOR_NAME)
    if not CONSELHOOS_URL:
        run.errors.append("CONSELHOOS_DATABASE_URL nao configurado")
        return run

    current_hashes: List[str] = []

    # === Le ConselhoOS DB pra RACI + reunioes ===
    try:
        with savepoint(conn, "block_1"):
            co_conn = psycopg2.connect(CONSELHOOS_URL)
            co_cur = co_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            # ----- 1. RACI vencido + 2. perto de vencer -----
            co_cur.execute("""
                SELECT r.id::text AS raci_id, r.acao, r.prazo, r.status,
                       r.responsavel_r, r.responsavel_a, r.notas,
                       r.empresa_id::text AS empresa_id,
                       COALESCE(e.nome, 'sem empresa') AS empresa,
                       r.atualizado_em
                FROM raci_itens r
                LEFT JOIN empresas e ON e.id = r.empresa_id
                WHERE r.status NOT IN ('concluido', 'cancelado')
                  AND r.prazo IS NOT NULL
                  AND r.prazo <= CURRENT_DATE + INTERVAL '7 days'
                ORDER BY r.prazo ASC
                LIMIT 100
            """)
            for r in co_cur.fetchall():
                prazo = r["prazo"]
                if prazo is None:
                    continue
                dias = (prazo - date.today()).days
                if dias < 0:
                    tipo = "raci_vencido"
                    # Urgencia: 5 base + 1 por semana atrasada, cap 10
                    urg = min(10, 5 + abs(dias) // 7)
                elif dias <= 7:
                    tipo = "raci_perto_vencer"
                    urg = 7 - dias  # 0-3d=4-7 / 4-7d=0-3 (curva crescente)
                    urg = max(3, min(7, urg + 3))
                else:
                    continue

                sh = make_signal_hash(tipo, r["empresa_id"], r["raci_id"])
                current_hashes.append(sh)
                ctx = {
                    "raci_id": r["raci_id"],
                    "empresa": r["empresa"],
                    "empresa_id": r["empresa_id"],
                    "acao": (r["acao"] or "")[:200],
                    "prazo": prazo.isoformat(),
                    "dias_atraso": -dias if dias < 0 else 0,
                    "dias_ate_vencer": dias if dias >= 0 else 0,
                    "status": r["status"],
                    "responsavel_r": r["responsavel_r"],
                    "responsavel_a": r["responsavel_a"],
                    "notas": (r["notas"] or "")[:500],
                    "ultimo_update": r["atualizado_em"].isoformat() if r["atualizado_em"] else None,
                }
                result = emit_signal(conn, tipo=tipo, signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME)
                if result == "emitted":
                    run.emitted += 1
                elif result == "updated":
                    run.updated += 1
                else:
                    run.skipped += 1

            # ----- 3. Reuniao proxima sem dossie / 4. sem pauta -----
            co_cur.execute("""
                SELECT r.id::text AS reuniao_id, r.titulo, r.data, r.status,
                       r.dossie_md, r.pauta_md, r.empresa_id::text AS empresa_id,
                       e.nome AS empresa
                FROM reunioes r
                JOIN empresas e ON e.id = r.empresa_id
                WHERE r.status = 'agendada'
                  AND r.data BETWEEN NOW() AND NOW() + INTERVAL '7 days'
                ORDER BY r.data ASC
                LIMIT 30
            """)
            for r in co_cur.fetchall():
                data_reuniao = r["data"].date() if r["data"] else None
                if not data_reuniao:
                    continue
                dias = (data_reuniao - date.today()).days

                if not r["dossie_md"] and dias <= 7:
                    sh = make_signal_hash("reuniao_sem_dossie", r["empresa_id"], r["reuniao_id"])
                    current_hashes.append(sh)
                    urg = max(3, min(9, 10 - dias))  # quanto mais perto, mais urgente
                    ctx = {
                        "reuniao_id": r["reuniao_id"],
                        "empresa": r["empresa"],
                        "empresa_id": r["empresa_id"],
                        "titulo": r["titulo"],
                        "data": data_reuniao.isoformat(),
                        "dias_ate": dias,
                        "tem_pauta": bool(r["pauta_md"]),
                    }
                    result = emit_signal(conn, tipo="reuniao_sem_dossie", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME)
                    if result == "emitted": run.emitted += 1
                    elif result == "updated": run.updated += 1
                    else: run.skipped += 1

                if not r["pauta_md"] and dias <= 3:
                    sh = make_signal_hash("reuniao_sem_pauta", r["empresa_id"], r["reuniao_id"])
                    current_hashes.append(sh)
                    urg = max(5, min(10, 11 - dias))
                    ctx = {
                        "reuniao_id": r["reuniao_id"],
                        "empresa": r["empresa"],
                        "titulo": r["titulo"],
                        "data": data_reuniao.isoformat(),
                        "dias_ate": dias,
                    }
                    result = emit_signal(conn, tipo="reuniao_sem_pauta", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME)
                    if result == "emitted": run.emitted += 1
                    elif result == "updated": run.updated += 1
                    else: run.skipped += 1

            co_conn.close()
    except Exception as e:
        run.errors.append(f"conselhoos query: {str(e)[:200]}")

    # ----- 5. Grupo WA silencioso (de conselho) -----
    try:
        with savepoint(conn, "block_2"):
            cur = conn.cursor()
            cur.execute("""
                SELECT pwg.group_jid, pwg.group_name, p.nome AS projeto
                FROM project_whatsapp_groups pwg
                JOIN projects p ON p.id = pwg.project_id
                WHERE pwg.ativo = TRUE
                  AND p.tipo = 'conselho'
                  AND NOT EXISTS (
                    SELECT 1 FROM group_messages gm
                    WHERE gm.group_jid = pwg.group_jid
                      AND gm.timestamp >= NOW() - INTERVAL '7 days'
                  )
                LIMIT 20
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("grupo_wa_silencioso", r["group_jid"])
                current_hashes.append(sh)
                ctx = {
                    "group_jid": r["group_jid"],
                    "group_name": r["group_name"],
                    "projeto": r["projeto"],
                }
                result = emit_signal(conn, tipo="grupo_wa_silencioso", signal_hash=sh, urgencia=4, contexto=ctx, detector=DETECTOR_NAME)
                if result == "emitted": run.emitted += 1
                elif result == "updated": run.updated += 1
                else: run.skipped += 1
    except Exception as e:
        run.errors.append(f"grupo_wa: {str(e)[:200]}")

    # Expira signals que nao reapareceram
    run.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)

    return run
