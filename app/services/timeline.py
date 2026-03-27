"""
Timeline Service - Historico unificado de interacoes

Endpoint: GET /api/contacts/{contact_id}/timeline
"""
from typing import List, Dict
from datetime import datetime
from database import get_db


class TimelineService:
    def get_contact_timeline(self, contact_id: int, limit: int = 50) -> List[Dict]:
        """
        Retorna timeline unificada do contato.
        Inclui mensagens, memorias/notas e outras interacoes.
        """
        timeline = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Mensagens (email + whatsapp)
            cursor.execute("""
                SELECT
                    'message' as type,
                    m.id,
                    CASE
                        WHEN m.direcao = 'inbound' THEN 'Mensagem recebida'
                        ELSE 'Mensagem enviada'
                    END as title,
                    LEFT(m.conteudo, 200) as content,
                    m.enviado_em as timestamp,
                    c.canal as channel,
                    c.assunto as subject
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.contact_id = %s
                ORDER BY m.enviado_em DESC
                LIMIT %s
            """, (contact_id, limit))

            for row in cursor.fetchall():
                item = dict(row)
                # Convert timestamp to ISO format
                if item.get('timestamp'):
                    item['timestamp'] = item['timestamp'].isoformat() if hasattr(item['timestamp'], 'isoformat') else str(item['timestamp'])
                timeline.append(item)

            # Memorias/Notas
            cursor.execute("""
                SELECT
                    'note' as type,
                    id,
                    COALESCE(titulo, 'Nota') as title,
                    resumo as content,
                    data_ocorrencia as timestamp,
                    tipo as category,
                    subtipo as subcategory,
                    importancia as importance,
                    e_marco as is_milestone
                FROM contact_memories
                WHERE contact_id = %s
                ORDER BY data_ocorrencia DESC
                LIMIT %s
            """, (contact_id, limit))

            for row in cursor.fetchall():
                item = dict(row)
                if item.get('timestamp'):
                    item['timestamp'] = item['timestamp'].isoformat() if hasattr(item['timestamp'], 'isoformat') else str(item['timestamp'])
                timeline.append(item)

            # Fatos extraidos (AI)
            cursor.execute("""
                SELECT
                    'fact' as type,
                    id,
                    'Fato identificado' as title,
                    fato as content,
                    criado_em as timestamp,
                    categoria as category,
                    fonte as source,
                    confianca as confidence,
                    verificado as verified
                FROM contact_facts
                WHERE contact_id = %s
                ORDER BY criado_em DESC
                LIMIT %s
            """, (contact_id, limit // 2))

            for row in cursor.fetchall():
                item = dict(row)
                if item.get('timestamp'):
                    item['timestamp'] = item['timestamp'].isoformat() if hasattr(item['timestamp'], 'isoformat') else str(item['timestamp'])
                timeline.append(item)

        # Ordenar por timestamp (mais recente primeiro)
        def get_timestamp(x):
            ts = x.get('timestamp')
            if not ts:
                return datetime.min
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts.replace('Z', '+00:00'))
                except:
                    return datetime.min
            return ts

        timeline.sort(key=get_timestamp, reverse=True)
        return timeline[:limit]

    def get_contact_summary(self, contact_id: int) -> Dict:
        """Retorna resumo rapido do contato para o timeline"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Dados basicos do contato
            cursor.execute("""
                SELECT
                    id, nome, apelido, empresa, cargo,
                    foto_url, linkedin, circulo, health_score,
                    ultimo_contato, total_interacoes,
                    resumo_ai, tags
                FROM contacts
                WHERE id = %s
            """, (contact_id,))
            contact = cursor.fetchone()

            if not contact:
                return None

            result = dict(contact)

            # Contagem de mensagens
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.contact_id = %s
            """, (contact_id,))
            result['total_messages'] = cursor.fetchone()['total']

            # Contagem de memorias
            cursor.execute("""
                SELECT COUNT(*) as total FROM contact_memories WHERE contact_id = %s
            """, (contact_id,))
            result['total_memories'] = cursor.fetchone()['total']

            # Contagem de fatos
            cursor.execute("""
                SELECT COUNT(*) as total FROM contact_facts WHERE contact_id = %s
            """, (contact_id,))
            result['total_facts'] = cursor.fetchone()['total']

            return result


_timeline_service = None


def get_timeline_service() -> TimelineService:
    global _timeline_service
    if _timeline_service is None:
        _timeline_service = TimelineService()
    return _timeline_service
