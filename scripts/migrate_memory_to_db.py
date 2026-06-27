"""F1 — Migra memórias .md do Claude Code pra system_memories no DB.

Source: /Users/rap/.claude/projects/-Users-rap-prospect-system/memory/*.md
Target: system_memories (Neon prod) com embedding Voyage voyage-4-lite

Estrategia: DELETE fonte='claude_code_migration' + INSERT all. Idempotente.
Subsequentes runs sincronizam o estado atual dos .md (Claude Code = source of
truth, DB = mirror).

Skip type='user' (carrega no CLAUDE.md system prompt, redundante migrar).

Usage:
  source .venv/bin/activate
  python3 scripts/migrate_memory_to_db.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import yaml

# Setup path pra importar app modules
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from database import get_db
from services.embeddings import VOYAGE_API_URL, VOYAGE_MODEL, embedding_to_pg_literal


# Voyage free tier: 3 RPM + 10K TPM. Free w/ payment method: 60 RPM + 1M TPM.
# Pra free, batch=5 com 25s gap = 12 RPM efetivo (ainda dentro de 10K TPM).
# Pra free-cc, batch=50 com 1s gap = OK em qualquer cenario.
VOYAGE_BATCH_SIZE = 5
VOYAGE_BATCH_THROTTLE_S = 25.0
VOYAGE_BATCH_TIMEOUT_S = 60.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate_memory")

MEMORY_DIR = Path("/Users/rap/.claude/projects/-Users-rap-prospect-system/memory")
FONTE = "claude_code_migration"
SKIP_TYPES = {"user"}


def embed_chunk(texts: list[str], api_key: str) -> list[list[float]] | None:
    """Embed um chunk de textos via Voyage. Retorna lista de vetores ou None."""
    try:
        with httpx.Client(timeout=VOYAGE_BATCH_TIMEOUT_S) as client:
            resp = client.post(
                VOYAGE_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": VOYAGE_MODEL,
                    "input": texts,
                    "input_type": "document",
                },
            )
        if resp.status_code == 429:
            return None  # rate limited, caller retries
        if resp.status_code != 200:
            logger.error(f"Voyage chunk falhou {resp.status_code}: {resp.text[:300]}")
            return None
        data = resp.json().get("data", [])
        if len(data) != len(texts):
            logger.error(f"Chunk retorno mismatch {len(data)} vs {len(texts)}")
            return None
        return [item.get("embedding", []) for item in data]
    except Exception as e:
        logger.error(f"Voyage chunk exception: {e}")
        return None


def backfill_embeddings(throttle_s: float = VOYAGE_BATCH_THROTTLE_S) -> None:
    """Pega rows fonte=claude_code_migration sem embedding e preenche em chunks
    throttled. Idempotente — pode ser rerunado ate completar todos."""
    api_key = os.getenv("VOYAGE_API_KEY", "").strip()
    if not api_key:
        logger.error("VOYAGE_API_KEY ausente — skip backfill")
        return

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, titulo, conteudo FROM system_memories "
            "WHERE fonte = %s AND embedding IS NULL ORDER BY id",
            (FONTE,),
        )
        rows = cur.fetchall()

    if not rows:
        logger.info("Backfill: nada pendente")
        return

    total = len(rows)
    logger.info(f"Backfill: {total} rows sem embedding — chunks de {VOYAGE_BATCH_SIZE}, gap {throttle_s}s")
    filled = 0
    skipped = 0
    for start in range(0, total, VOYAGE_BATCH_SIZE):
        chunk_rows = rows[start : start + VOYAGE_BATCH_SIZE]
        texts = [f"{r['titulo']}\n\n{r['conteudo']}" for r in chunk_rows]
        vectors = embed_chunk(texts, api_key)
        if vectors is None:
            # Retry uma vez apos throttle dobrado
            logger.warning(f"  chunk {start} falhou — retry em {throttle_s*2}s")
            time.sleep(throttle_s * 2)
            vectors = embed_chunk(texts, api_key)
        if vectors is None:
            logger.warning(f"  chunk {start} skip definitivo")
            skipped += len(chunk_rows)
        else:
            with get_db() as conn:
                cur = conn.cursor()
                for row, vec in zip(chunk_rows, vectors):
                    cur.execute(
                        "UPDATE system_memories SET embedding = %s::vector "
                        "WHERE id = %s",
                        (embedding_to_pg_literal(vec), row["id"]),
                    )
                conn.commit()
            filled += len(chunk_rows)
        done = start + len(chunk_rows)
        logger.info(f"  progress {done}/{total} (filled={filled}, skipped={skipped})")
        if done < total:
            time.sleep(throttle_s)

    logger.info(f"Backfill concluido: {filled} embeddings ok, {skipped} skipped")


def _parse_frontmatter_simple(fm_text: str) -> dict:
    """Fallback parser quando YAML falha por colons unquoted em description.
    Line-based 'key: value' — pega so primeiro `:` de cada linha."""
    out: dict = {}
    for line in fm_text.split("\n"):
        line = line.rstrip()
        if not line or ":" not in line:
            continue
        # Linhas com indent (multiline) — agrega no key anterior se existir
        if line[0] in " \t":
            continue  # ignora multilinha por simplicidade
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # Remove aspas externas
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        out[key] = value
    return out


def parse_md_file(path: Path) -> dict | None:
    """Parse YAML frontmatter + body. Returns dict, ou None se irreparavel.
    Fallback line-based pra cobrir colons unquoted em description."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        logger.warning(f"{path.name}: sem frontmatter — pulando")
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        logger.warning(f"{path.name}: frontmatter mal fechado — pulando")
        return None
    meta: dict | None = None
    try:
        loaded = yaml.safe_load(parts[1])
        if isinstance(loaded, dict):
            meta = loaded
    except yaml.YAMLError:
        pass
    if meta is None:
        # Fallback: parser simples line-based
        meta = _parse_frontmatter_simple(parts[1])
        logger.debug(f"{path.name}: fallback parser usado")
    if not meta:
        logger.warning(f"{path.name}: frontmatter vazio mesmo com fallback — pulando")
        return None
    body = parts[2].strip()
    return {
        "name": meta.get("name") or path.stem,
        "description": meta.get("description") or "",
        "type": meta.get("type") or "unknown",
        "body": body,
        "file": path.name,
    }


