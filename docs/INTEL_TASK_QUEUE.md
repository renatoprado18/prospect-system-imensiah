# Fila de Tarefas 2INTEL

**Atualizacao**: 2026-03-26
**Modo**: AUTONOMO TOTAL - executar todas as tarefas sem aguardar aprovacao

---

## TAREFAS CONCLUIDAS

| Tarefa | Commit | Resumo |
|--------|--------|--------|
| Recalcular Circulos | 620de30 | 6647 contatos, C1=5, C2=6, C3=44, C4=378, C5=6266 |
| Auto-Tags | 620de30 | 1242 contatos, 1526 tags (c-level, diretor, gerente, etc) |
| Verificar Duplicados | 620de30 | 15 email, 4 phone, 20 name groups |
| Briefing Context | 698214e | briefing_context.py + 5 endpoints |
| Engajamento | merged | engajamento.py + 4 endpoints |
| Duplicados Service | 8c93930 | duplicados.py + Levenshtein + 3 endpoints |

---

## NOVAS TAREFAS (Executar em ordem)

### Tarefa 1: Gmail Sync Automatico

**Status**: PENDENTE
**Prioridade**: CRITICA

**Objetivo**: Sincronizar emails do Gmail para popular interacoes dos contatos. 93.5% dos contatos estao em C5 por falta de dados.

**Arquivos existentes**:
- `app/integrations/gmail.py` - GmailIntegration class (OAuth, list_messages, get_message)
- `app/database.py` - get_db()

**Criar**: `app/services/gmail_sync.py`

```python
"""
Gmail Sync Service
Sincroniza emails do Gmail com contatos do INTEL
"""
from datetime import datetime, timedelta
from app.integrations.gmail import GmailIntegration
from app.database import get_db

class GmailSyncService:
    def __init__(self):
        self.gmail = GmailIntegration()

    async def sync_contact_emails(self, contact_id: int, email: str, access_token: str):
        """Busca emails trocados com um contato e atualiza interacoes"""
        # 1. Buscar mensagens de/para o email
        # 2. Contar total de interacoes
        # 3. Pegar data do ultimo email
        # 4. Atualizar contact: ultimo_contato, total_interacoes
        pass

    async def sync_all_contacts(self, access_token: str, months: int = 12):
        """Sincroniza todos os contatos que tem email"""
        # 1. SELECT id, emails FROM contacts WHERE emails IS NOT NULL
        # 2. Para cada contato, chamar sync_contact_emails
        # 3. Retornar relatorio
        pass
```

**Endpoints a criar** em `app/main.py`:

```python
@app.post("/api/gmail/sync")
async def gmail_sync_all(request: Request):
    """Inicia sync de todos os contatos"""
    # Pegar access_token da sessao ou DB
    # Chamar GmailSyncService.sync_all_contacts()
    pass

@app.post("/api/gmail/sync/{contact_id}")
async def gmail_sync_contact(contact_id: int, request: Request):
    """Sync emails de um contato especifico"""
    pass

@app.get("/api/gmail/status")
async def gmail_sync_status():
    """Status da ultima sincronizacao"""
    pass
```

**Tabela para criar** (se nao existir):

```sql
CREATE TABLE IF NOT EXISTS email_interactions (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    message_id VARCHAR(255) UNIQUE,
    direction VARCHAR(20), -- 'incoming' ou 'outgoing'
    subject TEXT,
    snippet TEXT,
    email_date TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**Criterios de aceite**:
- [ ] Service gmail_sync.py criado
- [ ] Endpoints funcionando
- [ ] Atualiza ultimo_contato e total_interacoes
- [ ] Recalcula circulos apos sync

---

### Tarefa 2: WhatsApp Sync Automatico

**Status**: PENDENTE
**Prioridade**: CRITICA

**Objetivo**: Processar mensagens do WhatsApp (Evolution API) e atualizar interacoes.

**Arquivos existentes**:
- `app/integrations/whatsapp.py` - WhatsAppIntegration class
  - `get_all_chats()` - lista todas conversas
  - `get_messages_for_chat(phone)` - mensagens de um numero
  - `parse_stored_message()` - parser de mensagem

**Criar**: `app/services/whatsapp_sync.py`

```python
"""
WhatsApp Sync Service
Sincroniza mensagens do WhatsApp com contatos do INTEL
"""
from app.integrations.whatsapp import WhatsAppIntegration
from app.database import get_db

class WhatsAppSyncService:
    def __init__(self):
        self.wa = WhatsAppIntegration()

    async def sync_all_chats(self):
        """
        1. Buscar todos os chats via get_all_chats()
        2. Para cada chat com _phone:
           - Buscar contato pelo telefone
           - Se encontrar, atualizar ultimo_contato e total_interacoes
           - Salvar mensagens na tabela whatsapp_messages
        """
        pass

    async def process_webhook(self, payload: dict):
        """
        Processar webhook do Evolution API em tempo real
        Chamado quando nova mensagem chega
        """
        pass
```

**Endpoint de webhook** (verificar se existe em main.py):

```python
@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    """Webhook do Evolution API para mensagens em tempo real"""
    payload = await request.json()
    service = WhatsAppSyncService()
    await service.process_webhook(payload)
    return {"status": "ok"}
