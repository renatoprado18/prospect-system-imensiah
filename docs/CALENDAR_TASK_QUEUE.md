# Fila de Tarefas 2INTEL - Google Calendar Integration

**Atualizacao**: 2026-03-27
**Modo**: AUTONOMO TOTAL - executar TODAS as tarefas sem parar
**Prioridade**: Executar APÓS concluir AI Avançado

---

## INSTRUCOES GERAIS

- NAO perguntar nada, apenas executar
- Se encontrar erro, resolver e continuar
- Commit apos cada tarefa concluida
- Push direto em main
- Atualizar este arquivo marcando tarefas como CONCLUIDAS

---

## TAREFA 1: Criar Tabelas do Calendar

**Status**: PENDENTE
**Prioridade**: CRITICA

### 1.1 Executar no Neon (SQL)

```sql
-- Tabela principal de eventos
CREATE TABLE IF NOT EXISTS calendar_events (
    id SERIAL PRIMARY KEY,
    google_event_id TEXT UNIQUE NOT NULL,
    summary TEXT NOT NULL,
    description TEXT,
    location TEXT,
    start_datetime TIMESTAMP NOT NULL,
    end_datetime TIMESTAMP NOT NULL,
    all_day BOOLEAN DEFAULT FALSE,
    timezone TEXT DEFAULT 'America/Sao_Paulo',
    recurring_event_id TEXT,
    recurrence_rule TEXT,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    prospect_id INTEGER REFERENCES prospects(id) ON DELETE SET NULL,
    ai_suggestion_id INTEGER,
    conference_url TEXT,
    conference_type TEXT,
    attendees JSONB DEFAULT '[]',
    status TEXT DEFAULT 'confirmed',
    etag TEXT,
    source TEXT DEFAULT 'google',
    last_synced_at TIMESTAMP,
    local_only BOOLEAN DEFAULT FALSE,
    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_google_id ON calendar_events(google_event_id);
CREATE INDEX IF NOT EXISTS idx_calendar_events_contact ON calendar_events(contact_id);
CREATE INDEX IF NOT EXISTS idx_calendar_events_start ON calendar_events(start_datetime);
CREATE INDEX IF NOT EXISTS idx_calendar_events_source ON calendar_events(source);

-- Estado de sincronização
CREATE TABLE IF NOT EXISTS calendar_sync_state (
    id SERIAL PRIMARY KEY,
    google_account_email TEXT UNIQUE NOT NULL,
    calendar_id TEXT DEFAULT 'primary',
    sync_token TEXT,
    last_full_sync TIMESTAMP,
    last_incremental_sync TIMESTAMP,
    events_synced INTEGER DEFAULT 0,
    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 1.2 Adicionar em database.py (função init_db)

Adicionar criação das tabelas na função `init_db()`.

**Commit**: `git commit -m "Add calendar_events and calendar_sync_state tables"`

---

## TAREFA 2: Atualizar OAuth Scope

**Status**: PENDENTE
**Prioridade**: CRITICA

### Modificar `app/integrations/google_contacts.py`

Trocar scope de `calendar.readonly` para `calendar` (full access):

```python
# Localizar CONTACTS_SCOPES e adicionar/modificar:
CONTACTS_SCOPES = [
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",  # Full access (era .readonly)
]
```

**Commit**: `git commit -m "Upgrade Google Calendar scope to full access"`

---

## TAREFA 3: Adicionar Métodos de Escrita no google_calendar.py

**Status**: PENDENTE
**Prioridade**: CRITICA

### Modificar `app/integrations/google_calendar.py`

Adicionar os métodos abaixo na classe ou como funções:

```python
async def create_calendar_event(
    access_token: str,
    summary: str,
    start_datetime: datetime,
    end_datetime: datetime,
    description: str = None,
    attendees: List[str] = None,
    create_meet: bool = True,
    calendar_id: str = "primary"
) -> Dict:
    """Cria evento no Google Calendar"""
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

    event_body = {
        "summary": summary,
        "start": {
            "dateTime": start_datetime.isoformat(),
            "timeZone": "America/Sao_Paulo"
        },
        "end": {
            "dateTime": end_datetime.isoformat(),
            "timeZone": "America/Sao_Paulo"
        }
    }

    if description:
        event_body["description"] = description

    if attendees:
        event_body["attendees"] = [{"email": email} for email in attendees]

    if create_meet:
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": f"meet-{datetime.now().timestamp()}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"}
            }
        }

    params = {}
    if create_meet:
        params["conferenceDataVersion"] = 1

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            json=event_body,
            timeout=30.0
        )

        if response.status_code in [200, 201]:
            return response.json()
        else:
            raise Exception(f"Erro ao criar evento: {response.status_code} - {response.text}")


