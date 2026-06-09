-- 019_email_triage_account.sql
-- Email triage CoS reativacao (07/06/2026).
--
-- Adiciona:
--   1. account_email TEXT — email da conta Gmail (renato@... vs renato.almeida.prado@gmail.com)
--      A coluna `account_type` (professional/personal) ja existe mas nao identifica
--      a conta unica, importante pra rastreabilidade quando ambas estao conectadas.
--   2. UNIQUE (message_id) — garante idempotencia do sweep cron. Sem isso,
--      sweep rodando 2x em <30min duplicava registros pra mesmo email.
--
-- Backfill: rows existentes ficam com account_email=NULL (best-effort).
-- O sweep novo sempre escreve account_email.

ALTER TABLE email_triage
  ADD COLUMN IF NOT EXISTS account_email TEXT;

CREATE INDEX IF NOT EXISTS idx_email_triage_account_email
  ON email_triage(account_email);

-- Constraint UNIQUE em message_id pra evitar duplicacao do sweep.
-- IF NOT EXISTS pra constraint nao tem suporte em PG <15; usamos DO block.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'email_triage_message_id_unique'
  ) THEN
    -- Remove duplicatas antes de aplicar UNIQUE (mantem id mais antigo)
    DELETE FROM email_triage
    WHERE id IN (
      SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
          PARTITION BY message_id ORDER BY criado_em ASC, id ASC
        ) AS rn
        FROM email_triage
        WHERE message_id IS NOT NULL
      ) t
      WHERE t.rn > 1
    );
    ALTER TABLE email_triage
      ADD CONSTRAINT email_triage_message_id_unique UNIQUE (message_id);
  END IF;
END $$;
