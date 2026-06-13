-- 025_proposal_block_rules.sql
-- Fix bug "Ignorar e nao sugerir mais" (13/06/2026).
--
-- O frontend chama dismissAndBlock que envia POST /api/action-proposals/{id}/dismiss?block=true,
-- mas o endpoint ignorava o parametro — proposta era dismissed e o sensor/analyzer recriava
-- igual no proximo tick. Auditoria 13/06 mostrou #714 RACI Critico re-aparecendo apos clique.
--
-- Regra de bloqueio = tupla (action_type, contact_id, title_prefix) ou (action_type, signature).
-- Se contact_id NULL, bloqueia por title_prefix (ex.: "RACI Critico" — propostas do sensor sem contato).
-- Se contact_id presente, bloqueia tipo+contato (ex.: pending_response para Heloisa).

CREATE TABLE IF NOT EXISTS proposal_block_rules (
  id              SERIAL PRIMARY KEY,
  action_type     TEXT NOT NULL,
  contact_id      INT REFERENCES contacts(id) ON DELETE CASCADE,
  title_prefix    TEXT,                                  -- primeiros 60 chars do title
  reason          TEXT,
  source_proposal_id  INT REFERENCES action_proposals(id) ON DELETE SET NULL,
  criado_em       TIMESTAMP DEFAULT NOW(),
  CHECK (contact_id IS NOT NULL OR title_prefix IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_pbr_contact_type
  ON proposal_block_rules(contact_id, action_type)
  WHERE contact_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pbr_title_prefix
  ON proposal_block_rules(action_type, title_prefix)
  WHERE title_prefix IS NOT NULL;
