-- 011_linkedin_engagement_signals.sql
-- F2 da estrategia LinkedIn: engagement-driven prospecting.
-- Quem comentou nos posts proprios → cruza com contacts → leads warm/cold.
--
-- LinkdAPI /posts/likes nao retorna dados no Hobby — F2 v1 cobre so commenters
-- (sinal mais forte de engagement de qualquer forma).

CREATE TABLE IF NOT EXISTS linkedin_engagement_signals (
    id                  SERIAL PRIMARY KEY,
    post_id             INT NOT NULL REFERENCES editorial_posts(id) ON DELETE CASCADE,
    action              TEXT NOT NULL DEFAULT 'comment',  -- 'comment' | 'like' (futuro)
    comment_urn         TEXT,                              -- urn:li:comment:... unique p/ idempotencia
    profile_urn         TEXT,                              -- author.urn do LinkdAPI
    profile_url         TEXT,                              -- author.url (linkedin.com/in/...)
    profile_name        TEXT,
    profile_headline    TEXT,
    comment_text        TEXT,                              -- snippet do comment (200 chars)
    comment_at          TIMESTAMP,                         -- author.createdAt do comment
    detected_at         TIMESTAMP DEFAULT NOW(),

    -- Cross com contacts
    contact_id          INT REFERENCES contacts(id) ON DELETE SET NULL,
    contact_match_type  TEXT,                              -- 'url_exact' | 'urn_exact' | 'name_fuzzy' | null
    task_id             INT REFERENCES tasks(id) ON DELETE SET NULL,

    -- Workflow status
    status              TEXT NOT NULL DEFAULT 'pending',
    -- pending: aguardando processamento
    -- warm_task_created: contato ja existia, task de follow-up criada
    -- cold_lead_created: contato criado novo + dossier enqueued
    -- self: era o proprio Renato comentando, ignorado
    -- dismissed: usuario marcou como nao-relevante
    processed_at        TIMESTAMP,
    notes               TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_les_unique_comment
    ON linkedin_engagement_signals (post_id, comment_urn)
    WHERE comment_urn IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_les_post ON linkedin_engagement_signals (post_id);
CREATE INDEX IF NOT EXISTS idx_les_contact ON linkedin_engagement_signals (contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_les_status ON linkedin_engagement_signals (status, detected_at DESC);

COMMENT ON TABLE linkedin_engagement_signals IS
    'F2 engagement-driven prospecting: commenters dos posts proprios cruzados com contacts.';
