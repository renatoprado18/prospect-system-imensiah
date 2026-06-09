-- 017_cos_agentic.sql
-- Onda 2 — Arquitetura CoS agentica v0 (10/jun/2026).
--
-- Pivot do briefing CoS determinístico/alucinatório (que inferia estado
-- de contatos sem ler mensagens reais) pra arquitetura agentica de
-- verdade: um Investigator agent (LLM com tool use) que descobre quem é
-- relevante a partir das pendências do dia e investiga sob demanda.
--
-- Três tabelas:
--
-- 1. cos_briefing_items — pool de items categorizados que o briefing 8h
--    LÊ (não gera). O Investigator (cron 7h10) popula. Categorias:
--    feito (já executado), one_way (decisão binária pro Renato),
--    monitor (observação factual), escalated (atenção alta).
--
-- 2. cos_action_log — trace completo de cada tool call do Investigator
--    pra auditoria, debug e custo. ToolUse + result + duration + error.
--
-- 3. cos_draft_responses — rascunhos que o Investigator gerou pra Renato
--    aprovar antes de enviar. WA/email/LinkedIn DM. Expira em 48h.
--
-- Princípios (ratificados com usuário):
-- - CoS descobre relevantes via pendências, não hardcode
-- - Investigação sob demanda
-- - Ação > reporte
-- - One-way decisions explícitas
-- - Auditabilidade (cada ação tem ID, motivo, revertible 48h)
-- - Bounded reasoning (max iterations, max tools/iter, max custo/dia)

CREATE TABLE IF NOT EXISTS cos_briefing_items (
    id              SERIAL PRIMARY KEY,
    cycle_id        TEXT NOT NULL,                          -- "2026-06-10-morning"
    categoria       TEXT NOT NULL CHECK (categoria IN ('feito','one_way','monitor','escalated')),
    frente          INT CHECK (frente BETWEEN 1 AND 5),     -- frente CoS (1=Vallen, 2=imensIAH, 3a=familia, 3b=saude, etc — int simplificado)
    texto           TEXT NOT NULL,
    refs            JSONB DEFAULT '{}'::jsonb,              -- {task_id, event_id, draft_id, message_ids, contact_id, project_id}
    prioridade      INT DEFAULT 5,                          -- 1=critical, 5=normal, 9=baixa
    ja_reportado_em TIMESTAMP,                              -- preenchido apos briefing 8h enviar
    criado_em       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cos_briefing_items_cycle
    ON cos_briefing_items(cycle_id);

CREATE INDEX IF NOT EXISTS idx_cos_briefing_items_unrep
    ON cos_briefing_items(categoria)
    WHERE ja_reportado_em IS NULL;

COMMENT ON TABLE cos_briefing_items IS
    'Pool de items que o briefing 8h LÊ (compose_briefing_from_items). Populado pelo Investigator (cron 7h10). Categorias ✅/🟡/👀/escalated.';
COMMENT ON COLUMN cos_briefing_items.cycle_id IS
    'Agrupador do ciclo. Formato: YYYY-MM-DD-morning ou YYYY-MM-DD-evening.';
COMMENT ON COLUMN cos_briefing_items.categoria IS
    'feito=acao ja executada (✅). one_way=decisao binaria pro Renato (🟡). monitor=observacao factual (👀). escalated=critico, sobe pra topo.';


CREATE TABLE IF NOT EXISTS cos_action_log (
    id              SERIAL PRIMARY KEY,
    cycle_id        TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    params          JSONB,
    result          JSONB,
    iteration       INT,
    duration_ms     INT,
    error_message   TEXT,
    criado_em       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cos_action_log_cycle
    ON cos_action_log(cycle_id);

CREATE INDEX IF NOT EXISTS idx_cos_action_log_tool
    ON cos_action_log(tool_name, criado_em DESC);

COMMENT ON TABLE cos_action_log IS
    'Trace de cada tool call do Investigator pra auditoria/debug/custo. Toda tool chama log_tool_call() no fim.';


CREATE TABLE IF NOT EXISTS cos_draft_responses (
    id              SERIAL PRIMARY KEY,
    contact_id      INT REFERENCES contacts(id) ON DELETE SET NULL,
    channel         TEXT NOT NULL CHECK (channel IN ('whatsapp','email','linkedin_dm')),
    text_draft      TEXT NOT NULL,
    motivo          TEXT,
    status          TEXT DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','sent','expired')),
    approved_at     TIMESTAMP,
    sent_at         TIMESTAMP,
    expires_at      TIMESTAMP NOT NULL DEFAULT (NOW() + INTERVAL '48 hours'),
    criado_em       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cos_draft_responses_pending
    ON cos_draft_responses(status, expires_at)
    WHERE status = 'pending';

COMMENT ON TABLE cos_draft_responses IS
    'Rascunhos que o Investigator gerou pra Renato aprovar antes de enviar. Expira em 48h se nao for tocado.';
COMMENT ON COLUMN cos_draft_responses.motivo IS
    'Justificativa curta do Investigator pra por que esse draft existe (ex: "Thalita pediu ETA do plano de acao na ultima mensagem").';
