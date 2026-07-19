"""
INTEL Worker - Railway
Handles bot message processing, audio transcription, and image analysis.
Runs on Railway with no timeout limit.
"""
import os
import sys
import json
import asyncio
import logging
import httpx
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

SP_TZ = ZoneInfo("America/Sao_Paulo")
DIAS_PT = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]


def _now_sp():
    return datetime.now(SP_TZ)


def _format_sp_datetime(dt: datetime = None) -> str:
    if dt is None:
        dt = _now_sp()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=SP_TZ)
    else:
        dt = dt.astimezone(SP_TZ)
    return f"{dt.strftime('%Y-%m-%d')} {DIAS_PT[dt.weekday()]} {dt.strftime('%H:%M')}"
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="INTEL Worker")

# Marker pra confirmar versao do codigo deployado (atualiza ao mudar logica chunked)
WORKER_BUILD = "gmail-sync-chunked-v7-internal-loop"
logger.info(f"INTEL Worker started — build={WORKER_BUILD}")


@app.get("/version")
async def version():
    return {"build": WORKER_BUILD}

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
CONSELHOOS_DATABASE_URL = os.getenv("CONSELHOOS_DATABASE_URL", "")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
INTEL_BOT_INSTANCE = os.getenv("INTEL_BOT_INSTANCE", "intel-bot")
INTEL_API_URL = os.getenv("INTEL_API_URL", "https://intel.almeida-prado.com")
# WORKER_SECRET sem fallback hardcoded (08/07/2026). Env ausente:
# validators rejeitam 401, senders falham no destino (Vercel valida).
WORKER_SECRET = (os.getenv("WORKER_SECRET") or "").strip()
CRON_SECRET = (os.getenv("CRON_SECRET") or "").strip()
# Tonia e Vercel-only (sem scheduler in-process). Este worker Railway hospeda o
# tick do /delegate/pickup dela (ponte ate F-2 consolidacao Railway). Secret:
# fallback pro CRON_SECRET se a Tonia usar a mesma chave; senao setar
# TONIA_CRON_SECRET no Railway. URL default = dominio prod da Tonia.
TONIA_API_URL = (os.getenv("TONIA_API_URL") or "https://tonia.almeida-prado.com").strip().rstrip("/")
TONIA_CRON_SECRET = (os.getenv("TONIA_CRON_SECRET") or CRON_SECRET or "").strip()

if not WORKER_SECRET:
    logger.error("WORKER_SECRET não configurado — auth de/para o worker vai falhar (sem fallback)")


def _check_worker_secret(provided) -> bool:
    """Validator: env ausente => rejeita (401) e loga erro. Nunca aceita default."""
    if not WORKER_SECRET:
        logger.error("WORKER_SECRET não configurado — request rejeitado (401)")
        return False
    return bool(provided) and str(provided).strip() == WORKER_SECRET

# ============================================================================
# Scheduler (substitui GH Actions crons high-freq)
# ----------------------------------------------------------------------------
# Motivacao: GH Actions schedule sofre drift/skip sob load do runner pool.
# APScheduler in-process num worker sempre-on nao tem drift. Catchup hourly
# do Vercel continua como safety net se o worker reiniciar.
# Jobs sao fire-and-forget GETs pros endpoints /api/cron/* no Vercel,
# autenticados via Bearer CRON_SECRET (mesma chave dos crons Vercel-scheduled).
# ============================================================================
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import debounce

scheduler = AsyncIOScheduler(timezone="UTC")


def _record_cron_heartbeat(job_id: str, http_status: int | None, duration_ms: int) -> None:
    """Insere heartbeat em cron_heartbeats. Defensivo: erros sao logados mas
    nao propagam — telemetria nao pode derrubar o scheduler.

    Schema em scripts/migrations/023_cron_heartbeats.sql. Lido pelo cron
    /api/cron/monitor-cron-health (1x/h) que alerta via WA se gap > 2x interval.
    """
    if not DATABASE_URL:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cron_heartbeats (job_id, http_status, duration_ms, source)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (job_id, http_status, duration_ms, "railway-worker"),
                )
                conn.commit()
    except Exception as e:
        logger.warning(f"scheduler: heartbeat insert failed for {job_id}: {e}")


async def _call_vercel_cron(path: str, job_id: str | None = None) -> None:
    """Dispara endpoint /api/cron/* autenticado. Log status; nao levanta
    exceptions (deixa o scheduler continuar pros jobs proximos).

    Apos o HTTP call, grava heartbeat em cron_heartbeats (defensivo: nao falha
    se DB cair) pro monitor de saude detectar regressao silenciosa."""
    url = f"{INTEL_API_URL.rstrip('/')}{path}"
    headers = {
        "User-Agent": "intel-worker-scheduler/1.0",
        "X-Cron-Source": "railway-worker",
    }
    if CRON_SECRET:
        headers["Authorization"] = f"Bearer {CRON_SECRET}"
    started = datetime.now()
    http_status: int | None = None
    try:
        timeout = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
        http_status = resp.status_code
        logger.info(f"scheduler: {path} -> http {resp.status_code}")
    except Exception as e:
        logger.exception(f"scheduler: {path} failed: {e}")
    finally:
        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        _record_cron_heartbeat(job_id or path, http_status, duration_ms)


# Jobs (path, trigger). Schedules em UTC matching as expressoes originais dos
# .github/workflows/cron-*.yml. Adicionar novos = appendar nesta lista.
_SCHEDULER_JOBS = [
    ("classify-messages", "/api/cron/classify-messages", CronTrigger(minute=15)),
    ("auto-collect-linkedin-metrics", "/api/cron/auto-collect-linkedin-metrics", CronTrigger(minute=0)),
    ("proactive-check", "/api/cron/proactive-check", CronTrigger(minute="*/30")),
    ("run-whatsapp-sync", "/api/cron/run-whatsapp-sync", CronTrigger(minute=5)),
    ("run-social-groups", "/api/cron/run-social-groups", CronTrigger(minute=20)),
    ("agent-intents-tick", "/api/cron/agent-intents-tick", CronTrigger(minute="*/30")),
    ("wa-catchup", "/api/cron/wa-catchup", CronTrigger(minute="*/30")),
    # 11/07/26 — F-2 Passo B: arquiva o binário dos anexos WA no Google Drive
    # (re-baixa da Evolution, sobe pro Drive, grava drive_file_id). Só go-forward
    # (mídia Evolution expira → janela 3 dias). Endpoint roda no Vercel (token
    # Google). Ver services/wa_drive_archive.py.
    ("wa-drive-archive", "/api/cron/wa-drive-archive?limit=25", CronTrigger(minute="*/20")),
    # 15/06/26 KILL SWITCH ARQUITETURA — TODOS jobs CoS proativos desligados.
    # Motivo: 11 agentes LLM rodando em cron geravam 8+ proposals/dia, conflito
    # de propostas, alucinacoes de tools, defesa em recuo. Em rebuild estrutural
    # pra Tonha unica (Sonnet 4.6, single loop, executa silenciosa). Veja
    # ARCHITECTURE_REBUILD.md (a criar). Restaurar SO se rebuild abortar.
    #
    # ("cos-sensor-tick", "/api/cron/cos-sensor-tick", CronTrigger(minute="*/30")),
    # Migrado de vercel.json (Hobby cron limit bloqueava deploy) — 11/06/26
    ("monitor-cron-health", "/api/cron/monitor-cron-health", CronTrigger(minute=0)),
    # Briefings — legados aposentados em 23/06/2026.
    # cos-digest-morning/evening emitem signals; Tonha entrega via tick dedicado.
    # ("daily-morning-briefing", "/api/cron/daily-morning-briefing", CronTrigger(hour=11, minute=7)),
    # ("daily-evening-debriefing", "/api/cron/daily-evening-debriefing", CronTrigger(hour=22, minute=0)),
    # 13/06/2026: migrados de GH Actions apos frustracao com unreliability
    # (top-of-hour drift + Vercel Hobby cron limit). Railway scheduler in-process
    # = 100% reliable, custo $0 adicional (worker ja roda 24/7).
    ("process-scheduled-actions", "/api/cron/process-scheduled-actions", CronTrigger(minute="*/5")),
    ("hetzner-evolution-health", "/api/cron/hetzner-evolution-health", CronTrigger(minute="*/10")),
    ("sync-gmail-outbound", "/api/cron/sync-gmail-outbound", CronTrigger(minute="12,42")),
    ("email-triage-sweep", "/api/cron/email-triage-sweep", CronTrigger(minute="7,37")),
    # 24/06/2026 — aging policy: auto-dismiss pendings estourados (silent>2d,
    # important>7d, must_read/urgent>14d, archive_proposed>3d com archive Gmail).
    ("email-triage-aging", "/api/cron/email-triage-aging", CronTrigger(hour=6, minute=0)),
    # 24/06/2026 — gate de auto-archive: avalia FP rate per conta, notifica
    # WA quando elegivel pra ligar (manual via /api/admin/auto-archive-enable).
    ("auto-archive-gate-eval", "/api/cron/auto-archive-gate-eval", CronTrigger(hour=5, minute=0)),
    ("catchup", "/api/cron/catchup", CronTrigger(minute=30)),
    # 28/06/26 — F3.1 WA Triage sweep. Janela 4h batched Sonnet classifica
    # msgs incoming nao classificadas. Shadow mode (status=shadow), sem
    # action_proposal. Migrado de GH Actions (criado nesta mesma sessao por
    # erro de leitura do memo feedback_cron_host_choice). Custo ~$0.50/dia.
    ("wa-triage-sweep", "/api/cron/wa-triage-sweep?window_hours=4",
     CronTrigger(hour="*/4", minute=3)),
    # 28/06/26 — News watchers sessao paralela (4x/dia, modo silent default).
    # Migrado de vercel.json (Hobby plan limita 1 cron/dia, estava bloqueando
    # deploy). 5 watchers ativos, ~15s/run total (modo silent nao chama LLM).
    ("run-project-news-watchers", "/api/cron/run-project-news-watchers",
     CronTrigger(hour="*/6", minute=15)),
    # 28/06/26 — News digest diario (Modo D). 8h BRT = 11h UTC.
    # Agrupa hits 24h por watcher em digest_daily, resume via Sonnet 4.6,
    # manda WA. Renato responde "ok" (arquiva) ou nome/numero (drill).
    # Custo ~$0.005/digest, hits NAO viram action_proposals (skip_proposal
    # no check_watcher). Cron skipado se 0 hits novos.
    #
    # 17/07/26 — TICK OCIOSO DESREGISTRADO. O A3 (porta-voz unico) ja matou o
    # ENVIO do digest self-chat via early-return no endpoint, entao este tick
    # so disparava a toa ~11h UTC (chamada no-op). Removido do scheduler; o
    # endpoint /api/cron/run-daily-news-digest e o service seguem existentes
    # (nada apagado). cron_registry propaga sozinho: _sync_cron_registry marca
    # active=FALSE em jobs ausentes na proxima subida do worker (sem DDL).
    # ("run-daily-news-digest", "/api/cron/run-daily-news-digest",
    #  CronTrigger(hour=11, minute=0)),
    # 22/06/2026: APOSENTADO — cos-digest-morning (10:08 UTC) ja cobre LLM
    # narrative. daily-morning-briefing volta ao static template que e suficiente.
    # ("cos-investigator", "/api/cron/cos-investigator", CronTrigger(hour=10, minute=10)),
    # 15/06/26 FASE 1 REBUILD — detectores deterministas, zero LLM.
    # Le DB, emite signals com signal_hash UNIQUE (idempotente). Brain Sonnet
    # le signals e decide. Ver docs/ARCHITECTURE_REBUILD.md.
    ("detectors-run", "/api/cron/detectors-run", CronTrigger(minute=22)),
    # 12/07/26 — Canary de saldo Anthropic (horario). 1 chamada minima (1 token
    # Haiku, ~$0) detecta 'credit balance too low' e alerta WA DIRETO pro Renato
    # (independente da Tonia, que cai junto quando o saldo zera). State machine
    # deduped: 1 alerta/episodio + WA de recuperacao. Ja roda no
    # platform-costs-daily (pre-briefing); este tick horario pega zeragem no
    # meio do dia (saldo zerou ~10h55 em 10/07, fora da janela diaria). Railway
    # per feedback_cron_host_choice (sub-diario). Ver project_cost_tracker.
    ("anthropic-canary", "/api/cron/anthropic-canary", CronTrigger(minute=33)),
    # 21/06/26 — CoS Context Agent (substituiu CCR routines que tem egress restrito).
    # Patrol horário: 1h de contexto → Claude decide → notifica se urgente → salva digest.
    # Digests narrativos: 12h de contexto → briefing WA. 7h BRT=10h UTC, 18h BRT=21h UTC.
    # 11/07/26 SUNSET GEN-1 PARTE 2 — patrol horário aposentado (cos_sensor reencarnado,
    # mandava 🔴 [CoS Agent] no WA = ruído net-negative). Endpoint também neutralizado na
    # fonte (main.py) contra re-disparo do catchup. Julgamento: Tônia + signals.
    # ("cos-context-agent", "/api/cron/cos-context-agent", CronTrigger(minute=8)),
    # 11/07/26 SUNSET GEN-1 PARTE 3 — digests 🔵 [CoS Agent] 2x/dia aposentados (a Tônia
    # já faz briefing/urgent = redundante). Endpoint também neutralizado na fonte (main.py).
    # ("cos-digest-morning", "/api/cron/cos-digest?mode=morning", CronTrigger(hour=10, minute=8)),
    # ("cos-digest-evening", "/api/cron/cos-digest?mode=evening", CronTrigger(hour=21, minute=8)),
    # 15/06/26 FASE 2A REBUILD — Tonha brain (Sonnet 4.6 + extended thinking)
    # autonomous loop 4x/dia BRT. Ticks de briefing dedicados (23/06/26):
    # morning: 7h15 BRT (10h15 UTC) = 7min após cos-digest-morning (10h08 UTC).
    # evening: 18h15 BRT (21h15 UTC) = 7min após cos-digest-evening (21h08 UTC).
    # Demais ticks processam signals gerais.
    #
    # 10/07/26 — autonomous tick MORTO. Experimento 7d OFF (iniciado 27/06,
    # ~$40/mes, ~25% de falha) encerrado sem religar; alinhado ao sunset da
    # Tonha (05/09). Reactive (Renato chama via WA "patrol") e digests seguem.
    # 17/06/26 — consumer pra delegations(delegated_to='dev'). Fecha criterio 6
    # do ARCHITECTURE_REBUILD. Roda 4x dentro da janela 9-22 BRT (12,15,18,21 BRT
    # = 15,18,21,00 UTC). Default em SHADOW (DEV_DELEGATION_SHADOW=1) — sem custo
    # ate cutover. Cap diario USD + cap por cycle aplicados dentro do service.
    #
    # 19/06/26 VARIANT 2: portado pra in-process (Railway, sem teto 300s) apos
    # monitor detectar 2 runs stuck em status='running' (id 15, 16) por timeout
    # Vercel mid-call ao claude-code-delegator. Lista em _LOCAL_DEV_DELEGATION_JOBS
    # registrada no _start_scheduler — handler local chama
    # dev_delegation_pickup.process_due() direto via psycopg.
    # 17/06/26 — migracao em bloco de 16 crons Vercel daily/weekly pro Railway.
    # Politica feedback_cron_host_choice: criticos que nao podem ser silently
    # dropped → Railway in-process. Horarios UTC identicos aos do vercel.json
    # (remocao do vercel.json na mesma PR pra evitar trigger duplicado).
    ("daily-sync", "/api/cron/daily-sync", CronTrigger(hour=5, minute=0)),
    ("run-daily-ai", "/api/cron/run-daily-ai", CronTrigger(hour=5, minute=15)),
    ("run-auto-enrich", "/api/cron/run-auto-enrich", CronTrigger(hour=5, minute=25)),
    ("run-daily-clipping", "/api/cron/run-daily-clipping", CronTrigger(hour=5, minute=35)),
    ("sync-conselhoos-raci", "/api/cron/sync-conselhoos-raci", CronTrigger(hour=6, minute=0)),
    ("sync-whatsapp-history", "/api/cron/sync-whatsapp-history", CronTrigger(hour=6, minute=0)),
    ("index-drive-documents", "/api/cron/index-drive-documents", CronTrigger(hour=7, minute=0)),
    # SUSPENSO 10/07 (Bloco B LinkdAPI — ROI fraco: ~4 sinais/meses, 403 diario). Descomentar pra religar.
    # ("linkedin-monitor-topics", "/api/cron/linkedin-monitor-topics", CronTrigger(hour=9, minute=0)),
    ("linkedin-curator", "/api/cron/linkedin-curator", CronTrigger(hour=10, minute=0)),
    ("linkedin-outbound-check", "/api/cron/linkedin-outbound-check", CronTrigger(hour=11, minute=0)),
    # SUSPENSO 10/07 (Bloco B LinkdAPI — ROI fraco: ~4 sinais/meses, 403 diario). Descomentar pra religar.
    # ("linkedin-engagement-prospecting", "/api/cron/linkedin-engagement-prospecting", CronTrigger(hour=11, minute=30)),
    ("raci-weekly-report", "/api/cron/raci-weekly-report", CronTrigger(day_of_week="mon", hour=11, minute=0)),
    ("weekly-digest", "/api/cron/weekly-digest", CronTrigger(day_of_week="mon", hour=8, minute=0)),
    ("editorial-weekly-briefing", "/api/cron/editorial-weekly-briefing", CronTrigger(day_of_week="sun", hour=21, minute=0)),
    ("daily-synthesis", "/api/cron/daily-synthesis", CronTrigger(hour=1, minute=0)),
    # F-E instrumentacao v0 — snapshot diario do capability registry (custo/uso/
    # valor por capacidade). 01h40 UTC, depois do daily-synthesis; acumula a
    # serie point-in-time pra retro PDCA quinzenal. Idempotente por dia (UPSERT).
    ("capability-snapshot", "/api/cron/capability-snapshot", CronTrigger(hour=1, minute=40)),
    ("auto-resolve-editorial", "/api/cron/auto-resolve-editorial", CronTrigger(hour=15, minute=30)),
    # 13/07/26 — Passo 5 F-2: migracao final dos 7 crons Vercel-only restantes
    # (completa a leva de 17/06). Removidos do vercel.json no MESMO commit pra
    # evitar trigger duplicado. Horarios UTC identicos aos do vercel.json.
    # cleanup: cron "0 4 * * 0" (0=domingo Unix) → day_of_week="sun" no APScheduler
    # (day_of_week=0 seria SEGUNDA no APScheduler — nao usar).
    ("health-recalc", "/api/cron/health-recalc", CronTrigger(hour=18, minute=0)),
    ("cleanup", "/api/cron/cleanup", CronTrigger(day_of_week="sun", hour=4, minute=0)),
    ("editorial-metrics-reminder-evening", "/api/cron/editorial-metrics-reminder-evening", CronTrigger(hour=23, minute=0)),
    ("group-digest", "/api/cron/group-digest", CronTrigger(hour=0, minute=0)),
    ("platform-costs-snapshot", "/api/cron/platform-costs-snapshot", CronTrigger(day=2, hour=12, minute=0)),
    ("circulos-recalc", "/api/cron/circulos-recalc", CronTrigger(hour=9, minute=0)),
    ("wa-backfill-1to1", "/api/cron/wa-backfill-1to1", CronTrigger(hour=9, minute=30)),
    # 15/06/26 KILL SWITCH — 10 specialists CoS desligados em bloco.
    # Toda a fila abaixo gerava notificacao redundante, alucinacao de tools,
    # listas defensivas, anti-padrao de "trazer tarefa operacional pra Renato".
    # Em rebuild pra Tonha unica. Restaurar SO se rebuild abortar.
    #
    # ("cos-tonha-digest", "/api/cron/cos-tonha-digest", CronTrigger(hour=10, minute=0)),
    # ("cos-conselheiro-tick", "/api/cron/cos-conselheiro-tick", CronTrigger(hour=12, minute=0)),
    # ("cos-portfolio-tick", "/api/cron/cos-portfolio-tick", CronTrigger(hour=13, minute=0)),
    # ("cos-editorial-tick", "/api/cron/cos-editorial-tick", CronTrigger(hour=14, minute=0)),
    # ("cos-research-tick", "/api/cron/cos-research-tick", CronTrigger(hour=15, minute=0)),
    # ("cos-cs-tick", "/api/cron/cos-cs-tick", CronTrigger(hour=16, minute=0)),
    # ("cos-sales-tick", "/api/cron/cos-sales-tick", CronTrigger(hour=17, minute=0)),
    # ("cos-financial-tick", "/api/cron/cos-financial-tick", CronTrigger(hour=18, minute=0)),
    # ("cos-memory-tick", "/api/cron/cos-memory-tick", CronTrigger(hour=19, minute=0)),
    # ("cos-network-tick", "/api/cron/cos-network-tick", CronTrigger(hour=20, minute=0)),
]


