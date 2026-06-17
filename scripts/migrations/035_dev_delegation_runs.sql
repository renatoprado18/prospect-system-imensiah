-- 035_dev_delegation_runs.sql
-- Telemetria das execucoes do consumer cron pra delegations dev.
-- Tonha cria delegations(delegated_to='dev'). Cron pickup chama o worker
-- claude-code-delegator (Railway) que roda Claude Code SDK headless. Resultado
-- volta como response na delegations + signal pra Tonha surfacear.
--
-- Cada chamada vira 1 row aqui: payload enviado, output, custo, status.
-- Permite auditar drift de custo + debug quando Tonha re-delega seguidamente.

CREATE TABLE IF NOT EXISTS dev_delegation_runs (
    id BIGSERIAL PRIMARY KEY,
    delegation_id BIGINT REFERENCES delegations(id) ON DELETE CASCADE,
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP,
    duration_ms INT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN (
        'running', 'success', 'error', 'timeout', 'skipped', 'capped'
    )),
    mode TEXT NOT NULL DEFAULT 'investigate' CHECK (mode IN ('investigate','edit','full')),
    shadow BOOLEAN NOT NULL DEFAULT FALSE,
    request_payload JSONB,
    response_text TEXT,
    response_summary TEXT,
    cost_usd NUMERIC(10, 6),
    turn_count INT,
    tools_used JSONB,
    error_message TEXT,
    created_by TEXT NOT NULL DEFAULT 'cron'
);

CREATE INDEX IF NOT EXISTS idx_dev_delegation_runs_delegation
    ON dev_delegation_runs(delegation_id);
CREATE INDEX IF NOT EXISTS idx_dev_delegation_runs_recent
    ON dev_delegation_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_dev_delegation_runs_today_cost
    ON dev_delegation_runs(started_at) WHERE status='success';

COMMENT ON TABLE dev_delegation_runs IS 'Telemetria do consumer cron de delegations dev (claude-code-delegator). 1 row por chamada pro worker. Source of truth pra cap de custo diario.';
