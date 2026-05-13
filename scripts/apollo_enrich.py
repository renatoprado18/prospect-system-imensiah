#!/usr/bin/env python3
"""
Apollo.io people/match enrichment — uso pontual.

⚠️  AVISO IMPORTANTE — leia antes de rodar
─────────────────────────────────────────────
Apollo Free tier declara emails como "verified" baseado em heuristica (formato +
MX valido), NAO em SMTP real. Investigacao em 13/05/2026 mostrou 30% de bounce
nos emails "verified" do Apollo. Sempre rode `smtp_validate.py` em seguida pra
filtrar falsos positivos antes de usar os emails em qualquer outreach.

Contexto completo da investigacao + decisao No-Go pra uso em volume:
ver project_notes do projeto 18 ("Integracao Apollo.io" arquivado) em prod.

Use cases validos
─────────────────
- Investigacao caso-a-caso de 1-5 leads (sempre seguido de smtp_validate)
- Pegar firmografia (# funcionarios, industria) pra qualificacao ICP

NUNCA usar pra
──────────────
- Enrichment em massa pra cold outreach (30% bounce queima reputacao SMTP)
- Confiar no campo apollo_email_verificado=S sem validacao SMTP independente

Pre-requisitos
──────────────
- APOLLO_API_KEY em .env.local (Free tier basta — gerar em
  https://app.apollo.io/#/settings/integrations/api)
- CSV de entrada com pelo menos coluna `linkedin` (URL do perfil LinkedIn)

Uso
───
    python scripts/apollo_enrich.py <csv_in> [csv_out]

    # exemplo:
    python scripts/apollo_enrich.py contatos.csv contatos_enriched.csv

    # se csv_out omitido: usa <csv_in>_enriched.csv

Limites Free tier
─────────────────
- 50/min, 600/dia, ~10k email/mes
- Email pessoal/celular: BLOQUEADO (flags reveal_* nao destravam)
- Tech stack: BLOQUEADO
- Email empresarial: SIM (mas verificacao e declarativa, vide aviso)
- Firmografia (# funcionarios, industria, receita estimada): SIM

Colunas adicionadas no CSV de saida
───────────────────────────────────
apollo_email, apollo_email_verificado (S/?/N), apollo_telefone,
apollo_faturamento, apollo_funcionarios, apollo_tech_stack, observacoes
"""
import csv
import sys
import time
from pathlib import Path

import httpx

API_URL = "https://api.apollo.io/api/v1/people/match"
SLEEP_BETWEEN = 1.3  # ~46/min — abaixo do limite de 50/min Free