def _interval_min_from_trigger(trigger) -> int:
    """Deriva o intervalo esperado (em minutos) de um CronTrigger computando o
    MAIOR gap entre disparos consecutivos a partir de uma 2a-feira 00:00 UTC de
    referencia (cobre triggers day_of_week/hour/minute sem parsear cron na mao).

    Usa o maior gap (nao o menor) de proposito: o monitor alerta em gap > 2x
    intervalo, entao superestimar o intervalo evita falso-positivo em schedules
    irregulares. Fallback conservador 1440 (diario) se nao der pra derivar."""
    base = datetime(2026, 1, 5, tzinfo=ZoneInfo("UTC"))  # segunda-feira 00:00
    fires: list[datetime] = []
    prev_fire: datetime | None = None
    cur = base
    for _ in range(8):
        nxt = trigger.get_next_fire_time(prev_fire, cur)
        if nxt is None:
            break
        fires.append(nxt)
        prev_fire = nxt
        cur = nxt + timedelta(seconds=1)
    if len(fires) < 2:
        return 1440
    gaps = [
        (fires[i + 1] - fires[i]).total_seconds() / 60.0
        for i in range(len(fires) - 1)
    ]
    return max(1, round(max(gaps)))


def _sync_cron_registry() -> None:
    """Reescreve cron_registry com os jobs ATIVOS deste worker — fonte unica da
    verdade pro monitor-cron-health (app/main.py). Desligar/comentar um job em
    _SCHEDULER_JOBS propaga pro monitor sozinho, sem lista paralela pra manter.
    Ver scripts/migrations/045_cron_registry.sql.

    Defensivo: falha nao derruba o boot do scheduler (so loga)."""
    if not DATABASE_URL:
        logger.warning("cron_registry sync: DATABASE_URL ausente — skip")
        return
    jobs = [(jid, _interval_min_from_trigger(trig)) for jid, _p, trig in _SCHEDULER_JOBS]
    jobs += [(jid, _interval_min_from_trigger(trig)) for jid, trig in _LOCAL_DEV_DELEGATION_JOBS]
    jobs.append(("tonia-delegate-pickup", _interval_min_from_trigger(_TONIA_PICKUP_TRIGGER)))
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # active=FALSE em tudo primeiro: jobs removidos/comentados desde
                # o ultimo boot deixam de ser monitorados (nao ficam stale-forever).
                cur.execute("UPDATE cron_registry SET active = FALSE")
                for job_id, interval_min in jobs:
                    cur.execute(
                        """
                        INSERT INTO cron_registry (job_id, expected_interval_min, active, updated_at)
                        VALUES (%s, %s, TRUE, now())
                        ON CONFLICT (job_id) DO UPDATE
                          SET expected_interval_min = EXCLUDED.expected_interval_min,
                              active = TRUE,
                              updated_at = now()
                        """,
                        (job_id, interval_min),
                    )
                conn.commit()
        logger.info(f"cron_registry: synced {len(jobs)} active jobs")
    except Exception as e:
        logger.warning(f"cron_registry sync failed: {e}")


# Trigger do pickup da Tonia — modulo-level pra reusar no add_job e no registry.
_TONIA_PICKUP_TRIGGER = CronTrigger(minute="*/2")


async def _call_tonia_delegate_pickup() -> None:
    """Tick confiavel pro /delegate/pickup da Tonia (FASE 1.5 async delegation).

    Por que aqui: a Tonia e Vercel-only (sem scheduler in-process). O cron dela
    vivia em GH Actions (*/2), mas o GitHub THROTTLA cron de alta frequencia pra
    ~1x/h E o job tinha timeout-minutes:2 curto demais pro pickup (que roda a
    delegacao inline, ~2min) — resultado medido: delegacoes esperavam 5,5h. Este
    worker ja roda 24/7 no Railway: dispara a cada 2min, sem timeout de job.
    Ponte ate a Tonia migrar pro Railway (F-2). Ver feedback_cron_host_choice.

    POST autenticado por Bearer TONIA_CRON_SECRET (require_auth da Tonia).
    Heartbeat gravado pro monitor-cron-health cobrir tambem este tick.
    Defensivo: nunca levanta (nao derruba os proximos jobs)."""
    job_id = "tonia-delegate-pickup"
    if not TONIA_CRON_SECRET:
        logger.warning("tonia-pickup: TONIA_CRON_SECRET/CRON_SECRET ausente — pulando")
        _record_cron_heartbeat(job_id, None, 0)
        return
    url = f"{TONIA_API_URL}/delegate/pickup"
    started = datetime.now()
    http_status: int | None = None
    try:
        # read alto: o pickup roda a delegacao inline (delegador ate ~6min).
        timeout = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {TONIA_CRON_SECRET}",
                    "User-Agent": "intel-worker-scheduler/1.0",
                    "X-Cron-Source": "railway-worker",
                },
            )
        http_status = resp.status_code
        logger.info(f"scheduler: tonia-pickup -> http {resp.status_code}")
    except Exception as e:
        http_status = 500
        logger.exception(f"scheduler: tonia-pickup failed: {e}")
    finally:
        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        _record_cron_heartbeat(job_id, http_status, duration_ms)


async def _run_local_dev_delegation(job_id: str) -> None:
    """Handler in-process pros 4 disparos de dev_delegation_pickup.

    Substitui o pattern HTTP _call_vercel_cron pra esse cron especifico —
    no worker nao tem teto 300s, entao process_due pode executar a chamada
    _call_delegator (30-350s) sem ser cortada mid-flight. Mantemos heartbeat
    em cron_heartbeats pro monitor de saude continuar funcionando.
    """
    started = datetime.now()
    http_status: int | None = None
    try:
        from dev_delegation_pickup import process_due
        summary = await process_due()
        http_status = 200
        logger.info(f"scheduler local: {job_id} -> {summary}")
    except Exception as e:
        http_status = 500
        logger.exception(f"scheduler local: {job_id} failed: {e}")
    finally:
        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        _record_cron_heartbeat(job_id, http_status, duration_ms)


_LOCAL_DEV_DELEGATION_JOBS = [
    ("dev-delegation-pickup-12", CronTrigger(hour=15, minute=20)),
    ("dev-delegation-pickup-15", CronTrigger(hour=18, minute=20)),
    ("dev-delegation-pickup-18", CronTrigger(hour=21, minute=20)),
    ("dev-delegation-pickup-21", CronTrigger(hour=0, minute=20)),
]


@app.on_event("startup")
async def _start_scheduler():
    if not CRON_SECRET:
        logger.warning("scheduler: CRON_SECRET ausente — jobs vao disparar sem auth e tomar 401")
    for job_id, path, trigger in _SCHEDULER_JOBS:
        scheduler.add_job(
            _call_vercel_cron,
            trigger=trigger,
            args=[path, job_id],
            id=job_id,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
            replace_existing=True,
        )
    for job_id, trigger in _LOCAL_DEV_DELEGATION_JOBS:
        scheduler.add_job(
            _run_local_dev_delegation,
            trigger=trigger,
            args=[job_id],
            id=job_id,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
            replace_existing=True,
        )
    # Tick externo: pickup de delegacao da Tonia (ponte ate F-2 Railway).
    scheduler.add_job(
        _call_tonia_delegate_pickup,
        trigger=_TONIA_PICKUP_TRIGGER,
        id="tonia-delegate-pickup",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=120,
        replace_existing=True,
    )
    scheduler.start()
    n_total = len(_SCHEDULER_JOBS) + len(_LOCAL_DEV_DELEGATION_JOBS) + 1  # +1 = tonia-pickup
    logger.info(
        f"scheduler: started with {n_total} jobs "
        f"({len(_SCHEDULER_JOBS)} http + {len(_LOCAL_DEV_DELEGATION_JOBS)} local + 1 external)"
    )
    # Fonte unica da verdade pro monitor-cron-health: reescreve o registry com
    # os jobs vivos deste boot. Ver _sync_cron_registry / migration 045.
    _sync_cron_registry()


@app.on_event("shutdown")
async def _stop_scheduler():
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("scheduler: shutdown error")


def _cron_secret_fingerprint() -> dict:
    """Fingerprint do CRON_SECRET sem expor o valor — pareia com o formato
    do /api/admin/cron-auth-debug no Vercel pra comparacao 1:1.
    Use first4 + last4 + sha256_first8 pra detectar mismatch entre Railway e Vercel.
    """
    raw = os.getenv("CRON_SECRET", "")
    stripped = raw.strip()
    if not stripped:
        return {"present": False, "length": 0, "first4": "", "last4": "", "sha256_first8": "",
                "has_trailing_whitespace": raw != stripped}
    import hashlib
    return {
        "present": True,
        "length": len(stripped),
        "first4": stripped[:4],
        "last4": stripped[-4:],
        "sha256_first8": hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:8],
        "has_trailing_whitespace": raw != stripped,
    }


