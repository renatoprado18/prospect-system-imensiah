-- 047: Arquivamento dos binários de anexos WA no Google Drive (F-2, Passo B — raw completo)
--
-- O ingest de anexos WA extrai texto (extracted_text) mas descarta o binário
-- original. Este passo re-baixa o binário da Evolution (cron desacoplado) e o
-- arquiva no Drive do Renato, guardando aqui a referência. Só go-forward.

ALTER TABLE wa_attachments
    ADD COLUMN IF NOT EXISTS drive_file_id     TEXT,
    ADD COLUMN IF NOT EXISTS drive_web_link    TEXT,
    ADD COLUMN IF NOT EXISTS drive_archived_at TIMESTAMP;

-- Fila do cron: anexos ainda não arquivados, mais recentes primeiro (a mídia da
-- Evolution expira, então só os últimos dias valem re-download).
CREATE INDEX IF NOT EXISTS idx_wa_attachments_drive_pending
    ON wa_attachments (criado_em DESC)
    WHERE drive_file_id IS NULL;
