# INTEL + ConselhoOS — Backlog

## Concluído ✅

### Bot WhatsApp INTEL (intel-bot)
- [x] Bot conversacional com Claude + tools (query_intel, query_conselhoos, execute_action, draft_message, project_chat)
- [x] Memória de conversa (últimas 20 mensagens)
- [x] Acesso total ao banco INTEL e ConselhoOS via SQL
- [x] Rascunhar mensagens personalizadas com contexto completo
- [x] Criar tarefas, agendar reuniões, salvar notas via WhatsApp
- [x] Número dedicado (+5511915020192)
- [x] Notificações consolidadas no intel-bot
- [x] Anti-alucinação (nunca inventar fatos)
- [x] Transcrição de áudio (Groq Whisper via Railway worker)
- [x] Registro de ligações por áudio (transcreve + salva memória + cria tarefas)
- [x] Suporte a datas absolutas em tarefas (fix prazo_dias)
- [x] Project_chat tool (delega para assistente dedicado do projeto)

### Smart Message Processor
- [x] Detectar emails em mensagens → auto-atualizar contatos
- [x] Detectar propostas de reunião (datas/horários)
- [x] Análise de contexto com Claude
- [x] Notificação WhatsApp via intel-bot para action proposals
- [x] Fix LID format no webhook

### LinkedIn
- [x] 2.100 contatos enriquecidos via LinkdAPI
- [x] 411 mudanças de emprego detectadas
- [x] Auto-enrich C1-C3 no cron diário
- [x] Detecção de mudanças de emprego → action proposal urgente
- [x] OAuth + publicação direta no LinkedIn via API
- [x] Privacy policy page (/privacy)
- [x] Auto-seleção semanal de posts (IA escolhe + agenda + publica)

### Editorial
- [x] Funil editorial: Posts → Impressões → Engajamento → Mensagens → Reuniões
- [x] Briefing semanal automático (cron domingo 18h)
- [x] Clipping diário com IA + feedback learning
- [x] Publicar direto no LinkedIn (botão azul) ou copiar+abrir (fallback)
- [x] Auto-publisher: IA seleciona → usuário aprova → agenda → publica automaticamente

### Projetos
- [x] Edição de projetos (modal completo)
- [x] Pesquisa com IA em tarefas (botão + salva nota)
- [x] Markdown rendering nas notas/timeline
- [x] Smart Update com memória de pareceres
- [x] Condensar pareceres em resumo executivo
- [x] Assistente IA dedicado por projeto (chat com tools)
- [x] Briefing inclui memórias de ligações dos membros
- [x] Download docs grupos WA → Google Drive

### Contatos
- [x] Enriquecer com IA (WhatsApp + email + LinkedIn + fatos)
- [x] Google Tasks sync bidirecional (3x/dia)
- [x] Sugerir artigos para reconexão
- [x] IA inline: analisar conversas
- [x] Registrar ligação (botão na página + bot texto + bot áudio)

### Grupos Sociais
- [x] Extrair participantes + cruzar com contatos INTEL
- [x] Labels do WA Business nos cards
- [x] Toggle sync por grupo
- [x] Sync periódico de mensagens dos grupos marcados (cron)
- [x] Visualização de mensagens no modal
- [x] Cruzamento entre grupos (quem está em 2+)
- [x] Sugestão de introduções baseada em grupos em comum

### Resumo Semanal
- [x] Página /resumo-semanal com dados ao vivo
- [x] Resumo gerado por IA (acionável, não métricas)
- [x] WhatsApp com resumo completo + link
- [x] Cron segundas 8h

### Integração ConselhoOS ↔ INTEL
- [x] Bot consulta ConselhoOS (query_conselhoos tool)
- [x] Pessoas auto-sync → project_members
- [x] RACI ↔ Tasks bidirecional
- [x] Geração de ata DOCX + envio por email
- [x] RACI como Google Sheet

### Intelligence Automations
- [x] Task auto-resolver: auto-completa tarefas quando ações são executadas (email, WhatsApp, reunião, contato)
- [x] Daily morning briefing (cron 7h SP): tarefas vencidas, agenda, editorial, propostas
- [x] Editorial metrics reminder (cron 11h SP): lembra de coletar métricas 48h após publicação
- [x] Contatos que precisam de atenção enriquecidos: motivos, conversation starters (fatos pessoais, profissionais, relacionamento), tarefas pendentes, última memória

### ConselhoOS Ata + RACI
- [x] Ata generation via Railway worker (sem timeout, Claude com prompt detalhado)
- [x] DOCX profissional com renderer markdown flexível (qualquer empresa)
- [x] RACI matrix gerada por IA a partir de ata/transcrição
- [x] RACI export como Google Sheet (pasta "RACI", sobrescreve anterior)
- [x] RACI weekly report via WhatsApp grupo (cron segunda 8h)
- [x] Captura de respostas do grupo para atualizar status RACI
- [x] Grupo Conselho Vallen vinculado ao projeto #24
- [x] Grupo Conselho Alba vinculado ao projeto #26

### Dashboard
- [x] Melhorar card "Quem Contatar Hoje" — filtrar irrelevantes, priorizar por círculo, contexto real, conversation starters
- [x] Filtrar rodas stale (>14 dias com comunicação posterior)
- [x] Filtrar colaboradores diários (health 100% + contato recente)

### Campaigns
- [x] LinkedIn tasks com link direto do post (via LinkdAPI)
- [x] Skip contacts sem LinkedIn em campanhas LinkedIn
- [x] Batch enrichment de tarefas existentes

### Infra
- [x] Mobile responsividade + PWA
- [x] Railway worker para transcrição de áudio + ata generation
- [x] Dashboard otimizado (cache, 2 colunas, stat cards)

---

## Pendente — Baixa Prioridade

### UX
- [ ] Dark mode
- [ ] Notificações push PWA em mobile
- [ ] Atalhos de teclado (além do Cmd+K)

### Integrações
- [ ] Instagram suporte (carrossel, reels)

### Infra
- [ ] Logs de auditoria (quem fez o quê)
