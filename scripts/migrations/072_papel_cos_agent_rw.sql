-- 072 — a trava dura da camada que escreve: papel `cos_agent_rw`
--
-- POR QUE. A 071 criou o livro-razão e o `services/agent_write.py` criou a
-- lista fechada de operações. Faltava a trava: até aqui, "a camada só escreve
-- nestas 5 tabelas" era DISCIPLINA — valia enquanto todo código chamasse pelo
-- portão. Disciplina não sobrevive à primeira pressa. Este papel move a regra
-- para o Postgres, onde ela é negada e não desobedecida.
--
-- O PRINCÍPIO, herdado do `cos_agent_ro` (31/07): a permissão é do BANCO, não
-- do prompt. Um agente instruído a não apagar pode apagar; um papel sem DELETE
-- não pode.
--
-- LEITURA AMPLA, ESCRITA ESTREITA. Ele lê tudo (interpretar um fato exige o
-- contexto inteiro — foi por ler pouco que a camada antiga errou a data da FAAP
-- por 40 caracteres) e escreve só nas 5 tabelas da lista fechada.
--
-- O QUE FICA DE FORA, e é o ponto:
--   · DELETE em qualquer tabela — nunca. Nem nas 5.
--   · contacts / messages / users / system_memories — apagar ficha é
--     irreversível e mensagem é o registro do que aconteceu.
--   · UPDATE em agent_writes — o agente ESCREVE no livro-razão e não pode
--     editá-lo. Quem desfaz é humano, com a credencial do dono. Um agente que
--     edita o próprio registro de auditoria não tem auditoria.
--   · DDL — sem CREATE/DROP/ALTER.
--
-- A SENHA NÃO ESTÁ AQUI. É gerada fora e vai direto para o Railway
-- (`COS_RW_URL`). O CREATE ROLE roda via `psql -v senha=...` no ato da
-- aplicação; este arquivo guarda só o que é público — quem pode o quê.
--
-- Aplicar:
--   psql "$OWNER_URL" -v senha="$(openssl rand -base64 24 | tr -d '/+=')" \
--        -f scripts/migrations/072_papel_cos_agent_rw.sql

-- Idempotente: repetir a migration não derruba o papel nem troca a senha.
--
-- `\gexec` e não `DO $$`: o psql não interpola `:'senha'` dentro do corpo do
-- bloco (ali é literal), então a primeira versão disto falhou com "syntax error
-- at or near ':'" — e falhou ANTES de criar o papel, deixando os 14 GRANTs
-- seguintes gritando "role does not exist". Barulhento, mas sem estado
-- parcial. `format(%L)` escapa a senha.
SELECT format('CREATE ROLE cos_agent_rw LOGIN PASSWORD %L', :'senha')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cos_agent_rw')
\gexec

GRANT CONNECT ON DATABASE neondb TO cos_agent_rw;
GRANT USAGE ON SCHEMA public TO cos_agent_rw;

-- ---- LEITURA: ampla, porque inferir exige contexto ------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cos_agent_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO cos_agent_rw;

-- ---- ESCRITA: só a lista fechada de services/agent_write.py ---------------
GRANT INSERT, UPDATE ON board_hunt_frentes TO cos_agent_rw;  -- criar_frente + atualizar_fase
GRANT INSERT          ON project_members    TO cos_agent_rw;  -- ligar_contato_a_projeto
GRANT INSERT          ON tasks              TO cos_agent_rw;  -- criar_task_followup
GRANT INSERT          ON project_notes      TO cos_agent_rw;  -- registrar_nota_projeto

-- O livro-razão é append-only para o agente: escreve, nunca reescreve.
GRANT INSERT, SELECT  ON agent_writes       TO cos_agent_rw;

-- SERIAL precisa da sequence; sem isto o INSERT falha com "permission denied"
-- num lugar que parece bug de código e é permissão.
-- A de `project_members` faltou na primeira aplicação e só apareceu no teste de
-- controle POSITIVO: as 6 negações passaram, e o INSERT que devia funcionar
-- morreu com "permission denied for sequence". Testar só que o proibido é
-- negado teria certificado um papel que não escreve nada.
GRANT USAGE, SELECT ON SEQUENCE board_hunt_frentes_id_seq TO cos_agent_rw;
GRANT USAGE, SELECT ON SEQUENCE tasks_id_seq              TO cos_agent_rw;
GRANT USAGE, SELECT ON SEQUENCE project_notes_id_seq      TO cos_agent_rw;
GRANT USAGE, SELECT ON SEQUENCE project_members_id_seq    TO cos_agent_rw;
GRANT USAGE, SELECT ON SEQUENCE agent_writes_id_seq       TO cos_agent_rw;

COMMENT ON ROLE cos_agent_rw IS
    'Camada de inteligência com escrita ESTREITA (10/08/26). Lê tudo; escreve só nas 5 tabelas da lista fechada de services/agent_write.py. Sem DELETE, sem DDL, sem UPDATE no próprio livro-razão. Par do cos_agent_ro.';
