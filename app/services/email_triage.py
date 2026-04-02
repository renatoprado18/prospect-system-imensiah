"""
Email Triage Service - Triagem automatica de emails

Classifica emails que precisam de atenção baseado em:
- Remetente importante (C1/C2/C3)
- Palavras-chave (urgente, proposta, reunião, pagamento)
- Emails não respondidos há X dias

Oferece ações: responder, agendar reunião, criar tarefa, criar projeto
"""
import os
import json
import re
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from database import get_db

# Keywords para detecção
URGENT_KEYWORDS = ['urgente', 'urgent', 'asap', 'imediato', 'hoje', 'agora', 'crítico', 'critical']
MEETING_KEYWORDS = ['reunião', 'meeting', 'call', 'agenda', 'agendar', 'marcar', 'conversa', 'encontro']
MONEY_KEYWORDS = ['proposta', 'orçamento', 'pagamento', 'invoice', 'fatura', 'preço', 'valor', 'budget', 'contrato']
QUESTION_KEYWORDS = ['?', 'você pode', 'poderia', 'consegue', 'como', 'quando', 'onde', 'qual']
FOLLOWUP_KEYWORDS = ['aguardo', 'retorno', 'resposta', 'pending', 'waiting', 'follow up', 'follow-up']

# Regras default
DEFAULT_RULES = [
    {
        "nome": "Remetente C1 - Urgente",
        "conditions": {"sender_circles": [1]},
        "auto_classify": "urgent",
        "auto_tags": ["!!Renato"],
        "auto_priority": 10,
        "requires_approval": False
    },
    {
        "nome": "Remetente C2 - Importante",
        "conditions": {"sender_circles": [2]},
        "auto_classify": "important",
        "auto_tags": ["!!Renato"],
        "auto_priority": 8,
        "requires_approval": True
    },
    {
        "nome": "Remetente C3",
        "conditions": {"sender_circles": [3]},
        "auto_classify": "important",
        "auto_tags": [],
        "auto_priority": 6,
        "requires_approval": True
    },
    {
        "nome": "Keyword Urgente",
        "conditions": {"keywords": URGENT_KEYWORDS[:5]},
        "auto_classify": "urgent",
        "auto_tags": ["urgente"],
        "auto_priority": 9,
        "requires_approval": True
    },
    {
        "nome": "Proposta/Orçamento",
        "conditions": {"keywords": MONEY_KEYWORDS[:5]},
        "auto_classify": "important",
        "auto_tags": ["proposta", "!!Renato"],
        "auto_priority": 8,
        "requires_approval": True
    },
    {
        "nome": "Sem resposta 3+ dias",
        "conditions": {"unanswered_days": 3, "sender_circles": [1, 2, 3]},
        "auto_classify": "followup",
        "auto_tags": ["followup"],
        "auto_priority": 7,
        "requires_approval": True
    },
    {
        "nome": "Menção de reunião",
        "conditions": {"keywords": MEETING_KEYWORDS[:5]},
        "auto_classify": "important",
        "auto_tags": ["agendar"],
        "auto_priority": 6,
        "requires_approval": True
    }
]