@app.get("/scheduler-status")
async def scheduler_status():
    """Debug: lista jobs registrados + proximo fire scheduled + fingerprint
    do CRON_SECRET pra comparacao com Vercel (/api/admin/cron-auth-debug)."""
    jobs_info = []
    for job in scheduler.get_jobs():
        jobs_info.append({
            "id": job.id,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return {
        "scheduler_running": scheduler.running,
        "cron_secret_set": bool(CRON_SECRET),
        "cron_secret_fingerprint": _cron_secret_fingerprint(),
        "jobs": jobs_info,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "audio-transcriber"}


@app.get("/debug-db")
async def debug_db():
    """Test database connectivity."""
    results = {}
    # Test INTEL DB
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) as total FROM contacts")
        results["intel"] = {"ok": True, "contacts": cursor.fetchone()["total"]}
        conn.close()
    except Exception as e:
        results["intel"] = {"ok": False, "error": str(e), "url_prefix": DATABASE_URL[:50] if DATABASE_URL else "EMPTY"}

    # Test ConselhoOS DB
    try:
        conn = psycopg.connect(CONSELHOOS_DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) as total FROM empresas")
        results["conselhoos"] = {"ok": True, "empresas": cursor.fetchone()["total"]}
        conn.close()
    except Exception as e:
        results["conselhoos"] = {"ok": False, "error": str(e)}

    return results


@app.post("/organize-empresa")
async def organize_empresa(request: Request):
    """
    Organize Drive folder + extract empresa data. No timeout limit.
    Called by ConselhoOS or directly.
    """
    data = await request.json()
    if not _check_worker_secret(data.get("secret")):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    empresa_id = data.get("empresa_id", "")
    folder_id = data.get("folder_id", "")
    access_token = data.get("access_token", "")

    if not folder_id:
        return JSONResponse(status_code=400, content={"error": "folder_id required"})

    logger.info(f"Organizing empresa {empresa_id}, folder {folder_id}")

    results = {"subfolders_created": [], "files_moved": [], "docs_read": 0, "extracted": None}

    try:
        # 1. List all items in folder
        headers = {"Authorization": f"Bearer {access_token}"}

        resp = await _drive_list(folder_id, headers)
        items = resp.get("files", [])
        logger.info(f"Found {len(items)} items in folder")

        folders = {f["name"].lower(): f["id"] for f in items if "folder" in f.get("mimeType", "")}
        files = [f for f in items if "folder" not in f.get("mimeType", "")]

        # 2. Create standard subfolders
        standard = ["Atas", "Documentos", "RACI", "Pauta Anual", "Financeiro", "Preparação"]
        for name in standard:
            if name.lower() not in folders:
                created = await _drive_create_folder(name, folder_id, headers)
                if created:
                    folders[name.lower()] = created["id"]
                    results["subfolders_created"].append(name)

        # 3. Move loose files to correct subfolders
        for f in files:
            name_lower = f["name"].lower()
            target = None

            if any(k in name_lower for k in ["ata", "minuta", "acta"]):
                target = folders.get("atas")
            elif any(k in name_lower for k in ["raci", "ação", "acao"]):
                target = folders.get("raci")
            elif any(k in name_lower for k in ["pauta", "agenda"]):
                target = folders.get("pauta anual")
            elif any(k in name_lower for k in ["dfin", "financ", "balancete", "dre", "balanço", "receita", "orçamento"]):
                target = folders.get("financeiro")
            elif any(k in name_lower for k in ["briefing", "preparação", "preparacao"]):
                target = folders.get("preparação")

            if target:
                moved = await _drive_move_file(f["id"], target, headers)
                if moved:
                    target_name = next((n for n, fid in folders.items() if fid == target), "?")
                    results["files_moved"].append({"name": f["name"], "to": target_name})

        # 4. Read Google Docs content for enrichment
        readable = ["application/vnd.google-apps.document", "application/vnd.google-apps.spreadsheet",
                     "application/vnd.google-apps.presentation"]

        # Also try to export .docx and .pptx files
        all_readable = [f for f in items if f.get("mimeType", "") in readable]

        # Scan subfolders too
        for fname, fid in folders.items():
            try:
                sub_resp = await _drive_list(fid, headers)
                for sf in sub_resp.get("files", []):
                    if sf.get("mimeType", "") in readable:
                        all_readable.append(sf)
            except Exception:
                pass

        doc_contents = []
        for doc in all_readable[:15]:
            try:
                content = await _drive_export_text(doc["id"], headers)
                if content:
                    doc_contents.append({"name": doc["name"], "content": content[:3000]})
                    results["docs_read"] += 1
            except Exception:
                pass

        # Also use file names for context
        file_list = "\n".join([f"[{f.get('folder', 'raiz') if 'folder' in f else 'raiz'}] {f['name']}" for f in items])

        # 5. Claude enrichment
        if doc_contents or items:
            doc_texts = "\n".join([f"\n--- {d['name']} ---\n{d['content']}" for d in doc_contents])

            prompt = f"""Analise os documentos desta empresa e extraia TODAS as informações.

EMPRESA: {data.get('empresa_nome', 'desconhecida')}

ARQUIVOS ({len(items)}):
{file_list}

CONTEÚDO DOS DOCUMENTOS LIDOS ({len(doc_contents)}):
{doc_texts or '(nenhum documento Google Docs encontrado)'}

Extraia APENAS JSON (sem markdown):
{{
  "setor": "setor de atuação",
  "descricao": "descrição em 2-3 frases",
  "contexto_md": "contexto detalhado em markdown: histórico, missão, valores, posicionamento, desafios",
  "pessoas": [
    {{"nome": "Nome", "cargo": "Cargo", "tipo": "socio|conselheiro|executivo|funcionario"}}
  ],
  "riscos": ["risco 1", "risco 2"],
  "plano_estrategico": "resumo do plano estratégico",
  "insights": {{
    "governanca": "estrutura, maturidade",
    "mercado": "setor, posicionamento",
    "financeiro": "se disponível",
    "operacional": "estrutura, processos"
  }}
}}

Extraia APENAS do que está nos documentos. NÃO invente."""

            async with httpx.AsyncClient(timeout=60.0) as client:
                ai_resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000,
                          "messages": [{"role": "user", "content": prompt}]}
                )

            if ai_resp.status_code == 200:
                text = ai_resp.json().get("content", [{}])[0].get("text", "")
                js = text.find("{")
                je = text.rfind("}") + 1
                if js >= 0:
                    extracted = json.loads(text[js:je])
                    results["extracted"] = extracted

                    # Update empresa in ConselhoOS DB
                    if CONSELHOOS_DATABASE_URL and empresa_id:
                        conn = psycopg.connect(CONSELHOOS_DATABASE_URL, row_factory=dict_row)
                        cursor = conn.cursor()

                        updates = []
                        values = []
                        if extracted.get("setor"):
                            updates.append("setor = %s")
                            values.append(extracted["setor"])
                        if extracted.get("descricao"):
                            updates.append("descricao = %s")
                            values.append(extracted["descricao"])
                        if extracted.get("contexto_md"):
                            updates.append("contexto_md = %s")
                            values.append(extracted["contexto_md"])
                        if extracted.get("insights"):
                            updates.append("insights_json = %s")
                            values.append(json.dumps(extracted["insights"]))
                        if extracted.get("pessoas"):
                            updates.append("pessoas_chave = %s")
                            values.append(json.dumps(extracted["pessoas"]))

                        if updates:
                            updates.append("updated_at = NOW()")
                            values.append(empresa_id)
                            cursor.execute(f"UPDATE empresas SET {', '.join(updates)} WHERE id = %s", values)
                            conn.commit()

                        # Create pessoas records
                        for p in extracted.get("pessoas", []):
                            if not p.get("nome"):
                                continue
                            cursor.execute("SELECT id FROM pessoas WHERE empresa_id = %s AND nome = %s", (empresa_id, p["nome"]))
                            if not cursor.fetchone():
                                cursor.execute(
                                    "INSERT INTO pessoas (id, empresa_id, nome, cargo) VALUES (gen_random_uuid(), %s, %s, %s)",
                                    (empresa_id, p["nome"], p.get("cargo", ""))
                                )
                        conn.commit()
                        conn.close()

        return results

    except Exception as e:
        logger.error(f"Organize empresa error: {e}")
        return {"error": str(e)}


async def _drive_list(folder_id: str, headers: dict) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://www.googleapis.com/drive/v3/files?q='{folder_id}'+in+parents+and+trashed=false&fields=files(id,name,mimeType)&pageSize=100",
            headers=headers)
        return resp.json() if resp.status_code == 200 else {"files": []}


async def _drive_create_folder(name: str, parent_id: str, headers: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://www.googleapis.com/drive/v3/files",
            headers={**headers, "Content-Type": "application/json"},
            json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]})
        return resp.json() if resp.status_code == 200 else None


async def _drive_move_file(file_id: str, target_folder: str, headers: dict) -> bool:
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Get current parents
        resp = await client.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=parents", headers=headers)
        if resp.status_code != 200:
            return False
        parents = resp.json().get("parents", [])
        # Move
        resp = await client.patch(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?addParents={target_folder}&removeParents={','.join(parents)}",
            headers=headers)
        return resp.status_code == 200


async def _drive_export_text(file_id: str, headers: dict) -> str | None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/plain",
            headers=headers)
        if resp.status_code == 200:
            return resp.text
        # Fallback: direct download
        resp = await client.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", headers=headers)
        return resp.text if resp.status_code == 200 else None


async def _run_bot_and_respond(phone: str, content: str, message_id: str) -> Optional[str]:
    """Runner usado pelo debounce do path texto: _run_bot local + send_response."""
    try:
        response = await _run_bot(phone, content, message_id)
        if response:
            await _send_response(phone, response)
        return response
    except Exception as e:
        logger.exception(f"_run_bot_and_respond crashed: {e}")
        await _send_response(phone, "Desculpa, tive um erro. Tenta de novo?")
        return None


async def _post_to_intel_bot_webhook(phone: str, content: str, message_id: str) -> Optional[str]:
    """Runner usado pelo debounce do path audio: POSTa pra /api/webhooks/bot-message
    no Vercel, mesmo flow do path sem debounce. Preserva engine completo
    (services.intel_bot.handle_bot_message + _WA_SENT_VAR guard + persistencia)."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            bot_resp = await client.post(
                f"{INTEL_API_URL}/api/webhooks/bot-message",
                headers={"Content-Type": "application/json"},
                json={"phone": phone, "content": content, "message_id": message_id,
                      "secret": WORKER_SECRET},
                timeout=55.0,
            )
        if bot_resp.status_code == 200:
            return None
        logger.warning(f"_post_to_intel_bot_webhook HTTP {bot_resp.status_code}")
        await _send_response(phone, "Deixa eu reler o que voce mandou — te volto em instantes.")
    except httpx.TimeoutException:
        logger.warning("_post_to_intel_bot_webhook timeout")
        await _send_response(phone, "Demorei mais que o normal pra processar — te volto em instantes.")
    except Exception as e:
        logger.exception(f"_post_to_intel_bot_webhook crashed: {e}")
        await _send_response(phone, "Desculpa, tive um erro. Tenta de novo?")
    return None


@app.post("/process-message")
async def process_message(request: Request):
    """
    Process bot message directly on Railway with full DB access.
    No timeout limit. Has access to INTEL + ConselhoOS databases.

    Quando BOT_DEBOUNCE_ENABLED=1, msgs viram batch (janela ~6s) antes de
    chamar _run_bot — agrupa rajadas do mesmo phone.
    """
    data = await request.json()
    if not _check_worker_secret(data.get("secret")):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    phone = data.get("phone", "")
    content = data.get("content", "")
    message_id = data.get("message_id", "")

    if not phone or not content:
        return JSONResponse(status_code=400, content={"error": "missing phone or content"})

    logger.info(f"Processing bot message for {phone}: {content[:80]}")

    if debounce.is_enabled():
        queued = await debounce.enqueue(phone, content, message_id, _run_bot_and_respond)
        return {"status": "queued", **queued}

    try:
        response = await _run_bot(phone, content, message_id)
        if response:
            await _send_response(phone, response)
        return {"status": "success", "response_length": len(response or "")}
    except Exception as e:
        logger.error(f"Bot processing error: {e}")
        await _send_response(phone, "Desculpa, tive um erro. Tenta de novo?")
        return {"status": "error", "error": str(e)}


# ==================== BOT ENGINE (runs on Railway) ====================

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")

BOT_TOOLS = [
    {
        "name": "web_search",
        "description": "Pesquisa na internet. Use para buscar informacoes atuais, noticias, dados de empresas, pessoas, etc.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "Termo de busca"}}, "required": ["query"]}
    },
    {
        "name": "fetch_url",
        "description": "Busca conteudo de uma URL (artigo, pagina web). Retorna titulo + texto extraido. Use para ler artigos, noticias, documentos online.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "summarize": {"type": "boolean", "description": "Se true, resume com IA"}}, "required": ["url"]}
    },
    {
        "name": "query_intel",
        "description": "SELECT no banco INTEL (contatos, mensagens, projetos, tarefas, memorias). Apenas SELECT. LIMIT 20.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "query_conselhoos",
        "description": "SELECT no banco ConselhoOS (empresas, reunioes, raci_itens, decisoes, pessoas). Apenas SELECT.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "execute_conselhoos",
        "description": "INSERT/UPDATE/DELETE no ConselhoOS. IDs UUID (gen_random_uuid()). IMPORTANTE: ao criar empresas, SEMPRE inclua user_id='115322753506978752025'. Tabelas: empresas (id,nome,setor,descricao,user_id,cor_hex), reunioes, raci_itens, decisoes, pessoas, documentos.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "manage_email",
        "description": (
            "Gerencia emails do Gmail. Acoes:\n"
            "- archive_non_urgent: arquiva emails nao-urgentes do inbox (filtra newsletters, notificacoes, spam)\n"
            "- list_inbox: lista emails recentes do inbox (limit?)\n"
            "- archive_by_subject: arquiva emails com assunto especifico (subject_contains)\n"
            "Parametros: {action, account? 'professional'|'personal'|'both', subject_contains?, limit?}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"}
            },
            "required": ["action", "params"]
        }
    },
    {
        "name": "execute_intel",
        "description": (
            "Executa acao no INTEL:\n"
            "- create_task: {titulo, descricao?, project_id?, contact_id?, data_vencimento? YYYY-MM-DD}\n"
            "- complete_task: {task_id}\n"
            "- save_note: {project_id, titulo, conteudo}\n"
            "- save_memory: {contact_id, titulo, resumo, tipo?}\n"
            "- save_feedback: {conteudo, tipo? bug|melhoria|ideia}\n"
            "- save_article: {project_id, url} — busca artigo, resume com IA, salva no projeto"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "params": {"type": "object"}
            },
            "required": ["action", "params"]
        }
    }
]


def _db_query(url: str, sql: str, write: bool = False) -> str:
    """Execute SQL on a database."""
    if not url:
        return json.dumps({"erro": "Database URL nao configurada"})

    sql = sql.strip().rstrip(";").strip()
    sql_upper = sql.upper()

    if not write:
        if not sql_upper.startswith("SELECT"):
            return json.dumps({"erro": "Apenas SELECT permitido"})
        if "LIMIT" not in sql_upper:
            sql += " LIMIT 20"

    try:
        logger.info(f"DB query: {sql[:150]}")
        conn = psycopg.connect(url, row_factory=dict_row)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            if write:
                result = f"{cursor.rowcount} registro(s) afetado(s)"
                try:
                    rows = cursor.fetchall()
                    if rows:
                        result += "\n" + " | ".join(f"{k}: {v}" for k, v in dict(rows[0]).items())
                except Exception:
                    pass
                conn.commit()
                return json.dumps({"sucesso": True, "resultado": result}, ensure_ascii=False)
            else:
                rows = [dict(r) for r in cursor.fetchall()]
                if not rows:
                    return "Nenhum resultado"
                lines = []
                for i, row in enumerate(rows):
                    parts = [f"{k}: {str(v)[:200]}" for k, v in row.items() if v is not None]
                    lines.append(f"[{i+1}] " + " | ".join(parts))
                return f"{len(rows)} resultados:\n" + "\n".join(lines)
        finally:
            conn.close()
    except Exception as e:
        return json.dumps({"erro": str(e)})


def _audit_log(
    action_type: str,
    category: str,
    title: str,
    scope_ref: dict = None,
    payload: dict = None,
    undo_hint: str = None,
) -> None:
    """Inline P3 audit log para acoes do bot worker.

    Why: bot worker nao importa de app/services/ — codigo duplicado
    intencional. Sem isso, _execute_intel_action e _manage_email
    fazem mudancas de estado (Gmail, INTEL DB) sem trilha.
    """
    if not DATABASE_URL:
        return
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO agent_actions
                (action_type, category, title, scope_ref, source, payload, undo_hint)
            VALUES (%s, %s, %s, %s, 'intel_bot.worker', %s, %s)
        """, (
            action_type,
            category,
            title,
            json.dumps(scope_ref or {}),
            json.dumps(payload) if payload else None,
            undo_hint,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"audit_log failed ({action_type}): {e}")


