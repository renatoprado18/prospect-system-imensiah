"""
Calendar Sync Service - Sincronizacao bidirecional com Google Calendar

Funcionalidades:
- Full sync inicial
- Incremental sync com tokens
- Push de eventos locais para Google
- Deteccao de conflitos
"""
import json
from typing import Dict, List, Optional
from datetime import datetime
from database import get_db
from integrations.google_calendar import (
    get_calendar_integration,
    list_events_incremental,
    create_calendar_event,
    update_calendar_event,
    delete_calendar_event
)
from integrations.google_contacts import get_valid_token


class CalendarSyncService:
    """Servico de sincronizacao do Calendar"""

    async def get_access_token(self, email: str) -> str:
        """Obtem token valido para a conta"""
        return await get_valid_token(email)

    async def full_sync(self, google_account_email: str) -> Dict:
        """Sincronizacao completa inicial"""
        access_token = await self.get_access_token(google_account_email)

        result = await list_events_incremental(
            access_token=access_token,
            sync_token=None  # Forca full sync
        )

        if "error" in result:
            return {"error": result["error"], "created": 0, "updated": 0}

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
        """Sincronizacao incremental usando sync token"""
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

        if "error" in result:
            return {"error": result["error"], "created": 0, "updated": 0, "deleted": 0}

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
        conference_type = None
        if google_event.get("conferenceData"):
            entry_points = google_event["conferenceData"].get("entryPoints", [])
            for ep in entry_points:
                if ep.get("entryPointType") == "video":
                    conference_url = ep.get("uri")
                    conference_type = google_event["conferenceData"].get("conferenceSolution", {}).get("name")
                    break

        attendees_json = json.dumps(google_event.get("attendees", []))

        # Upsert atomico via ON CONFLICT (google_event_id).
        # Antes era SELECT-then-INSERT-or-UPDATE — vulneravel a:
        # 1. Race entre SELECT e INSERT (insert duplicado)
        # 2. SERIAL sequence dessincronizada (id auto-gerado ja existia,
        #    -> calendar_events_pkey duplicate em prod 02/05/2026)
        # ON CONFLICT (google_event_id) DO UPDATE evita ambos: nao geramos
        # novo id pra row ja existente.
        cursor.execute("""
            INSERT INTO calendar_events
            (google_event_id, summary, description, location, start_datetime, end_datetime,
             all_day, attendees, status, conference_url, conference_type, etag, source, last_synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'google', NOW())
            ON CONFLICT (google_event_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                description = EXCLUDED.description,
                location = EXCLUDED.location,
                start_datetime = EXCLUDED.start_datetime,
                end_datetime = EXCLUDED.end_datetime,
                all_day = EXCLUDED.all_day,
                attendees = EXCLUDED.attendees,
                status = EXCLUDED.status,
                conference_url = EXCLUDED.conference_url,
                conference_type = EXCLUDED.conference_type,
                etag = EXCLUDED.etag,
                last_synced_at = NOW(),
                atualizado_em = NOW()
        """, (
            event_id,
            google_event.get("summary", "Sem titulo"),
            google_event.get("description"),
            google_event.get("location"),
            start_dt, end_dt, all_day,
            attendees_json,
            google_event.get("status", "confirmed"),
            conference_url,
            conference_type,
            google_event.get("etag")
        ))
        return bool(existing)

    async def push_local_event(self, event_id: int) -> Dict:
        """Envia evento local para o Google Calendar"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM calendar_events WHERE id = %s", (event_id,))
            event = cursor.fetchone()

            if not event:
                return {"error": "Evento nao encontrado"}

            event = dict(event)

            if event["google_event_id"] and not event.get("local_only"):
                if not event["google_event_id"].startswith("local-"):
                    return {"error": "Evento ja sincronizado"}

        # Buscar conta Google para obter token
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT email FROM google_accounts WHERE conectado = TRUE LIMIT 1")
            account = cursor.fetchone()

        if not account:
            return {"error": "Nenhuma conta Google configurada"}

        access_token = await self.get_access_token(account["email"])

        # Criar no Google
        attendees = None
        if event.get("attendees"):
            attendees_data = event["attendees"]
            if isinstance(attendees_data, str):
                attendees_data = json.loads(attendees_data)
            attendees = [a.get("email") for a in attendees_data if a.get("email")]

        google_event = await create_calendar_event(
            access_token=access_token,
            summary=event["summary"],
            start_datetime=event["start_datetime"],
            end_datetime=event["end_datetime"],
            description=event.get("description"),
            attendees=attendees,
            create_meet=True
        )

        if "error" in google_event:
            return google_event

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

    async def sync_event_to_google(self, event_id: int) -> Dict:
        """Sincroniza alteracoes de um evento para o Google"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM calendar_events WHERE id = %s", (event_id,))
            event = cursor.fetchone()

            if not event:
                return {"error": "Evento nao encontrado"}

            event = dict(event)

            if not event["google_event_id"] or event.get("local_only"):
                return await self.push_local_event(event_id)

        # Buscar token
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT email FROM google_accounts WHERE conectado = TRUE LIMIT 1")
            account = cursor.fetchone()

        if not account:
            return {"error": "Nenhuma conta Google configurada"}

        access_token = await self.get_access_token(account["email"])

        # Preparar atualizacoes
        updates = {
            "summary": event["summary"],
            "description": event.get("description"),
            "location": event.get("location"),
            "start": {
                "dateTime": event["start_datetime"].isoformat() if hasattr(event["start_datetime"], 'isoformat') else event["start_datetime"],
                "timeZone": event.get("timezone", "America/Sao_Paulo")
            },
            "end": {
                "dateTime": event["end_datetime"].isoformat() if hasattr(event["end_datetime"], 'isoformat') else event["end_datetime"],
                "timeZone": event.get("timezone", "America/Sao_Paulo")
            }
        }

        result = await update_calendar_event(
            access_token=access_token,
            event_id=event["google_event_id"],
            updates=updates
        )

        if "error" not in result:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE calendar_events SET last_synced_at = NOW() WHERE id = %s
                """, (event_id,))
                conn.commit()

        return result

    async def delete_from_google(self, event_id: int, scope: str = "single") -> bool:
        """Deleta evento do Google Calendar.

        scope: "single" | "future" | "all" — passado direto pra integração.
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT google_event_id, local_only FROM calendar_events WHERE id = %s
            """, (event_id,))
            event = cursor.fetchone()

            if not event or not event["google_event_id"] or event.get("local_only"):
                return True  # Nada a deletar no Google

        # Buscar token
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT email FROM google_accounts WHERE conectado = TRUE LIMIT 1")
            account = cursor.fetchone()

        if not account:
            return False

        access_token = await self.get_access_token(account["email"])

        result = await delete_calendar_event(
            access_token=access_token,
            event_id=event["google_event_id"],
            scope=scope
        )
        return bool(result.get("deleted"))

    def get_sync_status(self) -> Dict:
        """Retorna status da sincronizacao"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM calendar_sync_state ORDER BY last_incremental_sync DESC NULLS LAST LIMIT 1
            """)
            state = cursor.fetchone()

            if state:
                state = dict(state)
                for key in ["last_full_sync", "last_incremental_sync", "criado_em"]:
                    if state.get(key) and hasattr(state[key], "isoformat"):
                        state[key] = state[key].isoformat()
                return state

            return {"status": "never_synced"}


_calendar_sync = None


def get_calendar_sync() -> CalendarSyncService:
    global _calendar_sync
    if _calendar_sync is None:
        _calendar_sync = CalendarSyncService()
    return _calendar_sync
