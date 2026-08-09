#!/usr/bin/env python3
"""
Backfill de embeddings em system_memories.

Itera memorias com embedding IS NULL, gera embedding via Voyage AI, salva.
Idempotente: re-rodar so pega o que faltou.

POR QUE ELE GANHOU PAUSA E FILTRO (08/08/26, sessao da #999768). A conta Voyage
e 3 RPM. Este script disparava em rajada, tomava 429 e contava o 429 como
"falha" — seguia em frente e imprimia um resultado final que parecia trabalho
feito. Foi assim que 54 memorias ficaram sem embedding sem ninguem notar; uma
delas, a `whatsapp_lid_migration`, so aparecia por palavra-chave havia semanas.

E ganhou `--fonte` porque 46 dos 54 NULL nao eram memoria: 28 eram spikes
diarios de custo, 10 fichas de contato semeadas. Backfillar tudo gastava 22
minutos de cota para embeddar telemetria que ninguem busca por sentido.

Usage:
    # So as memorias vindas dos .md do Claude Code (o caso normal)
    python scripts/backfill_memory_embeddings.py --fonte claude_code_migration

    # Producao (Neon) — exporta DATABASE_URL primeiro
    DATABASE_URL='postgres://...' python scripts/backfill_memory_embeddings.py

Requirements:
    - VOYAGE_API_KEY no env
    - Migration 006 ja rodou (coluna `embedding` existe)
"""
import argparse
import os
import sys
import time

# 3 RPM => 20s por chamada e o teto exato. 25 da folga pro jitter da rede sem
# transformar um backfill de 9 itens em cafe.
PAUSA_PADRAO = 25.0
# A conta Voyage esta no plano SEM metodo de pagamento: 3 RPM **e 10K TPM**.
# O teto que morde nos historicos e o de TOKENS, nao o de requisicoes — 30.000
# caracteres estouram os 10K TPM sozinhos e voltam 429. Medido em 08/08 nos tres
# `*_historico` (277 KB, 205 KB, 113 KB), que falharam mesmo sozinhos na fila.
#
# 8.000 caracteres (~2K tokens) passam com folga. Truncar NAO perde o memo — o
# conteudo inteiro continua no banco e a busca por palavra-chave le tudo. Mas
# seja honesto sobre o que isso compra: num historico de 277 KB o vetor cobre 3%
# do texto, entao ele responde a "que documento e esse" e nao a "o que foi dito
# no meio dele". Recall semantico de arquivo morto grande exige CHUNKING (partir
# em pedacos, um vetor cada) — backlog, nao coberto aqui.
MAX_CHARS_EMBED = 8000

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


def embeddar(text: str, tentativas: int = 3, pausa: float = PAUSA_PADRAO):
    """Chama o Voyage tratando recusa por cota como ESPERA, nao como falha.

    ⚠️ `embed_sync` ENGOLE a excecao: loga e devolve None. Um retry montado sobre
    `except` nunca dispara — foi o que escrevi primeiro, e o 429 seguiu contando
    como falha definitiva. Por isso o retry aqui olha o RETORNO (None), nao a
    excecao. Vale pro caso geral: quando a funcao chamada converte erro em valor,
    tratar erro por except e nao tratar nada.

    None tambem cobre "texto vazio" e "chave ausente", que nao melhoram com
    espera — perder alguns segundos nesses casos raros e mais barato que deixar
    passar um 429 disfarcado de falha.
    """
    for n in range(1, tentativas + 1):
        try:
            vec = embed_sync(text[:MAX_CHARS_EMBED], input_type="document")
        except Exception as e:
            return None, e
        if vec:
            return vec, None
        if n < tentativas:
            espera = pausa * n
            print(f"        sem vetor (provavel cota 3 RPM / 10K TPM) — espera {espera:.0f}s "
                  f"e tenta de novo ({n}/{tentativas - 1})")
            time.sleep(espera)
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonte", help="so memorias desta fonte (ex: claude_code_migration)")
    ap.add_argument("--pausa", type=float, default=PAUSA_PADRAO,
                    help=f"segundos entre chamadas (padrao {PAUSA_PADRAO:.0f}; a conta e 3 RPM)")
    args = ap.parse_args()

    if not is_enabled():
        print("ERRO: VOYAGE_API_KEY nao configurada — defina no .env e tente de novo.", file=sys.stderr)
        return 1

    where = "embedding IS NULL" + (" AND fonte = %s" if args.fonte else "")
    params = (args.fonte,) if args.fonte else ()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id, titulo, conteudo FROM system_memories WHERE {where} ORDER BY id ASC",
            params,
        )
        rows = [dict(r) for r in cursor.fetchall()]

    total = len(rows)
    if total == 0:
        print("Tudo backfillado — nenhuma memoria pendente.")
        return 0

    print(f"Backfill: {total} memorias pendentes"
          + (f" (fonte={args.fonte})" if args.fonte else "")
          + f" · pausa {args.pausa:.0f}s entre chamadas · ~{total * args.pausa / 60:.0f} min")
    ok = 0
    failed = 0

    for i, r in enumerate(rows, 1):
        if i > 1:
            time.sleep(args.pausa)
        text = f"{r['titulo']}\n\n{r['conteudo']}"
        t0 = time.time()
        vec, err = embeddar(text, pausa=args.pausa)
        if err is not None:
            print(f"  [{i}/{total}] #{r['id']} ERRO: {err}")
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
