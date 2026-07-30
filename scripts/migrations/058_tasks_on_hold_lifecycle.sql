-- 058: ciclo de vida do `on_hold` — desde quando está parqueada, e por quê
--
-- CONVENÇÃO (Renato, 29/07/2026 — memória feedback_aguardar_terceiro_on_hold):
-- task cujo entregável é "aguardar retorno de terceiro" fica `on_hold` (NÃO
-- conta como vencida) nos primeiros 7 dias de atraso; passados >7 dias sem
-- resposta ela volta a `pending` vencida, porque aí o terceiro sumiu e é hora
-- de o Renato decidir se cobra.
--
-- A convenção exige DOIS gatilhos de saída: (a) chegou resposta do contato →
-- volta a pending; (b) passou a janela → volta a pending. Nenhum dos dois
-- existia: medido em 30/07, a string "on_hold" aparece em 5 lugares em app/
-- inteiro, e nenhum reabre — quem escreve é a Tônia (actuators, snooze) e SQL
-- manual, e quem lê é o raci_weekly_report, pra EXCLUIR. O `task_reconciler`
-- filtra status='pending', então nem enxerga. `on_hold` era um beco: task
-- parqueada não voltava nunca, e foi por isso que a CoS teve de reverter
-- on_hold→pending à mão em 29/07 pra 3 tasks não sumirem do portão.
--
-- Os dois gatilhos precisam de um fato que a tabela não guardava: DESDE QUANDO
-- a task está parqueada. `atualizado_em` não serve como memória disso — o
-- próprio sync o reescreve (em 30/07 às 05:00:23 o daily-sync tocou 4 tasks
-- parqueadas e levou o carimbo com ele). E `tags` não serve de sede: é ARRAY
-- em 100% das 691 linhas (`["editorial","metricas"]`), não objeto — gravar
-- chave ali quebraria todo consumidor que itera a lista.
--
-- Ambas nullable, sem DEFAULT e sem backfill embutido: nenhuma linha existente
-- muda de significado ao aplicar esta migration. O backfill das 12 linhas
-- `on_hold` de hoje é feito em passo separado e explícito (script), porque
-- envolve JULGAMENTO — ver a nota sobre `parqueio_indefinido` abaixo.

-- Quando a task entrou em `on_hold`. É deste carimbo que a janela de espera é
-- contada — e não de `data_vencimento` sozinho, senão re-parquear uma task já
-- muito atrasada a faria reabrir na run seguinte, em loop (a classe de defeito
-- de feedback_livelock_reprocessamento). A janela real é
-- GREATEST(data_vencimento, on_hold_since) + 7 dias, ou seja: re-parquear
-- concede uma janela nova, que é o que o gesto humano quer dizer.
--
-- O sweep também LIMPA esta coluna ao reabrir. Sem isso, a mensagem que causou
-- a reabertura continuaria sendo "posterior ao parqueio" para sempre e a task
-- reabriria toda run mesmo depois de re-parqueada.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS on_hold_since TIMESTAMP;

-- Por que está parqueada. Dois valores, com consequências opostas:
--
--   'espera_terceiro'     — a convenção de 29/07. Bola com o outro. Ganha os
--                           dois gatilhos de saída (resposta / janela de 7d).
--   'parqueio_indefinido' — frente que o próprio Renato/board tirou do radar
--                           sem prazo ("não perdidos, só parqueados; pego
--                           quando a frente reabrir", project_dev_backlog).
--                           NÃO recebe aging: aplicar 7 dias a ela devolveria
--                           ao portão exatamente o que foi silenciado de
--                           propósito.
--
-- A distinção não é derivável do título: das 5 tasks parqueadas pela CoS em
-- 30/07, só UMA ("Alex Bereska — retorno do Piccino") casa um regex de
-- aguardar/retorno; as outras quatro são espera de terceiro com título que não
-- diz isso ("[Jabô] Acompanhar lead alemã", "[Reorg] Provisão contábil"). Por
-- isso a intenção é declarada, não adivinhada — e NULL significa "ainda não
-- classificada", que o sweep resolve carimbando na primeira vez que a vê.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS on_hold_reason TEXT;

-- O sweep varre exclusivamente `status='on_hold'` (12 linhas hoje, contra 691
-- na tabela). Índice parcial pra que ele não pague scan da tabela inteira 1×/dia.
CREATE INDEX IF NOT EXISTS idx_tasks_on_hold
    ON tasks (on_hold_since) WHERE status = 'on_hold';
