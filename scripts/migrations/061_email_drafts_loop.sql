-- 061 — fechamento do loop DRAFT → ENVIADO para e-mail.
--
-- POR QUE. 04/08/2026: a camada preparou um e-mail para o pai, deixou no
-- rascunho do Gmail e reportou ao Renato como "pronto no rascunho". Ele já
-- tinha enviado às 16h29. O sistema sabia o que ele mesmo CRIOU e era cego ao
-- que o Renato FEZ — a mesma classe de defeito de feedback_cos_action_blindness
-- (13/06), agora do lado do rascunho em vez do lado do envio.
--
-- A cegueira era literal: `admin_gmail_proxy` action=create_draft chamava a
-- Gmail API, recebia {id, message:{id, threadId}} e DEVOLVIA a resposta ao
-- caller HTTP. Nada persistia. Não existia a linha que dissesse "existe um
-- rascunho meu esperando o Renato" — logo não havia o que reconciliar quando o
-- e-mail aparecia em `in:sent`.
--
-- ⚠️ O message id MUDA quando um rascunho é enviado: o rascunho tem um
-- message id próprio, e o Gmail cria OUTRO ao despachar. Casar draft com
-- enviado por message id não casa nunca — falharia calado, do jeito de
-- feedback_filtro_vocabulario_errado_falha_calado (zero linhas, zero erro).
-- `thread_id` é a única âncora estável entre os dois: sobrevive ao envio, à
-- edição do rascunho pelo Renato e à resposta do outro lado.
--
-- POR QUE TABELA NOVA E NÃO `cos_draft_responses` (017). São dois objetos com
-- ciclos de vida opostos, apesar do nome parecido:
--   · cos_draft_responses guarda TEXTO que o Investigator propôs e que só sai
--     do lugar se o Renato APROVAR — a máquina é quem envia depois. Por isso
--     tem approved_at, rejected, expires_at 48h. Nada dela existe no Gmail.
--   · esta tabela guarda um objeto que JÁ EXISTE no Gmail, criado pela máquina,
--     e cujo ato de envio é do Renato, à mão, fora do sistema. Não há aprovação
--     a esperar nem expiração a cumprir: um rascunho pendente há 3 dias é um
--     fato sobre o Renato, não lixo a varrer.
-- Enfiar o Gmail draft em cos_draft_responses exigiria 5 colunas novas
-- (draft_id, thread_id, message_id, account, sent_message_id), afrouxar o CHECK
-- de status e conviver com o cron de expiração de 48h apagando rascunho vivo.
-- O acoplamento custaria mais que a tabela.
--
-- O QUE ESTA TABELA NÃO É: prova de que o Renato enviou o texto QUE A MÁQUINA
-- ESCREVEU. Ele pode abrir o rascunho, reescrever tudo e mandar. O que a
-- reconciliação afirma é mais estreito e é o que importa para não cobrar duas
-- vezes: "a conversa que este rascunho abria saiu da caixa". Quem quiser
-- comparar texto tem `sent_message_id` para ir buscar.

CREATE TABLE IF NOT EXISTS email_drafts (
    id              SERIAL PRIMARY KEY,

    -- Ids do Gmail. `draft_id` é o do objeto Draft (estável enquanto o rascunho
    -- existir); `draft_message_id` é o da mensagem DENTRO do rascunho e está
    -- aqui só para auditoria — ver o aviso acima sobre ele mudar no envio.
    draft_id            TEXT NOT NULL,
    thread_id           TEXT,
    draft_message_id    TEXT,

    account_email   TEXT,                       -- qual conta Gmail criou
    to_emails       JSONB NOT NULL DEFAULT '[]'::jsonb,
    subject         TEXT,
    contact_id      INTEGER REFERENCES contacts(id) ON DELETE SET NULL,

    -- Quem pediu o rascunho (cos_agent, sessao_claude_code, tonia, ...). Sem
    -- isto não dá para medir qual produtor gera rascunho que o Renato usa e
    -- qual gera rascunho que ele ignora.
    source          TEXT,

    -- 'pending'  -> criado, ninguém mandou ainda
    -- 'sent'     -> apareceu mensagem enviada na mesma thread depois da criação
    -- 'discarded'-> fechado à mão (o Renato desistiu); nunca automático
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sent', 'discarded')),

    sent_at             TIMESTAMP,  -- hora do ENVIO (header Date do e-mail)
    sent_message_id     TEXT,       -- id da mensagem que fechou o loop
    reconciled_at       TIMESTAMP,  -- hora em que o sync percebeu

    criado_em       TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Um rascunho do Gmail = uma linha. Reregistrar o mesmo draft_id atualiza
    -- em vez de duplicar (o proxy pode ser chamado de novo na mesma thread).
    UNIQUE (draft_id)
);

-- O único acesso quente é o da reconciliação: "há rascunho pendente nesta
-- thread?", uma vez por e-mail enviado a cada run do sync de 30min.
CREATE INDEX IF NOT EXISTS idx_email_drafts_thread_pending
    ON email_drafts (thread_id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_email_drafts_criado
    ON email_drafts (criado_em DESC);

COMMENT ON TABLE email_drafts IS
'Rascunhos de e-mail que a camada criou no Gmail. Fecha o loop DRAFT->ENVIADO: gmail_outbound_sync reconcilia por thread_id (NUNCA por message id — ele muda no envio). Caso 04/08/26: e-mail ao pai reportado como "pronto no rascunho" 40min depois de o Renato ter enviado.';

COMMENT ON COLUMN email_drafts.thread_id IS
'Ancora estavel entre rascunho e enviado. O message id do rascunho e DIFERENTE do da mensagem enviada — casar por ele nao casa nunca.';

COMMENT ON COLUMN email_drafts.status IS
'pending=criado e nao enviado. sent=reconciliado por thread_id com outgoing posterior a criacao. discarded=fechado a mao, nunca automatico (desfazer o dado por concluido nao e Auto — feedback_cos_autonomy_policy).';
