-- 050_channel_log.sql
-- F-B Frente 2: log REAL (nao shadow) das decisoes de canal do notification_router.
-- Registra o que foi decidido e enviado por canal, pra debug/telemetria quando
-- algo "nao chegar" ao Renato. Gravado sempre que NOTIFICATION_MULTICHANNEL='on'.
-- Idempotente — seguro reaplicar.

CREATE TABLE IF NOT EXISTS channel_decisions (
    id                SERIAL PRIMARY KEY,
    created_at        TIMESTAMP DEFAULT NOW(),
    source            TEXT,
    msg_type          TEXT,
    urgency_score     INT,
    decided_channel   TEXT,   -- 'whatsapp' | 'push' | 'pill'
    decision_rule     TEXT,
    sent_ok           BOOLEAN,
    multichannel_mode TEXT,   -- 'on' | 'off'
    dedup_key         TEXT,
    payload_title     TEXT
);

CREATE INDEX IF NOT EXISTS idx_channel_decisions_created_at
    ON channel_decisions (created_at);

CREATE INDEX IF NOT EXISTS idx_channel_decisions_decided_channel
    ON channel_decisions (decided_channel);