async def _execute_intel_action(action: str, params: dict) -> str:
    """Execute an INTEL CRM action."""
    if not DATABASE_URL:
        return json.dumps({"erro": "DATABASE_URL nao configurada"})

    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()

        if action == "create_task":
            dv = params.get("data_vencimento")
            if dv:
                try:
                    dv = datetime.strptime(str(dv)[:10], "%Y-%m-%d")
                except Exception:
                    dv = None
            if not dv and params.get("prazo_dias"):
                dv = (_now_sp() + timedelta(days=params["prazo_dias"])).replace(tzinfo=None)

            # Validate foreign keys
            contact_id = params.get("contact_id")
            if contact_id:
                cursor.execute("SELECT id FROM contacts WHERE id = %s", (contact_id,))
                if not cursor.fetchone():
                    contact_id = None  # Invalid, skip

            project_id = params.get("project_id")
            if project_id:
                cursor.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
                if not cursor.fetchone():
                    project_id = None

            cursor.execute("""
                INSERT INTO tasks (titulo, descricao, project_id, contact_id, data_vencimento,
                    prioridade, ai_generated, origem, status)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'intel_bot', 'pending') RETURNING id
            """, (params.get("titulo"), params.get("descricao", ""), project_id,
                  contact_id, dv, params.get("prioridade", 5)))
            tid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            _audit_log('task_created_by_bot', 'tasks',
                       f"Tarefa criada via bot: {params.get('titulo', '')[:80]}",
                       scope_ref={'task_id': tid, 'project_id': project_id, 'contact_id': contact_id},
                       payload={'prazo_dias': params.get('prazo_dias')},
                       undo_hint=f"DELETE FROM tasks WHERE id={tid};")
            return f"Tarefa #{tid} criada: {params.get('titulo')}"

        elif action == "complete_task":
            cursor.execute("UPDATE tasks SET status='completed', data_conclusao=NOW() WHERE id=%s RETURNING titulo",
                          (params["task_id"],))
            r = cursor.fetchone()
            conn.commit()
            conn.close()
            if r:
                _audit_log('task_completed_by_bot', 'tasks',
                           f"Tarefa concluida via bot: {r['titulo'][:80]}",
                           scope_ref={'task_id': params['task_id']},
                           undo_hint=f"UPDATE tasks SET status='pending', data_conclusao=NULL WHERE id={params['task_id']};")
            return f"Tarefa concluida: {r['titulo']}" if r else "Tarefa nao encontrada"

        elif action == "save_note":
            cursor.execute("INSERT INTO project_notes (project_id, titulo, conteudo, tipo, autor) VALUES (%s,%s,%s,%s,'INTEL Bot') RETURNING id",
                          (params.get("project_id"), params.get("titulo", ""), params.get("conteudo", ""), params.get("tipo", "nota")))
            nid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            _audit_log('note_saved_by_bot', 'system',
                       f"Nota em projeto: {params.get('titulo', '')[:80]}",
                       scope_ref={'note_id': nid, 'project_id': params.get('project_id')},
                       undo_hint=f"DELETE FROM project_notes WHERE id={nid};")
            return f"Nota #{nid} salva"

        elif action == "save_memory":
            cursor.execute("INSERT INTO contact_memories (contact_id, titulo, resumo, tipo) VALUES (%s,%s,%s,%s) RETURNING id",
                          (params["contact_id"], params.get("titulo", ""), params.get("resumo", ""), params.get("tipo", "nota")))
            mid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            _audit_log('memory_saved_by_bot', 'contacts',
                       f"Memoria de contato: {params.get('titulo', '')[:80]}",
                       scope_ref={'memory_id': mid, 'contact_id': params['contact_id']},
                       undo_hint=f"DELETE FROM contact_memories WHERE id={mid};")
            return f"Memoria #{mid} salva"

        elif action == "save_feedback":
            cursor.execute("INSERT INTO system_feedback (tipo, conteudo) VALUES (%s,%s) RETURNING id",
                          (params.get("tipo", "feedback"), params.get("conteudo", "")))
            fid = cursor.fetchone()["id"]
            conn.commit()
            conn.close()
            _audit_log('feedback_saved_by_bot', 'system',
                       f"Feedback {params.get('tipo', 'feedback')}: {(params.get('conteudo', ''))[:80]}",
                       scope_ref={'feedback_id': fid},
                       undo_hint=f"DELETE FROM system_feedback WHERE id={fid};")
            return f"Feedback #{fid} registrado"

        elif action == "save_article":
            conn.close()
            # Call the INTEL API to fetch, summarize, and save
            project_id = params.get("project_id")
            url = params.get("url", "")
            if not project_id or not url:
                return "project_id e url obrigatorios"
            try:
                async_resp = await _save_article_via_api(project_id, url)
                return async_resp
            except Exception as e:
                return f"Erro ao salvar artigo: {e}"

        conn.close()
        return f"Acao desconhecida: {action}"
    except Exception as e:
        return f"Erro: {e}"


async def _web_search(query: str) -> str:
    """Search the web via Brave Search API."""
    if not BRAVE_API_KEY:
        return "Web search indisponivel (BRAVE_API_KEY nao configurada)"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
                params={"q": query, "count": 5}
            )
        if resp.status_code != 200:
            return f"Erro na busca: {resp.status_code}"
        data = resp.json()
        results = data.get("web", {}).get("results", [])
        if not results:
            return "Nenhum resultado encontrado"
        lines = []
        for r in results[:5]:
            lines.append(f"**{r.get('title','')}**\n{r.get('description','')}\nURL: {r.get('url','')}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"Erro: {e}"


async def _fetch_url(url: str, summarize: bool = False) -> str:
    """Fetch and extract content from a URL."""
    import re
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return f"Erro HTTP {resp.status_code}"

        html = resp.text
        # Extract title
        title_match = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        title = title_match.group(1) if title_match else ""
        if not title:
            title_match = re.search(r'<title>([^<]+)</title>', html)
            title = title_match.group(1) if title_match else url

        # Extract text
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        article = re.search(r'<article[^>]*>(.*?)</article>', text, flags=re.DOTALL)
        if article:
            text = article.group(1)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()[:4000]

        result = f"**{title}**\n\n{text[:2000]}"

        if summarize and ANTHROPIC_API_KEY:
            async with httpx.AsyncClient(timeout=15.0) as client:
                ai_resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                          "messages": [{"role": "user", "content": f"Resuma em 3-4 frases:\n\n{title}\n{text[:3000]}"}]}
                )
            if ai_resp.status_code == 200:
                summary = ai_resp.json()["content"][0]["text"]
                result = f"**{title}**\n\n{summary}\n\nFonte: {url}"

        return result
    except Exception as e:
        return f"Erro ao buscar URL: {e}"


def _run_tool(name: str, input_data: dict) -> str:
    """Execute a bot tool (sync only - DB queries)."""
    if name == "query_intel":
        return _db_query(DATABASE_URL, input_data["sql"])
    elif name == "query_conselhoos":
        return _db_query(CONSELHOOS_DATABASE_URL, input_data["sql"])
    elif name == "execute_conselhoos":
        result = _db_query(CONSELHOOS_DATABASE_URL, input_data["sql"], write=True)
        # Auto-create INTEL project when empresa is created
        sql_upper = input_data.get("sql", "").upper()
        if "INSERT" in sql_upper and "EMPRESAS" in sql_upper:
            _auto_create_project_for_empresa(input_data["sql"], result)
        return result
    return "Tool desconhecida"


async def _get_gmail_token(account_type: str = "professional") -> tuple[str | None, str | None]:
    """Get fresh Gmail access token for an account."""
    if not DATABASE_URL:
        return None, None
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    cursor = conn.cursor()
    cursor.execute("SELECT email, access_token, refresh_token, token_expiry FROM google_accounts WHERE tipo = %s AND conectado = TRUE LIMIT 1", (account_type,))
    account = cursor.fetchone()
    conn.close()
    if not account:
        return None, None

    # Check if token is fresh
    if account.get('token_expiry') and account['token_expiry'] > datetime.now():
        return account['access_token'], account['email']

    # Refresh
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if not account.get('refresh_token'):
        return None, None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": account['refresh_token'], "grant_type": "refresh_token"
            })
        if resp.status_code == 200:
            new_token = resp.json()["access_token"]
            conn = psycopg.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("UPDATE google_accounts SET access_token = %s, token_expiry = NOW() + INTERVAL '1 hour' WHERE email = %s",
                          (new_token, account['email']))
            conn.commit()
            conn.close()
            return new_token, account['email']
    except Exception as e:
        logger.error(f"Gmail token refresh: {e}")
    return None, None


async def _manage_email(action: str, params: dict) -> str:
    """Manage Gmail emails (archive, list, etc.)."""
    account_type = params.get("account", "both")
    accounts_to_check = ["professional", "personal"] if account_type == "both" else [account_type]

    if action == "archive_non_urgent":
        total_archived = 0
        details = []

        for acct in accounts_to_check:
            token, email = await _get_gmail_token(acct)
            if not token:
                details.append(f"{acct}: token indisponível")
                continue

            try:
                # List inbox messages
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=in:inbox&maxResults=50",
                        headers={"Authorization": f"Bearer {token}"})
                    if resp.status_code != 200:
                        details.append(f"{acct}: erro ao listar")
                        continue
                    messages = resp.json().get("messages", [])

                # Get details and classify
                non_urgent_ids = []
                for msg in messages:
                    try:
                        detail = await client.get(
                            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject",
                            headers={"Authorization": f"Bearer {token}"})
                        if detail.status_code != 200:
                            continue
                        d = detail.json()
                        headers = {h['name']: h['value'] for h in d.get('payload', {}).get('headers', [])}
                        subject = (headers.get('Subject', '') or '').lower()
                        sender = (headers.get('From', '') or '').lower()

                        # Non-urgent patterns
                        is_non_urgent = any(p in subject or p in sender for p in [
                            'newsletter', 'digest', 'weekly', 'update', 'notification',
                            'noreply', 'no-reply', 'mailer-daemon', 'unsubscribe',
                            'linkedin', 'github', 'slack', 'notion', 'calendar',
                            'promoção', 'desconto', 'oferta', 'fatura', 'nfe',
                            'nota fiscal', 'boleto', 'comprovante', 'recibo'
                        ])
                        if is_non_urgent:
                            non_urgent_ids.append(msg['id'])
                    except Exception:
                        continue

                # Archive (remove INBOX label)
                if non_urgent_ids:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(
                            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                            json={"ids": non_urgent_ids, "removeLabelIds": ["INBOX"]})
                        if resp.status_code == 204:
                            total_archived += len(non_urgent_ids)
                            details.append(f"{email}: {len(non_urgent_ids)} arquivados")
                            _audit_log(
                                action_type='gmail_archived_non_urgent',
                                category='email',
                                title=f"Gmail: {len(non_urgent_ids)} emails nao-urgentes arquivados em {email}",
                                scope_ref={'account': acct, 'email': email},
                                payload={'message_ids': non_urgent_ids[:50]},
                                undo_hint=f"Gmail batchModify addLabelIds=['INBOX'] no token de {email} para os {len(non_urgent_ids)} ids em payload.message_ids",
                            )
                        else:
                            details.append(f"{email}: erro ao arquivar ({resp.status_code})")
                else:
                    details.append(f"{email}: nenhum não-urgente encontrado")

            except Exception as e:
                details.append(f"{acct}: {e}")

        return f"Arquivados: {total_archived} emails\n" + "\n".join(details)

    elif action == "archive_by_subject":
        subject_filter = params.get("subject_contains", "")
        if not subject_filter:
            return "Parâmetro subject_contains obrigatório"

        total_archived = 0
        for acct in accounts_to_check:
            token, email = await _get_gmail_token(acct)
            if not token:
                continue
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q=in:inbox+subject:{subject_filter}&maxResults=20",
                        headers={"Authorization": f"Bearer {token}"})
                    messages = resp.json().get("messages", [])
                    if messages:
                        ids = [m['id'] for m in messages]
                        await client.post(
                            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                            json={"ids": ids, "removeLabelIds": ["INBOX"]})
                        total_archived += len(ids)
                        _audit_log(
                            action_type='gmail_archived_by_subject',
                            category='email',
                            title=f"Gmail: {len(ids)} emails arquivados por subject '{subject_filter[:40]}' em {email}",
                            scope_ref={'account': acct, 'email': email},
                            payload={'subject_filter': subject_filter, 'message_ids': ids[:50]},
                            undo_hint=f"Gmail batchModify addLabelIds=['INBOX'] no token de {email} para os {len(ids)} ids em payload.message_ids",
                        )
            except Exception:
                pass

        return f"Arquivados {total_archived} emails com '{subject_filter}'"

    elif action == "list_inbox":
        limit = params.get("limit", 10)
        results = []
        for acct in accounts_to_check:
            token, email = await _get_gmail_token(acct)
            if not token:
                continue
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q=in:inbox&maxResults={limit}",
                        headers={"Authorization": f"Bearer {token}"})
                    messages = resp.json().get("messages", [])
                    for msg in messages[:limit]:
                        detail = await client.get(
                            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject",
                            headers={"Authorization": f"Bearer {token}"})
                        if detail.status_code == 200:
                            d = detail.json()
                            hdrs = {h['name']: h['value'] for h in d.get('payload', {}).get('headers', [])}
                            results.append(f"[{acct}] {hdrs.get('Subject','?')} — {hdrs.get('From','?')[:40]}")
            except Exception:
                pass
        return f"Inbox ({len(results)}):\n" + "\n".join(results) if results else "Inbox vazio"

    return f"Ação desconhecida: {action}"


async def _save_article_via_api(project_id: int, url: str) -> str:
    """Fetch, summarize, and save article to project via INTEL API."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{INTEL_API_URL}/api/projects/{project_id}/save-article",
                headers={"Content-Type": "application/json"},
                json={"url": url}
            )
        if resp.status_code == 200:
            data = resp.json()
            return f"Artigo salvo no projeto: {data.get('title', url)}\nResumo: {data.get('summary', '')[:300]}"
        else:
            # Fallback: save directly
            return await _save_article_direct(project_id, url)
    except Exception:
        return await _save_article_direct(project_id, url)


async def _save_article_direct(project_id: int, url: str) -> str:
    """Save article directly from worker (fallback if API fails)."""
    try:
        # Fetch
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return f"Erro HTTP {resp.status_code} ao buscar artigo"

        html = resp.text
        import re
        title_match = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        title = title_match.group(1) if title_match else url

        # Strip HTML
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()[:3000]

        # Summarize with Claude
        async with httpx.AsyncClient(timeout=20.0) as client:
            ai_resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 400,
                      "messages": [{"role": "user", "content": f"Resuma este artigo em português, 3-4 frases + pontos-chave:\n\nTÍTULO: {title}\n\n{text}"}]}
            )
        summary = ai_resp.json()["content"][0]["text"] if ai_resp.status_code == 200 else text[:300]

        # Save
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor, metadata)
            VALUES (%s, 'article', %s, %s, 'INTEL Bot', %s) RETURNING id
        """, (project_id, title, summary, json.dumps({"url": url})))
        nid = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return f"Artigo #{nid} salvo: {title}\nResumo: {summary[:300]}"
    except Exception as e:
        return f"Erro: {e}"


def _auto_create_project_for_empresa(sql: str, result: str):
    """When a ConselhoOS empresa is created, auto-create INTEL project."""
    if not DATABASE_URL:
        return
    try:
        # Extract empresa name from SQL (between quotes after nome)
        import re
        match = re.search(r"'([^']+)'", sql.split("nome" if "nome" in sql.lower() else ",")[0] + sql)
        # Better: query the empresa we just created
        conn_cos = psycopg.connect(CONSELHOOS_DATABASE_URL, row_factory=dict_row)
        cursor_cos = conn_cos.cursor()
        cursor_cos.execute("SELECT nome, setor, descricao, drive_folder_id FROM empresas ORDER BY created_at DESC LIMIT 1")
        emp = cursor_cos.fetchone()
        conn_cos.close()

        if not emp:
            return

        # Check if project already exists in INTEL
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM projects WHERE LOWER(nome) = LOWER(%s) LIMIT 1", (emp['nome'],))
        if cursor.fetchone():
            conn.close()
            return

        cursor.execute("""
            INSERT INTO projects (nome, descricao, tipo, status, google_drive_folder_id)
            VALUES (%s, %s, 'conselho', 'ativo', %s) RETURNING id
        """, (emp['nome'], emp.get('descricao') or f"Conselho consultivo - {emp.get('setor', '')}", emp.get('drive_folder_id')))
        pid = cursor.fetchone()['id']
        conn.commit()
        conn.close()
        logger.info(f"Auto-created INTEL project #{pid} for empresa {emp['nome']}")
    except Exception as e:
        logger.error(f"Auto-create project error: {e}")


def _load_history(phone: str, limit: int = 15) -> list:
    """Load conversation history from INTEL DB."""
    if not DATABASE_URL:
        return []
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM bot_conversations WHERE phone=%s ORDER BY created_at DESC LIMIT %s", (phone, limit))
        rows = list(reversed([dict(r) for r in cursor.fetchall()]))
        conn.close()
        return rows
    except Exception:
        return []


