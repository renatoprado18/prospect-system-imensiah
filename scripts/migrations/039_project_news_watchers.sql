-- 039_project_news_watchers.sql
-- Project News Watchers (28/06/2026).
--
-- Permite vincular um projeto a uma query de noticias (default: Google News RSS).
-- Cron roda watchers ativos, captura hits novos (dedup por url_hash), e cria
-- action_proposals (action_type='news_alert', urgency='low') pra Renato revisar
-- no dashboard existente.
--
-- Modelo MVP — apenas Propor (nao Auto). Renato decide ativacao do cron.
--
-- Seed inicial (manual, NAO no codigo):
--   INSERT INTO project_news_watchers (project_id, query) VALUES (5, 'Fictor');
--
-- Como ativar o cron em prod:
--   1. Adicionar entrada em vercel.json apontando pra /api/cron/run-project-news-watchers
--      (sugestao: cada 6h ou diario as 7h UTC).
--   2. Confirmar que CRON_SECRET esta setado no Vercel.
--   3. Alternativa: trigger manual via UI (botao "Test now") ou curl c/ X-API-Key.

CREATE TABLE IF NOT EXISTS project_news_watchers (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    query           TEXT NOT NULL,
    feed_url        TEXT,                              -- gerado a partir do query se NULL
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    last_check      TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_project_news_watchers_active
    ON project_news_watchers (active, last_check NULLS FIRST)
    WHERE active = TRUE;

CREATE INDEX IF NOT EXISTS idx_project_news_watchers_project
    ON project_news_watchers (project_id);

CREATE TABLE IF NOT EXISTS project_news_hits (
    id              SERIAL PRIMARY KEY,
    watcher_id      INTEGER NOT NULL REFERENCES project_news_watchers(id) ON DELETE CASCADE,
    url_hash        TEXT NOT NULL UNIQUE,              -- sha256 da URL normalizada (lowercase, sem utm_*)
    title           TEXT,
    url             TEXT,
    published_at    TIMESTAMP,
    source          TEXT,
    hit_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    proposal_id     INTEGER REFERENCES action_proposals(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_project_news_hits_watcher_recent
    ON project_news_hits (watcher_id, hit_at DESC);

CREATE INDEX IF NOT EXISTS idx_project_news_hits_proposal
    ON project_news_hits (proposal_id)
    WHERE proposal_id IS NOT NULL;

COMMENT ON TABLE project_news_watchers IS
    'Watchers de noticias por projeto. RSS (default Google News) + cron + action_proposals.';
COMMENT ON COLUMN project_news_watchers.feed_url IS
    'URL RSS custom. Se NULL, service gera https://news.google.com/rss/search?q={query}&hl=pt-BR&gl=BR&ceid=BR:pt-419';
COMMENT ON TABLE project_news_hits IS
    'Items capturados por watchers. Dedup via url_hash (sha256 da URL normalizada).';
COMMENT ON COLUMN project_news_hits.url_hash IS
    'sha256 hex da URL lowercase com utm_* removidos. Garante idempotencia entre runs.';
