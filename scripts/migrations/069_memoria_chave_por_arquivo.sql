-- 069 — a identidade de uma memória é o ARQUIVO, não o título
--
-- POR QUE (medido em 08/08/26, sessão dedicada à #999768).
--
-- O `/api/system-memories/sync-file` faz UPSERT com `ON CONFLICT (titulo)
-- WHERE fonte='claude_code_migration'`. O título vem do `name:` do frontmatter
-- — que é prosa, e prosa é reescrita. Quando alguém renomeia o `name:`, o
-- conflito não bate: nasce uma linha NOVA e a antiga fica no banco para sempre,
-- embeddada e retornável pela busca. O arquivo tem um dono; a memória dele tem
-- dois registros, e o velho nunca mais é atualizado.
--
-- Cinco pares assim hoje (id velho → id vivo):
--   349  → 1190   project_cos_status.md          board executivo de 30/07
--   350  → 1148   project_dev_backlog.md         **194.727 caracteres** — é o
--                                                board que a partição de 30/07
--                                                tirou de circulação por ser
--                                                grande demais. Saiu do arquivo,
--                                                nunca saiu do banco.
--   377  →  433   project_ienaga_prep_semana_07jul.md  "pauta da semana" ×
--                                                "reunião feita, reposicionado"
--   268  →  360   project_vallen_aptus_contract_review.md  "revisão do contrato"
--                                                × "renovado 90d" — o velho
--                                                CONTRADIZ o novo
--   272  →  358   project_vallen_team.md         equipe de junho × de agosto
--
-- Não é risco teórico: a busca por "equipe e papéis da Vallen Clinic" devolve o
-- registro de JUNHO à frente do de agosto. O recall entrega estado velho como
-- se fosse atual — exatamente o que [[feedback_anti_hallucination]] proíbe.
--
-- A chave estável JÁ EXISTE e ninguém usa: o endpoint grava `tags = ["<arquivo>.md"]`
-- em 100% dos 274 registros. Este migration promove essa coluna a identidade.
--
-- REBAIXA, NÃO APAGA. O registro velho troca de `fonte` e perde o embedding:
-- sai da busca (o índice parcial e o recall filtram por fonte) e continua no
-- banco, legível, reversível com um UPDATE. Aprender pelo uso, não demolir.

-- 1) Rebaixa o registro mais ANTIGO de cada arquivo duplicado. Genérico de
--    propósito: vale para qualquer par que exista no momento da aplicação, não
--    só para os 5 acima — o índice do passo 2 não sobe se sobrar um.
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY tags->>0
               ORDER BY atualizado_em DESC NULLS LAST, id DESC
           ) AS rn
    FROM system_memories
    WHERE fonte = 'claude_code_migration'
      AND tags->>0 IS NOT NULL
)
UPDATE system_memories m
   SET fonte = 'claude_code_migration_superseded',
       embedding = NULL
  FROM ranked r
 WHERE m.id = r.id
   AND r.rn > 1;

-- 2) A identidade passa a ser o arquivo. Com este índice, renomear o `name:`
--    do frontmatter atualiza o registro em vez de fabricar um irmão.
CREATE UNIQUE INDEX IF NOT EXISTS system_memories_arquivo_uk
    ON system_memories ((tags->>0))
    WHERE fonte = 'claude_code_migration';

COMMENT ON INDEX system_memories_arquivo_uk IS
'Identidade de memória sincronizada do Claude Code = o ARQUIVO (tags->>0), não o
titulo. Criado em 08/08/26: o UPSERT conflitava por titulo, e como o titulo vem
do `name:` do frontmatter (prosa reescrita), renomear fabricava um registro novo
e deixava o velho vivo na busca — 5 pares, um deles o board dev de 194 KB de
30/07. O titulo continua gravado e buscavel; so nao manda mais em quem e quem.';
