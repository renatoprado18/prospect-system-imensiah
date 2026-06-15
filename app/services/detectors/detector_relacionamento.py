"""
detector_relacionamento — substitui partes de cos_network/cos_sales.

Sinais:
- relacionamento_aniversario_hoje   — Aniversario hoje, contato com interacao previa
- relacionamento_aniversario_proximo — Aniversario em 1-7d
- relacionamento_requer_resposta    — conversation.requer_resposta=TRUE ha +3d
- relacionamento_esfriando          — Contato professional com interacao recente (>0) sem mensagem ha 30-60d
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_relacionamento"


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    cur = conn.cursor()

    # ----- 1. Aniversarios hoje + proximos 7d -----
    # Dedupe por (nome normalizado, aniversario): contatos duplicados (ex:
    # Ricardo Lemos professional + personal) viram UM signal so. Primary id =
    # MIN(id); all_contact_ids leva o array completo pra Brain referenciar.
    try:
        with savepoint(conn, "aniversario"):
            cur.execute("""
                SELECT
                    MIN(id) AS id,
                    (array_agg(id ORDER BY id))::int[] AS all_contact_ids,
                    (array_agg(nome ORDER BY LENGTH(nome) DESC, id))[1] AS nome,
                    (array_agg(apelido ORDER BY apelido NULLS LAST, id))[1] AS apelido,
                    (array_agg(DISTINCT empresa) FILTER (WHERE empresa IS NOT NULL)) AS empresas,
                    (array_agg(DISTINCT cargo)   FILTER (WHERE cargo   IS NOT NULL)) AS cargos,
                    aniversario,
                    (array_agg(DISTINCT contexto) FILTER (WHERE contexto IS NOT NULL)) AS contextos,
                    (array_agg(tags ORDER BY id))[1] AS tags
                FROM contacts
                WHERE aniversario IS NOT NULL
                  AND TO_CHAR(aniversario, 'MM-DD') BETWEEN TO_CHAR(CURRENT_DATE, 'MM-DD')
                                                      AND TO_CHAR(CURRENT_DATE + 7, 'MM-DD')
                GROUP BY LOWER(TRIM(nome)), aniversario
                ORDER BY TO_CHAR(aniversario, 'MM-DD')
                LIMIT 50
            """)
            hoje_mmdd = date.today().strftime("%m-%d")
            for r in cur.fetchall():
                ani = r["aniversario"]
                if not ani:
                    continue
                mmdd = ani.strftime("%m-%d")
                if mmdd == hoje_mmdd:
                    tipo, urg = "relacionamento_aniversario_hoje", 8
                else:
                    tipo, urg = "relacionamento_aniversario_proximo", 4
                # Hash usa primary id — se dedupe muda primary (ex: contato removido),
                # signal antigo expira e novo emite. OK.
                sh = make_signal_hash(tipo, r["id"], date.today().year)
                current_hashes.append(sh)
                ctx = {
                    "contact_id": r["id"],
                    "all_contact_ids": list(r["all_contact_ids"] or []),
                    "nome": r["nome"],
                    "apelido": r["apelido"],
                    "empresa": " / ".join(r["empresas"] or []) or None,
                    "cargo": " / ".join(r["cargos"] or []) or None,
                    "aniversario": ani.isoformat(),
                    "contextos": list(r["contextos"] or []),
                    "tags": r["tags"],
                }
                _bump(res, emit_signal(conn, tipo=tipo, signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"aniversario: {str(e)[:200]}")

    # ----- 2. Conversations requer_resposta ha +3d -----
    try:
        with savepoint(conn, "requer_resposta"):
            cur.execute("""
                SELECT c.id AS conv_id, c.contact_id, c.canal, c.assunto, c.ultimo_mensagem,
                       c.resumo_ai, ct.nome AS contato_nome, ct.empresa, ct.contexto
                FROM conversations c
                JOIN contacts ct ON ct.id = c.contact_id
                WHERE c.requer_resposta = TRUE
                  AND c.status = 'open'
                  AND c.ultimo_mensagem < NOW() - INTERVAL '3 days'
                  AND c.ultimo_mensagem > NOW() - INTERVAL '30 days'
                ORDER BY c.ultimo_mensagem ASC
                LIMIT 30
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("relacionamento_requer_resposta", r["conv_id"])
                current_hashes.append(sh)
                dias = (date.today() - r["ultimo_mensagem"].date()).days if r["ultimo_mensagem"] else 0
                urg = max(4, min(8, 4 + dias // 4))
                ctx = {
                    "conversation_id": r["conv_id"],
                    "contact_id": r["contact_id"],
                    "contato_nome": r["contato_nome"],
                    "empresa": r["empresa"],
                    "contexto": r["contexto"],
                    "canal": r["canal"],
                    "assunto": r["assunto"],
                    "ultimo_mensagem": r["ultimo_mensagem"].isoformat() if r["ultimo_mensagem"] else None,
                    "dias_sem_resposta": dias,
                    "resumo_ai": (r["resumo_ai"] or "")[:300],
                }
                _bump(res, emit_signal(conn, tipo="relacionamento_requer_resposta", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"requer_resposta: {str(e)[:200]}")

    # ----- 3. Contatos esfriando (professional, 30-60d sem msg) -----
    try:
        with savepoint(conn, "esfriando"):
            cur.execute("""
                SELECT ct.id, ct.nome, ct.empresa, ct.cargo, ct.tags,
                       MAX(c.ultimo_mensagem) AS ultima
                FROM contacts ct
                JOIN conversations c ON c.contact_id = ct.id
                WHERE ct.contexto = 'professional'
                  AND (
                    ct.tags::text LIKE '%c-level%'
                    OR ct.tags::text LIKE '%diretor%'
                    OR ct.tags::text LIKE '%founder%'
                    OR ct.tags::text LIKE '%parceiro%'
                  )
                GROUP BY ct.id, ct.nome, ct.empresa, ct.cargo, ct.tags
                HAVING MAX(c.ultimo_mensagem) BETWEEN NOW() - INTERVAL '60 days' AND NOW() - INTERVAL '30 days'
                ORDER BY MAX(c.ultimo_mensagem) ASC
                LIMIT 15
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("relacionamento_esfriando", r["id"])
                current_hashes.append(sh)
                dias = (date.today() - r["ultima"].date()).days if r["ultima"] else 0
                urg = 3 if dias < 45 else 4
                ctx = {
                    "contact_id": r["id"],
                    "nome": r["nome"],
                    "empresa": r["empresa"],
                    "cargo": r["cargo"],
                    "tags": r["tags"],
                    "dias_sem_contato": dias,
                    "ultima_interacao": r["ultima"].isoformat() if r["ultima"] else None,
                }
                _bump(res, emit_signal(conn, tipo="relacionamento_esfriando", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"esfriando: {str(e)[:200]}")

    res.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)
    return res


def _bump(res: DetectorRun, result: str) -> None:
    if result == "emitted":
        res.emitted += 1
    elif result == "updated":
        res.updated += 1
    else:
        res.skipped += 1
