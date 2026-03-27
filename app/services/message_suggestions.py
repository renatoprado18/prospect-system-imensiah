"""
Message Suggestions Service - Templates e sugestoes de mensagens

Funcionalidades:
- CRUD de templates de mensagens
- Sugestoes contextuais de mensagens
- Rendering de templates com variaveis
"""
import os
import json
import re
import httpx
from typing import List, Dict, Optional
from datetime import datetime
from database import get_db

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


class MessageSuggestionsService:
    # =========================================================================
    # TEMPLATE CRUD
    # =========================================================================

    def get_templates(
        self,
        categoria: str = None,
        canal: str = None,
        active_only: bool = True
    ) -> List[Dict]:
        """Lista templates de mensagens"""
        with get_db() as conn:
            cursor = conn.cursor()

            conditions = []
            params = []

            if active_only:
                conditions.append("ativo = TRUE")

            if categoria:
                conditions.append("categoria = %s")
                params.append(categoria)

            if canal:
                conditions.append("(canal = %s OR canal IS NULL)")
                params.append(canal)

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            cursor.execute(f"""
                SELECT * FROM message_templates
                WHERE {where_clause}
                ORDER BY categoria, uso_count DESC, nome
            """, params)

            templates = []
            for row in cursor.fetchall():
                t = dict(row)
                if t.get("criado_em"):
                    t["criado_em"] = t["criado_em"].isoformat()
                if t.get("ultima_uso"):
                    t["ultima_uso"] = t["ultima_uso"].isoformat()
                templates.append(t)

            return templates

    def get_template(self, template_id: int) -> Optional[Dict]:
        """Obtem um template especifico"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT * FROM message_templates WHERE id = %s",
                (template_id,)
            )

            row = cursor.fetchone()
            if row:
                t = dict(row)
                if t.get("criado_em"):
                    t["criado_em"] = t["criado_em"].isoformat()
                return t
            return None

    def create_template(
        self,
        nome: str,
        categoria: str,
        corpo: str,
        canal: str = None,
        assunto: str = None,
        variaveis: List[str] = None,
        tags: List[str] = None
    ) -> int:
        """Cria novo template"""
        # Auto-detectar variaveis se nao fornecidas
        if variaveis is None:
            variaveis = re.findall(r'\{(\w+)\}', corpo)
            if assunto:
                variaveis.extend(re.findall(r'\{(\w+)\}', assunto))
            variaveis = list(set(variaveis))

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO message_templates
                (nome, categoria, canal, assunto, corpo, variaveis, tags)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                nome,
                categoria,
                canal,
                assunto,
                corpo,
                json.dumps(variaveis),
                json.dumps(tags or [])
            ))

            template_id = cursor.fetchone()["id"]
            conn.commit()

            return template_id

    def update_template(
        self,
        template_id: int,
        nome: str = None,
        categoria: str = None,
        corpo: str = None,
        canal: str = None,
        assunto: str = None,
        ativo: bool = None
    ) -> bool:
        """Atualiza template"""
        with get_db() as conn:
            cursor = conn.cursor()

            updates = []
            params = []

            if nome is not None:
                updates.append("nome = %s")
                params.append(nome)

            if categoria is not None:
                updates.append("categoria = %s")
                params.append(categoria)

            if corpo is not None:
                updates.append("corpo = %s")
                params.append(corpo)
                # Re-detectar variaveis
                variaveis = re.findall(r'\{(\w+)\}', corpo)
                updates.append("variaveis = %s")
                params.append(json.dumps(variaveis))

            if canal is not None:
                updates.append("canal = %s")
                params.append(canal)

            if assunto is not None:
                updates.append("assunto = %s")
                params.append(assunto)

            if ativo is not None:
                updates.append("ativo = %s")
                params.append(ativo)

            if not updates:
                return False

            params.append(template_id)

            cursor.execute(f"""
                UPDATE message_templates
                SET {', '.join(updates)}
                WHERE id = %s
                RETURNING id
            """, params)

            result = cursor.fetchone()
            conn.commit()

            return result is not None

    def delete_template(self, template_id: int) -> bool:
        """Remove template"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "DELETE FROM message_templates WHERE id = %s",
                (template_id,)
            )

            deleted = cursor.rowcount > 0
            conn.commit()

            return deleted

    # =========================================================================
    # TEMPLATE RENDERING
    # =========================================================================

    def render_template(
        self,
        template_id: int,
        variables: Dict
    ) -> Dict:
        """Renderiza template com variaveis"""
        template = self.get_template(template_id)
        if not template:
            return {"error": "Template nao encontrado"}

        corpo = template["corpo"]
        assunto = template.get("assunto", "")

        # Substituir variaveis
        for key, value in variables.items():
            corpo = corpo.replace(f"{{{key}}}", str(value))
            if assunto:
                assunto = assunto.replace(f"{{{key}}}", str(value))

        # Registrar uso
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE message_templates
                SET uso_count = uso_count + 1, ultima_uso = NOW()
                WHERE id = %s
            """, (template_id,))
            conn.commit()

        return {
            "template_id": template_id,
            "nome": template["nome"],
            "canal": template.get("canal"),
            "assunto": assunto,
            "corpo": corpo
        }

    def render_for_contact(
        self,
        template_id: int,
        contact_id: int
    ) -> Dict:
        """Renderiza template para um contato especifico"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT nome, apelido, empresa, cargo, aniversario
                FROM contacts
                WHERE id = %s
            """, (contact_id,))

            contact = cursor.fetchone()
            if not contact:
                return {"error": "Contato nao encontrado"}

            contact = dict(contact)

        # Preparar variaveis
        variables = {
            "nome": contact.get("apelido") or contact["nome"].split()[0],
            "nome_completo": contact["nome"],
            "empresa": contact.get("empresa") or "",
            "cargo": contact.get("cargo") or "",
            "hoje": datetime.now().strftime("%d/%m/%Y"),
            "dia_semana": datetime.now().strftime("%A")
        }

        if contact.get("aniversario"):
            variables["aniversario"] = contact["aniversario"].strftime("%d/%m")

        return self.render_template(template_id, variables)

    # =========================================================================
    # AI SUGGESTIONS
    # =========================================================================

    async def suggest_message(
        self,
        contact_id: int,
        contexto: str = None,
        canal: str = "whatsapp"
    ) -> Dict:
        """Sugere mensagem personalizada usando IA"""
        if not ANTHROPIC_API_KEY:
            return {"error": "ANTHROPIC_API_KEY not configured"}

        with get_db() as conn:
            cursor = conn.cursor()

            # Obter dados do contato
            cursor.execute("""
                SELECT nome, apelido, empresa, cargo, circulo,
                       ultimo_contato, resumo_ai, insights_ai
                FROM contacts
                WHERE id = %s
            """, (contact_id,))

            contact = cursor.fetchone()
            if not contact:
                return {"error": "Contato nao encontrado"}

            contact = dict(contact)

            # Obter ultimas mensagens
            cursor.execute("""
                SELECT direcao, conteudo, enviado_em
                FROM messages
                WHERE contact_id = %s
                ORDER BY enviado_em DESC
                LIMIT 5
            """, (contact_id,))

            messages = [dict(row) for row in cursor.fetchall()]

        # Construir prompt
        prompt = f"""Sugira uma mensagem de {canal} para enviar para este contato.

