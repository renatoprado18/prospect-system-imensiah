-- 080 — o schema `copilot` era ESTRUTURALMENTE cego ao texto dos anexos.
--
-- 08/09/26. O pipeline transcreve audio (whisper-large-v3) e faz OCR de imagem
-- ha meses: 386 de 412 audios e 3.027 imagens tem `wa_attachments.extracted_text`
-- preenchido. Mas as views que a camada CoS consome (MCP `search_group_messages`,
-- `get_cockpit`, briefing da tonIAH) serviam `content` cru — para midia, o
-- literal `[Audio]` / `[Imagem]`.
--
-- Nao era bug de query: a view `copilot.group_messages` nao expunha nem o
-- `message_id`, que e a chave do join, e nao existe `copilot.wa_attachments`.
-- Quem quisesse o texto NAO TINHA COMO chegar nele por este schema. Foi assim
-- que uma medicao de 08/09 concluiu que o audio do Kesley "nao e capturado"
-- quando ele estava transcrito desde 05/09.
--
-- O custo real: os 3 audios da Andressa de 24/08 19h34 — Itau e Banco do Brasil
-- recusando os extratos da Carambola, e a proposta de pedir ao juiz — estavam
-- no banco desde o dia seguinte, com prazo em 11/09, tratados como canal cego.
--
-- `cos_agent_ro`/`cos_agent_rw` JA tinham SELECT em `public.wa_attachments`;
-- faltava so a view usar. Nenhum GRANT novo e necessario.
--
-- Reversivel: as duas views voltam trocando a expressao de `content` por
-- `gm.content` / `m.conteudo` (e removendo `message_id` do fim).

BEGIN;

-- group_messages: `content` passa a trazer a transcricao/OCR quando existe.
-- `message_id` entra no FIM (nunca no meio: CREATE OR REPLACE exige que as
-- colunas ja existentes mantenham nome, tipo e ordem).
CREATE OR REPLACE VIEW copilot.group_messages AS
 SELECT gm.id,
    gm.group_jid,
    COALESCE(pwg.group_name, sgc.group_name) AS group_name,
    pwg.project_id,
    gm.sender_name,
    gm.contact_id,
    COALESCE(
      NULLIF((SELECT wa_x.extracted_text
                FROM wa_attachments wa_x
               WHERE wa_x.message_id = gm.message_id
                 AND wa_x.extracted_text IS NOT NULL
                 AND wa_x.extracted_text <> ''
               ORDER BY wa_x.id
               LIMIT 1), ''),
      gm.content) AS content,
    gm.message_type,
    gm.from_me,
    gm."timestamp",
    gm.message_id
   FROM group_messages gm
     LEFT JOIN LATERAL ( SELECT p.group_name,
            p.project_id
           FROM project_whatsapp_groups p
          WHERE p.group_jid = gm.group_jid AND p.ativo
          ORDER BY p.id
         LIMIT 1) pwg ON true
     LEFT JOIN LATERAL ( SELECT s.group_name
           FROM social_groups_cache s
          WHERE s.group_jid = gm.group_jid
         LIMIT 1) sgc ON true;

-- messages (DM): mesma ideia, casando por `external_id`.
CREATE OR REPLACE VIEW copilot.messages AS
 SELECT m.id,
    m.conversation_id,
    m.contact_id,
    c.canal,
    m.direcao AS direction,
    COALESCE(
      NULLIF((SELECT wa_x.extracted_text
                FROM wa_attachments wa_x
               WHERE wa_x.message_id = m.external_id
                 AND wa_x.extracted_text IS NOT NULL
                 AND wa_x.extracted_text <> ''
               ORDER BY wa_x.id
               LIMIT 1), ''),
      m.conteudo) AS content,
    COALESCE(m.enviado_em, m.recebido_em, m.criado_em) AS "timestamp"
   FROM messages m
     LEFT JOIN conversations c ON c.id = m.conversation_id;

COMMIT;
