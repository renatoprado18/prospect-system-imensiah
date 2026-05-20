-- 014_pending_notifications_ack.sql
-- Adiciona acked_at em pending_notifications.
-- "Digest OK" no modal de auditoria — usuario confirma que viu e ok, some do modal.
-- Diferente de sent_at (foi enviado em digest) e expired_at (ficou >24h).

ALTER TABLE pending_notifications
    ADD COLUMN IF NOT EXISTS acked_at TIMESTAMP;

COMMENT ON COLUMN pending_notifications.acked_at IS
    'Marcado quando Renato clica "Digest OK" no modal de auditoria. Item some do modal mas fica no banco pra metricas.';

CREATE INDEX IF NOT EXISTS idx_pending_notif_acked
    ON pending_notifications (acked_at)
    WHERE acked_at IS NULL;
