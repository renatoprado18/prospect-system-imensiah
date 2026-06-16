"""
detector_inbox — sec 4.7 do ARCHITECTURE_REBUILD.

Email (via email_triage ja classificado) + WhatsApp DMs.

Sinais:
- inbox_atencao   — must_read/urgent + WA DM contato VIP        (urg 6-9)
- inbox_digest    — important                                    (urg 3-5, vai pro briefing 7h)
- WA DM medio gera tambem digest

Ruido: silent + archive_proposed (nada emitido).
"""
from __future__ import annotations

from typing import List

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_inbox"

# Tags que viram "VIP" pra WA DM
VIP_TAGS = ("c-level", "diretor", "founder", "socio", "parceiro_potencial", "familia")


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    cur = conn.cursor()

    # ----- 1. Emails alto sinal (must_read | urgent) ultimas 4h -----
    try:
        with savepoint(conn, "emails_alto"):
            cur.execute("""
                SELECT et.id, et.message_id, et.conversation_id, et.contact_id,
                       et.classification, et.priority, et.classification_reasons,
                       et.account_email, et.criado_em,
                       c.assunto, ct.nome AS contato_nome, ct.empresa
                FROM email_triage et
                LEFT JOIN conversations c ON c.id = et.conversation_id
                LEFT JOIN contacts ct ON ct.id = et.contact_id
                WHERE et.classification IN ('must_read', 'urgent')
                  AND et.status = 'pending'
                  AND et.criado_em > NOW() - INTERVAL '4 hours'
                ORDER BY et.priority DESC, et.criado_em DESC
                LIMIT 30
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("inbox_atencao", "gmail", r["id"])
                current_hashes.append(sh)
                base = 6 if r["classification"] == "must_read" else 8
                prio = r["priority"] or 5
                urg = min(9, base + max(0, (prio - 5)))
                # unknown_sender: contato nao mapeado no CRM. Brain usa pra
                # default-escalar-light (ack registro) em vez de gastar tokens
                # rascunhando resposta sem contexto. Caso 16/06/26: IBGC
                # pesquisa institucional virou urg 9 + sem contato -> Tonha
                # gastou 4 iters pra escalate.
                unknown_sender = r["contact_id"] is None
                ctx = {
                    "fonte": "gmail",
                    "triage_id": r["id"],
                    "conversation_id": r["conversation_id"],
                    "contact_id": r["contact_id"],
                    "contato_nome": r["contato_nome"],
                    "empresa": r["empresa"],
                    "unknown_sender": unknown_sender,
                    "assunto": (r["assunto"] or "")[:200],
                    "classification": r["classification"],
                    "reasons": r["classification_reasons"],
                    "account_email": r["account_email"],
                    "received_at": r["criado_em"].isoformat() if r["criado_em"] else None,
                }
                _bump(res, emit_signal(conn, tipo="inbox_atencao", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"emails_alto: {str(e)[:200]}")

    # ----- 2. Emails 'important' -> digest -----
    try:
        with savepoint(conn, "emails_digest"):
            cur.execute("""
                SELECT et.id, et.conversation_id, et.contact_id, et.account_email, et.criado_em,
                       c.assunto, ct.nome AS contato_nome, ct.empresa
                FROM email_triage et
                LEFT JOIN conversations c ON c.id = et.conversation_id
                LEFT JOIN contacts ct ON ct.id = et.contact_id
                WHERE et.classification = 'important'
                  AND et.status = 'pending'
                  AND et.criado_em > NOW() - INTERVAL '24 hours'
                ORDER BY et.criado_em DESC
                LIMIT 50
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("inbox_digest", "gmail", r["id"])
                current_hashes.append(sh)
                ctx = {
                    "fonte": "gmail",
                    "triage_id": r["id"],
                    "conversation_id": r["conversation_id"],
                    "contato_nome": r["contato_nome"],
                    "empresa": r["empresa"],
                    "assunto": (r["assunto"] or "")[:200],
                    "account_email": r["account_email"],
                    "received_at": r["criado_em"].isoformat() if r["criado_em"] else None,
                }
                _bump(res, emit_signal(conn, tipo="inbox_digest", signal_hash=sh, urgencia=4, contexto=ctx, detector=DETECTOR_NAME))
                # Buffer pro digest 7h
                try:
                    cur.execute("""
                        INSERT INTO inbox_digest_buffer (fonte, ref_id, preview, from_label, subject, received_at)
                        VALUES ('gmail', %s, %s, %s, %s, %s)
                        ON CONFLICT (fonte, ref_id) DO NOTHING
                    """, (
                        str(r["id"]),
                        (r["assunto"] or "")[:500],
                        f"{r['contato_nome'] or 'desconhecido'} ({r['empresa'] or ''})",
                        (r["assunto"] or "")[:200],
                        r["criado_em"],
                    ))
                except Exception:
                    pass
    except Exception as e:
        res.errors.append(f"emails_digest: {str(e)[:200]}")

    # ----- 3. WA DMs ultimas 4h de contato VIP -----
    try:
        with savepoint(conn, "wa_dm"):
            cur.execute("""
                SELECT wm.id, wm.contact_id, wm.phone, wm.content, wm.message_date,
                       ct.nome AS contato_nome, ct.empresa, ct.tags, ct.contexto
                FROM whatsapp_messages wm
                JOIN contacts ct ON ct.id = wm.contact_id
                WHERE wm.direction = 'incoming'
                  AND wm.message_date > NOW() - INTERVAL '4 hours'
                  AND wm.contact_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_messages out_m
                    WHERE out_m.contact_id = wm.contact_id
                      AND out_m.direction = 'outgoing'
                      AND out_m.message_date > wm.message_date
                  )
                ORDER BY wm.message_date DESC
                LIMIT 50
            """)
            for r in cur.fetchall():
                tags_str = str(r["tags"] or "")
                is_vip = any(t in tags_str for t in VIP_TAGS)
                if is_vip:
                    tipo, urg = "inbox_atencao", 7
                else:
                    tipo, urg = "inbox_digest", 4
                sh = make_signal_hash(tipo, "wa_dm", r["id"])
                current_hashes.append(sh)
                ctx = {
                    "fonte": "wa_dm",
                    "wa_message_id": r["id"],
                    "contact_id": r["contact_id"],
                    "contato_nome": r["contato_nome"],
                    "empresa": r["empresa"],
                    "contexto": r["contexto"],
                    "preview": (r["content"] or "")[:500],
                    "received_at": r["message_date"].isoformat() if r["message_date"] else None,
                    "is_vip": is_vip,
                }
                _bump(res, emit_signal(conn, tipo=tipo, signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
                if tipo == "inbox_digest":
                    try:
                        cur.execute("""
                            INSERT INTO inbox_digest_buffer (fonte, ref_id, preview, from_label, received_at)
                            VALUES ('wa_dm', %s, %s, %s, %s)
                            ON CONFLICT (fonte, ref_id) DO NOTHING
                        """, (
                            str(r["id"]),
                            (r["content"] or "")[:500],
                            f"{r['contato_nome'] or 'desconhecido'}",
                            r["message_date"],
                        ))
                    except Exception:
                        pass
    except Exception as e:
        res.errors.append(f"wa_dm: {str(e)[:200]}")

    res.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)
    return res


def _bump(res: DetectorRun, result: str) -> None:
    if result == "emitted":
        res.emitted += 1
    elif result == "updated":
        res.updated += 1
    else:
        res.skipped += 1