def _get_active_cos_proposal(phone: str, hours: int = 24) -> dict:
    """Busca a ultima proposta CoS Patrol ainda 'aberta' pra esse phone.

    "Aberta" = ja enviada via send_wa_to_renato dentro da janela X, e a ultima
    troca user nao parece ja ter encerrado (heuristica: pega a mais recente
    cos_patrol cuja timestamp > ultima resposta user a outra cos_patrol).

    Retorna dict {id, content, proposed_action, options, urgency, age_hours}
    ou {} se nao houver.
    """
    if not DATABASE_URL:
        return {}
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, content, tool_calls, created_at
            FROM bot_conversations
            WHERE phone = %s
              AND role = 'assistant'
              AND tool_calls IS NOT NULL
              AND tool_calls->>'cos_patrol' = 'true'
              AND created_at > NOW() - (%s || ' hours')::interval
            ORDER BY created_at DESC LIMIT 1
            """,
            (phone, str(hours)),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {}
        tc = row.get("tool_calls") or {}
        if isinstance(tc, str):
            try:
                tc = json.loads(tc)
            except Exception:
                tc = {}
        # Auto-resolve por outgoing: se a proposta tem contact_id e Renato JA
        # enviou outgoing (WA OU email) pra esse contato APOS criar a proposta,
        # considera fechada. Fecha gap de action blindness — Renato responder
        # direto pelo celular/Gmail nao deve mais re-cobrar.
        proposal_contact_id = tc.get("contact_id")
        if proposal_contact_id:
            cursor.execute(
                """
                SELECT 1 FROM messages m
                JOIN conversations cv ON cv.id = m.conversation_id
                WHERE cv.contact_id = %s
                  AND m.direcao = 'outgoing'
                  AND m.enviado_em > %s
                LIMIT 1
                """,
                (proposal_contact_id, row["created_at"]),
            )
            if cursor.fetchone():
                conn.close()
                return {}
        age_hours = None
        try:
            from datetime import datetime as _dt
            now = _dt.now()
            age_hours = round((now - row["created_at"]).total_seconds() / 3600, 1)
        except Exception:
            pass
        return {
            "id": row["id"],
            "content": row["content"],
            "proposed_action": tc.get("proposed_action") or {},
            "options": tc.get("options") or [],
            "urgency": tc.get("urgency"),
            "contact_id": tc.get("contact_id"),
            "context_link": tc.get("context_link"),
            "age_hours": age_hours,
        }
    except Exception as e:
        logger.warning(f"_get_active_cos_proposal failed: {e}")
        return {}


def _save_msg(phone: str, role: str, content: str):
    """Save message to conversation history."""
    if not content or not content.strip():
        return
    garbage = ['demorou demais', 'Erro interno', '__IMAGE_PENDING__', '__AUDIO_PENDING__',
               'indisponível', 'indisponivel', 'Não consigo acessar']
    if any(g in content for g in garbage):
        return
    if not DATABASE_URL:
        return
    try:
        conn = psycopg.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO bot_conversations (phone, role, content) VALUES (%s,%s,%s)", (phone, role, content))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _build_snapshot_block() -> str:
    """Snapshot situacional do INTEL — bot entra na conversa sabendo do estado atual.

    Why: P2 Inteligencia Real — bot reativo demais sem contexto. Reduz tool calls
    obvias e elimina performance theater ("aguarde, vou buscar...").
    """
    sections = []
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT t.id, t.titulo, t.data_vencimento::date AS due, p.nome AS projeto
            FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.status = 'pending' AND t.data_vencimento IS NOT NULL
              AND t.data_vencimento::date <= CURRENT_DATE
            ORDER BY t.data_vencimento ASC, t.prioridade ASC
            LIMIT 5
        """)
        tasks = cursor.fetchall()
        if tasks:
            lines = []
            for t in tasks:
                proj = f" — {t['projeto']}" if t['projeto'] else ""
                lines.append(f"  - [{t['id']}] {t['titulo'][:70]} (venc {t['due']}){proj}")
            sections.append("**Tarefas urgentes (<=hoje):**\n" + "\n".join(lines))

        cursor.execute("""
            SELECT id, summary, start_datetime
            FROM calendar_events
            WHERE start_datetime::date = CURRENT_DATE
              AND end_datetime >= NOW()
            ORDER BY start_datetime ASC
            LIMIT 5
        """)
        events = cursor.fetchall()
        if events:
            lines = [f"  - {e['start_datetime'].strftime('%H:%M')} {e['summary'][:70]}" for e in events]
            sections.append("**Agenda restante hoje:**\n" + "\n".join(lines))
        else:
            sections.append("**Agenda restante hoje:** vazio")

        cursor.execute("""
            SELECT id, nome, circulo, health_score, ultimo_contato::date AS ultimo
            FROM contacts
            WHERE circulo <= 2
              AND health_score IS NOT NULL
              AND health_score < 50
            ORDER BY health_score ASC, ultimo_contato ASC NULLS FIRST
            LIMIT 5
        """)
        cooling = cursor.fetchall()
        if cooling:
            lines = []
            for c in cooling:
                health = c['health_score'] if c['health_score'] is not None else 0
                ult = c['ultimo'] or 'nunca'
                lines.append(f"  - [{c['id']}] {c['nome']} (C{c['circulo']}, health {health}, ult {ult})")
            sections.append("**Contatos esfriando (C1-C2):**\n" + "\n".join(lines))

        cursor.execute("""
            SELECT
                (SELECT COUNT(*) FROM editorial_posts WHERE status = 'scheduled') AS scheduled,
                (SELECT COUNT(*) FROM editorial_posts WHERE status = 'draft') AS drafts,
                (SELECT COUNT(*) FROM hot_takes WHERE status = 'draft') AS hot_drafts,
                (SELECT data_publicacao FROM editorial_posts WHERE status = 'scheduled' ORDER BY data_publicacao ASC LIMIT 1) AS proximo
        """)
        ed = cursor.fetchone()
        if ed and (ed['scheduled'] or ed['drafts'] or ed['hot_drafts']):
            line = f"**Editorial:** {ed['scheduled']} agendados, {ed['drafts']} drafts, {ed['hot_drafts']} hot takes"
            if ed['proximo']:
                line += f" — proximo: {ed['proximo'].strftime('%d/%m %H:%M')}"
            sections.append(line)

        cursor.execute("""
            SELECT id, title, urgency
            FROM action_proposals
            WHERE status = 'pending'
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY CASE urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, criado_em DESC
            LIMIT 3
        """)
        props = cursor.fetchall()
        if props:
            lines = [f"  - [{p['id']}] {p['title'][:80]} ({p['urgency']})" for p in props]
            sections.append(f"**Propostas pendentes ({len(props)}):**\n" + "\n".join(lines))

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM email_triage
            WHERE status = 'pending' AND needs_attention = true
        """)
        row = cursor.fetchone()
        email_pending = row['total'] if row else 0
        if email_pending:
            sections.append(f"**Emails pendentes:** {email_pending}")

        conn.close()
    except Exception as e:
        logger.error(f"Error building snapshot block: {e}")
        return ""

    if not sections:
        return ""

    return "## SITUACAO ATUAL (snapshot — voce JA SABE disso, NAO precisa de tool call pra coisas obvias)\n\n" + "\n\n".join(sections) + "\n\n"


async def _run_bot(phone: str, message: str, message_id: str) -> str:
    """Full bot processing with tool_use loop. Runs on Railway (no timeout)."""
    now = _now_sp()
    snapshot = _build_snapshot_block()

    # CoS Patrol: proposta pendente nas ultimas 24h? Injeta contexto pro bot
    # interpretar a resposta do Renato como decisao sobre a proposta especifica.
    cos_block = ""
    try:
        active = _get_active_cos_proposal(phone, hours=24)
        if active:
            opts_str = ", ".join(f'"{o.get("label", "")}"' for o in (active.get("options") or [])[:6])
            proposed = active.get("proposed_action") or {}
            proposed_str = json.dumps(proposed, ensure_ascii=False, indent=2)[:1500] if proposed else "(nenhuma)"
            cos_block = f"""
## CONTEXTO CRITICO — PROPOSTA CoS PATROL PENDENTE ({active.get('age_hours', '?')}h atras)

Voce (CoS Patrol Agent, rodando 30/30min) mandou esta proposta pra Renato via WA ha {active.get('age_hours', '?')} horas:

\"\"\"
{active.get('content', '')[:1200]}
\"\"\"

**Opcoes que voce apresentou:** [{opts_str}]

**proposed_action (executar se Renato aprovar):**
```json
{proposed_str}
```

**Interprete a mensagem ATUAL do Renato como POSSIVEL resposta a essa proposta:**

- Se ele aprovou ("1", "ok", "pode", "manda", "aprovo", "sim", "vai"): EXECUTE proposed_action via tool apropriada (execute_intel/execute_conselhoos/manage_email/web_search conforme o caso). Confirme em 1 linha.
- Se pediu pra modificar ("muda X", "troca", "reescreve assim"): rascunhe a versao nova e RE-MANDE pra ele aprovar (envie o draft direto perguntando "manda assim?").
- Se descartou ("3", "ignora", "deixa", "nao", "depois"): apenas confirme em 1 linha. Nao execute.
- Se a mensagem dele e ASSUNTO NOVO (nao relacionado): trate normal, ignore essa proposta — nao force o link.
- Se ele perguntou sobre o assunto: responda contextualizadamente e mantenha aberta.

NAO precisa repetir o conteudo da proposta. Voce JA mandou. Vai direto pra acao.

"""
    except Exception as _e:
        logger.warning(f"_run_bot: cos_block build falhou: {_e}")

    system_prompt = f"""Voce e o INTEL Bot, assistente pessoal de Renato Almeida Prado (executivo, tecnologia e governanca).

{snapshot}
{cos_block}
TABELAS INTEL (nomes reais, use EXATAMENTE estes nomes):
- contacts: id, nome, empresa, cargo, circulo, health_score, telefones, emails, linkedin, ultimo_contato, resumo_ai
- messages: id, conversation_id, contact_id, direcao ('incoming'/'outgoing'), conteudo, enviado_em
- conversations: id, contact_id, canal ('whatsapp'/'email'), ultimo_mensagem
- projects: id, nome, descricao, tipo, status, prioridade, data_previsao
- tasks: id, titulo, descricao, status ('pending'/'completed'), data_vencimento, project_id, contact_id, prioridade
- contact_memories: id, contact_id, titulo, resumo, tipo, data_ocorrencia
- contact_facts: id, contact_id, categoria, fato
- calendar_events: id, summary, start_datetime, end_datetime
- project_notes: id, project_id, titulo, conteudo, tipo, criado_em
- action_proposals: id, contact_id, title, description, urgency, status

TABELAS CONSELHOOS:
- empresas: id (uuid), nome, setor, descricao, user_id (SEMPRE '115322753506978752025')
- reunioes: id (uuid), empresa_id, titulo, data, status, ata_md
- raci_itens: id (uuid), empresa_id, area, acao, prazo, status, responsavel_r
- decisoes: id, empresa_id, reuniao_id, decisao, area
- pessoas: id (uuid), empresa_id, nome, cargo, email, intel_contact_id

TOOLS:
- web_search: pesquisar na internet (noticias, empresas, pessoas, qualquer coisa)
- fetch_url: buscar conteudo de uma URL (artigo, pagina). Use summarize=true para resumir.
- query_intel: SELECT no banco INTEL. SEMPRE use nomes de tabela acima.
- query_conselhoos: SELECT no ConselhoOS.
- execute_conselhoos: INSERT/UPDATE/DELETE no ConselhoOS. IDs UUID (gen_random_uuid()).
- execute_intel: criar tarefas, salvar notas, memorias, feedback, salvar artigos.
- manage_email: gerenciar Gmail (archive_non_urgent, list_inbox, archive_by_subject).

EXEMPLOS SQL:
- Tarefas pendentes: SELECT id, titulo, data_vencimento FROM tasks WHERE status = 'pending' ORDER BY data_vencimento
- Contato por nome: SELECT id, nome, empresa FROM contacts WHERE nome ILIKE '%termo%'
- Projetos ativos: SELECT id, nome, tipo FROM projects WHERE status = 'ativo'
- Eventos de uma data: SELECT summary, start_datetime FROM calendar_events WHERE start_datetime::date = '2026-04-28'

REGRAS:
- NUNCA invente informacoes. Consulte antes de afirmar.
- NUNCA diga "Intel indisponivel". Se query falhar, tente de novo com SQL corrigido.
- NUNCA narre o processo: NADA de "buscando...", "aguarde um momento", "deixa eu verificar", "vou consultar". Se precisa de tool, chame e responda DIRETO com o resultado. Se ja tem no snapshot acima, responda ja com a info.
- Se a pergunta e sobre algo que ja esta no snapshot (tarefas hoje, agenda hoje, propostas, contatos esfriando, editorial), responda DIRETO sem tool call.
- Quando pedir para CRIAR no ConselhoOS, use execute_conselhoos com INSERT.
- Responda em portugues, conciso (WhatsApp). Use *negrito* para destaques.
- Data atual: {_format_sp_datetime(now)} (fuso America/Sao_Paulo, sempre)
- Para "segunda", "2a feira" = proximo dia util. Calcule a data.
- Audios transcritos: "[Audio transcrito] texto"
- Imagens analisadas: "[Imagem analisada] descricao"
- Feedback do sistema: use execute_intel save_feedback"""

    # Load history
    history = _load_history(phone)
    _save_msg(phone, "user", message)
    messages = [{"role": r["role"], "content": r["content"]} for r in history] + [{"role": "user", "content": message}]

    # Tool loop
    async with httpx.AsyncClient(timeout=30.0) as client:
        for iteration in range(10):
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1000,
                      "system": system_prompt, "tools": BOT_TOOLS, "messages": messages}
            )

            if resp.status_code != 200:
                logger.error(f"Claude error: {resp.status_code}")
                return None

            result = resp.json()
            text_parts = []
            tool_uses = []

            for block in result.get("content", []):
                if block["type"] == "text":
                    text_parts.append(block["text"])
                elif block["type"] == "tool_use":
                    tool_uses.append(block)

            if result.get("stop_reason") == "end_turn" or not tool_uses:
                response = "\n".join(text_parts)
                _save_msg(phone, "assistant", response)
                return response

            # Execute tools
            messages.append({"role": "assistant", "content": result["content"]})
            tool_results = []
            for tool in tool_uses:
                logger.info(f"Tool: {tool['name']} input: {json.dumps(tool.get('input', {}))[:200]}")
                tool_name = tool["name"]
                tool_input = tool.get("input", {})
                # Route to correct handler
                if tool_name == "web_search":
                    output = await _web_search(tool_input.get("query", ""))
                elif tool_name == "fetch_url":
                    output = await _fetch_url(tool_input.get("url", ""), tool_input.get("summarize", False))
                elif tool_name == "manage_email":
                    output = await _manage_email(tool_input.get("action", ""), tool_input.get("params", {}))
                elif tool_name == "execute_intel":
                    output = await _execute_intel_action(tool_input.get("action", ""), tool_input.get("params", {}))
                else:
                    output = _run_tool(tool_name, tool_input)
                tool_results.append({"type": "tool_result", "tool_use_id": tool["id"], "content": output})
            messages.append({"role": "user", "content": tool_results})

    return None


@app.post("/generate-ata")
async def generate_ata_endpoint(request: Request):
    """
    Generate a comprehensive ata from meeting transcription using Claude.
    Returns immediately, processes in background on Railway (no timeout).
    Saves directly to ConselhoOS database when done.
    """
    import asyncio

    data = await request.json()
    reuniao_id = data.get("reuniao_id")
    transcricao = data.get("transcricao", "")
    empresa_nome = data.get("empresa_nome", "")
    data_reuniao = data.get("data_reuniao", "")
    pauta_md = data.get("pauta_md", "")
    conselhoos_db_url = data.get("conselhoos_db_url") or CONSELHOOS_DATABASE_URL
    participantes_info = data.get("participantes", "")

    if not transcricao or not reuniao_id:
        return JSONResponse({"error": "reuniao_id e transcricao obrigatorios"}, status_code=400)

    logger.info(f"Queuing ata generation for {empresa_nome}, reuniao {reuniao_id} ({len(transcricao)} chars)")
    logger.info(f"ConselhoOS DB URL: {conselhoos_db_url[:30]}..." if conselhoos_db_url else "NO CONSELHOOS_DB_URL!")

    # Fire background task using asyncio (Railway keeps running after response)
    asyncio.create_task(
        _generate_ata_background(
            reuniao_id, transcricao, empresa_nome, data_reuniao,
            pauta_md, conselhoos_db_url, participantes_info
        )
    )

    return {"status": "processing", "message": "Ata sendo gerada em background. Recarregue em ~60s."}


async def _generate_ata_background(
    reuniao_id, transcricao, empresa_nome, data_reuniao,
    pauta_md, conselhoos_db_url, participantes_info
):
    """Background task for ata generation."""
    logger.info(f"[ATA-BG] Starting generation for {empresa_nome} ({len(transcricao)} chars)")

    try:
        await _do_generate_ata(
            reuniao_id, transcricao, empresa_nome, data_reuniao,
            pauta_md, conselhoos_db_url, participantes_info
        )
    except Exception as e:
        logger.error(f"[ATA-BG] FATAL ERROR: {e}", exc_info=True)


async def _do_generate_ata(
    reuniao_id, transcricao, empresa_nome, data_reuniao,
    pauta_md, conselhoos_db_url, participantes_info
):
    """Actual ata generation logic."""

    # Fetch real participant data from ConselhoOS
    if not participantes_info and conselhoos_db_url:
        try:
            with psycopg.connect(conselhoos_db_url) as conn:
                rows = conn.execute("""
                    SELECT p.nome, p.cargo, p.papel
                    FROM pessoas p
                    JOIN reunioes r ON r.empresa_id = p.empresa_id
                    WHERE r.id = %s
                    ORDER BY p.nome
                """, (reuniao_id,)).fetchall()
                if rows:
                    parts = []
                    for r in rows:
                        cargo = r[1] or r[2] or ''
                        parts.append(f"{r[0]} ({cargo})" if cargo else r[0])
                    participantes_info = ", ".join(parts)
                    logger.info(f"[ATA-BG] Found {len(rows)} participants from ConselhoOS")
        except Exception as e:
            logger.warning(f"[ATA-BG] Error fetching participants: {e}")

    prompt = f"""Você é um secretário executivo de alto nível especializado em governança corporativa.
