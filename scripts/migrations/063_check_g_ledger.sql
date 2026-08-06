-- 063 — check_g_ledger: registrar o que a triagem DESCARTOU
--
-- POR QUE. O check G existe pra achar inbound que devia virar acao e nao virou.
-- Ele mostra ~5 contatos por dia. O que ele NAO mostra some sem deixar linha —
-- e por isso o falso negativo dele e imensuravel. Em 05/08 descobrimos por
-- medicao MANUAL que um regex de vocabulario descartava **83%** do que tinha a
-- bola com o Renato (o pedido do Halfeld sobre o Judo, a consulta trabalhista
-- da Glaucia, o Julio Andery). O regex saiu. A cegueira nao: continuamos sem
-- saber o que os OUTROS gates matam.
--
-- Medido em 06/08, janela de 4 dias:
--     328 mensagens incoming
--     ├─  97 sem contact_id .............. somem: quem nao tem ficha nunca aparece
--     ├─ 127 bola com o terceiro ......... ele ja respondeu por ultimo
--     ├─   1 ruido comercial
--     └─  62 sobrevivem, em 20 contatos ... e ISTO e o que a /cos ve
--   266 mensagens morreram no caminho e nenhuma deixou registro.
--
-- Duas dores, um remedio:
--   (a) IMENSURAVEL — sem o descartado, "alta precisao" e afirmacao sobre o que
--       se ve, nunca sobre o que se perdeu. O ledger guarda a decisao e o gate.
--   (b) SESSION-BOUND — o check so roda quando o Renato abre a /cos, e a janela
--       e de 4 dias. Cinco dias sem abrir e o inbound daqueles dias evapora sem
--       nunca ter sido avaliado. Gravando todo dia, o que nao foi visto continua
--       marcado como nao-visto em vez de sumir.
--
-- O QUE ELE NAO E: nao notifica, nao cria proposta, nao age. Respeita a decisao
-- "ii" do Renato — zero notificacao nova. E memoria, nao alarme.

CREATE TABLE IF NOT EXISTS check_g_ledger (
    id            bigserial PRIMARY KEY,
    message_id    bigint      NOT NULL,          -- messages.id
    external_id   text,                          -- id do WhatsApp, pra cruzar com anexos
    contact_id    integer,                       -- NULL = sem ficha (e isso e o dado)
    telefone      text,                          -- unica identidade quando nao ha ficha
    msg_em        timestamp   NOT NULL,          -- quando a mensagem chegou
    decisao       text        NOT NULL,          -- mostrado | suprimido
    gate          text,                          -- qual gate matou (NULL se mostrado)
    trecho        text,                          -- 160 chars, pra auditar sem abrir o banco todo
    avaliado_em   timestamp   NOT NULL DEFAULT now(),
    -- Preenchido DEPOIS, pela medicao: houve acao sobre esta pessoa depois da
    -- mensagem? Se houve e a decisao foi `suprimido`, o gate errou — e e assim
    -- que o falso negativo deixa de ser opiniao.
    desfecho      text,                          -- agiu | virou_task | nada
    desfecho_em   timestamp,
    CONSTRAINT check_g_ledger_decisao_ck CHECK (decisao IN ('mostrado', 'suprimido')),
    CONSTRAINT check_g_ledger_msg_uk UNIQUE (message_id)
);

CREATE INDEX IF NOT EXISTS idx_check_g_ledger_msg_em   ON check_g_ledger (msg_em DESC);
CREATE INDEX IF NOT EXISTS idx_check_g_ledger_gate     ON check_g_ledger (gate) WHERE decisao = 'suprimido';
CREATE INDEX IF NOT EXISTS idx_check_g_ledger_contato  ON check_g_ledger (contact_id);

COMMENT ON TABLE check_g_ledger IS
'O que a triagem de inbound (check G) viu e o que descartou, com o gate que
descartou. Existe porque o falso negativo do check era imensuravel: em 05/08 um
regex cortava 83% em silencio e so apareceu por medicao a mao. Nao notifica —
e memoria pra medir, nao alarme.';

COMMENT ON COLUMN check_g_ledger.desfecho IS
'Preenchido pela medicao posterior. `suprimido` + desfecho `agiu`/`virou_task`
= o gate errou: descartou algo que exigia acao.';

GRANT SELECT ON check_g_ledger TO cos_agent_ro;
