"""
Payment Cycle Service - Automacao de ciclo de pagamento mensal

Gera emails de cobranca, detecta respostas com comprovantes,
auto-fecha milestones e abre proximo ciclo.
"""
import json
import logging
from datetime import datetime, date
from typing import Dict, List, Optional
from database import get_db

logger = logging.getLogger(__name__)

MESES = ['', 'Janeiro', 'Fevereiro', 'Marco', 'Abril', 'Maio', 'Junho',
         'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']


def get_payment_cycle_config(project_id: int) -> Optional[Dict]:
    """Carrega config do ciclo de pagamento do metadata do projeto."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT metadata FROM projects WHERE id = %s", (project_id,))
        row = cursor.fetchone()
        if not row:
            return None
        metadata = row['metadata'] or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return metadata.get('payment_cycle')


def generate_payment_email(project_id: int, month: int, year: int,
                           expenses_override: List[Dict] = None) -> Dict:
    """
    Gera o conteudo do email de cobranca.

    Args:
        project_id: ID do projeto
        month: Mes (1-12)
        year: Ano
        expenses_override: Lista de despesas com valores editados

    Returns:
        {to, subject, body_html, expenses, total}
    """
    config = get_payment_cycle_config(project_id)
    if not config:
        return {"error": "Projeto nao tem payment_cycle configurado"}

    expenses = expenses_override or config.get('expenses', [])
    total = sum(e.get('valor', 0) for e in expenses)
    mes_nome = MESES[month]
    to = config['contact_email']
    subject = config.get('email_subject_template', 'Despesas {mes}/{ano}').format(
        mes=mes_nome, ano=year
    )

    # Gerar tabela HTML
    rows_html = ""
    for e in expenses:
        valor = e.get('valor', 0)
        if valor <= 0:
            continue
        pix_info = f"<br><small style='color:#6b7280;'>PIX: {e['pix']}</small>" if e.get('pix') else ""
        nota = f" <em style='color:#9ca3af;'>({e['nota']})</em>" if e.get('nota') else ""
        venc = f"Dia {e.get('vencimento_dia', '-')}"
        rows_html += f"""
        <tr>
            <td style="padding:10px 12px; border-bottom:1px solid #e5e7eb;">{e['nome']}{nota}</td>
            <td style="padding:10px 12px; border-bottom:1px solid #e5e7eb;">{venc}</td>
            <td style="padding:10px 12px; border-bottom:1px solid #e5e7eb; text-align:right; font-weight:600;">
                R$ {valor:,.2f}{pix_info}
            </td>
        </tr>"""

    body_html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: linear-gradient(135deg, #6366f1, #8b5cf6); padding: 24px; border-radius: 12px 12px 0 0;">
            <h2 style="color: white; margin: 0; font-size: 20px;">Despesas Pessoais - {mes_nome}/{year}</h2>
            <p style="color: rgba(255,255,255,0.8); margin: 8px 0 0 0; font-size: 14px;">Controle mensal de pagamentos</p>
        </div>

        <div style="background: #ffffff; border: 1px solid #e5e7eb; border-top: none; padding: 24px; border-radius: 0 0 12px 12px;">
            <p style="color: #374151; margin: 0 0 16px 0;">Oi {config.get('contact_name', 'Pai')}!</p>
            <p style="color: #374151; margin: 0 0 20px 0;">Seguem as despesas do mes de <strong>{mes_nome}/{year}</strong> para pagamento:</p>

            <table style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">
                <thead>
                    <tr style="background: #f9fafb;">
                        <th style="padding: 10px 12px; text-align: left; border-bottom: 2px solid #e5e7eb; color: #6b7280; font-size: 13px;">DESPESA</th>
                        <th style="padding: 10px 12px; text-align: left; border-bottom: 2px solid #e5e7eb; color: #6b7280; font-size: 13px;">VENCIMENTO</th>
                        <th style="padding: 10px 12px; text-align: right; border-bottom: 2px solid #e5e7eb; color: #6b7280; font-size: 13px;">VALOR</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
                <tfoot>
                    <tr style="background: #f0fdf4;">
                        <td colspan="2" style="padding: 12px; font-weight: 700; color: #166534; border-top: 2px solid #22c55e;">TOTAL</td>
                        <td style="padding: 12px; text-align: right; font-weight: 700; font-size: 18px; color: #166534; border-top: 2px solid #22c55e;">R$ {total:,.2f}</td>
                    </tr>
                </tfoot>
            </table>

            <p style="color: #6b7280; font-size: 13px; margin: 16px 0 0 0;">
                Por favor, me envie os comprovantes quando pagar.
            </p>
            <p style="color: #374151; margin: 16px 0 0 0;">Obrigado! Abraco,<br>Renato</p>
        </div>
    </div>
    """

    return {
        "to": to,
        "subject": subject,
        "body_html": body_html,
        "expenses": expenses,
        "total": total,
        "month": month,
        "year": year,
        "month_name": mes_nome
    }


