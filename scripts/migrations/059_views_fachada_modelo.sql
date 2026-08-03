-- 059 — Views de fachada do modelo de dados
--
-- POR QUE. Em 03/08/26 fechamos um modelo de 10 entidades. Consolidar as tabelas
-- fisicamente custaria mais de 2.200 referencias de codigo — meses, em codigo
-- que funciona. Mas o problema que o modelo resolve e de LEITURA: "nao sei o
-- que buscar, quando e onde". Isso uma view resolve em dias.
--
-- Entao estas views sao o CONTRATO DE LEITURA do modelo, disponivel ja. A
-- migracao fisica continua no mapa como destino, vira opcional e incremental, e
-- so acontece onde doer de verdade.
--
-- O QUE ELAS NAO SAO: nao substituem as tabelas, nao sao usadas para escrita, e
-- nao escondem as tabelas de origem — cada linha carrega `origem_tabela` pra
-- que ninguem perca o rastro. Fachada que apaga a procedencia troca um problema
-- por outro.
--
-- QUEM CONSOME PRIMEIRO: o agente CoS (que hoje consulta 4 tabelas na mao pra
-- montar o historico de uma pessoa) e a camada de julgamento.

-- ─────────────────────────────────────────────────────────────────────────────
-- COMUNICACAO — o que foi dito, por qualquer canal.
-- Hoje espalhado em 4 tabelas: messages (22k) · group_messages (10,7k) ·
-- copilot.emails (1,5k) · whatsapp_messages (1,1k).
-- `canal` e `direcao` viram atributo; grupo e cardinalidade (N destinatarios).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW comunicacao AS
    SELECT
        'messages'::text                       AS origem_tabela,
        m.id                                   AS origem_id,
        'whatsapp'::text                       AS canal,
        m.contact_id                           AS pessoa_id,
        m.direcao                              AS direcao,
        false                                  AS coletiva,
        NULL::text                             AS grupo_jid,
        m.conteudo                             AS conteudo,
        NULL::text                             AS assunto,
        m.external_id::text                    AS id_externo,
        COALESCE(m.enviado_em, m.recebido_em, m.criado_em) AS quando
    FROM messages m

    UNION ALL

    SELECT
        'group_messages', g.id, 'whatsapp',
        g.contact_id,
        CASE WHEN g.from_me THEN 'outgoing' ELSE 'incoming' END,
        true,                                   -- coletiva: N destinatarios
        g.group_jid,
        g.content, NULL, g.message_id::text,
        COALESCE(g.timestamp, g.criado_em)
    FROM group_messages g

    UNION ALL

    SELECT
        'copilot.emails', e.id, 'email',
        e.contact_id, e.direction, false, NULL,
        e.content, e.subject, e.message_id::text,
        e.timestamp
    FROM copilot.emails e

    UNION ALL

    SELECT
        'whatsapp_messages', w.id, 'whatsapp',
        w.contact_id, w.direction, false, NULL,
        w.content, NULL, w.message_id::text,
        COALESCE(w.message_date, w.criado_em)
    FROM whatsapp_messages w;

COMMENT ON VIEW comunicacao IS
'Contrato de leitura da entidade Comunicacao (modelo 03/08/26). Une as 4 tabelas
de mensagem. `origem_tabela`+`origem_id` preservam a procedencia. SOMENTE
LEITURA — escrita continua na tabela de origem.';

-- ─────────────────────────────────────────────────────────────────────────────
-- CONHECIMENTO — o que ficou sabido.
-- Hoje em 5 tabelas. `tipo` distingue fato · inferencia · regra · registro,
-- e a distincao e operacional: fato tem 1 origem e morre se a origem estava
-- errada; inferencia tem N e morre quando a premissa muda.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW conhecimento AS
    SELECT
        'contact_facts'::text  AS origem_tabela,
        f.id                   AS origem_id,
        -- fato vindo do agente e INFERENCIA (ele concluiu), nao observacao.
        -- O `fonte` distingue: cos_agent:* raciocinou; o resto observou.
        CASE WHEN f.fonte LIKE 'cos_agent%' THEN 'inferencia' ELSE 'fato' END AS tipo,
        'pessoa'::text         AS sobre,
        f.contact_id           AS sobre_id,
        f.fato                 AS conteudo,
        f.categoria            AS categoria,
        f.confianca::numeric   AS confianca,
        f.fonte                AS metodo,
        f.criado_em            AS quando
    FROM contact_facts f

    UNION ALL

    SELECT
        'project_notes', n.id,
        CASE WHEN n.tipo = 'decisao' THEN 'registro'
             WHEN n.tipo = 'estado_cos' THEN 'inferencia'
             ELSE 'registro' END,
        'frente', n.project_id, n.conteudo, n.tipo, NULL::numeric, n.autor, n.criado_em
    FROM project_notes n

    UNION ALL

    -- system_memories sao REGRAS quando `feedback_*` (governam comportamento) e
    -- registro no resto. O prefixo do titulo e o unico sinal disponivel hoje —
    -- e a razao pela qual `tipo` precisa virar coluna de verdade um dia.
    SELECT
        'system_memories', s.id,
        CASE WHEN s.titulo LIKE 'feedback%' THEN 'regra' ELSE 'registro' END,
        'sistema', NULL::integer, s.conteudo, s.tipo, NULL::numeric, s.fonte, s.criado_em
    FROM system_memories s;

COMMENT ON VIEW conhecimento IS
'Contrato de leitura da entidade Conhecimento (modelo 03/08/26). `tipo`:
fato · inferencia · regra · registro. Derivado de heuristica sobre a origem
enquanto a coluna real nao existe. SOMENTE LEITURA.';

-- ─────────────────────────────────────────────────────────────────────────────
-- EVENTO — algo marcado na agenda. `tipo` distingue reuniao de compromisso.
-- `meetings` esta vazia (ancorada em prospect_id, modelo antigo) mas entra na
-- view pra que reancora-la nao exija mexer em quem consome.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW evento AS
    SELECT
        'calendar_events'::text AS origem_tabela,
        c.id                    AS origem_id,
        'compromisso'::text     AS tipo,
        c.summary               AS titulo,
        c.contact_id            AS pessoa_id,
        NULL::integer           AS frente_id,
        c.start_datetime        AS inicio,
        c.end_datetime          AS fim
    FROM calendar_events c

    UNION ALL

    SELECT
        'meetings', m.id, 'reuniao', m.summary, NULL::integer, NULL::integer,
        m.data_hora, NULL::timestamp
    FROM meetings m;

COMMENT ON VIEW evento IS
'Contrato de leitura da entidade Evento (modelo 03/08/26). Reuniao e um tipo de
evento — o que a distingue e ter pauta e desdobramento. SOMENTE LEITURA.';