async def update_calendar_event(
    access_token: str,
    event_id: str,
    updates: Dict,
    calendar_id: str = "primary"
) -> Dict:
    """Atualiza evento existente"""
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}"

    async with httpx.AsyncClient() as client:
        response = await client.patch(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            json=updates,
            timeout=30.0
        )

        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Erro ao atualizar evento: {response.status_code}")


async def delete_calendar_event(
    access_token: str,
    event_id: str,
    calendar_id: str = "primary"
) -> bool:
    """Deleta evento do Google Calendar"""
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}"

    async with httpx.AsyncClient() as client:
        response = await client.delete(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0
        )

        return response.status_code == 204


async def list_events_incremental(
    access_token: str,
    sync_token: str = None,
    calendar_id: str = "primary",
    max_results: int = 100
) -> Dict:
    """Lista eventos com sync incremental"""
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

    params = {
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime"
    }

    if sync_token:
        params["syncToken"] = sync_token
    else:
        # Full sync - eventos dos últimos 30 dias até 90 dias no futuro
        params["timeMin"] = (datetime.now() - timedelta(days=30)).isoformat() + "Z"
        params["timeMax"] = (datetime.now() + timedelta(days=90)).isoformat() + "Z"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0
        )

        if response.status_code == 200:
            data = response.json()
            return {
                "events": data.get("items", []),
                "nextSyncToken": data.get("nextSyncToken"),
                "nextPageToken": data.get("nextPageToken"),
                "fullSyncRequired": False
            }
        elif response.status_code == 410:
            # Sync token inválido - precisa full sync
            return {"events": [], "fullSyncRequired": True}
        else:
            raise Exception(f"Erro ao listar eventos: {response.status_code}")
```

**Commit**: `git commit -m "Add create/update/delete methods to Google Calendar integration"`

---

## TAREFA 4: Criar CalendarSyncService

**Status**: PENDENTE
**Prioridade**: ALTA

### Criar `app/services/calendar_sync.py`

```python
"""
Calendar Sync Service - Sincronização bidirecional com Google Calendar
"""
from typing import Dict, List
from datetime import datetime
from database import get_db
from integrations.google_calendar import (
    list_events_incremental,
    create_calendar_event,
    update_calendar_event,
    delete_calendar_event
)
from integrations.google_contacts import get_valid_token


