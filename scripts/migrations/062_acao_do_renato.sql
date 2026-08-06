-- 062 — acao_do_renato: a fonte unica do "ele ja fez isso?"
--
-- POR QUE (pedido do Renato, 06/08/26, prioridade ALTA na fila da CoS).
-- O sistema so sabe o que a CAMADA cria: draft, task, evento, proposta. E cego
-- ao que o RENATO faz direto — responde no WhatsApp, manda e-mail, aceita
-- convite, paga, assina, parabeniza. O resultado e uma classe inteira de
-- defeito: a camada propoe o que ele ja fez.
--
-- Foram CINCO falhas numa sessao so (06/08), e todas tinham evidencia no banco:
--   · Michele — ele ja tinha respondido "pode mandar"     → messages outgoing
--   · Orestes — "Agendado p/ amanha" no WA; eu li so o e-mail → messages outgoing
--   · Marson  — parabens ja enviados as 15:07             → messages outgoing
--   · Phisalia — convite ja aceito                        → RSVP no calendar
--   · itens do check-G ja engajados                       → messages outgoing
--
-- Nenhuma delas exigia dado novo. Exigia UM lugar pra perguntar. Quatro sao
-- outbound e uma e RSVP — e o RSVP nao aparecia em consulta nenhuma da camada.
--
-- O QUE ELA E: leitura, nao regra. Ela responde "o que ele fez, por qual canal,
-- quando, com que evidencia" — quem julga se aquilo CUMPRE o portao e quem
-- consome. Fachada que decide vira regra escondida.
--
-- CONSUMIDORES: o agente CoS (regra 1 do prompt: "se o Renato ja executou, o
-- portao esta cumprido"), os checks F e G da /cos, e qualquer proposta antes de
-- ser oferecida.

CREATE OR REPLACE VIEW acao_do_renato AS
    -- DM: WhatsApp e e-mail moram na MESMA tabela, separados por conversations.canal.
    -- Filtrar por canal='whatsapp' aqui foi o que fabricou "sem resposta" falso
    -- por meses — o e-mail sempre esteve neste cano.
    SELECT
        'messages'::text                      AS origem_tabela,
        m.id                                  AS origem_id,
        cv.canal                              AS canal,
        m.contact_id                          AS pessoa_id,
        CASE WHEN cv.canal = 'email' THEN 'e-mail enviado'
             ELSE 'mensagem enviada' END      AS o_que,
        left(m.conteudo, 240)                 AS evidencia,
        m.external_id::text                   AS ref,
        COALESCE(m.enviado_em, m.recebido_em, m.criado_em) AS quando
    FROM messages m
    JOIN conversations cv ON cv.id = m.conversation_id
    WHERE m.direcao = 'outgoing'

    UNION ALL

    -- Grupo: ele responde no grupo e a DM fica muda. Cobrar por DM o que foi
    -- respondido no grupo ja aconteceu — ver feedback_cross_group_before_send.
    SELECT
        'group_messages', g.id, 'whatsapp_grupo', g.contact_id,
        'mensagem no grupo', left(g.content, 240), g.group_jid,
        COALESCE(g.timestamp, g.criado_em)
    FROM group_messages g
    WHERE g.from_me

    UNION ALL

    -- RSVP: aceitar convite E responder. Nenhuma consulta da camada olhava isto
    -- — foi o caso Phisalia (06/08), em que a proposta pedia o que ja estava
    -- aceito na agenda. `self` marca a linha do proprio Renato entre os
    -- convidados; sem esse filtro, o "accepted" de OUTRA pessoa viraria ato dele.
    SELECT
        'calendar_events', c.id, 'agenda', c.contact_id,
        'convite ' || (a->>'responseStatus'),
        left(COALESCE(c.summary, '(sem titulo)'), 240),   -- `titulo` e da copilot.calendar_events, nao desta
        c.google_event_id,
        COALESCE(c.atualizado_em, c.criado_em)
    FROM calendar_events c
    CROSS JOIN LATERAL jsonb_array_elements(COALESCE(c.attendees, '[]'::jsonb)) a
    WHERE (a->>'self')::boolean IS TRUE
      AND a->>'responseStatus' IN ('accepted', 'declined', 'tentative')

    UNION ALL

    -- Draft que virou envio (061). Hoje a tabela esta VAZIA — o loop existe e
    -- ainda nao teve trafego. Entra na view pra que, quando tiver, ninguem
    -- precise lembrar de somar mais uma fonte.
    SELECT
        'email_drafts', d.id, 'email', d.contact_id,
        'e-mail enviado (era rascunho da camada)',
        left(COALESCE(d.subject, '(sem assunto)'), 240),
        d.thread_id,
        COALESCE(d.sent_at, d.criado_em)
    FROM email_drafts d
    WHERE d.status = 'sent';

COMMENT ON VIEW acao_do_renato IS
'O que o RENATO fez direto, por qualquer canal (06/08/26). Existe porque o
sistema so enxergava o que a camada cria e propunha o que ele ja tinha feito —
5 falhas medidas em 06/08. Responde "o que, quando, com que evidencia"; NAO
decide se o portao esta cumprido. SOMENTE LEITURA.';

GRANT SELECT ON acao_do_renato TO cos_agent_ro;
