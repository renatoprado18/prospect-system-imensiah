-- 056: RACI genérico no INTEL — matriz por projeto, seja ele conselho ou não
--
-- PEDIDO (Renato, 28/07/2026, task #999703): ver o RACI de QUALQUER projeto
-- no INTEL e imprimir em PDF on-brand num clique, pra compartilhar com quem
-- não tem acesso ao sistema (Piccino, Priscila, conselheiros).
--
-- Hoje o RACI existe em dois lugares e nenhum atende o pedido:
--   - ConselhoOS (`raci_itens`, outro Neon): estruturado, mas SÓ pra empresa
--     de conselho. A reorg das 7 empresas (#47) não é conselho.
--   - INTEL: RACI como TEXTO dentro de uma project_note (a #268 do #47), que
--     ninguém consegue ordenar por prazo, filtrar por status nem imprimir.
--
-- Esta migration cria o lado INTEL. A leitura é que é genérica: a matriz une
-- as DUAS fontes na hora da consulta (ver services/raci_matrix.py) — cada
-- Neon é lido na própria fonte, nada é duplicado nem sincronizado
-- ([[feedback_intel_conselhoos_sync_lacuna]]: são 2 bancos sem sync, e criar
-- um terceiro espelho seria a terceira cópia a divergir).

-- ---------------------------------------------------------------------------
-- (1) A ponte projeto -> empresa, que não existia
-- ---------------------------------------------------------------------------
-- `empresas.conselhoos_empresa_id` já liga empresa INTEL -> empresa
-- ConselhoOS. O elo que faltava era projeto -> empresa: `projects` só tinha
-- `empresa_relacionada`, um TEXT livre (preenchido em 5 de 28 projetos
-- ativos, com grafias que não casam com `empresas.nome_canonico`). Casar por
-- nome aqui seria fuzzy match dentro de uma FK — o tipo de atalho que já
-- custou caro ([[feedback_audit_link_tables_antes_de_bulk]]).
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS empresa_id INTEGER REFERENCES empresas(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_projects_empresa
    ON projects (empresa_id) WHERE empresa_id IS NOT NULL;

-- Seed dos 4 projetos de conselho. São os únicos com `tipo='conselho'` e
-- batem 1:1, por nome idêntico, com as 4 empresas que têm
-- `conselhoos_empresa_id`. O JOIN é por igualdade exata (não ILIKE) e só
-- preenche o que está NULL — rodar de novo não sobrescreve ajuste manual.
UPDATE projects p
   SET empresa_id = e.id
  FROM empresas e
 WHERE p.empresa_id IS NULL
   AND e.conselhoos_empresa_id IS NOT NULL
   AND lower(p.nome) = lower(e.nome_canonico);

-- ---------------------------------------------------------------------------
-- (2) O RACI do lado INTEL
-- ---------------------------------------------------------------------------
-- Colunas espelham o `raci_itens` do ConselhoOS de propósito: a matriz
-- unificada normaliza os dois lados pro MESMO formato, e divergir de nome
-- aqui só criaria trabalho de tradução na leitura.
--
-- Diferenças conscientes:
--   - `id` SERIAL (o ConselhoOS usa uuid) — o INTEL é integer em tudo, e a
--     matriz identifica a linha pelo par (fonte, id), nunca pelo id sozinho;
--   - `prazo` e `area` são NULL-áveis (lá são NOT NULL). Um RACI de projeto
--     não-conselho tem responsabilidade permanente sem data: "coordenação da
--     cadência" não vence. Exigir prazo forçaria data inventada, e data
--     inventada vira falso-atrasado — que foi exatamente o que corroeu a
--     credibilidade do RACI do Vallen em 13/07.
CREATE TABLE IF NOT EXISTS raci_itens (
    id             SERIAL PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    area           TEXT,
    acao           TEXT NOT NULL,
    responsavel_r  TEXT,
    responsavel_a  TEXT,
    responsavel_c  TEXT,
    responsavel_i  TEXT,
    prazo          DATE,
    -- Mesmos 4 valores do enum `raci_status` do ConselhoOS. CHECK em vez de
    -- ENUM: o INTEL não tem nenhum tipo enumerado, e CHECK aceita valor novo
    -- com um ALTER simples, sem o ritual de ALTER TYPE ... ADD VALUE.
    status         TEXT NOT NULL DEFAULT 'pendente'
                   CHECK (status IN ('pendente', 'em_andamento', 'concluido', 'atrasado')),
    notas          TEXT,
    -- De onde a linha veio: 'manual', 'nota:268' (a migração do texto do #47),
    -- 'fathom:<id>'. Sem isto não dá pra desfazer uma importação errada.
    origem         TEXT NOT NULL DEFAULT 'manual',
    -- Link opcional pra task do INTEL. Sem FK de propósito: o item de RACI
    -- sobrevive à task (a task fecha, a responsabilidade continua), e uma FK
    -- com ON DELETE CASCADE apagaria a linha de RACI junto com a task.
    task_id        INTEGER,
    criado_em      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    atualizado_em  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- A consulta da matriz é sempre "itens deste projeto, ordenados por prazo";
-- NULLS LAST no índice espelha a ordenação da view (item sem data vai pro
-- fim, não pro topo).
CREATE INDEX IF NOT EXISTS idx_raci_itens_project
    ON raci_itens (project_id, prazo NULLS LAST);
