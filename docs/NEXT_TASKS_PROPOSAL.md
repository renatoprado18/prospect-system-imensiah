# Proposta de Proximas Filas de Tarefas - 2INTEL

**Data**: 2026-03-27
**Para**: ARCH
**De**: 2INTEL

---

## Resumo do Status Atual

### Concluido nas Ultimas Sessoes

| Modulo | Tarefas | Status |
|--------|---------|--------|
| AI Sugestoes | Tabela + CRUD + Endpoints | ✅ |
| AI Agent | Geracao automatica de sugestoes | ✅ |
| Smart Triggers | Automacoes baseadas em eventos | ✅ |
| Health Predictions | Previsao de health score | ✅ |
| Message Templates | Templates + personalizacao | ✅ |
| Digest Generator | Resumos diarios/semanais | ✅ |
| **Calendar Integration** | Sync bidirecional + AI | ✅ |
| Gmail Sync | Leitura + envio | ✅ |
| WhatsApp | Integracao Evolution API | ✅ (parcial) |
| Google Tasks | Leitura de tarefas | ✅ |
| Busca Avancada | API de busca | ✅ |
| Exportacao | CSV/JSON contacts | ✅ |
| Batch Operations | Operacoes em lote | ✅ |
| SSE Notifications | Notificacoes real-time | ✅ |

---

## Filas Propostas

### FILA 1: WhatsApp Completo (ALTA PRIORIDADE)

**Objetivo**: Completar integracao WhatsApp com historico e UI

| # | Tarefa | Prioridade | Descricao |
|---|--------|------------|-----------|
| 1 | Parser WhatsApp .txt | CRITICA | Importar historico de chats exportados |
| 2 | UI Import WhatsApp | ALTA | Tela para upload e import de .txt |
| 3 | WhatsApp Settings Page | ALTA | Tela de configuracao/QR Code |
| 4 | Vinculacao Automatica | MEDIA | Vincular msgs a contatos por telefone |
| 5 | UI Envio WhatsApp | MEDIA | Botao + modal para enviar msg |

**Arquivos**:
- `app/services/whatsapp_import.py`
- `app/templates/rap_whatsapp_settings.html`
- `app/templates/rap_whatsapp_import.html`
- Modificar: `rap_contact_detail.html`

---

### FILA 2: ConselhoOS (MEDIA PRIORIDADE)

**Objetivo**: Modulo de gestao de conselhos de administracao

| # | Tarefa | Prioridade | Descricao |
|---|--------|------------|-----------|
| 1 | Tabelas ConselhoOS | ALTA | companies, board_members, meetings |
| 2 | Service ConselhoOS | ALTA | CRUD de empresas/conselhos |
| 3 | Endpoints API | ALTA | REST para ConselhoOS |
| 4 | UI Dashboard | MEDIA | Tela principal ConselhoOS |
| 5 | UI Detalhe Empresa | MEDIA | Membros + reunioes |
| 6 | Integracao Calendar | BAIXA | Reunioes no calendario |

**Arquivos**:
- `app/services/conselhoos.py`
- `app/templates/rap_conselhoos.html`
- `app/templates/rap_conselhoos_company.html`

---

### FILA 3: Google Tasks Bidirecional (MEDIA PRIORIDADE)

**Objetivo**: Sync completo de tarefas com Google Tasks

| # | Tarefa | Prioridade | Descricao |
|---|--------|------------|-----------|
| 1 | Tabela tasks local | ALTA | Armazenar tarefas localmente |
| 2 | Tasks Sync Service | ALTA | Pull/push de tarefas |
| 3 | Endpoints CRUD | ALTA | API de tarefas |
| 4 | UI Tasks | MEDIA | Tela de tarefas |
| 5 | Integracao AI | BAIXA | AI sugere tarefas |

**Arquivos**:
- `app/services/tasks_sync.py`
- `app/templates/rap_tasks.html`

---

### FILA 4: Dashboard Unificado (MEDIA PRIORIDADE)

**Objetivo**: Melhorar dashboard RAP com todos os dados

| # | Tarefa | Prioridade | Descricao |
|---|--------|------------|-----------|
| 1 | Widget Calendar | ALTA | Proximos eventos no dashboard |
| 2 | Widget Sugestoes AI | ALTA | Top sugestoes pendentes |
| 3 | Widget Health Alerts | MEDIA | Contatos em risco |
| 4 | Widget Tasks | MEDIA | Tarefas pendentes |
| 5 | Widget Aniversarios | BAIXA | Proximos aniversarios |

**Arquivos**:
- Modificar: `app/templates/rap_dashboard.html`
- Modificar: `app/services/dashboard.py`

---

### FILA 5: Cron Jobs Automaticos (BAIXA PRIORIDADE)

**Objetivo**: Automacoes agendadas

| # | Tarefa | Prioridade | Descricao |
|---|--------|------------|-----------|
| 1 | Vercel Cron Config | ALTA | Configurar cron jobs |
| 2 | Daily Sync | ALTA | Sync Gmail/Calendar/Tasks |
| 3 | Weekly Digest | MEDIA | Gerar digest semanal |
| 4 | Health Recalc | MEDIA | Recalcular health scores |
| 5 | Cleanup Expired | BAIXA | Limpar sugestoes expiradas |

**Arquivos**:
- `vercel.json` (cron config)
- `api/cron/*.py`

---

### FILA 6: Contact Enrichment (BAIXA PRIORIDADE)

**Objetivo**: Melhorar dados de contatos automaticamente

| # | Tarefa | Prioridade | Descricao |
|---|--------|------------|-----------|
| 1 | Proxycurl Integration | MEDIA | Dados LinkedIn |
| 2 | Clearbit Integration | MEDIA | Dados empresa |
| 3 | Photo Fetch | BAIXA | Buscar fotos automatico |
| 4 | UI Enrich Button | BAIXA | Botao "Enriquecer" |

**Arquivos**:
- `app/services/enrichment.py`
- Modificar: `rap_contact_detail.html`

---

## Recomendacao de Ordem de Execucao

```
PRIORIDADE ALTA:
1. FILA 1: WhatsApp Completo     ← Mais valor imediato
2. FILA 4: Dashboard Unificado   ← Visibilidade dos dados

PRIORIDADE MEDIA:
3. FILA 2: ConselhoOS            ← Feature solicitada
4. FILA 3: Google Tasks          ← Produtividade

PRIORIDADE BAIXA:
5. FILA 5: Cron Jobs             ← Automacao
6. FILA 6: Enrichment            ← Nice to have
```

---

## Estimativa Total

| Fila | Tarefas | Arquivos Novos | Modificacoes |
|------|---------|----------------|--------------|
| WhatsApp | 5 | 3 | 2 |
| ConselhoOS | 6 | 3 | 1 |
| Tasks | 5 | 2 | 1 |
| Dashboard | 5 | 0 | 2 |
| Cron | 5 | 3+ | 1 |
| Enrichment | 4 | 1 | 1 |
| **TOTAL** | **30** | **12** | **8** |

---

## Aguardando Aprovacao

Por favor, indique:
1. Qual fila executar primeiro?
2. Alguma fila deve ser removida/adiada?
3. Alguma tarefa adicional a incluir?

---

**2INTEL pronto para execucao apos aprovacao.**
