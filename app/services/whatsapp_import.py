"""
WhatsApp Chat Import Service
Importa historico de conversas de arquivos .txt exportados do WhatsApp.

Formatos suportados:
- iOS: [DD/MM/YYYY, HH:MM:SS] Nome: Mensagem
- Android: DD/MM/YYYY HH:MM - Nome: Mensagem
- Android alt: DD/MM/YY, HH:MM - Nome: Mensagem

Autor: INTEL
"""
import re
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
from database import get_db

logger = logging.getLogger(__name__)


class WhatsAppImportService:
    """Service para importar chats do WhatsApp de arquivos .txt"""

    # Regex patterns para diferentes formatos
    PATTERNS = [
        # iOS: [DD/MM/YYYY, HH:MM:SS] Nome: Mensagem
        re.compile(r'^\[(\d{2}/\d{2}/\d{4}), (\d{2}:\d{2}:\d{2})\] ([^:]+): (.+)$'),
        # Android: DD/MM/YYYY HH:MM - Nome: Mensagem
        re.compile(r'^(\d{2}/\d{2}/\d{4}) (\d{2}:\d{2}) - ([^:]+): (.+)$'),
        # Android alt: DD/MM/YY, HH:MM - Nome: Mensagem
        re.compile(r'^(\d{2}/\d{2}/\d{2}), (\d{2}:\d{2}) - ([^:]+): (.+)$'),
        # Outro formato comum
        re.compile(r'^(\d{1,2}/\d{1,2}/\d{2,4}),? (\d{1,2}:\d{2}(?::\d{2})?)(?:\s?[AP]M)? [-–] ([^:]+): (.+)$'),
    ]

    # Mensagens de sistema para ignorar
    SYSTEM_MESSAGES = [
        'criou este grupo',
        'adicionou',
        'removeu',
        'saiu',
        'mudou o assunto',
        'mudou a imagem',
        'as mensagens e ligacoes',
        'messages and calls are end-to-end encrypted',
        'criptografia de ponta a ponta',
        'alterou as configuracoes',
        'entrou usando o link',
        'agora e admin',
        'deixou de ser admin',
    ]

    def __init__(self):
        self._import_status = {
            "running": False,
            "filename": None,
            "total_lines": 0,
            "parsed_messages": 0,
            "linked_contacts": 0,
            "saved_messages": 0,
            "errors": [],
            "participants": []
        }

    def _parse_date(self, date_str: str, time_str: str) -> Optional[datetime]:
        """Parse date and time strings to datetime object."""
        formats = [
            ('%d/%m/%Y', '%H:%M:%S'),
            ('%d/%m/%Y', '%H:%M'),
            ('%d/%m/%y', '%H:%M'),
            ('%m/%d/%Y', '%H:%M:%S'),
            ('%m/%d/%Y', '%H:%M'),
            ('%m/%d/%y', '%H:%M'),
        ]

        for date_fmt, time_fmt in formats:
            try:
                dt_str = f"{date_str} {time_str}"
                return datetime.strptime(dt_str, f"{date_fmt} {time_fmt}")
            except ValueError:
                continue

        return None

    def _is_system_message(self, content: str) -> bool:
        """Check if message is a system message."""
        content_lower = content.lower()
        return any(sys_msg in content_lower for sys_msg in self.SYSTEM_MESSAGES)

    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone number to digits only."""
        return re.sub(r'\D', '', phone)

    def _extract_phone_from_name(self, name: str) -> Optional[str]:
        """Try to extract phone number from participant name."""
        # WhatsApp sometimes shows phone as name: +55 11 98765-4321
        digits = self._normalize_phone(name)
        if len(digits) >= 10:
            return digits
        return None

    def parse_file(self, content: str, filename: str = "chat.txt") -> Dict[str, Any]:
        """
        Parse WhatsApp export file content.

        Args:
            content: File content as string
            filename: Original filename

        Returns:
            Dict with parsed messages and metadata
        """
        self._import_status = {
            "running": True,
            "filename": filename,
            "total_lines": 0,
            "parsed_messages": 0,
            "linked_contacts": 0,
            "saved_messages": 0,
            "errors": [],
            "participants": []
        }

        lines = content.split('\n')
        self._import_status["total_lines"] = len(lines)

        messages = []
        participants = set()
        current_message = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Try to match with patterns
            matched = False
            for pattern in self.PATTERNS:
                match = pattern.match(line)
                if match:
                    # Save previous message if exists
                    if current_message:
                        messages.append(current_message)

                    date_str, time_str, sender, content = match.groups()

                    # Skip system messages
                    if self._is_system_message(content):
                        matched = True
                        current_message = None
                        break

                    # Parse datetime
                    timestamp = self._parse_date(date_str, time_str)
                    if not timestamp:
                        continue

                    # Extract phone if sender is a phone number
                    phone = self._extract_phone_from_name(sender)

                    current_message = {
                        "sender": sender.strip(),
                        "phone": phone,
                        "content": content.strip(),
                        "timestamp": timestamp,
                        "is_media": self._is_media_message(content)
                    }

                    participants.add(sender.strip())
                    matched = True
                    break

            # If no pattern matched, it's a continuation of previous message
            if not matched and current_message:
                current_message["content"] += "\n" + line

        # Don't forget last message
        if current_message:
            messages.append(current_message)

        self._import_status["parsed_messages"] = len(messages)
        self._import_status["participants"] = list(participants)
        self._import_status["running"] = False

        return {
            "filename": filename,
            "total_messages": len(messages),
            "participants": list(participants),
            "messages": messages,
            "date_range": {
                "start": messages[0]["timestamp"].isoformat() if messages else None,
                "end": messages[-1]["timestamp"].isoformat() if messages else None
            }
        }

    def _is_media_message(self, content: str) -> bool:
        """Check if message is a media message."""
        media_indicators = [
            '<midia oculta>',
            '<media omitted>',
            'imagem anexada',
            'video anexado',
            'audio anexado',
            'documento anexado',
            'figurinha omitida',
            'sticker omitted',
            '.jpg',
            '.mp4',
            '.opus',
            '.pdf',
        ]
        content_lower = content.lower()
        return any(indicator in content_lower for indicator in media_indicators)

    def find_contact_by_name_or_phone(self, sender: str, phone: Optional[str] = None) -> Optional[Dict]:
        """
        Find contact by name or phone number.

        Args:
            sender: Sender name from WhatsApp
            phone: Phone number if available

        Returns:
            Contact dict or None
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Try phone first
            if phone and len(phone) >= 8:
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
                        tel_number = tel.get("number", "") if isinstance(tel, dict) else str(tel)
                        tel_digits = self._normalize_phone(tel_number)

                        if tel_digits and len(tel_digits) >= 8:
                            if tel_digits[-9:] == phone[-9:] or tel_digits[-8:] == phone[-8:]:
                                return dict(contact)

            # Try name match
            cursor.execute("""
                SELECT id, nome FROM contacts
                WHERE LOWER(nome) = LOWER(%s)
                   OR LOWER(nome) LIKE LOWER(%s)
                LIMIT 1
            """, (sender, f"%{sender}%"))

            result = cursor.fetchone()
            if result:
                return dict(result)

            return None

    def import_to_contact(
        self,
        messages: List[Dict],
        contact_id: int,
        my_name: str = "Renato"
    ) -> Dict[str, Any]:
        """
        Import messages to a specific contact.

        Args:
            messages: List of parsed messages
            contact_id: Target contact ID
            my_name: Your name in the chat (to determine direction)

        Returns:
            Import statistics
        """
        result = {
            "contact_id": contact_id,
            "imported": 0,
            "skipped": 0,
            "errors": 0
        }

        with get_db() as conn:
            cursor = conn.cursor()

            # Ensure whatsapp_messages table exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS whatsapp_messages (
                    id SERIAL PRIMARY KEY,
                    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                    phone VARCHAR(50),
                    message_id VARCHAR(100) UNIQUE,
                    direction VARCHAR(20) NOT NULL,
                    content TEXT,
                    message_type VARCHAR(50) DEFAULT 'text',
                    message_date TIMESTAMP,
                    imported_from VARCHAR(255),
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Get contact info
            cursor.execute("SELECT nome FROM contacts WHERE id = %s", (contact_id,))
            contact = cursor.fetchone()
            if not contact:
                return {"error": "Contato nao encontrado"}

            contact_name = contact["nome"]

            # Import messages
            latest_date = None
            for msg in messages:
                try:
                    sender = msg["sender"]
                    content = msg["content"]
                    timestamp = msg["timestamp"]
                    is_media = msg.get("is_media", False)

                    # Determine direction
                    sender_lower = sender.lower()
                    my_name_lower = my_name.lower()

                    if my_name_lower in sender_lower or sender_lower in my_name_lower:
                        direction = "outbound"
                    else:
                        direction = "inbound"

                    # Generate unique message ID
                    msg_id = f"import_{contact_id}_{timestamp.timestamp()}_{hash(content) % 100000}"

                    # Insert message
                    cursor.execute("""
                        INSERT INTO whatsapp_messages
                        (contact_id, direction, content, message_type, message_date, message_id, imported_from)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (message_id) DO NOTHING
                        RETURNING id
                    """, (
                        contact_id,
                        direction,
                        content,
                        "media" if is_media else "text",
                        timestamp,
                        msg_id,
                        "whatsapp_export"
                    ))

                    if cursor.fetchone():
                        result["imported"] += 1
                        if latest_date is None or timestamp > latest_date:
                            latest_date = timestamp
                    else:
                        result["skipped"] += 1

                except Exception as e:
                    logger.error(f"Erro ao importar mensagem: {e}")
                    result["errors"] += 1

            # Update contact interaction
            if latest_date:
                cursor.execute("""
                    UPDATE contacts
                    SET ultimo_contato = GREATEST(COALESCE(ultimo_contato, %s), %s),
                        total_interacoes = COALESCE(total_interacoes, 0) + %s
                    WHERE id = %s
                """, (latest_date, latest_date, result["imported"], contact_id))

            conn.commit()

        return result

    def get_import_status(self) -> Dict[str, Any]:
        """Return current import status."""
        return self._import_status.copy()


# Singleton
_whatsapp_import_service = None


def get_whatsapp_import_service() -> WhatsAppImportService:
    """Get singleton instance."""
    global _whatsapp_import_service
    if _whatsapp_import_service is None:
        _whatsapp_import_service = WhatsAppImportService()
    return _whatsapp_import_service
