-- 084 — o PONTEIRO entre a linha de ata do ConselhoOS e o item de execução do
-- INTEL (05/10/2026). Passo 1 de `docs/RACI_FONTE_UNICA_PLANO.md`.
--
-- DECISÃO DO RENATO (26/09, reafirmada e fechada em 05/10): o RACI tem uma fonte
-- só, e a execução mora no INTEL. O que NÃO se move é a ata: 100% dos 122
-- `raci_itens` do ConselhoOS têm `reuniao_id`, e o app dele renderiza o RACI como
-- ABA DA REUNIÃO (`<RaciPanel>`), além de anexar a planilha no e-mail da ata. São
-- dois fatos distintos, não duas cópias de um:
--
--     ata      → "a reunião de 09/09 deliberou X, sob Y"   (ConselhoOS, imutável)
--     execução → "X está aberto, com Y, vence em Z"        (INTEL, fonte única)
--
-- Esta coluna é o elo entre eles. Ela NÃO é cópia: guarda o `id` (uuid) da linha
-- de ata que originou o item de execução.
--
-- POR QUE NÃO REUSAR `raci_itens.task_id`: são elos diferentes e confundi-los foi
-- o que travou o diagnóstico por duas semanas. `task_id` aponta para a TASK de
-- execução do Renato; `intel_task_id` (lado ConselhoOS) aponta para a mesma task,
-- não para a cópia-INTEL do RACI. Medido em 05/10: o `intel_task_id` está 32%
-- preenchido (39 de 122) e, mesmo assim, pareia **ZERO** pares INTEL↔ConselhoOS,
-- porque liga COS→task. O elo que faltava é este, e ele não existia.
--
-- O ÍNDICE ÚNICO PARCIAL É A PARTE QUE IMPORTA. É ele que torna a importação do
-- passo 2 idempotente: com `ON CONFLICT (conselhoos_raci_id)`, uma segunda rodada
-- do sync ATUALIZA em vez de criar a segunda cópia. Sem ele, o conserto da
-- duplicação se torna a sua maior fonte — e `UNIQUE` cheio não serve, porque os
-- itens de execução que NUNCA passaram por conselho têm de poder ficar NULL (são
-- 3 na Vallen: esteticista, Dra. Sayonê, Dra. Camila, e os projetos 28 e 47
-- inteiros, que não têm empresa de conselho nenhuma).
--
-- ⚠️ ORDEM IMPORTA: esta migration é inofensiva sozinha, mas a IMPORTAÇÃO do
-- passo 2 só pode rodar DEPOIS do pareamento manual do passo 3. Rodar antes cria
-- uma segunda cópia de cada um dos ~17 pares da Vallen e ~12 da Alba que já
-- existem. A coluna nasce vazia de propósito.
--
-- REVERSÍVEL: `ALTER TABLE raci_itens DROP COLUMN conselhoos_raci_id;` (derruba o
-- índice junto). Nada depende dela até o passo 2 ser ligado.

ALTER TABLE raci_itens
    ADD COLUMN IF NOT EXISTS conselhoos_raci_id UUID;

COMMENT ON COLUMN raci_itens.conselhoos_raci_id IS
    'uuid do raci_itens do ConselhoOS que originou este item de execucao. '
    'NULL = execucao que nunca passou por reuniao de conselho (estado valido). '
    'NAO confundir com task_id, que aponta pra task. Ver docs/RACI_FONTE_UNICA_PLANO.md';

-- Parcial: só as linhas que TÊM ponteiro entram no índice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_raci_itens_conselhoos_raci_id
    ON raci_itens (conselhoos_raci_id)
    WHERE conselhoos_raci_id IS NOT NULL;
