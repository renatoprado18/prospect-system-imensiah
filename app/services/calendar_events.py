"""
Calendar Events Service - CRUD de eventos

Funcionalidades:
- Criar eventos locais e sincronizar com Google
- Buscar eventos por periodo, contato ou prospect
- Atualizar e deletar eventos
- Vincular eventos a contatos
"""
import json
import asyncio
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db
from services.calendar_sync import get_calendar_sync


class CalendarEventsService:
    """Servico de CRUD de eventos"""

    def create_event(
        self,
        summary: str,
        start_datetime: datetime,
        end_datetime: datetime,
        description: str = None,
        location: str = None,
        contact_id: int = None,
        prospect_id: int = None,
        attendees: List[Dict] = None,
        create_in_google: bool = True
    ) -> Dict:
        """Cria novo evento"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Criar evento local primeiro
            attendees_json = json.dumps(attendees or [])

            cursor.execute("""
                INSERT INTO calendar_events
                (google_event_id, summary, description, location, start_datetime, end_datetime,
                 contact_id, prospect_id, attendees, source, local_only)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'system', %s)
                RETURNING id
            """, (
                f"local-{datetime.now().timestamp()}",  # ID temporario
                summary, description, location,
                start_datetime, end_datetime,
                contact_id, prospect_id,
                attendees_json,
                not create_in_google
            ))
            event_id = cursor.fetchone()["id"]
            conn.commit()

        # Se solicitado, enviar para Google
        if create_in_google:
            try:
                sync = get_calendar_sync()
                # Usar asyncio para chamar metodo async
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Se ja existe um loop, criar task
                    asyncio.create_task(sync.push_local_event(event_id))
                else:
                    loop.run_until_complete(sync.push_local_event(event_id))
            except Exception as e:
                print(f"Erro ao enviar para Google: {e}")
                # Evento fica como local_only

        return self.get_event(event_id)

    def get_event(self, event_id: int) -> Optional[Dict]:
        """Busca evento por ID"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.*, c.nome as contact_name, c.foto_url as contact_foto,
                       p.nome as prospect_name
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                LEFT JOIN prospects p ON p.id = e.prospect_id
                WHERE e.id = %s
            """, (event_id,))
            row = cursor.fetchone()

            if row:
                event = dict(row)
                # Converter datas para ISO
                for key in ["start_datetime", "end_datetime", "last_synced_at", "criado_em", "atualizado_em"]:
                    if event.get(key) and hasattr(event[key], "isoformat"):
                        event[key] = event[key].isoformat()
                return event
            return None

    def get_event_by_google_id(self, google_event_id: str) -> Optional[Dict]:
        """Busca evento por Google Event ID"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM calendar_events WHERE google_event_id = %s
            """, (google_event_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_event(self, event_id: int, updates: Dict, sync_to_google: bool = True) -> Dict:
        """Atualiza evento"""
        allowed_fields = ["summary", "description", "location", "start_datetime",
                         "end_datetime", "contact_id", "prospect_id", "status"]

        update_parts = []
        params = []
        for field in allowed_fields:
            if field in updates:
                update_parts.append(f"{field} = %s")
                params.append(updates[field])

        if not update_parts:
            return self.get_event(event_id)

        update_parts.append("atualizado_em = NOW()")
        params.append(event_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE calendar_events
                SET {', '.join(update_parts)}
                WHERE id = %s
                RETURNING google_event_id, local_only
            """, params)
            result = cursor.fetchone()
            conn.commit()

            # Se tem google_event_id e nao e local_only, atualizar no Google
            if sync_to_google and result and result["google_event_id"] and not result.get("local_only"):
                if not result["google_event_id"].startswith("local-"):
                    try:
                        sync = get_calendar_sync()
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(sync.sync_event_to_google(event_id))
                        else:
                            loop.run_until_complete(sync.sync_event_to_google(event_id))
                    except Exception as e:
                        print(f"Erro ao sincronizar com Google: {e}")

        return self.get_event(event_id)

    def delete_event(
        self,
        event_id: int,
        delete_from_google: bool = True,
        scope: str = "single"
    ) -> bool:
        """Deleta evento.

        scope: "single" | "future" | "all" — controla recorrência no Google.
        Para "future", o local fica como está (só trunca no Google); o sync
        incremental remove as instâncias locais futuras.
        Para "all", deleta tudo localmente que aponte pra mesma série.
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar dados para deletar no Google
            cursor.execute("""
                SELECT google_event_id, local_only FROM calendar_events WHERE id = %s
            """, (event_id,))
            event = cursor.fetchone()

            if not event:
                return False

            # Se solicitado e tem ID do Google valido, deletar la tambem
            if delete_from_google and event["google_event_id"] and not event.get("local_only"):
                if not event["google_event_id"].startswith("local-"):
                    try:
                        sync = get_calendar_sync()
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(sync.delete_from_google(event_id, scope=scope))
                        else:
                            loop.run_until_complete(sync.delete_from_google(event_id, scope=scope))
                    except Exception as e:
                        print(f"Erro ao deletar do Google: {e}")

            # Deletar localmente: para "future" não removemos nada (o sync
            # vai limpar as instâncias futuras quando rodar). Para "single"
            # e "all" removemos o registro local pelo ID.
            if scope != "future":
                cursor.execute("DELETE FROM calendar_events WHERE id = %s", (event_id,))
                conn.commit()

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

            events = []
            for row in cursor.fetchall():
                event = dict(row)
                for key in ["start_datetime", "end_datetime", "last_synced_at", "criado_em", "atualizado_em"]:
                    if event.get(key) and hasattr(event[key], "isoformat"):
                        event[key] = event[key].isoformat()
                events.append(event)
            return events

    def get_events_for_prospect(self, prospect_id: int, limit: int = 20) -> List[Dict]:
        """Lista eventos de um prospect"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM calendar_events
                WHERE prospect_id = %s
                ORDER BY start_datetime DESC
                LIMIT %s
            """, (prospect_id, limit))

            events = []
            for row in cursor.fetchall():
                event = dict(row)
                for key in ["start_datetime", "end_datetime", "last_synced_at", "criado_em", "atualizado_em"]:
                    if event.get(key) and hasattr(event[key], "isoformat"):
                        event[key] = event[key].isoformat()
                events.append(event)
            return events

    def get_events_for_period(
        self,
        start: datetime,
        end: datetime,
        contact_id: int = None,
        prospect_id: int = None
    ) -> List[Dict]:
        """Lista eventos em um periodo"""
        with get_db() as conn:
            cursor = conn.cursor()

            query = """
                SELECT e.*, c.nome as contact_name, c.foto_url as contact_foto,
                       p.nome as prospect_name
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                LEFT JOIN prospects p ON p.id = e.prospect_id
                WHERE e.start_datetime >= %s AND e.start_datetime <= %s
            """
            params = [start, end]

            if contact_id:
                query += " AND e.contact_id = %s"
                params.append(contact_id)

            if prospect_id:
                query += " AND e.prospect_id = %s"
                params.append(prospect_id)

            query += " ORDER BY e.start_datetime ASC"

            cursor.execute(query, params)

            events = []
            for row in cursor.fetchall():
                event = dict(row)
                for key in ["start_datetime", "end_datetime", "last_synced_at", "criado_em", "atualizado_em"]:
                    if event.get(key) and hasattr(event[key], "isoformat"):
                        event[key] = event[key].isoformat()
                events.append(event)
            return events

    def get_today_events(self) -> List[Dict]:
        """Lista eventos de hoje"""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        return self.get_events_for_period(today, tomorrow)

    def get_upcoming_events(self, days: int = 7, limit: int = 20) -> List[Dict]:
        """Lista proximos eventos"""
        now = datetime.now()
        end_date = now + timedelta(days=days)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.*, c.nome as contact_name, c.foto_url as contact_foto,
                       p.nome as prospect_name
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                LEFT JOIN prospects p ON p.id = e.prospect_id
                WHERE e.start_datetime >= %s AND e.start_datetime <= %s
                ORDER BY e.start_datetime ASC
                LIMIT %s
            """, (now, end_date, limit))

            events = []
            for row in cursor.fetchall():
                event = dict(row)
                for key in ["start_datetime", "end_datetime", "last_synced_at", "criado_em", "atualizado_em"]:
                    if event.get(key) and hasattr(event[key], "isoformat"):
                        event[key] = event[key].isoformat()
                events.append(event)
            return events

    def link_to_contact(self, event_id: int, contact_id: int) -> Dict:
        """Vincula evento a um contato"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE calendar_events SET contact_id = %s, atualizado_em = NOW() WHERE id = %s
            """, (contact_id, event_id))
            conn.commit()
        return self.get_event(event_id)

    def link_to_prospect(self, event_id: int, prospect_id: int) -> Dict:
        """Vincula evento a um prospect"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE calendar_events SET prospect_id = %s, atualizado_em = NOW() WHERE id = %s
            """, (prospect_id, event_id))
            conn.commit()
        return self.get_event(event_id)

    def unlink_contact(self, event_id: int) -> Dict:
        """Remove vinculo de contato"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE calendar_events SET contact_id = NULL, atualizado_em = NOW() WHERE id = %s
            """, (event_id,))
            conn.commit()
        return self.get_event(event_id)

    def get_events_count(self, days: int = 30) -> Dict:
        """Retorna contagem de eventos"""
        with get_db() as conn:
            cursor = conn.cursor()
            start_date = datetime.now() - timedelta(days=days)

            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE contact_id IS NOT NULL) as with_contact,
                    COUNT(*) FILTER (WHERE prospect_id IS NOT NULL) as with_prospect,
                    COUNT(*) FILTER (WHERE conference_url IS NOT NULL) as with_meet
                FROM calendar_events
                WHERE start_datetime >= %s
            """, (start_date,))

            row = cursor.fetchone()
            return dict(row) if row else {"total": 0}


_calendar_events = None


def get_calendar_events() -> CalendarEventsService:
    global _calendar_events
    if _calendar_events is None:
        _calendar_events = CalendarEventsService()
    return _calendar_events
