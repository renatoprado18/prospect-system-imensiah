#!/usr/bin/env python3
"""Re-classifica propostas do email_archive_proposals com as regras
calibradas (Fix 1-4 calibracao 10/06/26).

Roda contra DB apontado por POSTGRES_URL (default = prod). Para cada
proposta em status `STATUS_FILTER` (default 'shadow'):
  1. Carrega headers (subject, from, list-unsubscribe) + body do metadata
     da message vinculada via email_triage.
  2. Chama services.email_triage.EmailTriageService.classify_email_cos
     com as regras NOVAS.
  3. Se nova classificacao != 'archive_proposed':
     - Marca proposal como 'rejected' (ratified_by='cos_auto_recalibration')
     - Atualiza email_triage com nova classification/priority/reasons/tags
  4. Caso contrario, mantem status atual.

NAO arquiva email nenhum no Gmail. Apenas atualiza status no DB.

Uso:
    .venv/bin/python scripts/reclassify_shadow_proposals.py
    DRY_RUN=1 .venv/bin/python scripts/reclassify_shadow_proposals.py
    STATUS_FILTER=rejected DRY_RUN=1 ... scripts/reclassify_shadow_proposals.py
"""
import json
import os
import sys
from pathlib import Path

# Adiciona app/ ao path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# Garante que .env esta carregado
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

DRY_RUN = os.getenv("DRY_RUN") == "1"
STATUS_FILTER = os.getenv("STATUS_FILTER", "shadow")

from database import get_db  # noqa: E402
from services.email_triage import EmailTriageService  # noqa: E402


def reconstruct_headers(metadata: dict, subject_fallback: str) -> dict:
    """Reconstroi headers a partir do metadata da message.

    metadata estrutura (de _ensure_message_row):
      {account, from, from_name, subject}
    """
    from_email = metadata.get("from") or ""
    from_name = metadata.get("from_name") or ""
    subject = metadata.get("subject") or subject_fallback or ""

    if from_name and from_email and from_email != from_name:
        from_header = f'"{from_name}" <{from_email}>'
    elif from_email:
        from_header = from_email
    else:
        from_header = from_name or ""

    return {
        "subject": subject,
        "from": from_header,
        # list-unsubscribe nao temos salvo no metadata — fica vazio.
        # R5 nao acionara, mas R4/R3/R3.5 ainda funcionam.
    }


def main():
    svc = EmailTriageService()
    stats = {
        f"total_{STATUS_FILTER}": 0,
        "reclassified_to_must_read": 0,
        "reclassified_to_silent": 0,
        "kept_archive": 0,
        "skipped_no_message": 0,
        "errors": 0,
        "rule_hits_promoted": {},
    }
    promoted_examples = []

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                eap.id AS proposal_id,
                eap.email_triage_id,
                eap.message_id AS gmail_id,
                eap.account_email,
                eap.sender,
                eap.subject AS proposal_subject,
                et.id AS triage_id,
                et.account_type,
                et.contact_id,
                m.id AS message_id,
                m.metadata,
                m.conteudo
            FROM email_archive_proposals eap
            LEFT JOIN email_triage et ON et.id = eap.email_triage_id
            LEFT JOIN messages m ON m.id = et.message_id
            WHERE eap.status = %s
            ORDER BY eap.id
            """,
            (STATUS_FILTER,),
        )
        rows = cur.fetchall()
        stats[f"total_{STATUS_FILTER}"] = len(rows)

    if not rows:
        print(f"Nenhuma proposta {STATUS_FILTER} encontrada.")
        return stats

    print(f"Re-classificando {len(rows)} propostas {STATUS_FILTER} (DRY_RUN={DRY_RUN})...")
    print()

    for row in rows:
        try:
            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            headers = reconstruct_headers(metadata, row.get("proposal_subject") or "")
            body_text = row.get("conteudo") or ""

            decision = svc.classify_email_cos(
                headers=headers,
                body_text=body_text,
                gmail_label_ids=[],
                account_email=row.get("account_email") or "",
                account_type=row.get("account_type") or "professional",
                contact_id=row.get("contact_id"),
            )

            new_class = decision.get("classification")

            if new_class == "archive_proposed":
                stats["kept_archive"] += 1
                continue

            # Promovido — sai de archive
            if new_class == "must_read":
                stats["reclassified_to_must_read"] += 1
            else:
                stats["reclassified_to_silent"] += 1

            for hit in decision.get("rule_hits") or []:
                stats["rule_hits_promoted"][hit] = stats["rule_hits_promoted"].get(hit, 0) + 1

            if len(promoted_examples) < 10:
                promoted_examples.append({
                    "subject": headers["subject"][:80],
                    "from": headers["from"][:60],
                    "new_class": new_class,
                    "rule_hits": decision.get("rule_hits"),
                })

            if DRY_RUN:
                continue

            # Atualiza no DB
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE email_archive_proposals
                    SET status = 'rejected',
                        ratified_at = NOW(),
                        ratified_by = 'cos_auto_recalibration'
                    WHERE id = %s
                    """,
                    (row["proposal_id"],),
                )
                if row.get("triage_id"):
                    cur.execute(
                        """
                        UPDATE email_triage
                        SET classification = %s,
                            priority = %s,
                            ai_confidence = %s,
                            classification_reasons = %s,
                            suggested_tags = %s,
                            suggested_actions = %s,
                            needs_attention = %s,
                            status = 'pending'
                        WHERE id = %s
                        """,
                        (
                            new_class,
                            decision.get("priority", 5),
                            float(decision.get("ai_confidence") or 0.5),
                            json.dumps(decision.get("reasons") or []),
                            json.dumps(decision.get("suggested_tags") or []),
                            json.dumps(decision.get("suggested_actions") or []),
                            new_class == "must_read",
                            row["triage_id"],
                        ),
                    )
                conn.commit()

        except Exception as e:
            stats["errors"] += 1
            print(f"ERRO proposal_id={row.get('proposal_id')}: {e}")

    print("=" * 60)
    print("RESUMO")
    print("=" * 60)
    for k, v in stats.items():
        if k == "rule_hits_promoted":
            continue
        print(f"  {k}: {v}")
    print()
    print("Rule hits que promoveram propostas:")
    for hit, count in sorted(stats["rule_hits_promoted"].items(), key=lambda x: -x[1]):
        print(f"  {hit}: {count}")
    print()
    print("Exemplos de promocoes (ate 10):")
    for ex in promoted_examples:
        print(f"  - [{ex['new_class']}] {ex['rule_hits']} | {ex['from']} | {ex['subject']}")
    return stats


if __name__ == "__main__":
    main()
