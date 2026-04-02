"""
Timeline Service - Historico unificado de interacoes

Endpoint: GET /api/contacts/{contact_id}/timeline

Agrupa mensagens por dia + canal e gera resumos com IA.
"""
import os
import json
import hashlib
import httpx
import logging
from typing import List, Dict, Optional
from datetime import datetime, date
from collections import defaultdict
from database import get_db

logger = logging.getLogger(__name__)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


class TimelineService:
    def get_contact_timeline(self, contact_id: int, limit: int = 50) -> List[Dict]:
        """
        Retorna timeline unificada do contato.
        Mensagens sao agrupadas por dia + canal com resumo IA.
        """
        timeline = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar mensagens agrupadas por dia + canal
            cursor.execute("""
                SELECT
                    DATE(m.enviado_em) as msg_date,
                    c.canal,
                    COUNT(*) as msg_count,
                    MIN(m.enviado_em) as first_msg,
                    MAX(m.enviado_em) as last_msg,
                    array_agg(m.id ORDER BY m.enviado_em) as message_ids
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.contact_id = %s
                GROUP BY DATE(m.enviado_em), c.canal
                ORDER BY msg_date DESC
                LIMIT %s
            """, (contact_id, limit))

            message_groups = cursor.fetchall()

            for group in message_groups:
                group_dict = dict(group)
                msg_date = group_dict['msg_date']
                channel = group_dict['canal']
                msg_count = group_dict['msg_count']
                message_ids = group_dict['message_ids']

                # Buscar resumo do cache ou gerar novo
                summary = self._get_or_create_summary(
                    cursor, contact_id, msg_date, channel, message_ids
                )

                # Buscar preview das mensagens (primeiras 3)
                cursor.execute("""
                    SELECT m.conteudo, m.direcao, m.enviado_em
                    FROM messages m
                    WHERE m.id = ANY(%s)
                    ORDER BY m.enviado_em DESC
                    LIMIT 3
                """, (message_ids,))
                preview_msgs = [dict(r) for r in cursor.fetchall()]

                timeline.append({
                    'type': 'message_group',
                    'channel': channel,
                    'date': msg_date.isoformat() if hasattr(msg_date, 'isoformat') else str(msg_date),
                    'timestamp': group_dict['last_msg'].isoformat() if group_dict['last_msg'] else None,
                    'message_count': msg_count,
                    'summary': summary,
                    'preview': preview_msgs,
                    'message_ids': message_ids
                })

            # Interacoes manuais (nao agrupar)
            cursor.execute("""
                SELECT
                    'interaction' as type,
                    id,
                    COALESCE(titulo, tipo) as title,
                    descricao as content,
                    data_interacao as timestamp,
                    tipo as interaction_type,
                    sentimento as sentiment
                FROM contact_interactions
                WHERE contact_id = %s
                ORDER BY data_interacao DESC
                LIMIT %s
            """, (contact_id, limit))

            for row in cursor.fetchall():
                item = dict(row)
                if item.get('timestamp'):
                    item['timestamp'] = item['timestamp'].isoformat() if hasattr(item['timestamp'], 'isoformat') else str(item['timestamp'])
                timeline.append(item)

            # Memorias/Notas importantes (nao agrupar)
            cursor.execute("""
                SELECT
                    'note' as type,
                    id,
                    COALESCE(titulo, 'Nota') as title,
                    resumo as content,
                    data_ocorrencia as timestamp,
                    tipo as category,
                    importancia as importance,
                    e_marco as is_milestone
                FROM contact_memories
                WHERE contact_id = %s AND (importancia >= 7 OR e_marco = TRUE)
                ORDER BY data_ocorrencia DESC
                LIMIT %s
            """, (contact_id, limit // 2))

            for row in cursor.fetchall():
                item = dict(row)
                if item.get('timestamp'):
                    item['timestamp'] = item['timestamp'].isoformat() if hasattr(item['timestamp'], 'isoformat') else str(item['timestamp'])
                timeline.append(item)

            # Reunioes/Eventos de calendario
            cursor.execute("""
                SELECT
                    'meeting' as type,
                    id,
                    summary as title,
                    description as content,
                    start_datetime as timestamp,
                    end_datetime,
                    location,
                    status,
                    conference_url,
                    CASE WHEN start_datetime < NOW() THEN 'past' ELSE 'future' END as time_status
                FROM calendar_events
                WHERE contact_id = %s
                ORDER BY start_datetime DESC
                LIMIT %s
            """, (contact_id, limit))

            for row in cursor.fetchall():
                item = dict(row)
                if item.get('timestamp'):
                    item['timestamp'] = item['timestamp'].isoformat() if hasattr(item['timestamp'], 'isoformat') else str(item['timestamp'])
                if item.get('end_datetime'):
                    item['end_datetime'] = item['end_datetime'].isoformat() if hasattr(item['end_datetime'], 'isoformat') else str(item['end_datetime'])
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

    def _get_or_create_summary(
        self,
        cursor,
        contact_id: int,
        msg_date: date,
        channel: str,
        message_ids: List[int]
    ) -> str:
        """Busca resumo do cache ou gera novo com IA."""
        # Criar hash unico para este grupo de mensagens
        cache_key = f"{contact_id}_{msg_date}_{channel}_{len(message_ids)}"
        cache_hash = hashlib.md5(cache_key.encode()).hexdigest()[:16]

        # Verificar cache
        cursor.execute("""
            SELECT summary FROM timeline_summaries
            WHERE contact_id = %s AND cache_hash = %s
            AND created_at > NOW() - INTERVAL '7 days'
        """, (contact_id, cache_hash))
        cached = cursor.fetchone()

        if cached:
            return cached['summary']

        # Buscar conteudo das mensagens para gerar resumo
        cursor.execute("""
            SELECT m.conteudo, m.direcao
            FROM messages m
            WHERE m.id = ANY(%s)
            ORDER BY m.enviado_em ASC
        """, (message_ids,))
        messages = cursor.fetchall()

        if not messages:
            return "Conversa"

        # Gerar resumo com IA
        summary = self._generate_summary_sync(messages, channel)

        # Salvar no cache
        try:
            cursor.execute("""
                INSERT INTO timeline_summaries (contact_id, cache_hash, summary, message_count, channel, msg_date)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (contact_id, cache_hash) DO UPDATE SET
                    summary = EXCLUDED.summary,
                    created_at = NOW()
            """, (contact_id, cache_hash, summary, len(message_ids), channel, msg_date))
        except Exception as e:
            logger.warning(f"Erro ao salvar cache de resumo: {e}")

        return summary

    def _generate_summary_sync(self, messages: List[Dict], channel: str) -> str:
        """Gera resumo das mensagens usando Claude (sincrono)."""
        if not ANTHROPIC_API_KEY or not messages:
            return self._generate_fallback_summary(messages, channel)

        # Formatar mensagens para o prompt
        msg_text = "\n".join([
            f"{'[EU]' if m['direcao'] in ['outgoing', 'outbound'] else '[CONTATO]'}: {m['conteudo']}"
            for m in messages[:20]  # Limitar a 20 mensagens
        ])

        prompt = f"""Analise esta conversa de {channel.upper()} e crie um resumo MUITO curto (max 15 palavras).

CONVERSA:
{msg_text}

REGRAS:
- Maximo 15 palavras
- Foque no ASSUNTO principal (nao em saudacoes)
- Use verbos no gerundio quando apropriado
- Exemplos bons: "Agendamento de cafe para discutir parceria", "Negociacao de proposta comercial"
- NAO inclua nomes, datas ou horarios especificos

Responda APENAS com o resumo, sem explicacoes."""

        try:
            import httpx
            with httpx.Client(timeout=15.0) as client:
                response = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": prompt}]
                    }
                )

                if response.status_code == 200:
                    result = response.json()
                    summary = result.get("content", [{}])[0].get("text", "").strip()
                    # Limpar e validar
                    if summary and len(summary) < 200:
                        return summary

        except Exception as e:
            logger.error(f"Erro ao gerar resumo com IA: {e}")

        return self._generate_fallback_summary(messages, channel)

    def _generate_fallback_summary(self, messages: List[Dict], channel: str) -> str:
        """Gera resumo simples sem IA."""
        if not messages:
            return "Conversa"

        # Pegar primeira mensagem significativa (>10 chars)
        for msg in messages:
            content = msg.get('conteudo', '')
            if len(content) > 10:
                # Truncar e limpar
                preview = content[:50].strip()
                if len(content) > 50:
                    preview += "..."
                return preview

        return f"Conversa via {channel}"

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

            return result

    def get_message_group_details(self, contact_id: int, message_ids: List[int]) -> List[Dict]:
        """Retorna detalhes de um grupo de mensagens para expandir."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    m.id,
                    m.conteudo,
                    m.direcao,
                    m.enviado_em,
                    c.canal
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE m.id = ANY(%s) AND c.contact_id = %s
                ORDER BY m.enviado_em ASC
            """, (message_ids, contact_id))

            return [
                {
                    **dict(row),
                    'enviado_em': row['enviado_em'].isoformat() if row['enviado_em'] else None
                }
                for row in cursor.fetchall()
            ]


_timeline_service = None


def get_timeline_service() -> TimelineService:
    global _timeline_service
    if _timeline_service is None:
        _timeline_service = TimelineService()
    return _timeline_service
