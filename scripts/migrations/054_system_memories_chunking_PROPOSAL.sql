-- Migration 054 (PROPOSAL — NÃO APLICADA) — Chunking de granularidade
-- Buraco #2 (granularidade): o sync .md→system_memories é arquivo-inteiro.
-- Boards gigantes viram 1 linha + 1 embedding inútil:
--   dev_backlog      = 118.169 chars  -> id 350
--   session-locks    =  96.846 chars  -> id 674
--   cos_status board =  85.948 chars  -> id 349
-- Um único vetor 1024d sobre 118 KB é uma média borrada — a busca semântica
-- não recupera um fato específico, e a keyword devolve o blob inteiro (os
-- consumidores cortam conteudo[:500] => só o frontmatter aparece; o fato
-- soterrado é "recuperado" mas INVISÍVEL). Ver reference_memory_architecture #2.
--
-- STATUS: PROPOSTA. NÃO rodar solta — depende da mudança de código no endpoint
-- /api/system-memories/sync-file (splitter por seção) descrita no relatório.
-- Reversível: colunas nullable + índice novo; nada é dropado.
--
-- Estratégia (Fase 1 — aditiva, não-destrutiva):
--  1. Adiciona rastreio de origem/seção pra permitir 1 linha POR SEÇÃO de um .md.
--  2. Mantém as linhas arquivo-inteiro atuais funcionando (source_file/section NULL).
--  3. O endpoint passa a: (a) dividir o body por headings markdown (##/###),
--     (b) upsert 1 row por seção com embedding próprio, (c) deletar seções órfãs
--     do mesmo source_file (quando uma seção some/renomeia).
--  4. Unique parcial nova em (source_file, section_anchor) pros chunks; a unique
--     legada idx_system_memories_unique_migration (titulo WHERE fonte=cc) continua
--     valendo pros memos pequenos que seguem 1-arquivo-1-linha.

ALTER TABLE system_memories
    ADD COLUMN IF NOT EXISTS source_file    TEXT,   -- ex.: 'project_dev_backlog.md'
    ADD COLUMN IF NOT EXISTS section_anchor TEXT,   -- slug do heading da seção
    ADD COLUMN IF NOT EXISTS section_order  INT;     -- ordem da seção no arquivo

-- Unique pros chunks (só quando source_file+section_anchor presentes).
CREATE UNIQUE INDEX IF NOT EXISTS idx_system_memories_chunk_unique
    ON system_memories (source_file, section_anchor)
    WHERE source_file IS NOT NULL AND section_anchor IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_system_memories_source_file
    ON system_memories (source_file)
    WHERE source_file IS NOT NULL;

-- Rollback:
--   DROP INDEX IF EXISTS idx_system_memories_chunk_unique;
--   DROP INDEX IF EXISTS idx_system_memories_source_file;
--   ALTER TABLE system_memories DROP COLUMN IF EXISTS source_file,
--       DROP COLUMN IF EXISTS section_anchor, DROP COLUMN IF EXISTS section_order;
