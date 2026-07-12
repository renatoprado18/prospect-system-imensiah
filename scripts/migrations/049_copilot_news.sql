-- 049_copilot_news.sql
-- Contrato de leitura de NEWS pra Tonia/CoPiloto (F-A / A2, porta-voz único, 12/07/26).
--
-- Ate agora o pipeline de news (project_news_watcher.py, cron worker Railway) era
-- 100% paralelo a Tonia: hits gravados em project_news_hits, digests em news_digests,
-- entregues como self-chat WA (rap-whatsapp). Tonia era CEGA a news (zero refs, sem
-- view copilot). Este schema expoe as duas superficies pra Tonia poder:
--   - ler os hits recentes com o CONTEXTO do watcher (project_id + query/tema) e
--     cruzar noticia->projeto/objetivo no briefing (A5);
--   - ler os digests ja enviados (o que, quando, quantos hits, status de ack).
--
-- news_hits = project_news_hits LEFT JOIN project_news_watchers (o watcher_id sozinho
-- nao diz nada; o valor esta em project_id + query, o "porque essa noticia importa").
-- Watcher sempre existe (FK ON DELETE CASCADE apaga os hits junto), mas LEFT JOIN por
-- robustez, espelhando copilot.emails.
--
-- Contrato: docs/COPILOT_CONTRACT.md. Regras: colunas so sao ADICIONADAS, nunca
-- removidas/renomeadas. Idempotente (CREATE OR REPLACE VIEW). Zero mudanca de
-- comportamento — so leitura sobre tabelas existentes.

CREATE SCHEMA IF NOT EXISTS copilot;

-- =============================================================================
-- copilot.news_hits — noticias captadas pelos watchers, com contexto do watcher.
-- Base: public.project_news_hits (039/040). JOIN watcher pra project_id + query.
-- Nao expostas: url_hash (dedup interno), pushed_at (mecanica de entrega interna).
-- Aliases: relevance_score <- ai_relevance_score, watcher_query <- watchers.query,
--          project_name <- projects.nome (LEFT JOIN public.projects pra o briefing
--          nomear o projeto sem 2a query; ultima coluna = regra do CREATE OR REPLACE).
-- =============================================================================
CREATE OR REPLACE VIEW copilot.news_hits AS
SELECT
    h.id,
    h.watcher_id,
    w.project_id,
    w.query                 AS watcher_query,
    w.delivery_mode,
    h.title,
    h.url,
    h.source,
    h.published_at,
    h.hit_at,
    h.ai_relevance_score    AS relevance_score,
    h.digest_id,
    h.proposal_id,
    h.archived_at,
    p.nome                  AS project_name
FROM public.project_news_hits h
LEFT JOIN public.project_news_watchers w ON w.id = h.watcher_id
LEFT JOIN public.projects p ON p.id = w.project_id;

COMMENT ON VIEW copilot.news_hits IS 'Noticias captadas pelos watchers de projeto, com contexto (project_id + project_name + watcher_query pra cruzar noticia->projeto). relevance_score 0-1 (AI), digest_id preenchido = ja foi pra digest, proposal_id = virou proposta, archived_at = arquivada. Timestamps UTC naive.';

-- =============================================================================
-- copilot.news_digests — digests de news ja enviados (self-chat WA hoje; migra
-- pro briefing na A3). Base: public.news_digests (042).
-- Nao exposta: message_id_evolution (id interno da Evolution).
-- Aliases: target <- wa_target, content <- message_text.
-- =============================================================================
CREATE OR REPLACE VIEW copilot.news_digests AS
SELECT
    id,
    sent_at,
    wa_target        AS target,
    watchers_count,
    hits_count,
    message_text     AS content,
    ack_status,
    acked_at,
    expires_at
FROM public.news_digests;

COMMENT ON VIEW copilot.news_digests IS 'Digests de news enviados (self-chat WA hoje, migra pro briefing na A3). content = texto do digest, hits_count noticias, ack_status pending/acked_ok/drilled/expired. Timestamps UTC naive.';
