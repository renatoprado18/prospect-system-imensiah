"""
WhatsApp Batch Import Service
Importa múltiplos arquivos .txt do WhatsApp de uma vez.
Usa as tabelas conversations + messages para integrar com o Inbox.
"""
import re
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple
from database import get_db

logger = logging.getLogger(__name__)


class WhatsAppBatchImporter:
    """Importador em lote de conversas WhatsApp"""

    # Regex patterns para diferentes formatos de export
    PATTERNS = [
        # iOS: [DD/MM/YYYY, HH:MM:SS] Nome: Mensagem
        re.compile(r'^\[(\d{2}/\d{2}/\d{4}), (\d{2}:\d{2}:\d{2})\] ([^:]+): (.+)$'),
        # Android: DD/MM/YYYY HH:MM - Nome: Mensagem
        re.compile(r'^(\d{2}/\d{2}/\d{4}) (\d{2}:\d{2}) - ([^:]+): (.+)$'),
        # Android alt: DD/MM/YY, HH:MM - Nome: Mensagem
        re.compile(r'^(\d{2}/\d{2}/\d{2}), (\d{2}:\d{2}) - ([^:]+): (.+)$'),
        # Outro formato: D/M/YY HH:MM - Nome: Mensagem
        re.compile(r'^(\d{1,2}/\d{1,2}/\d{2,4}),? (\d{1,2}:\d{2}(?::\d{2})?)(?:\s?[AP]M)? [-–] ([^:]+): (.+)$'),
    ]

    # Mensagens de sistema para ignorar
    SYSTEM_PATTERNS = [
        'criou este grupo', 'created group', 'adicionou', 'added',
        'removeu', 'removed', 'saiu', 'left', 'mudou o assunto',
        'changed the subject', 'mudou a imagem', 'changed this group',
        'as mensagens e ligacoes', 'messages and calls are end-to-end',
        'criptografia de ponta', 'encryption', 'alterou as configuracoes',
        'entrou usando o link', 'joined using', 'agora e admin',
        'is now an admin', 'deixou de ser admin', 'no longer an admin',
        'apagou esta mensagem', 'deleted this message', 'mensagem apagada',
    ]

    # Indicadores de mídia
    MEDIA_INDICATORS = [
        '<mídia oculta>', '<media omitted>', 'imagem anexada',
        'vídeo anexado', 'áudio anexado', 'documento anexado',
        'figurinha omitida', 'sticker omitted', 'GIF omitido',
        '.jpg (arquivo anexado)', '.mp4 (arquivo anexado)',
        '.opus (arquivo anexado)', '.pdf (arquivo anexado)',
    ]

    def __init__(self, my_names: List[str] = None):
        """
        Args:
            my_names: Lista de nomes que identificam o usuário (para determinar direção)
        """
        self.my_names = my_names or ['Renato', 'Renato de Faria', 'Renato Almeida Prado']

    def parse_datetime(self, date_str: str, time_str: str) -> Optional[datetime]:
        """Parse date/time strings para datetime."""
        formats = [
            ('%d/%m/%Y', '%H:%M:%S'),
            ('%d/%m/%Y', '%H:%M'),
            ('%d/%m/%y', '%H:%M'),
            ('%m/%d/%Y', '%H:%M:%S'),
            ('%m/%d/%Y', '%H:%M'),
            ('%m/%d/%y', '%H:%M'),
            ('%d/%m/%Y', '%H:%M:%S'),
        ]
        for date_fmt, time_fmt in formats:
            try:
                return datetime.strptime(f"{date_str} {time_str}", f"{date_fmt} {time_fmt}")
            except ValueError:
                continue
        return None

    def is_system_message(self, content: str) -> bool:
        """Verifica se é mensagem de sistema."""
        content_lower = content.lower()
        return any(p in content_lower for p in self.SYSTEM_PATTERNS)

    def is_media_message(self, content: str) -> bool:
        """Verifica se é mensagem de mídia."""
        content_lower = content.lower()
        return any(m in content_lower for m in self.MEDIA_INDICATORS)

    def extract_phone(self, text: str) -> Optional[str]:
        """Extrai número de telefone de um texto."""
        digits = re.sub(r'\D', '', text)
        if len(digits) >= 10:
            return digits
        return None

    def is_my_message(self, sender: str) -> bool:
        """Verifica se a mensagem é do usuário (outgoing)."""
        sender_lower = sender.lower().strip()
        for name in self.my_names:
            name_lower = name.lower()
            if name_lower in sender_lower or sender_lower in name_lower:
                return True
        return False

    def parse_file_content(self, content: str) -> Dict[str, Any]:
        """
        Parseia conteúdo de arquivo .txt exportado do WhatsApp.

        Returns:
            Dict com messages, participants, date_range, etc.
        """
        lines = content.split('\n')
        messages = []
        participants = set()
        current_message = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            matched = False
            for pattern in self.PATTERNS:
                match = pattern.match(line)
                if match:
                    if current_message:
                        messages.append(current_message)

                    date_str, time_str, sender, msg_content = match.groups()

                    if self.is_system_message(msg_content):
                        matched = True
                        current_message = None
                        break

                    timestamp = self.parse_datetime(date_str, time_str)
                    if not timestamp:
                        continue

                    sender = sender.strip()
                    phone = self.extract_phone(sender)
                    is_outgoing = self.is_my_message(sender)

                    current_message = {
                        'sender': sender,
                        'phone': phone,
                        'content': msg_content.strip(),
                        'timestamp': timestamp,
                        'direction': 'outgoing' if is_outgoing else 'incoming',
                        'is_media': self.is_media_message(msg_content)
                    }

                    if not is_outgoing:
                        participants.add(sender)

                    matched = True
                    break

            if not matched and current_message:
                current_message['content'] += '\n' + line

        if current_message:
            messages.append(current_message)

        # Determinar o contato principal (quem mais aparece que não sou eu)
        contact_name = None
        if participants:
            contact_name = max(participants, key=lambda p: sum(1 for m in messages if m['sender'] == p))

        return {
            'messages': messages,
            'participants': list(participants),
            'contact_name': contact_name,
            'total_messages': len(messages),
            'date_range': {
                'start': messages[0]['timestamp'] if messages else None,
                'end': messages[-1]['timestamp'] if messages else None
            }
        }

    def normalize_phone(self, phone: str) -> str:
        """Normaliza telefone removendo caracteres e padronizando."""
        if not phone:
            return ""
        # Remove tudo exceto dígitos
        digits = re.sub(r'\D', '', phone)
        # Remove código do país se presente (55 para Brasil)
        if len(digits) > 11 and digits.startswith('55'):
            digits = digits[2:]
        # Remove nono dígito se presente (para comparação)
        if len(digits) == 11 and digits[2] == '9':
            digits_without_9 = digits[:2] + digits[3:]
        else:
            digits_without_9 = digits
        return digits

    def find_contact(self, name: str, phone: Optional[str] = None) -> Optional[Dict]:
        """Busca contato por nome ou telefone com múltiplas estratégias."""
        with get_db() as conn:
            cursor = conn.cursor()

            # 1. Tentar por telefone (múltiplas variações)
            if phone:
                phone_digits = self.normalize_phone(phone)

                # Tentar diferentes partes do telefone
                phone_variants = []
                if len(phone_digits) >= 8:
                    phone_variants.append(phone_digits[-8:])  # Últimos 8 dígitos
                if len(phone_digits) >= 9:
                    phone_variants.append(phone_digits[-9:])  # Últimos 9 dígitos
                if len(phone_digits) >= 10:
                    phone_variants.append(phone_digits[-10:])  # Últimos 10 dígitos
                if len(phone_digits) >= 11:
                    phone_variants.append(phone_digits[-11:])  # Todos os dígitos

                for variant in phone_variants:
                    cursor.execute("""
                        SELECT id, nome, telefones, foto_url
                        FROM contacts
                        WHERE telefones IS NOT NULL
                          AND telefones::text LIKE %s
                        LIMIT 1
                    """, (f'%{variant}%',))
                    result = cursor.fetchone()
                    if result:
                        logger.info(f"Contato encontrado por telefone: {result['nome']}")
                        return dict(result)

            if not name:
                return None

            # 2. Tentar por nome exato
            cursor.execute("""
                SELECT id, nome, telefones, foto_url
                FROM contacts
                WHERE LOWER(TRIM(nome)) = LOWER(TRIM(%s))
                LIMIT 1
            """, (name,))
            result = cursor.fetchone()
            if result:
                logger.info(f"Contato encontrado por nome exato: {result['nome']}")
                return dict(result)

            # 3. Tentar pelo primeiro nome
            first_name = name.split()[0] if name else ""
            if first_name and len(first_name) >= 3:
                cursor.execute("""
                    SELECT id, nome, telefones, foto_url
                    FROM contacts
                    WHERE LOWER(nome) LIKE LOWER(%s)
                    ORDER BY
                        CASE WHEN LOWER(nome) LIKE LOWER(%s) THEN 0 ELSE 1 END,
                        LENGTH(nome) ASC
                    LIMIT 1
                """, (f'{first_name}%', f'{first_name} %'))
                result = cursor.fetchone()
                if result:
                    logger.info(f"Contato encontrado por primeiro nome: {result['nome']}")
                    return dict(result)

            # 4. Tentar nome parcial (contém)
            cursor.execute("""
                SELECT id, nome, telefones, foto_url
                FROM contacts
                WHERE LOWER(nome) LIKE LOWER(%s)
                ORDER BY LENGTH(nome) ASC
                LIMIT 1
            """, (f'%{name}%',))
            result = cursor.fetchone()
            if result:
                logger.info(f"Contato encontrado por nome parcial: {result['nome']}")
                return dict(result)

            # 5. Tentar apelido/nickname
            cursor.execute("""
                SELECT id, nome, telefones, foto_url
                FROM contacts
                WHERE apelido IS NOT NULL
                  AND LOWER(apelido) LIKE LOWER(%s)
                LIMIT 1
            """, (f'%{name}%',))
            result = cursor.fetchone()
            if result:
                logger.info(f"Contato encontrado por apelido: {result['nome']}")
                return dict(result)

            # 6. Busca por palavras individuais do nome (para nomes compostos)
            name_parts = [p for p in name.split() if len(p) >= 3]
            for part in name_parts:
                cursor.execute("""
                    SELECT id, nome, telefones, foto_url
                    FROM contacts
                    WHERE LOWER(nome) LIKE LOWER(%s)
                    ORDER BY LENGTH(nome) ASC
                    LIMIT 1
                """, (f'%{part}%',))
                result = cursor.fetchone()
                if result:
                    logger.info(f"Contato encontrado por parte do nome '{part}': {result['nome']}")
                    return dict(result)

            return None

    def find_contact_for_participants(self, participants: List[str]) -> Optional[Dict]:
        """Tenta encontrar contato testando todos os participantes."""
        for participant in participants:
            phone = self.extract_phone(participant)
            contact = self.find_contact(participant, phone)
            if contact:
                return contact
        return None

    def get_or_create_conversation(self, contact_id: int, cursor) -> int:
        """Obtém ou cria conversa WhatsApp para um contato."""
        # Verificar se já existe
        cursor.execute("""
            SELECT id FROM conversations
            WHERE contact_id = %s AND canal = 'whatsapp'
        """, (contact_id,))
        result = cursor.fetchone()

        if result:
            return result['id']

        # Criar nova conversa
        cursor.execute("""
            INSERT INTO conversations (contact_id, canal, status, criado_em, atualizado_em)
            VALUES (%s, 'whatsapp', 'open', NOW(), NOW())
            RETURNING id
        """, (contact_id,))
        return cursor.fetchone()['id']

    def import_messages_to_inbox(
        self,
        messages: List[Dict],
        contact_id: int,
        conversation_id: int
    ) -> Dict[str, int]:
        """
        Importa mensagens para a tabela messages (usado pelo Inbox).

        Returns:
            Dict com estatísticas: imported, skipped, errors
        """
        stats = {'imported': 0, 'skipped': 0, 'errors': 0}

        with get_db() as conn:
            cursor = conn.cursor()

            latest_date = None

            for msg in messages:
                try:
                    timestamp = msg['timestamp']
                    content = msg['content']
                    direction = msg['direction']

                    # Gerar ID único
                    msg_hash = hash(f"{contact_id}_{timestamp}_{content[:50]}")
                    external_id = f"import_{contact_id}_{int(timestamp.timestamp())}_{abs(msg_hash) % 100000}"

                    # Verificar se já existe
                    cursor.execute("""
                        SELECT id FROM messages WHERE external_id = %s
                    """, (external_id,))

                    if cursor.fetchone():
                        stats['skipped'] += 1
                        continue

                    # Inserir mensagem
                    cursor.execute("""
                        INSERT INTO messages
                        (conversation_id, contact_id, external_id, direcao, conteudo,
                         enviado_em, metadata, criado_em)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    """, (
                        conversation_id,
                        contact_id,
                        external_id,
                        direction,
                        content,
                        timestamp,
                        json.dumps({'imported': True, 'is_media': msg.get('is_media', False)})
                    ))

                    stats['imported'] += 1

                    if latest_date is None or timestamp > latest_date:
                        latest_date = timestamp

                except Exception as e:
                    logger.error(f"Erro ao importar mensagem: {e}")
                    stats['errors'] += 1

            # Atualizar conversa
            if latest_date:
                cursor.execute("""
                    UPDATE conversations
                    SET ultimo_mensagem = GREATEST(COALESCE(ultimo_mensagem, %s), %s),
                        total_mensagens = COALESCE(total_mensagens, 0) + %s,
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (latest_date, latest_date, stats['imported'], conversation_id))

                # Atualizar contato
                cursor.execute("""
                    UPDATE contacts
                    SET ultimo_contato = GREATEST(COALESCE(ultimo_contato, %s), %s),
                        total_interacoes = COALESCE(total_interacoes, 0) + %s
                    WHERE id = %s
                """, (latest_date, latest_date, stats['imported'], contact_id))

            conn.commit()

        return stats

    def process_file(self, content: str, filename: str, contact_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Processa um arquivo .txt completo.

        Args:
            content: Conteúdo do arquivo
            filename: Nome do arquivo
            contact_id: ID do contato (se None, tenta encontrar automaticamente)

        Returns:
            Dict com resultado do processamento
        """
        result = {
            'filename': filename,
            'success': False,
            'contact_id': None,
            'contact_name': None,
            'messages_found': 0,
            'messages_imported': 0,
            'messages_skipped': 0,
            'errors': []
        }

        try:
            # Parsear arquivo
            parsed = self.parse_file_content(content)
            result['messages_found'] = parsed['total_messages']
            result['participants'] = parsed['participants']

            if not parsed['messages']:
                result['errors'].append('Nenhuma mensagem encontrada no arquivo')
                return result

            # Encontrar contato
            contact = None
            if contact_id:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, nome FROM contacts WHERE id = %s", (contact_id,))
                    row = cursor.fetchone()
                    if row:
                        contact = dict(row)
            else:
                # Tentar encontrar automaticamente - múltiplas estratégias
                contact_name = parsed['contact_name']

                # 1. Tentar pelo contato principal
                if contact_name:
                    phone = self.extract_phone(contact_name)
                    contact = self.find_contact(contact_name, phone)

                # 2. Se não encontrou, tentar por todos os participantes
                if not contact and parsed.get('participants'):
                    contact = self.find_contact_for_participants(parsed['participants'])

                # 3. Tentar extrair telefone de qualquer mensagem incoming
                if not contact:
                    for msg in parsed.get('messages', [])[:20]:  # Checar primeiras 20 mensagens
                        if msg.get('direction') == 'incoming' and msg.get('phone'):
                            contact = self.find_contact(msg.get('sender', ''), msg['phone'])
                            if contact:
                                break

            if not contact:
                result['errors'].append(f'Contato não encontrado: {parsed.get("contact_name", "desconhecido")}')
                result['suggested_contact'] = parsed.get('contact_name')
                return result

            result['contact_id'] = contact['id']
            result['contact_name'] = contact['nome']

            # Criar/obter conversa
            with get_db() as conn:
                cursor = conn.cursor()
                conversation_id = self.get_or_create_conversation(contact['id'], cursor)
                conn.commit()

            # Importar mensagens
            import_stats = self.import_messages_to_inbox(
                parsed['messages'],
                contact['id'],
                conversation_id
            )

            result['messages_imported'] = import_stats['imported']
            result['messages_skipped'] = import_stats['skipped']
            result['success'] = True

            if import_stats['errors'] > 0:
                result['errors'].append(f"{import_stats['errors']} mensagens com erro")

        except Exception as e:
            logger.exception(f"Erro ao processar arquivo {filename}")
            result['errors'].append(str(e))

        return result

    def process_batch(self, files: List[Tuple[str, str, Optional[int]]]) -> Dict[str, Any]:
        """
        Processa múltiplos arquivos em lote.

        Args:
            files: Lista de tuplas (filename, content, contact_id)

        Returns:
            Dict com resultado total
        """
        results = {
            'total_files': len(files),
            'successful': 0,
            'failed': 0,
            'total_messages_imported': 0,
            'files': []
        }

        for filename, content, contact_id in files:
            file_result = self.process_file(content, filename, contact_id)
            results['files'].append(file_result)

            if file_result['success']:
                results['successful'] += 1
                results['total_messages_imported'] += file_result['messages_imported']
            else:
                results['failed'] += 1

        return results


# Singleton
_batch_importer = None

def get_batch_importer() -> WhatsAppBatchImporter:
    global _batch_importer
    if _batch_importer is None:
        _batch_importer = WhatsAppBatchImporter()
    return _batch_importer
