-- 027_tonha_decisions.sql
-- Camada 2 da rebuild Tonha (15/06/26) — audit log das decisoes dela.
--
-- Cada vez que Tonha (Sonnet 4.6) decide algo — auto-execute, draft+send,
-- escalate, silence — grava aqui. Permite review do que ela fez, debugging,
-- e reverter decisoes erradas.
--
-- Ver docs/ARCHITECTURE_REBUILD.md secao 4.

CREATE TABLE IF NOT EXISTS tonha_decisions (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT REFERENCES signals(id) ON DELETE SET NULL,
    decision_type TEXT NOT NULL CHECK (decision_type IN ('auto_execute','draft_and_send','escalate','silence','delegate')),
    decision_summary TEXT NOT NULL,                         -- 1 linha do que decidiu
    reasoning TEXT,                                         -- pensamento (extended thinking, truncado 2000 chars)
    action_taken JSONB,                                     -- {tool: 'send_message', params: {...}, result: '...'}
    cost_usd FLOAT,
    model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    iteration_count INT,                                    -- quantos turns de tool_use
    mode TEXT NOT NULL CHECK (mode IN ('reactive','autonomous')),
    triggered_by TEXT NOT NULL,                             -- 'wa_msg' | 'chat_web' | 'cron_loop'
    triggered_by_ref TEXT,                                  -- bot_conversation_id | signal_id | etc.
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    -- Pra rollback / reversal
    reverted_at TIMESTAMP,
    reverted_by TEXT,                                       -- 'renato' | 'auto_revert' | NULL se ativa
    reverted_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_signal ON tonha_decisions(signal_id) WHERE signal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_recent ON tonha_decisions(criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_mode ON tonha_decisions(mode, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_active ON tonha_decisions(criado_em DESC) WHERE reverted_at IS NULL;

-- FK signals.decision_id -> tonha_decisions.id (deferred to here pois 026 cria signals sem FK)
ALTER TABLE signals
    ADD CONSTRAINT signals_decision_id_fkey
    FOREIGN KEY (decision_id) REFERENCES tonha_decisions(id) ON DELETE SET NULL;

COMMENT ON TABLE tonha_decisions IS 'Audit log de toda decisao da Tonha (autonomous e reactive). Permite review + rollback.';
COMMENT ON COLUMN tonha_decisions.decision_type IS 'auto_execute=fez sem perguntar, draft_and_send=mandou WA/email, escalate=mandou notif pro Renato, silence=ignorou consciente, delegate=passou pro Claude Code';
COMMENT ON COLUMN tonha_decisions.action_taken IS 'Snapshot do que executou: tool + params + result. Permite reproducao/reverter.';
