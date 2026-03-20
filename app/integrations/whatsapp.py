"""
WhatsApp Integration via Evolution API
Handles sending/receiving messages and webhook processing
"""
import os
import re
import httpx
from datetime import datetime
from typing import Optional, Dict, List, Any


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
