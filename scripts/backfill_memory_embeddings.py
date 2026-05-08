#!/usr/bin/env python3
"""
Backfill de embeddings em system_memories.

Itera memorias com embedding IS NULL, gera embedding via Voyage AI, salva.
Idempotente: re-rodar so pega o que faltou.

Usage:
    # Local (banco local)
    python scripts/backfill_memory_embeddings.py

    # Producao (Neon) — exporta DATABASE_URL primeiro
    DATABASE_URL='postgres://...' python scripts/backfill_memory_embeddings.py

Requirements:
    - VOYAGE_API_KEY no env
    - Migration 006 ja rodou (coluna `embedding` existe)

Reporta progresso a cada memoria.
"""
import os
import sys
import time

# Permite rodar de qualquer lugar
HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, APP_DIR)

# Carrega .env (mesmo padrao do dev.sh)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(HERE), ".env"))
    load_dotenv(os.path.join(os.path.dirname(HERE), ".env.local"), override=False)
except ImportError:
    pass

# Force banco local se nao especificado, pro caso de rodar sem set
if not os.getenv("DATABASE_URL") and not os.getenv("POSTGRES_URL"):
    os.environ["DATABASE_URL"] = "postgresql://localhost:5432/intel"

from database import get_db  # noqa: E402
from services.embeddings import embed_sync, embedding_to_pg_literal, is_enabled  # noqa: E402


def main() -> int:
    if not is_enabled():
        print("ERRO: VOYAGE_API_KEY nao configurada — defina no .env e tente de novo.", file=sys.stderr)
        return 1

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, titulo, conteudo
            FROM system_memories
            WHERE embedding IS NULL
            ORDER BY id ASC
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]

    total = len(rows)
    if total == 0:
        print("Tudo backfillado — nenhuma memoria pendente.")
        return 0

    print(f"Backfill: {total} memorias pendentes.")
    ok = 0
    failed = 0

    for i, r in enumerate(rows, 1):
        text = f"{r['titulo']}\n\n{r['conteudo']}"
        t0 = time.time()
        try:
            vec = embed_sync(text, input_type="document")
        except Exception as e:
            print(f"  [{i}/{total}] #{r['id']} ERRO: {e}")
            failed += 1
            continue

        if not vec:
            print(f"  [{i}/{total}] #{r['id']} FALHA (vec=None)")
            failed += 1
            continue

        literal = embedding_to_pg_literal(vec)
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE system_memories SET embedding = %s::vector WHERE id = %s",
                    (literal, r["id"]),
                )
                conn.commit()
        except Exception as e:
            print(f"  [{i}/{total}] #{r['id']} UPDATE error: {e}")
            failed += 1
            continue

        elapsed_ms = int((time.time() - t0) * 1000)
        print(f"  [{i}/{total}] #{r['id']} {r['titulo'][:60]!r} OK ({elapsed_ms}ms)")
        ok += 1

    print(f"\nResultado: {ok} ok, {failed} falhas, {total} total.")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
