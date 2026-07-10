-- 044_raci_group_shadow.sql
-- FIX #2 (10/07) — fecha o fantasma do RACI updater.
--
-- process_group_message (aplica RACI) so roda no webhook, mas as mensagens de
-- grupo chegam pelo group_message_sync (lote), que so grava. Resultado: o
-- updater nunca corria nos reportes reais. Este fix adiciona um cron que varre
-- group_messages nao-processados e gera PROPOSTAS pra revisao do Renato
-- (shadow-first — nada auto-aplica no RACI do cliente).
--
-- Idempotente.

-- Marca quais group_messages ja passaram pelo processamento shadow.
ALTER TABLE group_messages
    ADD COLUMN IF NOT EXISTS raci_processed_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_group_messages_raci_unprocessed
    ON group_messages(timestamp)
    WHERE raci_processed_at IS NULL;

-- Propostas de update de RACI geradas a partir de mensagens de grupo, aguardando
-- aprovacao do Renato. NADA e aplicado no ConselhoOS ate status='applied'.
CREATE TABLE IF NOT EXISTS raci_group_proposals (
    id                SERIAL PRIMARY KEY,
    group_message_id  INTEGER,               -- group_messages.id de origem
    group_jid         TEXT,
    empresa_id        TEXT,                  -- ConselhoOS empresas.id (uuid como texto)
    empresa_nome      TEXT,
    item_id           TEXT,                  -- ConselhoOS raci_itens.id (uuid como texto)
    item_acao         TEXT,                  -- snapshot da acao do item (contexto pro review)
    action            TEXT,                  -- update_status | update_prazo | add_note | complete
    new_status        TEXT,
    new_prazo         DATE,
    notes             TEXT,
    evidencia         TEXT,                  -- trecho da msg que justifica
    confianca         TEXT,                  -- alta | media | baixa
    sender_name       TEXT,
    status            TEXT NOT NULL DEFAULT 'pending_review'
                          CHECK (status IN ('pending_review', 'approved', 'dismissed', 'applied', 'apply_error')),
    criado_em         TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    reviewed_at       TIMESTAMP,
    apply_result      TEXT                   -- resultado do apply_proposal (old->new) ou erro
);

CREATE INDEX IF NOT EXISTS idx_raci_group_proposals_pending
    ON raci_group_proposals(criado_em)
    WHERE status = 'pending_review';