def clear_previous(dry_run: bool) -> int:
    """Remove migracoes anteriores. Idempotencia: cada run reflete o estado atual dos .md."""
    if dry_run:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM system_memories WHERE fonte = %s", (FONTE,))
            return int(cur.fetchone()["c"])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM system_memories WHERE fonte = %s RETURNING id", (FONTE,))
        deleted = len(cur.fetchall())
        conn.commit()
        return deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="So lista o que faria, sem tocar no DB")
    ap.add_argument("--limit", type=int, default=None, help="Processa so N arquivos (debug)")
    ap.add_argument("--skip-embed", action="store_true", help="Skip backfill embedding (so insert)")
    ap.add_argument("--only-embed", action="store_true", help="Pula insert, so backfill embedding")
    ap.add_argument("--embed-throttle", type=float, default=VOYAGE_BATCH_THROTTLE_S,
                    help=f"Gap entre chunks de embedding em segundos (default {VOYAGE_BATCH_THROTTLE_S})")
    args = ap.parse_args()

    if args.only_embed:
        backfill_embeddings(throttle_s=args.embed_throttle)
        return

    if not MEMORY_DIR.is_dir():
        logger.error(f"Memory dir nao existe: {MEMORY_DIR}")
        sys.exit(1)

    files = sorted(
        p for p in MEMORY_DIR.glob("*.md")
        if p.name != "MEMORY.md"
    )
    if args.limit:
        files = files[: args.limit]

    logger.info(f"Encontrados {len(files)} arquivos .md")

    parsed = []
    skip_count = 0
    fail_count = 0
    for path in files:
        rec = parse_md_file(path)
        if rec is None:
            fail_count += 1
            continue
        if rec["type"] in SKIP_TYPES:
            skip_count += 1
            logger.debug(f"  skip {rec['file']} (type={rec['type']})")
            continue
        parsed.append(rec)

    # Conta por tipo
    by_type: dict[str, int] = {}
    for r in parsed:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    logger.info(
        f"Para migrar: {len(parsed)} | "
        f"skip (type in {SKIP_TYPES}): {skip_count} | parse fail: {fail_count}"
    )
    logger.info(f"Breakdown por tipo: {by_type}")

    # Clear previous
    deleted = clear_previous(args.dry_run)
    logger.info(f"{'(dry-run) ' if args.dry_run else ''}Migrações anteriores: {deleted} linhas {'a remover' if args.dry_run else 'removidas'}")

    if args.dry_run:
        logger.info("DRY-RUN — nenhuma escrita feita. Sample dos 3 primeiros:")
        for r in parsed[:3]:
            logger.info(
                f"  [{r['type']}] {r['name']!r} — desc={r['description'][:60]!r} "
                f"body={len(r['body'])}c"
            )
        return

    # Constroi conteudo final
    for r in parsed:
        conteudo = r["description"]
        if r["body"]:
            conteudo = f"{r['description']}\n\n{r['body']}" if r["description"] else r["body"]
        r["conteudo"] = conteudo

    # 1. Insert sem embedding (rapido — DB only). Search hybrid fallback pra
    # keyword enquanto embedding nao chega.
    ok = 0
    err = 0
    with get_db() as conn:
        cur = conn.cursor()
        for r in parsed:
            try:
                cur.execute(
                    """
                    INSERT INTO system_memories (
                        titulo, conteudo, tipo, tags, fonte
                    )
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    RETURNING id
                    """,
                    (
                        r["name"][:500],
                        r["conteudo"],
                        r["type"],
                        json.dumps([r["file"]]),
                        FONTE,
                    ),
                )
                if cur.fetchone():
                    ok += 1
            except Exception as e:
                err += 1
                logger.warning(f"  {r['file']}: insert falhou — {e}")
        conn.commit()
    logger.info(f"Insert (sem embedding) concluido: {ok} OK, {err} falhas")

    # 2. Backfill embeddings (throttled — respeita 3 RPM + 10K TPM do free tier)
    if not args.skip_embed:
        backfill_embeddings(throttle_s=args.embed_throttle)

    logger.info(f"Migracao concluida: {ok} OK, {err} falhas")

    # Quick sanity check
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS with_embed "
            "FROM system_memories WHERE fonte = %s",
            (FONTE,),
        )
        row = cur.fetchone()
        logger.info(
            f"DB check: {row['total']} migradas, {row['with_embed']} com embedding"
        )


if __name__ == "__main__":
    main()
