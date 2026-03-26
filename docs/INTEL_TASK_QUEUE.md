# Fila de Tarefas 2INTEL

**Atualizacao**: 2026-03-26
**Modo**: AUTONOMO TOTAL - executar todas as tarefas sem aguardar aprovacao

---

## TAREFAS CONCLUIDAS

| Tarefa | Commit | Resumo |
|--------|--------|--------|
| Recalcular Circulos | 620de30 | 6647 contatos distribuidos em 5 circulos |
| Auto-Tags | 620de30 | 1242 contatos, 1526 tags aplicadas |
| Verificar Duplicados | 620de30 | 42 duplicados identificados |
| Briefing Context | 698214e | briefing_context.py + 5 endpoints |
| Engajamento | merged | engajamento.py + 4 endpoints |
| Duplicados Service | 8c93930 | duplicados.py + Levenshtein + 3 endpoints |
| Gmail Sync | 2f1dfa8 | gmail_sync.py + endpoints |
| WhatsApp Sync | 2f1dfa8 | whatsapp_sync.py + webhook |
| Google Calendar | 2f1dfa8 | google_calendar.py + /api/calendar |
| Google Tasks | 2f1dfa8 | google_tasks.py + /api/tasks |
| Merge Duplicados | 2f1dfa8 | 3 duplicados mergeados |

---

## NOVAS TAREFAS (Executar em ordem)

### Tarefa 1: API Inbox Unificado

**Status**: PENDENTE
**Prioridade**: CRITICA

**Objetivo**: Endpoints para o Inbox unificado que 3FLOW criou na UI.

**Criar/Atualizar**: `app/services/inbox.py`

```python
"""
Inbox Service - Unifica emails e WhatsApp
"""
class InboxService:
    async def get_conversations(self, limit=50, filter_type=None):
        """Lista conversas (email + whatsapp) ordenadas por data"""
        # Combinar dados de conversations table
        # Retornar: id, contact_id, contact_name, channel, last_message, unread_count, timestamp
        pass

    async def get_messages(self, conversation_id: int):
        """Mensagens de uma conversa especifica"""
        pass

    async def get_unread_count(self):
        """Total de nao lidos (para badge)"""
        pass

    async def mark_as_read(self, conversation_id: int):
        """Marca conversa como lida"""
        pass
```

**Endpoints**:
- `GET /api/inbox/conversations` - lista conversas
- `GET /api/inbox/conversations/{id}/messages` - mensagens
- `GET /api/inbox/unread` - contador nao lidos
- `POST /api/inbox/conversations/{id}/read` - marcar como lido

**Criterios**:
- [ ] InboxService criado
- [ ] Endpoints funcionando
- [ ] Combina email + WhatsApp
- [ ] Badge do sidebar funciona

---

### Tarefa 2: API Timeline de Contato

**Status**: PENDENTE
**Prioridade**: ALTA

**Objetivo**: Endpoint para timeline unificada na pagina de contato.

**Criar**: `app/services/timeline.py`

```python
"""
Timeline Service - Historico unificado de interacoes
"""
class TimelineService:
    async def get_contact_timeline(self, contact_id: int, limit=50):
        """
        Retorna timeline unificada:
        - Emails enviados/recebidos
        - Mensagens WhatsApp
        - Reunioes (calendar events)
        - Notas manuais
        - Mudancas de circulo
        """
        # Query contact_memories + messages + calendar
        # Ordenar por data DESC
        # Retornar: type, title, content, timestamp, metadata
        pass
```

**Endpoint**:
- `GET /api/contacts/{id}/timeline` - timeline do contato

**Criterios**:
- [ ] TimelineService criado
- [ ] Combina todas as fontes
- [ ] Ordenado por data
- [ ] Paginacao funciona

---

### Tarefa 3: API Notificacoes

**Status**: PENDENTE
**Prioridade**: ALTA

**Objetivo**: Sistema de notificacoes para o sino no header.

**Criar**: `app/services/notifications.py`

```python
"""
Notifications Service
"""
class NotificationService:
    async def get_notifications(self, limit=20):
        """
        Tipos:
        - birthday_today: Aniversarios de hoje
        - birthday_upcoming: Proximos 7 dias
        - low_health: Contatos precisando atencao
        - new_message: Novas mensagens
        - task_due: Tarefas vencendo
        """
        pass

    async def get_unread_count(self):
        """Total de notificacoes nao lidas"""
        pass

    async def mark_all_read(self):
        """Marca todas como lidas"""
        pass
```

**Endpoints**:
- `GET /api/notifications` - lista notificacoes
- `GET /api/notifications/count` - contador
- `POST /api/notifications/read-all` - marcar todas lidas

**Criterios**:
- [ ] NotificationService criado
- [ ] Todos os tipos implementados
- [ ] Polling funciona (3FLOW chama a cada 2min)

---

### Tarefa 4: Background Jobs

**Status**: PENDENTE
**Prioridade**: MEDIA

**Objetivo**: Jobs periodicos para sync automatico.

**Criar**: `app/services/background_jobs.py`

```python
"""
Background Jobs - Executar periodicamente
"""
import asyncio
from datetime import datetime, timedelta

class BackgroundJobsService:
    async def sync_gmail_periodic(self):
        """Sync Gmail a cada 15 minutos"""
        pass

    async def sync_whatsapp_periodic(self):
        """Processar webhooks pendentes"""
        pass

    async def recalculate_health_scores(self):
        """Recalcular health scores diariamente"""
        pass

    async def generate_daily_briefings(self):
        """Gerar briefings para reunioes do dia"""
        pass
```

**Implementar usando**:
- APScheduler ou
- Celery (se precisar mais robusto) ou
- Simples asyncio com sleep

**Criterios**:
- [ ] Jobs configurados
- [ ] Gmail sync periodico
- [ ] Health score recalculo
- [ ] Logs de execucao

---

### Tarefa 5: Melhorar Distribuicao de Circulos

**Status**: PENDENTE
**Prioridade**: BAIXA

**Objetivo**: 93.5% dos contatos ainda em C5. Melhorar algoritmo.

**Atualizar**: `app/services/circulos.py`

**Sugestoes**:
1. Dar mais peso para contatos com LinkedIn conectado
2. Considerar tags (c-level, diretor = +pontos)
3. Considerar empresa conhecida
4. Usar dados do Gmail/WhatsApp sync para interacoes

**Criterios**:
- [ ] Algoritmo revisado
- [ ] Distribuicao mais equilibrada
- [ ] Recalculo executado

---

## INSTRUCOES DE EXECUCAO

1. **Branch**: `git checkout -b feature/intel-apis-v2`
2. **Executar em ordem** (1 -> 5)
3. **Commit por tarefa**
4. **Merge direto em main**

## AUTONOMIA

- NAO aguardar aprovacao
- Se encontrar erro, resolver e continuar
- Atualizar este arquivo conforme progresso

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-26 | Gmail/WhatsApp/Calendar/Tasks | **CONCLUIDO** |
| 2026-03-26 | Recalculo + Tags + Duplicados | **CONCLUIDO** |
| 2026-03-25 | Services anteriores | **MERGED** |
