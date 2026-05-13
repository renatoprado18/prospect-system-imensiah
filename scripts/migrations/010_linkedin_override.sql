-- 010_linkedin_override.sql
-- Adiciona suporte a override manual de drafts pra posts com score < threshold.
-- User informa motivo (pra fine-tuning futuro do scorer) e dispara geracao.

ALTER TABLE linkedin_task_data
    ADD COLUMN IF NOT EXISTS user_override_reason TEXT,
    ADD COLUMN IF NOT EXISTS user_override_at TIMESTAMP;

COMMENT ON COLUMN linkedin_task_data.user_override_reason IS
    'Motivo do usuario pra forcar geracao de drafts mesmo com score < threshold. Input pra fine-tuning do _SCORING_SYSTEM_PROMPT.';
COMMENT ON COLUMN linkedin_task_data.user_override_at IS
    'Quando o override foi disparado. Permite ranking temporal e auditoria.';
