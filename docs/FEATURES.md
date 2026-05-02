# INTEL - Mapa Completo de Features

> Consulte ANTES de implementar qualquer feature.
> Última atualização: 2026-04-23
> **548 endpoints | 35 páginas | 62 services | 10 integrações | 66 tabelas**

---

## 1. Dashboard (`/`)
- **Principio UX (Fase 2 — 02/05)**: dashboard = inbox de acoes do dia. Statcard so existe se gera acao; quando count=0 fica `.muted` (opacity 0.45). Quando TODOS os 7 cards = 0, banner discreto "🌟 Tudo limpo por hoje" aparece acima do workbench (helper `updateCleanSlateBanner()`). Conteudo de leitura/exploracao mora em paginas dedicadas (ex: `/clipping`).
- Morning Briefing (saudação + resumo do dia) → `dashboard.py`
- Stat Cards (contatos, círculos, atenção, briefings) → `GET /api/v1/dashboard`
- **Drill-down Workbench**: clicar em qualquer um dos 7 statcards (Reuniões / Projetos c/ Atenção / Contatos c/ Atenção / Tarefas Avulsas / Editorial / Clipping / Alertas Veículos) abre painel abaixo dos cards listando os itens por trás do número. Card ativo ganha borda colorida + seta apontando pro painel. Click 2x no mesmo card fecha (toggle). Estado persiste em `localStorage['intel.dashboard.lastDrill']` — refresh reabre último card visitado. Pattern `drillConfig` (dict por statcard com `endpoint`, `extract`, `renderItem`, `fullPage`) — adicionar statcard novo = adicionar 1 entrada. Mobile: workbench oculto (Fase 4 traz FAB + bottom sheet).
- **Statcard Editorial** (NOVO): mostra `count = drafts pra aprovar + posts pra coletar métricas`, breakdown em 2 mini-pills (`✏ N` warning · `📊 N` info). Drill mostra 2 seções: "Para Aprovar" (drafts editorial_posts + hot_takes órfãos via UNION) e "Coletar Métricas" (publicados ≥48h sem `linkedin_metricas_em`). Endpoint: `GET /api/editorial/action-items`. Click em item de "Para Aprovar" abre `/editorial?post=N` ou `/hot-takes`; "Coletar Métricas" abre LinkedIn URL (se houver) ou `/editorial?post=N`.
- **Chat lateral no workbench (Fase 3)**: zona à direita do drill panel com o INTEL chat sempre disponível. Layout `grid 1fr 0.55fr`. Trocar de drill repopula só o painel esquerdo — o chat (mensagens + scroll + input) preserva estado entre switches. Init único no `DOMContentLoaded`, antes do autoload do último drill.
- **Widget reusável `_intel_chat_widget.html`**: o chat virou partial Jinja com CSS escopado em `.intel-chat-widget__*` e JS em IIFE expondo `window.IntelChat = { init(rootSelector, opts), focus(), reset() }`. IDs prefixados `intelChat-`. Altura customizável via CSS var `--intel-chat-height` (default `calc(100vh - 100px)`, dashboard usa 520px). A página `/intel-chat` agora é um wrapper de 16 linhas que `{% include %}` o partial e chama `IntelChat.init('#intelChatRoot')` — comportamento idêntico ao anterior.
- **Quick actions inline (Fase 2)**:
  - Tarefas: botão `[✓]` conclui inline (`PUT /api/projects/tasks/{id}` + fade out + decrementa contador do statcard)
  - Contatos: `[💬 WA]` (link `/whatsapp`) + `[📋]` (abre contato)
  - Reuniões: `[📋]` quando evento tem `contact_id` (abre página do contato)
  - Projetos / Veículos: sem ação extra — row click já basta (Smart Update / Adiar precisariam de modal, ficam pra futuro)
