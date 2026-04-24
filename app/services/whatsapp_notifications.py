"""
WhatsApp Notifications Service - Notificacoes proativas via intel-bot

Envia notificacoes de action proposals para Renato via intel-bot WhatsApp.
Formato conversacional: Renato responde diretamente ao bot para agir.
"""
import logging
from typing import Dict, Optional, List
from datetime import datetime
from database import get_db

logger = logging.getLogger(__name__)


class WhatsAppNotificationService:
    """Gerencia notificacoes WhatsApp para o INTEL Proativo via intel-bot."""

    async def send_proposal_notification(self, proposal: Dict) -> bool:
        """
        Envia notificacao de proposta para Renato via intel-bot.
        Formato conversacional sem links — Renato responde ao bot.

        Args:
            proposal: Dados da proposta de acao

        Returns:
            True se enviou com sucesso
        """
        from services.intel_bot import send_intel_notification

        message = self._format_proposal_message(proposal)

        try:
            success = await send_intel_notification(message)

            if success:
                self._mark_proposal_notified(proposal['id'])
                logger.info(f"Sent intel-bot notification for proposal {proposal['id']}")
                return True
            else:
                logger.error(f"Failed to send intel-bot notification for proposal {proposal['id']}")
                return False

        except Exception as e:
            logger.error(f"Error sending intel-bot notification: {e}")
            return False

    def _format_proposal_message(self, proposal: Dict) -> str:
        """Formata mensagem conversacional para o intel-bot."""
        action_type = proposal.get('action_type', '')
        contact_name = proposal.get('contact_name', 'Contato')
        proposal_id = proposal.get('id')
        trigger_text = proposal.get('trigger_text', '')

        # Build content based on action type
        if action_type == 'reschedule_event':
            header = f"📅 *{contact_name}* pediu para remarcar:"
            body = f'"{trigger_text[:150]}"'
            params = proposal.get('action_params', {})
            if params.get('original_date'):
                try:
                    dt = datetime.fromisoformat(params['original_date'].replace('Z', ''))
                    body += f"\nEvento original: {dt.strftime('%d/%m %H:%M')}"
                except Exception:
                    pass
            options_text = self._format_options_conversational(proposal, [
                ('remarcar', 'sugiro novos horarios'),
                ('criar tarefa', 'crio um lembrete'),
                ('ignorar', 'descarto'),
            ])

        elif action_type == 'cancel_event':
            header = f"❌ *{contact_name}* cancelou:"
            body = f'"{trigger_text[:150]}"'
            options_text = self._format_options_conversational(proposal, [
                ('confirmar', 'cancelo o evento'),
                ('criar tarefa', 'crio um lembrete para reagendar'),
                ('ignorar', 'descarto'),
            ])

        elif action_type == 'urgent_alert':
            header = f"🚨 *Urgente de {contact_name}:*"
            body = f'"{trigger_text[:200]}"'
            options_text = self._format_options_conversational(proposal, [
                ('responder', 'abro a conversa pra voce'),
                ('criar tarefa', 'crio um lembrete'),
                ('ignorar', 'descarto'),
            ])

        elif action_type == 'pending_response':
            header = f"❓ *{contact_name}* perguntou:"
            body = f'"{trigger_text[:200]}"'
            options_text = self._format_options_conversational(proposal, [
                ('responder', 'abro a conversa pra voce'),
                ('criar tarefa', 'crio um lembrete'),
                ('ignorar', 'descarto'),
            ])

        elif action_type == 'financial_alert':
            header = f"💰 *Financeiro - {contact_name}:*"
            body = f'"{trigger_text[:200]}"'
            options_text = self._format_options_conversational(proposal, [
                ('responder', 'abro a conversa pra voce'),
                ('criar tarefa', 'crio um lembrete'),
                ('ignorar', 'descarto'),
            ])

        elif action_type == 'update_contact_email':
            email = proposal.get('action_params', {}).get('email', '')
            header = f"📧 Email detectado de *{contact_name}*:"
            body = f"{email}"
            options_text = self._format_options_conversational(proposal, [
                ('atualizar', f'salvo o email em {contact_name}'),
                ('ignorar', 'descarto'),
            ])

        elif action_type == 'create_meeting':
            header = f"📅 Possivel reuniao com *{contact_name}*:"
            body = f'"{trigger_text[:200]}"'
            options_text = self._format_options_conversational(proposal, [
                ('criar evento', 'crio no calendario'),
                ('responder depois', 'lembro voce mais tarde'),
                ('ignorar', 'descarto'),
            ])

        else:
            title = proposal.get('title', 'Acao sugerida')
            description = proposal.get('description', '')
            header = f"📌 *{title}*"
            body = f'{description[:200]}' if description else f'"{trigger_text[:200]}"'
            options_text = self._format_options_from_proposal(proposal)

        # Urgency indicator
        urgency = proposal.get('urgency', 'medium')
        urgency_tag = "🔴 " if urgency == 'high' else ""

        message = f"{urgency_tag}{header}\n{body}\n\n{options_text}\n\nRef: #{proposal_id}"
        return message

    def _format_options_conversational(self, proposal: Dict, options: list) -> str:
        """Format options as conversational reply hints."""
        lines = ["Responda aqui:"]
        for keyword, description in options:
            lines.append(f'• "{keyword}" — {description}')
        return "\n".join(lines)

    def _format_options_from_proposal(self, proposal: Dict) -> str:
        """Format options from proposal's options list as conversational text."""
        options = proposal.get('options', [])
        if not options:
            return 'Responda aqui:\n• "ok" — entendido\n• "ignorar" — descarto'

        lines = ["Responda aqui:"]
        for opt in options[:4]:
            label = opt.get('label', '')
            opt_id = opt.get('id', '')
            # Use the label as the keyword hint
            keyword = label.lower().split()[0] if label else opt_id
            lines.append(f'• "{keyword}" — {label}')
        return "\n".join(lines)

    def _mark_proposal_notified(self, proposal_id: int):
        """Marca proposta como notificada."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE action_proposals
                    SET action_params = COALESCE(action_params, '{}'::jsonb) || '{"whatsapp_notified": true}'::jsonb
                    WHERE id = %s
                """, (proposal_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Error marking proposal {proposal_id} as notified: {e}")

    async def send_confirmation(self, message: str):
        """Envia mensagem de confirmacao/resposta para Renato via intel-bot."""
        from services.intel_bot import send_intel_notification

        try:
            await send_intel_notification(message)
        except Exception as e:
            logger.error(f"Error sending confirmation via intel-bot: {e}")

    async def process_reply(self, message_text: str, from_phone: str = None) -> Optional[Dict]:
        """
        Legacy: processa resposta a notificacao via rap-whatsapp.
        Mantido para compatibilidade — respostas agora vao pelo intel-bot.
        Retorna None para nao interferir no fluxo normal.
        """
        logger.debug(f"Legacy process_reply called (ignored): '{message_text}' from {from_phone}")
        return None


_whatsapp_notifications = None


def get_whatsapp_notifications() -> WhatsAppNotificationService:
    global _whatsapp_notifications
    if _whatsapp_notifications is None:
        _whatsapp_notifications = WhatsAppNotificationService()
    return _whatsapp_notifications
