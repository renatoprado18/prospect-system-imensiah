#!/usr/bin/env python3
"""
SMTP RCPT-TO validator — confirma bounces SEM enviar email real.

Companion obrigatorio do apollo_enrich.py. Apollo Free declara "verified"
declarativamente — investigacao em 13/05/2026 mostrou 30% bounce real nos
"verified". Este script faz o que Apollo nao faz: handshake SMTP de verdade.

Como funciona
─────────────
1. Pra cada email do CSV, faz lookup MX via `dig`
2. Conecta SMTP no servidor MX (porta 25)
3. HELO → MAIL FROM:<noreply@check.local> → RCPT TO:<email_a_validar>
4. Parse response:
   - 250 → exists (servidor aceita o destinatario)
   - 550/551/553 → not_exists (rejected)
   - 450/451/452 → blocked (greylisting / anti-abuse)
5. Nao envia DATA — fecha conexao apos RCPT
6. Resultados gravados em coluna `smtp_status` do CSV de saida

Limitacoes conhecidas
─────────────────────
- Provedores grandes (Gmail/Outlook/Yahoo) sao catch-all — sempre dizem 250
  mesmo se conta nao existir. Marcamos como `catch_all` (indeterminado).
- Alguns servidores empresariais bloqueiam validation pos algumas tentativas
  do mesmo IP (greylisting). Marcamos como `blocked`.
- Connection refused = empresa com SMTP desligado/firewall agressivo.
  Marcamos como `error_ConnectionRefusedError` (inconclusivo).
- Conta apenas como "real exists" os 250 explicitos em dominios nao-catch-all.

Pre-requisitos
──────────────
- Comando `dig` instalado (padrao em macOS/Linux)
- Conexao internet outbound porta 25 liberada (alguns ISPs/firewalls bloqueiam)
- IP do executante nao em blacklist SMTP (caso contrario, servidores recusam)

Uso
───
    python scripts/smtp_validate.py <csv_in> [csv_out]
    python scripts/smtp_validate.py <csv_in> --column apollo_email
    python scripts/smtp_validate.py <csv_in> --sample 10

Args
────
    csv_in           CSV com coluna de email a validar
    csv_out          opcional, default <csv_in>_smtp.csv
    --column NAME    coluna de email (default tenta apollo_email > email > emails)
    --sample N       valida apenas N amostras (default: todos)
    --skip-status S  pula linhas onde smtp_status ja eh S (idempotencia)

Output
──────
Adiciona colunas: smtp_status, smtp_mx, smtp_msg
Imprime resumo: quantos exists/not_exists/catch_all/blocked

Quando usar
───────────
- Sempre apos `apollo_enrich.py` antes de tomar acao em qualquer email
- Pra auditar lista de emails antigos antes de cold outreach
- Pra higiene de base periodica (~ a cada 6 meses recomendado)
"""
import argparse
import csv
import random
import smtplib
import socket
import subprocess
import sys
from pathlib import Path

CATCH_ALL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.com.br", "ymail.com",
    "icloud.com", "me.com",
    "uol.com.br", "bol.com.br", "terra.com.br",
}
EMAIL_COL_CANDIDATES = ["apollo_email", "email", "email_atual", "smtp_email"]


def get_mx(domain: str) -> str | None:
    try:
        r = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=10
        )
        parsed = []
        for line in r.stdout.strip().splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[0].isdigit():
                parsed.append((int(parts[0]), parts[1].rstrip('.')))
        return sorted(parsed)[0][1] if parsed else None
    except Exception:
        return None


def validate_email(email: str) -> dict:
    try:
        _, domain = email.lower().split("@", 1)
    except ValueError:
        return {"status": "invalid_format", "mx": "", "msg": "email malformado"}

    if domain in CATCH_ALL_DOMAINS:
        return {"status": "catch_all", "mx": "", "msg": "provedor catch-all"}

    mx = get_mx(domain)
    if not mx:
        return {"status": "no_mx", "mx": "", "msg": "dominio sem MX record"}

    try:
        s = smtplib.SMTP(mx, 25, timeout=12)
        s.helo("almeida-prado.com")
        s.mail("noreply@almeida-prado.com")
        code, msg = s.rcpt(email)
        try:
            s.quit()
        except Exception:
            pass
        msg_str = (msg.decode("utf-8", errors="ignore") if isinstance(msg, bytes) else str(msg))[:100]
        if code == 250:
            return {"status": "exists", "mx": mx, "msg": msg_str}
        if code in (550, 551, 553):
            return {"status": "not_exists", "mx": mx, "msg": msg_str}
        if code in (450, 451, 452):
            return {"status": "blocked", "mx": mx, "msg": msg_str}
        return {"status": f"unknown_{code}", "mx": mx, "msg": msg_str}
    except smtplib.SMTPServerDisconnected:
        return {"status": "blocked", "mx": mx, "msg": "server disconnected"}
    except socket.timeout:
        return {"status": "timeout", "mx": mx, "msg": "smtp timeout"}
    except ConnectionRefusedError:
        return {"status": "error_ConnectionRefusedError", "mx": mx, "msg": "connection refused"}
    except Exception as e:
        return {"status": f"error_{type(e).__name__}", "mx": mx, "msg": str(e)[:100]}


