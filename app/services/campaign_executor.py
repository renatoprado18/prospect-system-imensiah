"""
Campaign Executor - Processamento de Steps de Campanhas

Responsável por:
1. Processar steps pendentes (next_action_at <= NOW)
2. Criar tarefas e sugestões baseadas no tipo de step
3. Avançar enrollments para o próximo step
4. Registrar execuções no histórico
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, List
import json
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


def get_db():
    """Get database connection using existing pattern."""
    from database import get_connection
    return get_connection()


class CampaignExecutor:
    """Executor de steps de campanhas."""

    # Mapeamento de tipos de step para handlers
    STEP_HANDLERS = {
        'linkedin_like': '_handle_linkedin_like',
        'linkedin_comment': '_handle_linkedin_comment',
        'linkedin_message': '_handle_linkedin_message',
        'whatsapp_message': '_handle_whatsapp_message',
        'email': '_handle_email',
        'call': '_handle_call',
        'meeting_invite': '_handle_meeting_invite',
        'wait': '_handle_wait',
        'check_response': '_handle_check_response',
        'task': '_handle_generic_task',
    }

    def process_pending_steps(self, limit: int = 50) -> Dict:
        """
        Processa todos os steps pendentes (next_action_at <= NOW).

        Chamado por um cron/scheduler periodicamente.

        Returns:
            Dict com estatísticas de processamento
        """
        conn = get_db()
        cursor = conn.cursor()

        try:
            # Buscar enrollments com ação pendente
            cursor.execute("""
                SELECT
                    e.id as enrollment_id,
                    e.contact_id,
                    e.current_step,
                    e.campaign_id,
                    c.nome as campaign_nome,
                    c.motivo_contato,
                    s.id as step_id,
                    s.tipo,
                    s.titulo,
                    s.descricao as step_descricao,
                    s.config,
                    s.condicao,
                    ct.nome as contact_nome,
                    ct.empresa as contact_empresa,
                    ct.cargo as contact_cargo,
                    ct.linkedin as linkedin_url,
                    ct.emails,
                    ct.telefones,
                    ct.health_score,
                    bl.slug as business_line
                FROM campaign_enrollments e
                JOIN campaigns c ON e.campaign_id = c.id
                JOIN campaign_steps s ON s.campaign_id = c.id AND s.ordem = e.current_step
                JOIN contacts ct ON e.contact_id = ct.id
                JOIN business_lines bl ON c.business_line_id = bl.id
                WHERE e.status = 'active'
                  AND c.status = 'active'
                  AND e.next_action_at <= NOW()
                  AND s.ativo = TRUE
                ORDER BY e.next_action_at ASC
                LIMIT %s
            """, (limit,))

            pending = cursor.fetchall()

            stats = {
                "processed": 0,
                "tasks_created": 0,
                "suggestions_created": 0,
                "completed": 0,
                "skipped": 0,
                "errors": 0
            }

            for enrollment_row in pending:
                enrollment = dict(enrollment_row)
                try:
                    result = self._execute_step(conn, cursor, enrollment)

                    stats["processed"] += 1
                    if result.get("task_created"):
                        stats["tasks_created"] += 1
                    if result.get("suggestion_created"):
                        stats["suggestions_created"] += 1
                    if result.get("completed"):
                        stats["completed"] += 1
                    if result.get("skipped"):
                        stats["skipped"] += 1

                except Exception as e:
                    logger.error(f"Erro ao processar enrollment {enrollment['enrollment_id']}: {e}")
                    stats["errors"] += 1
                    conn.rollback()

            conn.commit()
            logger.info(f"Processamento de campanhas concluído: {stats}")
            return stats

        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _execute_step(self, conn, cursor, enrollment: Dict) -> Dict:
        """Executa um step específico."""
        step_type = enrollment['tipo']
        handler_name = self.STEP_HANDLERS.get(step_type, '_handle_generic_task')
        handler = getattr(self, handler_name)

        # Verificar condição do step
        condicao = enrollment.get('condicao')
        if condicao:
            if isinstance(condicao, str):
                condicao = json.loads(condicao)
            if not self._check_condition(cursor, enrollment, condicao):
                # Skip este step
                self._advance_to_next_step(cursor, enrollment)
                return {"skipped": True, "reason": "Condição não atendida"}

        # Executar handler
        result = handler(cursor, enrollment)

        # Registrar execução
        self._record_execution(
            cursor,
            enrollment['enrollment_id'],
            enrollment['step_id'],
            result
        )

        # Audit log (P3): step executado sem aprovação do usuario.
        # Skip handlers que sao pura espera/check (nao mudam estado)
        if step_type not in ('wait', 'check_response') and not result.get('skipped'):
            try:
                from services.agent_actions import log_action
                log_action(
                    action_type='campaign_step_executed',
                    category='whatsapp' if step_type == 'whatsapp_message' else 'email' if step_type == 'email' else 'contacts',
                    title=f"Campanha '{enrollment.get('campaign_nome', '?')}': step '{step_type}' p/ {enrollment.get('contact_nome', '?')}",
                    scope_ref={
                        'enrollment_id': enrollment['enrollment_id'],
                        'step_id': enrollment['step_id'],
                        'contact_id': enrollment.get('contact_id'),
                        'task_id': result.get('task_id'),
                    },
                    source='campaign_executor.process_pending_steps',
                    payload={'step_type': step_type, 'result': {k: v for k, v in result.items() if k != 'mensagem'}},
                    undo_hint=f"DELETE FROM tasks WHERE id={result.get('task_id')};" if result.get('task_id') else None,
                )
            except Exception as e:
                logger.warning(f"audit log failed for campaign step: {e}")

        # Avançar para próximo step
        self._advance_to_next_step(cursor, enrollment)

        return result

    def execute_single_enrollment(self, enrollment_id: int) -> Dict:
        """Executa o próximo step de um enrollment específico."""
        conn = get_db()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT
                    e.id as enrollment_id,
                    e.contact_id,
                    e.current_step,
                    e.campaign_id,
                    c.nome as campaign_nome,
                    c.motivo_contato,
                    s.id as step_id,
                    s.tipo,
                    s.titulo,
                    s.descricao as step_descricao,
                    s.config,
                    s.condicao,
                    ct.nome as contact_nome,
                    ct.empresa as contact_empresa,
                    ct.cargo as contact_cargo,
                    ct.linkedin as linkedin_url,
                    ct.emails,
                    ct.telefones,
                    ct.health_score,
                    bl.slug as business_line
                FROM campaign_enrollments e
                JOIN campaigns c ON e.campaign_id = c.id
                JOIN campaign_steps s ON s.campaign_id = c.id AND s.ordem = e.current_step
                JOIN contacts ct ON e.contact_id = ct.id
                JOIN business_lines bl ON c.business_line_id = bl.id
                WHERE e.id = %s AND e.status = 'active'
            """, (enrollment_id,))

            row = cursor.fetchone()
            if not row:
                return {"success": False, "error": "Enrollment não encontrado ou inativo"}

            enrollment = dict(row)
            result = self._execute_step(conn, cursor, enrollment)
            conn.commit()
            return {"success": True, **result}

        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    # =========================================================================
    # STEP HANDLERS
    # =========================================================================

    # Stoplist de tokens de nome muito comuns/genericos. NAO inclui sobrenomes
    # discriminantes (silva/santos/souza/etc). So tokens que aparecem em mta
    # gente diferente — primeiros nomes ultra-comuns e particulas.
    _NAME_STOPWORDS = {
        "joao", "jose", "maria", "ana",
        "neto", "junior", "jr", "filho",
        "da", "de", "do", "das", "dos",
        "sao", "sr", "dr",
    }

    @staticmethod
    def _normalize_name(s: str) -> str:
        """Lower + strip accents."""
        if not s:
            return ""
        return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()

    def _name_tokens(self, s: str) -> List[str]:
        """Tokeniza nome: normaliza, troca nao-alfanumerico por espaco, dropa
        tokens curtos (<3) e tokens stoplist."""
        norm = self._normalize_name(s)
        norm = re.sub(r'[^a-z0-9]+', ' ', norm)
        tokens = [t for t in norm.split() if len(t) >= 3 and t not in self._NAME_STOPWORDS]
        return tokens

    def _names_match(self, expected: str, actual: str) -> bool:
        """Heuristica: nomes batem se interseccao dos tokens filtrados nao for
        vazia. Pra nomes muito curtos (sem tokens uteis), cai pro raw equality.
        """
        if not expected or not actual:
            return False

        exp_tokens = set(self._name_tokens(expected))
        act_tokens = set(self._name_tokens(actual))

        if exp_tokens and act_tokens:
            return bool(exp_tokens & act_tokens)

        # Fallback: raw normalized equality (whitespace-collapsed)
        exp_raw = ' '.join(self._normalize_name(expected).split())
        act_raw = ' '.join(self._normalize_name(actual).split())
        return bool(exp_raw) and exp_raw == act_raw

    def _save_linkedin_task_data(self, cursor, task_id: int, post: Dict) -> None:
        """Persiste post completo na sidecar linkedin_task_data pra UI render
        full-text + futuro AI assess. Upsert idempotente por task_id."""
        cursor.execute(
            """
            INSERT INTO linkedin_task_data
                (task_id, post_url, post_text, post_posted_at, post_engagements)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (task_id) DO UPDATE SET
                post_url = EXCLUDED.post_url,
                post_text = EXCLUDED.post_text,
                post_posted_at = EXCLUDED.post_posted_at,
                post_engagements = EXCLUDED.post_engagements,
                fetched_at = NOW()
            """,
            (
                task_id,
                post.get("url"),
                post.get("text") or "",
                post.get("posted_at"),
                json.dumps(post.get("engagements")) if post.get("engagements") is not None else None,
            ),
        )

    def _fetch_recent_post(self, linkedin_url: str, expected_name: Optional[str] = None) -> Optional[Dict]:
        """Fetch most recent LinkedIn post via LinkdAPI.

        Se expected_name for fornecido, valida que o nome no perfil retornado
        bate com o esperado antes de buscar posts. Defesa contra contacts.linkedin
        apontando pra perfil de outra pessoa (bug de sync do Google Contacts).
        """
        import os
        import httpx

        api_key = (os.getenv("LINKDAPI_KEY") or "").strip()
        if not api_key:
            return None

        # Extract username from URL
        username = linkedin_url.rstrip('/').split('/in/')[-1].split('/')[0].split('?')[0]
        if not username:
            return None

        try:
            # Step 1: Get URN from profile
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    "https://linkdapi.com/api/v1/profile/full",
                    headers={"X-linkdapi-apikey": api_key},
                    params={"username": username}
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                urn = (data.get("data") or {}).get("urn")
                if not urn:
                    return None

                # Name-match guard: se expected_name foi passado, valida que
                # o perfil retornado pertence a essa pessoa.
                first_name = (data.get("data") or {}).get("firstName") or ""
                last_name = (data.get("data") or {}).get("lastName") or ""
                actual_name = f"{first_name} {last_name}".strip()
                if expected_name and actual_name and not self._names_match(expected_name, actual_name):
                    logger.warning(
                        f"Name mismatch on LinkedIn URL {linkedin_url}: "
                        f"expected '{expected_name}', got '{actual_name}' — skipping post fetch"
                    )
                    return None

                # Step 2: Fetch recent posts
                resp2 = client.get(
                    "https://linkdapi.com/api/v1/posts/all",
                    headers={"X-linkdapi-apikey": api_key},
                    params={"urn": urn}
                )
                if resp2.status_code != 200:
                    return None
                posts_data = resp2.json()
                posts = (posts_data.get("data") or {}).get("posts") or []

                # Find most recent non-repost with text
                for post in posts[:10]:
                    header = post.get("header") or ""
                    if "reposted" in header.lower():
                        continue
                    text = post.get("text") or ""
                    if len(text) < 10:
                        continue
                    return {
                        "url": post.get("url"),
                        "text": text,
                        "posted_at": post.get("postedAt"),
                        "engagements": post.get("engagements"),
                    }

                # No original posts found
                return None

        except Exception as e:
            logger.warning(f"Error fetching LinkedIn posts for {username}: {e}")
            return None

    def _handle_linkedin_like(self, cursor, enrollment: Dict) -> Dict:
        """Cria tarefa para curtir post no LinkedIn com link direto."""
        linkedin_url = enrollment.get('linkedin_url') or ''

        if not linkedin_url:
            return {"skipped": True, "reason": "no_linkedin_url"}

        # Fetch actual recent post
        post = self._fetch_recent_post(linkedin_url, expected_name=enrollment.get('contact_nome'))

        if not post or not post.get("url"):
            # No posts found — pause enrollment, nothing to like
            cursor.execute("""
                UPDATE campaign_enrollments SET status = 'paused'
                WHERE id = %s
            """, (enrollment['enrollment_id'],))
            logger.info(f"Paused enrollment {enrollment['enrollment_id']} "
                        f"({enrollment['contact_nome']}): no LinkedIn posts found")
            return {"skipped": True, "reason": "no_posts_found"}

        post_preview = post["text"][:100] + ("..." if len(post["text"]) > 100 else "")

        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=f"LinkedIn: Curtir post de {enrollment['contact_nome']}",
            descricao=f"""Curta este post de {enrollment['contact_nome']}:

"{post_preview}"

🔗 Abrir post: {post['url']}

💡 Dica: Depois de curtir, prepare um comentário relevante para a próxima etapa.""",
            prioridade=5
        )
        self._save_linkedin_task_data(cursor, task_id, post)
        return {"task_created": True, "task_id": task_id, "post_url": post["url"]}

    def _handle_linkedin_comment(self, cursor, enrollment: Dict) -> Dict:
        """Cria tarefa para comentar no LinkedIn com link direto."""
        linkedin_url = enrollment.get('linkedin_url') or ''

        if not linkedin_url:
            return {"skipped": True, "reason": "no_linkedin_url"}

        config = enrollment.get('config', {})
        if isinstance(config, str):
            config = json.loads(config)

        # Fetch actual recent post
        post = self._fetch_recent_post(linkedin_url, expected_name=enrollment.get('contact_nome'))

        if not post or not post.get("url"):
            cursor.execute("""
                UPDATE campaign_enrollments SET status = 'paused'
                WHERE id = %s
            """, (enrollment['enrollment_id'],))
            return {"skipped": True, "reason": "no_posts_found"}

        post_preview = post["text"][:150] + ("..." if len(post["text"]) > 150 else "")

        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=f"LinkedIn: Comentar post de {enrollment['contact_nome']}",
            descricao=f"""Comente neste post de {enrollment['contact_nome']}:

"{post_preview}"

🔗 Abrir post: {post['url']}

💡 Sugestões:
- Adicione valor com uma perspectiva complementar
- Faça uma pergunta relevante
- Compartilhe uma experiência relacionada

⚠️ Evite comentários genéricos como "Ótimo post!" """,
            prioridade=6
        )
        self._save_linkedin_task_data(cursor, task_id, post)
        return {"task_created": True, "task_id": task_id, "post_url": post["url"]}

    def _handle_linkedin_message(self, cursor, enrollment: Dict) -> Dict:
        """Cria sugestão e tarefa para DM no LinkedIn."""
        config = enrollment.get('config', {})
        if isinstance(config, str):
            config = json.loads(config)

        template = config.get('template', '')
        mensagem = self._personalize_template(template, enrollment)

        # Criar sugestão AI
        suggestion_id = self._create_suggestion(
            cursor,
            enrollment,
            tipo='outreach',
            titulo=f"DM LinkedIn para {enrollment['contact_nome']}",
            conteudo=mensagem
        )

        # Criar tarefa
        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=f"LinkedIn: Enviar DM para {enrollment['contact_nome']}",
            descricao=f"""Envie uma mensagem direta para {enrollment['contact_nome']}.

📋 Campanha: {enrollment['campaign_nome']}
🎯 Motivo: {enrollment['motivo_contato'] or 'Iniciar conversa'}

🔗 LinkedIn: {enrollment['linkedin_url'] or 'Buscar no LinkedIn'}

📝 Mensagem sugerida:
{mensagem}

💡 Personalize conforme contexto recente (posts, notícias, conexões em comum).""",
            prioridade=7
        )

        return {
            "task_created": True,
            "task_id": task_id,
            "suggestion_created": bool(suggestion_id),
            "suggestion_id": suggestion_id
        }

    def _handle_whatsapp_message(self, cursor, enrollment: Dict) -> Dict:
        """Cria sugestão e tarefa para WhatsApp."""
        config = enrollment.get('config', {})
        if isinstance(config, str):
            config = json.loads(config)

        template = config.get('template', '')
        mensagem = self._personalize_template(template, enrollment)

        # Extrair telefone
        telefones = enrollment.get('telefones', [])
        if isinstance(telefones, str):
            telefones = json.loads(telefones)
        telefone = telefones[0].get('numero') if telefones else None

        suggestion_id = self._create_suggestion(
            cursor,
            enrollment,
            tipo='outreach',
            titulo=f"WhatsApp para {enrollment['contact_nome']}",
            conteudo=mensagem
        )

        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=f"WhatsApp: Mensagem para {enrollment['contact_nome']}",
            descricao=f"""Envie mensagem por WhatsApp para {enrollment['contact_nome']}.

📋 Campanha: {enrollment['campaign_nome']}
📱 Telefone: {telefone or 'Verificar no contato'}

📝 Mensagem sugerida:
{mensagem}""",
            prioridade=7
        )

        return {
            "task_created": True,
            "task_id": task_id,
            "suggestion_created": bool(suggestion_id),
            "suggestion_id": suggestion_id
        }

    def _handle_email(self, cursor, enrollment: Dict) -> Dict:
        """Cria sugestão e tarefa para email."""
        config = enrollment.get('config', {})
        if isinstance(config, str):
            config = json.loads(config)

        template = config.get('template', '')
        primeiro_nome = enrollment['contact_nome'].split()[0] if enrollment['contact_nome'] else ''
        assunto = config.get('assunto', f"Olá {primeiro_nome}")
        mensagem = self._personalize_template(template, enrollment)

        # Extrair email
        emails = enrollment.get('emails', [])
        if isinstance(emails, str):
            emails = json.loads(emails)
        email = emails[0].get('email') if emails else None

        suggestion_id = self._create_suggestion(
            cursor,
            enrollment,
            tipo='outreach',
            titulo=f"Email para {enrollment['contact_nome']}",
            conteudo=f"Assunto: {assunto}\n\n{mensagem}"
        )

        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=f"Email: Enviar para {enrollment['contact_nome']}",
            descricao=f"""Envie email para {enrollment['contact_nome']}.

📋 Campanha: {enrollment['campaign_nome']}
📧 Email: {email or 'Verificar no contato'}

📝 Assunto: {assunto}

📝 Mensagem sugerida:
{mensagem}""",
            prioridade=6
        )

        return {
            "task_created": True,
            "task_id": task_id,
            "suggestion_created": bool(suggestion_id),
            "suggestion_id": suggestion_id
        }

    def _handle_call(self, cursor, enrollment: Dict) -> Dict:
        """Cria tarefa para ligação."""
        config = enrollment.get('config', {})
        if isinstance(config, str):
            config = json.loads(config)

        objetivo = config.get('objetivo', enrollment['motivo_contato'] or 'Retomar contato')

        # Extrair telefone
        telefones = enrollment.get('telefones', [])
        if isinstance(telefones, str):
            telefones = json.loads(telefones)
        telefone = telefones[0].get('numero') if telefones else None

        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=f"Ligar para {enrollment['contact_nome']}",
            descricao=f"""Faça uma ligação para {enrollment['contact_nome']}.

📋 Campanha: {enrollment['campaign_nome']}
📱 Telefone: {telefone or 'Verificar no contato'}
🏢 {enrollment['contact_cargo'] or ''} na {enrollment['contact_empresa'] or ''}

🎯 Objetivo: {objetivo}

💡 Tópicos para a conversa:
- Retomar relacionamento
- {enrollment['motivo_contato'] or 'Compartilhar novidades relevantes'}
- Agendar próximo passo (café/reunião)""",
            prioridade=8
        )
        return {"task_created": True, "task_id": task_id}

    def _handle_meeting_invite(self, cursor, enrollment: Dict) -> Dict:
        """Cria tarefa para convite de reunião/café."""
        config = enrollment.get('config', {})
        if isinstance(config, str):
            config = json.loads(config)

        assunto = config.get('assunto', f"Café com {enrollment['contact_nome']}")
        local = config.get('local', 'A definir')

        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=f"Convidar {enrollment['contact_nome']} para café/reunião",
            descricao=f"""Convide {enrollment['contact_nome']} para um café ou reunião.

📋 Campanha: {enrollment['campaign_nome']}
🎯 Assunto: {assunto}
📍 Local sugerido: {local}

🏢 {enrollment['contact_cargo'] or ''} na {enrollment['contact_empresa'] or ''}

💡 Sugestões:
- Proponha 2-3 horários flexíveis
- Ofereça opção virtual se necessário
- Seja específico sobre o que quer discutir

⚡ Este é o step de conversão da campanha!""",
            prioridade=9
        )
        return {"task_created": True, "task_id": task_id}

    def _handle_wait(self, cursor, enrollment: Dict) -> Dict:
        """Step de espera - apenas avança o tempo."""
        return {"skipped": True, "reason": "Wait step"}

    def _handle_check_response(self, cursor, enrollment: Dict) -> Dict:
        """
        Verifica se houve resposta/interação recente.
        Se houve, pausa a campanha para esse contato.
        """
        # Verificar último contato
        cursor.execute("""
            SELECT ultimo_contato FROM contacts WHERE id = %s
        """, (enrollment['contact_id'],))
        row = cursor.fetchone()
        last_contact = row['ultimo_contato'] if row else None

        cursor.execute("""
            SELECT enrolled_at FROM campaign_enrollments WHERE id = %s
        """, (enrollment['enrollment_id'],))
        row = cursor.fetchone()
        enrolled_at = row['enrolled_at'] if row else None

        # Se houve contato após enrollment, considerar como resposta
        if last_contact and enrolled_at and last_contact > enrolled_at:
            # Pausar para follow-up manual
            cursor.execute("""
                UPDATE campaign_enrollments
                SET status = 'paused',
                    paused_at = NOW(),
                    conversion_notes = 'Resposta detectada - verificar manualmente'
                WHERE id = %s
            """, (enrollment['enrollment_id'],))

            return {"paused": True, "reason": "Resposta detectada"}

        # Se não houve resposta, criar tarefa de follow-up
        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=f"Verificar resposta de {enrollment['contact_nome']}",
            descricao=f"""Verifique se {enrollment['contact_nome']} respondeu às interações anteriores.

📋 Campanha: {enrollment['campaign_nome']}

✅ Se respondeu positivamente:
   → Marque como convertido no sistema

❌ Se não respondeu:
   → A campanha continuará automaticamente""",
            prioridade=5
        )

        return {"task_created": True, "task_id": task_id}

    def _handle_generic_task(self, cursor, enrollment: Dict) -> Dict:
        """Handler genérico para tipos não específicos."""
        config = enrollment.get('config', {})
        if isinstance(config, str):
            config = json.loads(config)

        task_id = self._create_task(
            cursor,
            enrollment,
            titulo=enrollment['titulo'] or f"Tarefa: {enrollment['contact_nome']}",
            descricao=enrollment['step_descricao'] or f"""Tarefa de campanha para {enrollment['contact_nome']}.

📋 Campanha: {enrollment['campaign_nome']}
🎯 Motivo: {enrollment['motivo_contato'] or 'Avançar relacionamento'}""",
            prioridade=config.get('prioridade', 5)
        )
        return {"task_created": True, "task_id": task_id}

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _create_task(
        self,
        cursor,
        enrollment: Dict,
        titulo: str,
        descricao: str,
        prioridade: int = 5
    ) -> Optional[int]:
        """Cria uma tarefa no sistema existente."""
        try:
            cursor.execute("""
                INSERT INTO tasks (
                    contact_id, titulo, descricao, prioridade,
                    status, origem, data_criacao
                ) VALUES (%s, %s, %s, %s, 'pending', 'campaign', NOW())
                RETURNING id
            """, (enrollment['contact_id'], titulo, descricao, prioridade))
            row = cursor.fetchone()
            return row['id'] if row else None
        except Exception as e:
            logger.error(f"Erro ao criar tarefa: {e}")
            return None

    def _create_suggestion(
        self,
        cursor,
        enrollment: Dict,
        tipo: str,
        titulo: str,
        conteudo: str
    ) -> Optional[int]:
        """Cria uma sugestão AI no sistema existente."""
        try:
            cursor.execute("""
                INSERT INTO ai_suggestions (
                    contact_id, tipo, titulo, descricao,
                    status, criado_em
                ) VALUES (%s, %s, %s, %s, 'pending', NOW())
                RETURNING id
            """, (enrollment['contact_id'], tipo, titulo, conteudo))
            row = cursor.fetchone()
            return row['id'] if row else None
        except Exception as e:
            logger.error(f"Erro ao criar sugestão: {e}")
            return None

    def _record_execution(
        self,
        cursor,
        enrollment_id: int,
        step_id: int,
        result: Dict
    ) -> int:
        """Registra execução de step no histórico."""
        resultado = 'done'
        if result.get('skipped'):
            resultado = 'skipped'
        elif result.get('paused'):
            resultado = 'paused'

        cursor.execute("""
            INSERT INTO campaign_step_executions (
                enrollment_id, step_id, executed_at,
                resultado, notas, suggestion_id, task_id
            ) VALUES (%s, %s, NOW(), %s, %s, %s, %s)
            RETURNING id
        """, (
            enrollment_id,
            step_id,
            resultado,
            result.get('reason'),
            result.get('suggestion_id'),
            result.get('task_id')
        ))
        row = cursor.fetchone()
        return row['id'] if row else None

    def _advance_to_next_step(self, cursor, enrollment: Dict) -> None:
        """Avança enrollment para o próximo step."""
        current_step = enrollment['current_step']
        campaign_id = enrollment['campaign_id']

        # Buscar próximo step
        cursor.execute("""
            SELECT ordem, delay_dias FROM campaign_steps
            WHERE campaign_id = %s AND ordem > %s AND ativo = TRUE
            ORDER BY ordem
            LIMIT 1
        """, (campaign_id, current_step))
        next_step = cursor.fetchone()

        if next_step:
            # Avançar para próximo step
            next_action = datetime.now() + timedelta(days=next_step['delay_dias'])
            cursor.execute("""
                UPDATE campaign_enrollments
                SET current_step = %s, next_action_at = %s
                WHERE id = %s
            """, (next_step['ordem'], next_action, enrollment['enrollment_id']))
        else:
            # Campanha concluída para este contato
            cursor.execute("""
                UPDATE campaign_enrollments
                SET status = 'completed', completed_at = NOW()
                WHERE id = %s
            """, (enrollment['enrollment_id'],))

            # Atualizar contador da campanha
            cursor.execute("""
                UPDATE campaigns
                SET total_completed = total_completed + 1
                WHERE id = %s
            """, (campaign_id,))

    def _check_condition(
        self,
        cursor,
        enrollment: Dict,
        condicao: Dict
    ) -> bool:
        """Verifica se condição do step é atendida."""
        # health_min
        if 'health_min' in condicao:
            if enrollment.get('health_score', 0) < condicao['health_min']:
                return False

        # health_max
        if 'health_max' in condicao:
            if enrollment.get('health_score', 100) > condicao['health_max']:
                return False

        # sem_resposta
        if condicao.get('sem_resposta'):
            cursor.execute("""
                SELECT ultimo_contato FROM contacts WHERE id = %s
            """, (enrollment['contact_id'],))
            row = cursor.fetchone()
            last_contact = row['ultimo_contato'] if row else None

            cursor.execute("""
                SELECT enrolled_at FROM campaign_enrollments WHERE id = %s
            """, (enrollment['enrollment_id'],))
            row = cursor.fetchone()
            enrolled_at = row['enrolled_at'] if row else None

            if last_contact and enrolled_at and last_contact > enrolled_at:
                return False  # Houve resposta, pular step

        return True

    def _personalize_template(self, template: str, enrollment: Dict) -> str:
        """Personaliza template com dados do contato."""
        if not template:
            return ""

        nome = enrollment.get('contact_nome', '')
        primeiro_nome = nome.split()[0] if nome else ''

        replacements = {
            '{nome}': nome,
            '{primeiro_nome}': primeiro_nome,
            '{empresa}': enrollment.get('contact_empresa', '') or '',
            '{cargo}': enrollment.get('contact_cargo', '') or '',
            '{motivo}': enrollment.get('motivo_contato', '') or '',
        }

        result = template
        for key, value in replacements.items():
            result = result.replace(key, value)

        return result
