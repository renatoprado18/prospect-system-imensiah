# Funcionalidades do INTEL — Mapa Completo

> Auto-gerado + mantido manualmente. Consulte ANTES de implementar qualquer feature.
> Ultima atualizacao: 2026-04-19

## Por Pagina

### Dashboard (`/` → rap_dashboard.html)
- Stats gerais (contatos, health, circulos)
- Acoes sugeridas (action proposals) com botoes executaveis
- Contatos recentes
- Tarefas pendentes
- Calendario resumido
- **LinkedIn Editorial card**: posts semana, impressoes, engajamento, metricas pendentes, proximo post

### Contatos (`/contatos` → rap_contacts.html)
- Lista paginada com busca e filtros (circulo, empresa, tags)
- Importacao CSV
- Novo contato manual

### Contato Detalhe (`/contatos/{id}` → rap_contact_detail.html)
- Resumo IA (Claude)
- Timeline (memorias, interacoes, mensagens)
- Informacoes (empresa, cargo, telefone, email, LinkedIn)
- Relacionamentos inferidos
- Rodas de contato
- Documentos (Google Drive)
- **LinkedIn tile**: headline, about, experience, skills, conexoes, foto
- **Enriquecer LinkedIn** (botao refresh no tile)
- Fatos extraidos por IA
- Insights (relevancia, potencial, sentimento)

### Circulos (`/circulos` → rap_circulos.html)
- Visualizacao C1-C5 com health scores
- Reclassificacao de contatos
- Metricas por circulo

### WhatsApp (`/rap/whatsapp` → rap_inbox.html)
- Inbox de conversas
- Envio de mensagens
- Templates de mensagem

### Configuracoes (`/configuracoes` → rap_settings.html)
- Status conexao WhatsApp (Evolution API)
- Webhook status
- **Importar historico WhatsApp** (drag & drop .txt exportados do celular)

### Projetos (`/projetos` → rap_projetos.html)
- Lista com filtro por tipo (negocio, patrimonio, pessoal, conselho)
- Status: ativo, pausado, concluido
- Indicadores: tarefas vencidas, marcos atrasados

### Projeto Detalhe (`/projetos/{id}` → rap_projeto_detail.html)
- **AI Briefing** (analisa tarefas, msgs membros, notas, calendario)
- Tarefas agrupadas (vencidas/hoje/proximas/concluidas)
- Marcos com status
- Timeline/notas
- Participantes
- Documentos (Google Drive)

### Campanhas (`/campanhas` → rap_campanhas.html)
- Campanhas de outreach
- Acoes pendentes por dia

### Briefings (`/briefings` → rap_briefings.html)
- Briefings pre-reuniao gerados por IA
- Contexto enriquecido (msgs, historico, fatos)

### Calendario (`/calendario` → rap_calendario.html)
- Eventos Google Calendar sincronizados
- Vinculo com contatos

### Editorial (`/editorial` → editorial.html)
- Calendario semanal (Seg-Dom)
- Stats: agendados, publicados, hot takes, biblioteca
- Posts agendados/publicados com metricas LinkedIn
- Hot takes (noticia → hook → body → CTA)
- Biblioteca de artigos (141 rascunhos)
- Modal de edicao com metricas (impressoes, reacoes, comentarios)
- Agendar publicacao
- **Funil Editorial PDCA**: Posts → Impressoes → Engajamento → Mensagens → Reunioes
- **Tendencia semanal** com barras de impressoes (4 semanas)
- **Performance por pilar**: NeoGovernanca, IA aplicada, Bastidores
- **Insights automaticos** (melhor pilar, taxa de engajamento, tendencias)
- **Briefing semanal IA** (cron domingos 18h): analisa performance, sugere 3 posts, cria tarefas
- **Upload xlsx metricas**: Drag & drop de arquivo LinkedIn Analytics (.xlsx) com parse automatico
- **Historico de metricas** (`editorial_metrics_history`): snapshots de metricas ao longo do tempo
- **Comparativo normalizado 48h**: tabela comparando posts no mesmo ponto temporal
- **Auto-criacao de tarefas de metricas**: ao publicar, cria tarefa "Coletar metricas" para 48h depois
- **Dashboard card Editorial Metricas**: posts da semana, impressoes, engajamento, metricas pendentes

### Hot Takes (`/hot-takes` → hot_takes.html)
- Lista de hot takes com status
- Criar novo a partir de noticia
- Publicar com link LinkedIn

### LinkedIn Import (`/contatos/linkedin` → rap_linkedin_import.html)
- Importar conexoes via CSV do LinkedIn
- Preview antes de importar

### LinkedIn Bookmarklet (`/linkedin/bookmarklet` → rap_linkedin_bookmarklet.html)
- Bookmarklet para enriquecer perfil manualmente

### Analytics (`/analytics` → rap_analytics.html)
- Dashboard analitico
- Metricas de engajamento

### Emails (`/emails` → rap_emails.html)
- Triage de emails com IA
- Classificacao e prioridade

### Veiculos (`/veiculos` → rap_veiculos.html)
- Gestao de frota
- Ordens de servico
- Timeline de manutencao

### Oficinas (`/oficinas` → rap_oficinas.html)
- Cadastro de oficinas mecanicas

### News (`/news` → rap_news.html)
- Feed de noticias relevantes
- Match com contatos

### Artigos (`/artigos` → artigos.html)
- Biblioteca de artigos do blog

## Integrações Ativas

| Integracao | Funcionalidade | Config |
|------------|---------------|--------|
| WhatsApp (Evolution API) | Envio/recebimento msgs, webhook, historico | EVOLUTION_API_URL/KEY |
| Google Calendar | Sync eventos, criar/remarcar | GOOGLE_CLIENT_ID/SECRET |
| Google Gmail | Leitura emails, triage IA | GOOGLE_CLIENT_ID/SECRET |
| Google Drive | Documentos de contatos/projetos, push notifications (webhook), auto-reindex | GOOGLE_CLIENT_ID/SECRET, DRIVE_WEBHOOK_TOKEN |
| Google Tasks | Sync tarefas | GOOGLE_CLIENT_ID/SECRET |
| Claude (Anthropic) | Briefings, resumos, analises, OCR | ANTHROPIC_API_KEY |
| LinkdAPI | Enriquecimento LinkedIn (perfil, skills, exp) | LINKDAPI_KEY |
| RapidAPI Fresh LinkedIn | Fallback enriquecimento (legado) | RAPIDAPI_KEY |
| Fathom | Importar reunioes → projetos/tarefas | FATHOM_API_KEY_* |

## Scripts Utilitarios (`scripts/`)

| Script | Funcao |
|--------|--------|
| import_fathom_meeting.py | Importar reuniao Fathom → projetos/tarefas |
| sync_whatsapp_history.py | Sync historico WhatsApp via Evolution API |
| sync-local-db.sh | Sync producao → banco local |
| enrich_linkedin_batch.py | Batch enrichment LinkedIn (legado) |

## Cron Jobs (vercel.json)

| Cron | Endpoint | Frequencia |
|------|----------|------------|
| Daily sync | /api/cron/daily-sync | 5h UTC |
| WhatsApp history sync | /api/cron/sync-whatsapp-history | 6h UTC |
| Health recalc | /api/cron/recalculate-health | 5h + 18h UTC |
| Expire proposals | /api/cron/expire-proposals | 4h UTC |
| Editorial briefing | /api/cron/editorial-weekly-briefing | Dom 21h UTC (18h BR) |
| Drive documents reindex | /api/cron/index-drive-documents | 7h UTC |

## Numeros (528 endpoints, 32 templates, 65 tabelas, 60 services)
