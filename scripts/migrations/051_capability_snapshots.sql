-- 051_capability_snapshots.sql
-- F-E instrumentacao v0 — capability registry.
-- Serie point-in-time da retro PDCA quinzenal (1a em 31/07): 1 linha por
-- capacidade por dia, com as 3 dimensoes custo/uso/valor sobre uma janela.
--
-- Por que snapshot alem do rollup on-demand (build_registry): custo e uso ja
-- sao eventos timestampados (a serie existe em cron_runs/tonia_llm_usage), MAS
-- os proxies point-in-time (nº pending HOJE, ratio de sinais abertos) NAO viram
-- serie sozinhos — eles descrevem o estado do momento. O snapshot diario os
-- acumula pra retro enxergar TENDENCIA (o ratio de valor de um detector esta
-- subindo ou caindo?). So o rollup nunca mostraria isso.
--
-- Escrita unica de todo o F-E (o resto e read-only sobre a telemetria).
-- Idempotente — seguro reaplicar. UPSERT por (snapshot_date, capability_key).

CREATE TABLE IF NOT EXISTS capability_snapshots (
    id              SERIAL PRIMARY KEY,
    snapshot_date   DATE        NOT NULL,        -- dia UTC do snapshot
    capability_key  TEXT        NOT NULL,        -- ex 'detector:detector_inbox', 'cron:/api/cron/...', 'llm:briefing.generate', 'proposal:pending_response'
    capability_type TEXT        NOT NULL,        -- detector | cron | llm_function | proposal_source
    window_days     INT         NOT NULL,        -- janela agregada (default 14)
    cost_usd        NUMERIC(12,6),               -- NULL quando a capacidade nao tem custo isolado
    invocations     INTEGER,                     -- volume emitido/rodado na janela; NULL quando nao contavel
    value_acted     INTEGER,                     -- acionado (resolved/executed/success); NULL sem proxy
    value_ignored   INTEGER,                     -- ruido/ignorado (expired/dismissed/rejected/error); NULL sem proxy
    value_ratio     NUMERIC(6,4),                -- acted/(acted+ignored); NULL quando nao ha proxy de valor (honestidade > metrica-teatro)
    extra           JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- breakdown + notas (por que ratio e NULL, avg_duration, tokens, etc.)
    created_at      TIMESTAMP   NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_date, capability_key)
);

CREATE INDEX IF NOT EXISTS idx_capability_snapshots_date
    ON capability_snapshots (snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_capability_snapshots_key
    ON capability_snapshots (capability_key, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_capability_snapshots_type
    ON capability_snapshots (capability_type, snapshot_date DESC);
