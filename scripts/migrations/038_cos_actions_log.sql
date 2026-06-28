-- 038_cos_actions_log.sql
-- Log de acoes da skill /papel-cos (28/06/2026).
--
-- Cada invocacao da skill abre um sweep_id (uuid). Cada item classificado
-- vira uma row aqui com bucket (auto/propose/silence). Acoes auto-resolvidas
-- em BG gravam result + rollback_hint pra reversao manual.
--
-- Sem cron, sem FK -- skill grava direto, observability + auditoria so.

CREATE TABLE IF NOT EXISTS cos_actions_log (
  id SERIAL PRIMARY KEY,
  sweep_id UUID NOT NULL,

  -- O que foi avaliado
  source_table TEXT NOT NULL,   -- 'action_proposals' | 'tasks' | 'system_feedback'
  source_id INTEGER NOT NULL,
  source_summary TEXT,          -- snapshot legivel pra auditar sem JOIN

  -- Classificacao
  bucket TEXT NOT NULL,         -- 'auto' | 'propose' | 'silence'
  bucket_reason TEXT,           -- regra/heuristica que decidiu (ex: 'template:update_contact_phone')

  -- Execucao (so bucket=auto)
  action_type TEXT,             -- 'noop' pra propose/silence
  action_params JSONB,
  rollback_hint TEXT,           -- SQL ou instrucao pra reverter
  status TEXT DEFAULT 'pending',-- pending | running | done | failed | skipped
  result JSONB,
  error TEXT,

  criado_em TIMESTAMP DEFAULT NOW(),
  finished_em TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cos_actions_sweep ON cos_actions_log(sweep_id);
CREATE INDEX IF NOT EXISTS idx_cos_actions_status ON cos_actions_log(status);
CREATE INDEX IF NOT EXISTS idx_cos_actions_bucket ON cos_actions_log(bucket);
CREATE INDEX IF NOT EXISTS idx_cos_actions_source ON cos_actions_log(source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_cos_actions_criado ON cos_actions_log(criado_em DESC);