- **`/api/projects/with-attention`** (NOVO): lista projetos ativos com motivos `overdue` (task pendente vencida), `milestone_proximo` (≤7d), `parado` (sem update >14d). Substitui `/api/projects/overdue-count` no statcard pra count e drill ficarem alinhados.
- **`/api/projects/with-attention/detailed`** (NOVO, Fase 4): pré-categoriza ativos em `{atencao: [], proximos: []}` espelhando lógica de `categorizeProject()` da página `/projetos`. Cada projeto vem com shape completo do `list_projects` (tasks_pendentes, tasks_vencidas, marcos_pendentes, proximo_marco, proxima_tarefa) pra render rico. Statcard "Projetos c/ Atenção" agora conta deste endpoint pra alinhar com o drill.
- **Drill rendering rico (Fase 4)**:
  - Reuniões → estilo "Agenda de Hoje" (`.agenda-item`/`.agenda-time`/`.agenda-content`/`.agenda-meta`); empty-state grande com `bi-calendar-check` "Nenhum compromisso para hoje"
  - Projetos → seções coloridas "Precisa de Atenção" (vermelho) + "Próximos 7 Dias" (laranja), cards com border-left por tipo, badges de urgência, próxima tarefa, contadores de tasks/marcos. Lógica de categorização e badge no backend (`/with-attention/detailed`) + JS `drillProjectUrgencyInfo()`. Drop "Em dia"/"Pausados" — só mostra o que precisa de ação.
  - Contatos → endpoint trocado pra `/api/v1/contact-suggestions?limit=15` (mesmo do widget "Quem Contatar Hoje"); render via `drillRenderContactSuggestion()` reusa classes `.contact-today-*` e funções `openWhatsApp(id)` / `markAsContacted(id, rodaId, btn)` já existentes
  - Tarefas / Veículos → mantidos como Fase 1+2
- **`drillConfig` schema expandido**: além de `endpoint`/`extract`/`renderItem`/`fullPage`, agora suporta `renderAll(data)` (pra grouping/sectioning), `extractCount(data)` (pra contar quando `renderAll` substitui `extract`), `emptyIcon` e `emptyText` (custom por statcard). `openDrill` usa `renderAll` se definido, fallback pra `map(renderItem)`.
- **Statcard Clipping (Fase 2 — 02/05)**: 7o statcard, count = `data.clipping.length` do `/api/news/clipping` (ja vem filtrado por interactions hoje). Drill abre a queue interativa 1-por-vez (👍/👎/⏭/📤/📝) — mesmo state global `clippingState`/`clippingAction`/`clippingMakePost`/`clippingShare`/`clippingAdvance` que vivia no widget. Empty state: "Tudo lido por hoje". `fullPage: '/clipping'`.
- **Busca global page-aware**: input no header (`#globalSearch`, atalho Cmd+K) adapta endpoint/placeholder/render por página via `window.pageSearch`. Default = contatos. Override em `extra_js`: `window.pageSearch = { placeholder, endpoint(q), extract(data), renderItem(p) → {href,title,subtitle,icon|avatar}, fallbackUrl(q) }`. Implementado: `/projetos` (busca em nome+descrição+empresa_relacionada).
- **INTEL Chat (`/intel-chat`)**: interface web pra conversar com o mesmo cérebro do intel-bot do WhatsApp. Reutiliza `_build_system_prompt(mode='chat')`, `bot_conversations` (histórico unificado WA+web por phone=RENATO_PHONE), tools (query_intel, execute_action, etc). Mode 'chat' = persona de coach (sem emojis, sem ANOTADO!, sem tabela de DB como resposta, instrui tool-use pra topicos pessoais). Endpoint `POST /api/intel-chat`. UI estilo Claude — chat-empty + autoload + streaming-feel. Pensado pra conversas estratégicas (objetivos, reflexão) que precisam persistir.
- **System memories** (`system_memories` table): memórias persistentes do coach NÃO atreladas a contato — decisões de vida, compromissos consigo, padrões observados, reflexões, sínteses diárias. Tools: `save_system_memory` (titulo, conteudo, tipo, tags) e `search_system_memories` (query). Memórias recentes + última síntese aparecem no `_build_snapshot_block` (system prompt) — bot SEMPRE entra na conversa lembrando.
- **Síntese diária** (`/api/cron/daily-synthesis` @ 22h SP): cron noturno usa Claude pra ler `bot_conversations` das últimas 24h e gerar digest estruturado (temas, decisões, estados, compromissos, padrões, itens pra projeto/task, aberto pra próxima conversa). Salva em `system_memories` com tipo='sintese_diaria'. Próximo dia o bot entra com contexto completo do que foi conversado. Trigger manual: `POST /api/intel-chat/synthesize-now`. Migration `003_system_memories.sql`.
- Ações Sugeridas (dedup, auto-resolve on reply) → `GET /api/action-proposals`
- Projetos Ativos → `GET /api/projects/active-summary`
- Alertas Veículos → `GET /api/veiculos/alertas`
- Status API Claude (cache 1h) → `GET /api/ai/status`
- **Removidos do dashboard (2026-05-02)**: widgets "Quem Contatar Hoje", "Agenda de Hoje", "Tarefas", "LinkedIn Hoje" e o card "Clipping" original. Cada um vive em pagina dedicada ou virou drill+statcard. Content Grid completo eliminado. "Conteudo da Semana" mantido como bloco solo apos workbench (planejamento, nao acao).

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

