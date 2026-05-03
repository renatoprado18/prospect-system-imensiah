#!/usr/bin/env python3
"""
Classifica mensagens incoming recentes (ultimos 30 dias) ainda nao classificadas.

Pipeline hibrido (rule + LLM Haiku) — pega mensagens curtas tipo "obrigada 🙏" /
"coloquei na agenda" via regra (rapido/free) e delega ambiguas pro LLM.

Custo estimado: ~$0.0001/msg pelo Haiku => 1000 msgs ~= $0.10 total.

Uso:
    python scripts/classify_recent_messages.py            # LIMIT 1000
    python scripts/classify_recent_messages.py --no-limit # roda tudo
    python scripts/classify_recent_messages.py --days 60  # custom janela
"""
import os
import sys
import asyncio
import argparse
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                value = value.strip('"').strip("'")
                os.environ.setdefault(key, value)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db
from services.message_classifier import classify

# Custo aproximado Haiku 4.5: $1/1M input + $5/1M output.
# Prompt ~250 tokens + resposta ~30 tokens => ~$0.0004 por chamada LLM.
# Mas a maioria cai no rule (free) — media efetiva ~$0.0001/msg.
COST_PER_LLM_MSG_USD = 0.0004


async def main():
    parser = argparse.ArgumentParser(description="Classifica mensagens incoming")
    parser.add_argument("--days", type=int, default=30, help="Janela em dias (default 30)")
    parser.add_argument("--no-limit", action="store_true", help="Sem cap (default 1000)")
    parser.add_argument("--limit", type=int, default=1000, help="Cap (default 1000)")
    parser.add_argument("--dry-run", action="store_true", help="So lista, nao classifica")
    args = parser.parse_args()

    limit_clause = "" if args.no_limit else f"LIMIT {args.limit}"

    print(f"=== classify_recent_messages ===")
    print(f"Janela: ultimos {args.days} dias")
    print(f"Limit: {'sem limite' if args.no_limit else args.limit}")
    print(f"Dry-run: {args.dry_run}")
    print()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT m.id, m.conteudo, COALESCE(c.nome, '') AS sender_name,
                   m.enviado_em
            FROM messages m
            JOIN contacts c ON c.id = m.contact_id
            LEFT JOIN message_classifications mc
                ON mc.message_id = m.id AND mc.source_table = 'messages'
            WHERE m.direcao = 'incoming'
              AND m.enviado_em > NOW() - INTERVAL '{int(args.days)} days'
              AND mc.message_id IS NULL
              AND m.conteudo IS NOT NULL
              AND length(trim(m.conteudo)) > 0
            ORDER BY m.enviado_em DESC
            {limit_clause}
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]

    print(f"Encontradas {len(rows)} mensagens nao classificadas")

    if args.dry_run:
        for r in rows[:20]:
            content = (r["conteudo"] or "")[:80].replace("\n", " ")
            print(f"  msg={r['id']} de {r['sender_name'][:30]:30} | {content}")
        if len(rows) > 20:
            print(f"  ... e mais {len(rows) - 20} mensagens")
        return

    if not rows:
        print("Nada pra fazer.")
        return

    stats = {"rule_pass": 0, "llm_pass": 0, "errors": 0, "needs_reply": 0, "no_reply": 0}

    for i, row in enumerate(rows, 1):
        try:
            res = await classify(
                message_id=row["id"],
                source_table="messages",
                text=row["conteudo"] or "",
                sender_name=row.get("sender_name") or "",
            )
            method = res.get("method", "?")
            if method == "rule":
                stats["rule_pass"] += 1
            elif method == "llm":
                stats["llm_pass"] += 1
            if res.get("requires_reply"):
                stats["needs_reply"] += 1
            else:
                stats["no_reply"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"  ERROR msg={row['id']}: {e}")

        if i % 50 == 0:
            print(f"  ... {i}/{len(rows)} processadas")

    print()
    print("=== RESULTADO ===")
    print(f"Rule (free):       {stats['rule_pass']}")
    print(f"LLM (Haiku):       {stats['llm_pass']}")
    print(f"Errors:            {stats['errors']}")
    print(f"Precisa resposta:  {stats['needs_reply']}")
    print(f"Nao precisa:       {stats['no_reply']}")
    cost = stats["llm_pass"] * COST_PER_LLM_MSG_USD
    print(f"Custo estimado:    ${cost:.4f} ({stats['llm_pass']} chamadas LLM)")


if __name__ == "__main__":
    asyncio.run(main())
