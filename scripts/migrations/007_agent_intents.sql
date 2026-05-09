-- Migration 007: agent_intents (P6 Diligente — Fase 1)
-- Date: 2026-05-08
--
-- Persiste intents abertos do INTEL bot pra que ele deixe de ser chat reativo
-- e vire agente que cumpre palavra. Cada mensagem nova checa intents abertos
-- ANTES de responder. Cron 30min (Fase 2) tenta progredir autonomamente.
--
-- Design fechado em 08/05/2026 com Renato (memoria: project_inteligencia_real.md
-- secao "P6 — Diligente"). 4 decisoes ancoradas:
--   1. Detector automatico (write_tool_called OR imperativo -> abre intent)
--   2. Estado: open|in_progress|blocked|completed|cancelled
--   3. Auto-pickup em toda msg (Fase 1) + cron 30min (Fase 2) + dashboard pill (Fase 2)
--   4. Proatividade opt-OUT (bot age, avisa no debriefing 19h)
--
-- Idempotente. Pode rodar varias vezes sem erro.

CREATE TABLE IF NOT EXISTS agent_intents (
    id SERIAL PRIMARY KEY,
    intent_text TEXT NOT NULL,
    intent_type TEXT,                    -- 'multi_step_action' | 'coach_followup' | 'investigation'
    status TEXT NOT NULL DEFAULT 'open', -- 'open' | 'in_progress' | 'blocked' | 'completed' | 'cancelled'
    steps_done JSONB DEFAULT '[]'::jsonb,
    next_step_hint TEXT,
    blocker TEXT,
    related_message_id INTEGER REFERENCES bot_conversations(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC'),
    updated_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC'),
    completed_at TIMESTAMP
);

-- Index parcial: 99% das queries sao "abertos pra auto-pickup".
-- Filtrar por status IN ('open', 'in_progress') na ingestion-side
-- evita scan completo quando agent_intents virar centenas de linhas.
CREATE INDEX IF NOT EXISTS idx_agent_intents_status_created
    ON agent_intents(status, created_at DESC)
    WHERE status IN ('open', 'in_progress');