## 5. Tarefas (`/tarefas-pendentes`)
- Backlog combinado: tarefas diretas (avulsas, sem projeto) + RACI monitoradas → `rap_tarefas_avulsas.html`
- Carrega via `GET /api/projects/all-tasks?status=pending&limit=200`, filtragem client-side por toggle
- Atalho rápido no menu lateral (antes de Projetos)
- **Toggle Diretas | RACI | Todas** (default Diretas):
  - `Diretas` → `!isRaci(t) && !t.project_name` (avulsas não-RACI)
  - `RACI` → `isRaci(t)` (todas RACI, com ou sem projeto — RACI hoje sempre tem `project_id`)
  - `Todas` → `isRaci(t) || !t.project_name` (Diretas + RACI)
  - Detecção RACI: `origem='conselhoos_raci'` ou `conselhoos_raci_id IS NOT NULL`
- **Badge RACI** (pill roxo `#6366f1`) renderizado ao lado do título em todas as listas de tarefas: `/tarefas-pendentes`, dashboard (widget tasksList) e detalhe de projeto (`renderTaskRow` em `rap_projeto_detail.html`)

## 6. Projetos (`/projetos`)
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
- **Modal Editar Tarefa**: extrai URLs da descrição como botões clicáveis (LinkedIn/Drive). Quando descrição tem `post_id=N` mas não tem URL, busca via `GET /api/editorial/{post_id}` e adiciona botão "Abrir no LinkedIn" — útil pra tasks `editorial_metrics` antigas criadas antes de incluir URL na descrição (`renderTaskLinks` em `rap_projeto_detail.html`).

## 7. Veículos (`/veiculos`)
- Dashboard com itens de manutenção e status
- **Upload NF via foto** (compressão client-side + OCR Claude Vision + Google Drive) → `POST /api/veiculos/{id}/upload-os`
- Criar/finalizar OS → `veiculos.py`
- Editar itens em OS concluídas → `PUT /api/ordens/{id}/editar-itens`
- Timeline de manutenções
- Alertas de itens vencidos/atenção

## 8. Oficinas (`/oficinas`)
- CRUD com especialidades, serviços, contato → `oficinas.py`

