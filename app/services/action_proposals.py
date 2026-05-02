"""
Action Proposals Service - Gestao de propostas de acao

CRUD e gestao de propostas de acao geradas pelo RealtimeAnalyzer.
"""
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db

logger = logging.getLogger(__name__)


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

            contact_id = proposal_data.get('contact_id')
            action_type = proposal_data['action_type']
            message_id = proposal_data.get('message_id')

            # SKIP: se usuario ja respondeu APOS a mensagem que disparou a proposta, nao criar.
            # Why: analyzer roda async — pode criar proposta minutos depois da resposta do usuario.
            # How to apply: vale para tipos que pedem resposta ao contato.
            if message_id and contact_id and action_type in (
                'pending_response', 'urgent_alert', 'meeting_request',
                'reschedule_event', 'cancel_event', 'introduction_request',
                'financial_alert', 'opportunity_alert', 'follow_up'
            ):
                cursor.execute("""
                    SELECT 1
                    FROM messages out_msg
                    JOIN conversations cv ON cv.id = out_msg.conversation_id
                    WHERE cv.contact_id = %s
                      AND out_msg.direcao = 'outgoing'
                      AND out_msg.enviado_em > (
                          SELECT enviado_em FROM messages WHERE id = %s
                      )
                    LIMIT 1
                """, (contact_id, message_id))
                if cursor.fetchone():
                    logger.info(
                        f"create_proposal: usuario ja respondeu apos msg {message_id} "
                        f"(contato {contact_id}, tipo {action_type}) — proposta nao criada"
                    )
                    return None

            # DEDUP: se ja existe proposta pendente do mesmo tipo para esse contato nas ultimas 24h,
            # atualizar (com trigger mais recente) em vez de criar nova
            if contact_id:
                cursor.execute("""
                    SELECT id FROM action_proposals
                    WHERE contact_id = %s AND action_type = %s AND status = 'pending'
                      AND criado_em > NOW() - INTERVAL '24 hours'
                    ORDER BY criado_em DESC LIMIT 1
                """, (contact_id, action_type))
                existing = cursor.fetchone()

                if existing:
                    existing_id = existing['id']
                    cursor.execute("""
                        UPDATE action_proposals
                        SET message_id = %s,
                            trigger_text = %s,
                            title = %s,
                            description = %s,
                            urgency = %s,
                            expires_at = %s,
                            criado_em = NOW()
                        WHERE id = %s
                    """, (
                        proposal_data.get('message_id'),
                        proposal_data.get('trigger_text'),
                        proposal_data['title'],
                        proposal_data.get('description'),
                        urgency,
                        expires_at,
                        existing_id,
                    ))
                    conn.commit()
                    return self.get_proposal(existing_id)

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
                contact_id,
                proposal_data.get('message_id'),
                proposal_data.get('conversation_id'),
                action_type,
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

            proposal = self.get_proposal(proposal_id)

            # Audit log
            self._audit("proposal_created", proposal_id, {
                "action_type": action_type,
                "contact_id": contact_id,
                "urgency": urgency,
                "title": proposal_data.get("title"),
            })

            # Send push notification for new proposals
            if proposal:
                self._send_push_notification(proposal)

            return proposal

    def _audit(self, action: str, proposal_id: int, details: Optional[Dict] = None):
        """Registra evento no audit_log. Falhas nao quebram a operacao."""
        try:
            from services.audit_log import log
            log(
                f"action_proposal.{action}",
                entity_type="action_proposal",
                entity_id=proposal_id,
                actor="user" if action in ("accepted", "rejected", "executed", "dismissed") else "system",
                details=details or {},
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"audit failed for proposal {proposal_id}: {e}")

    def _send_push_notification(self, proposal: Dict):
        """Send browser push notification for a new proposal."""
        try:
            from services.push_notifications import get_push_service
            push_service = get_push_service()

            if push_service.is_configured():
                push_service.send_proposal_notification(proposal)
        except Exception as e:
            # Don't fail proposal creation if push fails
            import logging
            logging.getLogger(__name__).warning(f"Failed to send push notification: {e}")

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

    def auto_resolve_weekly_editorial(self) -> int:
        """
        Auto-resolve propostas 'weekly_editorial' cujos posts ja foram todos
        agendados/publicados (status != 'draft' em editorial_posts) ou cujos
        hot_takes referenciados ja sairam de 'draft'.

        Retorna o numero de propostas resolvidas.
        """
        resolved = 0
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, action_params FROM action_proposals
                WHERE action_type = 'weekly_editorial' AND status = 'pending'
            """)
            rows = cursor.fetchall()

            for row in rows:
                params = row.get('action_params') or {}
                if isinstance(params, str):
                    try:
                        params = json.loads(params)
                    except (json.JSONDecodeError, TypeError):
                        params = {}
                selected = params.get('selected', []) if isinstance(params, dict) else []
                if not selected:
                    continue

                still_draft = 0
                for s in selected:
                    pid = s.get('id')
                    src = s.get('source')
                    if not pid:
                        continue
                    if src == 'hot_take':
                        cursor.execute("SELECT status FROM hot_takes WHERE id = %s", (pid,))
                    else:
                        cursor.execute("SELECT status FROM editorial_posts WHERE id = %s", (pid,))
                    r = cursor.fetchone()
                    if r and r.get('status') == 'draft':
                        still_draft += 1

                if still_draft == 0:
                    cursor.execute("""
                        UPDATE action_proposals
                        SET status = 'executed', executed_at = NOW(),
                            execution_result = %s
                        WHERE id = %s AND status = 'pending'
                    """, (json.dumps({"auto_resolved": True, "reason": "all posts scheduled/published"}),
                          row['id']))
                    resolved += 1
            if resolved:
                conn.commit()
        return resolved

    def get_pending_proposals(self, limit: int = 20) -> List[Dict]:
        """Lista propostas pendentes ordenadas por urgencia e data"""
        # Resolve transparente: weekly_editorial cujos posts ja foram agendados
        try:
            self.auto_resolve_weekly_editorial()
        except Exception as e:
            logger.error(f"auto_resolve_weekly_editorial error: {e}")

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
            self._audit("accepted", proposal_id, {"option_id": option_id})
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

            # Record feedback for learning
            self._record_feedback(proposal_id, 'rejected')

            self._audit("rejected", proposal_id, {"reason": reason})
            return self.get_proposal(proposal_id)

    def mark_executed(self, proposal_id: int, result: Dict = None, option_chosen: str = None) -> Optional[Dict]:
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

            # Record feedback for learning
            self._record_feedback(proposal_id, 'accepted', option_chosen)

            self._audit("executed", proposal_id, {"option_chosen": option_chosen, "result": result})
            return self.get_proposal(proposal_id)

    def dismiss_proposal(self, proposal_id: int) -> Optional[Dict]:
        """Ignora uma proposta (mesmo que rejeitar mas sem conotacao negativa)"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE action_proposals
                SET status = 'rejected',
                    responded_at = NOW(),
                    ai_reasoning = COALESCE(ai_reasoning, '') || ' | Ignorado pelo usuario'
                WHERE id = %s AND status = 'pending'
                RETURNING id
            """, (proposal_id,))

            result = cursor.fetchone()
            if not result:
                return None

            conn.commit()

            # Record feedback for learning (dismissed is different from rejected)
            self._record_feedback(proposal_id, 'dismissed')

            self._audit("dismissed", proposal_id, {})
            return self.get_proposal(proposal_id)

    def expire_old_proposals(self) -> int:
        """Marca propostas expiradas"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE action_proposals
                SET status = 'expired'
                WHERE status = 'pending'
                AND (
                    (expires_at IS NOT NULL AND expires_at < NOW())
                    OR criado_em < NOW() - INTERVAL '7 days'
                )
            """)
            count = cursor.rowcount
            conn.commit()
            return count

    def dismiss_stale_on_reply(self, contact_id: int, reply_time: datetime = None) -> int:
        """
        Dismissa propostas pendentes de resposta/follow-up para um contato quando
        o usuario ja respondeu. Chamada quando uma mensagem outbound (de Renato) e salva.

        Args:
            contact_id: ID do contato
            reply_time: Timestamp da mensagem de resposta (default: now)

        Returns:
            Numero de propostas dismissadas
        """
        if reply_time is None:
            reply_time = datetime.now()

        with get_db() as conn:
            cursor = conn.cursor()
            # Dismissa apenas tipos relacionados a aguardar resposta
            # Exclui tipos que requerem acao especifica (reschedule, cancel, etc)
            cursor.execute("""
                UPDATE action_proposals
                SET status = 'resolved',
                    responded_at = NOW(),
                    ai_reasoning = COALESCE(ai_reasoning, '') || ' | Auto-resolvido: usuario respondeu'
                WHERE contact_id = %s
                  AND status = 'pending'
                  AND criado_em < %s
            """, (contact_id, reply_time))
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

    def _record_feedback(self, proposal_id: int, user_action: str, option_chosen: str = None):
        """Record feedback for learning system."""
        try:
            from services.analyzer_feedback import get_feedback_service
            feedback_service = get_feedback_service()
            feedback_service.record_feedback(proposal_id, user_action, option_chosen)
        except Exception as e:
            # Don't fail the main operation if feedback recording fails
            import logging
            logging.getLogger(__name__).warning(f"Failed to record feedback: {e}")

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
