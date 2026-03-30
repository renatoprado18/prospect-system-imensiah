"""
Evolution API Integration
Cliente para comunicação com Evolution API (WhatsApp via Baileys)

Docs: https://doc.evolution-api.com/v2/en
GitHub: https://github.com/EvolutionAPI/evolution-api
"""
import os
import httpx
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class EvolutionAPIClient:
    """Cliente para Evolution API v2"""

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        instance_name: str = None
    ):
        self.base_url = (base_url or os.getenv("EVOLUTION_API_URL", "")).rstrip('/')
        self.api_key = api_key or os.getenv("EVOLUTION_API_KEY", "")
        self.instance_name = instance_name or os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp")

        self.headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json"
        }

    @property
    def is_configured(self) -> bool:
        """Verifica se a API está configurada"""
        return bool(self.base_url and self.api_key)

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Dict = None,
        timeout: float = 30.0
    ) -> Dict:
        """Faz requisição à API"""
        if not self.is_configured:
            return {"error": "Evolution API não configurada"}

        url = f"{self.base_url}{endpoint}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    response = await client.get(url, headers=self.headers)
                elif method == "POST":
                    response = await client.post(url, headers=self.headers, json=data)
                elif method == "PUT":
                    response = await client.put(url, headers=self.headers, json=data)
                elif method == "DELETE":
                    response = await client.delete(url, headers=self.headers)
                else:
                    return {"error": f"Método não suportado: {method}"}

                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 201:
                    return response.json()
                else:
                    error_text = response.text
                    logger.error(f"Evolution API error: {response.status_code} - {error_text}")
                    return {"error": error_text, "status_code": response.status_code}

        except httpx.TimeoutException:
            logger.error(f"Evolution API timeout: {url}")
            return {"error": "Timeout na requisição"}
        except Exception as e:
            logger.error(f"Evolution API exception: {e}")
            return {"error": str(e)}

    # ==================== INSTANCE MANAGEMENT ====================

    async def create_instance(self, instance_name: str = None) -> Dict:
        """Cria nova instância do WhatsApp"""
        name = instance_name or self.instance_name
        return await self._request("POST", "/instance/create", {
            "instanceName": name,
            "integration": "WHATSAPP-BAILEYS",
            "qrcode": True,
            "number": "",
            "token": ""
        })

    async def get_instance_status(self, instance_name: str = None) -> Dict:
        """Obtém status da instância"""
        name = instance_name or self.instance_name
        return await self._request("GET", f"/instance/connectionState/{name}")

    async def get_all_instances(self) -> Dict:
        """Lista todas as instâncias"""
        return await self._request("GET", "/instance/fetchInstances")

    async def delete_instance(self, instance_name: str = None) -> Dict:
        """Deleta uma instância"""
        name = instance_name or self.instance_name
        return await self._request("DELETE", f"/instance/delete/{name}")

    async def logout_instance(self, instance_name: str = None) -> Dict:
        """Desconecta a instância (logout)"""
        name = instance_name or self.instance_name
        return await self._request("DELETE", f"/instance/logout/{name}")

    async def restart_instance(self, instance_name: str = None) -> Dict:
        """Reinicia a instância"""
        name = instance_name or self.instance_name
        return await self._request("PUT", f"/instance/restart/{name}")

    # ==================== CONNECTION ====================

    async def get_qr_code(self, instance_name: str = None) -> Dict:
        """
        Obtém QR Code para conexão.

        Resposta da Evolution API:
        {
            "pairingCode": "WZYEH1YY",
            "code": "2@y8eK+bjtEjUWy9/...",
            "base64": "data:image/png;base64,iVBORw0KGgo...",
            "count": 1
        }
        """
        name = instance_name or self.instance_name
        result = await self._request("GET", f"/instance/connect/{name}")

        if "error" in result:
            return result

        # Formato padrão v2: base64 direto no root
        return {
            "qr_base64": result.get("base64"),
            "qr_code": result.get("code"),
            "pairingCode": result.get("pairingCode"),
            "count": result.get("count", 0)
        }

    async def check_connection(self, instance_name: str = None) -> Dict:
        """Verifica se está conectado"""
        status = await self.get_instance_status(instance_name)

        if "error" in status:
            return {"connected": False, "state": "error", "error": status["error"]}

        state = status.get("state") or status.get("instance", {}).get("state")

        return {
            "connected": state == "open",
            "state": state,
            "instance": status.get("instance", {})
        }

    # ==================== MESSAGES ====================

    async def send_text(
        self,
        phone: str,
        message: str,
        instance_name: str = None
    ) -> Dict:
        """Envia mensagem de texto"""
        name = instance_name or self.instance_name

        # Normalizar telefone (remover caracteres não numéricos)
        phone_clean = ''.join(filter(str.isdigit, phone))

        # Garantir formato correto (com código do país)
        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        return await self._request("POST", f"/message/sendText/{name}", {
            "number": phone_clean,
            "text": message,
            "delay": 1200  # Delay para parecer mais humano
        })

    async def send_media(
        self,
        phone: str,
        media_url: str,
        caption: str = "",
        media_type: str = "image",
        instance_name: str = None
    ) -> Dict:
        """Envia mídia (imagem, vídeo, documento, áudio)"""
        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))

        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        endpoint = f"/message/sendMedia/{name}"

        return await self._request("POST", endpoint, {
            "number": phone_clean,
            "mediatype": media_type,
            "media": media_url,
            "caption": caption
        })

    async def send_document(
        self,
        phone: str,
        document_url: str,
        filename: str,
        caption: str = "",
        instance_name: str = None
    ) -> Dict:
        """Envia documento"""
        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))

        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        return await self._request("POST", f"/message/sendWhatsAppAudio/{name}", {
            "number": phone_clean,
            "mediatype": "document",
            "media": document_url,
            "fileName": filename,
            "caption": caption
        })

    # ==================== CONTACTS ====================

    async def check_is_whatsapp(
        self,
        phones: List[str],
        instance_name: str = None
    ) -> Dict:
        """Verifica se números têm WhatsApp"""
        name = instance_name or self.instance_name

        # Normalizar telefones
        clean_phones = []
        for phone in phones:
            phone_clean = ''.join(filter(str.isdigit, phone))
            if not phone_clean.startswith('55') and len(phone_clean) <= 11:
                phone_clean = '55' + phone_clean
            clean_phones.append(phone_clean)

        return await self._request("POST", f"/chat/whatsappNumbers/{name}", {
            "numbers": clean_phones
        })

    async def get_profile_picture(
        self,
        phone: str,
        instance_name: str = None
    ) -> Dict:
        """Obtém foto de perfil do contato"""
        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))

        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        return await self._request("GET", f"/chat/fetchProfilePictureUrl/{name}?number={phone_clean}")

    async def get_contacts(self, instance_name: str = None) -> Dict:
        """Lista contatos do WhatsApp"""
        name = instance_name or self.instance_name
        return await self._request("GET", f"/chat/findContacts/{name}")

    # ==================== CHATS ====================

    async def get_chats(self, instance_name: str = None) -> Dict:
        """Lista todas as conversas"""
        name = instance_name or self.instance_name
        return await self._request("GET", f"/chat/findChats/{name}")

    async def get_messages(
        self,
        phone: str,
        limit: int = 100,
        instance_name: str = None
    ) -> Dict:
        """Obtém mensagens de uma conversa"""
        name = instance_name or self.instance_name
        phone_clean = ''.join(filter(str.isdigit, phone))

        if not phone_clean.startswith('55') and len(phone_clean) <= 11:
            phone_clean = '55' + phone_clean

        return await self._request("POST", f"/chat/findMessages/{name}", {
            "where": {
                "key": {
                    "remoteJid": f"{phone_clean}@s.whatsapp.net"
                }
            },
            "limit": limit
        })

    # ==================== WEBHOOKS ====================

    async def set_webhook(
        self,
        webhook_url: str,
        events: List[str] = None,
        instance_name: str = None
    ) -> Dict:
        """
        Configura webhook para receber eventos.

        Eventos disponíveis:
        - MESSAGES_UPSERT: Nova mensagem recebida
        - MESSAGES_UPDATE: Mensagem atualizada (lida, etc)
        - SEND_MESSAGE: Confirmação de envio
        - CONNECTION_UPDATE: Status da conexão mudou
        - QRCODE_UPDATED: Novo QR code gerado
        - CONTACTS_UPSERT: Contato adicionado/atualizado
        """
        name = instance_name or self.instance_name

        default_events = [
            "MESSAGES_UPSERT",
            "MESSAGES_UPDATE",
            "SEND_MESSAGE",
            "CONNECTION_UPDATE",
            "QRCODE_UPDATED"
        ]

        return await self._request("POST", f"/webhook/set/{name}", {
            "url": webhook_url,
            "events": events or default_events,
            "enabled": True,
            "webhookByEvents": True,
            "webhookBase64": False
        })

    async def get_webhook(self, instance_name: str = None) -> Dict:
        """Obtém configuração do webhook"""
        name = instance_name or self.instance_name

        # Try different endpoint formats (varies by Evolution API version)
        endpoints = [
            f"/webhook/find/{name}",
            f"/webhook/{name}",
            f"/instance/fetchInstances"  # Fallback to get instance info
        ]

        for endpoint in endpoints:
            try:
                result = await self._request("GET", endpoint)
                if result and "error" not in result:
                    # For fetchInstances, extract webhook from instance data
                    if "fetchInstances" in endpoint and isinstance(result, list):
                        for inst in result:
                            if inst.get("instanceName") == name:
                                return inst.get("webhook", {})
                    return result
            except Exception as e:
                logger.debug(f"Webhook endpoint {endpoint} failed: {e}")
                continue

        return {"error": "Could not fetch webhook configuration"}


