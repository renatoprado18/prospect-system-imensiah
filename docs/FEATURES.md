# INTEL - Mapa Completo de Features

> Consulte ANTES de implementar qualquer feature.
> Última atualização: 2026-04-23
> **548 endpoints | 35 páginas | 62 services | 10 integrações | 66 tabelas**

---

## 1. Dashboard (`/`)
- Morning Briefing (saudação + resumo do dia) → `dashboard.py`
- Stat Cards (contatos, círculos, atenção, briefings) → `GET /api/v1/dashboard`
- **Clipping do Dia** (notícias curadas por IA, 12 fontes RSS) → `GET /api/news/clipping`
  - 👍👎 Feedback (sistema aprende) → `POST /api/news/{id}/feedback`
  - 📝 Criar post LinkedIn (gera texto + cruza artigo) → `POST /api/news/to-post`
  - 📤 Compartilhar com contato (msg personalizada por IA) → `POST /api/share/generate-message`
- LinkedIn Hoje (posts agendados + sugestão de draft) → `GET /api/editorial/dashboard-tasks`
- Ações Sugeridas (dedup, auto-resolve on reply) → `GET /api/action-proposals`
- Quem Contatar Hoje → `GET /api/ai/at-risk`
- Agenda de Hoje (exclui aniversários) → `GET /api/calendar/today`
- Tarefas → `GET /api/tasks`
- Projetos Ativos → `GET /api/projects/active-summary`
- Alertas Veículos → `GET /api/veiculos/alertas`
- Status API Claude (cache 1h) → `GET /api/ai/status`

## 2. Contatos (`/contatos`)
- Lista com busca, filtro letra/contexto, filter=needs_attention
- **Contatos que precisam de atenção** (enriched): `GET /api/contacts/needs-attention` — priority scoring + motivos (fatores), conversation starters (contact_facts), tarefas pendentes, última memória. Cards com borda colorida (urgent/important/attention), health indicator, "Para conversar sobre"
- Detalhe com tabs: Resumo, Timeline, Informações, Relacionamentos, Rodas, Documentos
- **Resumo IA** (enriquecimento com Claude) → `contact_enrichment.py`
- **Análise de conversas inline** (tom, pendências, oportunidades) → `POST /api/contacts/{id}/analyze-conversations`
- **Artigos para reconexão** → `GET /api/contacts/{id}/article-suggestions`
- Briefing pré-reunião → `POST /api/contacts/{id}/briefing`
- Chat IA → `POST /api/contacts/{id}/intelligence/chat`
- Quick actions contextuais → `GET /api/briefing/quick-actions/{id}`
- Merge duplicatas → `contact_dedup.py`
- Exportar CSV/JSON → `export.py`

## 3. Círculos (`/circulos`)
- Dashboard C1-C5 com health médio → `circulos.py`
- Frequências: C1=7d, C2=30d, C3=30d, C4=90d, C5=180d
- Recálculo 2x/dia (cron 5h + 18h)
- Definição manual de círculo

## 4. Grupos Sociais (`/grupos-sociais`)
- Mapeia grupos WhatsApp como círculos sociais
- **Cache** no DB (sync diário, carregamento instantâneo) → `social_groups_cache`
- Cruzamento participantes × contatos INTEL
- Health médio, quem precisa atenção
- Sync manual via botão

## 5. Projetos (`/projetos`)
- CRUD com milestones, tarefas, notas, membros, documentos
- **Smart Update** (IA analisa msgs → sugere completar tarefas/criar novas) → `project_smart_update.py`
  - Memória de pareceres anteriores (contexto persistente)
  - Mensagens agrupadas por conversa
  - Filtra por projeto (ignora outros assuntos)
- **Parecer IA** (análise contextual com docs + grupos WA) → `POST /api/projects/{id}/ai-analysis`
- **Compartilhar parecer** (adapta para destinatário via WA/email) → `POST /api/projects/{id}/adapt-analysis`
- **Ciclo financeiro** (email cobrança → detecta pagamento → auto-ciclo) → `payment_cycle.py`
- **Grupos WA vinculados** (mensagens incluídas na análise) → `project_whatsapp_groups`
- **Download docs dos grupos** → Google Drive → `POST /api/projects/{id}/download-group-docs`
- Link email no milestone (thread Gmail)

