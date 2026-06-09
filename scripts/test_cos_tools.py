#!/usr/bin/env python3
"""Smoke test pras 8 tools do CoS Investigator.

Roda cada tool 1x contra o banco local, imprime resultado compacto.
NÃO é unit test formal — é checagem de fumaça (não trava? retorna shape
esperado?). Pode ser rodado isolado durante dev.

Uso:
    cd /Users/rap/prospect-system
    python scripts/test_cos_tools.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

# Permite rodar de qualquer cwd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Forçar banco local
os.environ.setdefault("USE_LOCAL_DB", "1")

from services.cos_tools import (  # noqa: E402
    search_messages,
    get_messages_with,
    get_overdue_tasks,
    get_calendar,
    get_proposals,
    create_draft_response,
    record_observation,
    escalate_to_user,
    COS_TOOLS,
    execute_tool,
)


CYCLE = f"test-{date.today().isoformat()}"


def _dump(label: str, val):
    print(f"\n--- {label} ---")
    if isinstance(val, (list, dict)):
        s = json.dumps(val, default=str, ensure_ascii=False, indent=2)
        if len(s) > 1200:
            s = s[:1200] + "\n... [truncado]"
        print(s)
    else:
        print(val)


def main():
    print(f"=== Smoke test cos_tools (cycle={CYCLE}) ===")
    print(f"COS_TOOLS exporta {len(COS_TOOLS)} tools")
    for t in COS_TOOLS:
        print(f"  - {t['name']}")

    # READ TOOLS
    r1 = search_messages(CYCLE, iteration=0, query="reuniao", hours=720)
    _dump(f"search_messages('reuniao', 720h) → {len(r1)} resultados", r1[:3])

    r2 = get_messages_with(CYCLE, iteration=0, contact_id_or_name="renato", hours=720)
    _dump(f"get_messages_with('renato', 720h)", {
        "contact_id": r2.get("contact_id"),
        "nome": r2.get("nome"),
        "n_mensagens": len(r2.get("mensagens", [])),
        "primeira": (r2.get("mensagens") or [{}])[0] if r2.get("mensagens") else None,
    })

    r3 = get_overdue_tasks(CYCLE, iteration=0, limit=5)
    _dump(f"get_overdue_tasks(limit=5) → {len(r3)} resultados", r3[:3])

    today = date.today()
    next_week = today + timedelta(days=7)
    r4 = get_calendar(CYCLE, iteration=0, date_start=today.isoformat(), date_end=next_week.isoformat())
    _dump(f"get_calendar({today}..{next_week}) → {len(r4)} eventos", r4[:3])

    r5 = get_proposals(CYCLE, iteration=0, status="pending")
    _dump(f"get_proposals(status=pending) → {len(r5)} propostas", r5[:3])

    # WRITE TOOLS — só executa se tiver pelo menos 1 contato
    contact_id_para_draft = None
    if r2.get("contact_id"):
        contact_id_para_draft = r2["contact_id"]

    if contact_id_para_draft:
        r6 = create_draft_response(
            CYCLE, iteration=0,
            contact_id=contact_id_para_draft,
            channel="whatsapp",
            text_draft="(teste smoke) Oi, segue update.",
            motivo="(teste smoke) verificar pipeline de draft",
        )
        _dump("create_draft_response → ", r6)
    else:
        print("\n[skip] create_draft_response — sem contato resolvido")

    r7 = record_observation(
        CYCLE, iteration=0,
        texto="(teste smoke) Observacao gerada pelo smoke test",
        frente=1,
        refs={"smoke_test": True},
    )
    _dump("record_observation →", r7)

    r8 = escalate_to_user(
        CYCLE, iteration=0,
        texto="(teste smoke) Decisao Y/N de exemplo",
        motivo="(teste smoke) verificar pipeline de one_way",
        prioridade=3,
        refs={"smoke_test": True},
    )
    _dump("escalate_to_user →", r8)

    # Testa o dispatcher também
    via_dispatch = execute_tool(
        "search_messages",
        {"query": "Vallen", "hours": 168},
        CYCLE,
        iteration=99,
    )
    _dump(f"execute_tool(search_messages, 'Vallen', 7d) → {len(via_dispatch)} resultados", via_dispatch[:2])

    print("\n=== Smoke test concluído ===")
    print(f"Verifique: psql -d intel -c \"SELECT * FROM cos_action_log WHERE cycle_id='{CYCLE}' ORDER BY id;\"")


if __name__ == "__main__":
    main()