async def send_payment_email(project_id: int, month: int, year: int,
                              expenses: List[Dict] = None) -> Dict:
    """
    Envia email de cobranca e cria/atualiza milestone.
    """
    from integrations.gmail import GmailIntegration
    from integrations.google_drive import get_valid_token
    from services.projects import add_milestone, update_milestone, add_project_note

    # Gerar email
    email_data = generate_payment_email(project_id, month, year, expenses)
    if email_data.get('error'):
        return email_data

    # Obter token
    with get_db() as conn:
        access_token = await get_valid_token(conn, 'professional')

    if not access_token:
        return {"error": "Token Gmail nao disponivel. Reconecte sua conta Google."}

    # Enviar
    gmail = GmailIntegration()
    try:
        result = await gmail.send_message(
            access_token=access_token,
            to=email_data['to'],
            subject=email_data['subject'],
            body=f"Despesas {email_data['month_name']}/{year} - Total R$ {email_data['total']:,.2f}",
            html_body=email_data['body_html']
        )
    except Exception as e:
        logger.error(f"Erro ao enviar email de cobranca: {e}")
        return {"error": f"Erro ao enviar email: {str(e)}"}

    thread_id = result.get('threadId', '')
    message_id = result.get('id', '')
    mes_nome = email_data['month_name']

    # Criar/atualizar milestone
    milestone_titulo = f"Pagamento {mes_nome}/{year}"

    with get_db() as conn:
        cursor = conn.cursor()
        # Verificar se ja existe
        cursor.execute("""
            SELECT id FROM project_milestones
            WHERE project_id = %s AND titulo = %s
        """, (project_id, milestone_titulo))
        existing = cursor.fetchone()

    if existing:
        update_milestone(existing['id'], {
            'email_thread_id': thread_id,
            'email_message_id': message_id,
            'status': 'pendente'
        })
        milestone_id = existing['id']
    else:
        milestone = add_milestone(project_id, {
            'titulo': milestone_titulo,
            'descricao': f"Email de cobranca enviado em {datetime.now().strftime('%d/%m/%Y')}. Total: R$ {email_data['total']:,.2f}",
            'data_prevista': date(year, month, 5).isoformat()
        })
        milestone_id = milestone['id']
        # Atualizar com thread_id (add_milestone nao aceita esses campos)
        update_milestone(milestone_id, {
            'email_thread_id': thread_id,
            'email_message_id': message_id
        })

    # Adicionar nota ao projeto
    try:
        add_project_note(project_id, {
            'conteudo': f"Email de cobranca {mes_nome}/{year} enviado para {email_data['to']}. Total: R$ {email_data['total']:,.2f}",
            'tipo': 'atividade'
        })
    except Exception:
        pass

    gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"

    return {
        "success": True,
        "thread_id": thread_id,
        "message_id": message_id,
        "milestone_id": milestone_id,
        "gmail_url": gmail_url,
        "total": email_data['total'],
        "month_name": mes_nome
    }


