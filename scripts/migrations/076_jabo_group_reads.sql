-- 076 — o grupo da Governança Jabô passa a atualizar as tasks do #28 sozinho
--
-- DECISÃO DO RENATO, 16/08/2026: "será que os RACI precisam mesmo passar por
-- mim? Se eles são auto-atualizáveis, e todos sabem que é IA, basta ir
-- atualizando. Se o responsável detecta algo errado, ele mesmo manda uma msg
-- para corrigir."
--
-- O QUE OS DADOS DIZEM A FAVOR. De 91 propostas de RACI já geradas a partir de
-- grupos, 1 foi aplicada, 71 descartadas e 19 seguem paradas. Parece condenar a
-- automação — até se ler o MOTIVO dos descartes: quase todos dizem "evidência
-- stale reprocessada" ou "capturada na síntese, ponte manual é o caminho". Elas
-- não foram descartadas por estarem ERRADAS; foram descartadas por chegarem
-- tarde ou duplicadas. O conteúdo era bom. **O gate não filtrou erro, filtrou
-- timing** — e cobrou caro do outro lado: o RACI do Jabô parou em 03/08 e a
-- Andressa reportou 6× até 16/08 sem receber nada de volta.
--
-- POR QUE O JABÔ E NÃO OS CLIENTES. O `raci_group_shadow` diz, no cabeçalho,
-- que "governança de CLIENTE não muda sem gate humano" — e isso continua
-- valendo para Vallen/Alba/Despertar, onde o RACI carrega implicitamente "o
-- conselheiro está acompanhando" e um erro custa constrangimento profissional.
-- No Jabô a contraparte é a executora da própria operação: erro barato,
-- detecção imediata, incentivo direto de corrigir. A exceção é do domínio, não
-- da tecnologia.
--
-- POR QUE UMA TABELA E NÃO UMA COLUNA. `group_messages.raci_processed_at` já é
-- do shadow do ConselhoOS, que MARCA as mensagens do Jabô como processadas
-- justamente por não resolverem empresa (50 de 97 em 16/08). Reusar aquele
-- carimbo como fila daria uma fila permanentemente vazia — o defeito de
-- consumidor morto, de novo. Esta tabela é o registro próprio deste caminho.
--
-- E ELA É O MEDIDOR. Toda mensagem examinada entra aqui, inclusive as que não
-- viram nada — sem o denominador não há taxa, só contagem de sucesso
-- ([[feedback_mecanismo_que_nao_mede_o_denominador]]). `corrige` é o número que
-- decide se isto se estende a clientes: quantas vezes a Andressa precisou vir
-- corrigir o que a máquina escreveu. Sem ele, "funcionou" viraria impressão.

CREATE TABLE IF NOT EXISTS jabo_group_reads (
    id                BIGSERIAL PRIMARY KEY,
    group_message_id  BIGINT NOT NULL,
    examinada_em      TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),

    -- 'aplicada'    → virou UPDATE em public.tasks (via agent_write)
    -- 'duvida'      → confiança < piso, virou action_proposals
    -- 'sem_relacao' → o modelo leu e não achou task correspondente
    -- 'corrige'     → a mensagem CONTESTA algo que a máquina escreveu antes
    -- 'erro'        → falha técnica; fica registrada pra não sumir da taxa
    desfecho          TEXT NOT NULL,

    task_id           BIGINT,
    agent_write_id    BIGINT,
    confianca         NUMERIC(3,2),
    motivo            TEXT,

    -- TRUE quando a mensagem foi lida como correção de escrita anterior. É o
    -- numerador da medição do passo 3 e vive separado do `desfecho` de
    -- propósito: uma mensagem pode corrigir E aplicar no mesmo movimento
    -- ("não foi a Coffee Lab, foi a Garagem — já enviei pra Garagem").
    corrige           BOOLEAN NOT NULL DEFAULT FALSE,

    CONSTRAINT jabo_group_reads_msg_unica UNIQUE (group_message_id)
);

-- A fila é "mensagem do grupo sem linha aqui". O índice serve o anti-join.
CREATE INDEX IF NOT EXISTS idx_jabo_reads_msg ON jabo_group_reads (group_message_id);
CREATE INDEX IF NOT EXISTS idx_jabo_reads_quando ON jabo_group_reads (examinada_em DESC);
-- Parcial: a pergunta da medição é "quantas correções", e ela é rara.
CREATE INDEX IF NOT EXISTS idx_jabo_reads_corrige ON jabo_group_reads (examinada_em DESC)
    WHERE corrige;

COMMENT ON TABLE jabo_group_reads IS
    'Toda mensagem do grupo Governança Jabô examinada pelo caminho que atualiza as tasks do #28 — inclusive as que não viraram nada, porque sem denominador não há taxa. `corrige` mede quantas vezes a executora teve de consertar a máquina (076, 16/08/26).';