## 6. Veículos (`/veiculos`)
- Dashboard com itens de manutenção e status
- **Upload NF via foto** (compressão client-side + OCR Claude Vision + Google Drive) → `POST /api/veiculos/{id}/upload-os`
- Criar/finalizar OS → `veiculos.py`
- Editar itens em OS concluídas → `PUT /api/ordens/{id}/editar-itens`
- Timeline de manutenções
- Alertas de itens vencidos/atenção

## 7. Oficinas (`/oficinas`)
- CRUD com especialidades, serviços, contato → `oficinas.py`

## 8. Editorial Calendar (`/editorial`)
- Pipeline: import → análise IA → adaptação → agendamento → publicação → métricas
- 159 posts (10 publicados, 1 agendado, 148 drafts)
- Análise IA: categoria, público, complexidade, score, gancho LinkedIn
- Bulk schedule, import de artigos
- **Feedback loop IA**: `editorial_pdca.get_top_bottom_examples()` → top/bottom (engagement rate) injetados no prompt de `auto_publisher.select_weekly_posts` e `generate_weekly_briefing`
- **Coleta de métricas em 4 pontos**: 6h, 24h, 72h, 7d via `editorial_metrics_history` (dedup por `dias_apos_publicacao`)
  - Cron: `editorial-metrics-reminder` (11h SP) + `editorial-metrics-reminder-evening` (20h SP)
- **Alerta cadência semanal**: a partir de quinta sem post → ⚠️ no daily-morning-briefing
- **Doc detalhada**: `docs/FEATURE_EDITORIAL.md` + estratégia em `docs/EDITORIAL_STRATEGY.md`

## 9. Hot Takes (`/hot-takes`)
- Gerar de URL, digest, publicar → `hot_takes.py`

## 10. News Hub
- 12 fontes RSS gratuitas (Google News, Valor, Exame, MIT Tech Review, etc.)
- Clipping diário com IA (Haiku, ~$0.01/dia)
- Feedback 👍👎 com aprendizado de preferências
- Sugerir contatos para compartilhar notícia
- Criar post LinkedIn a partir de notícia

## 11. Comunicação
- **WhatsApp**: send, receive, sync, webhook, import .txt, grupos → Evolution API
- **Gmail**: sync, send, threading → Google API
- **Inbox unificado**: `/inbox`
- **Smart Follow-Up**: detecta emails sem resposta, cria FUP automático → `smart_fup.py`
- **Action Proposals**: dedup por contato+tipo, auto-resolve on reply, expire >7d

## 12. Calendário (`/calendario`)
- Sync Google Calendar bidirecional
- Eventos de hoje no dashboard (exclui aniversários)

## 13. Briefings (`/briefings`)
- Contatos que precisam briefing (C1-3, health<50, com interações)
- Geração com Claude (contexto: fatos, mensagens, tasks)

## 14. Campanhas (`/campanhas`)
- CRUD com steps, executor automático no cron

## 15. Integrações
| Integração | Uso | Config |
|------------|-----|--------|
| Google (Calendar, Contacts, Drive, Tasks, Gmail) | Sync bidirecional | OAuth scopes |
| WhatsApp (Evolution API) | Send/receive/groups | EVOLUTION_API_URL/KEY |
| Claude AI (Anthropic) | Briefings, OCR, análises, clipping | ANTHROPIC_API_KEY |
| LinkedIn (LinkdAPI) | Enriquecimento | LINKDAPI_KEY |
| Fathom | Import reuniões | FATHOM_API_KEY |
| ConselhoOS | Sync dados conselhos | CONSELHOOS_DATABASE_URL |

## 16. Cron Jobs (vercel.json)
| Horário | Job | Steps |
|---------|-----|-------|
| 5h diário | daily-sync | Health, Contacts, Calendar, Tasks, Gmail, PaymentCycle, WA, SmartFUP, AI, Campaigns, AutoEnrich, GroupDocs, Avatars, Clipping, SocialGroupsCache (13 steps) |
| 6h diário | sync-whatsapp-history | Histórico WA |
| 18h diário | health-recalc | Health scores |
| 8h segunda | weekly-digest | Digest semanal |
| 4h domingo | cleanup | Expirar propostas, limpar notificações |

