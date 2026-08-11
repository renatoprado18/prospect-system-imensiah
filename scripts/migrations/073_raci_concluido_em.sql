-- 073 — o RACI passa a registrar QUANDO o item fechou
--
-- POR QUE (pedido da CoS, 10/08/26). Hoje a matriz devolve `status`,
-- `status_efetivo` e `prazo`, então dá para dizer "52 concluídos" e "4 atrasados
-- hoje" — mas nunca "44 fecharam DENTRO DO PRAZO", porque não existe a data em
-- que o item foi concluído. O Renato está montando o posicionamento dele em
-- cima desse número (frente Gui/FESA), e "no prazo" é a versão forte da frase.
-- Sem esta coluna, daqui a seis meses a série ainda não existirá: é o tipo de
-- dado que só se acumula a partir do dia em que passa a ser gravado.
--
-- POR QUE UM TRIGGER, e não o código gravar. Há NOVE escritores de status
-- espalhados entre os dois bancos — raci_matrix, raci_weekly_report,
-- raci_smart_updates, raci_ata_reconciler, raci_group_shadow, conselhoos_sync,
-- conselhoos_raci_sync, intel_bot e o próprio app do ConselhoOS, que nem é
-- deste repositório. Confiar em nove chamadores para carimbar a data significa
-- que o primeiro esquecido fura a série em silêncio — e séries furadas só se
-- descobrem meses depois, quando alguém tenta usar o número. No trigger, quem
-- escreve não precisa saber que a coluna existe.
--
-- POR QUE DUAS COLUNAS. `concluido_em_fonte` separa o que foi CARIMBADO no ato
-- ('gatilho', exato) do que foi RECONSTRUÍDO no backfill ('relato', que é um
-- limite superior: sabemos que já estava concluído quando foi relatado, não
-- quando fechou). Um número que mistura os dois finge precisão que não tem —
-- e a régua honesta é a que diz sobre quantos itens ela fala.
--
-- O QUE NÃO FAZ, de propósito: não usa `updated_at` como proxy de conclusão.
-- Ele muda em qualquer edição posterior — trocar uma nota em agosto reescreveria
-- a data de fechamento de abril. É o mesmo defeito de `tasks.atualizado_em`, que
-- o board já registra: o daily-sync carimbava a coluna e 75 "movimentações"
-- viraram 4 reais. Item concluído sem data recuperável fica NULL e aparece como
-- descoberto na métrica, que é o honesto.
--
-- REABERTURA ZERA A DATA. Se o item sai de 'concluido', `concluido_em` volta a
-- NULL: para a pergunta "fechou no prazo?", item reaberto não fechou. O que
-- aconteceu fica no `updated_at` e no histórico de quem editou.
--
-- ⚠️ RODA EM DOIS BANCOS. INTEL (us-east) e ConselhoOS (sa-east) têm cada um a
-- sua `raci_itens`, com PK diferente (integer × uuid) e `status` de tipo
-- diferente (text × enum). Por isso todo comparativo usa `status::text`. Aplicar
-- com scripts/aplica_073.py, que roda nos dois e confere.

ALTER TABLE raci_itens ADD COLUMN IF NOT EXISTS concluido_em TIMESTAMP;
ALTER TABLE raci_itens ADD COLUMN IF NOT EXISTS concluido_em_fonte TEXT;

COMMENT ON COLUMN raci_itens.concluido_em IS
  'Quando o item passou a concluido. Carimbado por trigger; NULL se reaberto ou se a data nao e recuperavel.';
COMMENT ON COLUMN raci_itens.concluido_em_fonte IS
  'gatilho = carimbado no ato (exato) | relato = reconstruido do concluido_relatado_em (limite superior).';

CREATE OR REPLACE FUNCTION raci_carimba_conclusao() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Item que já nasce concluído (importação, ata retroativa): carimba,
        -- mas só se quem inseriu não trouxe a data. Respeitar o valor explícito
        -- é o que permite migrar histórico sem o trigger sobrescrever com hoje.
        IF NEW.status::text = 'concluido' AND NEW.concluido_em IS NULL THEN
            NEW.concluido_em := now();
            NEW.concluido_em_fonte := 'gatilho';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status::text = 'concluido' AND OLD.status::text <> 'concluido' THEN
        NEW.concluido_em := now();
        NEW.concluido_em_fonte := 'gatilho';
    ELSIF NEW.status::text <> 'concluido' AND OLD.status::text = 'concluido' THEN
        NEW.concluido_em := NULL;
        NEW.concluido_em_fonte := NULL;
    END IF;
    -- UPDATE que não mexe no status não toca na data: sync que reescreve a
    -- linha inteira toda hora não pode reescrever quando o item fechou.
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_raci_carimba_conclusao ON raci_itens;
CREATE TRIGGER trg_raci_carimba_conclusao
    BEFORE INSERT OR UPDATE ON raci_itens
    FOR EACH ROW EXECUTE FUNCTION raci_carimba_conclusao();

-- BACKFILL do que é recuperável, e só dele.
-- No ConselhoOS existe `concluido_relatado_em`: a data em que o relatório
-- semanal registrou o item como concluído. Não é quando fechou — é quando foi
-- relatado —, então entra como limite superior e marcado como tal. No INTEL a
-- coluna não existe e o bloco simplesmente não encontra nada para atualizar.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'raci_itens' AND column_name = 'concluido_relatado_em') THEN
        UPDATE raci_itens
           SET concluido_em = concluido_relatado_em,
               concluido_em_fonte = 'relato'
         WHERE status::text = 'concluido'
           AND concluido_relatado_em IS NOT NULL
           AND concluido_em IS NULL;
    END IF;
END $$;
