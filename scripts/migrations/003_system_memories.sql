-- Migration 003: system_memories table
-- Date: 2026-05-02
--
-- Memórias persistentes do INTEL coach que NÃO estão atreladas a um contato.
-- Cobre: decisões de vida, compromissos consigo, padrões observados, estados
-- emocionais notados, e o digest diário gerado pela síntese.
--
-- Distinção vs contact_memories: contact_memories.contact_id é obrigatório.
-- system_memories é livre — pra coisas que são sobre o Renato como um todo.

CREATE TABLE IF NOT EXISTS system_memories (
    id SERIAL PRIMARY KEY,
    titulo TEXT NOT NULL,
    conteudo TEXT NOT NULL,
    tipo TEXT,                          -- 'decisao' | 'compromisso' | 'padrao' | 'reflexao' | 'sintese_diaria' | etc
    tags JSONB DEFAULT '[]'::jsonb,
    fonte TEXT,                         -- 'chat' | 'whatsapp' | 'sintese' | 'manual'
    referencia_inicio DATE,             -- pra sínteses: período coberto (início)
    referencia_fim DATE,                -- pra sínteses: período coberto (fim)
    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_system_memories_tipo ON system_memories(tipo);
CREATE INDEX IF NOT EXISTS idx_system_memories_criado_em ON system_memories(criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_system_memories_referencia ON system_memories(referencia_inicio DESC, referencia_fim DESC);

-- Trigger pra auto-atualizar atualizado_em (função já existe da migration 002)
DROP TRIGGER IF EXISTS trg_system_memories_atualizado_em ON system_memories;
CREATE TRIGGER trg_system_memories_atualizado_em
    BEFORE UPDATE ON system_memories
    FOR EACH ROW
    EXECUTE FUNCTION set_atualizado_em();
