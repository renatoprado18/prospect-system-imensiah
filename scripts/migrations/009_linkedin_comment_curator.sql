-- Migration: LinkedIn Comment Curator (P1 MVP)
-- Date: 2026-05-12
-- Description: Estende `linkedin_task_data` com colunas pra scoring numerico,
-- drafts A/B/DM, e rastreio de publicacao + DM follow-up. Cria tabela
-- `linkedin_adhoc_drafts` espelhada pra rota /linkedin/comentar (posts sem
-- task associada).

-- 1) Estender linkedin_task_data
ALTER TABLE linkedin_task_data
    ADD COLUMN IF NOT EXISTS score_numeric INT,
    ADD COLUMN IF NOT EXISTS draft_a TEXT,
    ADD COLUMN IF NOT EXISTS draft_b TEXT,
    ADD COLUMN IF NOT EXISTS draft_dm TEXT,
    ADD COLUMN IF NOT EXISTS draft_recommended TEXT,
    ADD COLUMN IF NOT EXISTS post_author_name TEXT,
    ADD COLUMN IF NOT EXISTS post_author_headline TEXT,
    ADD COLUMN IF NOT EXISTS post_author_urn TEXT,
    ADD COLUMN IF NOT EXISTS published BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS published_version TEXT,
    ADD COLUMN IF NOT EXISTS published_text TEXT,
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS outbound_engagement_id INT REFERENCES linkedin_outbound_engagements(id),
    ADD COLUMN IF NOT EXISTS dm_followup_task_id INT REFERENCES tasks(id);

CREATE INDEX IF NOT EXISTS idx_linkedin_task_data_score
    ON linkedin_task_data(score_numeric DESC) WHERE score_numeric IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_linkedin_task_data_published
    ON linkedin_task_data(published) WHERE published = FALSE;

-- 2) Tabela espelho pra posts ad-hoc (sem task)
CREATE TABLE IF NOT EXISTS linkedin_adhoc_drafts (
    id SERIAL PRIMARY KEY,
    post_url TEXT,
    post_text TEXT NOT NULL,
    post_author_name TEXT,
    post_author_headline TEXT,
    post_author_urn TEXT,
    post_posted_at TEXT,
    post_engagements JSONB,

    score_numeric INT,
    ai_verdict TEXT,
    ai_rationale TEXT,
    ai_angle TEXT,
    ai_ran_at TIMESTAMP,

    draft_a TEXT,
    draft_b TEXT,
    draft_dm TEXT,
    draft_recommended TEXT,

    published BOOLEAN DEFAULT FALSE,
    published_version TEXT,
    published_text TEXT,
    published_at TIMESTAMP,
    outbound_engagement_id INT REFERENCES linkedin_outbound_engagements(id),
    dm_followup_task_id INT REFERENCES tasks(id),

    fetched_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_by_user_id INT
);

CREATE INDEX IF NOT EXISTS idx_linkedin_adhoc_score
    ON linkedin_adhoc_drafts(score_numeric DESC) WHERE score_numeric IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_linkedin_adhoc_post_url
    ON linkedin_adhoc_drafts(post_url);
