-- 071 — a camada deixa de só julgar e passa a ATUALIZAR o conhecimento
--
-- POR QUE (diretriz do Renato, 10/08/26). "Se eu envio uma msg de WA para o
-- Pretola, a inteligência deve interpretar essa mensagem e atualizar o
-- conhecimento (frentes, projetos, tarefas). Se houver dúvida, deve me
-- acionar."
--
-- O CASO QUE PROVOU. Em 10/08 ele mandou "Bora HH? Qua 12/08?" para o Rodrigo
-- Pretola (#5245) e nada aconteceu. A investigação achou três camadas: a
-- mensagem chegou 25min depois da última rodada; o Pretola só está ligado ao
-- projeto #3 (pausado) e a camada julga `status='ativo'`; e não existe frente
-- "Orbiz/Rodrigo" em board_hunt_frentes, apesar do board executivo dizer
-- "Orbiz reativado" desde 07/08. Ou seja: o fato não tinha ONDE pousar. A
-- camada leu tudo e não pôde criar nada — por desenho.
--
-- O QUE TRAVAVA. Em 31/07 a camada nasceu read-only de propósito: o papel
-- `cos_agent_ro` não escreve, negado pelo Postgres e não por instrução no
-- prompt. Isso protege do agente que resolve "consertar" o que não entendeu.
-- A diretriz não revoga essa preocupação — ela exige que a escrita seja
-- ESTREITA, AUDITADA e REVERSÍVEL, em vez de proibida.
--
-- O QUE ESTA MIGRATION FAZ. Só o livro-razão. Toda escrita da camada nasce
-- registrada aqui com o estado anterior, para que desfazer seja uma operação e
-- não uma arqueologia. Sem isto, "a camada pode escrever" significa "ninguém
-- sabe o que ela mudou" — e o dano fica inauditável, exatamente como ficou o
-- do merge de contatos (ver project_auditoria_duplicacao_10_08).
--
-- O QUE ELA NÃO FAZ, de propósito: não cria o papel `cos_agent_rw`. Papel novo
-- precisa de senha, e senha precisa de decisão sobre onde mora. A trava dura no
-- Postgres é o passo seguinte; o comportamento correto vem do módulo
-- `services/agent_write.py`, que é o que esta tabela sustenta.

CREATE TABLE IF NOT EXISTS agent_writes (
    id              SERIAL PRIMARY KEY,

    -- QUEM e QUANDO
    agente          TEXT        NOT NULL,           -- 'cos_agent_local', 'tonia', ...
    run_id          TEXT,                           -- amarra as escritas de uma mesma rodada
    criado_em       TIMESTAMP   NOT NULL DEFAULT now(),

    -- O QUE mudou
    operacao        TEXT        NOT NULL,           -- nome da operação da lista fechada
    tabela          TEXT        NOT NULL,
    registro_id     INTEGER,                        -- NULL enquanto é INSERT não confirmado
    valor_anterior  JSONB,                          -- NULL em INSERT; preenchido em UPDATE
    valor_novo      JSONB       NOT NULL,

    -- POR QUE — a parte que torna a escrita auditável por um humano.
    -- Sem isto o livro-razão vira log de diff: dá para ver o que mudou e não
    -- dá para julgar se devia ter mudado.
    motivo          TEXT        NOT NULL,
    fato_origem     TEXT,                           -- 'messages#27573', 'email#...', ...
    confianca       NUMERIC(3,2),                   -- 0..1 — abaixo do piso vira proposta

    -- REVERSÃO
    desfeito_em     TIMESTAMP,
    desfeito_por    TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_writes_criado  ON agent_writes (criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_agent_writes_run     ON agent_writes (run_id);
CREATE INDEX IF NOT EXISTS idx_agent_writes_alvo    ON agent_writes (tabela, registro_id);
-- Parcial: a pergunta frequente é "o que ainda está valendo", não "o que já foi
-- desfeito". Índice cheio pagaria pelas linhas que ninguém consulta.
CREATE INDEX IF NOT EXISTS idx_agent_writes_vigente ON agent_writes (criado_em DESC)
    WHERE desfeito_em IS NULL;

COMMENT ON TABLE  agent_writes IS
    'Livro-razão de toda escrita feita pela camada de inteligência. Guarda o estado anterior para permitir desfazer. Ver services/agent_write.py.';
COMMENT ON COLUMN agent_writes.motivo IS
    'Por que a camada julgou que devia escrever — em português, para um humano auditar sem ler código.';
COMMENT ON COLUMN agent_writes.confianca IS
    'Certeza da inferência (0..1). Abaixo do piso a camada NÃO escreve: abre proposta para o Renato decidir.';
