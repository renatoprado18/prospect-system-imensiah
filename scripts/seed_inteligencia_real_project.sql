-- Seed do projeto "Inteligência Real" (idempotente)
--
-- Rodar uma vez contra produção (Neon) ou contra qualquer banco que precise.
-- Já está aplicado no banco local. Se rodar 2x não duplica.
--
-- Uso (Neon via psql):
--   psql "$DATABASE_URL" -f scripts/seed_inteligencia_real_project.sql

DO $$
DECLARE
    pid INT;
    m1 INT; m2 INT; m3 INT; m4 INT; m5 INT;
BEGIN
    -- Idempotência: se já existe, pula
    SELECT id INTO pid FROM projects WHERE nome = 'Inteligência Real' LIMIT 1;
    IF pid IS NOT NULL THEN
        RAISE NOTICE 'Project "Inteligência Real" já existe (id=%). Skipping.', pid;
        RETURN;
    END IF;

    INSERT INTO projects (nome, descricao, tipo, status, prioridade, cor, icone, tags)
    VALUES (
        'Inteligência Real',
        'Transformar o INTEL num colaborador único que olha para tudo (Editorial, Relacionamento, Conselho, Tarefas, Mensagens, Emails), age sozinho no que não precisa de decisão e entrega digest matinal + debriefing 19h. Visão: assistente como pessoa, não sistema.',
        'interno', 'ativo', 1, '#6366f1', 'cpu',
        '["meta", "agent", "intelligence"]'::jsonb
    ) RETURNING id INTO pid;

    INSERT INTO project_milestones (project_id, titulo, descricao, ordem, status, data_conclusao) VALUES
    (pid, 'P1 — Audit Log de ações autônomas', 'Tabela agent_actions, service log_action, wiring em auto_publisher/task_auto_resolver/smart_message_processor, página /agente com undo, debriefing 19h, briefing matinal aumentado.', 1, 'concluido', CURRENT_DATE) RETURNING id INTO m1;

    INSERT INTO project_milestones (project_id, titulo, descricao, ordem, status) VALUES
    (pid, 'P2 — Snapshot context expandido no bot', 'Antes de cada turno do intel_bot, montar snapshot rico: tarefas vencendo, agenda do dia, contatos esfriando, estado editorial, propostas pendentes, emails não lidos, mensagens recentes. Bot entra na conversa sabendo de tudo.', 2, 'pendente') RETURNING id INTO m2;

    INSERT INTO project_milestones (project_id, titulo, descricao, ordem, status) VALUES
    (pid, 'P3 — Autonomia com policy explícita', 'Documentar tabela Auto/Notifica/Pergunta. Auditar ações existentes contra a tabela. Adicionar as ações Auto que faltam (linkedin_enrich, raci_status, métricas editoriais, categorização WA).', 3, 'pendente') RETURNING id INTO m3;

    INSERT INTO project_milestones (project_id, titulo, descricao, ordem, status) VALUES
    (pid, 'P4 — Proatividade do bot', 'Bot inicia mensagem por iniciativa: pós-reunião ("como foi?"), pico de atividade em grupo, decay de relacionamento, oportunidades cruzadas. Não só reativo a mensagens do Renato.', 4, 'pendente') RETURNING id INTO m4;

    INSERT INTO project_milestones (project_id, titulo, descricao, ordem, status) VALUES
    (pid, 'P5 — Memória de estilo', 'Tabela leve registrando o que o usuário aceita, edita, ignora. Realimenta o system prompt do bot pra aprender preferências (tom, formato, cadência).', 5, 'pendente') RETURNING id INTO m5;

    -- P1 concluídas (registro do que foi feito)
    INSERT INTO tasks (titulo, descricao, project_id, status, data_conclusao, contexto, prioridade, source_table, source_id, tags) VALUES
    ('Criar tabela agent_actions', 'Schema com action_type, category, scope_ref, undo_hint, payload. Migration via init_db.', pid, 'completed', NOW(), 'professional', 3, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Criar service agent_actions.py', 'log_action, list_actions, summarize_for_digest, format_digest_section, mark_undone.', pid, 'completed', NOW(), 'professional', 3, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Wirear ações autônomas existentes ao log', 'auto_publisher, task_auto_resolver, smart_message_processor logam.', pid, 'completed', NOW(), 'professional', 3, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Aumentar briefing matinal 7h', 'Seção "Fiz por você (24h)" puxando agent_actions.', pid, 'completed', NOW(), 'professional', 3, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Criar cron debriefing 19h SP', 'Endpoint /api/cron/daily-evening-debriefing com pendências do dia + amanhã + ações autônomas.', pid, 'completed', NOW(), 'professional', 3, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Página /agente com listagem e undo', 'Summary cards, filtros (período + categoria), botão Desfazer.', pid, 'completed', NOW(), 'professional', 3, 'project_milestones', m1, '["P1"]'::jsonb);

    -- P1 pendentes
    INSERT INTO tasks (titulo, descricao, project_id, status, contexto, prioridade, source_table, source_id, tags) VALUES
    ('Wirear linkedin_enrich ao agent_actions', 'Cron de enriquecimento LinkedIn diário deve logar contact_enriched.', pid, 'pending', 'professional', 4, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Wirear editorial_metrics_collected', 'Quando métricas de post são coletadas automaticamente, logar.', pid, 'pending', 'professional', 4, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Wirear raci_status_updated', 'Quando RACI é atualizado a partir de resposta em grupo WA, logar.', pid, 'pending', 'professional', 4, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Wirear whatsapp_categorized', 'Quando smart_message_processor vincula mensagem a projeto, logar.', pid, 'pending', 'professional', 5, 'project_milestones', m1, '["P1"]'::jsonb),
    ('Avaliar audit log após 1 semana de uso', 'Validar que digest matinal e debriefing 19h trazem valor real. Ajustar formato com base em feedback.', pid, 'pending', 'professional', 4, 'project_milestones', m1, '["P1", "review"]'::jsonb);

    -- P2
    INSERT INTO tasks (titulo, descricao, project_id, status, contexto, prioridade, source_table, source_id, tags) VALUES
    ('Desenhar shape do snapshot context', 'Definir o que entra no system prompt: tarefas, agenda, editorial, contatos atenção, propostas, emails não lidos, agent_actions recentes. Limite de tokens.', pid, 'pending', 'professional', 2, 'project_milestones', m2, '["P2"]'::jsonb),
    ('Implementar build_context_snapshot()', 'Função que retorna dict estruturado com todo o estado relevante. Usado em intel_bot antes de cada turno.', pid, 'pending', 'professional', 2, 'project_milestones', m2, '["P2"]'::jsonb),
    ('Injetar snapshot no system prompt do intel_bot', 'Cada turno passa snapshot ao Claude. Cache de prompt para reduzir custo.', pid, 'pending', 'professional', 2, 'project_milestones', m2, '["P2"]'::jsonb),
    ('Validar quality bump em conversas reais', 'Conversar com bot pré e pós snapshot. Comparar respostas. Ajustar prompt.', pid, 'pending', 'professional', 3, 'project_milestones', m2, '["P2"]'::jsonb);

    -- P3
    INSERT INTO tasks (titulo, descricao, project_id, status, contexto, prioridade, source_table, source_id, tags) VALUES
    ('Documentar policy de autonomia (Auto/Notifica/Pergunta)', 'Tabela explícita por categoria. Vai em docs/AUTONOMY_POLICY.md e referenciada no system prompt.', pid, 'pending', 'professional', 3, 'project_milestones', m3, '["P3"]'::jsonb),
    ('Auditar ações existentes contra a policy', 'Para cada ação automática, classificar e validar nível. Mover de Pergunta→Auto onde fizer sentido.', pid, 'pending', 'professional', 3, 'project_milestones', m3, '["P3"]'::jsonb),
    ('Adicionar Auto-archive newsletters', 'Detectar emails de newsletter/promoção e arquivar automaticamente. Logar em agent_actions.', pid, 'pending', 'professional', 4, 'project_milestones', m3, '["P3"]'::jsonb),
    ('Adicionar Auto-categorize WA → projeto', 'Mensagem chega → vincular ao projeto correto sem perguntar.', pid, 'pending', 'professional', 4, 'project_milestones', m3, '["P3"]'::jsonb);

    -- P4
    INSERT INTO tasks (titulo, descricao, project_id, status, contexto, prioridade, source_table, source_id, tags) VALUES
    ('Implementar trigger pós-reunião', 'Cron a cada 30min: detecta evento Calendar terminado nos últimos 30min → bot pergunta "como foi?" via WA.', pid, 'pending', 'professional', 3, 'project_milestones', m4, '["P4"]'::jsonb),
    ('Implementar trigger pico de atividade em grupo WA', 'Quando grupo WA com sync ativo passa de N msgs/hora, bot resume + pergunta se Renato quer agir.', pid, 'pending', 'professional', 4, 'project_milestones', m4, '["P4"]'::jsonb),
    ('Implementar trigger decay de relacionamento', 'C1/C2 sem interação > N dias → bot sugere reconexão com gancho personalizado.', pid, 'pending', 'professional', 4, 'project_milestones', m4, '["P4"]'::jsonb),
    ('Implementar trigger oportunidade cruzada', 'A precisa de X, B oferece X → bot sugere apresentar.', pid, 'pending', 'professional', 5, 'project_milestones', m4, '["P4"]'::jsonb);

    -- P5
    INSERT INTO tasks (titulo, descricao, project_id, status, contexto, prioridade, source_table, source_id, tags) VALUES
    ('Criar tabela agent_style_memory', 'Registra: ação proposta, decisão do usuário (aceito/editado/ignorado), edição feita.', pid, 'pending', 'professional', 4, 'project_milestones', m5, '["P5"]'::jsonb),
    ('Hook em ações: log da decisão do usuário', 'Quando user aceita/edita/ignora ação proposta, registrar.', pid, 'pending', 'professional', 4, 'project_milestones', m5, '["P5"]'::jsonb),
    ('Resumo de preferências no system prompt', 'Cada N decisões, resumir padrões de estilo via Claude e injetar no system prompt.', pid, 'pending', 'professional', 5, 'project_milestones', m5, '["P5"]'::jsonb);

    RAISE NOTICE 'Seed completo. Project=%, milestones=[%, %, %, %, %]', pid, m1, m2, m3, m4, m5;
END $$;
