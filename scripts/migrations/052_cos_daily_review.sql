-- 052 — Camada de inteligência: storage do debriefing diário por frente (fase 0).
-- O loop frente_review roda 1x/dia (worker), raciocina por frente e guarda o
-- payload aqui pro cockpit ler (read-only; sem ação). run_date = dia BRT do run.
CREATE TABLE IF NOT EXISTS cos_daily_review (
    id          SERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_date    DATE        NOT NULL,
    n_frentes   INT,
    n_precisa   INT,
    payload     JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cos_daily_review_run_at ON cos_daily_review (run_at DESC);
