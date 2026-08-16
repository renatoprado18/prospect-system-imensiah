#!/usr/bin/env python3
"""
Script para merge automatico de duplicados com score > 0.9
"""
import asyncio
import os
import sys
from pathlib import Path

# Load .env file
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
from services.duplicados import encontrar_duplicados, merge_par

print("=" * 60, flush=True)
print("MERGE AUTOMATICO DE DUPLICADOS (score > 0.9)", flush=True)
print("=" * 60, flush=True)

# Buscar duplicados com threshold alto
result = encontrar_duplicados(threshold=0.9, limit=100)

duplicates = result.get("duplicates", [])
print(f"\nDuplicados com score > 0.9: {len(duplicates)}", flush=True)

if not duplicates:
    print("Nenhum duplicado com score > 0.9 encontrado.", flush=True)
    sys.exit(0)

stats = {"merged": 0, "skipped": 0, "errors": 0, "google_ok": 0, "google_falhou": 0}


def count_fields(c):
    """Qual das duas fichas tem mais dados — essa sobrevive."""
    count = 0
    if c.get("empresa"): count += 1
    if c.get("cargo"): count += 1
    if c.get("linkedin"): count += 1
    if c.get("foto_url"): count += 1
    if c.get("aniversario"): count += 1
    count += (c.get("total_interacoes") or 0)
    return count


async def executar():
    """O loop virou async por UM motivo: `merge_par` propaga ao Google.

    Antes este script chamava `merge_contatos` direto, que só mexe no banco
    local. A ficha absorvida continuava viva no Google — e como o sync completo
    traz de volta o que existe lá, a duplicata REAPARECIA no INTEL na rodada
    seguinte. O mutirão desfazia à noite o que fazia de dia, e ninguém via.
    """
    for dup in duplicates:
        score = dup.get("score", 0)
        c1 = dup.get("contact1", {})
        c2 = dup.get("contact2", {})

        if score < 0.9:
            continue

        c1_score = count_fields(c1)
        c2_score = count_fields(c2)

        keep_id = c1["id"] if c1_score >= c2_score else c2["id"]
        merge_id = c2["id"] if c1_score >= c2_score else c1["id"]

        print(f"\nMerging: {c1['nome']} <-> {c2['nome']} (score: {score:.2f})", flush=True)
        print(f"  Keep ID: {keep_id}, Merge ID: {merge_id}", flush=True)

        try:
            result = await merge_par(keep_id, merge_id)
            if result.get("error"):
                stats["skipped"] += 1
                print(f"  Pulado: {result['error']}", flush=True)
                continue

            stats["merged"] += 1
            # A propagação é relatada SEPARADO do merge. Somar as duas num
            # "Sucesso!" esconderia exatamente o defeito que este script tinha:
            # merge local perfeito e Google intocado.
            prop = result.get("google_propagation")
            if prop is None:
                print("  Sucesso (sem propagação pedida)", flush=True)
            elif prop.get("error"):
                stats["google_falhou"] += 1
                print(f"  Merge OK · ⚠️ GOOGLE FALHOU: {prop['error']}", flush=True)
            else:
                stats["google_ok"] += 1
                print("  Sucesso! (local + Google)", flush=True)
        except Exception as e:
            stats["errors"] += 1
            print(f"  Erro: {e}", flush=True)


asyncio.run(executar())

print("\n" + "=" * 60, flush=True)
print("RESULTADO", flush=True)
print("=" * 60, flush=True)
print(f"Merged: {stats['merged']}", flush=True)
print(f"  · propagados ao Google: {stats['google_ok']}", flush=True)
# Este número tem que aparecer mesmo quando é zero: merge que não propagou
# volta como duplicata no próximo sync, e um relatório que só conta sucesso
# deixaria isso invisível até alguém reparar no contato repetido.
print(f"  · propagação FALHOU:    {stats['google_falhou']}", flush=True)
print(f"Skipped: {stats['skipped']}", flush=True)
print(f"Errors: {stats['errors']}", flush=True)