# Singleton
_evolution_client: Optional[EvolutionAPIClient] = None


def get_evolution_client() -> EvolutionAPIClient:
    """Obtém instância singleton do cliente"""
    global _evolution_client
    if _evolution_client is None:
        _evolution_client = EvolutionAPIClient()
    return _evolution_client


# ==================== WEBHOOK HANDLER ====================

async def handle_evolution_webhook(payload: Dict) -> Dict:
    """
    Processa webhook da Evolution API.
    Chamado pelo endpoint POST /api/webhooks/whatsapp

    Eventos principais:
    - messages.upsert: Nova mensagem recebida
    - connection.update: Mudança no status da conexão
    - qrcode.updated: Novo QR Code gerado
    - send.message: Mensagem enviada confirmada
    """
    from database import get_db
    from services.whatsapp_batch_import import get_batch_importer

    event = payload.get("event")
    instance = payload.get("instance")
    data = payload.get("data", {})

    logger.info(f"Evolution webhook: {event} from {instance}")

    result = {"event": event, "processed": False}

    try:
        if event == "messages.upsert":
            # Nova mensagem recebida
            result = await process_incoming_message(data)

        elif event == "connection.update":
            # Status da conexão mudou
            state = data.get("state")
            logger.info(f"Connection state changed: {state}")
            result = {"event": event, "state": state, "processed": True}

        elif event == "qrcode.updated":
            # Novo QR Code
            logger.info("QR Code updated")
            result = {"event": event, "processed": True}

        elif event == "send.message":
            # Mensagem enviada
            result = await process_sent_message(data)

    except Exception as e:
        logger.exception(f"Error processing webhook: {e}")
        result["error"] = str(e)

    return result


