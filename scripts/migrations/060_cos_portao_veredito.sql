-- 060 — onde o veredito do placar da camada CoS passa a morar.
--
-- POR QUE. O PDCA da camada (scripts/cos_agent/pdca.py) sempre foi honesto no
-- proprio docstring: "o que ele NAO mede e a QUALIDADE do julgamento; isso e o
-- placar manual que so o Renato preenche". O problema e que o placar nao tinha
-- onde morar — vivia num .md ou no localStorage do browser e morria ali. Sem
-- persistencia, o PDCA otimizava so o que sabia medir: VOLUME (frente barrada
-- pelo debounce, teto estourado).
--
-- O custo disso ficou medido em 04/08/2026. A calibracao de 03/08 (teto 28->36,
-- debounce 90->60) foi decidida por volume e a precisao caiu de 70% (7/10) para
-- 33% (2/6); 04/08 fechou 0 de 3. Otimizamos o observavel e pioramos o que
-- importa.
--
-- Com esta tabela o ciclo fecha: o Renato julga -> o veredito persiste -> o
-- PDCA le precisao junto com volume -> o ajuste seguinte sabe o que mexeu.
--
-- O que esta tabela NAO e: julgamento automatico. Medido em 04/08, o proxy
-- "houve movimento na frente?" NAO prediz o veredito (certa 5 x errada 2 com
-- movimento; certa 4 x errada 5 sem). Quatro portoes certos nao tiveram acao
-- nenhuma — estavam certos e o Renato so nao agiu ainda. A coluna `veredito` e
-- humana por natureza; `sinal_movimento` fica ao lado apenas como contexto,
-- nunca como fonte.

CREATE TABLE IF NOT EXISTS cos_portao_veredito (
    id              SERIAL PRIMARY KEY,
    run_date        DATE        NOT NULL,
    project_id      INTEGER,
    frente          TEXT        NOT NULL,
    portao          TEXT        NOT NULL,

    -- 'certa'   -> o portao fazia sentido
    -- 'errada'  -> cobrou a toa
    -- 'passou'  -> deixou passar (falso NEGATIVO; ver aviso abaixo)
    veredito        TEXT        NOT NULL
                    CHECK (veredito IN ('certa', 'errada', 'passou')),

    -- Contexto medido na hora do julgamento, NUNCA fonte do veredito:
    -- dias entre o portao e o primeiro movimento na frente (NULL = nenhum).
    latencia_dias   INTEGER,

    -- Parametros vigentes quando o portao foi gerado. Sem isto nao da pra
    -- atribuir uma variacao de precisao a um ajuste — foi exatamente o que
    -- faltou pra saber se a calibracao de 03/08 tinha piorado as coisas.
    debounce_min    INTEGER,
    teto_diario     INTEGER,
    max_por_rodada  INTEGER,

    motor           TEXT,       -- 'agente_local' | 'api' — os dois convivem
    nota            TEXT,       -- observacao livre do Renato
    criado_em       TIMESTAMP   NOT NULL DEFAULT NOW(),

    -- Um veredito por portao. Reenviar o mesmo dia corrige em vez de duplicar.
    UNIQUE (run_date, frente, portao)
);

CREATE INDEX IF NOT EXISTS idx_cos_veredito_data ON cos_portao_veredito (run_date DESC);
CREATE INDEX IF NOT EXISTS idx_cos_veredito_frente ON cos_portao_veredito (frente);

-- ⚠️ VIES DO INSTRUMENTO, registrado aqui porque quem ler a tabela precisa
-- saber: 'passou' (falso negativo) e SUB-CONTADO por construcao. O Renato julga
-- a lista que a camada mostrou; o que ela NAO mostrou nao aparece na tela pra
-- ser julgado. Zero 'passou' NAO significa que a camada nao deixou passar nada.
-- Ler a precisao como "dos portoes abertos, quantos prestavam" — nunca como
-- "a camada viu tudo que importava".
COMMENT ON TABLE cos_portao_veredito IS
'Placar de qualidade da camada CoS. Veredito HUMANO por portao (o proxy automatico nao prediz — medido 04/08/26). Alimenta o bloco 5 do pdca.py. Falso negativo (passou) e sub-contado por construcao.';