## 9. Editorial Calendar (`/editorial`)
- Pipeline: import → análise IA → adaptação → agendamento → publicação → métricas
- 159 posts (10 publicados, 1 agendado, 148 drafts)
- Análise IA: categoria, público, complexidade, score, gancho LinkedIn
- Bulk schedule, import de artigos
- **Stat cards (3, compactos)**: Pendências (rascunhos editorial_posts + hot_takes órfãos + métricas atrasadas) · Próxima publicação · Esta semana (X/4 com meta 1 artigo + 3 hot-takes)
- **Pipeline tabular**: 4 abas (Para Aprovar / Agendados / Coletar Métricas / Publicados) renderizadas como tabela sortable, click no header alterna asc/desc, cache local pra resort sem refetch. Schema de colunas por aba (categoria pra drafts/agendados; idade do post pra coletar; impressões/reações/comentários/eng% pra publicados). #ID badge em todos os cards/linhas pra bater com tarefas que referenciam `post_id=N`. Sort default por aba: agendados ASC (próximo primeiro), coletar métricas ASC (mais antigo = mais urgente), publicados DESC. **Aba "Para Aprovar" faz UNION** com `hot_takes WHERE status='draft' AND editorial_post_id IS NULL` — hot_takes órfãos aparecem com `tipo='hot_take'`, ação "Aprovar" chama `quickApproveHotTake()` em vez de `approvePost()`.
- **Seções removidas em favor do Pipeline**: "Esta Semana" (calendário Seg-Dom — substituído pela aba Agendados), "Publicar Hoje" (auto-publicação cobre o caso), "Para Revisar" (hot_takes drafts agora vivem na aba Para Aprovar via UNION). Mantidos: stat cards, Funil PDCA, Pipeline, Próximos Posts (lista compacta), Biblioteca, Ações Rápidas, Análise IA.
- **Auto-complete de tasks de coleta**: quando xlsx upload é matched (URL ou data PT-BR "7 de abr de 2026"), task com `origem='editorial_metrics'` cuja descrição contém `post_id=N` é marcada `completed`.
- **Upload de xlsx dentro do modal do post**: drop zone integrado ao card "Métricas do LinkedIn" do modal — autopreenche os 5 campos numéricos e mostra confirmação com botão "Registrar (mantém histórico)" → `POST /api/editorial/{post_id}/metrics` com `fonte='xlsx_upload'`. Cada upload gera linha em `editorial_metrics_history` (histórico por data preservado). Eliminada a etapa de auto-match por URL/data — post já é conhecido. Endpoint `/api/editorial/metrics/upload` (auto-match) mantido como dead code pra eventual batch import.
- **Feedback loop IA**: `editorial_pdca.get_top_bottom_examples()` → top/bottom (engagement rate) injetados no prompt de `auto_publisher.select_weekly_posts` e `generate_weekly_briefing`
- **Coleta de métricas em 4 pontos**: 6h, 24h, 72h, 7d via `editorial_metrics_history` (dedup por `dias_apos_publicacao`)
  - Cron: `editorial-metrics-reminder` (11h SP) + `editorial-metrics-reminder-evening` (20h SP)
- **Alerta cadência semanal**: a partir de quinta sem post → ⚠️ no daily-morning-briefing
- **Doc detalhada**: `docs/FEATURE_EDITORIAL.md` + estratégia em `docs/EDITORIAL_STRATEGY.md`

## 10. Hot Takes (`/hot-takes`)
- Gerar de URL, digest, publicar → `hot_takes.py`

