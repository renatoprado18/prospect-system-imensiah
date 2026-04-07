-- Migration: Add Campaigns System
-- Date: 2026-04-07
-- Description: Creates tables for relationship campaigns by business line

-- ============================================================================
-- 1. LINHAS DE NEGÓCIO
-- ============================================================================
CREATE TABLE IF NOT EXISTS business_lines (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(50) UNIQUE NOT NULL,
    nome VARCHAR(100) NOT NULL,
    descricao TEXT,
    cor VARCHAR(7),
    icone VARCHAR(50),
    conversion_metric VARCHAR(100),
    ativo BOOLEAN DEFAULT TRUE,
    criado_em TIMESTAMP DEFAULT NOW()
);

-- Seed das 4 linhas de negócio
INSERT INTO business_lines (slug, nome, descricao, cor, icone, conversion_metric) VALUES
('conselhos', 'Conselhos', 'Posições em boards, mentoria, governança', '#1E40AF', 'briefcase', 'reuniao'),
('imensiah', 'ImensIAH', 'Diagnósticos estratégicos para PMEs', '#7C3AED', 'chart-bar', 'demo'),
('fusiah', 'FusIAH', 'Teses de M&A, investimentos', '#059669', 'trending-up', 'tese'),
('jabo', 'Jabô Café', 'Café especial B2B, parcerias', '#92400E', 'coffee', 'pedido')
ON CONFLICT (slug) DO NOTHING;

-- ============================================================================
-- 2. CAMPANHAS
-- ============================================================================
CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    business_line_id INTEGER REFERENCES business_lines(id),
    nome VARCHAR(200) NOT NULL,
    descricao TEXT,
    objetivo TEXT,
    motivo_contato TEXT,

    -- Filtros de segmentação
    filtros JSONB NOT NULL DEFAULT '{}',

    status VARCHAR(20) DEFAULT 'draft',

    -- Métricas agregadas (cache)
    total_enrolled INTEGER DEFAULT 0,
    total_completed INTEGER DEFAULT 0,
    total_converted INTEGER DEFAULT 0,

    criado_em TIMESTAMP DEFAULT NOW(),
    iniciado_em TIMESTAMP,
    finalizado_em TIMESTAMP,
    atualizado_em TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_campaigns_business_line ON campaigns(business_line_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

-- ============================================================================
-- 3. PASSOS DA CAMPANHA
-- ============================================================================
CREATE TABLE IF NOT EXISTS campaign_steps (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    ordem INTEGER NOT NULL,

    tipo VARCHAR(50) NOT NULL,
    titulo VARCHAR(200),
    descricao TEXT,

    delay_dias INTEGER DEFAULT 0,

    config JSONB DEFAULT '{}',
    condicao JSONB,

    ativo BOOLEAN DEFAULT TRUE,
    criado_em TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_campaign_steps_campaign ON campaign_steps(campaign_id);

-- ============================================================================
-- 4. ENROLLMENT DE CONTATOS
-- ============================================================================
CREATE TABLE IF NOT EXISTS campaign_enrollments (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,

    status VARCHAR(20) DEFAULT 'active',

    current_step INTEGER DEFAULT 1,
    next_action_at TIMESTAMP,

    enrolled_at TIMESTAMP DEFAULT NOW(),
    paused_at TIMESTAMP,
    completed_at TIMESTAMP,
    converted_at TIMESTAMP,
    conversion_notes TEXT,

    UNIQUE(campaign_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_enrollments_campaign ON campaign_enrollments(campaign_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_contact ON campaign_enrollments(contact_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_next_action ON campaign_enrollments(next_action_at)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_enrollments_status ON campaign_enrollments(status);

-- ============================================================================
-- 5. EXECUÇÃO DE PASSOS
-- ============================================================================
CREATE TABLE IF NOT EXISTS campaign_step_executions (
    id SERIAL PRIMARY KEY,
    enrollment_id INTEGER REFERENCES campaign_enrollments(id) ON DELETE CASCADE,
    step_id INTEGER REFERENCES campaign_steps(id),

    executed_at TIMESTAMP DEFAULT NOW(),
    resultado VARCHAR(20),
    notas TEXT,

    suggestion_id INTEGER,
    task_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_step_executions_enrollment ON campaign_step_executions(enrollment_id);

-- ============================================================================
-- 6. TEMPLATES DE CAMPANHA (opcional, para reutilização)
-- ============================================================================
CREATE TABLE IF NOT EXISTS campaign_templates (
    id SERIAL PRIMARY KEY,
    business_line_id INTEGER REFERENCES business_lines(id),
    nome VARCHAR(200) NOT NULL,
    descricao TEXT,
    objetivo_template TEXT,
    motivo_template TEXT,
    filtros_template JSONB DEFAULT '{}',
    steps_template JSONB DEFAULT '[]',
    ativo BOOLEAN DEFAULT TRUE,
    criado_em TIMESTAMP DEFAULT NOW()
);

-- Seed de templates exemplo
INSERT INTO campaign_templates (business_line_id, nome, descricao, objetivo_template, motivo_template, filtros_template, steps_template)
SELECT
    bl.id,
    'Reativar Círculo 2 - ' || bl.nome,
    'Campanha para reativar contatos do círculo 2 que estão esfriando',
    'Agendar café/call para retomar relacionamento',
    'Compartilhar insights relevantes do setor',
    '{"circulo": [2], "health_max": 50, "ultimo_contato_dias_min": 60}'::jsonb,
    '[
        {"ordem": 1, "tipo": "linkedin_like", "titulo": "Engajar com post recente", "delay_dias": 0},
        {"ordem": 2, "tipo": "linkedin_comment", "titulo": "Comentário relevante", "delay_dias": 3},
        {"ordem": 3, "tipo": "linkedin_message", "titulo": "DM com valor", "delay_dias": 4, "config": {"template": "Oi {primeiro_nome}, vi seu post e lembrei de você. Tenho acompanhado algumas novidades interessantes - você teria 15min para um café virtual?"}},
        {"ordem": 4, "tipo": "check_response", "titulo": "Verificar resposta", "delay_dias": 7},
        {"ordem": 5, "tipo": "meeting_invite", "titulo": "Convite para café", "delay_dias": 7, "condicao": {"sem_resposta": true}}
    ]'::jsonb
FROM business_lines bl
ON CONFLICT DO NOTHING;

-- ============================================================================
-- VERIFICAÇÃO
-- ============================================================================
DO $$
BEGIN
    RAISE NOTICE 'Migration completed successfully!';
    RAISE NOTICE 'Tables created: business_lines, campaigns, campaign_steps, campaign_enrollments, campaign_step_executions, campaign_templates';
END $$;
