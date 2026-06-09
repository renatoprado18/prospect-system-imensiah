#!/usr/bin/env python3
"""Roda um ciclo do CoS Investigator localmente (banco local).

Uso:
    cd /Users/rap/prospect-system
    python3 scripts/run_cos_investigator.py [cycle_id]

Se cycle_id não passado, usa "test-<timestamp>" pra não colidir com
ciclo de produção. Use cycle_id real ("2026-06-10-morning") só se você
quer testar o pipeline end-to-end completo.

Requer ANTHROPIC_API_KEY no env.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime

# Path setup
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

os.environ.setdefault("USE_LOCAL_DB", "1")

# Importa após path setup
from services.cos_investigator import run_investigator_cycle  # noqa: E402


async def main():
    if len(sys.argv) > 1:
        cycle_id = sys.argv[1]
    else:
        cycle_id = f"test-{int(datetime.now().timestamp())}"

    print(f"=== Rodando CoS Investigator (cycle_id={cycle_id}) ===")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("AVISO: ANTHROPIC_API_KEY não setada — agent vai retornar status='skipped'")

    result = await run_investigator_cycle(cycle_id=cycle_id)
    print("\n--- RESULTADO ---")
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))

    print("\n--- COMANDOS DE VERIFICAÇÃO ---")
    print(f"psql -d intel -c \"SELECT id, categoria, frente, prioridade, LEFT(texto, 100) FROM cos_briefing_items WHERE cycle_id='{cycle_id}' ORDER BY id;\"")
    print(f"psql -d intel -c \"SELECT id, tool_name, iteration, duration_ms FROM cos_action_log WHERE cycle_id='{cycle_id}' ORDER BY id;\"")


if __name__ == "__main__":
    asyncio.run(main())
