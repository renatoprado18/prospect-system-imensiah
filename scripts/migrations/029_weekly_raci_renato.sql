-- 029_weekly_raci_renato.sql
-- RACI semanal pessoal do Renato — sec 4.6 do ARCHITECTURE_REBUILD.md.
-- Tonha gera segunda 7h BRT no grupo Governança APCE. Renato + Andressa
-- atualizam assincronamente ao longo da semana.

CREATE TABLE IF NOT EXISTS weekly_raci_renato (
    id BIGSERIAL PRIMARY KEY,
    semana_inicio DATE NOT NULL,                            -- segunda da semana
    item_tipo TEXT NOT NULL CHECK (item_tipo IN (
        'concluido', 'em_andamento', 'sem_movimento', 'novo'
    )),
    titulo TEXT NOT NULL,
    descricao TEXT,
    fonte_ref JSONB,                                        -- {project_id, task_id, conversation_id, raci_id, ...}
    frente_cos TEXT,                                        -- frente da CoS Config v5
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'updated', 'closed', 'dropped')),
    renato_response TEXT,                                   -- resposta assincrona dele
    response_at TIMESTAMP,
    andressa_update TEXT,                                   -- updates da Andressa
    andressa_update_at TIMESTAMP,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_raci_renato_semana ON weekly_raci_renato(semana_inicio DESC);
CREATE INDEX IF NOT EXISTS idx_raci_renato_open ON weekly_raci_renato(semana_inicio DESC, item_tipo) WHERE status='open';

CREATE OR REPLACE FUNCTION update_weekly_raci_atualizado_em()
RETURNS TRIGGER AS $$
BEGIN
    NEW.atualizado_em = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS weekly_raci_atualizado_em ON weekly_raci_renato;
CREATE TRIGGER weekly_raci_atualizado_em
    BEFORE UPDATE ON weekly_raci_renato
    FOR EACH ROW EXECUTE FUNCTION update_weekly_raci_atualizado_em();

COMMENT ON TABLE weekly_raci_renato IS 'RACI semanal pessoal do Renato (NAO confundir com RACI conselhos Vallen/Alba). Tonha posta seg 7h no grupo Governança APCE. Renato + Andressa atualizam assincronamente.';
