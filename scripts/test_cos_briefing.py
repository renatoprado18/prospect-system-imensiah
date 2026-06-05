#!/usr/bin/env python3
"""Teste local do generate_cos_briefing_narrative — bypassa cron/HTTP."""
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from services.briefings import generate_cos_briefing_narrative


# Dados sinteticos plausiveis pra ver o output do CoS
TODAY_TASKS = [
    "Enviar devolutiva tecnica Vallen RACI semana",
    "Revisar rascunho artigo LinkedIn imensIAH",
    "Marcar 90min de brief Wadhwani na proxima quarta",
    "Responder Cecilia Zanotti (Despertar)",
]

EVENTS = [
    {"summary": "Vallen Clinic - reuniao RACI", "start_datetime": datetime(2026, 6, 6, 10, 0)},
    {"summary": "Almeida Prado - operacao firma", "start_datetime": datetime(2026, 6, 6, 14, 0)},
    {"summary": "Emma - jantar", "start_datetime": datetime(2026, 6, 6, 20, 0)},
]

EDITORIAL_TODAY = [
    {"article_title": "PMEs nao precisam de mais ferramentas — precisam de menos decisoes", "data_publicacao": datetime(2026, 6, 6, 11, 0)},
]


async def main() -> int:
    now = datetime(2026, 6, 6, 8, 0)
    text = await generate_cos_briefing_narrative(
        overdue_count=3,
        today_tasks=TODAY_TASKS,
        events=EVENTS,
        editorial_today=EDITORIAL_TODAY,
        needs_metrics_count=2,
        proposals_count=7,
        agent_total_24h=12,
        no_post_alert=None,
        cost_mtd=None,
        pending_count=15,
        pending_top=[
            "WA grupo Assespro: msg de Cecilia (mention)",
            "Email Wadhwani — confirma reuniao decisao",
            "Linkedin DM founder PME interessado em imensIAH",
        ],
        now=now,
    )
    if not text:
        print("[ERRO] narrative nao gerada (None) — checar API key, cos_config, ou logs")
        return 1
    print("=" * 60)
    print("BRIEFING COS GERADO")
    print("=" * 60)
    print(text)
    print("=" * 60)
    print(f"Tamanho: {len(text)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
