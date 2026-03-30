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
        Primeiro verifica contact_id, depois busca pelo nome no titulo.
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Se ja tem contact_id vinculado
            if task.get('linked_contact_id'):
                cursor.execute("""
                    SELECT id, nome, email, telefone, empresa, cargo, circulo
                    FROM contacts WHERE id = %s
                """, (task['linked_contact_id'],))
                row = cursor.fetchone()
                if row:
                    return dict(row)

            # Buscar pelo nome no titulo da tarefa
            titulo = task.get('titulo', '')

            # Extrair possivel nome do titulo
            # Ex: "Mensagem para Rodrigo Pretola" -> "Rodrigo Pretola"
            import re
            patterns = [
                r'para\s+(.+?)(?:\s*$|\s*[-:])',
                r'com\s+(.+?)(?:\s*$|\s*[-:])',
                r'de\s+(.+?)(?:\s*$|\s*[-:])',
                r'(?:ligar|contatar|email|mensagem|whatsapp)\s+(?:para\s+)?(.+?)(?:\s*$|\s*[-:])',
            ]

            potential_name = None
            for pattern in patterns:
                match = re.search(pattern, titulo, re.IGNORECASE)
                if match:
                    potential_name = match.group(1).strip()
                    break

            if potential_name:
                # Buscar contato por nome similar (usando ILIKE para compatibilidade)
                # Primeiro tenta match exato, depois parcial
                cursor.execute("""
                    SELECT id, nome, email, telefone, empresa, cargo, circulo
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

            # Participantes
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, pp.papel
                FROM project_participants pp
                JOIN contacts c ON c.id = pp.contact_id
                WHERE pp.project_id = %s
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

        # Contato
        if contact:
            context_parts.append(f"\nCONTATO: {contact.get('nome', 'Desconhecido')}")
            if contact.get('empresa'):
                context_parts.append(f"Empresa: {contact['empresa']}")
            if contact.get('cargo'):
                context_parts.append(f"Cargo: {contact['cargo']}")

        # Projeto
        if project:
            context_parts.append(f"\nPROJETO: {project.get('nome', '')}")
            if project.get('descricao'):
                context_parts.append(f"Descricao: {project['descricao']}")
            if project.get('participantes'):
                participants = ", ".join([
                    f"{p['nome']} ({p.get('papel', 'participante')})"
                    for p in project['participantes']
                ])
                context_parts.append(f"Participantes: {participants}")

        # Mensagens WhatsApp
        if messages:
            context_parts.append("\nULTIMAS MENSAGENS WHATSAPP:")
            for msg in messages[-10:]:  # Ultimas 10
                direction = "Voce" if msg.get('direcao') == 'outgoing' else contact.get('nome', 'Contato')
                date = msg.get('enviado_em', '')
                if isinstance(date, datetime):
                    date = date.strftime('%d/%m %H:%M')
                context_parts.append(f"[{date}] {direction}: {msg.get('conteudo', '')[:200]}")

        # Emails
        if emails:
            context_parts.append("\nEMAILS RECENTES:")
            for email in emails[:5]:
                direction = "Enviado" if email.get('direction') == 'sent' else "Recebido"
                context_parts.append(f"- {direction}: {email.get('subject', 'Sem assunto')}")

        context = "\n".join(context_parts)

        prompt = f"""Voce e um assistente pessoal de gestao de relacionamentos.

Analise o contexto abaixo e sugira como executar a tarefa.

{context}

Responda em JSON:
{{
  "resumo_situacao": "Breve resumo do contexto e historico com a pessoa",
  "ultima_interacao": "Quando e sobre o que foi a ultima conversa",
  "sugestao_acao": "O que fazer agora (ex: enviar WhatsApp, ligar, aguardar)",
  "mensagem_sugerida": "Se for enviar mensagem, sugira o texto",
  "timing": "Melhor momento para contato",
  "observacoes": "Outras observacoes relevantes"
}}"""

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


# Singleton
_task_context_service = None


def get_task_context_service() -> TaskContextService:
    """Retorna instancia singleton do servico."""
    global _task_context_service
    if _task_context_service is None:
        _task_context_service = TaskContextService()
    return _task_context_service
