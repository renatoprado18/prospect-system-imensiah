-- 032 — bot_conversations dedup atomico
--
-- Triple-fire encontrado 16/06/26: msg 780 Renato disparou 3 reactive_decisions
-- (116/117/118) em 13s, cada um custou Sonnet 4.6 separado ($0.022 + $0.021 +
-- $0.044). Causa: webhook Evolution entrega ate 3x; check-then-act dedup em
-- intel_bot.py:2526 tem race entre SELECT e INSERT.
--
-- Fix: dedup_key TEXT computado pelo app (md5(phone:content:minute_bucket))
-- com unique index partial (so role='user'). INSERT ... ON CONFLICT DO NOTHING
-- RETURNING id vira atomico. Se id null -> dup, returna "" no caller.
--
-- Bucket de 1 minuto: msgs identicas espacadas > 60s sao legitimas.
ALTER TABLE bot_conversations
    ADD COLUMN IF NOT EXISTS dedup_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_conversations_user_dedup
    ON bot_conversations (dedup_key)
    WHERE dedup_key IS NOT NULL;
