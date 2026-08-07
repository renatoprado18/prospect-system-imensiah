-- 065 — de onde veio o veredito: do Renato ou da inferência.
--
-- POR QUE (07/08/2026). O placar de precisão virou uma fila de 26 itens
-- esperando que o Renato classificasse o desempenho do sistema. A frase dele:
-- "deste jeito parece que eu trabalho para você e não o inverso". A correção é
-- o sistema inferir o veredito do comportamento observado (ato dele depois do
-- portão) e só pedir correção na exceção.
--
-- A COLUNA EXISTE PRA NUNCA MISTURAR OS DOIS. O que ele julgou é GABARITO — é
-- contra isso que a inferência é medida antes de valer. Somar os dois no mesmo
-- balde destruiria a única régua externa que este placar tem, e o número
-- resultante pareceria melhor exatamente onde é mais frágil.
--
-- `motor` continua sendo o motor que ABRIU o portão (api × agente_local);
-- `origem` é quem o JULGOU. Sobrecarregar `motor` teria confundido as duas
-- perguntas para sempre.

ALTER TABLE cos_portao_veredito
    ADD COLUMN IF NOT EXISTS origem TEXT NOT NULL DEFAULT 'humano';

ALTER TABLE cos_portao_veredito
    DROP CONSTRAINT IF EXISTS cos_portao_veredito_origem_check;
ALTER TABLE cos_portao_veredito
    ADD CONSTRAINT cos_portao_veredito_origem_check
    CHECK (origem IN ('humano', 'inferido'));

COMMENT ON COLUMN cos_portao_veredito.origem IS
    'humano = o Renato marcou (gabarito); inferido = deduzido do comportamento '
    'observado depois do portao. Nunca somar os dois ao medir a inferencia.';

-- Confiança da inferência, pra poder exigir piso quando ela virar placar.
ALTER TABLE cos_portao_veredito
    ADD COLUMN IF NOT EXISTS evidencia TEXT;

COMMENT ON COLUMN cos_portao_veredito.evidencia IS
    'Por que este veredito. Obrigatorio no inferido: veredito automatico sem o '
    'porque e numero que ninguem consegue contestar depois.';