def load_key() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env.local"
    if not env_path.exists():
        sys.exit(f"❌ {env_path} não existe — crie e adicione APOLLO_API_KEY=xxx")
    for line in env_path.read_text().splitlines():
        if line.startswith("APOLLO_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("❌ APOLLO_API_KEY não encontrada em .env.local")


def enrich_one(client: httpx.Client, api_key: str, linkedin_url: str) -> dict:
    try:
        r = client.post(
            API_URL,
            headers={"X-Api-Key": api_key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
            json={"linkedin_url": linkedin_url},
            timeout=20,
        )
        if r.status_code == 429:
            return {"_error": "rate_limit"}
        if r.status_code != 200:
            return {"_error": f"http_{r.status_code}"}
        return r.json()
    except Exception as e:
        return {"_error": f"exc_{type(e).__name__}"}


def extract_fields(payload: dict) -> dict:
    if "_error" in payload:
        return {
            "apollo_email": "",
            "apollo_email_verificado": "",
            "apollo_telefone": "",
            "apollo_faturamento": "",
            "apollo_funcionarios": "",
            "apollo_tech_stack": "",
            "observacoes": f"erro: {payload['_error']}",
        }
    person = payload.get("person") or {}
    org = person.get("organization") or {}
    email = person.get("email") or ""
    status = person.get("email_status") or ""
    # NOTA: "verified" do Apollo e declarativo — confirmar com smtp_validate.py
    verificado = "verified" in status.lower() or "valid" in status.lower()

    return {
        "apollo_email": email,
        "apollo_email_verificado": "S" if verificado else ("?" if email else "N"),
        "apollo_telefone": (person.get("sanitized_phone") or person.get("mobile_phone") or "")[:40],
        "apollo_faturamento": (org.get("annual_revenue_printed") or org.get("estimated_annual_revenue") or ""),
        "apollo_funcionarios": str(org.get("estimated_num_employees") or ""),
        "apollo_tech_stack": ", ".join((org.get("technology_names") or [])[:5]),
        "observacoes": "" if person else "sem match",
    }


APOLLO_COLS = [
    "apollo_email", "apollo_email_verificado", "apollo_telefone",
    "apollo_faturamento", "apollo_funcionarios", "apollo_tech_stack", "observacoes",
]


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) > 1 else 1)

    csv_in = Path(sys.argv[1]).expanduser()
    csv_out = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else csv_in.with_name(csv_in.stem + "_enriched.csv")

    if not csv_in.exists():
        sys.exit(f"❌ CSV de entrada nao existe: {csv_in}")

    api_key = load_key()

    with csv_in.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("❌ CSV vazio")
    if "linkedin" not in rows[0]:
        sys.exit("❌ CSV precisa de coluna `linkedin` (URL do perfil)")

    fieldnames = list(rows[0].keys())
    for col in APOLLO_COLS:
        if col not in fieldnames:
            fieldnames.append(col)

    print(f"📋 {len(rows)} contatos a enriquecer")
    print(f"📥 IN : {csv_in}")
    print(f"📤 OUT: {csv_out}")
    print(f"⏳ Rate limit Free: 50/min — sleep {SLEEP_BETWEEN}s entre chamadas\n")

    stats = {"ok": 0, "sem_match": 0, "erro": 0, "com_email": 0, "com_firmografia": 0}

    with httpx.Client() as client:
        for i, row in enumerate(rows, 1):
            linkedin = (row.get("linkedin") or "").strip()
            if not linkedin:
                row["observacoes"] = "sem linkedin"
                stats["erro"] += 1
                continue

            print(f"[{i:03d}/{len(rows)}] {(row.get('nome') or row.get('name') or '')[:35]:35} ... ", end="", flush=True)
            payload = enrich_one(client, api_key, linkedin)
            fields = extract_fields(payload)
            row.update(fields)

            if "_error" in payload:
                stats["erro"] += 1
                print(f"❌ {payload['_error']}")
            elif not payload.get("person"):
                stats["sem_match"] += 1
                print("⚠️  sem match")
            else:
                stats["ok"] += 1
                if fields["apollo_email"]:
                    stats["com_email"] += 1
                if fields["apollo_funcionarios"]:
                    stats["com_firmografia"] += 1
                summary = []
                if fields["apollo_email"]:
                    summary.append(f"email({fields['apollo_email_verificado']})")
                if fields["apollo_funcionarios"]:
                    summary.append(f"emp={fields['apollo_funcionarios']}")
                print("✅ " + " ".join(summary) if summary else "✅ basico")

            if i < len(rows):
                time.sleep(SLEEP_BETWEEN)

    with csv_out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    n = len(rows)
    print("\n" + "=" * 60)
    print("RESUMO")
    print("=" * 60)
    print(f"Total processados   : {n}")
    print(f"Match encontrado    : {stats['ok']} ({stats['ok']*100//max(n,1)}%)")
    print(f"Sem match           : {stats['sem_match']}")
    print(f"Erros               : {stats['erro']}")
    print(f"Com email Apollo    : {stats['com_email']} ({stats['com_email']*100//max(stats['ok'],1)}% dos matches)")
    print(f"Com firmografia     : {stats['com_firmografia']} ({stats['com_firmografia']*100//max(stats['ok'],1)}% dos matches)")
    print(f"\n📄 CSV gravado: {csv_out}")
    print(f"\n⚠️  PROXIMO PASSO obrigatorio: validar bounces reais com")
    print(f"    python scripts/smtp_validate.py {csv_out}")


if __name__ == "__main__":
    main()
