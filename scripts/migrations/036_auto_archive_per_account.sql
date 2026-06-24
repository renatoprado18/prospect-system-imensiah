-- 036_auto_archive_per_account.sql
-- Auto-archive gate (24/06/2026).
--
-- Substitui o flag global AUTO_ARCHIVE_ENABLED em services/email_triage.py
-- por config per conta. Permite destravar profissional e pessoal
-- independentemente (perfis de ruido bem diferentes).
--
-- Criterio pra destravar: FP rate < 1% nos ultimos 14 dias.
-- Decidido manualmente (Q4=B) — cron diario avisa quando elegivel,
-- Renato libera explicitamente.

ALTER TABLE google_accounts
  ADD COLUMN IF NOT EXISTS auto_archive_enabled BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS auto_archive_enabled_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS auto_archive_enabled_by TEXT;

-- Telemetria: registro historico de avaliacoes do gate
CREATE TABLE IF NOT EXISTS auto_archive_gate_evals (
  id SERIAL PRIMARY KEY,
  account_email TEXT NOT NULL,
  window_days INTEGER NOT NULL DEFAULT 14,
  total_proposed INTEGER NOT NULL,
  decided INTEGER NOT NULL,
  archived INTEGER NOT NULL,
  rejected INTEGER NOT NULL,
  fp_rate DOUBLE PRECISION,
  eligible BOOLEAN NOT NULL,
  recommendation TEXT,
  criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gate_evals_account_recent
  ON auto_archive_gate_evals(account_email, criado_em DESC);