```

**Tabela para criar**:

```sql
CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    phone VARCHAR(20),
    message_id VARCHAR(255) UNIQUE,
    direction VARCHAR(20),
    content TEXT,
    message_type VARCHAR(20),
    message_date TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**Criterios de aceite**:
- [ ] Service whatsapp_sync.py criado
- [ ] Endpoint webhook funcionando
- [ ] Sync manual de chats existentes
- [ ] Atualiza ultimo_contato e total_interacoes

---

### Tarefa 3: Google Calendar Integration

**Status**: PENDENTE
**Prioridade**: ALTA

**Objetivo**: Integrar com Google Calendar para mostrar agenda no dashboard.

**Criar**: `app/integrations/google_calendar.py`

```python
"""
Google Calendar Integration
OAuth ja configurado - usar mesmo client_id/secret
"""
import os
import httpx
from datetime import datetime, timedelta
from typing import Dict, List, Any

class GoogleCalendarIntegration:
    CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

    # Adicionar scope ao OAuth existente:
    # "https://www.googleapis.com/auth/calendar.readonly"

    def __init__(self):
        self.client_id = os.getenv("GOOGLE_CLIENT_ID")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    async def list_events(
        self,
        access_token: str,
        time_min: datetime = None,
        time_max: datetime = None,
        max_results: int = 10
    ) -> List[Dict]:
        """Lista eventos do calendario"""
        pass

    async def get_today_events(self, access_token: str) -> List[Dict]:
        """Eventos de hoje para o dashboard"""
        pass

    async def get_upcoming_events(self, access_token: str, days: int = 7) -> List[Dict]:
        """Proximos eventos"""
        pass
```

**Endpoints**:

```python
@app.get("/api/calendar/events")
async def calendar_events(request: Request, days: int = 7):
    """Lista eventos dos proximos X dias"""
    pass

@app.get("/api/calendar/today")
async def calendar_today(request: Request):
    """Eventos de hoje (para dashboard)"""
    pass
```

**Atualizar OAuth** em `app/auth.py`:
- Adicionar scope: `https://www.googleapis.com/auth/calendar.readonly`

**Criterios de aceite**:
- [ ] Integration google_calendar.py criado
- [ ] Endpoints funcionando
- [ ] Retorna eventos formatados para UI

---

### Tarefa 4: Google Tasks Integration

**Status**: PENDENTE
**Prioridade**: MEDIA

**Objetivo**: Sincronizar tarefas com Google Tasks (bi-direcional).

**Criar**: `app/integrations/google_tasks.py`

```python
"""
Google Tasks Integration
Sync bi-direcional de tarefas
"""
import os
import httpx
from typing import Dict, List, Any

class GoogleTasksIntegration:
    TASKS_API_BASE = "https://tasks.googleapis.com/tasks/v1"

    # Scope: "https://www.googleapis.com/auth/tasks"

    async def list_task_lists(self, access_token: str) -> List[Dict]:
        """Lista todas as listas de tarefas"""
        pass

    async def list_tasks(self, access_token: str, tasklist_id: str = "@default") -> List[Dict]:
        """Lista tarefas de uma lista"""
        pass

    async def create_task(self, access_token: str, title: str, notes: str = None, due: datetime = None) -> Dict:
        """Cria nova tarefa"""
        pass

    async def complete_task(self, access_token: str, task_id: str) -> Dict:
        """Marca tarefa como concluida"""
        pass
```

**Endpoints**:

```python
@app.get("/api/tasks")
async def list_tasks(request: Request):
    pass

@app.post("/api/tasks")
async def create_task(request: Request):
    pass

@app.put("/api/tasks/{task_id}/complete")
async def complete_task(task_id: str, request: Request):
    pass
```

**Criterios de aceite**:
- [ ] Integration google_tasks.py criado
- [ ] CRUD de tarefas funcionando
- [ ] Sync bi-direcional

---

### Tarefa 5: Resolver Duplicados

**Status**: PENDENTE
**Prioridade**: BAIXA

**Objetivo**: Merge automatico dos 42 duplicados com score > 0.9

**Script**: `scripts/merge_duplicates.py`

```python
# 1. GET /api/contacts/duplicates?threshold=0.9
# 2. Para cada par com score > 0.9:
#    - Manter o mais completo (mais campos preenchidos)
#    - POST /api/contacts/merge {keep_id, merge_id}
# 3. Gerar relatorio
```

---

## INSTRUCOES DE EXECUCAO

1. **Criar branch**: `git checkout -b feature/intel-integrations`
2. **Executar tarefas em ordem** (1 -> 5)
3. **Testar cada uma** antes de prosseguir
4. **Commit por tarefa**: `git commit -m "feat(intel): Gmail sync service"`
5. **Ao finalizar**: Push e criar PR ou merge direto

## AUTONOMIA

- NAO aguardar aprovacao entre tarefas
- Se encontrar erro, tentar resolver e continuar
- Documentar decisoes no commit
- Atualizar este arquivo conforme progresso

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-26 | Recalculo + Tags + Duplicados | **CONCLUIDO** |
| 2026-03-25 | Todas 5 tarefas INTEL | **MERGED** |
