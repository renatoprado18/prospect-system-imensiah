#!/usr/bin/env python3
"""
Script para merge automatico de duplicados com score > 0.9
"""
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
from services.duplicados import encontrar_duplicados, merge_contatos

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

stats = {"merged": 0, "skipped": 0, "errors": 0}

for dup in duplicates:
    score = dup.get("score", 0)
    c1 = dup.get("contact1", {})
    c2 = dup.get("contact2", {})
    
    if score < 0.9:
        continue
    
    # Decidir qual manter (o com mais dados)
    def count_fields(c):
        count = 0
        if c.get("empresa"): count += 1
        if c.get("cargo"): count += 1
        if c.get("linkedin"): count += 1
        if c.get("foto_url"): count += 1
        if c.get("aniversario"): count += 1
        count += (c.get("total_interacoes") or 0)
        return count
    
    c1_score = count_fields(c1)
    c2_score = count_fields(c2)
    
    keep_id = c1["id"] if c1_score >= c2_score else c2["id"]
    merge_id = c2["id"] if c1_score >= c2_score else c1["id"]
    
    print(f"\nMerging: {c1['nome']} <-> {c2['nome']} (score: {score:.2f})", flush=True)
    print(f"  Keep ID: {keep_id}, Merge ID: {merge_id}", flush=True)
    
    try:
        result = merge_contatos(keep_id, merge_id)
        if result.get("success"):
            stats["merged"] += 1
            print(f"  Sucesso!", flush=True)
        else:
            stats["skipped"] += 1
            print(f"  Pulado: {result.get('error', 'Unknown')}", flush=True)
    except Exception as e:
        stats["errors"] += 1
        print(f"  Erro: {e}", flush=True)

print("\n" + "=" * 60, flush=True)
print("RESULTADO", flush=True)
print("=" * 60, flush=True)
print(f"Merged: {stats['merged']}", flush=True)
print(f"Skipped: {stats['skipped']}", flush=True)
print(f"Errors: {stats['errors']}", flush=True)
