-- 055: Marcador de tentativa no arquivamento de anexos WA (mata o livelock)
--
-- Medido em produção 28/07/2026: o cron /api/cron/wa-drive-archive roda a cada
-- 20 min e acumulou 547 falhas em 215 runs de 3 dias — sobre um punhado de
-- itens. A causa é a mesma do wa-catchup (1eaf1be): a fila é definida só por
-- "drive_file_id IS NULL", e NADA registra que uma tentativa já foi feita e
-- falhou. Um anexo cuja mídia expirou na Evolution nunca vai baixar, mas volta
-- a ocupar slot do LIMIT a cada run, até envelhecer 3 dias e sair pela janela —
-- até 216 re-tentativas por item. E o cron reporta status='success' o tempo
-- todo, então nenhum monitor enxerga.
--
-- Calibração do teto (30 dias de dados, 906 anexos arquivados com sucesso):
--   84% arquivam em ≤20min (1ª tentativa)   88% em ≤4h (12 tentativas)
--   86% em ≤1h                              89% em ≤12h
-- Os poucos acima de 12h são ids consecutivos (679-689) = backfill único de
-- quando o cron subiu, não retry orgânico. 12 tentativas preservam a janela que
-- de fato entrega e cortam ~94% das chamadas inúteis à Evolution.
--
-- ADD COLUMN com default constante é instantâneo no PG 11+ (não reescreve a
-- tabela); são 1.594 linhas / 4 MB de qualquer forma.

ALTER TABLE wa_attachments
    ADD COLUMN IF NOT EXISTS drive_attempts        SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS drive_last_attempt_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS drive_last_error      TEXT;

-- A fila do cron passa a excluir quem esgotou as tentativas. O índice de 047
-- (só drive_file_id IS NULL) continua válido para outros consumidores; este é
-- o que o SELECT do cron usa agora.
--
-- O teto vive no código (WA_DRIVE_MAX_ATTEMPTS, default 12) e não pode entrar
-- num índice parcial — a expressão do WHERE precisa ser imutável. Por isso o
-- índice indexa drive_attempts e o filtro numérico fica na query.
CREATE INDEX IF NOT EXISTS idx_wa_attachments_drive_queue
    ON wa_attachments (drive_attempts, criado_em DESC)
    WHERE drive_file_id IS NULL;