Analise a transcrição desta reunião de conselho e produza uma ATA COMPLETA E DETALHADA.

**Empresa:** {empresa_nome}
**Data:** {data_reuniao}
{f"**Participantes cadastrados (use estes nomes e cargos EXATOS):** {participantes_info}" if participantes_info else ""}
{f"**Pauta prevista:**\n{pauta_md}" if pauta_md else ""}

INSTRUÇÕES DE QUALIDADE:
1. A ata deve ter entre 8.000 e 15.000 caracteres — seja DETALHADO
2. Use tabelas Markdown para dados financeiros (faturamento, metas, indicadores)
3. Cada decisão deve ter: o que foi decidido, por quê, quem é responsável, prazo
4. Cada discussão deve ter: contexto, argumentos apresentados, conclusão
5. Identifique números, valores em R$, percentuais e datas mencionados
6. Capture nuances: preocupações expressas, ressalvas, condições
7. Distingua entre DECISÕES (aprovadas pelo conselho) e PENDÊNCIAS (a resolver)
8. Se houve divergência de opinião, registre ambos os lados
9. Use formatação profissional com cabeçalhos numerados (1., 1.1, 1.2...)

ESTRUTURA OBRIGATÓRIA:

# [EMPRESA] — Ata de Reunião de Conselho
**Data:** ... | **Duração:** ~Xh | **Participantes:** N presentes

## PARTICIPANTES
Lista com nome, cargo/papel e status (presente/ausente)

## 1. CONTEXTO E ABERTURA
Contexto da reunião, revisão de ata anterior se mencionada

## 2. [TEMA PRINCIPAL 1] — título descritivo
### 2.1 Subtema
Análise detalhada com números, tabelas se aplicável

## 3. [TEMA PRINCIPAL 2] — título descritivo
(continuar para cada tema relevante)

## DECISÕES APROVADAS
Lista numerada com responsável e prazo

## PENDÊNCIAS E PRÓXIMOS PASSOS
Lista com responsável e prazo

## PRÓXIMA REUNIÃO
Data se mencionada

---

**Transcrição:**
{transcricao[:80000]}

Produza a ata completa em Markdown.

REGRAS CRÍTICAS:
- NUNCA invente informações não presentes na transcrição
- Use os nomes e cargos EXATOS da lista de participantes cadastrados (se fornecida)
- Se não souber o cargo de alguém, deixe em branco — NÃO invente cargos como CEO, Sócio, etc."""

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 16000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if resp.status_code != 200:
            logger.error(f"Claude API error: {resp.status_code} {resp.text[:200]}")
            return JSONResponse({"error": f"Claude API error: {resp.status_code}"}, status_code=500)

        result = resp.json()
        ata_md = result["content"][0]["text"]
        logger.info(f"Ata generated: {len(ata_md)} chars")

        # Save to ConselhoOS database
        if conselhoos_db_url:
            try:
                with psycopg.connect(conselhoos_db_url) as conn:
                    conn.execute(
                        "UPDATE reunioes SET ata_md = %s, updated_at = NOW() WHERE id = %s",
                        (ata_md, reuniao_id)
                    )
                    conn.commit()
                logger.info(f"Ata saved to ConselhoOS for reuniao {reuniao_id}")
            except Exception as e:
                logger.error(f"Error saving ata to ConselhoOS: {e}")
                return JSONResponse({
                    "status": "generated_not_saved",
                    "error": str(e),
                    "ata_md": ata_md
                }, status_code=200)

        # Notify via WhatsApp
        try:
            await _send_response(
                os.getenv("RENATO_PHONE", "5511984153337"),
                f"✅ Ata gerada para {empresa_nome} ({data_reuniao}). {len(ata_md)} chars. Recarregue a página."
            )
        except Exception:
            pass

        return {"status": "success", "chars": len(ata_md), "ata_md": ata_md}

    except Exception as e:
        logger.error(f"Ata generation error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/transcribe")
async def transcribe_audio(request: Request):
    """Fast-ACK quando silent (source != "bot"): retorna 200 em <500ms e
    processa em background. Resolve cancelamento de async tasks no Vercel
    caller. Bot path (source='bot') segue sync — bot precisa do resultado."""
    data = await request.json()
    if not _check_worker_secret(data.get("secret")):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    if not data.get("phone") or not data.get("key"):
        return JSONResponse(status_code=400, content={"error": "missing phone or key"})

    silent = data.get("source", "bot") != "bot"
    if silent:
        asyncio.create_task(_transcribe_audio_inner(data))
        return {"status": "accepted", "message_id": data.get("message_id", ""), "queued": True}
    return await _transcribe_audio_inner(data)


async def _transcribe_audio_inner(data: dict) -> dict:
    """Logica interna: download + Groq Whisper + persist wa_attachments + sanity check
    + (opcionalmente) forward pro bot quando source='bot'."""
    key = data.get("key", {})
    phone = data.get("phone", "")
    message_id = data.get("message_id", "")
    # source != "bot" (main_instance_audio, main_instance, main_group): audio chegou
    # fora do bot Tonha. Worker so transcreve+salva em wa_attachments; NAO manda
    # feedback pro remetente e NAO repassa pro /api/webhooks/bot-message. So
    # enriquece o RAG. Default "bot" preserva comportamento intel-bot.
    source = data.get("source", "bot")
    instance = data.get("instance") or (
        os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp") if source != "bot"
        else INTEL_BOT_INSTANCE
    )
    silent = source != "bot"

    logger.info(f"Transcription request for {phone} source={source} instance={instance}")

    async def _maybe_respond(msg: str) -> None:
        """Helper: so envia feedback pro remetente se nao for audio silent (instancia principal)."""
        if not silent:
            await _send_response(phone, msg)

    try:
        # Step 1: Download audio from Evolution API (instancia variavel)
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl_resp = await client.post(
                f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{instance}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"message": {"key": key}, "convertToMp4": False}
            )

        if dl_resp.status_code not in (200, 201):
            logger.error(f"Download failed: {dl_resp.status_code}")
            await _maybe_respond("Nao consegui baixar o audio. Pode digitar?")
            return {"error": "download_failed"}

        dl_data = dl_resp.json()
        audio_b64 = dl_data.get("base64", "")
        mimetype = dl_data.get("mimetype", "audio/ogg")

        if not audio_b64:
            await _maybe_respond("Audio vazio. Pode digitar?")
            return {"error": "empty_audio"}

        logger.info(f"Audio downloaded: {len(audio_b64)} chars, type={mimetype}")

        # Step 2: Transcribe with Groq Whisper (free, fast, supports ogg)
        import base64
        audio_bytes = base64.b64decode(audio_b64)

        # Determine file extension from mimetype
        ext_map = {"audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}
        clean_mime = mimetype.split(";")[0].strip() if mimetype else "audio/ogg"
        ext = ext_map.get(clean_mime, "ogg")

        # 14/06/26: melhoria de transcricao —
        # 1. modelo whisper-large-v3 (nao turbo) — mais preciso em PT-BR,
        #    especialmente em audio curto onde turbo aluciana muito;
        # 2. initial_prompt com nomes proprios + jargao do Renato pra Whisper
        #    nao trocar "Tonha" por "tonha de ferro", "imensIAH" por "imensa",
        #    "ConselhoOS" por "conselho/os", etc;
        # 3. response_format=verbose_json pra pegar avg_logprob e no_speech_prob;
        # 4. temperature=0 pra menos alucinacao.
        whisper_prompt = (
            "Renato Almeida Prado, Tonha (assistente), ImensIAH, ConselhoOS, "
            "Vallen Clinic, Almeida Prado, Assespro, Wadhwani, Despertar, "
            "Daniela, Emma Sakamoto, Renato DAP, Manuela, Orestes, Thalita "
            "Mendes, Cecilia Zanotti, Villela, Itausa, Amadeo, Marcelo, "
            "Veridiana, RACI, briefing, dossie, CoS, conselheiro, board, "
            "governanca corporativa."
        )
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"audio.{ext}", audio_bytes, clean_mime)},
                data={
                    "model": "whisper-large-v3",
                    "language": "pt",
                    "prompt": whisper_prompt,
                    "temperature": "0",
                    "response_format": "verbose_json",
                }
            )

        if resp.status_code != 200:
            error_detail = resp.text[:500]
            logger.error(f"Groq transcription failed: {resp.status_code} - {error_detail}")
            await _maybe_respond(f"Erro na transcricao ({resp.status_code}). Pode digitar?")
            return {"error": f"transcription_failed: {resp.status_code}", "detail": error_detail}

        groq_response = resp.json()
        transcription = (groq_response.get("text") or "").strip()

        # Persiste em wa_attachments (idempotente). Brain pode buscar depois
        # via search_context scope='attachments'.
        if transcription and message_id:
            _save_wa_attachment(
                message_id, phone, "audio",
                mime_type=clean_mime, size_bytes=len(audio_bytes),
                extracted_text=transcription, extraction_model="whisper-large-v3",
            )

        # Filtro anti-alucinacao: se Whisper retorna texto curto + segments com
        # no_speech_prob alto OU avg_logprob muito negativo, e provavel
        # alucinacao em audio em silencio/ruido. Avisa e pede digitacao.
        segments = groq_response.get("segments") or []
        if segments:
            avg_no_speech = sum(s.get("no_speech_prob", 0) for s in segments) / len(segments)
            avg_logprob = sum(s.get("avg_logprob", 0) for s in segments) / len(segments)
            logger.info(f"Whisper quality: no_speech={avg_no_speech:.2f}, logprob={avg_logprob:.2f}, len={len(transcription)}")
            if avg_no_speech > 0.6 or avg_logprob < -1.0:
                logger.warning(f"Whisper hallucination suspected (no_speech={avg_no_speech:.2f}, logprob={avg_logprob:.2f})")
                await _maybe_respond("Nao consegui entender o audio (qualidade baixa ou silencio). Pode digitar ou mandar de novo?")
                return {"error": "hallucination_filter", "no_speech": avg_no_speech, "logprob": avg_logprob}

        if not transcription:
            await _maybe_respond("Nao consegui entender o audio. Pode digitar?")
            return {"error": "empty_transcription"}

        logger.info(f"Transcribed ({len(transcription)} chars): {transcription[:120]}")

        # 14/06/26: Validacao semantica via Claude antes de mandar pro INTEL.
        # Whisper-large-v3 ainda aluciana em audio improvisado/curto/baixo SNR
        # — inventa nomes proprios ("Rui Teino", "Ediliano Paulini",
        # "Associacao dos Profetores dos Estudantes Unidos") com aparencia
        # convincente. Filtro de no_speech_prob nao pega (Whisper retorna
        # logprob "saudavel" mesmo alucinando). Claude faz sanity check
        # adicional. Se claramente alucinacao, descarta.
        ANTHROPIC_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if ANTHROPIC_KEY and len(transcription) > 10:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    sanity_resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": ANTHROPIC_KEY,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json={
                            "model": "claude-haiku-4-5-20251001",
                            "max_tokens": 80,
                            "messages": [{
                                "role": "user",
                                "content": (
                                    "Voce e um classificador de transcricoes de audio em PT-BR. "
                                    "O contexto: Renato Almeida Prado mandou audio pra sua assistente "
                                    "Tonha. Whisper transcreveu o audio. "
                                    "Diga se a transcricao parece (A) COERENTE — frases com sentido "
                                    "e nexo, mesmo que improvisada/conversacional; ou (B) ALUCINACAO "
                                    "— frases desconexas, nomes proprios estranhos sem contexto "
                                    "(ex: 'Rui Teino', 'Ediliano Paulini'), referencias a "
                                    "'Associacao dos Profetores', 'Poder da Nova Iorque', "
                                    "'projeto de explotacao de cafe', frases que nao se conectam.\n\n"
                                    f"Transcricao: \"{transcription[:600]}\"\n\n"
                                    "Responda APENAS uma palavra: COERENTE ou ALUCINACAO."
                                ),
                            }],
                        },
                    )
                    if sanity_resp.status_code == 200:
                        verdict = (sanity_resp.json()["content"][0]["text"] or "").strip().upper()
                        logger.info(f"Sanity check verdict: {verdict}")
                        if "ALUCINACAO" in verdict or "ALUCINAÇÃO" in verdict:
                            await _maybe_respond(
                                "Whisper alucinou na transcricao do audio (sai com nomes/eventos "
                                "que nao existem). Pode mandar de novo ou digitar? Audio curto ou "
                                "baixo volume costuma dar isso."
                            )
                            return {
                                "error": "sanity_check_hallucination",
                                "transcription": transcription[:200],
                            }
                    else:
                        logger.warning(f"Sanity check API {sanity_resp.status_code}")
            except Exception as e:
                logger.warning(f"Sanity check failed (passing through): {e}")

        # main_instance_audio: ja gravamos em wa_attachments; NAO repassar pro bot
        # nem responder ao remetente. Apenas enriquece o RAG pra Tonha consultar.
        if silent:
            logger.info(f"silent transcribe done — msg={message_id} len={len(transcription)}")
            return {"status": "success_silent", "transcription": transcription[:200]}

        # Step 3: Send transcribed text to INTEL bot for processing
        content = f"[Audio transcrito] {transcription}"

        if debounce.is_enabled():
            # Path audio: runner mantem POST pro Vercel /api/webhooks/bot-message
            # preservando engine legacy (vs path texto que roda _run_bot local).
            queued = await debounce.enqueue(phone, content, message_id, _post_to_intel_bot_webhook)
            return {"status": "queued", "transcription": transcription[:200], **queued}

        # Step 3: Send to intel-bot for full processing (has query_intel, save_memory, etc)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                bot_resp = await client.post(
                    f"{INTEL_API_URL}/api/webhooks/bot-message",
                    headers={"Content-Type": "application/json"},
                    json={"phone": phone, "content": content, "message_id": message_id,
                          "secret": WORKER_SECRET},
                    timeout=55.0
                )
            if bot_resp.status_code == 200:
                return {"status": "success", "transcription": transcription[:200]}
            else:
                logger.warning(f"Bot API failed: {bot_resp.status_code} — fallback envia apologia, NAO eco da transcricao (nao e comportamento de CoS).")
                await _maybe_respond("Deixa eu reler o que voce mandou — te volto em instantes.")
                return {"status": "partial", "transcription": transcription[:200]}
        except httpx.TimeoutException:
            logger.warning("Bot API timeout — fallback apologia, sem eco de transcricao.")
            await _maybe_respond("Demorei mais que o normal pra processar — te volto em instantes.")
            return {"status": "partial_timeout", "transcription": transcription[:200]}

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        await _maybe_respond("Erro ao processar audio. Tenta digitar?")
        return {"error": str(e)}


@app.post("/transcribe-raw")
async def transcribe_raw(request: Request):
    """Transcreve audio via Groq Whisper sem orquestracao do bot.

    Input: {secret, audio_b64, mimetype?, language?}
    Output: {text} ou {error}

    Util pra audios fora do fluxo bot (DMs do rap-whatsapp, conselhos, etc).
    """
    import base64
    data = await request.json()
    if not _check_worker_secret(data.get("secret")):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    audio_b64 = data.get("audio_b64", "")
    mimetype = data.get("mimetype", "audio/ogg")
    language = data.get("language", "pt")
    if not audio_b64:
        return JSONResponse(status_code=400, content={"error": "missing audio_b64"})
    try:
        audio_bytes = base64.b64decode(audio_b64)
        clean_mime = mimetype.split(";")[0].strip() if mimetype else "audio/ogg"
        ext_map = {"audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}
        ext = ext_map.get(clean_mime, "ogg")
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"audio.{ext}", audio_bytes, clean_mime)},
                data={"model": "whisper-large-v3", "language": language, "temperature": "0"},
            )
        if resp.status_code != 200:
            return JSONResponse(status_code=resp.status_code, content={"error": "groq_failed", "detail": resp.text[:500]})
        return {"text": resp.json().get("text", "")}
    except Exception as e:
        logger.exception("transcribe-raw error")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/analyze-image")
async def analyze_image(request: Request):
    """Fast-ACK quando silent (source != "bot"): retorna 200 em <500ms e
    processa em background. Resolve cancelamento de async tasks no Vercel
    caller. Bot path (source='bot') segue sync."""
    data = await request.json()
    if not _check_worker_secret(data.get("secret")):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    if not data.get("phone") or not data.get("key"):
        return JSONResponse(status_code=400, content={"error": "missing phone or key"})

    silent = data.get("source", "bot") != "bot"
    if silent:
        asyncio.create_task(_analyze_image_inner(data))
        return {"status": "accepted", "message_id": data.get("message_id", ""), "queued": True}
    return await _analyze_image_inner(data)


async def _analyze_image_inner(data: dict) -> dict:
    """Logica interna: download + Claude Haiku Vision + persist + forward (bot path)."""
    import base64

    key = data.get("key", {})
    phone = data.get("phone", "")
    message_id = data.get("message_id", "")
    caption = data.get("caption", "")
    source = data.get("source", "bot")
    instance = data.get("instance") or (
        os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp") if source != "bot"
        else INTEL_BOT_INSTANCE
    )
    silent = source != "bot"

    async def _maybe_respond(msg: str) -> None:
        if not silent:
            await _send_response(phone, msg)

    logger.info(f"Image analysis request for {phone} source={source} instance={instance}, caption: {caption[:50]}")

    try:
        # Step 1: Download image from Evolution API (instancia variavel)
        async with httpx.AsyncClient(timeout=30.0) as client:
            dl_resp = await client.post(
                f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{instance}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"message": {"key": key}, "convertToMp4": False}
            )

        if dl_resp.status_code not in (200, 201):
            await _maybe_respond("Nao consegui baixar a imagem.")
            return {"error": "download_failed"}

        dl_data = dl_resp.json()
        image_b64 = dl_data.get("base64", "")
        mimetype = dl_data.get("mimetype", "image/jpeg").split(";")[0].strip()

        if not image_b64:
            await _maybe_respond("Imagem vazia.")
            return {"error": "empty_image"}

        logger.info(f"Image downloaded: {len(image_b64)} chars, type={mimetype}")

        # Step 2: Analyze with Claude Vision
        user_instruction = caption if caption else "Descreva o que voce ve nesta imagem. Se for uma tela do sistema, identifique o que pode ser melhorado. Se for uma mensagem, resuma o conteudo."

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mimetype,
                                    "data": image_b64
                                }
                            },
                            {
                                "type": "text",
                                "text": user_instruction
                            }
                        ]
                    }]
                }
            )

        if resp.status_code != 200:
            logger.error(f"Claude Vision failed: {resp.status_code} - {resp.text[:200]}")
            await _maybe_respond("Erro ao analisar imagem.")
            return {"error": f"vision_failed: {resp.status_code}"}

        analysis = resp.json().get("content", [{}])[0].get("text", "")
        if not analysis:
            await _maybe_respond("Nao consegui analisar a imagem.")
            return {"error": "empty_analysis"}

        logger.info(f"Image analyzed: {analysis[:100]}")

        # Persiste em wa_attachments
        if message_id:
            _save_wa_attachment(
                message_id, phone, "image",
                mime_type=mimetype, size_bytes=int(len(image_b64) * 3 / 4),
                extracted_text=analysis, extraction_model="claude-haiku-4-5-vision",
            )

        # Silent (main instance/group): so persiste em wa_attachments. NAO encaminha
        # pro bot — instancia principal ja roda analyze_message_in_background propria.
        if silent:
            return {"status": "success", "silent": True, "analysis_chars": len(analysis)}

        # Step 3: Send to intel-bot for processing with CRM context
        content = f"[Imagem analisada] {caption + ': ' if caption else ''}{analysis}"

        async with httpx.AsyncClient(timeout=55.0) as client:
            bot_resp = await client.post(
                f"{INTEL_API_URL}/api/webhooks/bot-message",
                headers={"Content-Type": "application/json"},
                json={"phone": phone, "content": content, "message_id": message_id,
                      "secret": WORKER_SECRET},
                timeout=55.0
            )

        if bot_resp.status_code == 200:
            return {"status": "success", "analysis": analysis[:200]}
        else:
            # Fallback: send analysis directly
            await _send_response(phone, f"📸 *Analise da imagem:*\n\n{analysis}")
            return {"status": "partial"}

    except Exception as e:
        logger.error(f"Image analysis error: {e}")
        await _maybe_respond("Erro ao processar imagem.")
        return {"error": str(e)}


def _save_wa_attachment(
    message_id: str, phone: str, kind: str,
    original_filename: str = None, mime_type: str = None,
    size_bytes: int = None, extracted_text: str = None,
    extraction_model: str = None, extraction_cost_usd: float = None,
    error: str = None,
) -> Optional[int]:
    """Persiste anexo WA processado. Idempotente via UNIQUE (message_id, kind)."""
    if not DATABASE_URL or not message_id:
        return None
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO wa_attachments (
                message_id, phone, kind, original_filename, mime_type,
                size_bytes, extracted_text, extraction_model, extraction_cost_usd, error
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (message_id, kind) DO UPDATE
            SET extracted_text = EXCLUDED.extracted_text,
                extraction_model = EXCLUDED.extraction_model,
                extraction_cost_usd = EXCLUDED.extraction_cost_usd,
                error = EXCLUDED.error
            RETURNING id
        """, (
            message_id, phone, kind, original_filename, mime_type,
            size_bytes, extracted_text, extraction_model, extraction_cost_usd, error,
        ))
        row = cur.fetchone()
        conn.commit()
        conn.close()
        return row["id"] if row else None
    except Exception as e:
        logger.warning(f"_save_wa_attachment falhou: {e}")
        return None


