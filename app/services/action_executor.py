"""
Action Executor Service - Executa acoes confirmadas pelo usuario

Acoes suportadas:
- reschedule_event: Remarcar evento no Google Calendar
- cancel_event: Cancelar evento
- confirm_event: Confirmar presenca em evento
- create_task: Criar tarefa
- open_conversation: Abrir conversa (apenas retorna dados)
"""
import json
from typing import Dict, Optional
from datetime import datetime, timedelta
from database import get_db
from services.action_proposals import get_action_proposals
from services.calendar_events import get_calendar_events


class ActionExecutor:
    """Executa acoes apos confirmacao do usuario."""

    def __init__(self):
        self.proposals_service = get_action_proposals()
        self.calendar_service = get_calendar_events()

    async def execute(self, proposal_id: int, option_id: str = None, custom_params: Dict = None) -> Dict:
        """
        Executa a acao escolhida pelo usuario.

        Args:
            proposal_id: ID da proposta de acao
            option_id: ID da opcao escolhida
            custom_params: Parametros adicionais

        Returns:
            {
                'success': bool,
                'action_type': str,
                'result': dict,
                'message': str
            }
        """
        # Buscar proposta
        proposal = self.proposals_service.get_proposal(proposal_id)
        if not proposal:
            return {
                'success': False,
                'action_type': None,
                'result': None,
                'message': 'Proposta nao encontrada'
            }

        if proposal['status'] != 'pending':
            return {
                'success': False,
                'action_type': proposal['action_type'],
                'result': None,
                'message': f'Proposta ja foi processada (status: {proposal["status"]})'
            }

        action_type = proposal['action_type']
        action_params = proposal.get('action_params', {})
        options = proposal.get('options', [])

        # Encontrar opcao escolhida
        chosen_option = None
        if option_id:
            for opt in options:
                if opt.get('id') == option_id:
                    chosen_option = opt
                    break

        # Se opcao eh dismiss/ignore, apenas rejeitar
        if chosen_option and chosen_option.get('action') == 'dismiss':
            self.proposals_service.dismiss_proposal(proposal_id)
            return {
                'success': True,
                'action_type': action_type,
                'result': {'dismissed': True},
                'message': 'Proposta ignorada'
            }

        # Executar acao baseada no tipo
        result = None
        message = ''

        try:
            if action_type == 'reschedule_event':
                result = await self.reschedule_event(action_params, chosen_option, custom_params)
                message = result.get('message', 'Evento remarcado')

            elif action_type == 'cancel_event':
                result = await self.cancel_event(action_params, chosen_option)
                message = result.get('message', 'Evento cancelado')

            elif action_type == 'confirm_event':
                result = await self.confirm_event(action_params)
                message = result.get('message', 'Evento confirmado')

            elif action_type == 'urgent_alert':
                result = await self.handle_urgent(proposal, chosen_option, custom_params)
                message = result.get('message', 'Alerta processado')

            elif action_type == 'pending_response':
                result = await self.handle_pending_response(proposal, chosen_option, custom_params)
                message = result.get('message', 'Resposta registrada')

            elif action_type == 'financial_alert':
                result = await self.handle_financial(proposal, chosen_option, custom_params)
                message = result.get('message', 'Alerta financeiro processado')

            else:
                # Acao generica - apenas marcar como executada
                result = {'generic': True, 'action_type': action_type}
                message = f'Acao {action_type} registrada'

            # Verificar se a acao teve sucesso antes de marcar como executada
            if result.get('success', True) is not False:
                # Marcar proposta como executada apenas se teve sucesso
                self.proposals_service.mark_executed(proposal_id, result)
                return {
                    'success': True,
                    'action_type': action_type,
                    'result': result,
                    'message': message
                }
            else:
                # Acao falhou - nao marcar como executada
                return {
                    'success': False,
                    'action_type': action_type,
                    'result': result,
                    'message': result.get('message', 'Acao falhou')
                }

        except Exception as e:
            return {
                'success': False,
                'action_type': action_type,
                'result': {'error': str(e)},
                'message': f'Erro ao executar acao: {str(e)}'
            }

    async def reschedule_event(
        self,
        action_params: Dict,
        chosen_option: Dict = None,
        custom_params: Dict = None
    ) -> Dict:
        """Remarca evento no calendario"""
        event_id = action_params.get('event_id')
        google_event_id = action_params.get('google_event_id')

        if not event_id:
            return {'success': False, 'message': 'Evento nao especificado'}

        # Determinar nova data
        new_datetime = None

        if custom_params and custom_params.get('new_datetime'):
            # Data especifica fornecida
            new_datetime = datetime.fromisoformat(custom_params['new_datetime'])
        elif chosen_option:
            option_params = chosen_option.get('params', {})
            days_offset = option_params.get('days', 1)

            # Buscar evento atual para manter o horario
            event = self.calendar_service.get_event(event_id)
            if event and event.get('start_datetime'):
                original_dt = datetime.fromisoformat(event['start_datetime'])
                new_datetime = original_dt + timedelta(days=days_offset)
            else:
                # Fallback: amanha no mesmo horario que seria o original
                new_datetime = datetime.now().replace(hour=10, minute=0) + timedelta(days=days_offset)
        else:
            # Default: amanha
            new_datetime = datetime.now().replace(hour=10, minute=0) + timedelta(days=1)

        # Calcular end_datetime (assumir mesma duracao original ou 1h)
        event = self.calendar_service.get_event(event_id)
        if event and event.get('start_datetime') and event.get('end_datetime'):
            original_start = datetime.fromisoformat(event['start_datetime'])
            original_end = datetime.fromisoformat(event['end_datetime'])
            duration = original_end - original_start
            new_end_datetime = new_datetime + duration
        else:
            new_end_datetime = new_datetime + timedelta(hours=1)

        # Atualizar evento
        updated_event = self.calendar_service.update_event(
            event_id,
            {
                'start_datetime': new_datetime,
                'end_datetime': new_end_datetime
            },
            sync_to_google=True
        )

        if updated_event:
            return {
                'success': True,
                'message': f'Evento remarcado para {new_datetime.strftime("%d/%m/%Y %H:%M")}',
                'event': updated_event,
                'new_datetime': new_datetime.isoformat()
            }
        else:
            return {'success': False, 'message': 'Falha ao remarcar evento'}

    async def cancel_event(self, action_params: Dict, chosen_option: Dict = None) -> Dict:
        """Cancela evento no calendario"""
        event_id = action_params.get('event_id')

        if not event_id:
            return {'success': False, 'message': 'Evento nao especificado'}

        # Se opcao foi reschedule em vez de cancel, remarcar
        if chosen_option and chosen_option.get('action') == 'reschedule':
            return await self.reschedule_event(action_params, chosen_option)

        # Cancelar evento
        success = self.calendar_service.delete_event(event_id, delete_from_google=True)

        if success:
            return {
                'success': True,
                'message': 'Evento cancelado com sucesso',
                'event_id': event_id
            }
        else:
            return {'success': False, 'message': 'Falha ao cancelar evento'}

    async def confirm_event(self, action_params: Dict) -> Dict:
        """Confirma presenca em evento"""
        event_id = action_params.get('event_id')

        if not event_id:
            return {'success': False, 'message': 'Evento nao especificado'}

        # Atualizar status do evento
        updated = self.calendar_service.update_event(
            event_id,
            {'status': 'confirmed'},
            sync_to_google=True
        )

        if updated:
            return {
                'success': True,
                'message': 'Presenca confirmada',
                'event': updated
            }
        else:
            return {'success': False, 'message': 'Falha ao confirmar evento'}

    async def handle_urgent(
        self,
        proposal: Dict,
        chosen_option: Dict = None,
        custom_params: Dict = None
    ) -> Dict:
        """Processa alerta urgente"""
        contact_id = proposal.get('contact_id')
        message_id = proposal.get('message_id')

        action = chosen_option.get('action') if chosen_option else 'acknowledge'

        if action == 'create_task':
            # Criar tarefa
            return await self.create_task_from_proposal(proposal, custom_params)

        elif action == 'open_conversation':
            # Retornar dados para abrir conversa
            return {
                'success': True,
                'message': 'Abrir conversa',
                'action': 'open_conversation',
                'contact_id': contact_id,
                'message_id': message_id
            }

        else:
            # Apenas reconhecer
            return {
                'success': True,
                'message': 'Alerta urgente reconhecido',
                'action': 'acknowledged'
            }

    async def handle_pending_response(
        self,
        proposal: Dict,
        chosen_option: Dict = None,
        custom_params: Dict = None
    ) -> Dict:
        """Processa pergunta pendente"""
        contact_id = proposal.get('contact_id')
        message_id = proposal.get('message_id')

        action = chosen_option.get('action') if chosen_option else 'acknowledge'

        if action == 'create_task':
            return await self.create_task_from_proposal(proposal, custom_params)

        elif action == 'open_conversation':
            return {
                'success': True,
                'message': 'Abrir conversa para responder',
                'action': 'open_conversation',
                'contact_id': contact_id,
                'message_id': message_id
            }

        else:
            return {
                'success': True,
                'message': 'Pergunta registrada',
                'action': 'acknowledged'
            }

    async def handle_financial(
        self,
        proposal: Dict,
        chosen_option: Dict = None,
        custom_params: Dict = None
    ) -> Dict:
        """Processa alerta financeiro"""
        contact_id = proposal.get('contact_id')

        action = chosen_option.get('action') if chosen_option else 'acknowledge'

        if action == 'create_task':
            return await self.create_task_from_proposal(proposal, custom_params)

        elif action == 'open_conversation':
            return {
                'success': True,
                'message': 'Abrir conversa',
                'action': 'open_conversation',
                'contact_id': contact_id
            }

        else:
            return {
                'success': True,
                'message': 'Alerta financeiro registrado',
                'action': 'acknowledged'
            }

    async def create_task_from_proposal(self, proposal: Dict, custom_params: Dict = None) -> Dict:
        """Cria tarefa a partir de uma proposta"""
        with get_db() as conn:
            cursor = conn.cursor()

            title = custom_params.get('task_title') if custom_params else None
            if not title:
                title = proposal.get('title', 'Tarefa do INTEL')

            description = proposal.get('description', '')
            contact_id = proposal.get('contact_id')

            # Definir vencimento baseado na urgencia
            urgency = proposal.get('urgency', 'medium')
            if urgency == 'high':
                due_date = datetime.now() + timedelta(hours=4)
            elif urgency == 'medium':
                due_date = datetime.now() + timedelta(days=1)
            else:
                due_date = datetime.now() + timedelta(days=3)

            cursor.execute("""
                INSERT INTO tasks (titulo, descricao, contact_id, data_vencimento, prioridade, ai_generated, origem)
                VALUES (%s, %s, %s, %s, %s, TRUE, 'action_proposal')
                RETURNING id
            """, (
                title,
                description,
                contact_id,
                due_date,
                9 if urgency == 'high' else 7 if urgency == 'medium' else 5
            ))

            task_id = cursor.fetchone()['id']
            conn.commit()

            return {
                'success': True,
                'message': f'Tarefa criada: {title}',
                'action': 'task_created',
                'task_id': task_id
            }


_action_executor = None


def get_action_executor() -> ActionExecutor:
    global _action_executor
    if _action_executor is None:
        _action_executor = ActionExecutor()
    return _action_executor
