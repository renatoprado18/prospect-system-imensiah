-- 026_signals.sql
-- Camada 1 da rebuild Tonha (15/06/26) — tabela de signals estruturados.
--
-- Detectores deterministas (Python puro, zero LLM) inserem signals aqui via
-- INSERT ... ON CONFLICT (signal_hash) DO UPDATE. A Tonha (Sonnet) le e decide.
--
-- Ver docs/ARCHITECTURE_REBUILD.md secao 4.

CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    signal_hash TEXT UNIQUE NOT NULL,                       -- hash(tipo + entidade + key_data)
    tipo TEXT NOT NULL,                                     -- 'raci_vencido', 'post_sem_imagem', etc.
    urgencia INT NOT NULL CHECK (urgencia BETWEEN 1 AND 10),
    contexto JSONB NOT NULL,                                -- payload pra Tonha decidir
    detector TEXT NOT NULL,                                 -- 'detector_conselhos', etc.
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','expired','dismissed')),
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMP,
    resolved_by TEXT,                                       -- 'tonha_auto' | 'tonha_escalated' | 'renato_direct' | 'detector_expired'
    decision_id BIGINT                                      -- FK pra tonha_decisions(id) - sera adicionada apos 027
);

CREATE INDEX IF NOT EXISTS idx_signals_open ON signals(status, urgencia DESC) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_signals_tipo ON signals(tipo, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_signals_detector ON signals(detector, criado_em DESC);

-- Trigger atualizado_em
CREATE OR REPLACE FUNCTION update_signals_atualizado_em()
RETURNS TRIGGER AS $$
BEGIN
    NEW.atualizado_em = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS signals_atualizado_em ON signals;
CREATE TRIGGER signals_atualizado_em
    BEFORE UPDATE ON signals
    FOR EACH ROW EXECUTE FUNCTION update_signals_atualizado_em();

COMMENT ON TABLE signals IS 'Sinais estruturados emitidos por detectores deterministas. Tonha le e decide.';
COMMENT ON COLUMN signals.signal_hash IS 'Hash de dedup. Mesmo signal_hash = mesmo sinal logico (ON CONFLICT DO UPDATE).';
COMMENT ON COLUMN signals.urgencia IS '1-10. 10=ação imediata, 1=podia ter esperado mais.';
COMMENT ON COLUMN signals.contexto IS 'Payload JSON com tudo que Tonha precisa pra decidir sem precisar buscar mais.';