@app.post("/analyze-pdf")
async def analyze_pdf(request: Request):
    """Recebe PDF do WhatsApp. Fast-ACK quando silent (source != "bot"):
    retorna 200 em <500ms e processa em background (Railway sustenta loop).
    Resolve cancelamento de async tasks no Vercel serverless caller."""
    data = await request.json()
    if not _check_worker_secret(data.get("secret")):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    if not data.get("phone") or not data.get("key"):
        return JSONResponse(status_code=400, content={"error": "missing phone or key"})

    silent = data.get("source", "bot") != "bot"
    if silent:
        asyncio.create_task(_analyze_pdf_inner(data))
        return {"status": "accepted", "message_id": data.get("message_id", ""), "queued": True}
    return await _analyze_pdf_inner(data)


async def _analyze_pdf_inner(data: dict) -> dict:
    """Logica interna: download Evolution + Claude Sonnet PDF + persist wa_attachments
    + (opcionalmente) forward pro bot quando source='bot'."""
    import base64

    key = data.get("key", {})
    phone = data.get("phone", "")
    message_id = data.get("message_id", "")
    filename = data.get("filename", "documento.pdf")
    caption = data.get("caption", "")
    source = data.get("source", "bot")
    instance = data.get("instance") or (
        os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp") if source != "bot"
        else INTEL_BOT_INSTANCE
    )
    silent = source != "bot"

    async def _maybe_respond(msg: str) -> None:
        if not silent:
            await _send_response(phone, msg)

    logger.info(f"PDF analysis request for {phone} source={source} instance={instance}, file={filename}")

    try:
        # 1. Download PDF base64 from Evolution (instancia variavel)
        async with httpx.AsyncClient(timeout=60.0) as client:
            dl_resp = await client.post(
                f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{instance}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"message": {"key": key}, "convertToMp4": False}
            )
        if dl_resp.status_code not in (200, 201):
            await _maybe_respond("Nao consegui baixar o PDF.")
            _save_wa_attachment(message_id, phone, "pdf", original_filename=filename,
                                error=f"download_failed {dl_resp.status_code}")
            return {"error": "download_failed"}

        dl_data = dl_resp.json()
        pdf_b64 = dl_data.get("base64", "")
        size_bytes = int(len(pdf_b64) * 3 / 4) if pdf_b64 else 0

        if not pdf_b64:
            _save_wa_attachment(message_id, phone, "pdf", original_filename=filename, error="empty_pdf")
            await _maybe_respond("PDF vazio.")
            return {"error": "empty_pdf"}

        logger.info(f"PDF downloaded: {size_bytes} bytes, file={filename}")

        # 2. Send to Claude Sonnet com type=document
        instruction = (
            caption.strip() if caption.strip() else
            f"Extraia e resuma o conteudo deste PDF '{filename}'. "
            "Se for evento/programa: liste datas, local, horarios, participantes/protagonistas com cargos. "
            "Se for contrato/documento: principais pontos + clausulas relevantes. "
            "Se for tabela/dados: estrutura + numeros-chave. "
            "Maximo 800 palavras. Portugues."
        )

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 2000,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                            {"type": "text", "text": instruction},
                        ],
                    }],
                },
            )
        if resp.status_code != 200:
            logger.error(f"Claude PDF failed: {resp.status_code} - {resp.text[:200]}")
            _save_wa_attachment(message_id, phone, "pdf", original_filename=filename,
                                size_bytes=size_bytes, error=f"claude_{resp.status_code}")
            await _maybe_respond("Erro ao analisar PDF.")
            return {"error": f"pdf_failed: {resp.status_code}"}

        result = resp.json()
        extracted = result.get("content", [{}])[0].get("text", "")
        usage = result.get("usage", {}) or {}
        # custo aprox sonnet 4.6: $3/1M in + $15/1M out
        cost = (usage.get("input_tokens", 0) * 3 + usage.get("output_tokens", 0) * 15) / 1_000_000

        if not extracted:
            await _maybe_respond("Nao consegui extrair conteudo do PDF.")
            return {"error": "empty_extraction"}

        # 3. Persiste
        att_id = _save_wa_attachment(
            message_id, phone, "pdf",
            original_filename=filename, mime_type="application/pdf",
            size_bytes=size_bytes, extracted_text=extracted,
            extraction_model="claude-sonnet-4-6", extraction_cost_usd=cost,
        )
        logger.info(f"PDF analyzed: attachment #{att_id}, {len(extracted)} chars, ${cost:.4f}")

        # Silent (main instance/group): so persiste em wa_attachments. NAO encaminha
        # pro bot — instancia principal ja roda analyze_message_in_background propria.
        if silent:
            return {"status": "success", "silent": True, "attachment_id": att_id, "chars": len(extracted)}

        # 4. Forward to bot
        content = f"[PDF anexado: {filename}] {caption + ' — ' if caption else ''}{extracted}"

        async with httpx.AsyncClient(timeout=90.0) as client:
            bot_resp = await client.post(
                f"{INTEL_API_URL}/api/webhooks/bot-message",
                headers={"Content-Type": "application/json"},
                json={"phone": phone, "content": content, "message_id": message_id, "secret": WORKER_SECRET},
                timeout=90.0,
            )

        if bot_resp.status_code == 200:
            return {"status": "success", "attachment_id": att_id, "chars": len(extracted)}
        else:
            await _send_response(phone, f"📄 PDF analisado: {extracted[:1000]}")
            return {"status": "partial"}

    except Exception as e:
        logger.exception(f"PDF analysis error: {e}")
        _save_wa_attachment(message_id, phone, "pdf", original_filename=filename, error=str(e)[:300])
        await _maybe_respond("Erro ao processar PDF.")
        return {"error": str(e)}


async def _send_response(phone: str, message: str):
    """Send WhatsApp message via intel-bot instance."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{EVOLUTION_API_URL}/message/sendText/{INTEL_BOT_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"number": phone, "text": message}
            )
    except Exception as e:
        logger.error(f"Failed to send response: {e}")


# ============== GMAIL SYNC JOB ==============
# Migrated from Vercel cron (services/gmail_sync.py) — was timing out at 300s
# because of O(N×M) loop over 3.5k contacts × 2 accounts × 3 emails.

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"


async def _gmail_list_messages(access_token: str, query: str, max_results: int = 100) -> dict:
    """List Gmail messages via REST API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": query, "maxResults": max_results, "includeSpamTrash": False},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code != 200:
            return {"error": resp.text[:200]}
        return resp.json()


