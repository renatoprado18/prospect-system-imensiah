"""
WhatsApp Integration via Evolution API
Handles sending/receiving messages and webhook processing
"""
import os
import re
import httpx
from datetime import datetime
from typing import Optional, Dict, List, Any


# Message Templates
MESSAGE_TEMPLATES = {
    "saudacao": {
        "id": "saudacao",
        "nome": "Saudacao Inicial",
        "categoria": "primeiro_contato",
        "mensagem": "Ola {nome}! Aqui e o Renato da ImensIAH. Tudo bem com voce?",
        "variaveis": ["nome"],
        "descricao": "Primeira mensagem para novo contato"
    },
    "followup_reuniao": {
        "id": "followup_reuniao",
        "nome": "Follow-up Pos-Reuniao",
        "categoria": "followup",
        "mensagem": "Ola {nome}! Foi um prazer conversar com voce hoje. Conforme combinamos, segue o resumo dos pontos principais:\n\n{pontos}\n\nFico a disposicao para qualquer duvida!",
        "variaveis": ["nome", "pontos"],
        "descricao": "Enviar apos reuniao com resumo"
    },
    "lembrete_reuniao": {
        "id": "lembrete_reuniao",
        "nome": "Lembrete de Reuniao",
        "categoria": "lembrete",
        "mensagem": "Ola {nome}! Passando para lembrar da nossa reuniao {quando}. Nos falamos em breve!",
        "variaveis": ["nome", "quando"],
        "descricao": "Lembrar de reuniao agendada"
    },
    "envio_proposta": {
        "id": "envio_proposta",
        "nome": "Envio de Proposta",
        "categoria": "comercial",
        "mensagem": "Ola {nome}! Conforme conversamos, segue a proposta para {empresa}. Qualquer duvida, estou a disposicao para alinharmos.",
        "variaveis": ["nome", "empresa"],
        "descricao": "Acompanhar envio de proposta"
    },
    "agradecimento": {
        "id": "agradecimento",
        "nome": "Agradecimento",
        "categoria": "relacionamento",
        "mensagem": "Ola {nome}! Muito obrigado pela conversa de hoje. Foi otimo conhecer mais sobre {assunto}. Vamos nos falando!",
        "variaveis": ["nome", "assunto"],
        "descricao": "Agradecer apos interacao"
    },
    "retomada": {
        "id": "retomada",
        "nome": "Retomada de Contato",
        "categoria": "followup",
        "mensagem": "Ola {nome}! Espero que esteja tudo bem. Faz um tempo que nao nos falamos e gostaria de saber como estao as coisas por ai. Podemos marcar um cafe virtual?",
        "variaveis": ["nome"],
        "descricao": "Retomar contato apos periodo sem interacao"
    },
    "aniversario": {
        "id": "aniversario",
        "nome": "Parabens Aniversario",
        "categoria": "relacionamento",
        "mensagem": "Ola {nome}! Feliz aniversario! Desejo um dia incrivel e um ano repleto de realizacoes. Grande abraco!",
        "variaveis": ["nome"],
        "descricao": "Parabenizar por aniversario"
    },
    "indicacao": {
        "id": "indicacao",
        "nome": "Pedido de Indicacao",
        "categoria": "comercial",
        "mensagem": "Ola {nome}! Espero que esteja bem. Estou expandindo minha rede e gostaria de saber se voce conhece alguem que poderia se beneficiar dos nossos servicos de {servico}. Agradeco qualquer indicacao!",
        "variaveis": ["nome", "servico"],
        "descricao": "Pedir indicacoes de novos contatos"
    }
}


def get_all_templates() -> List[Dict[str, Any]]:
    """Return all available message templates"""
    return list(MESSAGE_TEMPLATES.values())


