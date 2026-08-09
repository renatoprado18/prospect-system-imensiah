-- 070 — telemetria de recall: quais memórias o sistema REALMENTE puxa
--
-- POR QUE (#999768, passo 5). O índice `MEMORY.md` é lido inteiro em toda
-- abertura de sessão e cresce sem freio. Decidir o que sai dele exige saber o
-- que é usado — e hoje não se sabe nada: nenhuma memória registra se alguma vez
-- foi recuperada. Sem isso, "esta pode sair" é opinião, e opinião de máquina
-- sobre prosa já errou hoje (a coluna "sugestão" do inventário marcava SAI no
-- board_hunt, que é frente ATIVA).
--
-- Ordem invertida de propósito: a task pedia julgar primeiro (passo 3) e medir
-- depois (passo 5). Medir primeiro custa quase nada e troca o palpite por
-- evidência — o julgamento espera as semanas que a medição levar.
--
-- O QUE É CONTADO, e a honestidade sobre isso: conta-se aparecer entre os
-- **3 primeiros** resultados de uma busca. Não é "foi lido" — é o proxy mais
-- perto disso que existe sem instrumentar o leitor. Contar TODOS os retornados
-- inflaria o ruído: memória que sempre volta em 5º acumularia contador alto e o
-- auto-aparo passaria a proteger justamente o que não serve.
--
-- ⚠️ São DOIS caminhos de recall, com implementações separadas e ambos apontando
-- pro mesmo Neon: `app/services/system_memory.py` (INTEL: bot + frente_review) e
-- `mcp/db.py` (CoPiloto/Claude.ai). Instrumentar só um mede metade do
-- denominador e devolve um número que parece inteiro. A coluna `via` distingue.

ALTER TABLE system_memories
    ADD COLUMN IF NOT EXISTS recall_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ultimo_recall_em TIMESTAMP,
    ADD COLUMN IF NOT EXISTS ultimo_recall_via TEXT;

COMMENT ON COLUMN system_memories.recall_count IS
'Quantas vezes esta memoria apareceu no TOP 3 de uma busca. Proxy de uso, nao de
leitura: ninguem instrumenta o leitor. Contar todos os retornados inflaria ruido
(o que sempre volta em 5o lugar acumularia contador e seria protegido a toa).
Zerado em 09/08/26 quando a telemetria nasceu — idade do contador se le por
`ultimo_recall_em IS NULL` + a data desta migration, nunca pelo numero sozinho.';

COMMENT ON COLUMN system_memories.ultimo_recall_via IS
'Qual caminho puxou: `intel` (services/system_memory.py — bot, frente_review) ou
`mcp` (mcp/db.py — CoPiloto/Claude.ai). Sao dois consumidores independentes;
sem esta coluna, medir um e concluir sobre o todo.';

-- Ordena a varredura do auto-aparo: nunca-puxada primeiro, mais antiga depois.
CREATE INDEX IF NOT EXISTS system_memories_recall_ix
    ON system_memories (recall_count, ultimo_recall_em NULLS FIRST)
    WHERE fonte = 'claude_code_migration';
