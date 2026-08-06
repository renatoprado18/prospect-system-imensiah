-- 064 — atos_que_resolvem: quem fecha a tarefa nem sempre e o Renato
--
-- POR QUE (pedido da CoS, 06/08/26). A view `acao_do_renato` (062) fechou o
-- "ele ja fez isso?". Mas o Renato perguntou DUAS vezes no cockpit por que a
-- task do G100 (#999562) seguia aberta se a **Andressa ja tinha dito no WA que
-- pagou** — "Esta sim. Paguei hoje.", 05/08 16:48, respondendo ao boleto que
-- ele mandou a ela em 31/07.
--
-- Eram dois furos somados:
--   (1) a task estava com `contact_id` NULL — o check F cruza
--       `tasks.contact_id = messages.contact_id` e nunca casaria;
--   (2) mesmo linkada, apontaria pro contato do G100 (a CONTRAPARTE), enquanto
--       quem confirma o pagamento e a ANDRESSA (a EXECUTORA).
--
-- Nao havia cano "delegado confirma tarefa feita -> tarefa fecha". O eixo novo
-- e esse: o ato que resolve pode vir de um TERCEIRO DELEGADO. Andressa paga e
-- regulariza; Priscila fecha o contabil; Piccino protocola.
--
-- QUEM SAO OS EXECUTORES: `tonha_role_contacts`, a fonte de verdade dos papeis
-- (nunca hardcode de contact_id — ver feedback_no_hardcoded_contact_ids). Papel
-- novo entra la e aparece aqui sozinho.
--
-- ⚠️ O QUE ESTA VIEW NAO FAZ, E POR QUE. Ela NAO filtra por "declarou execucao".
-- O regex de verbo acerta ~15% (65 de 439 mensagens da Andressa em 90d) e nao
-- distingue FEITO de VOU FAZER: "vou transferir agora", "poderia verificar",
-- "obrigada pelo pagamento" e "podemos enviar e-mail" casam todos. Filtrar por
-- vocabulario e exatamente o defeito que derrubamos no check G em 05/08, quando
-- um regex descartava 83% em silencio. Entao `declara_execucao` e SINAL, nao
-- porta: ordena a leitura, nunca esconde a linha. Quem julga tem contexto; o
-- SQL nao tem.

CREATE OR REPLACE VIEW atos_que_resolvem AS
    -- O Renato, por qualquer canal (view 062 inteira, sem duplicar regra).
    SELECT
        'renato'::text        AS quem,
        NULL::text            AS papel,
        a.origem_tabela,
        a.origem_id,
        a.canal,
        a.pessoa_id,
        a.o_que,
        a.evidencia,
        a.ref,
        a.quando,
        false                 AS declara_execucao
    FROM acao_do_renato a

    UNION ALL

    -- O executor delegado. A mensagem e `incoming` (ele avisando o Renato), e e
    -- justamente por isso que nenhuma consulta de "outbound do Renato" a via.
    SELECT
        'executor',
        r.role,
        'messages',
        m.id,
        cv.canal,
        m.contact_id,
        'executor relatou',
        left(m.conteudo, 240),
        m.external_id::text,
        COALESCE(m.recebido_em, m.criado_em),
        m.conteudo ~* '\m(paguei|pago|paga|quitad|transferi|enviei|enviado|mandei|protocol|assinei|assinad|emiti|emitid|entreguei|entregue|regulariz|resolvi|resolvid|conclu|finaliz|feito|fiz|agendei|agendad|solicitei|cancelei|cancelad|baixad|reduzi)'
    FROM messages m
    JOIN conversations cv        ON cv.id = m.conversation_id
    JOIN tonha_role_contacts r   ON r.contact_id = m.contact_id
    WHERE m.direcao = 'incoming';

COMMENT ON VIEW atos_que_resolvem IS
'Atos que podem fechar uma tarefa, do Renato OU de um executor delegado
(tonha_role_contacts). Nasceu do caso #999562: a Andressa disse "Paguei hoje" no
WA e a task ficou aberta, porque o cruzamento so olhava Renato x contraparte.
`declara_execucao` e SINAL de leitura, nao filtro — regex de verbo acerta ~15% e
confunde "paguei" com "vou transferir". SOMENTE LEITURA.';

GRANT SELECT ON atos_que_resolvem TO cos_agent_ro;
