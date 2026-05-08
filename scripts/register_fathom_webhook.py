"""Registra webhook Fathom (POST /webhooks) em uma ou mais contas.

Uso:
    python scripts/register_fathom_webhook.py [--account profissional|pessoal|both] [--dry-run]

Default: ambas as contas, destino https://intel.almeida-prado.com/api/webhooks/fathom,
triggered_for=[my_recordings, shared_external_recordings],
includes: summary=true, action_items=true, transcript=false, crm_matches=false.

Saida: imprime o secret retornado pra cada conta. Salve em .env e Vercel:
    FATHOM_WEBHOOK_SECRET_PROFISSIONAL=whsec_...
    FATHOM_WEBHOOK_SECRET_PESSOAL=whsec_...
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Permite rodar de qualquer cwd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

# Carrega .env
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from integrations.fathom import FathomIntegration  # noqa: E402

DESTINATION_URL = "https://intel.almeida-prado.com/api/webhooks/fathom"
TRIGGERED_FOR = ["my_recordings", "shared_external_recordings"]


async def register_for_account(account: str, dry_run: bool) -> dict:
    fathom = FathomIntegration(account=account)
    if not fathom.api_key:
        return {"account": account, "error": f"sem API key (FATHOM_API_KEY_{account.upper()} vazia)"}

    # Lista existentes pra evitar duplicar
    existing = await fathom.list_webhooks()
    items = existing.get("items", existing) if isinstance(existing, dict) else []
    if isinstance(items, list):
        for w in items:
            if (w.get("url") or "").rstrip("/") == DESTINATION_URL.rstrip("/"):
                return {
                    "account": account,
                    "skipped": True,
                    "reason": "ja existe webhook pra essa URL",
                    "existing_id": w.get("id"),
                }

    if dry_run:
        return {
            "account": account,
            "dry_run": True,
            "would_post": {
                "destination_url": DESTINATION_URL,
                "triggered_for": TRIGGERED_FOR,
                "include_summary": True,
                "include_action_items": True,
                "include_transcript": False,
                "include_crm_matches": False,
            },
        }

    result = await fathom.create_webhook(
        destination_url=DESTINATION_URL,
        triggered_for=TRIGGERED_FOR,
        include_summary=True,
        include_action_items=True,
        include_transcript=False,
        include_crm_matches=False,
    )
    return {"account": account, "result": result}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", choices=["profissional", "pessoal", "both"], default="both")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    accounts = ["profissional", "pessoal"] if args.account == "both" else [args.account]

    print(f"Destino: {DESTINATION_URL}")
    print(f"triggered_for: {TRIGGERED_FOR}")
    print(f"Contas: {', '.join(accounts)}")
    print(f"Dry run: {args.dry_run}\n")

    for acc in accounts:
        print(f"=== {acc.upper()} ===")
        out = await register_for_account(acc, args.dry_run)
        if out.get("error"):
            print(f"  ERRO: {out['error']}\n")
            continue
        if out.get("skipped"):
            print(f"  SKIP: {out['reason']} (id existente: {out.get('existing_id')})\n")
            continue
        if out.get("dry_run"):
            print(f"  DRY-RUN: rodaria POST com {out['would_post']}\n")
            continue
        result = out.get("result", {})
        if result.get("error"):
            print(f"  ERRO API: {result['error']}\n")
            continue
        print(f"  id:       {result.get('id')}")
        print(f"  url:      {result.get('url')}")
        print(f"  secret:   {result.get('secret')}  ← salve em FATHOM_WEBHOOK_SECRET_{acc.upper()}")
        print(f"  triggered: {result.get('triggered_for')}\n")

    print("Proximo passo: copie os secret(s) acima pra .env e Vercel envs, depois redeploy.")


if __name__ == "__main__":
    asyncio.run(main())
