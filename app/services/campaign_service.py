"""
Campaign Service - Gerenciamento de Campanhas de Relacionamento

Sistema de campanhas multi-step para converter contatos em resultados,
segmentado por 4 linhas de negócio:
- Conselhos: Posições em boards, mentoria → Reunião/Café
- ImensIAH: Diagnósticos estratégicos → Demo/Proposta
- FusIAH: Deals de M&A, investidores → Tese/Reunião
- Jabô Café: Vendas B2B, parcerias → Pedido/Degustação
"""

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import json
import logging

logger = logging.getLogger(__name__)


def get_db():
    """Get database connection using existing pattern."""
    from database import get_connection
    return get_connection()


class CampaignService:
    """Serviço principal para gestão de campanhas de relacionamento."""

    # =========================================================================
    # BUSINESS LINES
    # =========================================================================

    def get_business_lines(self, only_active: bool = True) -> List[Dict]:
        """Lista todas as linhas de negócio."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            query = "SELECT * FROM business_lines"
            if only_active:
                query += " WHERE ativo = TRUE"
            query += " ORDER BY id"

            cursor.execute(query)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_business_line_by_slug(self, slug: str) -> Optional[Dict]:
        """Busca linha de negócio por slug."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT * FROM business_lines WHERE slug = %s",
                (slug,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # =========================================================================
    # CAMPAIGN CRUD
    # =========================================================================

    def create_campaign(
        self,
        business_line_id: int,
        nome: str,
        objetivo: str,
        filtros: Dict,
        steps: List[Dict],
        descricao: Optional[str] = None,
        motivo_contato: Optional[str] = None
    ) -> int:
        """
        Cria uma nova campanha com seus steps.

        Args:
            business_line_id: ID da linha de negócio
            nome: Nome da campanha
            objetivo: Objetivo da campanha
            filtros: Filtros de segmentação (JSONB)
            steps: Lista de steps da campanha
            descricao: Descrição opcional
            motivo_contato: Motivo para contato (contexto)

        Returns:
            ID da campanha criada
        """
        conn = get_db()
        cursor = conn.cursor()
        try:
            # Criar campanha
            cursor.execute("""
                INSERT INTO campaigns (
                    business_line_id, nome, descricao, objetivo,
                    motivo_contato, filtros, status
                ) VALUES (%s, %s, %s, %s, %s, %s, 'draft')
                RETURNING id
            """, (business_line_id, nome, descricao, objetivo,
                  motivo_contato, json.dumps(filtros)))

            campaign_id = cursor.fetchone()['id']

            # Criar steps
            for step in steps:
                cursor.execute("""
                    INSERT INTO campaign_steps (
                        campaign_id, ordem, tipo, titulo, descricao,
                        delay_dias, config, condicao
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    campaign_id,
                    step.get('ordem'),
                    step.get('tipo'),
                    step.get('titulo'),
                    step.get('descricao'),
                    step.get('delay_dias', 0),
                    json.dumps(step.get('config', {})),
                    json.dumps(step.get('condicao')) if step.get('condicao') else None
                ))

            conn.commit()
            logger.info(f"Campanha criada: {campaign_id} - {nome}")
            return campaign_id
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def get_campaign(self, campaign_id: int) -> Optional[Dict]:
        """Busca campanha por ID com steps e métricas."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            # Campanha base
            cursor.execute("""
                SELECT c.*, bl.slug as business_line_slug, bl.nome as business_line_nome,
                       bl.conversion_metric
                FROM campaigns c
                JOIN business_lines bl ON c.business_line_id = bl.id
                WHERE c.id = %s
            """, (campaign_id,))

            campaign = cursor.fetchone()
            if not campaign:
                return None

            result = dict(campaign)

            # Steps
            cursor.execute("""
                SELECT * FROM campaign_steps
                WHERE campaign_id = %s AND ativo = TRUE
                ORDER BY ordem
            """, (campaign_id,))
            result['steps'] = [dict(s) for s in cursor.fetchall()]

            # Métricas atualizadas
            cursor.execute("""
                SELECT
                    COUNT(*) as total_enrolled,
                    COUNT(*) FILTER (WHERE status = 'active') as active,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed,
                    COUNT(*) FILTER (WHERE status = 'converted') as converted,
                    COUNT(*) FILTER (WHERE status = 'paused') as paused,
                    COUNT(*) FILTER (WHERE status = 'removed') as removed
                FROM campaign_enrollments
                WHERE campaign_id = %s
            """, (campaign_id,))
            result['metrics'] = dict(cursor.fetchone())

            return result
        finally:
            conn.close()

    def list_campaigns(
        self,
        business_line_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict]:
        """Lista campanhas com filtros opcionais."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            conditions = []
            params = []

            if business_line_id:
                conditions.append("c.business_line_id = %s")
                params.append(business_line_id)

            if status:
                conditions.append("c.status = %s")
                params.append(status)

            where = "WHERE " + " AND ".join(conditions) if conditions else ""

            params.extend([limit, offset])

            query = f"""
                SELECT c.*, bl.slug as business_line_slug, bl.nome as business_line_nome
                FROM campaigns c
                JOIN business_lines bl ON c.business_line_id = bl.id
                {where}
                ORDER BY c.criado_em DESC
                LIMIT %s OFFSET %s
            """

            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    def update_campaign(
        self,
        campaign_id: int,
        **kwargs
    ) -> bool:
        """
        Atualiza campos da campanha.
        Só permite update se status = 'draft'.
        """
        allowed_fields = ['nome', 'descricao', 'objetivo', 'motivo_contato', 'filtros']

        conn = get_db()
        cursor = conn.cursor()
        try:
            # Verificar status
            cursor.execute(
                "SELECT status FROM campaigns WHERE id = %s",
                (campaign_id,)
            )
            row = cursor.fetchone()
            if not row or row['status'] != 'draft':
                logger.warning(f"Tentativa de editar campanha {campaign_id} com status incorreto")
                return False

            updates = []
            params = []

            for field, value in kwargs.items():
                if field in allowed_fields:
                    if field == 'filtros':
                        value = json.dumps(value)
                    updates.append(f"{field} = %s")
                    params.append(value)

            if not updates:
                return False

            updates.append("atualizado_em = %s")
            params.append(datetime.now())
            params.append(campaign_id)

            query = f"""
                UPDATE campaigns
                SET {', '.join(updates)}
                WHERE id = %s
            """

            cursor.execute(query, params)
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def delete_campaign(self, campaign_id: int) -> bool:
        """Deleta campanha (apenas se draft)."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                DELETE FROM campaigns
                WHERE id = %s AND status = 'draft'
            """, (campaign_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    # =========================================================================
    # CAMPAIGN LIFECYCLE
    # =========================================================================

    def activate_campaign(self, campaign_id: int) -> Dict:
        """
        Ativa uma campanha e enrolla contatos baseado nos filtros.

        Returns:
            Dict com total de contatos enrolled
        """
        conn = get_db()
        cursor = conn.cursor()
        try:
            # Verificar se é draft
            cursor.execute(
                "SELECT * FROM campaigns WHERE id = %s",
                (campaign_id,)
            )
            campaign = cursor.fetchone()

            if not campaign:
                return {"success": False, "error": "Campanha não encontrada"}

            if campaign['status'] != 'draft':
                return {"success": False, "error": f"Campanha com status {campaign['status']}, esperado 'draft'"}

            # Buscar contatos pelos filtros
            filtros = campaign['filtros'] if isinstance(campaign['filtros'], dict) else json.loads(campaign['filtros'])
            contact_ids = self._apply_filters(cursor, filtros)

            if not contact_ids:
                return {"success": False, "error": "Nenhum contato encontrado com os filtros especificados"}

            # Buscar primeiro step para calcular next_action_at
            cursor.execute("""
                SELECT delay_dias FROM campaign_steps
                WHERE campaign_id = %s AND ordem = 1
            """, (campaign_id,))
            first_step = cursor.fetchone()

            delay = first_step['delay_dias'] if first_step else 0
            next_action = datetime.now() + timedelta(days=delay)

            # Enrollar contatos
            enrolled_count = 0
            for contact_id in contact_ids:
                try:
                    cursor.execute("""
                        INSERT INTO campaign_enrollments (
                            campaign_id, contact_id, status,
                            current_step, next_action_at
                        ) VALUES (%s, %s, 'active', 1, %s)
                        ON CONFLICT (campaign_id, contact_id) DO NOTHING
                    """, (campaign_id, contact_id, next_action))
                    if cursor.rowcount > 0:
                        enrolled_count += 1
                except Exception as e:
                    logger.warning(f"Erro ao enrollar contato {contact_id}: {e}")

            # Atualizar status da campanha
            cursor.execute("""
                UPDATE campaigns
                SET status = 'active',
                    iniciado_em = NOW(),
                    total_enrolled = %s,
                    atualizado_em = NOW()
                WHERE id = %s
            """, (enrolled_count, campaign_id))

            conn.commit()
            logger.info(f"Campanha {campaign_id} ativada com {enrolled_count} contatos")

            return {
                "success": True,
                "enrolled_count": enrolled_count,
                "first_action_at": next_action.isoformat()
            }
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def pause_campaign(self, campaign_id: int) -> bool:
        """Pausa uma campanha ativa."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE campaigns
                SET status = 'paused', atualizado_em = NOW()
                WHERE id = %s AND status = 'active'
            """, (campaign_id,))
            success = cursor.rowcount > 0
            conn.commit()
            return success
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def resume_campaign(self, campaign_id: int) -> bool:
        """Retoma uma campanha pausada."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE campaigns
                SET status = 'active', atualizado_em = NOW()
                WHERE id = %s AND status = 'paused'
            """, (campaign_id,))
            success = cursor.rowcount > 0
            conn.commit()
            return success
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def complete_campaign(self, campaign_id: int) -> Dict:
        """
        Finaliza uma campanha e retorna métricas finais.
        """
        conn = get_db()
        cursor = conn.cursor()
        try:
            # Buscar métricas
            cursor.execute("""
                SELECT
                    COUNT(*) as total_enrolled,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed,
                    COUNT(*) FILTER (WHERE status = 'converted') as converted,
                    COUNT(*) FILTER (WHERE status = 'active') as still_active
                FROM campaign_enrollments
                WHERE campaign_id = %s
            """, (campaign_id,))
            metrics = cursor.fetchone()

            # Atualizar campanha
            cursor.execute("""
                UPDATE campaigns
                SET status = 'completed',
                    finalizado_em = NOW(),
                    total_completed = %s,
                    total_converted = %s,
                    atualizado_em = NOW()
                WHERE id = %s
            """, (metrics['completed'], metrics['converted'], campaign_id))

            # Marcar enrollments ativos como completed
            cursor.execute("""
                UPDATE campaign_enrollments
                SET status = 'completed', completed_at = NOW()
                WHERE campaign_id = %s AND status = 'active'
            """, (campaign_id,))

            conn.commit()

            total = metrics['total_enrolled'] or 1
            return {
                "total_enrolled": metrics['total_enrolled'],
                "completed": metrics['completed'],
                "converted": metrics['converted'],
                "conversion_rate": round(metrics['converted'] / total * 100, 1) if total > 0 else 0
            }
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    # =========================================================================
    # ENROLLMENT MANAGEMENT
    # =========================================================================

    def enroll_contact(
        self,
        campaign_id: int,
        contact_id: int
    ) -> bool:
        """Enrolla um contato específico em uma campanha."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            # Buscar primeiro step
            cursor.execute("""
                SELECT delay_dias FROM campaign_steps
                WHERE campaign_id = %s AND ordem = 1
            """, (campaign_id,))
            first_step = cursor.fetchone()

            delay = first_step['delay_dias'] if first_step else 0
            next_action = datetime.now() + timedelta(days=delay)

            cursor.execute("""
                INSERT INTO campaign_enrollments (
                    campaign_id, contact_id, status,
                    current_step, next_action_at
                ) VALUES (%s, %s, 'active', 1, %s)
            """, (campaign_id, contact_id, next_action))

            # Atualizar contador
            cursor.execute("""
                UPDATE campaigns
                SET total_enrolled = total_enrolled + 1
                WHERE id = %s
            """, (campaign_id,))

            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            logger.warning(f"Erro ao enrollar contato {contact_id} na campanha {campaign_id}: {e}")
            return False
        finally:
            conn.close()

    def remove_from_campaign(
        self,
        campaign_id: int,
        contact_id: int,
        motivo: Optional[str] = None
    ) -> bool:
        """Remove um contato de uma campanha."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE campaign_enrollments
                SET status = 'removed',
                    conversion_notes = COALESCE(conversion_notes || ' | ', '') || %s
                WHERE campaign_id = %s AND contact_id = %s AND status IN ('active', 'paused')
            """, (motivo or 'Removido manualmente', campaign_id, contact_id))
            success = cursor.rowcount > 0
            conn.commit()
            return success
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def pause_enrollment(self, enrollment_id: int) -> bool:
        """Pausa um enrollment específico."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE campaign_enrollments
                SET status = 'paused', paused_at = NOW()
                WHERE id = %s AND status = 'active'
            """, (enrollment_id,))
            success = cursor.rowcount > 0
            conn.commit()
            return success
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def resume_enrollment(self, enrollment_id: int) -> bool:
        """Retoma um enrollment pausado."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE campaign_enrollments
                SET status = 'active', paused_at = NULL
                WHERE id = %s AND status = 'paused'
            """, (enrollment_id,))
            success = cursor.rowcount > 0
            conn.commit()
            return success
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def mark_converted(
        self,
        enrollment_id: int,
        notes: str
    ) -> bool:
        """Marca um enrollment como convertido."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE campaign_enrollments
                SET status = 'converted',
                    converted_at = NOW(),
                    conversion_notes = %s
                WHERE id = %s AND status IN ('active', 'paused', 'completed')
            """, (notes, enrollment_id))

            if cursor.rowcount > 0:
                # Atualizar contador da campanha
                cursor.execute("""
                    UPDATE campaigns c
                    SET total_converted = total_converted + 1
                    FROM campaign_enrollments e
                    WHERE e.id = %s AND c.id = e.campaign_id
                """, (enrollment_id,))
                conn.commit()
                return True

            conn.commit()
            return False
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def get_enrollments(
        self,
        campaign_id: int,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict]:
        """Lista enrollments de uma campanha."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            conditions = ["e.campaign_id = %s"]
            params = [campaign_id]

            if status:
                conditions.append("e.status = %s")
                params.append(status)

            params.extend([limit, offset])

            query = f"""
                SELECT e.*, c.nome as contact_nome, c.empresa as contact_empresa
                FROM campaign_enrollments e
                JOIN contacts c ON e.contact_id = c.id
                WHERE {' AND '.join(conditions)}
                ORDER BY e.enrolled_at DESC
                LIMIT %s OFFSET %s
            """

            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    def get_contact_campaigns(self, contact_id: int) -> List[Dict]:
        """Lista todas as campanhas de um contato."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT e.*, c.nome as campaign_nome, c.status as campaign_status,
                       bl.slug as business_line
                FROM campaign_enrollments e
                JOIN campaigns c ON e.campaign_id = c.id
                JOIN business_lines bl ON c.business_line_id = bl.id
                WHERE e.contact_id = %s
                ORDER BY e.enrolled_at DESC
            """, (contact_id,))
            return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    # =========================================================================
    # QUERIES & ANALYTICS
    # =========================================================================

    def get_pending_actions(self, limit: int = 50) -> List[Dict]:
        """
        Retorna ações pendentes para hoje (next_action_at <= NOW).
        Usado pelo dashboard e pelo executor.
        """
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT
                    e.id as enrollment_id,
                    e.contact_id,
                    e.current_step,
                    e.next_action_at,
                    c.id as campaign_id,
                    c.nome as campaign_nome,
                    c.motivo_contato,
                    ct.nome as contact_nome,
                    ct.empresa as contact_empresa,
                    s.tipo as step_tipo,
                    s.titulo as step_titulo,
                    s.config as step_config,
                    bl.slug as business_line
                FROM campaign_enrollments e
                JOIN campaigns c ON e.campaign_id = c.id
                JOIN contacts ct ON e.contact_id = ct.id
                JOIN campaign_steps s ON s.campaign_id = c.id AND s.ordem = e.current_step
                JOIN business_lines bl ON c.business_line_id = bl.id
                WHERE e.status = 'active'
                  AND c.status = 'active'
                  AND e.next_action_at <= NOW()
                ORDER BY e.next_action_at ASC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    def preview_campaign_audience(self, filtros: Dict) -> Dict:
        """
        Preview dos contatos que seriam enrollados com os filtros.
        Útil antes de ativar a campanha.
        """
        conn = get_db()
        cursor = conn.cursor()
        try:
            contact_ids = self._apply_filters(cursor, filtros)

            if not contact_ids:
                return {"count": 0, "contacts": []}

            # Buscar detalhes dos primeiros 20
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, health_score
                FROM contacts
                WHERE id = ANY(%s)
                LIMIT 20
            """, (contact_ids,))

            return {
                "count": len(contact_ids),
                "sample": [dict(r) for r in cursor.fetchall()]
            }
        finally:
            conn.close()

    def get_campaign_funnel(self, campaign_id: int) -> List[Dict]:
        """Retorna métricas de funil por step."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT s.ordem, s.titulo, s.tipo,
                       COUNT(DISTINCT e.id) FILTER (WHERE e.current_step >= s.ordem) as reached,
                       COUNT(DISTINCT x.id) as executed
                FROM campaign_steps s
                LEFT JOIN campaign_enrollments e ON e.campaign_id = s.campaign_id
                LEFT JOIN campaign_step_executions x ON x.step_id = s.id
                WHERE s.campaign_id = %s
                GROUP BY s.id, s.ordem, s.titulo, s.tipo
                ORDER BY s.ordem
            """, (campaign_id,))
            return [dict(s) for s in cursor.fetchall()]
        finally:
            conn.close()

    def get_dashboard_stats(self) -> Dict:
        """Estatísticas gerais para dashboard."""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'active') as active_campaigns,
                    COUNT(*) FILTER (WHERE status = 'draft') as draft_campaigns,
                    (SELECT COUNT(*) FROM campaign_enrollments WHERE status = 'active') as active_enrollments,
                    (SELECT COUNT(*) FROM campaign_enrollments WHERE status = 'converted') as total_conversions
                FROM campaigns
            """)
            stats = dict(cursor.fetchone())

            # Ações pendentes hoje
            cursor.execute("""
                SELECT COUNT(*) as count FROM campaign_enrollments
                WHERE status = 'active' AND next_action_at <= NOW()
            """)
            pending = cursor.fetchone()['count']

            # Por linha de negócio
            cursor.execute("""
                SELECT bl.slug, bl.nome,
                       COUNT(c.id) FILTER (WHERE c.status = 'active') as active,
                       COUNT(e.id) FILTER (WHERE e.status = 'converted') as converted
                FROM business_lines bl
                LEFT JOIN campaigns c ON c.business_line_id = bl.id
                LEFT JOIN campaign_enrollments e ON e.campaign_id = c.id
                GROUP BY bl.id, bl.slug, bl.nome
            """)

            return {
                **stats,
                "pending_actions_today": pending,
                "by_business_line": [dict(r) for r in cursor.fetchall()]
            }
        finally:
            conn.close()

    # =========================================================================
    # PRIVATE HELPERS
    # =========================================================================

    def _apply_filters(self, cursor, filtros: Dict) -> List[int]:
        """
        Aplica filtros e retorna lista de contact_ids.

        Filtros suportados:
        - circulo: [1, 2, 3] - lista de círculos
        - tags: ["governança", "tech"] - qualquer tag
        - health_min: 0 - health_score mínimo
        - health_max: 100 - health_score máximo
        - ultimo_contato_dias_min: 30 - dias desde último contato
        - empresa_contem: "tech" - nome da empresa contém
        - cargo_contem: "CEO" - cargo contém
        """
        conditions = ["1=1"]
        params = []

        if 'circulo' in filtros:
            conditions.append("circulo = ANY(%s)")
            params.append(filtros['circulo'])

        if 'tags' in filtros:
            conditions.append("tags && %s")
            params.append(filtros['tags'])

        if 'health_min' in filtros:
            conditions.append("health_score >= %s")
            params.append(filtros['health_min'])

        if 'health_max' in filtros:
            conditions.append("health_score <= %s")
            params.append(filtros['health_max'])

        if 'ultimo_contato_dias_min' in filtros:
            conditions.append(f"ultimo_contato < NOW() - INTERVAL '{filtros['ultimo_contato_dias_min']} days'")

        if 'empresa_contem' in filtros:
            conditions.append("empresa ILIKE %s")
            params.append(f"%{filtros['empresa_contem']}%")

        if 'cargo_contem' in filtros:
            conditions.append("cargo ILIKE %s")
            params.append(f"%{filtros['cargo_contem']}%")

        query = f"""
            SELECT id FROM contacts
            WHERE {' AND '.join(conditions)}
        """

        cursor.execute(query, params if params else None)
        return [r['id'] for r in cursor.fetchall()]
