-- 040_news_watchers_mode_b.sql
-- News Watchers Modo B: push critico com filtro IA (28/06/2026).
--
-- Modo A (default existente): hit RSS -> action_proposal silenciosa, sem push.
-- Modo B (critical_push): hit RSS -> filtro Claude Haiku -> se score >=
--   criticality_threshold, manda WA pro Renato. Caso contrario, vira proposal
--   silenciosa igual modo A.
-- Modo C (digest_weekly): hits acumulam, cron semanal manda 1 WA agregado.
--   (Implementacao do digest cron e backlog; coluna ja preparada.)
--
-- Politica de autonomia (cf feedback_cos_autonomy_policy.md):
--   send_wa_message DM = "Propor SEMPRE", MAS push de news_alert pra o proprio
--   Renato afeta SO ele (blast radius = self). Por isso classificado como
--   "Auto-com-condicao": condicao = score IA >= threshold do watcher + watcher
--   esta em delivery_mode='critical_push'. Renato controla o threshold por
--   watcher via UI.

-- Modo de entrega: como o hit notifica Renato.
ALTER TABLE project_news_watchers
    ADD COLUMN IF NOT EXISTS delivery_mode TEXT NOT NULL DEFAULT 'silent';

-- Score minimo IA pra disparar push (so relevante se delivery_mode='critical_push').
-- 0.0 = manda tudo, 1.0 = so a noticia mais critica imaginavel.
ALTER TABLE project_news_watchers
    ADD COLUMN IF NOT EXISTS criticality_threshold FLOAT DEFAULT 0.7;

-- Ultimo push enviado (qualquer modo). Permite anti-loop e cooldown manual.
ALTER TABLE project_news_watchers
    ADD COLUMN IF NOT EXISTS last_push_at TIMESTAMP;

-- Numero WA destino. NULL = default Renato (env var ou hardcoded).
ALTER TABLE project_news_watchers
    ADD COLUMN IF NOT EXISTS wa_target TEXT;

-- Constraint: delivery_mode so aceita os 3 valores conhecidos.
-- Usa DO block pra ser idempotente (CONSTRAINT IF NOT EXISTS so existe em PG 14+).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'project_news_watchers_delivery_mode_check'
    ) THEN
        ALTER TABLE project_news_watchers
            ADD CONSTRAINT project_news_watchers_delivery_mode_check
            CHECK (delivery_mode IN ('silent', 'critical_push', 'digest_weekly'));
    END IF;
END $$;


-- Score IA do hit (cache: nao reprocessa o mesmo url se ja pontuou).
-- NULL = ainda nao pontuado (modo silent nem chama IA).
ALTER TABLE project_news_hits
    ADD COLUMN IF NOT EXISTS ai_relevance_score FLOAT;

-- Quando o hit foi pushed via WA. NULL = nao foi pushed.
-- Permite dedup defensivo (nao pushar 2x o mesmo hit) e telemetria.
ALTER TABLE project_news_hits
    ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_project_news_hits_pushed
    ON project_news_hits (pushed_at DESC)
    WHERE pushed_at IS NOT NULL;


COMMENT ON COLUMN project_news_watchers.delivery_mode IS
    'silent (default, so proposal) | critical_push (filtro IA + WA push) | digest_weekly (agregado semanal).';
COMMENT ON COLUMN project_news_watchers.criticality_threshold IS
    'Score minimo IA (0.0-1.0) pra disparar push. So relevante se delivery_mode=critical_push.';
COMMENT ON COLUMN project_news_watchers.last_push_at IS
    'Timestamp do ultimo push WA disparado por esse watcher. Usado pra cooldown opcional.';
COMMENT ON COLUMN project_news_watchers.wa_target IS
    'Numero WA destino. NULL = default Renato +5511984153337.';
COMMENT ON COLUMN project_news_hits.ai_relevance_score IS
    'Score Claude Haiku 0.0-1.0. NULL = nao pontuado (watcher em modo silent).';
COMMENT ON COLUMN project_news_hits.pushed_at IS
    'Quando foi enviado WA push (modo critical). NULL = nao pushed.';