CONTATO:
- Nome: {contact['nome']}
- Apelido: {contact.get('apelido') or 'N/A'}
- Empresa: {contact.get('empresa') or 'N/A'}
- Cargo: {contact.get('cargo') or 'N/A'}
- Circulo: C{contact.get('circulo') or 5}
- Resumo: {contact.get('resumo_ai') or 'N/A'}

CONTEXTO SOLICITADO: {contexto or 'Reconexao geral'}

ULTIMAS MENSAGENS:
"""
        for msg in messages[:3]:
            direction = "EU" if msg["direcao"] == "outbound" else contact["nome"]
            content = (msg["conteudo"] or "")[:100]
            prompt += f"- {direction}: {content}\n"

        prompt += """
Gere uma mensagem curta, natural e personalizada em portugues brasileiro.
A mensagem deve ser adequada para WhatsApp (informal mas profissional).

Responda APENAS com a mensagem, sem explicacoes."""

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": prompt}]
                    },
                    timeout=30.0
                )

                if response.status_code == 200:
                    data = response.json()
                    mensagem = data["content"][0]["text"].strip()

                    return {
                        "contact_id": contact_id,
                        "contact_name": contact["nome"],
                        "canal": canal,
                        "contexto": contexto or "Reconexao geral",
                        "mensagem": mensagem,
                        "ai_generated": True
                    }
                else:
                    return {"error": f"Claude API error: {response.status_code}"}

        except Exception as e:
            return {"error": str(e)}

    # =========================================================================
    # DEFAULT TEMPLATES
    # =========================================================================

    def setup_default_templates(self) -> int:
        """Configura templates padrao"""
        defaults = [
            {
                "nome": "Reconexao Informal",
                "categoria": "reconnect",
                "canal": "whatsapp",
                "corpo": "Oi {nome}! Tudo bem? Faz tempo que a gente nao se fala. Como voce esta? Vamos marcar um cafe?"
            },
            {
                "nome": "Reconexao Profissional",
                "categoria": "reconnect",
                "canal": "email",
                "assunto": "Vamos nos reconectar?",
                "corpo": "Ola {nome},\n\nEspero que esteja bem! Faz um tempo que nao nos falamos e gostaria de saber como voce esta.\n\nPodemos marcar um cafe ou call para colocar o papo em dia?\n\nAbraco"
            },
            {
                "nome": "Parabens Aniversario",
                "categoria": "birthday",
                "canal": "whatsapp",
                "corpo": "Oi {nome}! Feliz aniversario! Que este novo ano seja repleto de realizacoes. Saude e sucesso!"
            },
            {
                "nome": "Follow-up Reuniao",
                "categoria": "followup",
                "canal": "email",
                "assunto": "Follow-up da nossa conversa",
                "corpo": "Ola {nome},\n\nFoi muito bom conversar com voce! Conforme combinamos, segue o que discutimos:\n\n[PONTOS]\n\nFico a disposicao para qualquer duvida.\n\nAbraco"
            },
            {
                "nome": "Agradecimento",
                "categoria": "thanks",
                "canal": "whatsapp",
                "corpo": "Oi {nome}! Queria agradecer muito pela ajuda com [ASSUNTO]. Fez toda diferenca! Valeu mesmo."
            },
            {
                "nome": "Indicacao",
                "categoria": "referral",
                "canal": "whatsapp",
                "corpo": "Oi {nome}! Tudo bem? Lembrei de voce porque conheci uma pessoa que pode te interessar. Posso te apresentar?"
            }
        ]

        created = 0
        for d in defaults:
            try:
                # Check if already exists
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id FROM message_templates WHERE nome = %s",
                        (d["nome"],)
                    )
                    if cursor.fetchone():
                        continue

                self.create_template(**d)
                created += 1
            except Exception as e:
                print(f"Error creating default template: {e}")

        return created

    def get_categories(self) -> List[Dict]:
        """Lista categorias disponiveis"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT categoria, COUNT(*) as count
                FROM message_templates
                WHERE ativo = TRUE
                GROUP BY categoria
                ORDER BY count DESC
            """)

            return [dict(row) for row in cursor.fetchall()]


_message_suggestions = None


def get_message_suggestions() -> MessageSuggestionsService:
    global _message_suggestions
    if _message_suggestions is None:
        _message_suggestions = MessageSuggestionsService()
    return _message_suggestions
