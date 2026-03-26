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

    # Scope necessario (adicionar ao OAuth existente)
    SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

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

    async def get_today_events(self, access_token: str) -> List[Dict[str, Any]]:
        """Retorna eventos de hoje para o dashboard."""
        now = datetime.utcnow()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        result = await self.list_events(
            access_token=access_token,
            time_min=start_of_day,
            time_max=end_of_day,
            max_results=20
        )

        if "error" in result:
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
