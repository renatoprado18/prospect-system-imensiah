-- 037_wa_triage.sql
-- F3.1 WA window classifier shadow mode (28/06/2026).
--
-- Tabela espelho do email_triage adaptada pra WhatsApp. Shadow mode:
-- classifica msg incoming com janela de 5 turnos prior + politica por
-- contact_circulo, mas NAO cria action_proposal ainda.
--
-- Sweep 4/4h (Vercel cron). Batched Claude call com prompt cache.

CREATE TABLE IF NOT EXISTS wa_triage (
  id SERIAL PRIMARY KEY,
  message_id INTEGER UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  contact_circulo SMALLINT,

  -- Output do classifier
  classification TEXT,        -- must_read | follow_up | silent | archive
  intent TEXT,                -- pergunta | combinacao | broadcast | social | informacao | outro
  priority SMALLINT DEFAULT 5,
  ai_confidence DOUBLE PRECISION DEFAULT 0.0,
  reasoning TEXT,

  -- Diagnostico LLM
  thread_window_size SMALLINT,  -- qtos turnos foram lidos
  llm_input_tokens INTEGER,
  llm_output_tokens INTEGER,
  llm_cache_read_tokens INTEGER,
  llm_cache_creation_tokens INTEGER,

  -- Rastreio
  batch_id TEXT,             -- agrupa msgs do mesmo sweep run
  trigger_source TEXT,       -- 'sweep_4h' | 'l0_circulo1' (futuro)
  status TEXT DEFAULT 'shadow',  -- shadow | actioned | dismissed
  criado_em TIMESTAMP DEFAULT NOW(),
  processed_em TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_wa_triage_message ON wa_triage(message_id);
CREATE INDEX IF NOT EXISTS idx_wa_triage_contact ON wa_triage(contact_id);
CREATE INDEX IF NOT EXISTS idx_wa_triage_batch ON wa_triage(batch_id);
CREATE INDEX IF NOT EXISTS idx_wa_triage_status ON wa_triage(status);
CREATE INDEX IF NOT EXISTS idx_wa_triage_classification ON wa_triage(classification);
CREATE INDEX IF NOT EXISTS idx_wa_triage_criado ON wa_triage(criado_em DESC);
