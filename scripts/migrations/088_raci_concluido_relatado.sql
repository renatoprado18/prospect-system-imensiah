-- 088 — o relatório semanal não sabe o que já relatou (27/10/2026)
--
-- O PROBLEMA. `generate_raci_report` filtra os concluídos por
-- `(status != 'concluido' OR concluido_relatado_em IS NULL)` e depois
-- `mark_concluidos_as_reported` carimba a coluna. É o que faz um item fechado
-- aparecer UMA vez na seção "✅ Concluídas" e desaparecer no relatório seguinte.
--
-- Essa coluna só existe no ConselhoOS. Enquanto o relatório lia de lá, ninguém
-- notou; ao mover a leitura para o `raci_itens` do INTEL (a metade que faltou da
-- fonte única de 06/10), o filtro passa a não ter em que se apoiar. Sem ela, os
-- 81 concluídos da Vallen entram como novidade toda segunda-feira — e o
-- `[:5]` do formatador esconderia o defeito em vez de evitá-lo: a lista sairia
-- plausível, com cinco itens fechados há meses apresentados como da semana.
--
-- Reversível: DROP COLUMN. Aditiva, sem default, ninguém depende dela ainda.

ALTER TABLE raci_itens
    ADD COLUMN IF NOT EXISTS concluido_relatado_em TIMESTAMP;

COMMENT ON COLUMN raci_itens.concluido_relatado_em IS
    'Quando este item concluído JÁ SAIU num relatório semanal de RACI. NULL = '
    'fechou e ainda não foi comunicado (entra na seção Concluídas do próximo). '
    'Espelha a coluna homônima do ConselhoOS — ver raci_store.report_rows.';

-- Índice parcial: a consulta do relatório é sempre "concluído E não relatado",
-- e é ela que roda no cron de segunda sobre a tabela inteira.
CREATE INDEX IF NOT EXISTS idx_raci_concluido_nao_relatado
    ON raci_itens (project_id)
    WHERE status = 'concluido' AND concluido_relatado_em IS NULL;

-- ── BACKFILL, e ele é a parte que precisa de critério ───────────────────────
--
-- Deixar tudo NULL faria o primeiro relatório pós-migração anunciar como
-- novidade o que fechou em setembro. Carimbar tudo faria o oposto: o que fechou
-- esta semana nunca seria comunicado. Então o corte é por DATA DE FECHAMENTO,
-- com a janela do próprio relatório (7 dias), e vem em duas partes:
--
-- (a) o que o ConselhoOS JÁ relatou (10 itens, medido em 27/10) NÃO é feito
--     aqui, e o limite é declarado em vez de contornado: uma migration roda
--     contra UM banco e não alcança o outro Neon. Essa parte é do
--     `scripts/aplica_088.py`, que abre as duas conexões e propaga pelo
--     ponteiro `conselhoos_raci_id` da 084. Re-relatar o que o cliente já viu
--     é o erro mais visível que esta migração poderia cometer, então ela roda
--     ANTES de (b) — rodar só este arquivo deixa esses 10 itens para o passo
--     (b) decidir pela data, que é um default seguro mas menos preciso.
--
-- (b) o que fechou há mais de 7 dias: já passou da janela em que o relatório
--     semanal o teria mostrado, então tratá-lo como novidade agora seria
--     inventar movimento. Quem fechou DENTRO da janela fica NULL de propósito —
--     é exatamente o que tem de sair no próximo relatório.
UPDATE raci_itens
   SET concluido_relatado_em = COALESCE(concluido_em, NOW())
 WHERE status = 'concluido'
   AND concluido_relatado_em IS NULL
   AND concluido_em IS NOT NULL
   AND concluido_em < (NOW() - INTERVAL '7 days');
