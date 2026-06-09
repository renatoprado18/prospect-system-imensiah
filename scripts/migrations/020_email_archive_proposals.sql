-- 020_email_archive_proposals.sql
-- Email triage shadow mode (07/06/2026).
--
-- Tabela pra propostas de auto-archive que o sweep_email_triage detecta
-- mas NAO executa (shadow mode 2 semanas). Renato ratifica em lote
-- semanalmente via endpoint /api/email-triage/archive-proposals/review.
--
-- Apos 14 dias com FP rate < 1% (rejected / decided), liga
-- AUTO_ARCHIVE_ENABLED=True em services/email_triage.py.

CREATE TABLE IF NOT EXISTS email_archive_proposals (
  id                     SERIAL PRIMARY KEY,
  email_triage_id        INT REFERENCES email_triage(id) ON DELETE CASCADE,
  message_id             TEXT NOT NULL,           -- gmail_id (external_id em messages)
  account_email          TEXT NOT NULL,
  sender                 TEXT,
  subject                TEXT,
  classification_reason  TEXT,
  ai_confidence          DOUBLE PRECISION,
  status                 TEXT DEFAULT 'shadow'
                          CHECK (status IN ('shadow', 'approved', 'rejected', 'archived', 'expired')),
  ratified_at            TIMESTAMP,
  ratified_by            TEXT,                    -- 'renato' | 'auto' | NULL
  criado_em              TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eap_status ON email_archive_proposals(status, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_eap_message_id ON email_archive_proposals(message_id);
CREATE INDEX IF NOT EXISTS idx_eap_triage ON email_archive_proposals(email_triage_id);
