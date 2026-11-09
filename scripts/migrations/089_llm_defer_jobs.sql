-- 089 — com o canário aceso, o julgamento que não acontece não deixa rastro (09/11/2026)
--
-- O PROBLEMA, medido hoje. A conta Anthropic está sem saldo desde 28/10 19:33 BRT
-- (flag `anthropic_credit_down` em `system_memories`, tipo `credit_canary`). São
-- 284 horas em que CADA um dos 11 sítios de `messages.create` falha por si: o
-- `_judge`/`_judge_group` do `task_reconciler` devolve `judge_failed=True`, loga um
-- warning e segue. Nada registra o julgamento que não foi feito. O reconciler
-- responde por 41% dos fechamentos de task — então o efeito de 12 dias de canário
-- aceso é invisível: as tasks simplesmente não fecham, e o atraso aparece no board
-- executivo como "326 tasks atrasadas" sem que nada aponte a causa.
--
-- Esta tabela é a FILA desse trabalho diferido. Com o flag aceso, o passo LLM é
-- enfileirado em vez de perdido; uma sessão `/sys` (assinatura, não API) responde o
-- prompt e a resposta é aplicada pelo caminho NORMAL — `_finaliza_verdict` →
-- `_close_task` — com as guardas intactas (conferência de citação, undo_hint,
-- agent_actions). O que muda é QUEM responde o prompt, não o que se faz com a
-- resposta.
--
-- POR QUE A FILA VEM PRIMEIRO, e não o papel `/sys`: a 068 criou o projeto
-- "Sistema / Dev" como caixa de entrada e nasceu sem leitor nenhum
-- ([[feedback_consumidor_morto_wiring]]). O espelho disso é pior — um leitor sem
-- fila abre, não acha nada e reporta verde, que é exatamente o modo de falha das
-- aberturas de 04/10 (passaram verdes com 30h de LLM parado).
--
-- Reversível: DROP TABLE. Aditiva, nenhum consumidor existente a conhece.

CREATE TABLE IF NOT EXISTS llm_defer_jobs (
    id              SERIAL PRIMARY KEY,
    function        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    model           TEXT NOT NULL,
    max_tokens      INTEGER NOT NULL DEFAULT 1000,
    prompt          TEXT NOT NULL,
    context         JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedup_key       TEXT,
    answer          TEXT,
    result          JSONB,
    error           TEXT,
    claimed_by      TEXT,
    criado_em       TIMESTAMP NOT NULL DEFAULT NOW(),
    claimed_at      TIMESTAMP,
    answered_at     TIMESTAMP
);

COMMENT ON TABLE llm_defer_jobs IS
    'Trabalho LLM diferido enquanto o canário de saldo Anthropic está aceso. '
    'Produtor: services/llm_defer.enqueue (só com o flag anthropic_credit_down '
    'presente). Consumidor: sessão /sys via /api/sys/llm-jobs. A resposta é '
    'aplicada pelo caminho normal da função de origem — a fila carrega o prompt, '
    'nunca a decisão.';

COMMENT ON COLUMN llm_defer_jobs.function IS
    'Rótulo modulo.funcao — o MESMO de tonia_llm_usage.endpoint, pra o custo '
    'evitado ser comparável ao custo que a função teria tido. Decide também qual '
    'aplicador recebe a resposta (registro em llm_defer._APPLIERS).';

COMMENT ON COLUMN llm_defer_jobs.status IS
    'pending (esperando sessão) | claimed (sessão pegou, respondendo) | applied '
    '(resposta aplicada pela função de origem) | failed (aplicação recusou) | '
    'expired (o lote envelheceu antes de alguém responder)';

COMMENT ON COLUMN llm_defer_jobs.context IS
    'O que o aplicador precisa pra aplicar a resposta COM AS MESMAS GUARDAS. No '
    'reconciler: as tasks (id/titulo/contact_id/project_id/rotulo/fingerprint) e '
    '`exibidas` = o texto das mensagens COMO foi ao prompt. Guardar `exibidas` em '
    'vez de re-buscar as mensagens na hora de aplicar é deliberado: a conferência '
    'de citação tem de rodar contra o que o modelo VIU, senão ela afere outra coisa.';

COMMENT ON COLUMN llm_defer_jobs.dedup_key IS
    'SHA-256 do prompt. O reconciler roda todo dia e remontaria o mesmo bloco a '
    'cada passada — sem isto a fila cresceria por run, não por trabalho.';

-- A fila acionável é sempre "pending mais antigo primeiro".
CREATE INDEX IF NOT EXISTS idx_llm_defer_pending
    ON llm_defer_jobs (criado_em)
    WHERE status = 'pending';

-- Dedup só entre os VIVOS: um job já aplicado não deve impedir que a mesma
-- pergunta volte a ser feita amanhã (lote novo, pergunta legítima). Parcial de
-- propósito.
CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_defer_dedup_vivos
    ON llm_defer_jobs (dedup_key)
    WHERE dedup_key IS NOT NULL AND status IN ('pending', 'claimed');

-- Housekeeping (expirar lote velho, soltar claim travado) varre por status+tempo.
CREATE INDEX IF NOT EXISTS idx_llm_defer_housekeeping
    ON llm_defer_jobs (status, criado_em);
