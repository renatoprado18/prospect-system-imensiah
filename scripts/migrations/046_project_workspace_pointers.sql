-- 046_project_workspace_pointers.sql
-- Camada de workspace multi-superficie (11/07/2026).
--
-- Contexto: INTEL = sistema de registro (estado estruturado do projeto). As
-- superficies de trabalho novas — Projeto no claude.ai (workspace conversacional
-- que le o INTEL via CoPiloto MCP) e pasta local do Claude Code (oficina de
-- artefatos/drafts) — NAO devem replicar o estado. Coordenacao = PONTEIROS pro
-- ID canonico (projects.id), nao copia sincronizada (evita o silo tipo os 2
-- Neons INTEL x ConselhoOS). projects ja tem google_drive_folder_id; estas 2
-- colunas completam o registry de satelites.
--
-- Idempotente.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS claude_project_url TEXT,   -- Projeto no claude.ai (workspace)
    ADD COLUMN IF NOT EXISTS local_folder_path  TEXT;   -- pasta local Claude Code (oficina)

COMMENT ON COLUMN projects.claude_project_url IS
    'URL do Projeto no claude.ai que serve de workspace conversacional deste projeto (le INTEL via CoPiloto MCP). Ponteiro, nao copia.';
COMMENT ON COLUMN projects.local_folder_path IS
    'Caminho da pasta local (Claude Code) que serve de oficina de artefatos/drafts deste projeto. Ponteiro, nao copia.';
