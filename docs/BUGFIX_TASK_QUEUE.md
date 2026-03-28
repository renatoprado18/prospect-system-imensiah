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
**Status**: [ ] Pendente
**Arquivo**: `app/integrations/google_calendar.py`, `app/services/calendar_sync.py`

**Problema**:
- Dashboard mostra "Nenhum compromisso para hoje"
- Usuario tem evento "[Foco] Silencio, Sabedoria, Retiro 10:30am-12pm"
- Calendario em /calendario esta completamente vazio

**Investigar**:
1. Verificar se o token OAuth tem scope correto (`calendar.readonly` ou `calendar`)
2. Verificar endpoint `/api/calendar/today` - esta retornando dados?
3. Verificar endpoint `/api/calendar/events` - lista eventos?
4. Verificar se `calendar_sync_state` tem sync_token valido
5. Verificar logs de erro no sync

**Solucao esperada**:
- Dashboard deve mostrar eventos de hoje
- /calendario deve listar todos os eventos do Google Calendar

---

### Task 2: Debugar Google Tasks Sync
**Status**: [ ] Pendente
**Arquivo**: `app/integrations/google_tasks.py` (se existir)

**Problema**:
- Dashboard mostra "Nenhuma tarefa pendente"
- Usuario tem 7 tarefas no Google Tasks

**Investigar**:
1. Verificar se Google Tasks API esta integrada
2. Se nao existir, CRIAR integracao com Google Tasks API
3. Scope necessario: `https://www.googleapis.com/auth/tasks.readonly`
4. Criar tabela `google_tasks` se necessario
5. Criar endpoints `/api/tasks` para listar tarefas

**Solucao esperada**:
- Dashboard deve mostrar tarefas pendentes do Google Tasks
- Widget "Tarefas" deve ser funcional

---

### Task 3: Fix Badge Circulos Loading
**Status**: [ ] Pendente
**Arquivo**: `app/main.py`, endpoint que retorna stats

**Problema**:
- Badge "Circulos" no dashboard mostra "Carregando..." infinito

**Investigar**:
1. Verificar endpoint `/api/v1/dashboard` ou similar
2. Verificar se campo `circulos_ativos` esta sendo retornado
3. Verificar JS que popula o badge

**Solucao esperada**:
- Badge deve mostrar numero de circulos ativos (ex: "444")

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
**Status**: [ ] Pendente
**Arquivo**: `app/services/briefings.py`, `app/main.py`

**Requisito**:
- Briefing deve poder gerar acoes (nao apenas texto para copiar)
- Acoes possiveis: criar tarefa, agendar reuniao, enviar email, enviar WhatsApp

**Implementacao**:
1. Endpoint `POST /api/briefings/{id}/create-task` - cria tarefa a partir do briefing
2. Endpoint `POST /api/briefings/{id}/schedule-meeting` - agenda reuniao
3. Endpoint `POST /api/briefings/{id}/draft-email` - gera rascunho de email
4. Endpoint `POST /api/briefings/{id}/draft-whatsapp` - gera mensagem WhatsApp

---

## Ordem de Execucao

1. **Task 1** - Calendar Sync (mais visivel para usuario)
2. **Task 2** - Tasks Sync
3. **Task 3** - Badge Circulos
4. **Task 4** - AI Auto-Enrich
5. **Task 5** - Briefing Actions

---

## Verificacao

Apos completar, testar:
- [ ] Dashboard mostra eventos de hoje do Google Calendar
- [ ] Dashboard mostra tarefas do Google Tasks
- [ ] /calendario lista eventos sincronizados
- [ ] Badge Circulos mostra numero correto
- [ ] Contatos circulo 1-2 tem resumo AI automatico
