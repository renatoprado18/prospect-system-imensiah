-- 023_cron_heartbeats.sql
-- Heartbeat table pra detectar silent failures de crons.
--
-- Contexto: 10-11/06/2026 o briefing matinal nao chegou. Investigacao mostrou
-- drift acumulado do GH Actions scheduler (1h->3h->5h ao longo de 03-09/jun)
-- e drop total nos dias 10 e 11. Migracao pro Railway worker (APScheduler
-- in-process, sem drift) resolveu o disparo, mas precisamos de telemetria
-- pra detectar regressao futura.
--
-- Cada job no Railway worker insere uma linha aqui apos o HTTP GET pro endpoint
-- /api/cron/*. Cron de monitor (1x/hora) compara MAX(fired_at) por job_id com
-- intervalo esperado e alerta via WA se gap > 2x.

CREATE TABLE IF NOT EXISTS cron_heartbeats (
  id SERIAL PRIMARY KEY,
  job_id TEXT NOT NULL,
  fired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  http_status INTEGER,
  duration_ms INTEGER,
  source TEXT NOT NULL DEFAULT 'railway-worker'
);

CREATE INDEX IF NOT EXISTS idx_cron_heartbeats_job_time ON cron_heartbeats(job_id, fired_at DESC);
