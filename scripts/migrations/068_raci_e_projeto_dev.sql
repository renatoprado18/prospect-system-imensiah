-- 068 — accountable_id nas tasks + projeto "Sistema / Dev"
--
-- POR QUE (conversa com o Renato, 08/08/26, ao provar o ritual de planning).
--
-- 1) `tasks.contact_id` estava fazendo DOIS papéis ao mesmo tempo: "quem faz" e
--    "de quem é o assunto". A prova apareceu na planning: a task
--    "[Dev] Habilitar leitura de Google Sheets" caiu na lista da Andressa —
--    porque a PLANILHA é dela. Ela não vai programar nada. E as tarefas da
--    reunião da Alba ficaram na ficha-EMPRESA "Alba Consultoria" (#75, sem
--    telefone, 2 mensagens), então o Renato não conseguia ver que uma delas era
--    da Larisse: nunca foi da Larisse no sistema.
--
--    `accountable_id` separa: R = contact_id (quem executa), A = accountable_id
--    (quem responde pelo resultado). É o que a planning de segunda usa. C e I
--    ficam de fora de propósito — quase nunca mudam a conversa, e já existem em
--    `raci_itens` para a revisão de conselho, que tem outro ritmo. Duas
--    estruturas para a MESMA pergunta é como se chega a "a planning não bate com
--    o RACI" em três semanas.
--
-- 2) Demanda de código não tinha onde morar. Sem projeto e sem responsável
--    possível (a sessão Dev não é contato), sobrava o `[Dev]` no título — uma
--    gambiarra que inventava um campo inexistente. Com projeto próprio, essas
--    tarefas saem da planning das PESSOAS e passam a ter ritual próprio: a
--    abertura `/dev` lê a fila e promove o que cabe na sessão.
--
--    FILA ≠ BOARD, e a distinção evita duplicar: o projeto é onde qualquer um
--    (Renato, CoS, tonIAH) joga demanda; o `project_dev_backlog.md` continua
--    sendo o board da sessão, com o que está em curso e as prioridades.

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS accountable_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL;

COMMENT ON COLUMN tasks.accountable_id IS
'Quem RESPONDE pelo resultado (o A do RACI). `contact_id` é quem EXECUTA (o R).
Separados em 08/08/26 porque um campo só fazia os dois papéis e a planning
mostrava tarefa de dev na lista da assistente — ela era o ASSUNTO, não a
executora. NULL = o accountable e o executor sao a mesma pessoa.';

CREATE INDEX IF NOT EXISTS tasks_accountable_ix ON tasks (accountable_id)
    WHERE accountable_id IS NOT NULL;

-- Projeto que recebe demanda de código. `tipo='sistema'` para as telas e a
-- planning poderem excluí-lo do ritual das pessoas sem depender do nome.
INSERT INTO projects (nome, descricao, tipo, status, prioridade, icone, cor)
SELECT 'Sistema / Dev',
       'Fila de entrada das demandas de código do INTEL, da tonIAH e das telas. '
       'Qualquer sessão joga aqui; a abertura /dev lê e promove o que cabe. '
       'NÃO entra na planning semanal das pessoas: o executor é a sessão Dev, '
       'não gente — o ritual dela é a própria abertura. O board com o que está '
       'em curso continua sendo project_dev_backlog.md.',
       'sistema', 'ativo', 5, 'code', '#3f6f92'
 WHERE NOT EXISTS (SELECT 1 FROM projects WHERE nome = 'Sistema / Dev');
