-- 012_pending_notifications.sql
-- M2 do plano "reduzir poluicao WA":
-- Notificacoes nao-urgentes acumulam aqui ate proximo briefing (08h BRT) ou
-- debriefing (19h BRT). Urgentes vao direto via send_whatsapp (sem passar aqui).
--
-- Pending >24h vira item no morning briefing seguinte (decisao Renato 18/05).

CREATE TABLE IF NOT EXISTS pending_notifications (
    id              SERIAL PRIMARY KEY,
    source          TEXT NOT NULL,          -- ex: 'agent_intent', 'editorial_alert', 'linkedin_outbound', 'campaign_step', 'manual'
    msg_type        TEXT,                   -- ex: 'task_created', 'reuniao_proxima', 'reply_received', 'metric_alert'
    payload         JSONB NOT NULL,         -- {title, body, links, contact_id?, project_id?, urls?}
    urgency_score   INT,                    -- 0-10 (referencia, nao bloqueia roteamento)
    digest_target   TEXT NOT NULL,          -- 'morning' | 'evening' | 'either'
    queued_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMP,
    sent_in_digest  TEXT,                   -- 'morning_2026_05_19', 'evening_2026_05_18'
    expired_at      TIMESTAMP,              -- pending >24h sem ser enviado vira item do morning seguinte E ganha expired_at; nunca apaga
    dedup_key       TEXT                    -- opcional: evita duplicar mesmo evento (source+key UNIQUE)
);

CREATE INDEX IF NOT EXISTS idx_pending_notif_unsent
    ON pending_notifications (queued_at)
    WHERE sent_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_pending_notif_digest
    ON pending_notifications (sent_in_digest);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_notif_dedup
    ON pending_notifications (source, dedup_key)
    WHERE dedup_key IS NOT NULL AND sent_at IS NULL;

COMMENT ON TABLE pending_notifications IS
    'M2: fila de notificacoes nao-urgentes esperando proximo briefing/debriefing. Urgentes vao direto.';
COMMENT ON COLUMN pending_notifications.digest_target IS
    '"morning"=so manha (08h), "evening"=so noite (19h), "either"=primeiro que rodar';
COMMENT ON COLUMN pending_notifications.expired_at IS
    'Marcado quando pending fica >24h sem digest. Item ainda sai no proximo morning, mas vem com flag de atraso.';
