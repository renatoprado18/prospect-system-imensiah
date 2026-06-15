-- 031_wa_attachments.sql
-- Persiste anexos WA (PDF, audio, image) pra Brain poder consultar depois.
-- Hoje audio+image sao transcritos in-place e descartados. PDF nao tinha
-- handling. Esta tabela centraliza.

CREATE TABLE IF NOT EXISTS wa_attachments (
    id BIGSERIAL PRIMARY KEY,
    message_id TEXT NOT NULL,                       -- WA message id
    phone TEXT NOT NULL,                            -- numero remetente
    kind TEXT NOT NULL CHECK (kind IN ('pdf', 'audio', 'image')),
    original_filename TEXT,                         -- nome se for documento
    mime_type TEXT,
    size_bytes INT,
    extracted_text TEXT,                            -- transcrição (audio), descricao (image), texto (pdf)
    extraction_model TEXT,                          -- 'whisper', 'claude-haiku-vision', 'claude-sonnet-pdf'
    extraction_cost_usd NUMERIC(10,6),
    error TEXT,                                     -- se extracao falhou
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (message_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_wa_att_phone_recent ON wa_attachments(phone, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_wa_att_kind ON wa_attachments(kind, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_wa_att_text_search ON wa_attachments USING gin (to_tsvector('portuguese', COALESCE(extracted_text, '')));

COMMENT ON TABLE wa_attachments IS 'Anexos WA processados (PDF, audio, image). Brain consulta via search_context scope=attachments.';
