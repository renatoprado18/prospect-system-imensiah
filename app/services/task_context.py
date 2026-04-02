"""
Task Context Service
Enriquece tarefas com contexto de WhatsApp, Email e Projeto.

Autor: INTEL
Data: 2026-03-30
"""
import logging
import os
from datetime import datetime
from typing import Dict, List, Any, Optional
import httpx
from database import get_db

logger = logging.getLogger(__name__)


class TaskContextService:
    """
    Servico para buscar contexto de uma tarefa.

    Dado uma tarefa, busca:
    - Contato relacionado (pelo titulo ou contact_id)
    - Mensagens WhatsApp recentes
    - Emails recentes
    - Contexto do projeto
    - Sugestao de acao com IA
    """

    def __init__(self):
        self.claude_api_key = os.getenv("ANTHROPIC_API_KEY")

    def get_task_with_project(self, task_id: int) -> Optional[Dict]:
        """Busca tarefa com dados do projeto."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT t.*, p.nome as project_name, p.descricao as project_description,
                       c.nome as contact_name, c.id as linked_contact_id
                FROM tasks t
                LEFT JOIN projects p ON t.project_id = p.id
                LEFT JOIN contacts c ON t.contact_id = c.id
                WHERE t.id = %s
            """, (task_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def find_contact_from_task(self, task: Dict) -> Optional[Dict]:
        """
        Encontra o contato relacionado a tarefa.
        Ordem de busca:
        1. contact_id direto na tarefa
        2. Nome no titulo da tarefa
        3. Participantes do projeto (se task pertence a um projeto)
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Query completa para buscar contato com resumo IA
            contact_query = """
                SELECT id, nome, email, telefone, empresa, cargo, circulo,
                       circulo_pessoal, circulo_profissional, contexto,
                       resumo_ia, ultimo_contato, aniversario, linkedin
                FROM contacts WHERE id = %s
            """

            # 1. Se ja tem contact_id vinculado
            if task.get('linked_contact_id'):
                cursor.execute(contact_query, (task['linked_contact_id'],))
                row = cursor.fetchone()
                if row:
                    return dict(row)

            # 2. Buscar pelo nome no titulo da tarefa
            titulo = task.get('titulo', '')

            # Extrair possivel nome do titulo
            # Ex: "Mensagem para Rodrigo Pretola" -> "Rodrigo Pretola"
            # Ex: "Follow-up: Mensagem para Rodrigo Pretola" -> "Rodrigo Pretola"
            import re
            patterns = [
                r'para\s+([A-Z][a-záéíóúâêîôûãõ]+(?:\s+[A-Z][a-záéíóúâêîôûãõ]+)*)',  # "para Nome Sobrenome"
                r'com\s+([A-Z][a-záéíóúâêîôûãõ]+(?:\s+[A-Z][a-záéíóúâêîôûãõ]+)*)',  # "com Nome Sobrenome"
                r'de\s+([A-Z][a-záéíóúâêîôûãõ]+(?:\s+[A-Z][a-záéíóúâêîôûãõ]+)*)',   # "de Nome Sobrenome"
                r'(?:ligar|contatar|email|mensagem|whatsapp)\s+(?:para\s+)?([A-Z][a-záéíóúâêîôûãõ]+(?:\s+[A-Z][a-záéíóúâêîôûãõ]+)*)',
            ]

            potential_name = None
            for pattern in patterns:
                match = re.search(pattern, titulo)
                if match:
                    potential_name = match.group(1).strip()
                    break

            if potential_name and len(potential_name) > 2:
                # Buscar contato por nome similar
                cursor.execute("""
                    SELECT id, nome, email, telefone, empresa, cargo, circulo,
                           circulo_pessoal, circulo_profissional, contexto,
                           resumo_ia, ultimo_contato, aniversario, linkedin
                    FROM contacts
                    WHERE nome ILIKE %s
                    ORDER BY
                        CASE WHEN nome ILIKE %s THEN 0 ELSE 1 END,
                        LENGTH(nome)
                    LIMIT 1
                """, (f'%{potential_name}%', potential_name))
                row = cursor.fetchone()
                if row:
                    return dict(row)

            # 3. Buscar entre participantes do projeto
            if task.get('project_id'):
                # Extrair qualquer nome proprio do titulo
                words = titulo.split()
                name_candidates = []
                for i, word in enumerate(words):
                    # Palavras que começam com maiúscula e não são conectivos
                    if word and word[0].isupper() and word.lower() not in [
                        'follow-up:', 'follow-up', 'followup', 'fup', 'mensagem',
                        'email', 'ligar', 'contatar', 'reuniao', 'meeting', 'call',
                        'para', 'com', 'de', 'tarefa', 'task', 'enviar', 'responder'
                    ]:
                        name_candidates.append(word)

                # Buscar participantes do projeto (tabela project_members)
                cursor.execute("""
                    SELECT c.id, c.nome, c.email, c.telefone, c.empresa, c.cargo, c.circulo,
                           c.circulo_pessoal, c.circulo_profissional, c.contexto,
                           c.resumo_ia, c.ultimo_contato, c.aniversario, c.linkedin,
                           pm.papel
                    FROM project_members pm
                    JOIN contacts c ON c.id = pm.contact_id
                    WHERE pm.project_id = %s
                """, (task['project_id'],))

                participants = [dict(row) for row in cursor.fetchall()]

                # Tentar match com nome do titulo
                for participant in participants:
                    nome = participant.get('nome', '')
                    # Verificar se algum candidato de nome está no nome do participante
                    for candidate in name_candidates:
                        if candidate.lower() in nome.lower():
                            return participant

                    # Verificar match direto
                    if potential_name and potential_name.lower() in nome.lower():
                        return participant

        return None

    def get_whatsapp_messages(self, contact_id: int, limit: int = 20) -> List[Dict]:
        """Busca mensagens WhatsApp recentes com o contato."""
        messages = []
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.id, m.direcao, m.conteudo, m.enviado_em
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.contact_id = %s
                ORDER BY m.enviado_em DESC
                LIMIT %s
            """, (contact_id, limit))
            messages = [dict(row) for row in cursor.fetchall()]

        # Inverter para ordem cronologica
        messages.reverse()
        return messages

    def get_emails(self, contact_id: int, limit: int = 10) -> List[Dict]:
        """Busca emails recentes com o contato."""
        emails = []
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar email do contato
            cursor.execute("SELECT email FROM contacts WHERE id = %s", (contact_id,))
            contact = cursor.fetchone()
            if not contact or not contact.get('email'):
                return []

            email = contact['email']

            # Buscar emails enviados ou recebidos
            cursor.execute("""
                SELECT id, subject, snippet, sender, recipients, date, direction
                FROM emails
                WHERE sender ILIKE %s OR recipients ILIKE %s
                ORDER BY date DESC
                LIMIT %s
            """, (f'%{email}%', f'%{email}%', limit))
            emails = [dict(row) for row in cursor.fetchall()]

        return emails

    def get_project_context(self, project_id: int) -> Dict:
        """Busca contexto do projeto."""
        with get_db() as conn:
            cursor = conn.cursor()

            # Projeto
            cursor.execute("""
                SELECT id, nome, descricao, tipo, status, valor, empresa_relacionada
                FROM projects WHERE id = %s
            """, (project_id,))
            project = cursor.fetchone()

            if not project:
                return {}

            project = dict(project)

            # Participantes (tabela project_members)
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, pm.papel
                FROM project_members pm
                JOIN contacts c ON c.id = pm.contact_id
                WHERE pm.project_id = %s
            """, (project_id,))
            project['participantes'] = [dict(row) for row in cursor.fetchall()]

            # Outras tarefas do projeto
            cursor.execute("""
                SELECT id, titulo, status, data_vencimento
                FROM tasks
                WHERE project_id = %s
                ORDER BY data_vencimento ASC NULLS LAST
                LIMIT 10
            """, (project_id,))
            project['outras_tarefas'] = [dict(row) for row in cursor.fetchall()]

            return project

    async def generate_action_suggestion(
        self,
        task: Dict,
        contact: Optional[Dict],
        messages: List[Dict],
        emails: List[Dict],
        project: Dict
    ) -> Dict:
        """
        Usa Claude AI para sugerir acao baseada no contexto.
        """
        if not self.claude_api_key:
            return {"error": "API key nao configurada"}

        # Montar contexto
        context_parts = []

        # Tarefa
        context_parts.append(f"TAREFA: {task.get('titulo', 'Sem titulo')}")
        if task.get('descricao'):
            context_parts.append(f"Descricao: {task['descricao']}")

        # Contato - com informações completas
        if contact:
            context_parts.append(f"\n=== CONTATO ===")
            context_parts.append(f"Nome: {contact.get('nome', 'Desconhecido')}")
            if contact.get('empresa'):
                context_parts.append(f"Empresa: {contact['empresa']}")
            if contact.get('cargo'):
                context_parts.append(f"Cargo: {contact['cargo']}")
            if contact.get('email'):
                context_parts.append(f"Email: {contact['email']}")
            if contact.get('telefone'):
                context_parts.append(f"Telefone: {contact['telefone']}")

            # Círculos e contexto
            circulo_info = []
            if contact.get('circulo_pessoal'):
                circulo_info.append(f"Pessoal P{contact['circulo_pessoal']}")
            if contact.get('circulo_profissional'):
                circulo_info.append(f"Profissional R{contact['circulo_profissional']}")
            if circulo_info:
                context_parts.append(f"Circulos: {', '.join(circulo_info)}")

            # Último contato
            if contact.get('ultimo_contato'):
                uc = contact['ultimo_contato']
                if isinstance(uc, datetime):
                    dias = (datetime.now() - uc).days
                    context_parts.append(f"Ultimo contato: {uc.strftime('%d/%m/%Y')} ({dias} dias atras)")

            # RESUMO IA - contexto rico sobre o relacionamento
            if contact.get('resumo_ia'):
                context_parts.append(f"\nRESUMO DO RELACIONAMENTO:")
                context_parts.append(contact['resumo_ia'][:1000])

            # Papel no projeto (se veio do participante)
            if contact.get('papel'):
                context_parts.append(f"Papel no projeto: {contact['papel']}")

        # Projeto
        if project:
            context_parts.append(f"\n=== PROJETO ===")
            context_parts.append(f"Nome: {project.get('nome', '')}")
            if project.get('tipo'):
                context_parts.append(f"Tipo: {project['tipo']}")
            if project.get('status'):
                context_parts.append(f"Status: {project['status']}")
            if project.get('descricao'):
                context_parts.append(f"Descricao: {project['descricao']}")
            if project.get('participantes'):
                participants = ", ".join([
                    f"{p['nome']} ({p.get('papel', 'participante')})"
                    for p in project['participantes']
                ])
                context_parts.append(f"Participantes: {participants}")

            # Outras tarefas do projeto
            if project.get('outras_tarefas'):
                tarefas_status = []
                for t in project['outras_tarefas'][:5]:
                    status_icon = "✓" if t.get('status') == 'completed' else "○"
                    tarefas_status.append(f"{status_icon} {t.get('titulo', '')}")
                if tarefas_status:
                    context_parts.append(f"Tarefas do projeto: {'; '.join(tarefas_status)}")

        # Mensagens WhatsApp - mais detalhes
        if messages:
            context_parts.append(f"\n=== MENSAGENS WHATSAPP ({len(messages)} msgs) ===")
            for msg in messages[-15:]:  # Ultimas 15
                contact_name = contact.get('nome', 'Contato') if contact else 'Contato'
                direction = "Eu" if msg.get('direcao') == 'outgoing' else contact_name
                date = msg.get('enviado_em', '')
                if isinstance(date, datetime):
                    date = date.strftime('%d/%m %H:%M')
                content = msg.get('conteudo', '')[:300]
                context_parts.append(f"[{date}] {direction}: {content}")
        else:
            context_parts.append("\nSem historico de mensagens WhatsApp")

        # Emails
        if emails:
            context_parts.append(f"\n=== EMAILS RECENTES ({len(emails)}) ===")
            for email in emails[:5]:
                direction = "Enviado" if email.get('direction') == 'sent' else "Recebido"
                context_parts.append(f"- {direction}: {email.get('subject', 'Sem assunto')}")

        context = "\n".join(context_parts)

        prompt = f"""Voce e um assistente executivo de alto nivel, especializado em gestao de relacionamentos estrategicos.