## 11. News Hub
- 12 fontes RSS gratuitas (Google News, Valor, Exame, MIT Tech Review, etc.)
- Clipping diário com IA (Haiku, ~$0.01/dia)
- Feedback 👍👎 com aprendizado de preferências
- Sugerir contatos para compartilhar notícia
- Criar post LinkedIn a partir de notícia
- **Pagina `/clipping`** (Fase 2 — 02/05): home dedicada do feature, agrupada na nav em "Conteudo" (Editorial / Clipping / Hot Takes / Artigos). Filtros: date range (default ultimos 7d), categoria, fonte, status (unread/liked/disliked/shared/posted/dismissed), busca textual. Lista paginada de cards com mesmas acoes do dashboard (👍/👎/Encaminhar/Post/Abrir) + badge de status pra items ja tratados (dimmed). Endpoints: `GET /api/clipping/archive` (CTE em `news_clippings.conteudo` JSONB com `jsonb_array_elements WITH ORDINALITY` + LATERAL join opcional contra `news_interactions` pra filtros de status; tambem disponivel como `/api/news/clipping/archive`); `GET /api/clipping/facets?days=30` (categorias + fontes distintas pra dropdowns). Modal share + busca page-aware via `window.pageSearch`.

## 12. Comunicação
- **WhatsApp**: send, receive, sync, webhook, import .txt, grupos → Evolution API
- **Gmail**: sync, send, threading → Google API
  - **Sync via Railway worker** (02/05): `services/gmail_sync.py:sync_all_contacts` migrado pro worker. Vercel matava em 300s pq loop e O(N x M) (3.5k contatos x 2 contas x 3 emails x 2 calls Gmail API + sleep 0.1 = piso ~2100s). `/api/cron/sync-gmail` e `step_gmail` agora chamam `services/job_dispatcher.enqueue_job` que cria registro em `background_jobs` (status queued) e dispara POST fire-and-forget pra `{AUDIO_WORKER_URL}/sync-gmail`. Worker (Railway) faz idempotencia (skip se ja tem job running < 1h), processa async com checkpoint a cada 50 contatos, atualiza status pra completed/error. `GET /api/jobs/{id}` pra inspecionar. Doc completa: [`docs/GMAIL_SYNC_RAILWAY.md`](GMAIL_SYNC_RAILWAY.md)
- **Inbox unificado**: `/inbox`
- **Smart Follow-Up**: detecta emails sem resposta, cria FUP automático → `smart_fup.py`
- **Action Proposals**: dedup por contato+tipo, auto-resolve on reply, expire >7d

## 13. Calendário (`/calendario`)
- Sync Google Calendar bidirecional
- Eventos de hoje no dashboard (exclui aniversários)

## 14. Briefings (`/briefings`)
- Contatos que precisam briefing (C1-3, health<50, com interações)
- Geração com Claude (contexto: fatos, mensagens, tasks)

## 15. Campanhas (`/campanhas`)
- CRUD com steps, executor automático no cron

## 16. Integrações
| Integração | Uso | Config |
|------------|-----|--------|
| Google (Calendar, Contacts, Drive, Tasks, Gmail) | Sync bidirecional | OAuth scopes |
| WhatsApp (Evolution API) | Send/receive/groups | EVOLUTION_API_URL/KEY |
| Claude AI (Anthropic) | Briefings, OCR, análises, clipping | ANTHROPIC_API_KEY |
| LinkedIn (LinkdAPI) | Enriquecimento | LINKDAPI_KEY |
| Fathom | Import reuniões | FATHOM_API_KEY |
| ConselhoOS | Sync dados conselhos | CONSELHOOS_DATABASE_URL |

## 17. Cron Jobs (vercel.json)
| Horário | Job | Steps |
|---------|-----|-------|
| 5h diário | daily-sync | Health, Contacts, Calendar, Tasks, Gmail, PaymentCycle, WA, SmartFUP, AI, Campaigns, AutoEnrich, GroupDocs, Avatars, Clipping, SocialGroupsCache (13 steps) |
| 6h diário | sync-whatsapp-history | Histórico WA |
| 18h diário | health-recalc | Health scores |
| 8h segunda | weekly-digest | Digest semanal |
| 4h domingo | cleanup | Expirar propostas, limpar notificações |

## 18. Intel Bot (WhatsApp conversacional via intel-bot)
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

## 19. Intelligence Automations
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

## 20. ConselhoOS Integration — Ata + RACI Flow
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

## 21. PWA & Mobile
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
