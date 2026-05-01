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
- introduction_request: Pedido de apresentacao/indicacao
- opportunity_signal: Sinal de oportunidade de negocio
- complaint: Reclamacao ou feedback negativo
- meeting_request: Pedido de nova reuniao
- follow_up_needed: Precisa de follow-up
"""
import os
import re
import json
import httpx
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db
from services.rodas_service import get_rodas_service

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
    'introduction_request',
    'opportunity_signal',
    'complaint',
    'meeting_request',
    'follow_up_needed',
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
            'rodas': [],
            'requires_action': False,
            'urgency': 'low',
            'suggested_actions': [],
            'context': {},
            'message_id': message_id,
            'contact_id': contact_id,
            'direction': message_direction
        }

        # Mensagens muito curtas geralmente nao precisam de analise
        if len(message_text.strip()) < 10:
            return result

        # Filtrar mensagens automaticas/sistema (portaria, OTP, erros, pingbacks)
        # Why: feedback 2026-04-25 — mensagens de portaria viraram pedidos de indicacao
        if message_direction == "incoming":
            from services.message_filters import is_automated_message
            is_auto, reason = is_automated_message(message_text, contact_id)
            if is_auto:
                print(f"[RealtimeAnalyzer] Skipping msg {message_id} contact {contact_id}: {reason}")
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

        # Detectar intencao e rodas com AI
        ai_result = await self.detect_intent_with_ai(
            message_text,
            contact,
            calendar_events,
            recent_messages
        )

        # Compatibilidade: se retornou lista (formato antigo), converter
        if isinstance(ai_result, list):
            ai_result = {'intents': ai_result, 'rodas': [], 'summary': ''}

        intents = ai_result.get('intents', [])
        rodas = ai_result.get('rodas', [])

        # Filtrar rodas validando direcao + beneficiario semantico
        # - promessa: Renato prometeu algo (outgoing)
        # - favor_recebido: contato ajudou Renato (Renato e BENEFICIARIO)
        # - favor_feito: Renato ajudou o contato (Renato e DOADOR, sem acao requerida)
        # - topico, proximo_passo: qualquer direcao
        #
        # IMPORTANTE: nao basta filtrar por direcao. Mensagens incoming de
        # agradecimento ("obrigada pela indicacao") referenciam favor que RENATO
        # fez, nao recebeu. O campo `beneficiario` do JSON da IA resolve isso.
        thank_you_pattern = re.compile(
            r'\b(obrigad[ao]|valeu|grat[ao]|thanks?|obg|gracias)\b',
            re.IGNORECASE
        )
        is_thank_you_msg = bool(thank_you_pattern.search(message_text or ''))

        filtered_rodas = []
        for roda in rodas:
            tipo = roda.get('tipo', '')
            beneficiario = (roda.get('beneficiario') or '').lower()

            if tipo == 'promessa' and message_direction != 'outgoing':
                continue  # Promessa so vale se Renato enviou

            if tipo == 'favor_recebido':
                # Beneficiario tem que ser Renato; se ausente, exigir incoming + nao-agradecimento
                if beneficiario and beneficiario != 'renato':
                    print(f"[RODAS] Pulando favor_recebido: beneficiario={beneficiario}")
                    continue
                if message_direction != 'incoming':
                    continue
                if is_thank_you_msg and beneficiario != 'renato':
                    # Heuristica defensiva: agradecimento incoming sem beneficiario explicito
                    # quase sempre significa que RENATO foi o doador
                    print(f"[RODAS] Pulando favor_recebido: mensagem de agradecimento sem beneficiario explicito")
                    # Reescreve como favor_feito (Renato doou)
                    roda['tipo'] = 'favor_feito'
                    roda['beneficiario'] = 'contato'
                    filtered_rodas.append(roda)
                    continue

            if tipo == 'favor_feito':
                # Renato ajudou o contato; aceita qualquer direcao
                # (Renato pode ter mencionado outgoing, ou contato pode ter agradecido incoming)
                if beneficiario and beneficiario == 'renato':
                    # Inconsistencia: tipo diz favor_feito mas beneficiario diz Renato
                    print(f"[RODAS] Pulando favor_feito inconsistente: beneficiario=renato")
                    continue

            filtered_rodas.append(roda)

        result['rodas'] = filtered_rodas

        # Persistir rodas no banco
        if filtered_rodas and contact_id:
            self._persist_rodas(contact_id, message_id, filtered_rodas)

        # Para mensagens de saida, nao processar intencoes (nao gera action proposals)
        if message_direction == 'outgoing':
            return result

        if not intents:
            return result

        result['intents'] = intents

        # Determinar se requer acao e urgencia
        action_intents = ['reschedule_meeting', 'cancel_meeting', 'urgent_request',
                         'question', 'payment_mention', 'deadline_mention',
                         'introduction_request', 'opportunity_signal', 'complaint',
                         'meeting_request', 'follow_up_needed']

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

## PARTE 1: INTENCOES
Identifique se a mensagem contem alguma destas intencoes:
- reschedule_meeting: Pedido para remarcar reuniao/encontro
- cancel_meeting: Cancelamento de reuniao/encontro
- confirm_meeting: Confirmacao de reuniao/encontro
- meeting_request: Pedido para agendar NOVA reuniao/call
- urgent_request: Pedido urgente que precisa atencao imediata
- question: Pergunta que precisa de resposta
- payment_mention: Mencao a pagamento, cobranca ou valores
- deadline_mention: Mencao a prazo, data limite
- introduction_request: Pedido de apresentacao, indicacao ou conexao com outra pessoa
- opportunity_signal: Sinal de interesse em fazer negocio, contratar servico, comprar
- complaint: Reclamacao, insatisfacao ou feedback negativo
- follow_up_needed: Assunto que ficou pendente e precisa de acompanhamento
- important_info: Informacao importante que Renato precisa saber
- none: Mensagem trivial, agradecimento, etc.

## PARTE 2: RODAS DE RELACIONAMENTO (MUITO SELETIVO)
Extraia rodas APENAS se TODOS os criterios forem atendidos:
1. Contexto PROFISSIONAL ou NETWORKING (nao familiar, nao romantico)
2. Acao concreta que pode ser esquecida se nao registrada
3. Identificacao CLARA de quem fez/promete o que para quem

TIPOS VALIDOS:
- promessa: RENATO prometeu ENTREGAR algo ao CONTATO ("envio a proposta", "te apresento ao diretor")
- favor_recebido: CONTATO ajudou RENATO. RENATO e o BENEFICIARIO. Ex: contato escreve "te indiquei pro fulano", ou Renato escreve "obrigado por me indicar"
- favor_feito: RENATO ajudou o CONTATO. RENATO e o DOADOR. Ex: Renato escreve "te indiquei a Wanelise", ou contato escreve "obrigada pela indicacao"
- topico: PROJETO ou NEGOCIO discutido que pode gerar oportunidade
- proximo_passo: COMPROMISSO PROFISSIONAL agendado ("reuniao segunda", "proposta ate sexta")

## REGRA CRITICA: BENEFICIARIO DO FAVOR
Antes de classificar como favor_recebido OU favor_feito, identifique:
- Quem PERFORMOU a acao (subject)?
- Quem RECEBEU o beneficio (object)?

Mensagem INCOMING (do contato) com "obrigado/obrigada/valeu pela indicacao/apresentacao/ajuda"
  → contato esta AGRADECENDO Renato
  → significa que RENATO foi o doador
  → tipo = "favor_feito", beneficiario = "contato"
  → NAO classifique como favor_recebido

Mensagem OUTGOING (do Renato) com "obrigado por me indicar/apresentar"
  → Renato esta agradecendo o contato
  → contato foi o doador
  → tipo = "favor_recebido", beneficiario = "renato"

Mensagem INCOMING com "te indiquei", "vou te apresentar a fulano", "vou te ajudar"
  → contato fez/vai fazer algo POR Renato
  → tipo = "favor_recebido", beneficiario = "renato"

Mensagem OUTGOING com "te indiquei a fulano", "vou te apresentar ao fulano"
  → Renato fez/vai fazer algo POR contato
  → tipo = "favor_feito" (se ja realizado) OU "promessa" (se futuro)

RETORNE rodas: [] se:
- Mensagem entre familiares ou casal
- Conversa social sem contexto de negocios
- Cumprimento generico, rotina
- Nao da pra identificar quem ajudou quem
- Na duvida sobre beneficiario

CONTEUDO da roda DEVE preservar o sujeito explicitamente.
  RUIM: "indicacao de advogada Wanelise"
  BOM: "Renato indicou a advogada Wanelise para o contato"
  BOM: "Contato indicou Renato como palestrante para o evento X"

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
    "rodas": [
        {{
            "tipo": "promessa" ou "favor_recebido" ou "favor_feito" ou "topico" ou "proximo_passo",
            "conteudo": "descricao com sujeito explicito (quem fez o que para quem)",
            "beneficiario": "renato" ou "contato" ou null (so para favor_recebido/favor_feito)",
            "prazo": "data ou prazo mencionado, se houver (null se nao houver)",
            "tags": ["palavras-chave", "relevantes"],
            "confidence": 0.0 a 1.0
        }}
    ],
    "summary": "resumo de uma linha do que a mensagem comunica"
}}

Se nao detectar intencoes ou rodas, retorne arrays vazios."""

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
            # Retorna dict com intents e rodas
            return {
                'intents': data.get('intents', []),
                'rodas': data.get('rodas', []),
                'summary': data.get('summary', '')
            }
        except json.JSONDecodeError as e:
            print(f"Failed to parse Claude response: {e}")
            return {'intents': self._detect_intent_keywords(message_text), 'rodas': [], 'summary': ''}

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

        # Urgente (nao incluir 'agora' - muito generico, ex: 'consegui parar so agora')
        if any(word in text_lower for word in ['urgente', 'urgencia', 'imediato', 'socorro', 'emergencia', 'preciso ja', 'sos']):
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

        # Pedido de apresentacao/indicacao
        if any(word in text_lower for word in ['apresentar', 'indicar', 'conhece alguem', 'voce conhece', 'pode me conectar', 'contato de']):
            intents.append({
                'type': 'introduction_request',
                'confidence': 0.75,
                'urgency': 'medium',
                'details': {'action_needed': 'Avaliar indicacao'}
            })

        # Sinal de oportunidade
        if any(word in text_lower for word in ['interesse', 'orcamento', 'proposta', 'quanto custa', 'preciso de', 'gostaria de contratar', 'fechar negocio', 'vamos fechar']):
            intents.append({
                'type': 'opportunity_signal',
                'confidence': 0.80,
                'urgency': 'high',
                'details': {'action_needed': 'Explorar oportunidade'}
            })

        # Reclamacao (evitar 'problema' puro - ex: 'nao tem problema' = resposta positiva)
        # Tambem excluir 'ruim' puro pois pode ser 'nao esta ruim'
        complaint_phrases = ['insatisfeito', 'reclamar', 'estou com problema', 'tive problema', 'tenho um problema serio',
                             'nao funcionou', 'nao funciona', 'decepcionado', 'pessimo', 'horrivel', 'inaceitavel',
                             'muito ruim', 'esta ruim', 'foi ruim', 'que absurdo', 'nao aguento mais']
        # Excluir se a mensagem comeca com negacao de problema
        negacoes_problema = ['nao tem problema', 'sem problema', 'tudo bem', 'sem problemas']
        has_complaint_phrase = any(phrase in text_lower for phrase in complaint_phrases)
        has_negation = any(neg in text_lower for neg in negacoes_problema)
        if has_complaint_phrase and not has_negation:
            intents.append({
                'type': 'complaint',
                'confidence': 0.80,
                'urgency': 'high',
                'details': {'action_needed': 'Resolver reclamacao'}
            })

        # Pedido de reuniao nova
        if any(word in text_lower for word in ['marcar reuniao', 'agendar call', 'podemos conversar', 'bater um papo', 'tomar um cafe', 'encontrar']):
            intents.append({
                'type': 'meeting_request',
                'confidence': 0.75,
                'urgency': 'medium',
                'details': {'action_needed': 'Agendar reuniao'}
            })

        # Follow-up necessario
        if any(word in text_lower for word in ['e aquele', 'como ficou', 'alguma novidade', 'tem retorno', 'e sobre', 'lembra que']):
            intents.append({
                'type': 'follow_up_needed',
                'confidence': 0.70,
                'urgency': 'medium',
                'details': {'action_needed': 'Dar retorno'}
            })

        return intents

    def _persist_rodas(self, contact_id: int, message_id: int, rodas: List[Dict]) -> None:
        """
        Persiste rodas extraidas no banco de dados.

        Args:
            contact_id: ID do contato
            message_id: ID da mensagem de origem
            rodas: Lista de rodas extraidas pela IA
        """
        try:
            service = get_rodas_service()
            for roda in rodas:
                tipo = roda.get('tipo', '')
                conteudo = roda.get('conteudo', '')
                confidence = roda.get('confidence', 0.5)
                prazo = roda.get('prazo')
                tags = roda.get('tags', [])

                # Validar tipo
                if tipo not in ['promessa', 'favor_recebido', 'favor_feito', 'topico', 'proximo_passo']:
                    continue

                # Conteudo minimo
                if not conteudo or len(conteudo) < 5:
                    continue

                # Confianca minima
                if confidence < 0.6:
                    continue

                service.create_roda(
                    contact_id=contact_id,
                    tipo=tipo,
                    conteudo=conteudo,
                    message_id=message_id,
                    tags=tags,
                    prazo=prazo,
                    confidence=confidence
                )
        except Exception as e:
            print(f"Error persisting rodas: {e}")

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

            elif intent_type == 'introduction_request' and confidence >= 0.7:
                proposal = {
                    'action_type': 'introduction_request',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': urgency,
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Pedido de apresentacao ou indicacao",
                    'title': f"Indicacao: {contact_name}",
                    'description': f"Pedido de apresentacao: {message_text[:150]}",
                    'action_params': {},
                    'options': [
                        {'id': 'respond', 'label': 'Responder agora', 'action': 'open_conversation'},
                        {'id': 'task', 'label': 'Avaliar indicacao', 'action': 'create_task'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

            elif intent_type == 'opportunity_signal' and confidence >= 0.7:
                proposal = {
                    'action_type': 'opportunity_alert',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': 'high',  # Oportunidades sao sempre alta prioridade
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Sinal de oportunidade de negocio detectado",
                    'title': f"🎯 Oportunidade: {contact_name}",
                    'description': f"Possivel interesse comercial: {message_text[:150]}",
                    'action_params': {},
                    'options': [
                        {'id': 'respond_now', 'label': 'Responder AGORA', 'action': 'open_conversation'},
                        {'id': 'schedule_call', 'label': 'Agendar call', 'action': 'create_task'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

            elif intent_type == 'complaint' and confidence >= 0.7:
                proposal = {
                    'action_type': 'complaint_alert',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': 'high',  # Reclamacoes sao sempre alta prioridade
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Reclamacao ou feedback negativo detectado",
                    'title': f"⚠️ Reclamacao: {contact_name}",
                    'description': f"Feedback negativo: {message_text[:150]}",
                    'action_params': {},
                    'options': [
                        {'id': 'respond_now', 'label': 'Resolver agora', 'action': 'open_conversation'},
                        {'id': 'task', 'label': 'Criar tarefa', 'action': 'create_task'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

            elif intent_type == 'meeting_request' and confidence >= 0.7:
                proposal = {
                    'action_type': 'meeting_request',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': urgency,
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Pedido de nova reuniao",
                    'title': f"Agendar: {contact_name}",
                    'description': f"Pedido de reuniao: {message_text[:150]}",
                    'action_params': {
                        'suggested_date': details.get('extracted_date'),
                        'suggested_time': details.get('extracted_time')
                    },
                    'options': [
                        {'id': 'schedule', 'label': 'Abrir agenda', 'action': 'open_conversation'},
                        {'id': 'task', 'label': 'Criar tarefa', 'action': 'create_task'},
                        {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
                    ]
                }
                proposals.append(proposal)

            elif intent_type == 'follow_up_needed' and confidence >= 0.7:
                proposal = {
                    'action_type': 'follow_up_alert',
                    'contact_id': contact_id,
                    'message_id': message_id,
                    'confidence': confidence,
                    'urgency': urgency,
                    'trigger_text': message_text[:300],
                    'ai_reasoning': f"Assunto pendente precisa de retorno",
                    'title': f"Follow-up: {contact_name}",
                    'description': f"Aguardando retorno: {message_text[:150]}",
                    'action_params': {},
                    'options': [
                        {'id': 'respond', 'label': 'Dar retorno', 'action': 'open_conversation'},
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