class CalendarSyncService:
    """Serviço de sincronização do Calendar"""

    async def get_access_token(self, email: str) -> str:
        """Obtém token válido para a conta"""
        return await get_valid_token(email)

    async def full_sync(self, google_account_email: str) -> Dict:
        """Sincronização completa inicial"""
        access_token = await self.get_access_token(google_account_email)

        result = await list_events_incremental(
            access_token=access_token,
            sync_token=None  # Força full sync
        )

        stats = {"created": 0, "updated": 0}

        with get_db() as conn:
            cursor = conn.cursor()

            for event in result.get("events", []):
                existed = await self._upsert_event(cursor, event, google_account_email)
                stats["updated" if existed else "created"] += 1

            # Salvar sync token
            if result.get("nextSyncToken"):
                cursor.execute("""
                    INSERT INTO calendar_sync_state (google_account_email, sync_token, last_full_sync, events_synced)
                    VALUES (%s, %s, NOW(), %s)
                    ON CONFLICT (google_account_email) DO UPDATE SET
                        sync_token = EXCLUDED.sync_token,
                        last_full_sync = NOW(),
                        events_synced = EXCLUDED.events_synced
                """, (google_account_email, result["nextSyncToken"], stats["created"] + stats["updated"]))

            conn.commit()

        return stats

    async def incremental_sync(self, google_account_email: str) -> Dict:
        """Sincronização incremental usando sync token"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT sync_token FROM calendar_sync_state
                WHERE google_account_email = %s
            """, (google_account_email,))
            row = cursor.fetchone()

        sync_token = row["sync_token"] if row else None

        if not sync_token:
            return await self.full_sync(google_account_email)

        access_token = await self.get_access_token(google_account_email)

        result = await list_events_incremental(
            access_token=access_token,
            sync_token=sync_token
        )

        if result.get("fullSyncRequired"):
            return await self.full_sync(google_account_email)

        stats = {"created": 0, "updated": 0, "deleted": 0}

        with get_db() as conn:
            cursor = conn.cursor()

            for event in result.get("events", []):
                if event.get("status") == "cancelled":
                    cursor.execute("""
                        DELETE FROM calendar_events WHERE google_event_id = %s
                    """, (event["id"],))
                    stats["deleted"] += 1
                else:
                    existed = await self._upsert_event(cursor, event, google_account_email)
                    stats["updated" if existed else "created"] += 1

            # Atualizar sync token
            if result.get("nextSyncToken"):
                cursor.execute("""
                    UPDATE calendar_sync_state
                    SET sync_token = %s, last_incremental_sync = NOW()
                    WHERE google_account_email = %s
                """, (result["nextSyncToken"], google_account_email))

            conn.commit()

        return stats

    async def _upsert_event(self, cursor, google_event: Dict, email: str) -> bool:
        """Insere ou atualiza evento local. Retorna True se existia."""
        event_id = google_event["id"]

        # Extrair dados
        start = google_event.get("start", {})
        end = google_event.get("end", {})

        start_dt = start.get("dateTime") or start.get("date")
        end_dt = end.get("dateTime") or end.get("date")
        all_day = "date" in start and "dateTime" not in start

        # Verificar se existe
        cursor.execute("SELECT id FROM calendar_events WHERE google_event_id = %s", (event_id,))
        existing = cursor.fetchone()

        conference_url = None
        if google_event.get("conferenceData"):
            entry_points = google_event["conferenceData"].get("entryPoints", [])
            for ep in entry_points:
                if ep.get("entryPointType") == "video":
                    conference_url = ep.get("uri")
                    break

        if existing:
            cursor.execute("""
                UPDATE calendar_events SET
                    summary = %s, description = %s, location = %s,
                    start_datetime = %s, end_datetime = %s, all_day = %s,
                    attendees = %s, status = %s, conference_url = %s,
                    etag = %s, last_synced_at = NOW(), atualizado_em = NOW()
                WHERE google_event_id = %s
            """, (
                google_event.get("summary", "Sem título"),
                google_event.get("description"),
                google_event.get("location"),
                start_dt, end_dt, all_day,
                google_event.get("attendees", []),
                google_event.get("status", "confirmed"),
                conference_url,
                google_event.get("etag"),
                event_id
            ))
            return True
        else:
            cursor.execute("""
                INSERT INTO calendar_events
                (google_event_id, summary, description, location, start_datetime, end_datetime,
                 all_day, attendees, status, conference_url, etag, source, last_synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'google', NOW())
            """, (
                event_id,
                google_event.get("summary", "Sem título"),
                google_event.get("description"),
                google_event.get("location"),
                start_dt, end_dt, all_day,
                google_event.get("attendees", []),
                google_event.get("status", "confirmed"),
                conference_url,
                google_event.get("etag")
            ))
            return False

    async def push_local_event(self, event_id: int) -> Dict:
        """Envia evento local para o Google Calendar"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM calendar_events WHERE id = %s", (event_id,))
            event = cursor.fetchone()

            if not event:
                raise Exception("Evento não encontrado")

            if event["google_event_id"] and not event["local_only"]:
                raise Exception("Evento já sincronizado")

        # Buscar conta Google para obter token
        # (usar primeira conta disponível ou conta do usuário)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT email FROM google_accounts LIMIT 1")
            account = cursor.fetchone()

        if not account:
            raise Exception("Nenhuma conta Google configurada")

        access_token = await self.get_access_token(account["email"])

        # Criar no Google
        google_event = await create_calendar_event(
            access_token=access_token,
            summary=event["summary"],
            start_datetime=event["start_datetime"],
            end_datetime=event["end_datetime"],
            description=event["description"],
            create_meet=True
        )

        # Atualizar local com ID do Google
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE calendar_events SET
                    google_event_id = %s,
                    conference_url = %s,
                    local_only = FALSE,
                    last_synced_at = NOW()
                WHERE id = %s
            """, (
                google_event["id"],
                google_event.get("hangoutLink"),
                event_id
            ))
            conn.commit()

        return google_event


