"""
Calendar AI Service - Sugestoes automaticas de reuniao baseadas em health

Funcionalidades:
- Gera sugestoes de reuniao para contatos com health baixo
- Calcula urgencia baseado em health e circulo
- Sugere datetime ideal para reunioes
- Aceita sugestao e cria evento automaticamente
"""
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db
from services.calendar_events import get_calendar_events


class CalendarAIService:
    """Servico de sugestoes de calendario baseadas em AI"""

    def generate_calendar_suggestions(self, limit: int = 10) -> List[Dict]:
        """Gera sugestoes de reuniao para contatos com health baixo"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Contatos com health baixo sem reuniao agendada
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, c.circulo, c.health_score,
                       c.ultimo_contato, c.contexto, c.foto_url,
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
                contact = dict(contact)
                urgency = self._calculate_urgency(
                    contact["health_score"] or 50,
                    contact["circulo"] or 5,
                    contact["dias_sem_contato"] or 0
                )

                suggested_dt = self._suggest_datetime(urgency)

                suggestion = {
                    "contact_id": contact["id"],
                    "tipo": "calendar_reminder",
                    "titulo": f"Agendar reuniao com {contact['nome']}",
                    "descricao": f"Health {contact['health_score'] or 50}% - {contact['dias_sem_contato'] or 'N/A'} dias sem contato",
                    "acao_sugerida": {
                        "action": "create_calendar_event",
                        "suggested_datetime": suggested_dt.isoformat(),
                        "duration_minutes": 30,
                        "contact_name": contact["nome"],
                        "contact_empresa": contact.get("empresa")
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
                    (contact_id, tipo, titulo, descricao, dados, razao, prioridade, validade, confianca)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + INTERVAL '7 days', 0.85)
                    RETURNING id
                """, (
                    suggestion["contact_id"],
                    suggestion["tipo"],
                    suggestion["titulo"],
                    suggestion["descricao"],
                    json.dumps(suggestion["acao_sugerida"]),
                    f"Health baixo ({contact['health_score']}%) e {contact['dias_sem_contato'] or 'muitos'} dias sem contato",
                    suggestion["prioridade"]
                ))
                suggestion["id"] = cursor.fetchone()["id"]
                suggestion["contact_name"] = contact["nome"]
                suggestion["contact_foto"] = contact.get("foto_url")
                suggestions.append(suggestion)

            conn.commit()
            return suggestions

    def _calculate_urgency(self, health: int, circulo: int, dias: int) -> str:
        """Calcula urgencia baseado em health e circulo"""
        if circulo == 1 and (health < 20 or dias > 30):
            return "high"
        if circulo <= 2 and (health < 30 or dias > 45):
            return "high"
        if health < 40 or dias > 60:
            return "medium"
        return "low"

    def _suggest_datetime(self, urgency: str) -> datetime:
        """Sugere datetime baseado na urgencia"""
        now = datetime.now()

        if urgency == "high":
            days_ahead = 1
        elif urgency == "medium":
            days_ahead = 3
        else:
            days_ahead = 7

        suggested = now + timedelta(days=days_ahead)

        # Ajustar para horario comercial (10h)
        suggested = suggested.replace(hour=10, minute=0, second=0, microsecond=0)

        # Se cair no fim de semana, mover para segunda
        while suggested.weekday() >= 5:
            suggested += timedelta(days=1)

        return suggested

    def get_calendar_suggestions(self, limit: int = 20) -> List[Dict]:
        """Lista sugestoes de reuniao pendentes"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.*, c.nome as contact_name, c.foto_url as contact_foto,
                       c.empresa as contact_empresa, c.circulo as contact_circulo
                FROM ai_suggestions s
                JOIN contacts c ON c.id = s.contact_id
                WHERE s.tipo = 'calendar_reminder' AND s.status = 'pending'
                AND (s.validade IS NULL OR s.validade > NOW())
                ORDER BY s.prioridade DESC
                LIMIT %s
            """, (limit,))

            suggestions = []
            for row in cursor.fetchall():
                s = dict(row)
                for key in ["criado_em", "aceita_em", "descartada_em", "executada_em", "validade"]:
                    if s.get(key) and hasattr(s[key], "isoformat"):
                        s[key] = s[key].isoformat()
                suggestions.append(s)
            return suggestions

    async def accept_and_create_event(
        self,
        suggestion_id: int,
        custom_datetime: datetime = None,
        duration_minutes: int = None
    ) -> Dict:
        """Aceita sugestao e cria evento no Google Calendar"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar sugestao
            cursor.execute("""
                SELECT s.*, c.nome as contact_name, c.email as contact_email
                FROM ai_suggestions s
                JOIN contacts c ON c.id = s.contact_id
                WHERE s.id = %s AND s.status = 'pending'
            """, (suggestion_id,))
            suggestion = cursor.fetchone()

            if not suggestion:
                return {"error": "Sugestao nao encontrada ou ja processada"}

            suggestion = dict(suggestion)
            dados = suggestion.get("dados", {})
            if isinstance(dados, str):
                dados = json.loads(dados)

        # Determinar datetime
        if custom_datetime:
            event_datetime = custom_datetime
        elif dados.get("suggested_datetime"):
            event_datetime = datetime.fromisoformat(dados["suggested_datetime"])
        else:
            event_datetime = self._suggest_datetime("medium")

        # Determinar duracao
        event_duration = duration_minutes or dados.get("duration_minutes", 30)

        # Criar evento
        events_svc = get_calendar_events()
        event = events_svc.create_event(
            summary=f"Reuniao com {suggestion['contact_name']}",
            start_datetime=event_datetime,
            end_datetime=event_datetime + timedelta(minutes=event_duration),
            description=f"Reuniao agendada via sugestao AI\n\n{suggestion['descricao']}",
            contact_id=suggestion["contact_id"],
            attendees=[{"email": suggestion["contact_email"]}] if suggestion.get("contact_email") else None,
            create_in_google=True
        )

        # Marcar sugestao como aceita
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE ai_suggestions
                SET status = 'accepted', aceita_em = NOW()
                WHERE id = %s
            """, (suggestion_id,))

            # Atualizar ultimo contato do contact (agendamento conta como interacao)
            cursor.execute("""
                UPDATE contacts SET ultimo_contato = NOW() WHERE id = %s
            """, (suggestion["contact_id"],))

            conn.commit()

        return {
            "status": "created",
            "event": event,
            "suggestion_id": suggestion_id
        }

    def dismiss_suggestion(self, suggestion_id: int, motivo: str = None) -> bool:
        """Descarta uma sugestao de reuniao"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE ai_suggestions
                SET status = 'dismissed', descartada_em = NOW(), motivo_descarte = %s
                WHERE id = %s AND status = 'pending'
                RETURNING id
            """, (motivo, suggestion_id))
            result = cursor.fetchone()
            conn.commit()
            return result is not None

    def get_contacts_needing_meeting(self, limit: int = 20) -> List[Dict]:
        """Lista contatos que precisam de reuniao (sem sugestao criada)"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, c.circulo, c.health_score,
                       c.ultimo_contato, c.foto_url,
                       EXTRACT(DAY FROM NOW() - c.ultimo_contato)::int as dias_sem_contato
                FROM contacts c
                WHERE COALESCE(c.circulo, 5) <= 3
                AND (
                    COALESCE(c.health_score, 50) < 40
                    OR (c.circulo = 1 AND c.ultimo_contato < NOW() - INTERVAL '30 days')
                    OR (c.circulo = 2 AND c.ultimo_contato < NOW() - INTERVAL '45 days')
                    OR (c.circulo = 3 AND c.ultimo_contato < NOW() - INTERVAL '60 days')
                )
                AND c.id NOT IN (
                    SELECT contact_id FROM calendar_events
                    WHERE contact_id IS NOT NULL
                    AND start_datetime > NOW()
                )
                ORDER BY c.circulo ASC, c.health_score ASC NULLS LAST
                LIMIT %s
            """, (limit,))

            contacts = []
            for row in cursor.fetchall():
                c = dict(row)
                if c.get("ultimo_contato") and hasattr(c["ultimo_contato"], "isoformat"):
                    c["ultimo_contato"] = c["ultimo_contato"].isoformat()
                c["urgency"] = self._calculate_urgency(
                    c["health_score"] or 50,
                    c["circulo"] or 5,
                    c["dias_sem_contato"] or 0
                )
                contacts.append(c)
            return contacts

    def get_suggestion_stats(self) -> Dict:
        """Retorna estatisticas das sugestoes de calendario"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE status = 'accepted') as accepted,
                    COUNT(*) FILTER (WHERE status = 'dismissed') as dismissed,
                    COUNT(*) FILTER (WHERE status = 'accepted' AND aceita_em > NOW() - INTERVAL '7 days') as accepted_last_week
                FROM ai_suggestions
                WHERE tipo = 'calendar_reminder'
            """)
            return dict(cursor.fetchone())


_calendar_ai = None


def get_calendar_ai() -> CalendarAIService:
    global _calendar_ai
    if _calendar_ai is None:
        _calendar_ai = CalendarAIService()
    return _calendar_ai
