-- 079 — "essa empresa fica como está" também vira registro
--
-- Terceira vez no mesmo dia. A 077 registrou pares de contatos do INTEL que não
-- se fundem; a 078, fichas do Google que ficam separadas; agora a divergência de
-- EMPRESA que o Renato decidiu manter. Em 22/08 ele decidiu 27 casos: 23 trocas
-- e 4 mantidos — #34 (advogado da LBZ **e** dirigente da Federação ASSESPRO-SP),
-- #15081, #1453 e #17910. Sem registro, os quatro reapareceriam na próxima
-- rodada de `empresas.py` pedindo a mesma decisão.
--
-- ⚠️ TRÊS TABELAS COM A MESMA FORMA É SINAL, e fica registrado aqui em vez de
-- virar descoberta de alguém daqui a dois meses: todas guardam "o Renato já
-- decidiu que este par NÃO é o que a heurística acha". A generalização óbvia é
-- uma `cadastro_decisoes (tipo, chave_a, chave_b, motivo)`. Não foi feita agora
-- de propósito — trocar as 077 e 078 no mesmo dia em que foram criadas, com
-- dados já dentro e três scripts lendo, é reescrever o que ainda não provou o
-- formato. Se aparecer uma quarta, vale consolidar as quatro de uma vez.
--
-- POR QUE GUARDA A EMPRESA IGNORADA, e não só o contact_id: a decisão é sobre
-- ESTA divergência. Se a pessoa mudar de emprego de novo amanhã, é caso novo e
-- deve ser perguntado — silenciar o contato pra sempre esconderia a mudança
-- seguinte, que é justamente o que este mecanismo existe pra pegar.

CREATE TABLE IF NOT EXISTS contato_empresa_mantida (
    id           SERIAL PRIMARY KEY,
    contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    empresa_intel     TEXT NOT NULL,  -- o que fica no cadastro
    empresa_ignorada  TEXT NOT NULL,  -- o que o LinkedIn diz e foi recusado
    motivo       TEXT NOT NULL,
    decidido_por TEXT NOT NULL DEFAULT 'renato',
    criado_em    TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT contato_empresa_mantida_par UNIQUE (contact_id, empresa_ignorada)
);

CREATE INDEX IF NOT EXISTS idx_empresa_mantida_contact
    ON contato_empresa_mantida (contact_id);

COMMENT ON TABLE contato_empresa_mantida IS
    'Divergencias empresa INTEL x LinkedIn que o Renato decidiu MANTER (vinculo '
    'paralelo, nao troca de emprego). Lido por scripts/empresas.py. Guarda a '
    'empresa ignorada: mudanca NOVA volta a ser perguntada.';