_calendar_sync = None

def get_calendar_sync() -> CalendarSyncService:
    global _calendar_sync
    if _calendar_sync is None:
        _calendar_sync = CalendarSyncService()
    return _calendar_sync
```

**Commit**: `git commit -m "Add CalendarSyncService for bidirectional sync"`

---

## TAREFA 5: Criar CalendarEventsService

**Status**: PENDENTE
**Prioridade**: ALTA

### Criar `app/services/calendar_events.py`

```python
"""
Calendar Events Service - CRUD de eventos
"""
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db
from services.calendar_sync import get_calendar_sync


class CalendarEventsService:
    """Serviço de CRUD de eventos"""

    def create_event(
        self,
        summary: str,
        start_datetime: datetime,
        end_datetime: datetime,
        description: str = None,
        contact_id: int = None,
        prospect_id: int = None,
        create_in_google: bool = True
    ) -> Dict:
        """Cria novo evento"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Criar evento local primeiro
            cursor.execute("""
                INSERT INTO calendar_events
                (google_event_id, summary, description, start_datetime, end_datetime,
                 contact_id, prospect_id, source, local_only)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'system', %s)
                RETURNING id
            """, (
                f"local-{datetime.now().timestamp()}",  # ID temporário
                summary, description,
                start_datetime, end_datetime,
                contact_id, prospect_id,
                not create_in_google
            ))
            event_id = cursor.fetchone()["id"]
            conn.commit()

        # Se solicitado, enviar para Google
        if create_in_google:
            try:
                sync = get_calendar_sync()
                # Usar asyncio para chamar método async
                import asyncio
                asyncio.run(sync.push_local_event(event_id))
            except Exception as e:
                print(f"Erro ao enviar para Google: {e}")
                # Evento fica como local_only

        return self.get_event(event_id)

    def get_event(self, event_id: int) -> Optional[Dict]:
        """Busca evento por ID"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.*, c.nome as contact_name, p.nome as prospect_name
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                LEFT JOIN prospects p ON p.id = e.prospect_id
                WHERE e.id = %s
            """, (event_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_event(self, event_id: int, updates: Dict) -> Dict:
        """Atualiza evento"""
        allowed_fields = ["summary", "description", "location", "start_datetime",
                         "end_datetime", "contact_id", "prospect_id"]

        update_parts = []
        params = []
        for field in allowed_fields:
            if field in updates:
                update_parts.append(f"{field} = %s")
                params.append(updates[field])

        if not update_parts:
            return self.get_event(event_id)

        params.append(event_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE calendar_events
                SET {', '.join(update_parts)}, atualizado_em = NOW()
                WHERE id = %s
                RETURNING google_event_id, local_only
            """, params)
            result = cursor.fetchone()
            conn.commit()

            # Se tem google_event_id e não é local_only, atualizar no Google
            if result and result["google_event_id"] and not result["local_only"]:
                # TODO: Implementar update no Google
                pass

        return self.get_event(event_id)

    def delete_event(self, event_id: int, delete_from_google: bool = True) -> bool:
        """Deleta evento"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar dados para deletar no Google
            cursor.execute("""
                SELECT google_event_id, local_only FROM calendar_events WHERE id = %s
            """, (event_id,))
            event = cursor.fetchone()

            if not event:
                return False

            # Deletar localmente
            cursor.execute("DELETE FROM calendar_events WHERE id = %s", (event_id,))
            conn.commit()

            # Se solicitado e tem ID do Google, deletar lá também
            if delete_from_google and event["google_event_id"] and not event["local_only"]:
                # TODO: Implementar delete no Google
                pass

            return True

    def get_events_for_contact(self, contact_id: int, limit: int = 20) -> List[Dict]:
        """Lista eventos de um contato"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM calendar_events
                WHERE contact_id = %s
                ORDER BY start_datetime DESC
                LIMIT %s
            """, (contact_id, limit))
            return [dict(row) for row in cursor.fetchall()]

    def get_events_for_period(
        self,
        start: datetime,
        end: datetime,
        contact_id: int = None
    ) -> List[Dict]:
        """Lista eventos em um período"""
        with get_db() as conn:
            cursor = conn.cursor()

            query = """
                SELECT e.*, c.nome as contact_name
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                WHERE e.start_datetime >= %s AND e.start_datetime <= %s
            """
            params = [start, end]

            if contact_id:
                query += " AND e.contact_id = %s"
                params.append(contact_id)

            query += " ORDER BY e.start_datetime ASC"

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def link_to_contact(self, event_id: int, contact_id: int) -> Dict:
        """Vincula evento a um contato"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE calendar_events SET contact_id = %s WHERE id = %s
            """, (contact_id, event_id))
            conn.commit()
        return self.get_event(event_id)


_calendar_events = None

def get_calendar_events() -> CalendarEventsService:
    global _calendar_events
    if _calendar_events is None:
        _calendar_events = CalendarEventsService()
    return _calendar_events
```

**Commit**: `git commit -m "Add CalendarEventsService for event CRUD"`

---

## TAREFA 6: Endpoints de Calendar em main.py

**Status**: PENDENTE
**Prioridade**: ALTA

### Adicionar em main.py

```python
# ========== CALENDAR EVENTS ==========

@app.post("/api/calendar/events")
async def create_calendar_event_endpoint(request: Request):
    """Cria evento no calendário"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    data = await request.json()
    from services.calendar_events import get_calendar_events

    event = get_calendar_events().create_event(
        summary=data["summary"],
        start_datetime=datetime.fromisoformat(data["start_datetime"]),
        end_datetime=datetime.fromisoformat(data["end_datetime"]),
        description=data.get("description"),
        contact_id=data.get("contact_id"),
        prospect_id=data.get("prospect_id"),
        create_in_google=data.get("create_in_google", True)
    )
    return event


