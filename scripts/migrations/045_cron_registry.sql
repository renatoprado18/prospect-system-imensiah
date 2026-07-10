-- 045_cron_registry.sql
-- Meta-fix (10/07/2026) — mata o drift do monitor-cron-health.
--
-- Problema: JOB_INTERVALS vivia hardcoded em app/main.py (deploy Vercel), lista
-- paralela ao _SCHEDULER_JOBS real do worker Railway. Ao desligar um job no
-- worker (briefings aposentados 23/06; LinkedIn Bloco B suspenso 10/07), ninguem
-- tirava do monitor -> ele gritava "stale" eternamente sobre jobs mortos POR
-- DESIGN. Descoberto quando daily-morning/evening-briefing apareceram stale ha
-- ~18 dias (ok:false permanente = alerta de cron-health virou ruido ignoravel).
--
-- Fix: o worker Railway vira a UNICA fonte da verdade. No boot ele reescreve
-- esta tabela (active=FALSE em tudo, depois upsert active=TRUE nos jobs vivos de
-- _SCHEDULER_JOBS + _LOCAL_DEV_DELEGATION_JOBS). O monitor le daqui. Desligar/
-- comentar um job em UM lugar (worker) passa a propagar pro monitor sozinho.
-- Intervalo derivado do proprio CronTrigger (workers/audio-transcriber/main.py
-- :_interval_min_from_trigger) — sem parsear cron na mao.
--
-- A tabela nasce VAZIA de proposito: o seed e responsabilidade do worker no
-- boot (evita reintroduzir a lista paralela que este fix elimina). Enquanto
-- vazia (janela entre deploy Vercel e reboot do worker), o monitor pula o check
-- e loga — degradacao segura de 1 ciclo horario.
--
-- Idempotente.

CREATE TABLE IF NOT EXISTS cron_registry (
    job_id                TEXT PRIMARY KEY,
    expected_interval_min INTEGER NOT NULL,
    active                BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at            TIMESTAMP NOT NULL DEFAULT now()
);

-- Monitor consulta WHERE active = TRUE; index cobre o filtro.
CREATE INDEX IF NOT EXISTS idx_cron_registry_active
    ON cron_registry(active)
    WHERE active = TRUE;