async def process_incoming_message(data: Dict) -> Dict:
    """Processa mensagem recebida"""
    from database import get_db
    from services.inbox import serialize_datetime

    message = data.get("message", {})
    key = data.get("key", {})

    # Extrair dados
    remote_jid = key.get("remoteJid", "")
    from_me = key.get("fromMe", False)
    message_id = key.get("id")

    # Ignorar mensagens de grupos por enquanto
    if "@g.us" in remote_jid:
        return {"processed": False, "reason": "group_message"}

    # Extrair telefone
    phone = remote_jid.replace("@s.whatsapp.net", "")

    # Extrair conteúdo
    content = ""
    message_type = "text"

    if "conversation" in message:
        content = message["conversation"]
    elif "extendedTextMessage" in message:
        content = message["extendedTextMessage"].get("text", "")
    elif "imageMessage" in message:
        content = message["imageMessage"].get("caption", "[Imagem]")
        message_type = "image"
    elif "videoMessage" in message:
        content = message["videoMessage"].get("caption", "[Vídeo]")
        message_type = "video"
    elif "audioMessage" in message:
        content = "[Áudio]"
        message_type = "audio"
    elif "documentMessage" in message:
        content = message["documentMessage"].get("fileName", "[Documento]")
        message_type = "document"
    elif "stickerMessage" in message:
        content = "[Figurinha]"
        message_type = "sticker"

    if not content:
        return {"processed": False, "reason": "empty_content"}

    # Buscar contato pelo telefone
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contato
        cursor.execute("""
            SELECT id, nome FROM contacts
            WHERE telefones::text LIKE %s
            LIMIT 1
        """, (f'%{phone[-8:]}%',))
        contact = cursor.fetchone()

        if not contact:
            logger.info(f"Contact not found for phone: {phone}")
            return {"processed": False, "reason": "contact_not_found", "phone": phone}

        contact_id = contact["id"]

        # Buscar ou criar conversa
        cursor.execute("""
            SELECT id FROM conversations
            WHERE contact_id = %s AND canal = 'whatsapp'
        """, (contact_id,))
        conv = cursor.fetchone()

        if conv:
            conversation_id = conv["id"]
        else:
            cursor.execute("""
                INSERT INTO conversations (contact_id, canal, status, criado_em, atualizado_em)
                VALUES (%s, 'whatsapp', 'open', NOW(), NOW())
                RETURNING id
            """, (contact_id,))
            conversation_id = cursor.fetchone()["id"]

        # Verificar se mensagem já existe
        cursor.execute("""
            SELECT id FROM messages WHERE external_id = %s
        """, (message_id,))

        if cursor.fetchone():
            return {"processed": False, "reason": "duplicate"}

        # Inserir mensagem
        direction = "outgoing" if from_me else "incoming"
        timestamp = datetime.now()

        cursor.execute("""
            INSERT INTO messages
            (conversation_id, contact_id, external_id, direcao, conteudo, enviado_em, metadata, criado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (
            conversation_id,
            contact_id,
            message_id,
            direction,
            content,
            timestamp,
            {"phone": phone, "type": message_type, "from_webhook": True}
        ))

        new_msg_id = cursor.fetchone()["id"]

        # Atualizar conversa
        cursor.execute("""
            UPDATE conversations
            SET ultimo_mensagem = %s,
                total_mensagens = COALESCE(total_mensagens, 0) + 1,
                requer_resposta = %s,
                atualizado_em = NOW()
            WHERE id = %s
        """, (timestamp, not from_me, conversation_id))

        # Atualizar contato
        cursor.execute("""
            UPDATE contacts
            SET ultimo_contato = %s,
                total_interacoes = COALESCE(total_interacoes, 0) + 1
            WHERE id = %s
        """, (timestamp, contact_id))

        conn.commit()

    return {
        "processed": True,
        "message_id": new_msg_id,
        "contact_id": contact_id,
        "direction": direction
    }


async def process_sent_message(data: Dict) -> Dict:
    """Processa confirmação de mensagem enviada"""
    # Por enquanto, apenas log
    logger.info(f"Message sent: {data.get('key', {}).get('id')}")
    return {"processed": True, "event": "send.message"}
