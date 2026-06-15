-- 028_delegations.sql
-- Capacidade de delegacao da Tonha — sec 4.5 do ARCHITECTURE_REBUILD.md.
-- Tonha cria delegations pra time humano (Andressa, João Piccino, Priscila),
-- Dev (Claude Code), evaluator/collector (modos internos). Cobranca automatica
-- por cron + detector_delegacoes.

CREATE TABLE IF NOT EXISTS delegations (
    id BIGSERIAL PRIMARY KEY,
    delegated_to TEXT NOT NULL CHECK (delegated_to IN (
        'andressa', 'joao_piccino', 'priscila_contadora',
        'dev', 'evaluator', 'collector'
    )),
    contact_id INT REFERENCES contacts(id) ON DELETE SET NULL,
    task_summary TEXT NOT NULL,
    task_full TEXT NOT NULL,
    deadline DATE,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
        'open', 'in_progress', 'completed', 'overdue', 'escalated', 'cancelled'
    )),
    response TEXT,
    response_at TIMESTAMP,
    last_followup_at TIMESTAMP,
    followup_count INT NOT NULL DEFAULT 0,
    decision_id BIGINT REFERENCES tonha_decisions(id) ON DELETE SET NULL,
    signal_id BIGINT REFERENCES signals(id) ON DELETE SET NULL,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_delegations_open ON delegations(deadline ASC) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_delegations_to ON delegations(delegated_to, status);
CREATE INDEX IF NOT EXISTS idx_delegations_recent ON delegations(criado_em DESC);

CREATE OR REPLACE FUNCTION update_delegations_atualizado_em()
RETURNS TRIGGER AS $$
BEGIN
    NEW.atualizado_em = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS delegations_atualizado_em ON delegations;
CREATE TRIGGER delegations_atualizado_em
    BEFORE UPDATE ON delegations
    FOR EACH ROW EXECUTE FUNCTION update_delegations_atualizado_em();

COMMENT ON TABLE delegations IS 'Delegacoes da Tonha pra time humano (Andressa, João Piccino, Priscila) ou interno (Dev/Evaluator/Collector). Tonha cria + faz cobranca automatica.';
