-- 066 — "nada a fazer" precisa sobreviver ao próximo cron.
--
-- POR QUE (07/08/2026). O Renato triou 12 conversas do check-G no cockpit e
-- escreveu "nada a fazer" na maioria. Sem lugar pra guardar isso, todas voltam
-- amanhã — e pedir de novo o que já foi respondido é o jeito mais rápido de
-- fazer alguém parar de responder (a mesma lição do placar, no mesmo dia).
--
-- COLUNA NOVA, E NÃO `desfecho`, POR UM MOTIVO CONCRETO: `medir()` reescreve
-- `desfecho` em TODA rodada do cron (UPDATE ... SET desfecho = CASE ...), então
-- gravar a decisão dele ali seria apagá-la na hora seguinte. `desfecho` é o que
-- o SISTEMA observou; `dispensado_em` é o que o RENATO decidiu. Misturar as duas
-- perguntas na mesma coluna é o que faz uma sobrescrever a outra em silêncio.

ALTER TABLE check_g_ledger
    ADD COLUMN IF NOT EXISTS dispensado_em TIMESTAMP,
    ADD COLUMN IF NOT EXISTS dispensado_nota TEXT;

COMMENT ON COLUMN check_g_ledger.dispensado_em IS
    'Quando o Renato disse que nao ha nada a fazer. NUNCA sobrescrito pelo cron: '
    'decisao humana, nao observacao do sistema.';
COMMENT ON COLUMN check_g_ledger.dispensado_nota IS
    'O porque, nas palavras dele. E o que distingue "ja resolvi" de "e ruido" '
    'quando alguem for medir se o gate acerta.';

CREATE INDEX IF NOT EXISTS idx_check_g_ledger_dispensado
    ON check_g_ledger (contact_id, dispensado_em)
    WHERE dispensado_em IS NOT NULL;
