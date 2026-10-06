-- 085 — "sem par" precisa de um lugar próprio no banco
--
-- O PROBLEMA, descoberto em 06/10/2026 logo depois de o Renato terminar o passo 3:
-- declarar "este item nunca passou por reunião de conselho" gravava
-- `conselhoos_raci_id = NULL` — que é EXATAMENTE o estado de "ainda não decidi".
-- As duas coisas ficavam indistinguíveis, e por isso:
--
--   · o trabalho de decidir 12 itens não aparecia em lugar nenhum. Reabrir a tela
--     mostraria os mesmos 12 como pendentes, como se nada tivesse sido feito;
--   · a única evidência da decisão era `atualizado_em = hoje`, que qualquer
--     escrita posterior apaga sem aviso;
--   · e ninguém conseguia responder "o passo 3 terminou?", que é justamente o
--     gate da importação.
--
-- Guardar a decisão numa coluna própria é o que transforma uma ausência (NULL)
-- numa AFIRMAÇÃO datada. Ausência não se audita; afirmação sim.
--
-- Reversível: DROP COLUMN. A coluna é aditiva e ninguém depende dela ainda.

ALTER TABLE raci_itens
    ADD COLUMN IF NOT EXISTS sem_par_declarado_em TIMESTAMP;

COMMENT ON COLUMN raci_itens.sem_par_declarado_em IS
    'Quando se declarou que este item de execução nunca passou por reunião de '
    'conselho (não tem gêmeo de ata no ConselhoOS). NULL + conselhoos_raci_id '
    'NULL = ainda não decidido. Preenchido = decidido, e a importação não deve '
    'ofertá-lo de novo.';

-- Um item não pode, ao mesmo tempo, apontar para uma linha de ata e declarar que
-- nunca passou por conselho. A trava vale mais que a boa intenção do chamador:
-- é o banco recusando o estado contraditório, não o código lembrando de checar.
ALTER TABLE raci_itens
    DROP CONSTRAINT IF EXISTS chk_raci_sem_par_exclusivo;
ALTER TABLE raci_itens
    ADD CONSTRAINT chk_raci_sem_par_exclusivo
    CHECK (NOT (conselhoos_raci_id IS NOT NULL AND sem_par_declarado_em IS NOT NULL));

-- BACKFILL da decisão de 06/10, e ele é o motivo de a migration ser urgente.
-- Os 12 itens dos projetos 24 e 26 sem ponteiro foram TODOS tocados hoje, na
-- tela de pareamento: 9 não tinham candidato nenhum e 3 tinham candidato que o
-- Renato leu e recusou (#48, #64, #29 — scores 0.49, 0.57 e 0.47, exatamente os
-- que a similaridade sugeria e a leitura desmentia).
--
-- O critério é estreito de propósito — só os dois projetos que passaram pela
-- tela, só quem não tem ponteiro, só quem foi tocado HOJE. Nenhum outro projeto
-- passou pelo pareamento, e marcar item que ninguém decidiu seria inventar
-- decisão, que é pior do que não ter nenhuma.
UPDATE raci_itens
   SET sem_par_declarado_em = atualizado_em
 WHERE project_id IN (24, 26)
   AND conselhoos_raci_id IS NULL
   AND sem_par_declarado_em IS NULL
   AND atualizado_em::date = DATE '2026-10-06';