## 17. Intel Bot (WhatsApp conversacional via intel-bot)
- Bot WhatsApp dedicado na instancia "intel-bot" (numero 5511915020192)
- Acesso exclusivo do Renato (+5511984153337)
- **Arquitetura**: Claude tool_use com 4 meta-tools — acesso TOTAL ao INTEL + ConselhoOS
- **Memoria conversacional**: tabela `bot_conversations` armazena historico (ultimas 20 msgs por telefone)
- **Modelo**: claude-sonnet-4-20250514 com max_tokens 1000
- **Loop de ferramentas**: ate 5 iteracoes (Claude pode encadear multiplas queries)
- **4 Meta-tools poderosas**:
  - `query_intel(sql)`: query SQL READ-ONLY em QUALQUER tabela do banco INTEL. Claude conhece o schema completo e monta queries livremente. Apenas SELECT permitido, limite 20 linhas, rejeita INSERT/UPDATE/DELETE
  - `query_conselhoos(sql)`: query SQL READ-ONLY no banco ConselhoOS (governanca corporativa). Acesso a empresas, reunioes, atas, transcricoes, RACI, decisoes, pautas e documentos. SELECT only, limite 20 linhas
  - `execute_action(action, params)`: executa acoes no sistema (create_task, complete_task, save_note, save_memory, schedule_meeting, send_whatsapp, enrich_contact, update_contact)
  - `draft_message(contact_id, context)`: gera rascunho personalizado com contexto rico (WhatsApp, email, LinkedIn, fatos, memorias)
- System prompt dinamico: data/hora, projetos ativos, tarefas vencidas, perfil do Renato, schema completo do banco
- Rate limit: ignora emojis e mensagens triviais
- **Notificacoes proativas consolidadas**: TODAS as notificacoes do sistema vao via intel-bot (nao mais via rap-whatsapp "mensagem pra si mesmo")
  - Action proposals: formato conversacional, Renato responde ao bot para agir (sem links)
  - Editorial briefing semanal, alertas do sistema
  - Smart message processor: emails, reunioes, telefones detectados
- Service: `app/services/intel_bot.py`
- Helper: `send_intel_notification(text, phone)` para qualquer servico enviar notificacao
- **Notificacao de Proposals** (`whatsapp_notifications.py`): formato conversacional sem links
  - Renato recebe notificacao no intel-bot com opcoes em texto ("responder", "criar tarefa", "ignorar")
  - Renato responde ao bot e Claude processa a acao em contexto (tool_use)
  - Ref: #ID no footer para rastreabilidade
- **Smart Message Processor**: pos-processamento inteligente de mensagens recebidas → `smart_message_processor.py`
  - Detecta emails (regex + contexto): verifica se ja existe, analisa mensagens anteriores com Claude para atribuir ao contato correto
  - Auto-executa casos claros (email pedido e respondido): atualiza contato e notifica Renato
  - Detecta propostas de reuniao (horarios, datas, dias da semana, "amanha", "semana que vem")
  - Detecta telefones (10-11 digitos, formato BR) e propoe adicionar ao contato
  - Cria action proposals para casos ambiguos com opcoes (Atualizar/Criar novo/Ignorar)
  - Notifica Renato via intel-bot para cada proposta criada (formato conversacional)
  - Chamado automaticamente em `analyze_message_in_background` (evolution_api.py)

## 18. Intelligence Automations
- **Task Auto-Resolver**: `services/task_auto_resolver.py` - auto-completa tarefas pendentes quando acoes correspondentes sao executadas
  - email_sent: match com tarefas "enviar ata", "mandar email" + empresa/contato
  - whatsapp_sent: match com tarefas "mensagem para X", "follow-up X", "cobrar X"
  - meeting_created: match com tarefas "agendar reuniao", "marcar call" + contato
  - contact_updated: match com tarefas "pegar email", "cadastrar email" + contato
  - Clear match (score >= 0.8): auto-completa + notifica Renato
  - Ambiguous match (score 0.4-0.8): pergunta via WhatsApp "Posso marcar como concluida?"
  - Integrado em: intel_bot.py (send_whatsapp, schedule_meeting, update_contact), main.py (send_ata_email)
