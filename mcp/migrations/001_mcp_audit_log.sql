-- Migration 001 — mcp_audit_log
-- Trilha de auditoria de toda ESCRITA feita pelo CoPiloto MCP server.
-- Toda tool de escrita (create_task, update_task, create_document, create_note,
-- save_memory) grava uma linha aqui ANTES/DEPOIS de tocar a tabela fisica.
--
-- Aplicar local:  psql postgresql://rap@localhost/intel -f mcp/migrations/001_mcp_audit_log.sql
-- Idempotente (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS mcp_audit_log (
    id          SERIAL PRIMARY KEY,
    tool        TEXT        NOT NULL,           -- nome da tool (ex: create_document)
    args        JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- argumentos recebidos
    result      JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- resultado (id criado, status, erro)
    created_at  TIMESTAMP   NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')  -- UTC naive
);

CREATE INDEX IF NOT EXISTS idx_mcp_audit_tool      ON mcp_audit_log (tool);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_created   ON mcp_audit_log (created_at DESC);

COMMENT ON TABLE  mcp_audit_log        IS 'Trilha de auditoria de escritas do CoPiloto MCP server.';
COMMENT ON COLUMN mcp_audit_log.tool   IS 'Nome da tool MCP que executou a escrita.';
COMMENT ON COLUMN mcp_audit_log.args   IS 'Argumentos recebidos pela tool (JSON).';
COMMENT ON COLUMN mcp_audit_log.result IS 'Resultado da escrita: id criado, status ou mensagem de erro.';
