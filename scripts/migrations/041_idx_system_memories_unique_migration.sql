-- 039_idx_system_memories_unique_migration.sql
-- Formaliza index criado direto via psql 28/06/2026 durante F1 hook
-- sync (.md → Neon). Garante UPSERT atomico (titulo) WHERE
-- fonte='claude_code_migration' — evita duplicar memo quando hook re-roda.
--
-- Idempotente: IF NOT EXISTS.

CREATE UNIQUE INDEX IF NOT EXISTS idx_system_memories_unique_migration
  ON system_memories (titulo)
  WHERE fonte = 'claude_code_migration';
