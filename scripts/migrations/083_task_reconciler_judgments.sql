-- 083 — o marcador do ÚLTIMO julgamento do reconciler (30/09/26).
--
-- MEDIDO em prod hoje: a run diária do `task-reconciler` faz **197 chamadas de
-- LLM**, e **100 delas (51%) julgam um lote de mensagens que não mudou desde a
-- run anterior** — mesma task, mesmas mensagens, mesmo enunciado, resposta
-- necessariamente igual. A causa está em `task_reconciler.py`: o lote vem de
-- `_fetch_messages_since(scope, task["data_criacao"])` — a data de CRIAÇÃO da
-- task, nunca o último julgamento. Mediana de idade das julgadas: 27 dias; a
-- mais velha, 165. A curva 151→193/dia que o board registrou segue o TAMANHO DA
-- FILA, não atividade nova: com a fila abrindo 3,8× mais do que fecha, cresce
-- sem teto por construção.
--
-- Por que uma tabela e não uma coluna em `tasks`: o marcador é de um consumidor
-- só, tem ciclo de vida próprio (morre com a task, não com o ciclo dela) e
-- carrega CONTAGEM — `judged_count` é o que responde "quantas vezes esta task
-- já foi re-julgada", pergunta que hoje não tem resposta em lugar nenhum
-- (`tonia_llm_usage.metadata` vem `{}` no `judge`, então o medidor de custo sabe
-- QUANTO se gastou e não sabe COM O QUÊ).
--
-- Por que o hash do PROMPT INTEIRO e não um timestamp de "última mensagem":
-- timestamp erra em três casos reais. (a) A transcrição de áudio e o OCR de
-- imagem chegam DEPOIS, por outro cano (`wa_attachments.extracted_text`, que o
-- `texto_efetivo_sql` costura na leitura): a mensagem é antiga, o texto é novo, e
-- o julgamento tem de acontecer de novo. (b) A CoS edita o título/descrição da
-- task — mesmo lote, outro enunciado, outra pergunta. (c) O próprio texto do
-- prompt muda num deploy, e aí TODO marcador tem de ser invalidado de uma vez.
-- O hash do prompt cobre os três sem lista de exceções a manter: a pergunta é
-- "a entrada do modelo é byte-a-byte a mesma?", e é ela que decide.
--
-- ON DELETE CASCADE: o marcador é dado DERIVADO, sem valor fora da task. Apagar
-- a task apaga o marcador e não alcança mais nada — não amplia o alcance de
-- nenhum delete existente (ver feedback_cascade_chain_warning: o risco lá é
-- cadeia que chega em dado original, não em carimbo de leitura).

BEGIN;

CREATE TABLE IF NOT EXISTS task_reconciler_judgments (
    task_id         INTEGER PRIMARY KEY REFERENCES public.tasks(id) ON DELETE CASCADE,
    -- SHA-256 do prompt exato que foi ao modelo. Igual ⇒ a resposta seria igual.
    prompt_hash     TEXT      NOT NULL,
    last_judged_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    -- Quantas chamadas de LLM esta task já custou, e quantas foram poupadas pelo
    -- skip. O par é a régua do conserto: se `skipped_count` não subir, o skip não
    -- está pegando, e um conserto que não se mede certifica a si mesmo.
    judged_count    INTEGER   NOT NULL DEFAULT 0,
    skipped_count   INTEGER   NOT NULL DEFAULT 0,
    -- Último veredito, pra auditar sem reabrir o prompt.
    last_done       BOOLEAN,
    last_confidence NUMERIC(4,3),
    last_reason     TEXT,
    criado_em       TIMESTAMP NOT NULL DEFAULT NOW()
);

-- "Quem não é julgado há mais tempo" — a query da auditoria e do próximo
-- diagnóstico; sem índice ela varre a tabela inteira.
CREATE INDEX IF NOT EXISTS idx_trj_last_judged ON task_reconciler_judgments (last_judged_at DESC);

COMMIT;
