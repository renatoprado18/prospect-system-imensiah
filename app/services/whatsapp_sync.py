"""
WhatsApp Sync Service
Sincroniza mensagens do WhatsApp com contatos do INTEL

Funcionalidades:
- Sync de chats existentes via Evolution API
- Processamento de webhooks em tempo real
- Atualiza ultimo_contato e total_interacoes
- Vincula mensagens a contatos por telefone

Autor: INTEL
Data: 2026-03-26
"""
import os
import re
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any

from database import get_db
from integrations.whatsapp import WhatsAppIntegration, parse_webhook_message
from services.circulos import calcular_score_circulo, calcular_health_score

logger = logging.getLogger(__name__)


class WhatsAppSyncService:
    """
    Service para sincronizar WhatsApp com contatos INTEL.
    """

    def __init__(self):
        self.wa = WhatsAppIntegration()
        self._sync_status = {
            "running": False,
            "started_at": None,
            "completed_at": None,
            "total_chats": 0,
            "processed": 0,
            "linked": 0,
            "messages_saved": 0,
            "errors": 0,
            "last_error": None
        }

    def _normalize_phone(self, phone: str) -> str:
        """Normaliza telefone para formato consistente (apenas digitos)."""
        return re.sub(r'\D', '', str(phone))

    def _find_contact_by_phone(self, phone: str) -> Optional[Dict]:
        """
        Busca contato pelo numero de telefone.
        Tenta match exato e parcial (ultimos 8-9 digitos).
        """
        phone_digits = self._normalize_phone(phone)
        if not phone_digits or len(phone_digits) < 8:
            return None

        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar contatos com telefones
            cursor.execute("""
                SELECT id, nome, telefones
                FROM contacts
                WHERE telefones IS NOT NULL AND telefones::text != '[]'
            """)
            contacts = cursor.fetchall()

            for contact in contacts:
                telefones = contact["telefones"]
                if isinstance(telefones, str):
                    try:
                        telefones = json.loads(telefones)
                    except:
                        telefones = []

                for tel in telefones:
                    if isinstance(tel, dict):
                        tel_number = tel.get("number", "") or tel.get("phone", "")
                    else:
                        tel_number = str(tel)

                    tel_digits = self._normalize_phone(tel_number)
                    if not tel_digits:
                        continue

                    # Match exato
                    if tel_digits == phone_digits:
                        return dict(contact)

                    # Match parcial (ultimos 8-9 digitos)
                    if len(tel_digits) >= 8 and len(phone_digits) >= 8:
                        if tel_digits[-9:] == phone_digits[-9:]:
                            return dict(contact)
                        if tel_digits[-8:] == phone_digits[-8:]:
                            return dict(contact)

            return None

    def _update_contact_interaction(
        self,
        contact_id: int,
        message_count: int,
        latest_date: datetime
    ) -> bool:
        """Atualiza ultimo_contato e total_interacoes do contato."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Buscar valores atuais
                cursor.execute(
                    "SELECT total_interacoes, ultimo_contato FROM contacts WHERE id = %s",
                    (contact_id,)
                )
                current = cursor.fetchone()
                if not current:
                    return False

                current_interactions = current["total_interacoes"] or 0
                current_ultimo = current["ultimo_contato"]

                # Merge valores
                new_interactions = current_interactions + message_count
                new_ultimo = latest_date

                if current_ultimo:
                    try:
                        current_naive = current_ultimo.replace(tzinfo=None) if hasattr(current_ultimo, 'tzinfo') and current_ultimo.tzinfo else current_ultimo
                        latest_naive = latest_date.replace(tzinfo=None) if hasattr(latest_date, 'tzinfo') and latest_date.tzinfo else latest_date
                        new_ultimo = latest_date if latest_naive > current_naive else current_ultimo
                    except:
                        pass

                cursor.execute("""
                    UPDATE contacts
                    SET total_interacoes = %s, ultimo_contato = %s
                    WHERE id = %s
                """, (new_interactions, new_ultimo, contact_id))
                conn.commit()

                return True

        except Exception as e:
            logger.error(f"Erro ao atualizar contato {contact_id}: {e}")
            return False

    def _get_or_create_conversation(self, contact_id: int, phone: str) -> Optional[int]:
        """
        Busca ou cria uma conversa WhatsApp para o contato.
        Retorna o conversation_id.
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Buscar conversa existente
                cursor.execute("""
                    SELECT id FROM conversations
                    WHERE contact_id = %s AND canal = 'whatsapp'
                    LIMIT 1
                """, (contact_id,))
                existing = cursor.fetchone()

                if existing:
                    return existing["id"]

                # Criar nova conversa
                cursor.execute("""
                    INSERT INTO conversations (contact_id, canal, ultimo_mensagem, total_mensagens)
                    VALUES (%s, 'whatsapp', NOW(), 0)
                    RETURNING id
                """, (contact_id,))
                result = cursor.fetchone()
                conn.commit()

                logger.info(f"Criada conversa WhatsApp {result['id']} para contato {contact_id}")
                return result["id"]

        except Exception as e:
            logger.error(f"Erro ao criar conversa para contato {contact_id}: {e}")
            return None

    def _save_messages_to_conversation(
        self,
        conversation_id: int,
        contact_id: int,
        messages: List[Dict]
    ) -> int:
        """
        Salva mensagens na tabela messages.
        Retorna quantidade de mensagens salvas.
        """
        saved = 0
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                for msg in messages:
                    parsed = self.wa.parse_stored_message(msg)
                    if not parsed:
                        continue

                    content = parsed.get("content", "")
                    if not content or content.strip() == "":
                        continue

                    direction = parsed.get("direction", "incoming")
                    timestamp = parsed.get("timestamp")
                    msg_id = parsed.get("message_id", "")

                    # Verificar se ja existe (por message_id ou conteudo+data)
                    if msg_id:
                        cursor.execute("""
                            SELECT id FROM messages
                            WHERE conversation_id = %s AND metadata->>'message_id' = %s
                            LIMIT 1
                        """, (conversation_id, msg_id))
                    else:
                        cursor.execute("""
                            SELECT id FROM messages
                            WHERE conversation_id = %s
                            AND conteudo = %s
                            AND enviado_em = %s
                            LIMIT 1
                        """, (conversation_id, content, timestamp))

                    if cursor.fetchone():
                        continue  # Ja existe

                    # Inserir mensagem
                    metadata = json.dumps({"message_id": msg_id, "source": "whatsapp_sync"}) if msg_id else None

                    cursor.execute("""
                        INSERT INTO messages (conversation_id, contact_id, direcao, conteudo, enviado_em, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (conversation_id, contact_id, direction, content, timestamp, metadata))
                    saved += 1

                    # Se e mensagem outbound (Renato respondeu), dismissar propostas pendentes
                    if direction == "outgoing" and contact_id:
                        try:
                            from services.action_proposals import ActionProposalsService
                            ActionProposalsService().dismiss_stale_on_reply(contact_id, timestamp)
                        except Exception as e:
                            logger.warning(f"Erro ao dismissar propostas apos resposta: {e}")

                # Atualizar totais da conversa
                if saved > 0:
                    cursor.execute("""
                        UPDATE conversations
                        SET total_mensagens = (SELECT COUNT(*) FROM messages WHERE conversation_id = %s),
                            ultimo_mensagem = (SELECT MAX(enviado_em) FROM messages WHERE conversation_id = %s)
                        WHERE id = %s
                    """, (conversation_id, conversation_id, conversation_id))

                conn.commit()

        except Exception as e:
            logger.error(f"Erro ao salvar mensagens: {e}")

        return saved

    async def sync_all_chats(self, include_groups: bool = False) -> Dict[str, Any]:
        """
        Sincroniza todos os chats do WhatsApp com contatos.

        Args:
            include_groups: Se True, inclui grupos

        Returns:
            Estatisticas do sync
        """
        if self._sync_status["running"]:
            return {"error": "Sync já em execução", "status": self._sync_status}

        self._sync_status = {
            "running": True,
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
            "total_chats": 0,
            "processed": 0,
            "linked": 0,
            "messages_saved": 0,
            "errors": 0,
            "last_error": None
        }

        try:
            # Buscar todos os chats
            chats = await self.wa.get_all_chats(include_groups=include_groups)
            self._sync_status["total_chats"] = len(chats)

            for chat in chats:
                try:
                    phone = chat.get("_phone")
                    if not phone:
                        continue

                    # Buscar contato pelo telefone
                    contact = self._find_contact_by_phone(phone)
                    if not contact:
                        self._sync_status["processed"] += 1
                        continue

                    contact_id = contact["id"]

                    # Buscar mensagens do chat
                    messages = await self.wa.get_messages_for_chat(phone, limit=100)

                    if messages:
                        # Contar mensagens e pegar data mais recente
                        msg_count = len(messages)
                        latest_date = None

                        for msg in messages:
                            parsed = self.wa.parse_stored_message(msg)
                            if parsed and parsed.get("timestamp"):
                                if latest_date is None or parsed["timestamp"] > latest_date:
                                    latest_date = parsed["timestamp"]

                        if latest_date:
                            self._update_contact_interaction(contact_id, msg_count, latest_date)

                        # Criar conversa e salvar mensagens na tabela messages
                        conversation_id = self._get_or_create_conversation(contact_id, phone)
                        if conversation_id:
                            saved = self._save_messages_to_conversation(conversation_id, contact_id, messages)
                            self._sync_status["messages_saved"] += saved
                            if saved > 0:
                                logger.info(f"Salvas {saved} mensagens para contato {contact['nome']} (ID: {contact_id})")

                        self._sync_status["linked"] += 1

                    self._sync_status["processed"] += 1

                except Exception as e:
                    logger.error(f"Erro ao processar chat: {e}")
                    self._sync_status["errors"] += 1
                    self._sync_status["last_error"] = str(e)

        except Exception as e:
            logger.error(f"Erro no sync WhatsApp: {e}")
            self._sync_status["errors"] += 1
            self._sync_status["last_error"] = str(e)

        finally:
            self._sync_status["running"] = False
            self._sync_status["completed_at"] = datetime.now().isoformat()

        return self._sync_status

    async def process_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Processa webhook do Evolution API em tempo real.

        Args:
            payload: Payload do webhook

        Returns:
            Resultado do processamento
        """
        result = {"processed": False, "event": None, "contact_id": None}

        try:
            # Parsear mensagem do webhook
            parsed = parse_webhook_message(payload)
            if not parsed:
                return result

            event = parsed.get("event", "message")
            result["event"] = event

            # Se for update de status, ignorar
            if event in ["message_status", "connection_update"]:
                return {"processed": True, "event": event, "data": parsed}

            # Se for mensagem nova
            phone = parsed.get("phone")
            if not phone:
                return result

            # Buscar contato
            contact = self._find_contact_by_phone(phone)
            if not contact:
                # Opcional: criar contato novo
                return {"processed": True, "event": "message", "contact_found": False, "phone": phone}

            contact_id = contact["id"]
            result["contact_id"] = contact_id

            # Atualizar interacao
            timestamp = parsed.get("timestamp", datetime.now())
            self._update_contact_interaction(contact_id, 1, timestamp)

            # Salvar mensagem na tabela messages (via conversation)
            try:
                conversation_id = self._get_or_create_conversation(contact_id, phone)
                if conversation_id:
                    content = parsed.get("content", "")
                    direction = parsed.get("direction", "incoming")
                    msg_id = parsed.get("message_id", "")

                    if content and content.strip():
                        with get_db() as conn:
                            cursor = conn.cursor()

                            # Verificar duplicata
                            if msg_id:
                                cursor.execute("""
                                    SELECT id FROM messages
                                    WHERE conversation_id = %s AND metadata->>'message_id' = %s
                                    LIMIT 1
                                """, (conversation_id, msg_id))
                            else:
                                cursor.execute("""
                                    SELECT id FROM messages
                                    WHERE conversation_id = %s AND conteudo = %s AND enviado_em = %s
                                    LIMIT 1
                                """, (conversation_id, content, timestamp))

                            if not cursor.fetchone():
                                metadata = json.dumps({"message_id": msg_id, "source": "webhook"}) if msg_id else None
                                cursor.execute("""
                                    INSERT INTO messages (conversation_id, contact_id, direcao, conteudo, enviado_em, metadata)
                                    VALUES (%s, %s, %s, %s, %s, %s)
                                """, (conversation_id, contact_id, direction, content, timestamp, metadata))

                                # Atualizar conversa
                                cursor.execute("""
                                    UPDATE conversations
                                    SET total_mensagens = total_mensagens + 1, ultimo_mensagem = %s
                                    WHERE id = %s
                                """, (timestamp, conversation_id))

                                conn.commit()
                                logger.info(f"Mensagem WhatsApp salva para contato {contact_id}")

                                # Se e mensagem outbound (Renato respondeu), dismissar propostas pendentes
                                if direction == "outgoing" and contact_id:
                                    try:
                                        from services.action_proposals import ActionProposalsService
                                        ActionProposalsService().dismiss_stale_on_reply(contact_id, timestamp)
                                    except Exception as e:
                                        logger.warning(f"Erro ao dismissar propostas apos resposta: {e}")

            except Exception as e:
                logger.warning(f"Erro ao salvar mensagem WhatsApp: {e}")

            result["processed"] = True

        except Exception as e:
            logger.error(f"Erro ao processar webhook: {e}")
            result["error"] = str(e)

        return result

    async def sync_single_chat(self, phone: str) -> Dict[str, Any]:
        """
        Sincroniza chat de um numero especifico.
        """
        result = {"success": False, "messages": 0, "saved": 0, "contact_id": None}

        try:
            # Buscar contato
            contact = self._find_contact_by_phone(phone)
            if not contact:
                return {"success": False, "error": "Contato não encontrado"}

            contact_id = contact["id"]
            result["contact_id"] = contact_id
            result["contact_name"] = contact.get("nome")

            # Buscar mensagens
            messages = await self.wa.get_messages_for_chat(phone, limit=100)
            result["messages"] = len(messages)

            if messages:
                latest_date = None
                for msg in messages:
                    parsed = self.wa.parse_stored_message(msg)
                    if parsed and parsed.get("timestamp"):
                        if latest_date is None or parsed["timestamp"] > latest_date:
                            latest_date = parsed["timestamp"]

                if latest_date:
                    self._update_contact_interaction(contact_id, len(messages), latest_date)

                # Salvar mensagens na tabela messages
                conversation_id = self._get_or_create_conversation(contact_id, phone)
                if conversation_id:
                    saved = self._save_messages_to_conversation(conversation_id, contact_id, messages)
                    result["saved"] = saved
                    logger.info(f"Sync single chat: {saved} mensagens salvas para {contact['nome']}")

            result["success"] = True

        except Exception as e:
            logger.error(f"Erro ao sincronizar chat {phone}: {e}")
            result["error"] = str(e)

        return result

    def get_sync_status(self) -> Dict[str, Any]:
        """Retorna status atual do sync."""
        return self._sync_status.copy()


# Singleton instance
_whatsapp_sync_service = None


def get_whatsapp_sync_service() -> WhatsAppSyncService:
    """Retorna instancia singleton do servico."""
    global _whatsapp_sync_service
    if _whatsapp_sync_service is None:
        _whatsapp_sync_service = WhatsAppSyncService()
    return _whatsapp_sync_service