@app.get("/api/calendar/events/{event_id}")
async def get_calendar_event_endpoint(request: Request, event_id: int):
    """Busca evento por ID"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    from services.calendar_events import get_calendar_events
    event = get_calendar_events().get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Evento não encontrado")
    return event


@app.put("/api/calendar/events/{event_id}")
async def update_calendar_event_endpoint(request: Request, event_id: int):
    """Atualiza evento"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    data = await request.json()
    from services.calendar_events import get_calendar_events

    event = get_calendar_events().update_event(event_id, data)
    return event


@app.delete("/api/calendar/events/{event_id}")
async def delete_calendar_event_endpoint(request: Request, event_id: int):
    """Deleta evento"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    from services.calendar_events import get_calendar_events
    success = get_calendar_events().delete_event(event_id)
    if not success:
        raise HTTPException(status_code=404, detail="Evento não encontrado")
    return {"deleted": True}


@app.get("/api/contacts/{contact_id}/calendar")
async def get_contact_calendar(request: Request, contact_id: int):
    """Lista eventos de um contato"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    from services.calendar_events import get_calendar_events
    events = get_calendar_events().get_events_for_contact(contact_id)
    return {"events": events}


@app.post("/api/calendar/sync")
async def trigger_calendar_sync(request: Request):
    """Dispara sincronização manual do calendário"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    from services.calendar_sync import get_calendar_sync

    # Buscar conta Google
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM google_accounts LIMIT 1")
        account = cursor.fetchone()

    if not account:
        raise HTTPException(status_code=400, detail="Nenhuma conta Google configurada")

    sync = get_calendar_sync()
    stats = await sync.incremental_sync(account["email"])
    return {"status": "completed", "stats": stats}


@app.get("/api/calendar/sync/status")
async def get_calendar_sync_status(request: Request):
    """Retorna status da sincronização"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM calendar_sync_state ORDER BY last_incremental_sync DESC LIMIT 1
        """)
        state = cursor.fetchone()
        return dict(state) if state else {"status": "never_synced"}
```

**Commit**: `git commit -m "Add Calendar CRUD and sync endpoints"`

---

## TAREFA 7: Criar CalendarAIService

**Status**: PENDENTE
**Prioridade**: MEDIA

### Criar `app/services/calendar_ai.py`

```python
"""
Calendar AI Service - Sugestões automáticas de reunião baseadas em health
"""
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db
from services.calendar_events import get_calendar_events
from services.calendar_sync import get_calendar_sync


class CalendarAIService:
    """Serviço de sugestões de calendário baseadas em AI"""

    def generate_calendar_suggestions(self, limit: int = 10) -> List[Dict]:
        """Gera sugestões de reunião para contatos com health baixo"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Contatos com health baixo sem reunião agendada
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, c.circulo, c.health_score,
                       c.ultimo_contato, c.contexto,
                       EXTRACT(DAY FROM NOW() - c.ultimo_contato)::int as dias_sem_contato
                FROM contacts c
                WHERE COALESCE(c.circulo, 5) <= 3
                AND COALESCE(c.health_score, 50) < 40
                AND c.id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'calendar_reminder' AND status = 'pending'
                )
                AND c.id NOT IN (
                    SELECT contact_id FROM calendar_events
                    WHERE contact_id IS NOT NULL
                    AND start_datetime > NOW()
                    AND start_datetime < NOW() + INTERVAL '14 days'
                )
                ORDER BY c.circulo ASC, c.health_score ASC
                LIMIT %s
            """, (limit,))

            contacts = cursor.fetchall()
            suggestions = []

            for contact in contacts:
                urgency = self._calculate_urgency(
                    contact["health_score"],
                    contact["circulo"],
                    contact["dias_sem_contato"] or 0
                )

                suggested_dt = self._suggest_datetime(urgency)

                suggestion = {
                    "contact_id": contact["id"],
                    "tipo": "calendar_reminder",
                    "titulo": f"Agendar reunião com {contact['nome']}",
                    "descricao": f"Health {contact['health_score']}% - {contact['dias_sem_contato'] or 'N/A'} dias sem contato",
                    "acao_sugerida": {
                        "action": "create_calendar_event",
                        "suggested_datetime": suggested_dt.isoformat(),
                        "duration_minutes": 30,
                        "contact_name": contact["nome"],
                        "contact_empresa": contact["empresa"]
                    },
                    "contexto": {
                        "health": contact["health_score"],
                        "circulo": contact["circulo"],
                        "dias_sem_contato": contact["dias_sem_contato"],
                        "urgency": urgency
                    },
                    "prioridade": 9 if urgency == "high" else (7 if urgency == "medium" else 5)
                }

                # Inserir no banco
                cursor.execute("""
                    INSERT INTO ai_suggestions
                    (contact_id, tipo, titulo, descricao, acao_sugerida, contexto, prioridade, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL '7 days')
                    RETURNING id
                """, (
                    suggestion["contact_id"],
                    suggestion["tipo"],
                    suggestion["titulo"],
                    suggestion["descricao"],
                    json.dumps(suggestion["acao_sugerida"]),
                    json.dumps(suggestion["contexto"]),
                    suggestion["prioridade"]
                ))
                suggestion["id"] = cursor.fetchone()["id"]
                suggestions.append(suggestion)

            conn.commit()
            return suggestions

    def _calculate_urgency(self, health: int, circulo: int, dias: int) -> str:
        """Calcula urgência baseado em health e círculo"""
        if circulo == 1 and (health < 20 or dias > 30):
            return "high"
        if circulo <= 2 and (health < 30 or dias > 45):
            return "high"
        if health < 40 or dias > 60:
            return "medium"
        return "low"

    def _suggest_datetime(self, urgency: str) -> datetime:
        """Sugere datetime baseado na urgência"""
        now = datetime.now()

        if urgency == "high":
            days_ahead = 1
        elif urgency == "medium":
            days_ahead = 3
        else:
            days_ahead = 7

        suggested = now + timedelta(days=days_ahead)

        # Ajustar para horário comercial (10h)
        suggested = suggested.replace(hour=10, minute=0, second=0, microsecond=0)

        # Se cair no fim de semana, mover para segunda
        while suggested.weekday() >= 5:
            suggested += timedelta(days=1)

        return suggested

    async def accept_and_create_event(self, suggestion_id: int) -> Dict:
        """Aceita sugestão e cria evento no Google Calendar"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar sugestão
            cursor.execute("""
                SELECT s.*, c.nome as contact_name
                FROM ai_suggestions s
                JOIN contacts c ON c.id = s.contact_id
                WHERE s.id = %s AND s.status = 'pending'
            """, (suggestion_id,))
            suggestion = cursor.fetchone()

            if not suggestion:
                raise Exception("Sugestão não encontrada ou já processada")

            acao = suggestion["acao_sugerida"]
            if isinstance(acao, str):
                acao = json.loads(acao)

        # Criar evento
        events_svc = get_calendar_events()
        event = events_svc.create_event(
            summary=f"Reunião com {suggestion['contact_name']}",
            start_datetime=datetime.fromisoformat(acao["suggested_datetime"]),
            end_datetime=datetime.fromisoformat(acao["suggested_datetime"]) + timedelta(minutes=acao.get("duration_minutes", 30)),
            description=f"Reunião agendada via sugestão AI\n\n{suggestion['descricao']}",
            contact_id=suggestion["contact_id"],
            create_in_google=True
        )

        # Marcar sugestão como aceita
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE ai_suggestions SET status = 'accepted' WHERE id = %s
            """, (suggestion_id,))

            # Atualizar último contato do contact (agendamento conta como interação)
            cursor.execute("""
                UPDATE contacts SET ultimo_contato = NOW() WHERE id = %s
            """, (suggestion["contact_id"],))

            conn.commit()

        return event


_calendar_ai = None

def get_calendar_ai() -> CalendarAIService:
    global _calendar_ai
    if _calendar_ai is None:
        _calendar_ai = CalendarAIService()
    return _calendar_ai
```

### Adicionar endpoints em main.py

```python
@app.get("/api/ai/calendar-suggestions")
async def get_calendar_suggestions(request: Request, limit: int = 10):
    """Lista sugestões de reunião da AI"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.*, c.nome as contact_name, c.foto_url as contact_foto
            FROM ai_suggestions s
            JOIN contacts c ON c.id = s.contact_id
            WHERE s.tipo = 'calendar_reminder' AND s.status = 'pending'
            ORDER BY s.prioridade DESC
            LIMIT %s
        """, (limit,))
        return {"suggestions": [dict(row) for row in cursor.fetchall()]}


@app.post("/api/ai/calendar-suggestions/{suggestion_id}/create-event")
async def accept_calendar_suggestion(request: Request, suggestion_id: int):
    """Aceita sugestão e cria evento"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    from services.calendar_ai import get_calendar_ai
    event = await get_calendar_ai().accept_and_create_event(suggestion_id)
    return {"status": "created", "event": event}


@app.post("/api/ai/generate-calendar-suggestions")
async def generate_calendar_suggestions(request: Request):
    """Gera novas sugestões de reunião"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)

    from services.calendar_ai import get_calendar_ai
    suggestions = get_calendar_ai().generate_calendar_suggestions()
    return {"generated": len(suggestions), "suggestions": suggestions}
```

**Commit**: `git commit -m "Add CalendarAIService for automatic meeting suggestions"`

---

## APOS COMPLETAR TODAS

```bash
git push origin main
```

Atualizar este arquivo marcando todas como **CONCLUIDAS**.

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| - | Criar tabelas calendar | PENDENTE |
| - | Atualizar OAuth scope | PENDENTE |
| - | Métodos de escrita Google Calendar | PENDENTE |
| - | CalendarSyncService | PENDENTE |
| - | CalendarEventsService | PENDENTE |
| - | Endpoints Calendar | PENDENTE |
| - | CalendarAIService | PENDENTE |
