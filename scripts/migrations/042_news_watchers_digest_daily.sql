-- 042_news_watchers_digest_daily.sql
-- News Watchers Modo D: digest diario interativo (28/06/2026).
--
-- Pedido Renato 28/06: IA le 1x/dia, manda resumo agrupado por watcher via WA,
-- Renato responde:
--   "ok" -> hits do digest sao archived (sem virar action_proposal)
--   "fictor" ou "1" -> manda WA com titulos+URLs completos do watcher escolhido
--
-- IMPORTANTE: hits de watcher em digest_daily NAO viram action_proposal.
-- So sao salvos em project_news_hits (pra dedup). Action proposal = ruido no
-- dashboard quando o canal de entrega e digest. Mudanca no check_watcher.
--
-- Anti-spam: cron 8h BRT (11h UTC). Se 0 hits novos, NAO manda WA.
-- Anti-loop: hits so entram em UM digest (digest_id setado on send).
--   Rodar cron 2x seguido = 2a chamada nao acha hits novos, sai sem WA.

-- ===== ALTER project_news_watchers =====

-- Override WA destino do digest. NULL = default Renato +5511984153337.
ALTER TABLE project_news_watchers
    ADD COLUMN IF NOT EXISTS digest_target TEXT;

-- Atualiza CHECK constraint pra incluir digest_daily.
-- Drop+recreate pra idempotencia (sem usar IF NOT EXISTS que so bate nome).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'project_news_watchers_delivery_mode_check'
    ) THEN
        ALTER TABLE project_news_watchers
            DROP CONSTRAINT project_news_watchers_delivery_mode_check;
    END IF;

    ALTER TABLE project_news_watchers
        ADD CONSTRAINT project_news_watchers_delivery_mode_check
        CHECK (delivery_mode IN ('silent', 'critical_push', 'digest_weekly', 'digest_daily'));
END $$;


-- ===== Tabela news_digests =====
-- 1 row por envio de digest. Guarda texto e estado do ack.

CREATE TABLE IF NOT EXISTS news_digests (
    id                      BIGSERIAL PRIMARY KEY,
    sent_at                 TIMESTAMP NOT NULL DEFAULT NOW(),
    wa_target               TEXT NOT NULL,
    watchers_count          INT NOT NULL DEFAULT 0,
    hits_count              INT NOT NULL DEFAULT 0,
    message_text            TEXT NOT NULL,
    message_id_evolution    TEXT,
    ack_status              TEXT NOT NULL DEFAULT 'pending',  -- pending | acked_ok | drilled | expired
    acked_at                TIMESTAMP,
    expires_at              TIMESTAMP                          -- 48h apos sent
);

CREATE INDEX IF NOT EXISTS idx_news_digests_pending
    ON news_digests (ack_status, sent_at DESC)
    WHERE ack_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_news_digests_target_recent
    ON news_digests (wa_target, sent_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'news_digests_ack_status_check'
    ) THEN
        ALTER TABLE news_digests
            ADD CONSTRAINT news_digests_ack_status_check
            CHECK (ack_status IN ('pending', 'acked_ok', 'drilled', 'expired'));
    END IF;
END $$;


-- ===== ALTER project_news_hits =====
-- Cada hit pode pertencer a 1 digest. Renato OK marca archived_at.

ALTER TABLE project_news_hits
    ADD COLUMN IF NOT EXISTS digest_id BIGINT REFERENCES news_digests(id) ON DELETE SET NULL;

ALTER TABLE project_news_hits
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_project_news_hits_digest
    ON project_news_hits (digest_id)
    WHERE digest_id IS NOT NULL;

-- Pra build_daily_digest pegar hits ainda nao digested rapidamente.
CREATE INDEX IF NOT EXISTS idx_project_news_hits_pending_digest
    ON project_news_hits (watcher_id, hit_at DESC)
    WHERE digest_id IS NULL AND archived_at IS NULL;


-- ===== Comments =====

COMMENT ON COLUMN project_news_watchers.digest_target IS
    'Override do WA destino do digest_daily. NULL = default Renato +5511984153337.';

COMMENT ON TABLE news_digests IS
    'Cada envio de digest diario interativo. ack_status rastreia resposta do Renato.';
COMMENT ON COLUMN news_digests.ack_status IS
    'pending=aguardando resposta; acked_ok=Renato disse "ok" (hits archived); drilled=Renato pediu detalhe de algum watcher; expired=48h sem resposta.';
COMMENT ON COLUMN news_digests.expires_at IS
    '48h apos sent_at. Cron de expire opcional muda ack_status pra expired.';

COMMENT ON COLUMN project_news_hits.digest_id IS
    'FK pro news_digest onde esse hit foi enviado. NULL = ainda nao foi pra nenhum digest.';
COMMENT ON COLUMN project_news_hits.archived_at IS
    'Quando o hit foi arquivado (Renato OK no digest, ou explicit archive). NULL = ativo.';
