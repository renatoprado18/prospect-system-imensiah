"""
Action Proposals Service - Gestao de propostas de acao

CRUD e gestao de propostas de acao geradas pelo RealtimeAnalyzer.
"""
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db


class ActionProposalsService:
    """Gerencia propostas de acao para o usuario."""

    def create_proposal(self, proposal_data: Dict) -> Optional[Dict]:
        """
        Cria uma nova proposta de acao.

        Args:
            proposal_data: {
                'action_type': str,
                'contact_id': int,
                'message_id': int (optional),
                'conversation_id': int (optional),
                'title': str,
                'description': str,
                'trigger_text': str,
                'ai_reasoning': str,
                'confidence': float,
                'urgency': str,
                'action_params': dict,
                'options': list
            }

        Returns:
            Proposta criada com ID
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Calcular expiracao (24h para urgentes, 72h para outros)
            urgency = proposal_data.get('urgency', 'medium')
            if urgency == 'high':
                expires_at = datetime.now() + timedelta(hours=24)
            else:
                expires_at = datetime.now() + timedelta(hours=72)

            cursor.execute("""
                INSERT INTO action_proposals (
                    contact_id, message_id, conversation_id,
                    action_type, action_params,
                    trigger_text, ai_reasoning, confidence, urgency,
                    status, title, description, options, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                proposal_data.get('contact_id'),
                proposal_data.get('message_id'),
                proposal_data.get('conversation_id'),
                proposal_data['action_type'],
                json.dumps(proposal_data.get('action_params', {})),
                proposal_data.get('trigger_text'),
                proposal_data.get('ai_reasoning'),
                proposal_data.get('confidence', 0.5),
                urgency,
                'pending',
                proposal_data['title'],
                proposal_data.get('description'),
                json.dumps(proposal_data.get('options', [])),
                expires_at
            ))

            proposal_id = cursor.fetchone()['id']
            conn.commit()

            return self.get_proposal(proposal_id)

    def create_from_analysis(
        self,
        message_id: int,
        contact_id: int,
        analysis: Dict
    ) -> List[Dict]:
        """
        Cria propostas a partir do resultado da analise do RealtimeAnalyzer.

        Args:
            message_id: ID da mensagem que disparou a analise
            contact_id: ID do contato
            analysis: Resultado de RealtimeAnalyzer.analyze_message()

        Returns:
            Lista de propostas criadas
        """
        created = []

        for action in analysis.get('suggested_actions', []):
            action['message_id'] = message_id
            action['contact_id'] = contact_id

            proposal = self.create_proposal(action)
            if proposal:
                created.append(proposal)

        return created

    def get_proposal(self, proposal_id: int) -> Optional[Dict]:
        """Busca proposta por ID"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ap.*, c.nome as contact_name, c.foto_url as contact_foto
                FROM action_proposals ap
                LEFT JOIN contacts c ON c.id = ap.contact_id
                WHERE ap.id = %s
            """, (proposal_id,))

            row = cursor.fetchone()
            if row:
                return self._serialize_proposal(dict(row))
            return None

    def get_pending_proposals(self, limit: int = 20) -> List[Dict]:
        """Lista propostas pendentes ordenadas por urgencia e data"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ap.*, c.nome as contact_name, c.foto_url as contact_foto
                FROM action_proposals ap
                LEFT JOIN contacts c ON c.id = ap.contact_id
                WHERE ap.status = 'pending'
                AND (ap.expires_at IS NULL OR ap.expires_at > NOW())
                ORDER BY
                    CASE ap.urgency
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                    END,
                    ap.criado_em DESC
                LIMIT %s
            """, (limit,))

            proposals = []
            for row in cursor.fetchall():
                proposals.append(self._serialize_proposal(dict(row)))
            return proposals

    def get_proposals_for_contact(self, contact_id: int, include_resolved: bool = False) -> List[Dict]:
        """Lista propostas de um contato"""
        with get_db() as conn:
            cursor = conn.cursor()

            query = """
                SELECT ap.*, c.nome as contact_name, c.foto_url as contact_foto
                FROM action_proposals ap
                LEFT JOIN contacts c ON c.id = ap.contact_id
                WHERE ap.contact_id = %s
            """
            if not include_resolved:
                query += " AND ap.status = 'pending'"

            query += " ORDER BY ap.criado_em DESC LIMIT 50"

            cursor.execute(query, (contact_id,))

            proposals = []
            for row in cursor.fetchall():
                proposals.append(self._serialize_proposal(dict(row)))
            return proposals

    def accept_proposal(self, proposal_id: int, option_id: str = None) -> Optional[Dict]:
        """
        Aceita uma proposta de acao.

        Args:
            proposal_id: ID da proposta
            option_id: ID da opcao escolhida (se houver)

        Returns:
            Proposta atualizada
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE action_proposals
                SET status = 'accepted', responded_at = NOW()
                WHERE id = %s AND status = 'pending'
                RETURNING id
            """, (proposal_id,))

            result = cursor.fetchone()
            if not result:
                return None

            conn.commit()
            return self.get_proposal(proposal_id)

    def reject_proposal(self, proposal_id: int, reason: str = None) -> Optional[Dict]:
        """Rejeita uma proposta de acao"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Adicionar razao ao ai_reasoning se fornecida
            if reason:
                cursor.execute("""
                    UPDATE action_proposals
                    SET status = 'rejected',
                        responded_at = NOW(),
                        ai_reasoning = COALESCE(ai_reasoning, '') || ' | Rejeitado: ' || %s
                    WHERE id = %s AND status = 'pending'
                    RETURNING id
                """, (reason, proposal_id))
            else:
                cursor.execute("""
                    UPDATE action_proposals
                    SET status = 'rejected', responded_at = NOW()
                    WHERE id = %s AND status = 'pending'
                    RETURNING id
                """, (proposal_id,))

            result = cursor.fetchone()
            if not result:
                return None

            conn.commit()
            return self.get_proposal(proposal_id)

    def mark_executed(self, proposal_id: int, result: Dict = None) -> Optional[Dict]:
        """Marca proposta como executada"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE action_proposals
                SET status = 'executed',
                    executed_at = NOW(),
                    execution_result = %s
                WHERE id = %s AND status IN ('pending', 'accepted')
                RETURNING id
            """, (json.dumps(result or {}), proposal_id))

            row = cursor.fetchone()
            if not row:
                return None

            conn.commit()
            return self.get_proposal(proposal_id)

    def dismiss_proposal(self, proposal_id: int) -> Optional[Dict]:
        """Ignora uma proposta (mesmo que rejeitar mas sem conotacao negativa)"""
        return self.reject_proposal(proposal_id, reason="Ignorado pelo usuario")

    def expire_old_proposals(self) -> int:
        """Marca propostas expiradas"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE action_proposals
                SET status = 'expired'
                WHERE status = 'pending'
                AND expires_at IS NOT NULL
                AND expires_at < NOW()
            """)
            count = cursor.rowcount
            conn.commit()
            return count

    def get_pending_count(self) -> Dict:
        """Retorna contagem de propostas pendentes por urgencia"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE urgency = 'high') as high,
                    COUNT(*) FILTER (WHERE urgency = 'medium') as medium,
                    COUNT(*) FILTER (WHERE urgency = 'low') as low
                FROM action_proposals
                WHERE status = 'pending'
                AND (expires_at IS NULL OR expires_at > NOW())
            """)
            row = cursor.fetchone()
            return dict(row) if row else {'total': 0, 'high': 0, 'medium': 0, 'low': 0}

    def get_stats(self, days: int = 30) -> Dict:
        """Estatisticas de propostas"""
        with get_db() as conn:
            cursor = conn.cursor()
            start_date = datetime.now() - timedelta(days=days)

            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE status = 'accepted') as accepted,
                    COUNT(*) FILTER (WHERE status = 'rejected') as rejected,
                    COUNT(*) FILTER (WHERE status = 'executed') as executed,
                    COUNT(*) FILTER (WHERE status = 'expired') as expired,
                    AVG(confidence) as avg_confidence
                FROM action_proposals
                WHERE criado_em >= %s
            """, (start_date,))

            row = cursor.fetchone()
            return dict(row) if row else {}

    def _serialize_proposal(self, row: Dict) -> Dict:
        """Serializa proposta para JSON"""
        # Converter datas
        for key in ['criado_em', 'expires_at', 'responded_at', 'executed_at']:
            if row.get(key) and hasattr(row[key], 'isoformat'):
                row[key] = row[key].isoformat()

        # Parse JSON fields se necessario
        for key in ['action_params', 'options', 'execution_result']:
            if isinstance(row.get(key), str):
                try:
                    row[key] = json.loads(row[key])
                except (json.JSONDecodeError, TypeError):
                    pass

        return row


_action_proposals = None


def get_action_proposals() -> ActionProposalsService:
    global _action_proposals
    if _action_proposals is None:
        _action_proposals = ActionProposalsService()
    return _action_proposals
