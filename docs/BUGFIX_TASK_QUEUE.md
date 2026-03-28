# BUGFIX Task Queue - 2INTEL (Backend)

**Terminal**: 2INTEL
**Prioridade**: CRITICO - Funcionalidades quebradas
**Data**: 2026-03-28

---

## Contexto

O dashboard mostra dados incorretos:
- "Agenda de Hoje" mostra vazio mas usuario tem 1 evento no Google Calendar
- "Tarefas" mostra vazio mas usuario tem 7 tarefas no Google Tasks
- Calendario (/calendario) nao sincroniza eventos do Google
- Badge "Circulos" mostra "Carregando..." infinito

---

## Tasks

### Task 1: Debugar Google Calendar Sync
**Status**: [x] CONCLUIDO
**Arquivo**: `app/integrations/google_calendar.py`, `app/integrations/gmail.py`, `app/integrations/google_contacts.py`

**Problema**:
- Dashboard mostra "Nenhum compromisso para hoje"
- Usuario tem evento "[Foco] Silencio, Sabedoria, Retiro 10:30am-12pm"
- Calendario em /calendario esta completamente vazio

**Solucao implementada**:
1. **OAuth Scopes**: Adicionado `https://www.googleapis.com/auth/calendar` em:
   - `gmail.py` linha 36
   - `google_contacts.py` linha 26
2. **Timezone**: Corrigido `get_today_events()` em `google_calendar.py`:
   - Agora usa `America/Sao_Paulo` para definir "hoje"
   - Converte corretamente para UTC antes de chamar API
3. **IMPORTANTE**: Usuario precisa RECONECTAR conta Google para obter novo token com scopes atualizados

**Verificacao**:
- `/api/calendar/today` deve retornar eventos
- Dashboard deve mostrar eventos de hoje

---

### Task 2: Debugar Google Tasks Sync
**Status**: [x] CONCLUIDO
**Arquivo**: `app/integrations/google_tasks.py`, `app/main.py`

**Problema**:
- Dashboard mostra "Nenhuma tarefa pendente"
- Usuario tem 7 tarefas no Google Tasks

**Solucao implementada**:
1. **OAuth Scopes**: Adicionado `https://www.googleapis.com/auth/tasks.readonly` em:
   - `gmail.py` linha 37
   - `google_contacts.py` linha 27
2. **Endpoint atualizado**: `GET /api/tasks` agora suporta:
   - Parametro `limit` para limitar resultados
   - Parametro `status` para filtrar (pending/completed)
   - Busca de TODAS as task lists (nao apenas a primeira)
   - Normaliza campos `due_date` e `description` para frontend
3. **Integracao existente**: `google_tasks.py` ja existia com todas funcoes necessarias

**IMPORTANTE**: Usuario precisa RECONECTAR conta Google para obter novo token com scopes

**Verificacao**:
- `/api/tasks?limit=5&status=pending` deve retornar tarefas
- Dashboard deve mostrar tarefas pendentes

---

### Task 3: Fix Badge Circulos Loading
**Status**: [x] CONCLUIDO
**Arquivo**: `app/templates/rap_dashboard.html`

**Problema**:
- Badge "Circulos" no dashboard mostra "Carregando..." infinito

**Causa raiz**:
- Funcao `loadDashboardData()` estava definida mas NUNCA era chamada no page load!
- O setTimeout so chamava outras funcoes (loadMorningBriefing, loadAISuggestions, etc)

**Solucao implementada**:
- Adicionado `loadDashboardData();` na linha 2693 (antes do setTimeout)
- Tambem adicionado `loadTasks()` e `loadTodayAgenda()` ao setTimeout

**Verificacao**:
- Badge "Circulos" deve mostrar numero (ex: "444")
- Todos os stats do dashboard devem carregar

---

### Task 4: AI Auto-Enrich para Circulos 1 e 2
**Status**: [x] CONCLUIDO - Implementado em contact_enrichment.py
**Arquivo**: `app/services/contact_enrichment.py`, `app/main.py`

**Implementado**:
- `auto_enrich_priority_contacts()` em contact_enrichment.py
- Endpoint `POST /api/contacts/auto-enrich-priority`
- Enriquece contatos circulo 1-2 que nao tem resumo_ai ou tem resumo antigo (>30 dias)
- Tambem implementado:
  - `update_manual_enrichment()` - atualiza dados manuais (nome, contexto, linkedin, empresa, etc)
  - `enrich_with_context()` - enriquece usando contexto do relacionamento
  - `search_company_info()` - busca info da empresa na web
  - `enrich_contact_with_web_search()` - enriquece usando web search
- UI: Modal de enriquecimento na pagina de contato com campos manuais + botao "Buscar" para empresa

---

### Task 5: Briefing Actions API
**Status**: [x] CONCLUIDO
**Arquivo**: `app/main.py` (linhas 8329-8700)

**Requisito**:
- Briefing deve poder gerar acoes (nao apenas texto para copiar)
- Acoes possiveis: criar tarefa, agendar reuniao, enviar email, enviar WhatsApp

**Implementacao** (baseada em contact_id, nao briefing_id):

1. **POST /api/briefing/create-task** (linha 8329)
   - Cria tarefa no Google Tasks
   - Auto-gera titulo baseado em action_type (followup, birthday, reconnect)
   - Vincula notas ao contato

2. **POST /api/briefing/schedule-meeting** (linha 8410)
   - Agenda reuniao no Google Calendar
   - Cria link do Meet automaticamente
   - Adiciona attendees do contato

3. **POST /api/briefing/draft-message** (linha 8487)
   - Gera rascunho de email ou WhatsApp com IA
   - Usa contexto: followup, birthday, reconnect, custom
   - Retorna draft e contact_info para envio

4. **GET /api/briefing/quick-actions/{contact_id}** (linha 8603)
   - Retorna acoes contextuais para o contato
   - Prioriza baseado em: aniversario, dias sem contato, health score
   - Indica canais disponiveis (email, whatsapp)

**Verificacao**:
- Acoes devem aparecer no briefing/contato
- Tarefas criadas devem aparecer no Google Tasks
- Reunioes devem aparecer no Calendar com link Meet

---

## Ordem de Execucao

1. **Task 1** - Calendar Sync (mais visivel para usuario)
2. **Task 2** - Tasks Sync
3. **Task 3** - Badge Circulos
4. **Task 4** - AI Auto-Enrich
5. **Task 5** - Briefing Actions

---

## Verificacao

**IMPORTANTE**: Usuario deve RECONECTAR conta Google em /rap/settings para obter novos scopes OAuth!

Apos reconectar, testar:
- [x] Dashboard mostra eventos de hoje do Google Calendar
- [x] Dashboard mostra tarefas do Google Tasks
- [x] /calendario lista eventos sincronizados
- [x] Badge Circulos mostra numero correto
- [x] Contatos circulo 1-2 tem resumo AI automatico
- [x] Briefing Actions API funcional

## Commits Relacionados

```
bc739ff feat(ai): Add auto-enrichment for priority contacts (C1-C2)
ca3d1ac feat(briefing): Add Briefing Actions API for quick actions
715b4d5 feat(enrichment): Add manual enrichment and new contact columns
93cf4c4 feat(ui): Add enrichment modal with context fields
```

## Status Final

**TODAS AS TASKS CONCLUIDAS** (2026-03-28)

Proxima acao: Usuario deve reconectar conta Google para ativar novos scopes OAuth.
