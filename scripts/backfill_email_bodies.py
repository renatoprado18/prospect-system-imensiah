#!/usr/bin/env python3
"""
Backfill de corpo de email (F-2 raw email, 11/07/2026).

Re-busca no Gmail (por external_id/gmail_id) os emails de ALTO VALOR que ficaram
sem corpo capturado (blind spot antigo: HTML-only descartado + truncagem 5000).
Aplica o mesmo `extract_email_body` do sync novo (fallback HTML->texto) e faz
UPDATE em messages.conteudo/conteudo_html.

Escopo padrao: só os de alto valor — priority>=7 OU contato circulo 1-2. Os
~206 `archive_proposed` (ruído marketing) NÃO são tocados.

Uso (da máquina do Renato, alvo prod explícito — ver reference_db_target_protocol):
    # dry-run (só mostra o que faria):
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/backfill_email_bodies.py
    # aplicar de verdade:
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 python3 scripts/backfill_email_bodies.py --apply

Idempotente: só toca linhas com conteudo vazio. Rodar 2x não duplica nada.
Emails deletados no Gmail (404) são pulados e contados.
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from database import get_db
from integrations.gmail import GmailIntegration
from services.email_triage import extract_email_body

APPLY = "--apply" in sys.argv
# Filtro de alto valor. Ajustável: --all inclui TODOS os vazios (inclui ruído).
INCLUDE_ALL = "--all" in sys.argv


def fetch_targets():
    """Emails vazios de alto valor, com gmail_id e conta."""
    filtro = "" if INCLUDE_ALL else "AND (et.priority >= 7 OR c.circulo IN (1,2))"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT et.id AS triage_id, m.id AS message_id, m.external_id AS gmail_id,
                   COALESCE(et.account_email, m.metadata->>'account') AS account_email,
                   et.priority, et.classification
            FROM email_triage et
            JOIN messages m ON m.id = et.message_id
            LEFT JOIN contacts c ON c.id = et.contact_id
            WHERE (m.conteudo IS NULL OR length(m.conteudo) = 0)
              AND m.external_id IS NOT NULL
              {filtro}
            ORDER BY et.priority DESC
        """)
        return cur.fetchall()


async def get_tokens():
    """{email: access_token} — refresh de todas as contas com refresh_token."""
    gmail = GmailIntegration()
    tokens = {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT email, refresh_token FROM google_accounts WHERE refresh_token IS NOT NULL")
        accounts = cur.fetchall()
    for acc in accounts:
        try:
            r = await gmail.refresh_access_token(acc["refresh_token"])
            if "access_token" in r:
                tokens[acc["email"]] = r["access_token"]
        except Exception as e:
            print(f"  ⚠️  refresh falhou {acc['email']}: {e}")
    return gmail, tokens


async def main():
    targets = fetch_targets()
    print(f"Alvos (vazios de {'TODOS' if INCLUDE_ALL else 'alto valor'}): {len(targets)}")
    if not targets:
        print("Nada a fazer.")
        return
    for t in targets[:10]:
        print(f"  triage#{t['triage_id']} msg#{t['message_id']} p{t['priority']} "
              f"{t['classification']} <{t['account_email']}> gmail={t['gmail_id']}")
    if len(targets) > 10:
        print(f"  ... +{len(targets)-10}")

    if not APPLY:
        print("\n[DRY-RUN] nada gravado. Rode com --apply pra aplicar.")
        return

    gmail, tokens = await get_tokens()
    print(f"\nContas com token: {list(tokens.keys())}")

    updated = 0
    still_empty = 0
    not_found = 0
    no_token = 0
    for t in targets:
        acc = t["account_email"]
        token = tokens.get(acc)
        if not token:
            no_token += 1
            continue
        try:
            msg = await gmail.get_message(token, t["gmail_id"])
            if not msg or "error" in msg:
                not_found += 1
                continue
            body = gmail.parse_message_body(msg)
            text, html = extract_email_body(body)
            if not text and not html:
                still_empty += 1
                continue
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE messages SET conteudo = %s, conteudo_html = %s "
                    "WHERE id = %s AND (conteudo IS NULL OR length(conteudo) = 0)",
                    (text, html or None, t["message_id"]),
                )
                conn.commit()
                updated += cur.rowcount
        except Exception as e:
            print(f"  ⚠️  msg#{t['message_id']}: {e}")

    print(f"\n✅ Atualizados: {updated}")
    print(f"   Sem corpo mesmo após re-fetch (HTML+text vazios): {still_empty}")
    print(f"   404/deletados no Gmail: {not_found}")
    print(f"   Sem token pra conta: {no_token}")


if __name__ == "__main__":
    asyncio.run(main())
