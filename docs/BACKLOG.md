# INTEL + ConselhoOS — Backlog

## Concluído ✅

### Bot WhatsApp INTEL (intel-bot)
- [x] Bot conversacional com Claude + tools (query_intel, query_conselhoos, execute_action, draft_message)
- [x] Memória de conversa (últimas 20 mensagens)
- [x] Acesso total ao banco INTEL e ConselhoOS via SQL
- [x] Rascunhar mensagens personalizadas com contexto completo
- [x] Criar tarefas, agendar reuniões, salvar notas via WhatsApp
- [x] Número dedicado (+5511915020192)
- [x] Notificações consolidadas no intel-bot (kill INTEL Proativo)
- [x] Anti-alucinação (nunca inventar fatos)
- [x] Preferências de comunicação (grato/agradeço, lamento/sinto muito)

### Smart Message Processor
- [x] Detectar emails em mensagens → auto-atualizar contatos
- [x] Detectar propostas de reunião (datas/horários)
- [x] Análise de contexto com Claude (entender de quem é o email)
- [x] Notificação WhatsApp via intel-bot pra toda action proposal
- [x] Fix LID format no webhook (WhatsApp Meta migration)

### LinkedIn Enrichment
- [x] 2.100 contatos enriquecidos via LinkdAPI
- [x] 411 mudanças de emprego detectadas
- [x] Migração Fresh LinkedIn → LinkdAPI
- [x] Campos: headline, about, experience, skills, connections, foto
- [x] LinkedIn tile no contato (experience, skills, conexões)
- [x] Auto-enrich C1-C3 no cron

### Editorial PDCA
- [x] Funil editorial: Posts → Impressões → Engajamento → Mensagens → Reuniões
- [x] Briefing semanal automático (cron domingo 18h)
- [x] Tarefas automáticas (publicar, medir, avaliar)
- [x] Upload xlsx de métricas
- [x] Histórico de métricas (editorial_metrics_history)
- [x] Comparação normalizada 48h
- [x] Card Editorial no dashboard
- [x] Clipping diário com IA + feedback learning

### Projetos
- [x] AI Briefing na página do projeto (analisa tarefas, msgs, notas)
- [x] Tarefas agrupadas por status (vencidas/hoje/próximas/concluídas)
- [x] Smart Update com memória de pareceres
- [x] Download docs grupos WA → Google Drive

### Contatos
- [x] Enriquecer com IA (WhatsApp + email + LinkedIn + fatos)
- [x] Google Tasks sync bidirecional
- [x] Sugerir artigos para reconexão

### WhatsApp
- [x] Sync histórico via Evolution API (script + cron diário)
- [x] Import de .txt exportados (UI em /configuracoes)
- [x] Fix duplicatas (external_id no whatsapp_sync)

### Grupos Sociais
- [x] Extrair participantes dos grupos
- [x] Cruzar com contatos INTEL
- [x] Página "Meus Grupos"
- [x] Cruzamento entre grupos
- [x] Sugestão de introduções

### Mobile / PWA
- [x] Responsividade (dashboard, contatos, projetos, editorial)
- [x] PWA: apple-mobile-web-app-capable, viewport-fit, service worker
- [x] Touch targets 44px
- [x] Sidebar hamburger com safe-area

### Integração ConselhoOS ↔ INTEL
- [x] Bot consulta ConselhoOS (query_conselhoos tool)
- [x] Tabela pessoas no ConselhoOS com link ao INTEL (intel_contact_id)
- [x] Seed Vallen com 6 participantes
- [x] Pessoas auto-sync → project_members no INTEL
- [x] RACI ↔ Tasks bidirecional (cron diário)
- [x] Geração de ata DOCX profissional (python-docx, template padrão)
- [x] Sistema de templates (padrão + customizável por empresa)
- [x] RACI como Google Sheet formatado (cores R/A/C/I)
- [x] Envio de ata por email com permissão commenter
- [x] Modal com destinatários, assunto e mensagem editáveis
- [x] RACI auto-incluído no envio
- [x] Botão Drive na página da reunião
- [x] Exportar ata (Gerar Ata + Baixar DOCX)

### Action Proposals
- [x] Botões executáveis no dashboard
- [x] Fallback quando evento não existe (cria tarefa)
- [x] Skip análise de mensagens próprias (contact_id 14911)

### Fathom Integration
- [x] Import de reuniões → projetos/tarefas
- [x] Contact hints (--contact-hint)
- [x] Min projects (--min-projects)
- [x] System user resolution

### Google Drive
- [x] Cron diário de reindexação de documentos
- [x] Fix column mapping bug em documentos
- [x] Webhook Google Drive (push notifications)

---

## Pendente — Alta Prioridade

### Integração ConselhoOS ↔ INTEL (continuação)
- [ ] Briefing pré-reunião usando contexto INTEL (WhatsApp, LinkedIn, memórias dos participantes)
- [ ] Reuniões ConselhoOS → Calendar INTEL (com participantes)
- [ ] Bot como ponte: "Gere a ata da Vallen" / "Mande a ata pro Amadeo"

### Bot WhatsApp
- [ ] Suporte a áudio (transcrição via Whisper/Claude)
- [ ] Proatividade: briefing diário de manhã, lembretes de tarefas

### Editorial
- [ ] Fix drag & drop xlsx no editorial
- [ ] Auto-agendar posts semanais (IA seleciona drafts)
- [ ] Limpar pasta Atas no Drive (arquivos de teste)

---

## Pendente — Média Prioridade

### Contexto Persistente
- [ ] Resumo acumulativo por projeto (condensar 10 pareceres em 1)
- [ ] "Assistente dedicado" por projeto com personalidade/contexto fixo

### Editorial
- [ ] LinkedIn agendamento direto via API (não possível atualmente)
- [ ] Sugerir artigo específico baseado no perfil do contato (IA)
- [ ] Instagram suporte (carrossel, reels)

### Fluxos
- [ ] Registrar conversa após ligação (post-call flow)
- [ ] Resumo semanal por email

### ConselhoOS
- [ ] Melhorar visualização markdown da ata no ConselhoOS (ReactMarkdown + prose)
- [ ] Ata: melhorar quebras de página no DOCX

---

## Pendente — Baixa Prioridade

### UX
- [ ] Dark mode
- [ ] Notificações push PWA em mobile
- [ ] Atalhos de teclado (além do Cmd+K)

### Infra
- [ ] Cache de queries pesadas (dashboard, circles)
- [ ] Logs de auditoria (quem fez o quê)
- [ ] Vercel Pro (60s timeout pra bot)
