-- 016_scheduled_actions.sql
-- Primitivo de primeira classe pra agendar acoes futuras com confirmacao ativa.
-- Substitui o anti-pattern de GH Actions one-shot pra envios agendados (incidente
-- Marcos Tanaka 07/06/26 que escalou pra 09/06 ao inves de 08/06).
--
-- V0 suporta apenas action_type='wa_send'. Interface extensivel pra futuros
-- tipos (email, task creation, etc) via payload JSONB.
--
-- Renato confere o que esta agendado/falhando em /admin/scheduled-actions, e
-- recebe ack ativo via WA depois de cada envio (sucesso ou falha terminal).

CREATE TABLE IF NOT EXISTS scheduled_actions (
    id              SERIAL PRIMARY KEY,
    action_type     TEXT NOT NULL,                       -- V0: 'wa_send'. Futuro: 'email', 'create_task', etc.
    payload         JSONB NOT NULL,                      -- pra wa_send: {instance, number, text}
    scheduled_for   TIMESTAMP NOT NULL,                  -- UTC. Cron processa quando NOW() >= scheduled_for
    status          TEXT NOT NULL DEFAULT 'pending',     -- pending|sent|failed|cancelled
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 3,
    last_error      TEXT,
    created_by      TEXT,                                -- 'cos' | 'user_renato' | 'system' | nome do script
    source          TEXT,                                -- contexto livre ("session 08/06/26 retomada FUP Marcos")
    dedup_key       TEXT UNIQUE,                         -- idempotency: mesmo cron firing 2x nao duplica
    result_msg_id   TEXT,                                -- msg id retornado pelo Evolution apos envio
    result_status   TEXT,                                -- ultima atualizacao status WA (PENDING/DELIVERY_ACK/etc)
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    executed_at     TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scheduled_actions_due
    ON scheduled_actions (status, scheduled_for)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_scheduled_actions_dedup
    ON scheduled_actions (dedup_key)
    WHERE dedup_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_scheduled_actions_recent
    ON scheduled_actions (created_at DESC);

COMMENT ON TABLE scheduled_actions IS
    'Primitivo CoS pra agendar acoes futuras (WA, email, etc) com confirmacao ativa pos-execucao. V0=wa_send.';
COMMENT ON COLUMN scheduled_actions.action_type IS
    'V0 aceita apenas wa_send. Interface extensivel pra futuros tipos via payload JSONB.';
COMMENT ON COLUMN scheduled_actions.payload IS
    'Para wa_send: {"instance": "rap-whatsapp"|"intel-bot", "number": "5511...", "text": "..."}';
COMMENT ON COLUMN scheduled_actions.dedup_key IS
    'Pra idempotency: cron pode rodar 2x e schedule_wa retorna id existente em vez de duplicar.';
COMMENT ON COLUMN scheduled_actions.scheduled_for IS
    'Timestamp UTC. Cron processa rows com status=pending AND scheduled_for <= NOW().';