CONTEXTO COMPLETO:
{context}

TAREFA A EXECUTAR: {task.get('titulo', '')}

Analise TODO o contexto acima - historico de conversas, resumo do relacionamento, projeto em andamento, e status atual - e forneca uma sugestao pratica e especifica.

Responda APENAS em JSON valido (sem markdown):
{{
  "resumo_situacao": "Contexto completo: quem e a pessoa, qual o historico, em que ponto esta o relacionamento/negociacao",
  "ultima_interacao": "Quando foi, o que foi discutido, se ha pendencias ou expectativas",
  "sugestao_acao": "Acao especifica e pratica (ex: 'Enviar WhatsApp agradecendo e propondo proximos passos', 'Ligar para alinhar expectativas')",
  "mensagem_sugerida": "Texto completo da mensagem, personalizado para o contexto e tom da conversa anterior. Se for follow-up, referencie a conversa anterior.",
  "timing": "Momento ideal considerando o contexto (ex: 'Hoje pela manha - ha 2 dias aguardando resposta')",
  "observacoes": "Insights estrategicos: riscos, oportunidades, pontos de atencao baseados no historico"
}}

IMPORTANTE:
- Use o RESUMO DO RELACIONAMENTO para entender o contexto historico
- Analise o TOM das mensagens anteriores para manter consistencia
- Se houver negociacao em andamento, considere a fase atual
- A mensagem sugerida deve ser natural, no estilo das conversas anteriores"""

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.claude_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": prompt}]
                    }
                )

                if response.status_code == 200:
                    result = response.json()
                    content = result.get("content", [{}])[0].get("text", "{}")

                    # Parse JSON
                    import json
                    try:
                        # Extrair JSON do texto
                        json_match = content
                        if "```json" in content:
                            json_match = content.split("```json")[1].split("```")[0]
                        elif "```" in content:
                            json_match = content.split("```")[1].split("```")[0]

                        return json.loads(json_match.strip())
                    except:
                        return {"resumo_situacao": content, "sugestao_acao": "Verificar contexto manualmente"}
                else:
                    logger.error(f"Claude API error: {response.status_code}")
                    return {"error": f"API error: {response.status_code}"}

        except Exception as e:
            logger.error(f"Error calling Claude: {e}")
            return {"error": str(e)}

    async def get_task_context(self, task_id: int) -> Dict:
        """
        Busca contexto completo de uma tarefa.
        Retorna: tarefa, contato, mensagens, emails, projeto, sugestao IA.
        """
        try:
            # Buscar tarefa
            task = self.get_task_with_project(task_id)
            if not task:
                return {"error": "Tarefa nao encontrada"}

            # Encontrar contato
            contact = None
            try:
                contact = self.find_contact_from_task(task)
            except Exception as e:
                logger.error(f"Error finding contact: {e}")

            # Buscar mensagens e emails
            messages = []
            emails = []
            if contact:
                try:
                    messages = self.get_whatsapp_messages(contact['id'])
                except Exception as e:
                    logger.error(f"Error fetching messages: {e}")
                try:
                    emails = self.get_emails(contact['id'])
                except Exception as e:
                    logger.error(f"Error fetching emails: {e}")

            # Buscar contexto do projeto
            project = {}
            if task.get('project_id'):
                try:
                    project = self.get_project_context(task['project_id'])
                except Exception as e:
                    logger.error(f"Error fetching project context: {e}")

            # Gerar sugestao com IA
            suggestion = {}
            try:
                suggestion = await self.generate_action_suggestion(
                    task, contact, messages, emails, project
                )
            except Exception as e:
                logger.error(f"Error generating suggestion: {e}")
                suggestion = {"error": str(e)}

            return {
                "task": task,
                "contact": contact,
                "messages": messages,
                "emails": emails,
                "project": project,
                "suggestion": suggestion
            }

        except Exception as e:
            logger.error(f"Error in get_task_context: {e}")
            return {"error": str(e)}

    async def suggest_followup(self, task_id: int) -> Dict:
        """
        Sugere follow-up ao completar uma tarefa.
        Analisa contexto e sugere prazo apropriado.
        """
        try:
            # Buscar tarefa e contexto
            task = self.get_task_with_project(task_id)
            if not task:
                return {"needs_followup": False}

            contact = None
            try:
                contact = self.find_contact_from_task(task)
            except:
                pass

            # Buscar últimas mensagens para contexto
            messages = []
            if contact:
                try:
                    messages = self.get_whatsapp_messages(contact['id'], limit=5)
                except:
                    pass

            # Buscar projeto
            project = {}
            if task.get('project_id'):
                try:
                    project = self.get_project_context(task['project_id'])
                except:
                    pass

            # Determinar se precisa follow-up e prazo
            # Regras simples primeiro, depois IA
            titulo = task.get('titulo', '').lower()

            # Detectar tipo de tarefa
            is_communication = any(word in titulo for word in [
                'mensagem', 'email', 'ligar', 'contatar', 'whatsapp', 'wa ',
                'enviar', 'responder', 'falar com', 'cobran', 'cobrança',
                'follow-up', 'followup', 'fup', 'retorno', 'lembrar'
            ])

            is_proposal = any(word in titulo for word in [
                'proposta', 'orçamento', 'cotação', 'pitch', 'apresentação',
                'enviar doc', 'contrato', 'acordo'
            ])

            is_meeting = any(word in titulo for word in [
                'reunião', 'meeting', 'call', 'ligação', 'agendar', 'marcar'
            ])

            is_financial = any(word in titulo for word in [
                'pagamento', 'pagar', 'receber', 'cobrar', 'cobrança',
                'boleto', 'fatura', 'honorário', 'r$', 'valor'
            ])

            # Sugerir prazo baseado no tipo
            if not is_communication and not is_proposal and not is_meeting and not is_financial:
                return {"needs_followup": False}

            # Prazo sugerido
            if is_financial:
                days = 7
                reason = "Verificar se pagamento foi efetuado"
            elif is_proposal:
                days = 5
                reason = "Proposta enviada - aguardar retorno"
            elif is_meeting:
                days = 1
                reason = "Confirmar agenda ou preparar próximos passos"
            else:
                days = 3
                reason = "Verificar se houve resposta"

            # Gerar título do follow-up
            contact_name = contact.get('nome', '') if contact else ''
            if contact_name:
                followup_title = f"Follow-up com {contact_name}"
            else:
                # Extrair nome do título original
                followup_title = f"Follow-up: {task.get('titulo', 'Tarefa')}"

            # Se tiver API key, usar IA para refinar
            if self.claude_api_key and contact:
                try:
                    suggestion = await self._ai_suggest_followup(
                        task, contact, messages, project
                    )
                    if suggestion and not suggestion.get('error'):
                        return suggestion
                except Exception as e:
                    logger.error(f"AI followup error: {e}")

            return {
                "needs_followup": True,
                "followup_title": followup_title,
                "suggested_days": days,
                "reason": reason,
                "contact_id": contact.get('id') if contact else None,
                "contact_name": contact_name,
                "project_id": task.get('project_id')
            }

        except Exception as e:
            logger.error(f"Error suggesting followup: {e}")
            return {"needs_followup": False, "error": str(e)}

    async def _ai_suggest_followup(
        self,
        task: Dict,
        contact: Dict,
        messages: List[Dict],
        project: Dict
    ) -> Dict:
        """Usa IA para sugerir follow-up mais preciso."""

        context_parts = [
            f"Tarefa completada: {task.get('titulo', '')}",
            f"Contato: {contact.get('nome', '')} - {contact.get('empresa', '')}",
        ]

        if project:
            context_parts.append(f"Projeto: {project.get('nome', '')} ({project.get('tipo', '')})")

        if messages:
            context_parts.append("Últimas mensagens:")
            for msg in messages[-3:]:
                dir_label = "Enviado" if msg.get('direcao') == 'outgoing' else "Recebido"
                context_parts.append(f"- {dir_label}: {msg.get('conteudo', '')[:100]}")

        prompt = f"""Analise esta tarefa que foi completada e sugira um follow-up apropriado.