async def _gmail_get_message_metadata(access_token: str, message_id: str) -> dict:
    """Fetch single message metadata."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "metadata"},
        )
        if resp.status_code == 401:
            return {"error": "token_expired"}
        if resp.status_code != 200:
            return {"error": resp.text[:200]}
        return resp.json()


def _parse_gmail_date(date_str: str):
    """Parse RFC 2822 Gmail date header to datetime."""
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str)
    except Exception:
        return None


def _parse_gmail_headers(message: dict) -> dict:
    """Extract from/to/date headers from message payload."""
    headers = {}
    for h in message.get("payload", {}).get("headers", []):
        name = (h.get("name") or "").lower()
        if name in ("from", "to", "cc", "bcc", "subject", "date", "message-id"):
            headers[name] = h.get("value", "")
    return headers


async def _refresh_gmail_token_full(account: dict) -> tuple[str | None, str | None]:
    """Refresh access token from refresh_token. Returns (token, error_reason)."""
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        return None, "no_refresh_token"
    client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        return None, "missing_google_client_env"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            err = f"http_{resp.status_code}: {resp.text[:120]}"
            logger.error(f"Gmail token refresh failed: {err}")
            return None, err
        return resp.json().get("access_token"), None
    except Exception as e:
        logger.error(f"Gmail token refresh exception: {e}")
        return None, f"exception: {str(e)[:120]}"


async def _count_messages_for_email(access_token: str, email: str, months_back: int) -> dict:
    """Returns {count, latest_date, error?}."""
    query = f"(from:{email} OR to:{email})"
    if months_back:
        date_after = (datetime.now() - timedelta(days=months_back * 30)).strftime("%Y/%m/%d")
        query += f" after:{date_after}"

    response = await _gmail_list_messages(access_token, query, max_results=100)
    if "error" in response:
        return {"count": 0, "latest_date": None, "error": response["error"]}

    messages = response.get("messages", []) or []
    result = {"count": len(messages), "latest_date": None}

    if messages:
        msg_detail = await _gmail_get_message_metadata(access_token, messages[0]["id"])
        if "error" not in msg_detail:
            headers = _parse_gmail_headers(msg_detail)
            date_str = headers.get("date", "")
            if date_str:
                result["latest_date"] = _parse_gmail_date(date_str)
    return result


async def _sync_contact_emails_worker(
    contact_id: int, email: str, access_token: str, months_back: int
) -> dict:
    """Sync one contact's emails. Updates contacts.total_interacoes/ultimo_contato."""
    res = {"success": False, "count": 0, "updated": False}
    try:
        msg_result = await _count_messages_for_email(access_token, email, months_back)
        if msg_result.get("error") == "token_expired":
            return {"success": False, "error": "token_expired"}
        res["count"] = msg_result["count"]
        if msg_result["count"] <= 0:
            res["success"] = True
            return res

        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT total_interacoes, ultimo_contato FROM contacts WHERE id = %s",
                (contact_id,),
            )
            current = cursor.fetchone()
            if not current:
                return res

            current_interactions = current.get("total_interacoes") or 0
            current_ultimo = current.get("ultimo_contato")
            new_interactions = max(current_interactions, msg_result["count"])
            new_ultimo = msg_result["latest_date"]

            if current_ultimo and msg_result["latest_date"]:
                try:
                    cur_naive = current_ultimo.replace(tzinfo=None) if current_ultimo.tzinfo else current_ultimo
                    lat_naive = msg_result["latest_date"].replace(tzinfo=None) if msg_result["latest_date"].tzinfo else msg_result["latest_date"]
                    new_ultimo = msg_result["latest_date"] if lat_naive > cur_naive else current_ultimo
                except Exception:
                    new_ultimo = msg_result["latest_date"] or current_ultimo
            elif current_ultimo:
                new_ultimo = current_ultimo

            cursor.execute(
                "UPDATE contacts SET total_interacoes = %s, ultimo_contato = %s WHERE id = %s",
                (new_interactions, new_ultimo, contact_id),
            )
            conn.commit()
            res["updated"] = True

        res["success"] = True
    except Exception as e:
        logger.error(f"sync_contact {contact_id} error: {e}")
        res["error"] = str(e)
    return res


CHUNK_SIZE_GMAIL_SYNC = 300  # contatos por invocacao do worker


async def _gmail_sync_load_state(job_id: int) -> tuple[dict, int]:
    """Le state persistido em background_jobs.result. Retorna (result_dict, total_items)."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT total_items, result FROM background_jobs WHERE id=%s",
            (job_id,),
        )
        row = cursor.fetchone()
    if not row:
        return {}, 0
    return (row.get("result") or {}), int(row.get("total_items") or 0)


async def _gmail_sync_save_state(job_id: int, result: dict, processed_items: int,
                                  force: bool = False) -> bool:
    """Persiste cursor + stats. Default = optimistic concurrency check.

    Why optimistic check: continuation POSTs podem chegar duplicados (LB retry).
    Sem checagem, dois chunks rodam em paralelo: ambos load proc=N, ambos
    escrevem N+300 — flutuacao na row.

    force=True: usado em transicoes de conta, quando processed_items NAO
    avanca (so o cursor muda — e.g., conta 1 esgotada, current_account=None).
    Sem force, transicao falharia (N < N = false) e worker ficava preso.
    Transicoes sao idempotentes (mesmo cursor final), ent two writes are safe.
    """
    stats = result.get("stats") or {}
    with psycopg.connect(DATABASE_URL) as conn:
        cur = conn.cursor()
        if force:
            cur.execute(
                "UPDATE background_jobs SET processed_items=%s, success_count=%s, "
                "failed_count=%s, result=%s WHERE id=%s RETURNING id",
                (processed_items, stats.get("updated", 0), stats.get("errors", 0),
                 json.dumps(result), job_id),
            )
        else:
            cur.execute(
                "UPDATE background_jobs SET processed_items=%s, success_count=%s, "
                "failed_count=%s, result=%s "
                "WHERE id=%s AND processed_items < %s "
                "RETURNING id",
                (processed_items, stats.get("updated", 0), stats.get("errors", 0),
                 json.dumps(result), job_id, processed_items),
            )
        won = cur.fetchone() is not None
        conn.commit()
    if not won and not force:
        logger.warning(f"[GmailSync job={job_id}] save_state perdeu race "
                       f"(quis={processed_items}, mas DB ja avancou) — abort chunk")
    return won


async def _gmail_sync_finish(job_id: int, status: str, result: dict, processed_items: int, error: str | None = None):
    """Marca job como completed ou error."""
    stats = result.get("stats") or {}
    with psycopg.connect(DATABASE_URL) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE background_jobs SET status=%s, processed_items=%s, "
            "success_count=%s, failed_count=%s, result=%s, error=%s, completed_at=NOW() WHERE id=%s",
            (status, processed_items, stats.get("updated", 0), stats.get("errors", 0),
             json.dumps(result), (error[:500] if error else None), job_id),
        )
        conn.commit()


# V7: chunks rodam num LOOP dentro da mesma BackgroundTask.
# Sem HTTP self-dispatch, sem asyncio.create_task novo.
# Cursor salvo por chunk garante que crashes sao recuperaveis.
async def _gmail_sync_dispatch_continuation(job_id: int, months_back: int):
    """No-op em V7. O loop em _run_gmail_sync_loop cuida do proximo chunk."""
    pass


async def _run_gmail_sync_chunk(job_id: int, months_back: int = 1):
    """Process ONE chunk of gmail sync (max CHUNK_SIZE contatos) entao termina.

    Pattern resumivel: state persistido em background_jobs.result.
    Cada invocacao avanca o cursor; auto-dispatch do proximo chunk via httpx.
    Robust contra: Railway idle kill, OOM, restart de processo, race conditions.
    """
    import asyncio as _aio
    logger.info(f"[GmailSync job={job_id}] chunk start (months_back={months_back})")

    # Load persisted state
    result, total_items = await _gmail_sync_load_state(job_id)
    cursor_state = result.get("cursor") or {}
    stats = result.get("stats") or {"updated": 0, "errors": 0, "processed": 0,
                                     "accounts": 0, "error_samples": []}
    accounts_done = cursor_state.get("accounts_done", [])
    current_account_id = cursor_state.get("current_account_id")
    last_contact_id = cursor_state.get("last_contact_id", 0)

    try:
        # First-time setup: total_items not yet computed
        if not total_items:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM contacts "
                            "WHERE emails IS NOT NULL AND emails::text != '[]'")
                contacts_n = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) AS n FROM google_accounts WHERE conectado=TRUE")
                accounts_n = cur.fetchone()["n"]
                if not accounts_n:
                    await _gmail_sync_finish(job_id, "error", {"stats": stats}, 0,
                                             "Nenhuma conta Gmail conectada")
                    return
                total_items = contacts_n * accounts_n
                cur.execute("UPDATE background_jobs SET total_items=%s WHERE id=%s",
                            (total_items, job_id))
                conn.commit()
            stats["accounts"] = accounts_n

        # Pick next account if needed
        if not current_account_id:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                cur = conn.cursor()
                if accounts_done:
                    cur.execute(
                        "SELECT id FROM google_accounts WHERE conectado=TRUE "
                        "AND NOT (id = ANY(%s)) ORDER BY id LIMIT 1",
                        (accounts_done,),
                    )
                else:
                    cur.execute("SELECT id FROM google_accounts WHERE conectado=TRUE ORDER BY id LIMIT 1")
                row = cur.fetchone()
            if not row:
                # All accounts done — finalize
                processed_items = stats.get("processed", 0)
                await _gmail_sync_finish(job_id, "completed",
                                         {"stats": stats, "cursor": cursor_state},
                                         processed_items)
                logger.info(f"[GmailSync job={job_id}] completed: {stats}")
                return
            current_account_id = row["id"]
            last_contact_id = 0

        # Load account + refresh token
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM google_accounts WHERE id=%s", (current_account_id,))
            account = cur.fetchone()
        account_email = (account or {}).get("email", "")
        access_token, refresh_err = await _refresh_gmail_token_full(account)
        if not access_token:
            stats["errors"] += 1
            if len(stats.get("error_samples", [])) < 5:
                stats.setdefault("error_samples", []).append(f"{account_email}: {refresh_err}")
            logger.warning(f"[GmailSync job={job_id}] No token for {account_email}: {refresh_err}")
            accounts_done.append(current_account_id)
            current_account_id = None
            last_contact_id = 0
            cursor_state.update({"accounts_done": accounts_done,
                                 "current_account_id": None, "last_contact_id": 0})
            # force=True: transicao de conta nao avanca processed_items, so cursor
            await _gmail_sync_save_state(job_id, {"stats": stats, "cursor": cursor_state},
                                          stats.get("processed", 0), force=True)
            await _gmail_sync_dispatch_continuation(job_id, months_back)
            return

        # Load chunk of contacts
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, nome, emails FROM contacts "
                "WHERE emails IS NOT NULL AND emails::text != '[]' AND id > %s "
                "ORDER BY id LIMIT %s",
                (last_contact_id, CHUNK_SIZE_GMAIL_SYNC),
            )
            contacts_chunk = cur.fetchall()

        if not contacts_chunk:
            # Account exhausted — switch to next
            try:
                with psycopg.connect(DATABASE_URL) as conn:
                    conn.execute("UPDATE google_accounts SET ultima_sync=CURRENT_TIMESTAMP WHERE id=%s",
                                 (current_account_id,))
                    conn.commit()
            except Exception:
                pass
            accounts_done.append(current_account_id)
            current_account_id = None
            last_contact_id = 0
            cursor_state.update({"accounts_done": accounts_done,
                                 "current_account_id": None, "last_contact_id": 0})
            # force=True: account exhausted nao avanca processed_items, so cursor
            await _gmail_sync_save_state(job_id, {"stats": stats, "cursor": cursor_state},
                                          stats.get("processed", 0), force=True)
            await _gmail_sync_dispatch_continuation(job_id, months_back)
            return

        # Process this chunk
        for contact in contacts_chunk:
            contact_id = contact["id"]
            emails_data = contact.get("emails")
            email_list = []
            if isinstance(emails_data, str):
                try:
                    email_list = json.loads(emails_data)
                except Exception:
                    email_list = [{"email": emails_data}]
            elif isinstance(emails_data, list):
                email_list = emails_data

            for email_obj in email_list[:3]:
                email = email_obj.get("email", "") if isinstance(email_obj, dict) else str(email_obj)
                if not email or email == account_email:
                    continue
                res = await _sync_contact_emails_worker(contact_id, email.lower(),
                                                        access_token, months_back)
                if res.get("error") == "token_expired":
                    access_token, _ = await _refresh_gmail_token_full(account)
                    if not access_token:
                        break
                if res.get("updated"):
                    stats["updated"] += 1
                if res.get("error") and res.get("error") != "token_expired":
                    stats["errors"] += 1
                await _aio.sleep(0.1)

            stats["processed"] += 1
            last_contact_id = contact_id

        # Save state and dispatch next chunk (so se ganhar o race)
        cursor_state.update({"accounts_done": accounts_done,
                             "current_account_id": current_account_id,
                             "last_contact_id": last_contact_id})
        won = await _gmail_sync_save_state(job_id, {"stats": stats, "cursor": cursor_state},
                                            stats.get("processed", 0))
        logger.info(f"[GmailSync job={job_id}] chunk done: processed={stats['processed']}/{total_items} "
                    f"updated={stats['updated']} errors={stats['errors']} won={won}")
        if won:
            await _gmail_sync_dispatch_continuation(job_id, months_back)

    except Exception as e:
        logger.exception(f"[GmailSync job={job_id}] chunk error")
        # NAO marca status='error' aqui — proxima invocacao pode tentar de novo.
        # So loga e deixa o cron eventual reprocessar.
        try:
            cursor_state.update({"accounts_done": accounts_done,
                                 "current_account_id": current_account_id,
                                 "last_contact_id": last_contact_id})
            await _gmail_sync_save_state(job_id, {"stats": stats, "cursor": cursor_state,
                                                   "last_error": str(e)[:200]},
                                          stats.get("processed", 0))
        except Exception:
            pass


async def _run_gmail_sync_loop(job_id: int, months_back: int = 1):
    """Roda chunks em loop ate o job estar completo ou crashar.

    Why: V5/V6 (asyncio.create_task) e V3/V4 (HTTP self-dispatch) ambos
    paravam a chain depois de 1-5 chunks. Loop in-process dentro da
    mesma BackgroundTask elimina toda transicao entre chunks — chunks
    rodam em sequencia, com pequeno yield entre eles.

    Cursor salvo por chunk garante recuperacao se Railway matar o processo.
    """
    import asyncio as _aio
    max_iters = 50  # safety limit (50 chunks × 300 = 15k contatos, mais que suficiente)
    for i in range(max_iters):
        try:
            await _run_gmail_sync_chunk(job_id, months_back)
        except Exception:
            logger.exception(f"[GmailSync job={job_id}] chunk iter {i} crashed")
            break

        # Check if job finalized
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                cur = conn.cursor()
                cur.execute("SELECT status FROM background_jobs WHERE id=%s", (job_id,))
                row = cur.fetchone()
                current_status = row[0] if row else None
        except Exception:
            current_status = None

        if current_status in ("completed", "error", "skipped"):
            logger.info(f"[GmailSync job={job_id}] loop done (status={current_status}, iters={i+1})")
            return

        await _aio.sleep(0.1)  # yield to event loop

    logger.warning(f"[GmailSync job={job_id}] loop hit max_iters={max_iters}")


# Backward-compat alias (codigo antigo pode chamar pelo nome antigo)
_run_gmail_sync = _run_gmail_sync_loop


@app.post("/sync-gmail")
async def sync_gmail(request: Request, background_tasks: BackgroundTasks):
    """
    Receive gmail-sync job from Vercel cron OR self-continuation.

    Body:
      - secret: WORKER_SECRET
      - job_id: id em background_jobs
      - months_back: int (default 1)
      - continuation: bool (true se for self-dispatch do proximo chunk)
    """
    data = await request.json()
    if not _check_worker_secret(data.get("secret")):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    job_id = data.get("job_id")
    months_back = int(data.get("months_back", 1))
    is_continuation = bool(data.get("continuation", False))

    if not job_id:
        return JSONResponse(status_code=400, content={"error": "job_id required"})

    # First call (nao-continuation): claim atomico e idempotency check.
    # Continuation: skip checks — confiamos que veio de nos mesmos.
    if not is_continuation:
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id FROM background_jobs "
                    "WHERE job_type='gmail_sync' AND status='running' "
                    "AND started_at > NOW() - INTERVAL '1 hour' AND id <> %s LIMIT 1",
                    (job_id,),
                )
                existing = cursor.fetchone()
                if existing:
                    cursor.execute(
                        "UPDATE background_jobs SET status='skipped', "
                        "error=%s, completed_at=NOW() WHERE id=%s",
                        (f"another job ({existing['id']}) already running", job_id),
                    )
                    conn.commit()
                    return JSONResponse(
                        status_code=202,
                        content={"status": "skipped", "reason": "already_running",
                                 "running_job_id": existing["id"]},
                    )

                # Claim
                cursor.execute(
                    "UPDATE background_jobs SET status='running', started_at=NOW() "
                    "WHERE id=%s AND status='queued' RETURNING id",
                    (job_id,),
                )
                claimed = cursor.fetchone()
                conn.commit()
                if not claimed:
                    return JSONResponse(
                        status_code=202,
                        content={"status": "skipped", "reason": "already_claimed",
                                 "job_id": job_id},
                    )
        except Exception as e:
            logger.error(f"[GmailSync] idempotency check failed: {e}")

    # V7: loop interno processa todos os chunks. Continuation flag agora é
    # ignorada (toda chamada apos claim sucesso roda o loop ate o fim).
    # Se Railway matar o processo, cursor salvo permite retomar via novo dispatch.
    background_tasks.add_task(_run_gmail_sync_loop, job_id, months_back)
    return JSONResponse(status_code=202,
                        content={"status": "accepted", "job_id": job_id,
                                 "continuation": is_continuation})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
