"""
Realtime Analyzer Service - Analise de mensagens em tempo real

Detecta intencoes em mensagens WhatsApp/Email e sugere acoes:
- reschedule_meeting: Pedido de remarcacao
- cancel_meeting: Cancelamento
- confirm_meeting: Confirmacao
- urgent_request: Pedido urgente
- question: Pergunta que precisa resposta
- payment_mention: Mencao a pagamento/cobranca
- deadline_mention: Mencao a prazo
- important_info: Informacao importante
"""
import os
import json
import httpx
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Tipos de intencao suportados
INTENT_TYPES = [
    'reschedule_meeting',
    'cancel_meeting',
    'confirm_meeting',
    'urgent_request',
    'question',
    'payment_mention',
    'deadline_mention',
    'important_info',
]


class RealtimeAnalyzer:
    """Analisa mensagens em tempo real e detecta intencoes/acoes."""

    def __init__(self):
        self.api_key = ANTHROPIC_API_KEY

    async def call_claude(self, prompt: str, max_tokens: int = 1000) -> Optional[str]:
        """Chama API do Claude para analise"""
        if not self.api_key:
            return None

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}]
                    },
                    timeout=30.0
                )
                if response.status_code == 200:
                    data = response.json()
                    return data["content"][0]["text"]
        except Exception as e:
            print(f"Claude API error in analyzer: {e}")
        return None

    def get_contact_info(self, contact_id: int) -> Optional[Dict]:
        """Busca informacoes do contato"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, emails, telefones
                FROM contacts
                WHERE id = %s
            """, (contact_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_calendar_context(self, contact_id: int, days_range: int = 3) -> List[Dict]:
        """
        Busca eventos do calendario relacionados ao contato.
        Procura eventos dos ultimos 3 dias ate os proximos 7 dias.
        """
        events = []
        with get_db() as conn:
            cursor = conn.cursor()

            start_date = datetime.now() - timedelta(days=days_range)
            end_date = datetime.now() + timedelta(days=7)

            # Buscar eventos do contato
            cursor.execute("""
                SELECT id, google_event_id, summary, description, location,
                       start_datetime, end_datetime, status, attendees
                FROM calendar_events
                WHERE contact_id = %s
                AND start_datetime BETWEEN %s AND %s
                ORDER BY start_datetime ASC
            """, (contact_id, start_date, end_date))

            for row in cursor.fetchall():
                event = dict(row)
                # Converter datas para string
                if event.get("start_datetime"):
                    event["start_datetime"] = event["start_datetime"].isoformat()
                if event.get("end_datetime"):
                    event["end_datetime"] = event["end_datetime"].isoformat()
                events.append(event)

            # Se nao encontrou pelo contact_id, tentar pelo nome
            if not events:
                contact = self.get_contact_info(contact_id)
                if contact and contact.get("nome"):
                    contact_name = contact["nome"]
                    # Buscar eventos que mencionam o contato no summary ou attendees
                    cursor.execute("""
                        SELECT id, google_event_id, summary, description, location,
                               start_datetime, end_datetime, status, attendees
                        FROM calendar_events
                        WHERE (summary ILIKE %s OR attendees::text ILIKE %s)
                        AND start_datetime BETWEEN %s AND %s
                        ORDER BY start_datetime ASC
                        LIMIT 10
                    """, (f"%{contact_name}%", f"%{contact_name}%", start_date, end_date))

                    for row in cursor.fetchall():
                        event = dict(row)
                        if event.get("start_datetime"):
                            event["start_datetime"] = event["start_datetime"].isoformat()
                        if event.get("end_datetime"):
                            event["end_datetime"] = event["end_datetime"].isoformat()
                        events.append(event)

        return events

    def get_recent_messages(self, contact_id: int, limit: int = 5) -> List[Dict]:
        """Busca mensagens recentes com o contato para contexto"""
        messages = []
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.id, m.direcao, m.conteudo, m.enviado_em
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.contact_id = %s
                ORDER BY m.enviado_em DESC
                LIMIT %s
            """, (contact_id, limit))

            for row in cursor.fetchall():
                msg = dict(row)
                if msg.get("enviado_em"):
                    msg["enviado_em"] = msg["enviado_em"].isoformat()
                messages.append(msg)

        return list(reversed(messages))  # Ordem cronologica

    async def analyze_message(
        self,
        message_text: str,
        contact_id: int,
        message_direction: str = "incoming",
        message_id: int = None
    ) -> Dict:
        """
        Analisa mensagem e retorna intencoes detectadas.

        Args:
            message_text: Conteudo da mensagem
            contact_id: ID do contato
            message_direction: 'incoming' ou 'outgoing'
            message_id: ID da mensagem no banco

        Returns:
            {
                'intents': [{'type': str, 'confidence': float, 'details': dict}],
                'requires_action': bool,
                'urgency': 'high' | 'medium' | 'low',
                'suggested_actions': [ActionProposal dict],
                'context': dict
            }
        """
        result = {
            'intents': [],
            'requires_action': False,
            'urgency': 'low',
            'suggested_actions': [],
            'context': {},
            'message_id': message_id,
            'contact_id': contact_id
        }

        # Ignorar mensagens de saida (enviadas por Renato)
        if message_direction == 'outgoing':
            return result

        # Mensagens muito curtas geralmente nao precisam de acao
        if len(message_text.strip()) < 10:
            return result

        # Buscar contexto
        contact = self.get_contact_info(contact_id)
        calendar_events = self.get_calendar_context(contact_id)
        recent_messages = self.get_recent_messages(contact_id, limit=5)

        result['context'] = {
            'contact': contact,
            'calendar_events': calendar_events,
            'recent_messages': recent_messages
        }

        # Detectar intencao com AI
        intents = await self.detect_intent_with_ai(
            message_text,
            contact,
            calendar_events,
            recent_messages
        )

        if not intents:
            return result

        result['intents'] = intents

        # Determinar se requer acao e urgencia
        action_intents = ['reschedule_meeting', 'cancel_meeting', 'urgent_request',
                         'question', 'payment_mention', 'deadline_mention']

        max_confidence = 0
        highest_urgency = 'low'

        for intent in intents:
            if intent['type'] in action_intents and intent['confidence'] >= 0.7:
                result['requires_action'] = True

            if intent['confidence'] > max_confidence:
                max_confidence = intent['confidence']

            # Determinar urgencia
            intent_urgency = intent.get('urgency', 'low')
            if intent_urgency == 'high':
                highest_urgency = 'high'
            elif intent_urgency == 'medium' and highest_urgency != 'high':
                highest_urgency = 'medium'

        result['urgency'] = highest_urgency

        # Gerar acoes sugeridas se necessario
        if result['requires_action']:
            result['suggested_actions'] = self.generate_action_proposals(
                intents, contact, calendar_events, message_text, message_id
            )

        return result

    async def detect_intent_with_ai(
        self,
        message_text: str,
        contact: Dict,
        calendar_events: List[Dict],
        recent_messages: List[Dict]
    ) -> List[Dict]:
        """Usa Claude para detectar intencao da mensagem."""

        if not self.api_key:
            # Fallback: deteccao basica por palavras-chave
            return self._detect_intent_keywords(message_text)

        # Formatar eventos do calendario
        events_text = "Nenhum evento encontrado"
        if calendar_events:
            events_list = []
            for e in calendar_events[:5]:
                start = e.get('start_datetime', '')
                events_list.append(f"- {e['summary']} em {start}")
            events_text = "\n".join(events_list)

        # Formatar mensagens recentes
        history_text = ""
        if recent_messages:
            for m in recent_messages[-3:]:
                direction = "Renato" if m['direcao'] == 'outgoing' else contact.get('nome', 'Contato')
                history_text += f"{direction}: {m['conteudo'][:200]}\n"

        contact_name = contact.get('nome', 'Desconhecido') if contact else 'Desconhecido'
        contact_role = contact.get('cargo', '') if contact else ''
        contact_company = contact.get('empresa', '') if contact else ''

        prompt = f"""Analise esta mensagem de WhatsApp e identifique a intencao principal.

MENSAGEM: "{message_text}"

REMETENTE: {contact_name} ({contact_role} na {contact_company})

EVENTOS NO CALENDARIO COM ESTE CONTATO:
{events_text}

HISTORICO RECENTE DA CONVERSA:
{history_text if history_text else "Nenhuma mensagem anterior"}

Identifique se a mensagem contem alguma destas intencoes:
- reschedule_meeting: Pedido para remarcar reuniao/encontro
- cancel_meeting: Cancelamento de reuniao/encontro
- confirm_meeting: Confirmacao de reuniao/encontro
- urgent_request: Pedido urgente que precisa atencao imediata
- question: Pergunta que precisa de resposta
- payment_mention: Mencao a pagamento, cobranca ou valores
- deadline_mention: Mencao a prazo, data limite
- important_info: Informacao importante que Renato precisa saber
- none: Mensagem trivial, agradecimento, etc.

Responda APENAS com JSON valido no formato:
{{
    "intents": [
        {{
            "type": "tipo_da_intencao",
            "confidence": 0.0 a 1.0,
            "urgency": "high" ou "medium" ou "low",
            "details": {{
                "extracted_date": "data mencionada se houver",
                "extracted_time": "horario mencionado se houver",
                "reason": "motivo se mencionado",
                "action_needed": "descricao curta da acao necessaria"
            }}
        }}
    ],
    "summary": "resumo de uma linha do que a mensagem comunica"
}}

Se nao detectar nenhuma intencao relevante, retorne intents como array vazio."""

        response = await self.call_claude(prompt, max_tokens=500)

        if not response:
            return self._detect_intent_keywords(message_text)

        try:
            # Limpar resposta (remover markdown se houver)
            response = response.strip()
            if response.startswith("```"):
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            response = response.strip()

            data = json.loads(response)
            return data.get('intents', [])
        except json.JSONDecodeError as e:
            print(f"Failed to parse Claude response: {e}")
            return self._detect_intent_keywords(message_text)

    def _detect_intent_keywords(self, message_text: str) -> List[Dict]:
        """Deteccao basica de intencao por palavras-chave (fallback)"""
        intents = []
        text_lower = message_text.lower()

        # Remarcacao
        if any(word in text_lower for word in ['remarcar', 'adiar', 'mudar horario', 'outro dia', 'outro horario', 'reagendar']):
            intents.append({
                'type': 'reschedule_meeting',
                'confidence': 0.75,
                'urgency': 'high',
                'details': {'action_needed': 'Verificar agenda e remarcar'}
            })

        # Cancelamento
        if any(word in text_lower for word in ['cancelar', 'cancela', 'desmarcar', 'nao vai dar', 'nao posso']):
            intents.append({
                'type': 'cancel_meeting',
                'confidence': 0.75,
                'urgency': 'high',
                'details': {'action_needed': 'Confirmar cancelamento'}
            })

        # Confirmacao
        if any(word in text_lower for word in ['confirmado', 'confirmo', 'pode ser', 'fechado', 'combinado', 'ok', 'beleza']):
            intents.append({
                'type': 'confirm_meeting',
                'confidence': 0.70,
                'urgency': 'medium',
                'details': {'action_needed': 'Registrar confirmacao'}
            })

        # Urgente
        if any(word in text_lower for word in ['urgente', 'urgencia', 'imediato', 'agora', 'socorro', 'emergencia']):
            intents.append({
                'type': 'urgent_request',
                'confidence': 0.80,
                'urgency': 'high',
                'details': {'action_needed': 'Responder imediatamente'}
            })

        # Pergunta
        if '?' in message_text or any(word in text_lower for word in ['voce sabe', 'pode me', 'consegue', 'como faco']):
            intents.append({
                'type': 'question',
                'confidence': 0.70,
                'urgency': 'medium',
                'details': {'action_needed': 'Responder pergunta'}
            })

        # Pagamento
        if any(word in text_lower for word in ['pagamento', 'pagar', 'boleto', 'fatura', 'cobranca', 'pix', 'transferencia', 'nota fiscal']):
            intents.append({
                'type': 'payment_mention',
                'confidence': 0.80,
                'urgency': 'medium',
                'details': {'action_needed': 'Verificar status financeiro'}
            })

        # Prazo
        if any(word in text_lower for word in ['prazo', 'deadline', 'ate dia', 'ate o dia', 'data limite', 'vencimento']):
            intents.append({
                'type': 'deadline_mention',
                'confidence': 0.75,
                'urgency': 'medium',
                'details': {'action_needed': 'Verificar prazo e planejar'}
            })

        return intents

    def generate_action_proposals(
        self,
        intents: List[Dict],
        contact: Dict,
        calendar_events: List[Dict],
        message_text: str,
        message_id: int
    ) -> List[Dict]:
        """Gera propostas de acao baseadas nas intencoes detectadas."""
        proposals = []
        contact_name = contact.get('nome', 'Contato') if contact else 'Contato'
        contact_id = contact.get('id') if contact else None

        for intent in intents:
            intent_type = intent['type']
            confidence = intent.get('confidence', 0.5)
            urgency = intent.get('urgency', 'medium')
            details = intent.get('details', {})

            if intent_type == 'reschedule_meeting' and confidence >= 0.7:
                # Encontrar evento para remarcar
                event = self._find_relevant_event(calendar_events, message_text)

                proposal = {
                    'action_type': 'reschedule_event',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': urgency,
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Mensagem indica pedido de remarcacao: {details.get('reason', 'nao especificado')}",
                    'title': f"Remarcar reuniao com {contact_name}",
                    'description': f"{contact_name} pediu para remarcar. " + (
                        f"Evento encontrado: {event['summary']}" if event else "Verificar agenda manualmente."
                    ),
                    'action_params': {
                        'event_id': event.get('id') if event else None,
                        'google_event_id': event.get('google_event_id') if event else None,
                        'original_date': event.get('start_datetime') if event else None,
                        'suggested_date': details.get('extracted_date'),
                        'suggested_time': details.get('extracted_time')
                    },
                    'options': [
                        {'id': 'reschedule_tomorrow', 'label': 'Remarcar para amanha', 'action': 'reschedule', 'params': {'days': 1}},
                        {'id': 'reschedule_next_week', 'label': 'Remarcar para proxima semana', 'action': 'reschedule', 'params': {'days': 7}},
                        {'id': 'cancel', 'label': 'Cancelar reuniao', 'action': 'cancel'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

            elif intent_type == 'cancel_meeting' and confidence >= 0.7:
                event = self._find_relevant_event(calendar_events, message_text)

                proposal = {
                    'action_type': 'cancel_event',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': urgency,
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Mensagem indica cancelamento",
                    'title': f"Cancelar reuniao com {contact_name}",
                    'description': f"{contact_name} cancelou. " + (
                        f"Evento: {event['summary']}" if event else "Verificar agenda."
                    ),
                    'action_params': {
                        'event_id': event.get('id') if event else None,
                        'google_event_id': event.get('google_event_id') if event else None
                    },
                    'options': [
                        {'id': 'cancel_event', 'label': 'Cancelar evento', 'action': 'cancel'},
                        {'id': 'reschedule', 'label': 'Propor nova data', 'action': 'reschedule'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

            elif intent_type == 'confirm_meeting' and confidence >= 0.7:
                event = self._find_relevant_event(calendar_events, message_text)

                if event:
                    proposal = {
                        'action_type': 'confirm_event',
                        'contact_id': contact_id,
                        'message_id': message_id,
                        'confidence': confidence,
                        'urgency': 'low',
                        'trigger_text': message_text[:300],
                        'ai_reasoning': f"Contato confirmou presenca",
                        'title': f"{contact_name} confirmou reuniao",
                        'description': f"Evento: {event['summary']} em {event.get('start_datetime', '')}",
                        'action_params': {
                            'event_id': event.get('id'),
                            'google_event_id': event.get('google_event_id')
                        },
                        'options': [
                            {'id': 'mark_confirmed', 'label': 'Marcar como confirmado', 'action': 'confirm'},
                            {'id': 'ignore', 'label': 'Apenas notificar', 'action': 'dismiss'}
                        ]
                    }
                    proposals.append(proposal)

            elif intent_type == 'urgent_request' and confidence >= 0.7:
                proposal = {
                    'action_type': 'urgent_alert',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': 'high',
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Mensagem urgente detectada",
                    'title': f"URGENTE: Mensagem de {contact_name}",
                    'description': message_text[:200],
                    'action_params': {},
                    'options': [
                        {'id': 'respond_now', 'label': 'Responder agora', 'action': 'open_conversation'},
                        {'id': 'create_task', 'label': 'Criar tarefa', 'action': 'create_task'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

            elif intent_type == 'question' and confidence >= 0.7:
                proposal = {
                    'action_type': 'pending_response',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': urgency,
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Pergunta aguardando resposta",
                    'title': f"Responder {contact_name}",
                    'description': f"Pergunta: {message_text[:150]}...",
                    'action_params': {},
                    'options': [
                        {'id': 'respond', 'label': 'Responder agora', 'action': 'open_conversation'},
                        {'id': 'later', 'label': 'Responder depois', 'action': 'create_task'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

            elif intent_type == 'payment_mention' and confidence >= 0.7:
                proposal = {
                    'action_type': 'financial_alert',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': urgency,
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Mencao a assunto financeiro",
                    'title': f"Financeiro: {contact_name}",
                    'description': f"Mensagem sobre pagamento/cobranca: {message_text[:150]}",
                    'action_params': {},
                    'options': [
                        {'id': 'review', 'label': 'Revisar agora', 'action': 'open_conversation'},
                        {'id': 'task', 'label': 'Criar tarefa', 'action': 'create_task'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

        return proposals

    def _find_relevant_event(self, calendar_events: List[Dict], message_text: str) -> Optional[Dict]:
        """Encontra o evento mais relevante baseado no contexto"""
        if not calendar_events:
            return None

        # Priorizar eventos de hoje ou proximos
        now = datetime.now()
        today_events = []
        upcoming_events = []

        for event in calendar_events:
            start_str = event.get('start_datetime', '')
            if start_str:
                try:
                    start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                    if start_dt.date() == now.date():
                        today_events.append(event)
                    elif start_dt > now:
                        upcoming_events.append(event)
                except (ValueError, TypeError):
                    continue

        # Retornar evento de hoje se houver, senao o proximo
        if today_events:
            return today_events[0]
        if upcoming_events:
            return upcoming_events[0]
        if calendar_events:
            return calendar_events[0]

        return None


_realtime_analyzer = None


def get_realtime_analyzer() -> RealtimeAnalyzer:
    global _realtime_analyzer
    if _realtime_analyzer is None:
        _realtime_analyzer = RealtimeAnalyzer()
    return _realtime_analyzer
