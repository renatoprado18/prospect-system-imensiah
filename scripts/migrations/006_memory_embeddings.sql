-- Migration 006: vector embeddings em system_memories (Fase 6 — Life Coaching)
-- Date: 2026-05-07
--
-- Adiciona coluna `embedding` (pgvector) em system_memories pra busca semantica.
-- Sem isso, search_system_memories so faz keyword e perde sinonimos / parafraseamentos
-- (ex: user pergunta "drenado" e nao acha memoria "exausto").
--
-- Provider: Voyage AI (voyage-4-lite, 1024 dims, multilingual incluindo PT-BR).
-- Index: HNSW com cosine distance — bom recall + insercao razoavel pra volume baixo (<100 mems).
--
-- Idempotente: pode rodar varias vezes sem erro. Pra rodar em prod (Neon),
-- pgvector ja esta disponivel — basta executar este arquivo via psql.

-- Garantir que extensao vector esta ativa
CREATE EXTENSION IF NOT EXISTS vector;

-- Coluna de embedding (1024 dims do voyage-4-lite)
ALTER TABLE system_memories
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- Index HNSW (cosine distance — alinhado com Voyage, que normaliza embeddings).
-- m=16 (default), ef_construction=64 (default) — adequado pro volume atual.
-- IMPORTANT: HNSW indexes em pgvector podem ser criados em coluna que pode ser NULL;
-- linhas com embedding NULL nao entram no index, sao filtradas naturalmente.
CREATE INDEX IF NOT EXISTS idx_system_memories_embedding
    ON system_memories
    USING hnsw (embedding vector_cosine_ops);

-- Sinaliza visualmente memorias sem embedding pro backfill saber o que falta
CREATE INDEX IF NOT EXISTS idx_system_memories_embedding_null
    ON system_memories (id)
    WHERE embedding IS NULL;
