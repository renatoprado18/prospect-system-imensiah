-- 053 — (B) Roteador de sinal: vínculo mensagem → projeto por CONTEÚDO.
-- Cada inbound (email/WA DM) é etiquetado com o(s) projeto(s) que toca, pra a
-- frente_review ler por conteúdo (não só por membership). project_id=0 = sentinela
-- "processado, sem vínculo" (não re-roda LLM). Sem FK em project_id de propósito
-- (o 0 sentinela + tolerância a projeto removido). Read-only sobre a mensagem.
CREATE TABLE IF NOT EXISTS message_project_links (
    message_id  BIGINT      NOT NULL,
    project_id  INTEGER     NOT NULL,   -- 0 = sentinela sem-vínculo
    source      TEXT        NOT NULL DEFAULT 'router',  -- router | none | manual
    confidence  REAL        DEFAULT 0.85,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (message_id, project_id)
);

-- leitura pela frente_review: mensagens de um projeto
CREATE INDEX IF NOT EXISTS idx_mpl_project ON message_project_links (project_id) WHERE project_id > 0;
