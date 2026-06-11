-- Migration 024: webhook_audit
-- Telemetria de cada chamada do webhook Evolution (MESSAGES_UPSERT etc).
-- Why: mensagens do Felipe Orioli (351938588722, jid 197864965841105@lid)
-- somem silenciosamente entre webhook e tabela `messages`. Sem audit,
-- impossivel saber em qual ramo do handler a msg foi descartada.
--
-- Cada chamada do handler grava 1 row com `decision` em
-- {processed, skipped, error} + reason. Telemetria nunca pode falhar o
-- webhook — INSERT em try/except defensivo no codigo.

CREATE TABLE IF NOT EXISTS webhook_audit (
  id BIGSERIAL PRIMARY KEY,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source TEXT NOT NULL,
  event_type TEXT,
  instance TEXT,
  remote_jid TEXT,
  remote_jid_alt TEXT,
  from_me BOOLEAN,
  message_id TEXT,
  decision TEXT NOT NULL,
  decision_reason TEXT,
  resulting_message_id INTEGER,
  payload JSONB NOT NULL,
  processing_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_webhook_audit_received ON webhook_audit(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_audit_jid ON webhook_audit(remote_jid);
CREATE INDEX IF NOT EXISTS idx_webhook_audit_decision ON webhook_audit(decision) WHERE decision != 'processed';