{chr(10).join(context_parts)}

Responda em JSON:
{{
    "needs_followup": true/false,
    "followup_title": "título sugerido para o follow-up",
    "suggested_days": número de dias (1-14),
    "reason": "breve explicação do porquê este prazo"
}}

Se a tarefa não precisar de follow-up (ex: tarefa interna, sem necessidade de resposta), retorne needs_followup: false."""

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.claude_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 256,
                        "messages": [{"role": "user", "content": prompt}]
                    }
                )

                if response.status_code == 200:
                    result = response.json()
                    content = result.get("content", [{}])[0].get("text", "{}")

                    import json
                    try:
                        json_match = content
                        if "```json" in content:
                            json_match = content.split("```json")[1].split("```")[0]
                        elif "```" in content:
                            json_match = content.split("```")[1].split("```")[0]

                        suggestion = json.loads(json_match.strip())

                        # Adicionar dados do contato e projeto
                        if suggestion.get("needs_followup"):
                            suggestion["contact_id"] = contact.get('id')
                            suggestion["contact_name"] = contact.get('nome', '')
                            suggestion["project_id"] = task.get('project_id')

                        return suggestion
                    except:
                        pass
        except Exception as e:
            logger.error(f"AI followup API error: {e}")

        return None


# Singleton
_task_context_service = None


def get_task_context_service() -> TaskContextService:
    """Retorna instancia singleton do servico."""
    global _task_context_service
    if _task_context_service is None:
        _task_context_service = TaskContextService()
    return _task_context_service