EMOJI = {
    "exists": "✅", "not_exists": "❌", "catch_all": "🌀",
    "no_mx": "🚫", "blocked": "🔒", "timeout": "⏱",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_in", help="CSV de entrada")
    ap.add_argument("csv_out", nargs="?", default=None, help="CSV de saida (default <csv_in>_smtp.csv)")
    ap.add_argument("--column", default=None, help="Coluna de email (default auto-detect)")
    ap.add_argument("--sample", type=int, default=None, help="Validar apenas N amostras aleatorias")
    ap.add_argument("--skip-status", default=None, help="Pular linhas onde smtp_status ja seja igual a este valor (idempotencia)")
    args = ap.parse_args()

    csv_in = Path(args.csv_in).expanduser()
    if not csv_in.exists():
        sys.exit(f"❌ CSV de entrada nao existe: {csv_in}")
    csv_out = Path(args.csv_out).expanduser() if args.csv_out else csv_in.with_name(csv_in.stem + "_smtp.csv")

    with csv_in.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("❌ CSV vazio")

    email_col = args.column
    if not email_col:
        for cand in EMAIL_COL_CANDIDATES:
            if cand in rows[0]:
                email_col = cand
                break
    if not email_col or email_col not in rows[0]:
        sys.exit(f"❌ Coluna de email nao encontrada. Use --column. Disponiveis: {list(rows[0].keys())}")

    fieldnames = list(rows[0].keys())
    for c in ("smtp_status", "smtp_mx", "smtp_msg"):
        if c not in fieldnames:
            fieldnames.append(c)

    candidates = [r for r in rows if (r.get(email_col) or "").strip()]
    if args.skip_status:
        before = len(candidates)
        candidates = [r for r in candidates if r.get("smtp_status") != args.skip_status]
        print(f"⏭  skip_status={args.skip_status}: {before - len(candidates)} linhas puladas")

    if args.sample and len(candidates) > args.sample:
        candidates = random.sample(candidates, args.sample)

    print(f"📋 Total no CSV: {len(rows)} | com email: {len([r for r in rows if r.get(email_col)])} | a validar: {len(candidates)}")
    print(f"🔬 Coluna email: {email_col}\n")

    stats = {}
    for i, row in enumerate(candidates, 1):
        email = (row[email_col] or "").strip()
        print(f"  [{i:03d}/{len(candidates)}] {(row.get('nome') or row.get('name') or '')[:28]:28} {email[:42]:42} ", end="", flush=True)
        result = validate_email(email)
        row.update({"smtp_status": result["status"], "smtp_mx": result["mx"], "smtp_msg": result["msg"]})
        stats[result["status"]] = stats.get(result["status"], 0) + 1
        emoji = EMOJI.get(result["status"], "❓")
        print(f"{emoji} {result['status']}" + (f" (mx={result['mx'][:30]})" if result["mx"] else ""))

    with csv_out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    n = len(candidates)
    print()
    print("=" * 60)
    print("RESUMO")
    print("=" * 60)
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {EMOJI.get(k, '❓')} {k:25} : {v} ({v*100//max(n,1)}%)")

    exists = stats.get("exists", 0)
    not_exists = stats.get("not_exists", 0) + stats.get("no_mx", 0)
    indef = sum(v for k, v in stats.items() if k in ("catch_all", "blocked", "timeout") or k.startswith("error_") or k.startswith("unknown_"))
    print()
    print(f"  Validados (exists) — usar     : {exists}/{n}")
    print(f"  Bounce confirmado — descartar : {not_exists}/{n} ({not_exists*100//max(n,1)}%)")
    print(f"  Indeterminados — auditar       : {indef}/{n}")
    print(f"\n📄 CSV gravado: {csv_out}")


if __name__ == "__main__":
    main()
