-- 048_copilot_emails.sql
-- Contrato de leitura de EMAIL pra Tonia/CoPiloto (F-2 blind spot de email, 11/07/26).
--
-- Ate agora o schema `copilot` (043) expunha `copilot.messages` (email+WA juntos,
-- so id/canal/content/direction/timestamp) mas NAO havia superficie de email com o
-- assunto, remetente e a triagem (prioridade/classificacao). A Tonia era cega pra
-- email no briefing/urgent. Esta view junta email_triage + messages pra dar um
-- contrato email-cêntrico: corpo (conteudo, agora capturado com fallback HTML->texto
-- pela mudanca em email_triage.py) + assunto/from (messages.metadata) + a inteligencia
-- de triagem (priority 1-10, classification, needs_attention, status).
--
-- Base = email_triage (a tabela de "inteligencia de email"): cada linha e um email
-- ja triado. LEFT JOIN messages/conversations pra corpo e canal. Emails em messages
-- sem linha de triagem (raros) ficam de fora — por design, o valor esta na triagem.
--
-- Contrato: docs/COPILOT_CONTRACT.md. Regras: colunas so sao ADICIONADAS, nunca
-- removidas/renomeadas. Idempotente (CREATE OR REPLACE VIEW).

CREATE SCHEMA IF NOT EXISTS copilot;

-- =============================================================================
-- copilot.emails — emails triados com corpo + assunto + remetente + prioridade
-- Aliases: content ← conteudo, direction ← direcao, subject ← metadata.subject,
-- from_email ← metadata.from, from_name ← metadata.from_name.
-- =============================================================================
CREATE OR REPLACE VIEW copilot.emails AS
SELECT
    et.id,
    et.message_id,
    et.contact_id,
    COALESCE(et.account_email, m.metadata->>'account')      AS account_email,
    m.metadata->>'from'                                     AS from_email,
    m.metadata->>'from_name'                                AS from_name,
    COALESCE(m.metadata->>'subject', conv.assunto)          AS subject,
    m.conteudo                                              AS content,
    (m.conteudo IS NOT NULL AND length(m.conteudo) > 0)     AS has_body,
    m.direcao                                               AS direction,
    et.priority,
    et.classification,
    et.needs_attention,
    et.status,
    et.account_type,
    COALESCE(m.enviado_em, m.recebido_em, m.criado_em)      AS "timestamp",
    et.criado_em
FROM public.email_triage et
LEFT JOIN public.messages m       ON m.id = et.message_id
LEFT JOIN public.conversations conv ON conv.id = et.conversation_id;

COMMENT ON VIEW copilot.emails IS 'Emails triados: corpo (content, com fallback HTML->texto), assunto/remetente (de messages.metadata), triagem (priority 1-10, classification, needs_attention, status pending/approved/dismissed). has_body=false marca email sem corpo capturado. timestamp UTC naive.';
