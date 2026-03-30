"""
Inbox Service - Unifica emails e WhatsApp

Endpoint: GET /api/inbox/conversations
Endpoint: GET /api/inbox/conversations/{id}/messages
Endpoint: GET /api/inbox/unread
Endpoint: POST /api/inbox/conversations/{id}/read
"""
from typing import List, Dict, Optional
from datetime import datetime
from database import get_db


def serialize_value(obj):
    """Converte qualquer valor para formato JSON serializável"""
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: serialize_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [serialize_value(item) for item in obj]
    if isinstance(obj, (str, int, float, bool)):
        return obj
    # Fallback para outros tipos
    return str(obj)


def serialize_row(row: Dict) -> Dict:
    """Serializa uma row do banco para garantir compatibilidade JSON"""
    return {k: serialize_value(v) for k, v in row.items()}


class InboxService:
    def get_conversations(self, limit: int = 50, filter_type: str = None) -> List[Dict]:
        """Lista conversas ordenadas por data"""
        with get_db() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    c.id,
                    c.contact_id,
                    ct.nome as contact_name,
                    ct.foto_url,
                    c.canal as channel,
                    c.assunto as subject,
                    c.ultimo_mensagem as last_message_at,
                    c.total_mensagens,
                    c.status,
                    c.requer_resposta,
                    c.sentimento,
                    c.atualizado_em as updated_at
                FROM conversations c
                LEFT JOIN contacts ct ON ct.id = c.contact_id
                WHERE 1=1
            """
            params = []

            if filter_type and filter_type != 'all':
                if filter_type == 'unread':
                    query += " AND c.requer_resposta = TRUE"
                elif filter_type == 'email':
                    query += " AND c.canal = 'email'"
                elif filter_type == 'whatsapp':
                    query += " AND c.canal = 'whatsapp'"
                else:
                    query += " AND c.canal = %s"
                    params.append(filter_type)

            query += " ORDER BY c.ultimo_mensagem DESC NULLS LAST LIMIT %s"
            params.append(limit)

            cursor.execute(query, params)
            return [serialize_row(dict(row)) for row in cursor.fetchall()]

    def get_messages(self, conversation_id: int, limit: int = 100) -> List[Dict]:
        """Mensagens de uma conversa"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, conversation_id, direcao as direction, conteudo as content,
                       enviado_em as sent_at, lido_em as read_at, metadata,
                       anexos as attachments, resumo_ai
                FROM messages
                WHERE conversation_id = %s
                ORDER BY enviado_em DESC
                LIMIT %s
            """, (conversation_id, limit))
            return [serialize_row(dict(row)) for row in cursor.fetchall()]

    def get_unread_count(self) -> int:
        """Total de conversas que requerem resposta"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as count FROM conversations
                WHERE requer_resposta = TRUE
            """)
            row = cursor.fetchone()
            return row["count"] if row else 0

    def mark_as_read(self, conversation_id: int) -> bool:
        """Marca conversa como lida"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE conversations
                SET requer_resposta = FALSE, atualizado_em = NOW()
                WHERE id = %s
            """, (conversation_id,))
            cursor.execute("""
                UPDATE messages
                SET lido_em = NOW()
                WHERE conversation_id = %s AND lido_em IS NULL
            """, (conversation_id,))
            conn.commit()
            return True

    def get_conversation_by_id(self, conversation_id: int) -> Optional[Dict]:
        """Retorna detalhes de uma conversa"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    c.id,
                    c.contact_id,
                    ct.nome as contact_name,
                    ct.foto_url,
                    ct.empresa,
                    ct.cargo,
                    c.canal as channel,
                    c.assunto as subject,
                    c.ultimo_mensagem as last_message_at,
                    c.total_mensagens,
                    c.status,
                    c.requer_resposta,
                    c.sentimento,
                    c.resumo_ai,
                    c.resposta_sugerida,
                    c.criado_em as created_at,
                    c.atualizado_em as updated_at
                FROM conversations c
                LEFT JOIN contacts ct ON ct.id = c.contact_id
                WHERE c.id = %s
            """, (conversation_id,))
            row = cursor.fetchone()
            return serialize_row(dict(row)) if row else None


_inbox_service = None


def get_inbox_service() -> InboxService:
    global _inbox_service
    if _inbox_service is None:
        _inbox_service = InboxService()
    return _inbox_service