class EmailTriageService:
    def __init__(self):
        self.rules = None  # Carregadas do banco

    # =========================================================================
    # CLASSIFICAÇÃO
    # =========================================================================

    def analyze_email(self, message_id: int) -> Optional[Dict]:
        """Analisa um email e retorna classificação + sugestões"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar email com dados do contato
            cursor.execute("""
                SELECT
                    m.id, m.conversation_id, m.contact_id, m.conteudo, m.direcao,
                    m.metadata, m.enviado_em, m.criado_em,
                    c.assunto, c.canal,
                    ct.id as contact_id, ct.nome as contact_nome, ct.empresa,
                    ct.circulo, ct.circulo_pessoal, ct.circulo_profissional,
                    ct.emails as contact_emails,
                    ga.tipo as account_type
                FROM messages m
                LEFT JOIN conversations c ON c.id = m.conversation_id
                LEFT JOIN contacts ct ON ct.id = m.contact_id
                LEFT JOIN google_accounts ga ON ga.email = (m.metadata->>'account')
                WHERE m.id = %s
            """, (message_id,))

            row = cursor.fetchone()
            if not row:
                return None

            email_data = dict(row)

            # Extrair informações
            subject = email_data.get('assunto') or ''
            body = email_data.get('conteudo') or ''
            sender_circle = self._get_effective_circle(email_data)
            account_type = email_data.get('account_type') or 'professional'

            # Aplicar regras
            rules = self._get_rules(cursor)
            classification = self._apply_rules(email_data, rules, cursor)

            # Detectar keywords
            detected_keywords = self.detect_keywords(subject, body)

            # Verificar se thread sem resposta
            unanswered_days = self._check_unanswered(cursor, email_data.get('conversation_id'))

            # Construir razões
            reasons = []
            if sender_circle and sender_circle <= 3:
                reasons.append(f"Remetente C{sender_circle}")
            if detected_keywords:
                reasons.append(f"Contém: {', '.join(detected_keywords[:3])}")
            if unanswered_days and unanswered_days >= 3:
                reasons.append(f"Sem resposta há {unanswered_days} dias")

            # Sugerir ações
            suggested_actions = self._suggest_actions(subject, body, detected_keywords)

            return {
                "message_id": message_id,
                "conversation_id": email_data.get('conversation_id'),
                "contact_id": email_data.get('contact_id'),
                "contact_nome": email_data.get('contact_nome'),
                "empresa": email_data.get('empresa'),
                "subject": subject,
                "account_type": account_type,
                "sender_circle": sender_circle,
                "classification": classification.get('classify', 'fyi'),
                "priority": classification.get('priority', 5),
                "suggested_tags": classification.get('tags', []),
                "classification_reasons": reasons,
                "suggested_actions": suggested_actions,
                "requires_approval": classification.get('requires_approval', True),
                "detected_keywords": detected_keywords,
                "unanswered_days": unanswered_days
            }

    def process_new_emails(self, account_type: str = None, limit: int = 50) -> Dict:
        """Processa emails novos da inbox e cria registros de triagem"""
        stats = {
            "processed": 0,
            "urgent": 0,
            "important": 0,
            "followup": 0,
            "skipped": 0,
            "errors": 0,
            "error_details": []
        }

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Diagnóstico: contar emails disponíveis
                cursor.execute("""
                    SELECT COUNT(*) as total FROM messages m
                    LEFT JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.direcao = 'incoming' AND c.canal = 'email'
                """)
                total_emails = cursor.fetchone()['total']
                stats["total_emails_incoming"] = total_emails

                cursor.execute("""
                    SELECT COUNT(*) as total FROM messages m
                    LEFT JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.direcao = 'incoming' AND c.canal = 'email'
                    AND m.criado_em > NOW() - INTERVAL '30 days'
                """)
                recent_emails = cursor.fetchone()['total']
                stats["emails_last_30_days"] = recent_emails

                # Buscar emails não processados (incoming, últimos 30 dias)
                query = """
                    SELECT m.id
                    FROM messages m
                    LEFT JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.direcao = 'incoming'
                    AND c.canal = 'email'
                    AND m.criado_em > NOW() - INTERVAL '30 days'
                    AND m.id NOT IN (SELECT message_id FROM email_triage WHERE message_id IS NOT NULL)
                """
                params = []

                if account_type:
                    query += " AND (m.metadata->>'account') IN (SELECT email FROM google_accounts WHERE tipo = %s)"
                    params.append(account_type)

                query += " ORDER BY m.criado_em DESC LIMIT %s"
                params.append(limit)

                cursor.execute(query, params)
                message_ids = [row['id'] for row in cursor.fetchall()]
                stats["found_to_process"] = len(message_ids)

                for msg_id in message_ids:
                    try:
                        analysis = self.analyze_email(msg_id)
                        if not analysis:
                            stats["skipped"] += 1
                            continue

                        # Verificar se precisa de atenção
                        if not analysis.get('classification') or analysis['classification'] == 'fyi':
                            if analysis.get('sender_circle') and analysis['sender_circle'] > 3:
                                stats["skipped"] += 1
                                continue

                        # Criar registro de triagem
                        cursor.execute("""
                            INSERT INTO email_triage (
                                message_id, conversation_id, contact_id,
                                needs_attention, priority, classification,
                                classification_reasons, suggested_tags, suggested_actions,
                                status, account_type, ai_confidence,
                                expires_at
                            ) VALUES (
                                %s, %s, %s,
                                %s, %s, %s,
                                %s, %s, %s,
                                %s, %s, %s,
                                NOW() + INTERVAL '7 days'
                            )
                            RETURNING id
                        """, (
                            analysis['message_id'],
                            analysis.get('conversation_id'),
                            analysis.get('contact_id'),
                            True,
                            analysis.get('priority', 5),
                            analysis.get('classification'),
                            json.dumps(analysis.get('classification_reasons', [])),
                            json.dumps(analysis.get('suggested_tags', [])),
                            json.dumps(analysis.get('suggested_actions', [])),
                            'approved' if not analysis.get('requires_approval') else 'pending',
                            analysis.get('account_type'),
                            0.85
                        ))

                        stats["processed"] += 1
                        classification = analysis.get('classification', '')
                        if classification == 'urgent':
                            stats["urgent"] += 1
                        elif classification == 'important':
                            stats["important"] += 1
                        elif classification == 'followup':
                            stats["followup"] += 1

                    except Exception as e:
                        print(f"Error processing message {msg_id}: {e}")
                        stats["errors"] += 1
                        stats["error_details"].append(f"msg {msg_id}: {str(e)}")

                conn.commit()

                return stats
        except Exception as e:
            import traceback
            stats["error_details"].append(str(e))
            stats["traceback"] = traceback.format_exc()
            print(f"Error in process_new_emails: {traceback.format_exc()}")
            return stats

    def _get_effective_circle(self, email_data: Dict) -> Optional[int]:
        """Retorna o círculo efetivo do contato"""
        circulo = email_data.get('circulo')
        if circulo:
            return circulo

        cp = email_data.get('circulo_pessoal')
        cr = email_data.get('circulo_profissional')
        if cp and cr:
            return min(cp, cr)
        return cp or cr

    def _get_rules(self, cursor) -> List[Dict]:
        """Carrega regras do banco ou usa defaults"""
        cursor.execute("""
            SELECT * FROM email_triage_rules
            WHERE ativo = TRUE
            ORDER BY ordem ASC
        """)
        rules = cursor.fetchall()

        if not rules:
            return DEFAULT_RULES

        return [dict(r) for r in rules]

    def _apply_rules(self, email_data: Dict, rules: List[Dict], cursor) -> Dict:
        """Aplica regras de classificação ao email"""
        subject = email_data.get('assunto') or ''
        body = email_data.get('conteudo') or ''
        sender_circle = self._get_effective_circle(email_data)
        conversation_id = email_data.get('conversation_id')

        for rule in rules:
            conditions = rule.get('conditions', {})
            match = True

            # Verificar círculo do remetente
            if 'sender_circles' in conditions:
                if not sender_circle or sender_circle not in conditions['sender_circles']:
                    match = False

            # Verificar keywords
            if match and 'keywords' in conditions:
                text = f"{subject} {body}".lower()
                keyword_match = any(kw.lower() in text for kw in conditions['keywords'])
                if not keyword_match:
                    match = False

            # Verificar dias sem resposta
            if match and 'unanswered_days' in conditions:
                unanswered = self._check_unanswered(cursor, conversation_id)
                if not unanswered or unanswered < conditions['unanswered_days']:
                    # Mas só aplica se também passar no filtro de círculo
                    if 'sender_circles' in conditions:
                        if not sender_circle or sender_circle not in conditions['sender_circles']:
                            match = False
                    else:
                        match = False

            if match:
                return {
                    "classify": rule.get('auto_classify', 'important'),
                    "tags": rule.get('auto_tags', []),
                    "priority": rule.get('auto_priority', 5),
                    "requires_approval": rule.get('requires_approval', True),
                    "rule_name": rule.get('nome')
                }

        # Default se nenhuma regra aplicar
        return {
            "classify": "fyi",
            "tags": [],
            "priority": 3,
            "requires_approval": True
        }

    def _check_unanswered(self, cursor, conversation_id: int) -> Optional[int]:
        """Verifica se thread está sem resposta e retorna quantos dias"""
        if not conversation_id:
            return None

        cursor.execute("""
            SELECT
                MAX(CASE WHEN direcao = 'incoming' THEN enviado_em END) as last_incoming,
                MAX(CASE WHEN direcao = 'outgoing' THEN enviado_em END) as last_outgoing
            FROM messages
            WHERE conversation_id = %s
        """, (conversation_id,))

        row = cursor.fetchone()
        if not row or not row['last_incoming']:
            return None

        last_incoming = row['last_incoming']
        last_outgoing = row['last_outgoing']

        # Se não tem resposta ou última resposta é antes do último incoming
        if not last_outgoing or last_outgoing < last_incoming:
            days = (datetime.now() - last_incoming).days
            return days

        return None

    # =========================================================================
    # KEYWORDS
    # =========================================================================

    def detect_keywords(self, subject: str, body: str) -> List[str]:
        """Detecta keywords relevantes no email"""
        text = f"{subject} {body}".lower()
        detected = []

        all_keywords = {
            'urgente': URGENT_KEYWORDS,
            'reunião': MEETING_KEYWORDS,
            'proposta': MONEY_KEYWORDS,
            'pergunta': QUESTION_KEYWORDS,
            'followup': FOLLOWUP_KEYWORDS
        }

        for category, keywords in all_keywords.items():
            for kw in keywords:
                if kw.lower() in text:
                    if category not in detected:
                        detected.append(category)
                    break

        return detected

    def _suggest_actions(self, subject: str, body: str, keywords: List[str]) -> List[Dict]:
        """Sugere ações baseadas no conteúdo do email"""
        actions = []
        text = f"{subject} {body}".lower()

        # Sempre sugerir responder para emails importantes
        actions.append({
            "type": "respond",
            "reason": "Responder ao email"
        })

        # Se menciona reunião, sugerir agendar
        if 'reunião' in keywords or any(kw in text for kw in MEETING_KEYWORDS):
            actions.append({
                "type": "schedule",
                "reason": "Menciona reunião ou agenda"
            })

        # Se é proposta ou orçamento, sugerir criar projeto
        if 'proposta' in keywords or any(kw in text for kw in MONEY_KEYWORDS[:3]):
            actions.append({
                "type": "project",
                "reason": "Relacionado a proposta/negócio"
            })

        # Sempre sugerir criar tarefa como opção
        actions.append({
            "type": "task",
            "reason": "Criar lembrete/tarefa"
        })

        return actions

    # =========================================================================
    # LISTAGEM E FILTROS
    # =========================================================================

    def get_triage_list(
        self,
        status: str = 'pending',
        account_type: str = None,
        classification: str = None,
        limit: int = 50,
        offset: int = 0
    ) -> Dict:
        """Lista emails para triagem com filtros"""
        with get_db() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    et.*,
                    m.conteudo, m.metadata, m.enviado_em,
                    c.assunto, c.ultimo_mensagem,
                    ct.nome as contact_nome, ct.empresa, ct.foto_url,
                    ct.circulo, ct.circulo_pessoal, ct.circulo_profissional
                FROM email_triage et
                LEFT JOIN messages m ON m.id = et.message_id
                LEFT JOIN conversations c ON c.id = et.conversation_id
                LEFT JOIN contacts ct ON ct.id = et.contact_id
                WHERE 1=1
            """
            params = []

            if status:
                query += " AND et.status = %s"
                params.append(status)

            if account_type:
                query += " AND et.account_type = %s"
                params.append(account_type)

            if classification:
                query += " AND et.classification = %s"
                params.append(classification)

            query += " ORDER BY et.priority DESC, et.criado_em DESC"
            query += " LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            cursor.execute(query, params)
            items = [dict(row) for row in cursor.fetchall()]

            # Contar total
            count_query = """
                SELECT COUNT(*) as total FROM email_triage et WHERE 1=1
            """
            count_params = []
            if status:
                count_query += " AND et.status = %s"
                count_params.append(status)
            if account_type:
                count_query += " AND et.account_type = %s"
                count_params.append(account_type)
            if classification:
                count_query += " AND et.classification = %s"
                count_params.append(classification)

            cursor.execute(count_query, count_params)
            result = cursor.fetchone()
            total = result['total'] if result else 0

            return {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset
            }

    def get_stats(self) -> Dict:
        """Retorna estatísticas da triagem"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    status,
                    classification,
                    COUNT(*) as count
                FROM email_triage
                WHERE criado_em > NOW() - INTERVAL '7 days'
                GROUP BY status, classification
            """)

            rows = cursor.fetchall()

            stats = {
                "by_status": {},
                "by_classification": {},
                "total_pending": 0,
                "total_approved": 0,
                "total_actioned": 0
            }

            for row in rows:
                status = row['status']
                classification = row['classification']
                count = row['count']

                if status not in stats["by_status"]:
                    stats["by_status"][status] = 0
                stats["by_status"][status] += count

                if classification:
                    if classification not in stats["by_classification"]:
                        stats["by_classification"][classification] = 0
                    stats["by_classification"][classification] += count

            stats["total_pending"] = stats["by_status"].get("pending", 0)
            stats["total_approved"] = stats["by_status"].get("approved", 0)
            stats["total_actioned"] = stats["by_status"].get("actioned", 0)

            return stats

    # =========================================================================
    # APROVAÇÃO E AÇÕES
    # =========================================================================

    def approve_batch(self, triage_ids: List[int], tags: List[str] = None) -> Dict:
        """Aprova múltiplos emails em lote"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE email_triage
                SET status = 'approved',
                    approved_tags = %s,
                    approved_at = NOW()
                WHERE id = ANY(%s)
                AND status = 'pending'
                RETURNING id
            """, (json.dumps(tags) if tags else None, triage_ids))

            updated = cursor.fetchall()
            conn.commit()

            return {
                "approved": len(updated),
                "ids": [r['id'] for r in updated]
            }

    def dismiss_batch(self, triage_ids: List[int], reason: str = None) -> Dict:
        """Descarta múltiplos emails"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE email_triage
                SET status = 'dismissed',
                    dismissed_at = NOW()
                WHERE id = ANY(%s)
                AND status = 'pending'
                RETURNING id
            """, (triage_ids,))

            updated = cursor.fetchall()
            conn.commit()

            return {
                "dismissed": len(updated),
                "ids": [r['id'] for r in updated]
            }

    def mark_actioned(self, triage_id: int, action: str) -> Dict:
        """Marca email como tendo ação tomada"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE email_triage
                SET status = 'actioned',
                    action_taken = %s,
                    actioned_at = NOW()
                WHERE id = %s
                RETURNING id, contact_id
            """, (action, triage_id))

            row = cursor.fetchone()

            # Atualizar ultimo_contato do contato se tiver ação
            if row and row['contact_id']:
                cursor.execute("""
                    UPDATE contacts
                    SET ultimo_contato = NOW()
                    WHERE id = %s
                """, (row['contact_id'],))

            conn.commit()

            return {"success": bool(row), "id": triage_id}

    # =========================================================================
    # REGRAS
    # =========================================================================

    def get_rules_list(self) -> List[Dict]:
        """Lista todas as regras de classificação"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM email_triage_rules
                ORDER BY ordem ASC, criado_em ASC
            """)
            return [dict(r) for r in cursor.fetchall()]

    def create_rule(self, data: Dict) -> Dict:
        """Cria nova regra de classificação"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO email_triage_rules (
                    nome, descricao, conditions,
                    auto_classify, auto_tags, auto_priority,
                    requires_approval, ativo, ordem
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                data.get('nome'),
                data.get('descricao'),
                json.dumps(data.get('conditions', {})),
                data.get('auto_classify'),
                json.dumps(data.get('auto_tags', [])),
                data.get('auto_priority', 5),
                data.get('requires_approval', True),
                data.get('ativo', True),
                data.get('ordem', 100)
            ))

            rule = dict(cursor.fetchone())
            conn.commit()

            return rule

    def update_rule(self, rule_id: int, data: Dict) -> Dict:
        """Atualiza regra existente"""
        with get_db() as conn:
            cursor = conn.cursor()

            updates = []
            params = []

            for field in ['nome', 'descricao', 'auto_classify', 'auto_priority', 'requires_approval', 'ativo', 'ordem']:
                if field in data:
                    updates.append(f"{field} = %s")
                    params.append(data[field])

            if 'conditions' in data:
                updates.append("conditions = %s")
                params.append(json.dumps(data['conditions']))

            if 'auto_tags' in data:
                updates.append("auto_tags = %s")
                params.append(json.dumps(data['auto_tags']))

            if not updates:
                return {"error": "No fields to update"}

            params.append(rule_id)

            cursor.execute(f"""
                UPDATE email_triage_rules
                SET {', '.join(updates)}
                WHERE id = %s
                RETURNING *
            """, params)

            rule = cursor.fetchone()
            conn.commit()

            return dict(rule) if rule else {"error": "Rule not found"}

    def delete_rule(self, rule_id: int) -> Dict:
        """Deleta regra"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM email_triage_rules WHERE id = %s RETURNING id", (rule_id,))
            deleted = cursor.fetchone()
            conn.commit()
            return {"deleted": bool(deleted)}

    def init_default_rules(self) -> Dict:
        """Inicializa regras default se não existirem"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) as total FROM email_triage_rules")
            result = cursor.fetchone()
            count = result['total'] if result else 0

            if count > 0:
                return {"message": "Rules already exist", "count": count}

            created = 0
            for i, rule in enumerate(DEFAULT_RULES):
                cursor.execute("""
                    INSERT INTO email_triage_rules (
                        nome, conditions, auto_classify, auto_tags,
                        auto_priority, requires_approval, ordem
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    rule['nome'],
                    json.dumps(rule['conditions']),
                    rule.get('auto_classify'),
                    json.dumps(rule.get('auto_tags', [])),
                    rule.get('auto_priority', 5),
                    rule.get('requires_approval', True),
                    (i + 1) * 10
                ))
                created += 1

            conn.commit()
            return {"created": created}


# Singleton
_email_triage_service = None


def get_email_triage_service() -> EmailTriageService:
    global _email_triage_service
    if _email_triage_service is None:
        _email_triage_service = EmailTriageService()
    return _email_triage_service
