"""
Integração com Google Calendar API

Permite agendar reuniões diretamente do sistema de prospects
"""
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import json

# Google Calendar API imports
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False

SCOPES = ['https://www.googleapis.com/auth/calendar']

class GoogleCalendarIntegration:
    """Gerencia integração com Google Calendar"""

    def __init__(self, credentials_path: str = "credentials/google_credentials.json",
                 token_path: str = "credentials/google_token.json"):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.service = None
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    def authenticate(self) -> bool:
        """
        Autentica com Google Calendar API

        Returns:
            True se autenticado com sucesso
        """
        if not GOOGLE_API_AVAILABLE:
            print("Google API libraries not installed. Run: pip install google-api-python-client google-auth-oauthlib")
            return False

        creds = None

        # Carregar token existente
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        # Se não há credenciais válidas, autenticar
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_path):
                    print(f"Credentials file not found: {self.credentials_path}")
                    return False

                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            # Salvar token
            os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
            with open(self.token_path, 'w') as token:
                token.write(creds.to_json())

        self.service = build('calendar', 'v3', credentials=creds)
        return True

    def create_meeting(
        self,
        prospect_name: str,
        prospect_email: Optional[str],
        date_time: datetime,
        duration_minutes: int = 30,
        meeting_type: str = "discovery",
        notes: str = ""
    ) -> Optional[Dict]:
        """
        Cria reunião no Google Calendar

        Args:
            prospect_name: Nome do prospect
            prospect_email: Email para enviar convite
            date_time: Data/hora da reunião
            duration_minutes: Duração em minutos
            meeting_type: Tipo da reunião (discovery, demo, negociacao)
            notes: Notas adicionais

        Returns:
            Dados do evento criado ou None se falhar
        """
        if not self.service:
            if not self.authenticate():
                return None

        # Definir título baseado no tipo
        titles = {
            "discovery": f"Discovery Call - {prospect_name} | ImensIAH",
            "demo": f"Demo ImensIAH - {prospect_name}",
            "negociacao": f"Negociação - {prospect_name} | ImensIAH",
            "fechamento": f"Fechamento - {prospect_name} | ImensIAH"
        }

        title = titles.get(meeting_type, f"Reunião - {prospect_name}")

        # Preparar evento
        end_time = date_time + timedelta(minutes=duration_minutes)

        event = {
            'summary': title,
            'description': f"""
Reunião de {meeting_type} com prospect

Prospect: {prospect_name}
Tipo: {meeting_type}

{notes}

---
Gerado pelo Sistema de Prospects ImensIAH
            """.strip(),
            'start': {
                'dateTime': date_time.isoformat(),
                'timeZone': 'America/Sao_Paulo',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'America/Sao_Paulo',
            },
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 24 * 60},
                    {'method': 'popup', 'minutes': 30},
                ],
            },
        }

        # Adicionar convidado se tiver email
        if prospect_email:
            event['attendees'] = [
                {'email': prospect_email},
            ]
            event['sendUpdates'] = 'all'

        # Adicionar Google Meet
        event['conferenceData'] = {
            'createRequest': {
                'requestId': f"imensiah-{datetime.now().timestamp()}",
                'conferenceSolutionKey': {'type': 'hangoutsMeet'}
            }
        }

        try:
            created_event = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event,
                conferenceDataVersion=1
            ).execute()

            return {
                'id': created_event.get('id'),
                'link': created_event.get('htmlLink'),
                'meet_link': created_event.get('hangoutLink'),
                'start': created_event.get('start'),
                'end': created_event.get('end')
            }

        except Exception as e:
            print(f"Error creating event: {e}")
            return None

    def get_available_slots(
        self,
        start_date: datetime,
        days: int = 7,
        slot_duration: int = 30
    ) -> List[Dict]:
        """
        Retorna horários disponíveis para reunião

        Args:
            start_date: Data inicial
            days: Número de dias para buscar
            slot_duration: Duração do slot em minutos

        Returns:
            Lista de slots disponíveis
        """
        if not self.service:
            if not self.authenticate():
                return []

        end_date = start_date + timedelta(days=days)

        # Buscar eventos existentes
        try:
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=start_date.isoformat() + 'Z',
                timeMax=end_date.isoformat() + 'Z',
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            busy_times = []
            for event in events_result.get('items', []):
                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))
                busy_times.append((
                    datetime.fromisoformat(start.replace('Z', '+00:00')),
                    datetime.fromisoformat(end.replace('Z', '+00:00'))
                ))

        except Exception as e:
            print(f"Error fetching events: {e}")
            busy_times = []

        # Gerar slots disponíveis (9h-18h, seg-sex)
        available = []
        current = start_date.replace(hour=9, minute=0, second=0, microsecond=0)

        while current < end_date:
            # Pular finais de semana
            if current.weekday() < 5:  # Seg-Sex
                # Horário comercial
                if 9 <= current.hour < 18:
                    slot_end = current + timedelta(minutes=slot_duration)

                    # Verificar se não está ocupado
                    is_busy = any(
                        (busy_start <= current < busy_end) or
                        (busy_start < slot_end <= busy_end)
                        for busy_start, busy_end in busy_times
                    )

                    if not is_busy:
                        available.append({
                            'start': current.isoformat(),
                            'end': slot_end.isoformat(),
                            'formatted': current.strftime('%d/%m/%Y %H:%M')
                        })

            current += timedelta(minutes=slot_duration)

            # Pular para próximo dia se passou das 18h
            if current.hour >= 18:
                current = (current + timedelta(days=1)).replace(hour=9, minute=0)

        return available[:20]  # Limitar a 20 slots

    def update_meeting(self, event_id: str, updates: Dict) -> bool:
        """Atualiza uma reunião existente"""
        if not self.service:
            if not self.authenticate():
                return False

        try:
            event = self.service.events().get(
                calendarId=self.calendar_id,
                eventId=event_id
            ).execute()

            # Aplicar atualizações
            if 'summary' in updates:
                event['summary'] = updates['summary']
            if 'description' in updates:
                event['description'] = updates['description']
            if 'start' in updates:
                event['start']['dateTime'] = updates['start']
            if 'end' in updates:
                event['end']['dateTime'] = updates['end']

            self.service.events().update(
                calendarId=self.calendar_id,
                eventId=event_id,
                body=event
            ).execute()

            return True

        except Exception as e:
            print(f"Error updating event: {e}")
            return False

    def delete_meeting(self, event_id: str) -> bool:
        """Cancela uma reunião"""
        if not self.service:
            if not self.authenticate():
                return False

        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=event_id
            ).execute()
            return True

        except Exception as e:
            print(f"Error deleting event: {e}")
            return False


# Função helper para uso sem autenticação (modo demo)
def create_calendar_link(
    prospect_name: str,
    date_time: datetime,
    duration_minutes: int = 30,
    meeting_type: str = "discovery"
) -> str:
    """
    Gera link para adicionar evento ao Google Calendar (sem API)

    Útil para modo demo ou quando não há autenticação
    """
    titles = {
        "discovery": f"Discovery Call - {prospect_name} | ImensIAH",
        "demo": f"Demo ImensIAH - {prospect_name}",
        "negociacao": f"Negociação - {prospect_name}",
    }

    title = titles.get(meeting_type, f"Reunião - {prospect_name}")
    end_time = date_time + timedelta(minutes=duration_minutes)

    # Formato Google Calendar
    start_str = date_time.strftime('%Y%m%dT%H%M%S')
    end_str = end_time.strftime('%Y%m%dT%H%M%S')

    import urllib.parse
    params = {
        'action': 'TEMPLATE',
        'text': title,
        'dates': f'{start_str}/{end_str}',
        'details': f'Reunião de {meeting_type} com prospect via ImensIAH',
        'ctz': 'America/Sao_Paulo'
    }

    return f"https://calendar.google.com/calendar/render?{urllib.parse.urlencode(params)}"
