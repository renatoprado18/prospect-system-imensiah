-- Migration: Add atualizado_em columns + auto-update triggers
-- Date: 2026-05-01
-- Description: Habilita sync incremental local->remoto para tasks e project_milestones.
-- O script sync-to-remote.sh usa atualizado_em para detectar mudancas; sem ele, so
-- sincroniza com --force e nao para project_milestones (que estava fora da lista).

-- 1. Funcao generica para auto-atualizar atualizado_em em UPDATE
CREATE OR REPLACE FUNCTION set_atualizado_em()
RETURNS TRIGGER AS $$
BEGIN
    NEW.atualizado_em = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 2. tasks: adicionar coluna + trigger
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

DROP TRIGGER IF EXISTS trg_tasks_atualizado_em ON tasks;
CREATE TRIGGER trg_tasks_atualizado_em
    BEFORE UPDATE ON tasks
    FOR EACH ROW
    EXECUTE FUNCTION set_atualizado_em();

-- 3. project_milestones: adicionar coluna + trigger
ALTER TABLE project_milestones
    ADD COLUMN IF NOT EXISTS atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

DROP TRIGGER IF EXISTS trg_project_milestones_atualizado_em ON project_milestones;
CREATE TRIGGER trg_project_milestones_atualizado_em
    BEFORE UPDATE ON project_milestones
    FOR EACH ROW
    EXECUTE FUNCTION set_atualizado_em();
