-- 030_inbox_digest_buffer.sql
-- Sec 4.7 do ARCHITECTURE_REBUILD.md — triagem de inbox.
-- Buffer pra emails/WA DMs de urgencia media. Tonha bate tudo num
-- digest no briefing 7h BRT.

CREATE TABLE IF NOT EXISTS inbox_digest_buffer (
    id BIGSERIAL PRIMARY KEY,
    fonte TEXT NOT NULL CHECK (fonte IN ('gmail', 'wa_dm')),
    ref_id TEXT NOT NULL,                   -- thread_id (gmail) ou message_id (wa)
    preview TEXT,                           -- primeiros 500 chars
    from_label TEXT,                        -- "Nome <email>" ou nome do contato
    subject TEXT,
    received_at TIMESTAMP NOT NULL,
    delivered_in_digest_at TIMESTAMP,       -- quando virou parte do briefing
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (fonte, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_inbox_buffer_pending
    ON inbox_digest_buffer(received_at)
    WHERE delivered_in_digest_at IS NULL;

COMMENT ON TABLE inbox_digest_buffer IS 'Sec 4.7 — buffer de emails/WA DMs de urgencia media. Esvazia no briefing 7h BRT.';