async def check_payment_replies(access_token: str) -> Dict:
    """
    Verifica se Orestes respondeu aos emails de cobranca pendentes.
    Chamado pelo cron daily-sync.
    """
    from integrations.gmail import GmailIntegration
    from services.projects import update_milestone, add_project_note

    results = {"checked": 0, "completed": 0, "cycles_created": 0}

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar projetos com payment_cycle ativo
        cursor.execute("""
            SELECT id, metadata FROM projects
            WHERE metadata->>'payment_cycle' IS NOT NULL
              AND status = 'ativo'
        """)
        projects = cursor.fetchall()

    gmail = GmailIntegration()

    for project in projects:
        metadata = project['metadata'] or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        config = metadata.get('payment_cycle', {})

        if not config.get('enabled') or not config.get('contact_email'):
            continue

        contact_email = config['contact_email'].lower()

        # Buscar milestones pendentes com email enviado
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, titulo, email_thread_id
                FROM project_milestones
                WHERE project_id = %s
                  AND status = 'pendente'
                  AND email_thread_id IS NOT NULL
            """, (project['id'],))
            milestones = cursor.fetchall()

        for milestone in milestones:
            results["checked"] += 1
            try:
                thread = await gmail.get_thread(access_token, milestone['email_thread_id'])
                messages = thread.get('messages', [])

                # Verificar se ha resposta do contato (nao do proprio Renato)
                has_reply = False
                for msg in messages[1:]:  # Ignorar primeira mensagem (enviada por Renato)
                    headers = gmail.parse_message_headers(msg)
                    from_email = (headers.get('from', '') or '').lower()
                    if contact_email in from_email:
                        has_reply = True
                        break

                if has_reply:
                    # Marcar como concluido
                    update_milestone(milestone['id'], {
                        'status': 'concluido',
                        'data_conclusao': date.today().isoformat()
                    })

                    # Nota no projeto
                    try:
                        add_project_note(project['id'], {
                            'conteudo': f"{milestone['titulo']} - Comprovante recebido de {config.get('contact_name', 'contato')}. Ciclo concluido automaticamente.",
                            'tipo': 'atividade'
                        })
                    except Exception:
                        pass

                    # Criar proximo ciclo
                    created = create_next_cycle(project['id'], milestone['titulo'])
                    if created:
                        results["cycles_created"] += 1

                    results["completed"] += 1

                    # Audit log (P3 gap critico — fechamento financeiro silencioso era invisivel)
                    try:
                        from services.agent_actions import log_action
                        log_action(
                            action_type='payment_cycle_completed',
                            category='email',
                            title=f"Ciclo financeiro fechado: {milestone['titulo']} ({config.get('contact_name', 'contato')})",
                            scope_ref={'project_id': project['id'], 'milestone_id': milestone['id'], 'next_milestone_id': created.get('id') if created else None},
                            source='payment_cycle.check_payment_replies',
                            payload={'thread_id': milestone['email_thread_id'], 'next_cycle_created': bool(created)},
                            undo_hint=f"UPDATE project_milestones SET status='pendente', data_conclusao=NULL WHERE id={milestone['id']};",
                        )
                    except Exception as e:
                        logger.warning(f"audit log failed for payment_cycle: {e}")

                    logger.info(f"Payment cycle completed: {milestone['titulo']} (project {project['id']})")

            except Exception as e:
                logger.warning(f"Erro ao verificar thread {milestone['email_thread_id']}: {e}")

    return results


def create_next_cycle(project_id: int, completed_titulo: str) -> Optional[Dict]:
    """
    Cria milestone para o proximo mes apos conclusao do ciclo atual.
    """
    from services.projects import add_milestone

    # Extrair mes/ano do titulo (formato: "Pagamento Maio/2026")
    try:
        parts = completed_titulo.replace("Pagamento ", "").split("/")
        mes_nome = parts[0].strip()
        ano = int(parts[1].strip())
        mes_idx = MESES.index(mes_nome)
    except (ValueError, IndexError):
        logger.warning(f"Nao foi possivel parsear titulo do milestone: {completed_titulo}")
        return None

    # Proximo mes
    next_month = mes_idx + 1
    next_year = ano
    if next_month > 12:
        next_month = 1
        next_year += 1

    next_titulo = f"Pagamento {MESES[next_month]}/{next_year}"

    # Verificar se ja existe
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM project_milestones
            WHERE project_id = %s AND titulo = %s
        """, (project_id, next_titulo))
        if cursor.fetchone():
            return None  # Ja existe

    # Criar
    milestone = add_milestone(project_id, {
        'titulo': next_titulo,
        'descricao': f"Ciclo de pagamento {MESES[next_month]}/{next_year}",
        'data_prevista': date(next_year, next_month, 5).isoformat()
    })

    logger.info(f"Next payment cycle created: {next_titulo} (project {project_id})")
    return milestone
