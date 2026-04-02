"""
Google Calendar Integration for INTEL
Busca eventos do calendario para exibir no dashboard

Autor: INTEL
Data: 2026-03-26
"""
import os
import httpx
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)


class GoogleCalendarIntegration:
    """
    Integration with Google Calendar API
    Uses same OAuth credentials as Gmail
    """

    CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

    # Scope necessario (full access para leitura e escrita)
    SCOPE = "https://www.googleapis.com/auth/calendar"

    def __init__(self):
        self.client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")

    async def list_events(
        self,
        access_token: str,
        calendar_id: str = "primary",
        time_min: datetime = None,
        time_max: datetime = None,
        max_results: int = 10,
        single_events: bool = True,
        order_by: str = "startTime"
    ) -> Dict[str, Any]:
        """
        Lista eventos do calendario.
        """
        params = {
            "maxResults": max_results,
            "singleEvents": str(single_events).lower(),
            "orderBy": order_by
        }

        if time_min:
            params["timeMin"] = time_min.isoformat() + "Z"
        if time_max:
            params["timeMax"] = time_max.isoformat() + "Z"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                    timeout=30.0
                )

                if response.status_code == 401:
                    return {"error": "token_expired"}
                elif response.status_code != 200:
                    return {"error": response.text}

                return response.json()

            except Exception as e:
                logger.error(f"Erro ao listar eventos: {e}")
                return {"error": str(e)}

    async def search_events(
        self,
        access_token: str,
        query: str,
        calendar_id: str = "primary",
        max_results: int = 20
    ) -> Dict[str, Any]:
        """
        Busca eventos por texto (titulo, descricao, local, participantes).
        """
        params = {
            "q": query,
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime"
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                    timeout=30.0
                )

                if response.status_code == 401:
                    return {"error": "token_expired"}
                elif response.status_code != 200:
                    return {"error": response.text}

                return response.json()

            except Exception as e:
                logger.error(f"Erro ao buscar eventos: {e}")
                return {"error": str(e)}

    async def get_today_events(self, access_token: str) -> List[Dict[str, Any]]:
        """Retorna eventos de hoje para o dashboard (timezone America/Sao_Paulo)."""
        from zoneinfo import ZoneInfo

        # Usar timezone de São Paulo para definir "hoje"
        sp_tz = ZoneInfo("America/Sao_Paulo")
        now_sp = datetime.now(sp_tz)
        start_of_day_sp = now_sp.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day_sp = start_of_day_sp + timedelta(days=1)

        # Converter para UTC para a API
        start_of_day_utc = start_of_day_sp.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        end_of_day_utc = end_of_day_sp.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

        result = await self.list_events(
            access_token=access_token,
            time_min=start_of_day_utc,
            time_max=end_of_day_utc,
            max_results=20
        )

        if "error" in result:
            logger.error(f"Erro ao buscar eventos de hoje: {result.get('error')}")
            return []

        events = result.get("items", [])
        return [self._format_event(e) for e in events]

    async def get_upcoming_events(
        self,
        access_token: str,
        days: int = 7,
        max_results: int = 20
    ) -> List[Dict[str, Any]]:
        """Retorna proximos eventos."""
        now = datetime.utcnow()
        end_date = now + timedelta(days=days)

        result = await self.list_events(
            access_token=access_token,
            time_min=now,
            time_max=end_date,
            max_results=max_results
        )

        if "error" in result:
            return []

        events = result.get("items", [])
        return [self._format_event(e) for e in events]

    def _format_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Formata evento para exibicao na UI."""
        start = event.get("start", {})
        start_datetime = start.get("dateTime") or start.get("date")
        is_all_day = "date" in start and "dateTime" not in start

        end = event.get("end", {})
        end_datetime = end.get("dateTime") or end.get("date")

        attendees = []
        for attendee in event.get("attendees", []):
            attendees.append({
                "email": attendee.get("email"),
                "name": attendee.get("displayName"),
                "response": attendee.get("responseStatus"),
            })

        conference = None
        conference_data = event.get("conferenceData", {})
        entry_points = conference_data.get("entryPoints", [])
        for ep in entry_points:
            if ep.get("entryPointType") == "video":
                conference = {
                    "type": conference_data.get("conferenceSolution", {}).get("name", "Video"),
                    "url": ep.get("uri"),
                }
                break

        return {
            "id": event.get("id"),
            "summary": event.get("summary", "Sem titulo"),
            "description": event.get("description"),
            "location": event.get("location"),
            "start": start_datetime,
            "end": end_datetime,
            "is_all_day": is_all_day,
            "status": event.get("status"),
            "html_link": event.get("htmlLink"),
            "attendees": attendees,
            "conference": conference,
        }

    async def create_event(
        self,
        access_token: str,
        summary: str,
        start_datetime: datetime,
        end_datetime: datetime,
        description: str = None,
        location: str = None,
        attendees: List[str] = None,
        create_meet: bool = True,
        calendar_id: str = "primary"
    ) -> Dict[str, Any]:
        """Cria evento no Google Calendar."""
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

        if location:
            event_body["location"] = location

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
            try:
                response = await client.post(
                    f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events",
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
                    logger.error(f"Erro ao criar evento: {response.status_code} - {response.text}")
                    return {"error": f"Erro ao criar evento: {response.status_code}"}

            except Exception as e:
                logger.error(f"Erro ao criar evento: {e}")
                return {"error": str(e)}

    async def update_event(
        self,
        access_token: str,
        event_id: str,
        updates: Dict[str, Any],
        calendar_id: str = "primary"
    ) -> Dict[str, Any]:
        """Atualiza evento existente."""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.patch(
                    f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}",
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
                    logger.error(f"Erro ao atualizar evento: {response.status_code}")
                    return {"error": f"Erro ao atualizar evento: {response.status_code}"}

            except Exception as e:
                logger.error(f"Erro ao atualizar evento: {e}")
                return {"error": str(e)}

    async def delete_event(
        self,
        access_token: str,
        event_id: str,
        calendar_id: str = "primary"
    ) -> bool:
        """Deleta evento do Google Calendar."""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.delete(
                    f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30.0
                )

                return response.status_code == 204

            except Exception as e:
                logger.error(f"Erro ao deletar evento: {e}")
                return False

    async def list_events_incremental(
        self,
        access_token: str,
        sync_token: str = None,
        calendar_id: str = "primary",
        max_results: int = 100
    ) -> Dict[str, Any]:
        """Lista eventos com sync incremental."""
        params = {
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime"
        }

        if sync_token:
            params["syncToken"] = sync_token
        else:
            # Full sync - eventos dos últimos 30 dias até 90 dias no futuro
            params["timeMin"] = (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"
            params["timeMax"] = (datetime.utcnow() + timedelta(days=90)).isoformat() + "Z"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events",
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
                    logger.error(f"Erro ao listar eventos: {response.status_code}")
                    return {"error": f"Erro ao listar eventos: {response.status_code}"}

            except Exception as e:
                logger.error(f"Erro ao listar eventos: {e}")
                return {"error": str(e)}


_calendar_integration = None


def get_calendar_integration() -> GoogleCalendarIntegration:
    """Retorna instancia singleton."""
    global _calendar_integration
    if _calendar_integration is None:
        _calendar_integration = GoogleCalendarIntegration()
    return _calendar_integration


def create_calendar_link(
    title: str,
    start_datetime: datetime,
    duration_minutes: int = 60,
    meeting_type: str = "reuniao"
) -> str:
    """
    Cria link para adicionar evento ao Google Calendar (fallback quando API nao disponivel).

    Args:
        title: Nome do contato/prospect
        start_datetime: Data e hora de inicio
        duration_minutes: Duracao em minutos
        meeting_type: Tipo de reuniao (reuniao, call, cafe, etc)

    Returns:
        URL para abrir Google Calendar com evento pre-preenchido
    """
    from urllib.parse import quote

    # Formatar titulo
    event_title = f"{meeting_type.title()} com {title}"

    # Calcular data/hora fim
    end_datetime = start_datetime + timedelta(minutes=duration_minutes)

    # Formatar datas no formato Google Calendar (YYYYMMDDTHHmmss)
    date_format = "%Y%m%dT%H%M%S"
    start_str = start_datetime.strftime(date_format)
    end_str = end_datetime.strftime(date_format)

    # Construir URL
    base_url = "https://calendar.google.com/calendar/render"
    params = {
        "action": "TEMPLATE",
        "text": quote(event_title),
        "dates": f"{start_str}/{end_str}",
        "details": quote(f"Reuniao agendada via INTEL"),
    }

    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{base_url}?{query_string}"


# =========================================================================
# STANDALONE ASYNC FUNCTIONS FOR DIRECT USE
# =========================================================================

async def create_calendar_event(
    access_token: str,
    summary: str,
    start_datetime: datetime,
    end_datetime: datetime,
    description: str = None,
    attendees: List[str] = None,
    create_meet: bool = True,
    calendar_id: str = "primary"
) -> Dict[str, Any]:
    """Cria evento no Google Calendar (standalone function)."""
    integration = get_calendar_integration()
    return await integration.create_event(
        access_token=access_token,
        summary=summary,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        description=description,
        attendees=attendees,
        create_meet=create_meet,
        calendar_id=calendar_id
    )


async def update_calendar_event(
    access_token: str,
    event_id: str,
    updates: Dict[str, Any],
    calendar_id: str = "primary"
) -> Dict[str, Any]:
    """Atualiza evento existente (standalone function)."""
    integration = get_calendar_integration()
    return await integration.update_event(
        access_token=access_token,
        event_id=event_id,
        updates=updates,
        calendar_id=calendar_id
    )


async def delete_calendar_event(
    access_token: str,
    event_id: str,
    calendar_id: str = "primary"
) -> bool:
    """Deleta evento do Google Calendar (standalone function)."""
    integration = get_calendar_integration()
    return await integration.delete_event(
        access_token=access_token,
        event_id=event_id,
        calendar_id=calendar_id
    )


async def list_events_incremental(
    access_token: str,
    sync_token: str = None,
    calendar_id: str = "primary",
    max_results: int = 100
) -> Dict[str, Any]:
    """Lista eventos com sync incremental (standalone function)."""
    integration = get_calendar_integration()
    return await integration.list_events_incremental(
        access_token=access_token,
        sync_token=sync_token,
        calendar_id=calendar_id,
        max_results=max_results
    )
