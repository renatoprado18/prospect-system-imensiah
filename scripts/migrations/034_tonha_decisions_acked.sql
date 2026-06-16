-- 034 — acked_at em tonha_decisions
--
-- Renato precisa do botao "Ciente" pra marcar decisao revisada sem reverter.
-- Hoje so revert -> some da lista mas vira REVERTED audit. Falta um caminho
-- neutro "OK, vi, segue como esta". Default UI esconde acked + reverted.
ALTER TABLE tonha_decisions
    ADD COLUMN IF NOT EXISTS acked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS acked_by TEXT;

CREATE INDEX IF NOT EXISTS idx_tonha_decisions_unacked
    ON tonha_decisions (criado_em DESC)
    WHERE acked_at IS NULL AND reverted_at IS NULL;