def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific template by ID"""
    return MESSAGE_TEMPLATES.get(template_id)


def render_template(template_id: str, variables: Dict[str, str]) -> Optional[str]:
    """Render a template with the provided variables"""
    template = MESSAGE_TEMPLATES.get(template_id)
    if not template:
        return None
    message = template["mensagem"]
    for var_name, var_value in variables.items():
        message = message.replace("{" + var_name + "}", str(var_value))
    unreplaced = re.findall(r'\{(\w+)\}', message)
    for var in unreplaced:
        message = message.replace("{" + var + "}", "")
    return message.strip()


def get_templates_by_category(category: str) -> List[Dict[str, Any]]:
    """Get all templates in a specific category"""
    return [t for t in MESSAGE_TEMPLATES.values() if t["categoria"] == category]


class WhatsAppIntegration:
    """
    Integration with Evolution API for WhatsApp
    """

    def __init__(self):
        self.base_url = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
        self.api_key = os.getenv("EVOLUTION_API_KEY", "")
        self.instance = os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp")

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "apikey": self.api_key
        }

    def _normalize_phone(self, phone: str) -> str:
        """
        Normalize phone number to WhatsApp format (only digits)
        Examples:
            +55 11 98415-3337 -> 5511984153337
            (11) 98415-3337 -> 5511984153337
        """
        # Remove all non-digits
        digits = re.sub(r'\D', '', phone)

        # Add Brazil country code if not present
        if len(digits) == 11 and digits.startswith('11'):
            digits = '55' + digits
        elif len(digits) == 10:
            digits = '5511' + digits
        elif len(digits) == 9:
            digits = '55119' + digits

        return digits

    async def get_connection_status(self) -> Dict[str, Any]:
        """Get current connection status"""
        if not self.base_url or not self.api_key:
            return {"error": "Evolution API not configured"}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/instance/connectionState/{self.instance}",
                    headers=self._get_headers(),
                    timeout=10.0
                )
                return response.json()
            except Exception as e:
                return {"error": str(e)}

    async def send_text(self, phone: str, message: str) -> Dict[str, Any]:
        """
        Send a text message to a phone number

        Args:
            phone: Phone number (any format, will be normalized)
            message: Text message to send

        Returns:
            API response with message details
        """
        if not self.base_url or not self.api_key:
            return {"error": "Evolution API not configured"}

        normalized_phone = self._normalize_phone(phone)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/message/sendText/{self.instance}",
                    headers=self._get_headers(),
                    json={
                        "number": normalized_phone,
                        "text": message
                    },
                    timeout=30.0
                )
                return response.json()
            except Exception as e:
                return {"error": str(e)}

    async def send_with_template(self, phone: str, template_id: str, variables: Dict[str, str]) -> Dict[str, Any]:
        """Send a message using a predefined template"""
        template = get_template(template_id)
        if not template:
            return {"error": f"Template '{template_id}' not found"}
        message = render_template(template_id, variables)
        if not message:
            return {"error": "Failed to render template"}
        result = await self.send_text(phone, message)
        if "error" not in result:
            result["template_used"] = {"id": template_id, "nome": template["nome"], "variables": variables}
        return result

    async def send_media(self, phone: str, media_url: str, caption: str = "", media_type: str = "image") -> Dict[str, Any]:
        """
        Send media (image, video, document) to a phone number

        Args:
            phone: Phone number
            media_url: URL of the media file
            caption: Optional caption
            media_type: 'image', 'video', 'audio', 'document'
        """
        if not self.base_url or not self.api_key:
            return {"error": "Evolution API not configured"}

        normalized_phone = self._normalize_phone(phone)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/message/sendMedia/{self.instance}",
                    headers=self._get_headers(),
                    json={
                        "number": normalized_phone,
                        "mediatype": media_type,
                        "media": media_url,
                        "caption": caption
                    },
                    timeout=30.0
                )
                return response.json()
            except Exception as e:
                return {"error": str(e)}

    async def check_whatsapp_number(self, phone: str) -> Dict[str, Any]:
        """
        Check if a phone number has WhatsApp

        Returns:
            {"exists": True/False, "jid": "...@s.whatsapp.net"}
        """
        if not self.base_url or not self.api_key:
            return {"error": "Evolution API not configured"}

        normalized_phone = self._normalize_phone(phone)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/whatsappNumbers/{self.instance}",
                    headers=self._get_headers(),
                    json={"numbers": [normalized_phone]},
                    timeout=10.0
                )
                data = response.json()
                if isinstance(data, list) and len(data) > 0:
                    return data[0]
                return {"exists": False}
            except Exception as e:
                return {"error": str(e)}

    async def get_profile_picture(self, phone: str) -> Optional[str]:
        """Get profile picture URL for a phone number"""
        if not self.base_url or not self.api_key:
            return None

        normalized_phone = self._normalize_phone(phone)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/fetchProfilePictureUrl/{self.instance}",
                    headers=self._get_headers(),
                    json={"number": normalized_phone},
                    timeout=10.0
                )
                data = response.json()
                return data.get("profilePictureUrl")
            except:
                return None

    async def get_all_chats(self, include_groups: bool = False) -> List[Dict[str, Any]]:
        """
        Get all chats/conversations from WhatsApp

        Args:
            include_groups: If True, include group chats with _is_group=True

        Returns:
            List of chat objects with remoteJid, name, lastMessage, etc.
        """
        if not self.base_url or not self.api_key:
            return []

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/findChats/{self.instance}",
                    headers=self._get_headers(),
                    json={},
                    timeout=30.0
                )
                data = response.json()

                chats = []
                for chat in data:
                    remote_jid = chat.get("remoteJid", "") or chat.get("id", "")

                    # Handle groups
                    if remote_jid.endswith("@g.us"):
                        if include_groups:
                            chat["_is_group"] = True
                            chat["_group_id"] = remote_jid
                            chat["_group_name"] = chat.get("name") or chat.get("pushName") or "Grupo"
                            chats.append(chat)
                        continue

                    # Extract phone number from different formats
                    phone = None

                    if remote_jid.endswith("@s.whatsapp.net"):
                        # Standard format: 5511984153337@s.whatsapp.net
                        phone = remote_jid.replace("@s.whatsapp.net", "")
                    elif remote_jid.endswith("@lid"):
                        # LID format - check remoteJidAlt in lastMessage
                        last_msg = chat.get("lastMessage", {})
                        key = last_msg.get("key", {})
                        alt_jid = key.get("remoteJidAlt", "")
                        if alt_jid.endswith("@s.whatsapp.net"):
                            phone = alt_jid.replace("@s.whatsapp.net", "")

                    if phone and phone.isdigit():
                        chat["_phone"] = phone
                        chat["_is_group"] = False
                        chats.append(chat)

                return chats
            except Exception as e:
                print(f"Error fetching chats: {e}")
                return []

    async def get_group_messages(self, group_id: str, my_phone: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get messages from a group where the user participated (sent or was mentioned)

        Args:
            group_id: Group JID (e.g., 120363028921569581@g.us)
            my_phone: User's phone number to filter interactions
            limit: Maximum messages to fetch

        Returns:
            List of messages where user participated, with participant info
        """
        if not self.base_url or not self.api_key:
            return []

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/findMessages/{self.instance}",
                    headers=self._get_headers(),
                    json={
                        "where": {
                            "key": {
                                "remoteJid": group_id
                            }
                        },
                        "limit": limit
                    },
                    timeout=60.0
                )
                data = response.json()

                # Extract records
                if isinstance(data, dict):
                    messages = data.get("messages", {})
                    if isinstance(messages, dict):
                        all_messages = messages.get("records", [])
                    else:
                        all_messages = messages if isinstance(messages, list) else []
                else:
                    all_messages = data if isinstance(data, list) else []

                # Filter for interactions (fromMe=true OR mentions my number)
                my_interactions = []
                for msg in all_messages:
                    key = msg.get("key", {})

                    # Message I sent
                    if key.get("fromMe"):
                        my_interactions.append(msg)
                        continue

                    # Message that mentions me (check participantAlt or message content)
                    participant_alt = key.get("participantAlt", "")
                    if my_phone in participant_alt:
                        my_interactions.append(msg)
                        continue

                    # Check if message content mentions my number or name
                    message = msg.get("message", {})
                    content = ""
                    if "conversation" in message:
                        content = message["conversation"]
                    elif "extendedTextMessage" in message:
                        content = message["extendedTextMessage"].get("text", "")

                    # Check for @mentions (contextInfo.mentionedJid)
                    context_info = message.get("contextInfo") or msg.get("contextInfo") or {}
                    mentioned = context_info.get("mentionedJid", [])
                    for jid in mentioned:
                        if my_phone in jid:
                            my_interactions.append(msg)
                            break

                return my_interactions
            except Exception as e:
                print(f"Error fetching group messages: {e}")
                return []

    def parse_group_message(self, msg: Dict[str, Any], group_name: str) -> Optional[Dict[str, Any]]:
        """
        Parse a group message to extract participant info

        Returns:
            Dict with phone, direction, content, group_name, etc.
        """
        try:
            key = msg.get("key", {})
            message = msg.get("message", {})

            # Get participant phone
            participant_phone = None
            participant_alt = key.get("participantAlt", "")
            if participant_alt.endswith("@s.whatsapp.net"):
                participant_phone = participant_alt.replace("@s.whatsapp.net", "")

            # Direction
            from_me = key.get("fromMe", False)
            direction = "outgoing" if from_me else "incoming"

            # Extract content
            content = None
            message_type = "text"

            if "conversation" in message:
                content = message["conversation"]
            elif "extendedTextMessage" in message:
                content = message["extendedTextMessage"].get("text", "")
            elif "imageMessage" in message:
                content = message["imageMessage"].get("caption", "[Imagem]")
                message_type = "image"
            elif "videoMessage" in message:
                content = message["videoMessage"].get("caption", "[Video]")
                message_type = "video"
            elif "audioMessage" in message:
                content = "[Audio]"
                message_type = "audio"
            elif "documentMessage" in message:
                content = message["documentMessage"].get("fileName", "[Documento]")
                message_type = "document"

            if content is None:
                return None

            # Timestamp
            timestamp = msg.get("messageTimestamp")
            if timestamp:
                if isinstance(timestamp, dict):
                    timestamp = timestamp.get("low", 0)
                dt = datetime.fromtimestamp(int(timestamp))
            else:
                dt = datetime.now()

            return {
                "phone": participant_phone,
                "direction": direction,
                "content": content,
                "message_type": message_type,
                "timestamp": dt,
                "message_id": key.get("id"),
                "push_name": msg.get("pushName"),
                "group_name": group_name,
                "is_group": True
            }
        except Exception as e:
            print(f"Error parsing group message: {e}")
            return None

    async def get_messages_for_chat(self, phone: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get messages from a specific chat

        Args:
            phone: Phone number (will be normalized)
            limit: Maximum number of messages to fetch

        Returns:
            List of message objects
        """
        if not self.base_url or not self.api_key:
            return []

        normalized_phone = self._normalize_phone(phone)
        remote_jid = f"{normalized_phone}@s.whatsapp.net"

        async with httpx.AsyncClient() as client:
            try:
                # First try standard remoteJid format
                response = await client.post(
                    f"{self.base_url}/chat/findMessages/{self.instance}",
                    headers=self._get_headers(),
                    json={
                        "where": {
                            "key": {
                                "remoteJid": remote_jid
                            }
                        },
                        "limit": limit
                    },
                    timeout=60.0
                )
                data = response.json()

                # API returns {"messages": {"records": [...]}}
                messages = []
                if isinstance(data, dict):
                    msg_data = data.get("messages", {})
                    if isinstance(msg_data, dict):
                        messages = msg_data.get("records", [])
                    elif isinstance(msg_data, list):
                        messages = msg_data

                # If no messages found, try fetching recent and filter by remoteJidAlt
                # (Evolution API uses LID format for some contacts)
                if not messages:
                    response = await client.post(
                        f"{self.base_url}/chat/findMessages/{self.instance}",
                        headers=self._get_headers(),
                        json={"limit": 500},  # Fetch more to find matches
                        timeout=60.0
                    )
                    data = response.json()

                    all_messages = []
                    if isinstance(data, dict):
                        msg_data = data.get("messages", {})
                        if isinstance(msg_data, dict):
                            all_messages = msg_data.get("records", [])
                        elif isinstance(msg_data, list):
                            all_messages = msg_data

                    # Filter by remoteJidAlt (contains phone in @s.whatsapp.net format)
                    for msg in all_messages:
                        key = msg.get("key", {})
                        jid_alt = key.get("remoteJidAlt", "")
                        if remote_jid in jid_alt or jid_alt == remote_jid:
                            messages.append(msg)
                            if len(messages) >= limit:
                                break

                return messages

            except Exception as e:
                print(f"Error fetching messages for {phone}: {e}")
                return []

    def parse_stored_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parse a stored message from Evolution API into a normalized format

        Args:
            msg: Raw message object from findMessages

        Returns:
            Normalized message dict or None
        """
        try:
            key = msg.get("key", {})
            message = msg.get("message", {})

            # Get phone from remoteJid or remoteJidAlt (for LID format)
            remote_jid = key.get("remoteJid", "")
            remote_jid_alt = key.get("remoteJidAlt", "")

            # Try standard format first
            if remote_jid.endswith("@s.whatsapp.net"):
                phone = remote_jid.replace("@s.whatsapp.net", "")
            # Fall back to remoteJidAlt for LID format
            elif remote_jid_alt.endswith("@s.whatsapp.net"):
                phone = remote_jid_alt.replace("@s.whatsapp.net", "")
            # Skip group messages
            elif "@g.us" in remote_jid or "@lid" in remote_jid:
                # Check if it's a 1-1 chat using remoteJidAlt
                if remote_jid_alt and "@s.whatsapp.net" in remote_jid_alt:
                    phone = remote_jid_alt.replace("@s.whatsapp.net", "")
                else:
                    return None
            else:
                return None

            # Direction
            from_me = key.get("fromMe", False)
            direction = "outgoing" if from_me else "incoming"

            # Extract content
            content = None
            message_type = "text"

            if "conversation" in message:
                content = message["conversation"]
            elif "extendedTextMessage" in message:
                content = message["extendedTextMessage"].get("text", "")
            elif "imageMessage" in message:
                content = message["imageMessage"].get("caption", "[Imagem]")
                message_type = "image"
            elif "videoMessage" in message:
                content = message["videoMessage"].get("caption", "[Video]")
                message_type = "video"
            elif "audioMessage" in message:
                content = "[Audio]"
                message_type = "audio"
            elif "documentMessage" in message:
                content = message["documentMessage"].get("fileName", "[Documento]")
                message_type = "document"
            elif "stickerMessage" in message:
                content = "[Sticker]"
                message_type = "sticker"

            if content is None:
                return None

            # Timestamp
            timestamp = msg.get("messageTimestamp")
            if timestamp:
                if isinstance(timestamp, dict):
                    timestamp = timestamp.get("low", 0)
                dt = datetime.fromtimestamp(int(timestamp))
            else:
                dt = datetime.now()

            return {
                "phone": phone,
                "direction": direction,
                "content": content,
                "message_type": message_type,
                "timestamp": dt,
                "message_id": key.get("id"),
                "push_name": msg.get("pushName")
            }
        except Exception as e:
            print(f"Error parsing message: {e}")
            return None


def parse_webhook_message(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse incoming webhook payload from Evolution API

    Args:
        payload: Raw webhook payload

    Returns:
        Parsed message dict or None if not a message event
    """
    event = payload.get("event")

    if event == "messages.upsert":
        data = payload.get("data", {})
        key = data.get("key", {})
        message = data.get("message", {})

        # Skip status messages
        if key.get("remoteJid", "").endswith("@s.whatsapp.net") == False:
            return None

        # Extract phone from JID (5511984153337@s.whatsapp.net -> 5511984153337)
        remote_jid = key.get("remoteJid", "")
        phone = remote_jid.replace("@s.whatsapp.net", "")

        # Determine direction
        from_me = key.get("fromMe", False)
        direction = "outgoing" if from_me else "incoming"

        # Extract message content
        content = None
        message_type = "text"

        if "conversation" in message:
            content = message["conversation"]
        elif "extendedTextMessage" in message:
            content = message["extendedTextMessage"].get("text", "")
        elif "imageMessage" in message:
            content = message["imageMessage"].get("caption", "[Imagem]")
            message_type = "image"
        elif "videoMessage" in message:
            content = message["videoMessage"].get("caption", "[Video]")
            message_type = "video"
        elif "audioMessage" in message:
            content = "[Audio]"
            message_type = "audio"
        elif "documentMessage" in message:
            content = message["documentMessage"].get("fileName", "[Documento]")
            message_type = "document"
        elif "stickerMessage" in message:
            content = "[Sticker]"
            message_type = "sticker"

        if content is None:
            return None

        # Get timestamp
        timestamp = data.get("messageTimestamp")
        if timestamp:
            if isinstance(timestamp, dict):
                timestamp = timestamp.get("low", 0)
            dt = datetime.fromtimestamp(int(timestamp))
        else:
            dt = datetime.now()

        return {
            "phone": phone,
            "direction": direction,
            "content": content,
            "message_type": message_type,
            "timestamp": dt,
            "message_id": key.get("id"),
            "push_name": data.get("pushName"),
            "raw": data
        }

    elif event == "messages.update":
        # Message status update (sent -> delivered -> read)
        data = payload.get("data", {})

        # data can be a list of updates
        updates = data if isinstance(data, list) else [data]

        for update in updates:
            key = update.get("key", {})
            update_info = update.get("update", {})

            remote_jid = key.get("remoteJid", "")
            if not remote_jid.endswith("@s.whatsapp.net"):
                continue

            phone = remote_jid.replace("@s.whatsapp.net", "")
            message_id = key.get("id")

            # Determine status from update
            status = None
            status_code = update_info.get("status")

            # Evolution API status codes:
            # 1 = PENDING, 2 = SERVER_ACK (sent), 3 = DELIVERY_ACK (delivered), 4 = READ, 5 = PLAYED
            if status_code == 2:
                status = "sent"
            elif status_code == 3:
                status = "delivered"
            elif status_code == 4:
                status = "read"
            elif status_code == 5:
                status = "played"  # For audio/video

            if status and message_id:
                return {
                    "event": "message_status",
                    "phone": phone,
                    "message_id": message_id,
                    "status": status,
                    "timestamp": datetime.now()
                }

        return None

    elif event == "connection.update":
        # Connection status changed
        data = payload.get("data", {})
        return {
            "event": "connection_update",
            "state": data.get("state"),
            "instance": payload.get("instance")
        }

    return None


def format_phone_display(phone: str) -> str:
    """
    Format phone number for display
    5511984153337 -> +55 (11) 98415-3337
    """
    if not phone:
        return ""

    digits = re.sub(r'\D', '', phone)

    if len(digits) == 13 and digits.startswith('55'):
        # Brazilian mobile with country code
        return f"+{digits[:2]} ({digits[2:4]}) {digits[4:9]}-{digits[9:]}"
    elif len(digits) == 12 and digits.startswith('55'):
        # Brazilian landline with country code
        return f"+{digits[:2]} ({digits[2:4]}) {digits[4:8]}-{digits[8:]}"
    elif len(digits) == 11:
        # Brazilian mobile without country code
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    elif len(digits) == 10:
        # Brazilian landline without country code
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"

    return phone
