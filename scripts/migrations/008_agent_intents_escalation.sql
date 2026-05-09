-- Migration 008: agent_intents escalation (P6 Diligente — Fase 2)
-- Date: 2026-05-08
--
-- Adiciona escalated_at em agent_intents pra dedup de escalacao via WhatsApp.
-- Quando intent fica blocked > 60min e ainda nao foi escalado, o cron
-- agent-intents-tick manda WA pro Renato e seta escalated_at = NOW().
-- Isso evita spam (1 escalacao por blocker; se blocker mudar, pode escalar de novo
-- via reset manual de escalated_at).
--
-- Idempotente. Pode rodar varias vezes sem erro.

ALTER TABLE agent_intents ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMP;

-- Index parcial: query do cron filtra exatamente esse subconjunto.
-- "blocked + nao escalado" e o caso quente; nao quero scan completo quando
-- agent_intents virar centenas de linhas.
CREATE INDEX IF NOT EXISTS idx_agent_intents_escalated
    ON agent_intents(updated_at)
    WHERE status = 'blocked' AND escalated_at IS NULL;
