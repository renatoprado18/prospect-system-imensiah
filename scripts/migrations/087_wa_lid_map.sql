-- 087 — Mapa LID → telefone (17/10/2026)
--
-- POR QUE EXISTE
-- O WhatsApp migrou identificadores de participante para LID (`<digitos>@lid`),
-- que NAO e telefone e nao casa com ficha nenhuma. Medido em prod em 17/10:
-- **17.968 mensagens de grupo de 692 LIDs distintos em 49 grupos** sem contato,
-- mais 25 DMs que a recuperacao das quedas de 15-16/10 fechou como
-- `unresolvable_jid`. O backlog registrava isso como IRRECUPERAVEL porque o
-- `findMessages` nao devolve `remoteJidAlt` em replay.
--
-- O QUE MUDOU: `GET /group/findGroupInfos` devolve, para CADA participante,
-- o par `{id: "<lid>@lid", phoneNumber: "<e164>@s.whatsapp.net"}`. Medido nos 6
-- grupos com mais LID: **100% dos participantes vem com telefone** (276/276).
-- Logo o mapa EXISTE — so nunca foi colhido nem guardado.
--
-- POR QUE UMA TABELA, e nao resolver na hora
--  1. O par LID↔telefone e DURAVEL: colhido uma vez, serve para sempre.
--  2. Serve a DM tambem. O LID de uma DM irrecuperavel pode aparecer como
--     participante de um grupo — e e so pelo mapa que essa ponte existe.
--  3. Evita bater na Evolution a cada resolucao (a instancia ja deu timeout em
--     `fetchAllGroups`; nao se constroi caminho critico sobre ela).
--  4. Guarda PROCEDENCIA: de onde o par veio e quando. Sem isso, um par errado
--     e indistinguivel de um par certo.
--
-- Aditiva e reversivel: `DROP TABLE wa_lid_map;`. Nenhuma tabela existente muda.

CREATE TABLE IF NOT EXISTS wa_lid_map (
    lid             TEXT PRIMARY KEY,
    -- so digitos, no mesmo formato que `group_messages.sender_phone` grava, pra
    -- casar sem normalizar dos dois lados na hora da consulta
    phone           TEXT NOT NULL,
    -- de onde veio o par. 'group_infos' = /group/findGroupInfos. Campo aberto
    -- de proposito: quando entrar outra fonte (remoteJidAlt ao vivo, por ex.),
    -- a procedencia distingue o par colhido do par inferido.
    fonte           TEXT NOT NULL DEFAULT 'group_infos',
    -- qual grupo revelou o par. Serve pra auditoria: par vindo de um grupo onde
    -- a pessoa nao deveria estar e sinal, nao dado.
    origem_jid      TEXT,
    criado_em       TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em   TIMESTAMP NOT NULL DEFAULT NOW(),

    -- LID e telefone sao ambos so-digitos; vazio nao entra. Sem isto um
    -- `phoneNumber` ausente viraria string vazia e casaria por sufixo com
    -- qualquer coisa.
    CONSTRAINT wa_lid_map_lid_digitos   CHECK (lid   ~ '^[0-9]+$'),
    CONSTRAINT wa_lid_map_phone_digitos CHECK (phone ~ '^[0-9]{10,15}$')
);

-- A consulta quente e por telefone tambem (resolver "quem e este numero" ->
-- quais LIDs ele usa), alem do PK por lid.
CREATE INDEX IF NOT EXISTS idx_wa_lid_map_phone ON wa_lid_map (phone);

COMMENT ON TABLE wa_lid_map IS
  'Mapa LID->telefone colhido de /group/findGroupInfos. Resolve participante de '
  'grupo e DM cujo identificador veio como @lid. Procedencia em fonte/origem_jid.';