- **Daily Morning Briefing**: cron 0 10 * * * (7h Brasilia) → `/api/cron/daily-morning-briefing`
  - Resume do dia: tarefas vencidas, tarefas de hoje, reunioes, editorial, propostas pendentes
  - Formato conciso WhatsApp (max 500 chars), so inclui secoes com conteudo
  - Enviado via send_intel_notification
- **Editorial Metrics Reminder**: cron 0 14 * * * (11h Brasilia) → `/api/cron/editorial-metrics-reminder`
  - Detecta posts publicados ~48h atras sem metricas coletadas (linkedin_metricas_em IS NULL)
  - Envia lembrete via WhatsApp com lista de posts e instrucoes para coletar

## 19. ConselhoOS Integration — Ata + RACI Flow
- **Ata Generation**: ConselhoOS → Railway worker (no timeout) → Claude generates detailed ata (8-15K chars) → saves to ConselhoOS DB → notifies WhatsApp
  - Fetches participants from ConselhoOS `pessoas` table for correct names/cargos
  - Frontend shows animated spinner + auto-polls every 10s until ata appears
  - "Regenerar" button to re-generate from transcription
- **DOCX Generation**: ConselhoOS → INTEL `/api/ata/generate-docx` → python-docx professional template → Google Drive upload
  - Flexible markdown-to-DOCX renderer: handles any section structure (not Vallen-specific)
  - Markdown preprocessor converts `# ## ### **bold** | tables |` to DOCX elements
- **RACI Matrix**: Generated from ata decisions/pendências using Claude
  - Assigns R/A/C/I based on participant expertise and cargo
  - Exported as Google Sheet to empresa "RACI" folder (overwrites previous version)
  - `raciSheetDriveId` saved in reunião for email inclusion
- **RACI Weekly Report**: `services/raci_weekly_report.py` — cron Monday 8h SP
  - Sends formatted RACI status to empresa WhatsApp group
  - Groups by status: atrasados (🔴), em andamento (🟡), pendentes (⏳), concluídos (✅)
  - Captures responses ("3 concluído") from group → updates RACI status in ConselhoOS
  - Confirmation sent to group
- **Email Distribution**: Sends ata DOCX + RACI Sheet via Gmail
  - Auto-exports RACI Sheet if missing when sending
  - Recipients selected from empresa `pessoas`
- **Campaign LinkedIn Tasks**: `campaign_executor.py`
  - Fetches real LinkedIn posts via LinkdAPI `/api/v1/posts/all` before creating tasks
  - Tasks include direct post URL + text preview
  - Contacts without LinkedIn or without posts → enrollment paused
  - Batch enrichment endpoint: `POST /api/v1/campaigns/enrich-linkedin-tasks`

## 20. PWA & Mobile
- **Manifest**: `/static/manifest.json` (standalone, portrait, theme #6366f1)
- **Service Worker**: `/static/sw.js` - caches static assets (Bootstrap, icons, fonts) + network-first HTML pages with offline fallback
- **iOS PWA meta tags**: apple-mobile-web-app-capable, apple-mobile-web-app-status-bar-style (black-translucent), apple-touch-icon
- **Viewport**: viewport-fit=cover for iPhone notch/Dynamic Island support
- **Safe areas**: env(safe-area-inset-top) for hamburger button and main content padding
- **Responsive sidebar**: collapses off-screen on mobile (<768px), hamburger toggle (44px touch target), overlay backdrop, auto-close on nav click
- **Touch targets**: min 44px height on all buttons, nav items, tabs
- **Mobile layouts**: all grids (stats, content, tiles, projects) collapse to 1-column on mobile
- **Horizontal scroll prevention**: overflow-x hidden on main content, scrollable tabs with hidden scrollbar
- **overscroll-behavior: none** to prevent pull-to-refresh interference
- **-webkit-text-size-adjust: 100%** to prevent iOS text inflation
