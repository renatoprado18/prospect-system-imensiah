-- Migration: LinkedIn Task Data (sidecar)
-- Date: 2026-05-02
-- Description: Cache de dados de post LinkedIn associados a tasks de
-- "Curtir post" / "Comentar post" geradas pelo campaign_executor. Permite
-- mostrar o texto completo do post no expand da task (em vez de só preview
-- de 100 chars no descricao) e cachear assessment AI ("vale comentar?").

CREATE TABLE IF NOT EXISTS linkedin_task_data (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    post_url TEXT NOT NULL,
    post_text TEXT NOT NULL,
    post_posted_at TEXT,
    post_engagements JSONB,

    -- Fase 2 (AI assess) — preenchido sob demanda
    ai_verdict TEXT,           -- 'skip' | 'like_only' | 'comment'
    ai_rationale TEXT,
    ai_angle TEXT,             -- só preenche se verdict='comment'
    ai_ran_at TIMESTAMP,

    fetched_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_linkedin_task_data_post_url
    ON linkedin_task_data(post_url);
