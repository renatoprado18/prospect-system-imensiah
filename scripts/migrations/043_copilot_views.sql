-- 043_copilot_views.sql
-- Contrato de leitura pra Tonia/CoPiloto (decisão de arquitetura 09/07/26).
--
-- A Tonia (repo separado) coabita o mesmo Neon do INTEL. Hoje ela lê tabelas
-- físicas cruas — qualquer ALTER no schema interno quebra ela em silêncio.
-- Este schema `copilot` expõe SOMENTE views com colunas explícitas (nunca
-- SELECT *): se o schema físico mudar, atualizamos a view no MESMO commit e
-- a Tonia não percebe. Consumidores externos leem APENAS copilot.*.
--
-- Contrato completo (coluna → tipo → semântica): docs/COPILOT_CONTRACT.md
-- Regras: colunas nunca são removidas/renomeadas — só adicionadas.
--
-- Idempotente: CREATE OR REPLACE VIEW. Se uma view precisar REMOVER/renomear
-- coluna (violação de contrato — evitar), será preciso DROP VIEW antes.

CREATE SCHEMA IF NOT EXISTS copilot;

COMMENT ON SCHEMA copilot IS 'Contrato de leitura pra consumidores externos (Tonia/CoPiloto). Só views estáveis — nunca ler tabelas físicas.';

-- =============================================================================
-- copilot.signals — sinais dos detectores deterministas (tabela signals, 026)
-- =============================================================================
CREATE OR REPLACE VIEW copilot.signals AS
SELECT
    id,
    tipo,
    urgencia,
    contexto,
    detector,
    status,
    criado_em,
    atualizado_em
FROM public.signals;

COMMENT ON VIEW copilot.signals IS 'Sinais estruturados dos detectores (tipo, urgência 1-10, contexto JSONB, status open/resolved/expired/dismissed).';

-- =============================================================================
-- copilot.tasks — tarefas do INTEL (tabela tasks)
-- Aliases de contrato: due_date ← data_vencimento, criado_em ← data_criacao
-- =============================================================================
CREATE OR REPLACE VIEW copilot.tasks AS
SELECT
    id,
    titulo,
    descricao,
    status,
    prioridade,
    data_vencimento AS due_date,
    data_conclusao  AS completed_at,
    contact_id,
    project_id,
    source_table,
    source_id,
    data_criacao    AS criado_em,
    atualizado_em
FROM public.tasks;

COMMENT ON VIEW copilot.tasks IS 'Tarefas do INTEL (status pending/completed/etc, due_date, vínculos contact/project e origem source_table+source_id).';

-- =============================================================================
-- copilot.contacts — contatos essenciais (tabela contacts)
-- Não expõe colunas internas: scoring breakdown, sync Google, enriquecimento
-- LinkedIn bruto, endereços, dossiês.
-- =============================================================================
CREATE OR REPLACE VIEW copilot.contacts AS
SELECT
    id,
    nome,
    apelido,
    empresa,
    cargo,
    emails,
    telefones,
    circulo,
    tags,
    health_score,
    ultimo_contato,
    resumo_ai,
    criado_em,
    atualizado_em
FROM public.contacts;

COMMENT ON VIEW copilot.contacts IS 'Contatos essenciais: identificação, emails/telefones (JSONB arrays), círculo 1-5, health_score 0-100, resumo AI.';

-- =============================================================================
-- copilot.messages — mensagens unificadas email+WhatsApp (tabela messages)
-- canal vem da conversa (conversations.canal). Aliases: direction ← direcao,
-- content ← conteudo, "timestamp" ← COALESCE(enviado_em, recebido_em, criado_em).
-- =============================================================================
CREATE OR REPLACE VIEW copilot.messages AS
SELECT
    m.id,
    m.conversation_id,
    m.contact_id,
    c.canal,
    m.direcao   AS direction,
    m.conteudo  AS content,
    COALESCE(m.enviado_em, m.recebido_em, m.criado_em) AS "timestamp"
FROM public.messages m
LEFT JOIN public.conversations c ON c.id = m.conversation_id;

COMMENT ON VIEW copilot.messages IS 'Mensagens email+WhatsApp: direction outgoing (Renato enviou) / incoming (contato enviou), canal da conversa, timestamp UTC naive.';

-- =============================================================================
-- copilot.calendar_events — eventos de agenda (tabela calendar_events)
-- Alias: titulo ← summary. ATENÇÃO: horários armazenados em BRT naive
-- (exceção histórica à convenção UTC do resto do banco).
-- =============================================================================
CREATE OR REPLACE VIEW copilot.calendar_events AS
SELECT
    id,
    summary AS titulo,
    start_datetime,
    end_datetime,
    all_day,
    attendees,
    location,
    status,
    contact_id,
    criado_em,
    atualizado_em
FROM public.calendar_events;

COMMENT ON VIEW copilot.calendar_events IS 'Eventos de agenda (Google sync + locais). start/end_datetime em BRT naive (exceção à convenção UTC), attendees JSONB.';

-- =============================================================================
-- copilot.memories — memórias persistentes do sistema (tabela system_memories,
-- migrations 003/006). Aliases: name ← titulo, content ← conteudo,
-- type ← tipo, source ← fonte, created_at/updated_at ← criado_em/atualizado_em.
-- =============================================================================
CREATE OR REPLACE VIEW copilot.memories AS
SELECT
    id,
    titulo   AS name,
    conteudo AS content,
    tipo     AS type,
    tags,
    fonte    AS source,
    embedding,
    criado_em     AS created_at,
    atualizado_em AS updated_at
FROM public.system_memories;

COMMENT ON VIEW copilot.memories IS 'Memórias persistentes do INTEL (decisões, padrões, feedback), com embedding pgvector 1024d (voyage-4-lite) pra busca semântica.';

-- =============================================================================
-- copilot.action_proposals — propostas de ação do INTEL proativo
-- Aliases: tipo ← action_type, payload ← action_params.
-- =============================================================================
CREATE OR REPLACE VIEW copilot.action_proposals AS
SELECT
    id,
    action_type   AS tipo,
    contact_id,
    status,
    confidence,
    urgency,
    title,
    description,
    action_params AS payload,
    criado_em
FROM public.action_proposals;

COMMENT ON VIEW copilot.action_proposals IS 'Propostas de ação do INTEL proativo (status pending/accepted/rejected/executed/expired, confidence 0-1, payload JSONB).';
