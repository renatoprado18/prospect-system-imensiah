"""
WhatsApp Notifications Service - Notificacoes proativas via WhatsApp

Envia notificacoes de action proposals para Renato via WhatsApp
e processa respostas para executar acoes.
"""
import os
import json
import logging
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from database import get_db

logger = logging.getLogger(__name__)

# Numero do Renato para receber notificacoes
RENATO_PHONE = os.getenv("RENATO_WHATSAPP", "5511984153337")


class WhatsAppNotificationService:
    """Gerencia notificacoes WhatsApp para o INTEL Proativo."""

    def __init__(self):
        self.renato_phone = RENATO_PHONE

    async def send_proposal_notification(self, proposal: Dict) -> bool:
        """
        Envia notificacao de proposta para Renato via WhatsApp.

        Args:
            proposal: Dados da proposta de acao

        Returns:
            True se enviou com sucesso
        """
        from integrations.evolution_api import get_evolution_client

        client = get_evolution_client()
        if not client.is_configured:
            logger.warning("Evolution API not configured, skipping notification")
            return False

        # Montar mensagem
        message = self._format_proposal_message(proposal)

        try:
            # Enviar mensagem
            result = await client.send_text(self.renato_phone, message)

            if "error" not in result:
                # Salvar que notificamos esta proposta
                self._mark_proposal_notified(proposal['id'])
                logger.info(f"Sent WhatsApp notification for proposal {proposal['id']}")
                return True
            else:
                logger.error(f"Failed to send notification: {result.get('error')}")
                return False

        except Exception as e:
            logger.error(f"Error sending WhatsApp notification: {e}")
            return False

    def _format_proposal_message(self, proposal: Dict) -> str:
        """Formata mensagem de notificacao."""
        urgency_emoji = "🔴" if proposal.get('urgency') == 'high' else "🟡"
        action_type = proposal.get('action_type', '')
        contact_name = proposal.get('contact_name', 'Contato')

        # Header
        header = f"{urgency_emoji} *INTEL Proativo*\n\n"

        # Conteudo baseado no tipo
        if action_type == 'reschedule_event':
            content = f"📅 *{contact_name}* pediu para remarcar:\n"
            content += f"_{proposal.get('trigger_text', '')[:150]}_\n\n"

            params = proposal.get('action_params', {})
            if params.get('original_date'):
                try:
                    dt = datetime.fromisoformat(params['original_date'].replace('Z', ''))
                    content += f"Evento original: {dt.strftime('%d/%m %H:%M')}\n\n"
                except:
                    pass

        elif action_type == 'cancel_event':
            content = f"❌ *{contact_name}* cancelou:\n"
            content += f"_{proposal.get('trigger_text', '')[:150]}_\n\n"

        elif action_type == 'urgent_alert':
            content = f"🚨 *Urgente de {contact_name}:*\n"
            content += f"_{proposal.get('trigger_text', '')[:200]}_\n\n"

        elif action_type == 'pending_response':
            content = f"❓ *{contact_name}* perguntou:\n"
            content += f"_{proposal.get('trigger_text', '')[:200]}_\n\n"

        elif action_type == 'financial_alert':
            content = f"💰 *Financeiro - {contact_name}:*\n"
            content += f"_{proposal.get('trigger_text', '')[:200]}_\n\n"

        else:
            content = f"📌 *{proposal.get('title', 'Acao sugerida')}*\n"
            content += f"_{proposal.get('description', '')[:200]}_\n\n"

        # Opcoes com links clicaveis
        options = proposal.get('options', [])
        proposal_id = proposal.get('id')
        base_url = "https://intel.almeida-prado.com/api/action-proposals"

        if options and proposal_id:
            content += "*Clique para executar:*\n"
            for i, opt in enumerate(options[:4], 1):
                emoji = self._get_option_emoji(opt.get('id', ''))
                opt_id = opt.get('id', f'option_{i}')
                # Link direto para executar a acao
                link = f"{base_url}/{proposal_id}/quick-action?option={opt_id}"
                content += f"{emoji} {opt.get('label', f'Opcao {i}')}\n{link}\n\n"

        # Footer
        content += f"_Ref: #{proposal_id}_"

        return header + content

    def _get_option_emoji(self, option_id: str) -> str:
        """Retorna emoji apropriado para a opcao."""
        if 'reschedule' in option_id:
            return "📅"
        elif 'cancel' in option_id:
            return "❌"
        elif 'confirm' in option_id:
            return "✅"
        elif 'respond' in option_id or 'open' in option_id:
            return "💬"
        elif 'task' in option_id:
            return "📝"
        elif 'ignore' in option_id or 'dismiss' in option_id:
            return "🚫"
        return ""

    def _mark_proposal_notified(self, proposal_id: int):
        """Marca proposta como notificada."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE action_proposals
                SET action_params = COALESCE(action_params, '{}'::jsonb) || '{"whatsapp_notified": true}'::jsonb
                WHERE id = %s
            """, (proposal_id,))
            conn.commit()

    async def process_reply(self, message_text: str, from_phone: str = None) -> Optional[Dict]:
        """
        Processa resposta do Renato a uma notificacao.
        Nota: A verificacao de que e do Renato ja foi feita no webhook (fromMe=True)

        Args:
            message_text: Texto da mensagem de resposta
            from_phone: Telefone (opcional, para logging)

        Returns:
            Resultado da execucao ou None se nao for resposta a proposal
        """
        logger.info(f"Processing reply: '{message_text}' from {from_phone}")

        text = message_text.strip().lower()

        # Detectar se e uma resposta numerica (1, 2, 3, 4)
        if text in ['1', '2', '3', '4', '1️⃣', '2️⃣', '3️⃣', '4️⃣']:
            option_index = int(text[0]) - 1
            return await self._execute_pending_proposal(option_index)

        # Detectar referencia explicita (#123)
        if text.startswith('#') or 'ref:' in text:
            import re
            match = re.search(r'#?(\d+)', text)
            if match:
                proposal_id = int(match.group(1))
                # Extrair opcao se presente
                option_match = re.search(r'[1-4]', text.replace(match.group(0), ''))
                if option_match:
                    option_index = int(option_match.group(0)) - 1
                    return await self._execute_specific_proposal(proposal_id, option_index)

        # Comandos especiais
        if text in ['ignorar', 'ignore', 'skip', 'pular']:
            return await self._execute_pending_proposal(option_index=-1)  # Dismiss

        if text in ['pendentes', 'pending', 'lista', 'list']:
            return await self._send_pending_summary()

        return None

    async def _execute_pending_proposal(self, option_index: int) -> Dict:
        """Executa a proposta pendente mais recente."""
        from services.action_proposals import get_action_proposals
        from services.action_executor import get_action_executor

        proposals_service = get_action_proposals()
        executor = get_action_executor()

        # Buscar proposta pendente mais recente
        pending = proposals_service.get_pending_proposals(limit=1)

        if not pending:
            await self._send_reply("Nenhuma acao pendente no momento.")
            return {"success": False, "message": "No pending proposals"}

        proposal = pending[0]
        options = proposal.get('options', [])

        # Dismiss
        if option_index == -1 or option_index >= len(options):
            proposals_service.dismiss_proposal(proposal['id'])
            await self._send_reply(f"✅ Proposta #{proposal['id']} ignorada.")
            return {"success": True, "action": "dismissed"}

        # Executar opcao escolhida
        chosen_option = options[option_index]
        result = await executor.execute(proposal['id'], option_id=chosen_option.get('id'))

        # Enviar confirmacao
        if result.get('success'):
            await self._send_reply(f"✅ {result.get('message', 'Acao executada!')}")
        else:
            await self._send_reply(f"❌ Erro: {result.get('message', 'Falha na execucao')}")

        return result

    async def _execute_specific_proposal(self, proposal_id: int, option_index: int) -> Dict:
        """Executa uma proposta especifica por ID."""
        from services.action_proposals import get_action_proposals
        from services.action_executor import get_action_executor

        proposals_service = get_action_proposals()
        executor = get_action_executor()

        proposal = proposals_service.get_proposal(proposal_id)

        if not proposal:
            await self._send_reply(f"Proposta #{proposal_id} nao encontrada.")
            return {"success": False, "message": "Proposal not found"}

        if proposal['status'] != 'pending':
            await self._send_reply(f"Proposta #{proposal_id} ja foi processada.")
            return {"success": False, "message": "Already processed"}

        options = proposal.get('options', [])
        if option_index >= len(options):
            await self._send_reply(f"Opcao invalida para proposta #{proposal_id}.")
            return {"success": False, "message": "Invalid option"}

        chosen_option = options[option_index]
        result = await executor.execute(proposal_id, option_id=chosen_option.get('id'))

        if result.get('success'):
            await self._send_reply(f"✅ {result.get('message', 'Acao executada!')}")
        else:
            await self._send_reply(f"❌ Erro: {result.get('message', 'Falha')}")

        return result

    async def _send_pending_summary(self) -> Dict:
        """Envia resumo das propostas pendentes."""
        from services.action_proposals import get_action_proposals

        service = get_action_proposals()
        pending = service.get_pending_proposals(limit=5)

        if not pending:
            await self._send_reply("✅ Nenhuma acao pendente!")
            return {"success": True, "count": 0}

        message = f"📋 *{len(pending)} acoes pendentes:*\n\n"

        for i, p in enumerate(pending, 1):
            urgency = "🔴" if p.get('urgency') == 'high' else "🟡"
            message += f"{urgency} #{p['id']} - {p.get('title', 'Acao')[:40]}\n"

        message += "\n_Responda com o numero da acao para ver detalhes_"

        await self._send_reply(message)
        return {"success": True, "count": len(pending)}

    async def _send_reply(self, message: str):
        """Envia resposta para Renato."""
        from integrations.evolution_api import get_evolution_client

        client = get_evolution_client()
        if client.is_configured:
            await client.send_text(self.renato_phone, message)


_whatsapp_notifications = None


def get_whatsapp_notifications() -> WhatsAppNotificationService:
    global _whatsapp_notifications
    if _whatsapp_notifications is None:
        _whatsapp_notifications = WhatsAppNotificationService()
    return _whatsapp_notifications
