"""
Smart Follow-Up Service

Detecta emails enviados sem resposta e cria lembretes automaticos de follow-up.

Filtro de relevancia:
- Apenas emails que pedem acao (reuniao, proposta, pergunta)
- Ignora: agradecimentos, respostas curtas, newsletters, notificacoes

Fluxo:
1. Busca emails ENVIADOS nos ultimos 7 dias
2. Para cada email, verifica se houve resposta
3. Se nao houve resposta E o email e relevante → cria FUP action proposal
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)

# Dias sem resposta para cada nivel de urgencia
FUP_RULES = {
    'high': 3,     # Reuniao agendada, proposta enviada
    'medium': 5,   # Pedido de agendamento, introdução
    'low': 7,      # Networking geral
}


async def check_pending_fups(access_token: str) -> Dict:
    """
    Verifica emails enviados sem resposta e cria FUP proposals.
    Chamado pelo cron daily-sync.

    Returns:
        {checked: int, fups_created: int, details: [...]}
    """
    from integrations.gmail import GmailIntegration

    gmail = GmailIntegration()
    results = {"checked": 0, "fups_created": 0, "details": []}
    api_key = os.getenv("ANTHROPIC_API_KEY")

    try:
        # Buscar emails ENVIADOS nos ultimos 7 dias sem resposta
        # Gmail query: emails enviados, nao em thread com resposta
        sent_messages = await gmail.list_messages(
            access_token=access_token,
            query="in:sent newer_than:7d",
            max_results=30
        )

        if not sent_messages:
            return results

        # Agrupar por thread (so pegar o ultimo email por thread)
        threads_checked = set()

        for msg_summary in sent_messages:
            msg_id = msg_summary.get('id')
            thread_id = msg_summary.get('threadId')

            if thread_id in threads_checked:
                continue
            threads_checked.add(thread_id)
            results["checked"] += 1

            try:
                # Buscar thread completa
                thread = await gmail.get_thread(access_token, thread_id)
                messages = thread.get('messages', [])

                if not messages:
                    continue

                # Encontrar o ultimo email enviado por Renato nesta thread
                last_sent = None
                last_sent_date = None
                has_reply_after = False

                for m in messages:
                    headers = gmail.parse_message_headers(m)
                    from_email = (headers.get('from', '') or '').lower()
                    msg_date = headers.get('date', '')

                    is_from_me = 'renato@almeida-prado.com' in from_email or 'renatodaprado@gmail.com' in from_email

                    if is_from_me:
                        last_sent = m
                        last_sent_date = msg_date
                        has_reply_after = False  # Reset: procuramos resposta APOS este envio
                    elif last_sent:
                        has_reply_after = True  # Alguem respondeu depois do meu ultimo envio

                # Se ja houve resposta, pular
                if has_reply_after or not last_sent:
                    continue

                # Calcular dias sem resposta
                headers = gmail.parse_message_headers(last_sent)
                sent_date_str = headers.get('date', '')
                try:
                    from email.utils import parsedate_to_datetime
                    sent_date = parsedate_to_datetime(sent_date_str)
                    days_waiting = (datetime.now(sent_date.tzinfo) - sent_date).days if sent_date.tzinfo else (datetime.now() - sent_date).days
                except Exception:
                    days_waiting = 3  # fallback

                if days_waiting < 2:
                    continue  # Muito cedo para FUP

                # Extrair destinatario e assunto
                to_email = (headers.get('to', '') or '').lower()
                subject = headers.get('subject', 'Sem assunto')
                body = gmail.parse_message_body(last_sent)
                body_text = (body.get('text', '') or '')[:500]

                # Verificar se ja existe FUP proposal para este thread
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT id FROM action_proposals
                        WHERE trigger_text LIKE %s AND status = 'pending'
                    """, (f"%{thread_id}%",))
                    if cursor.fetchone():
                        continue  # Ja existe FUP para este thread

                # Classificar relevancia com IA
                if api_key and body_text:
                    relevance = await _classify_email_relevance(api_key, subject, body_text, to_email)
                else:
                    relevance = _classify_email_relevance_keywords(subject, body_text)

                if not relevance or relevance.get('needs_fup') == False:
                    continue  # Email nao relevante para FUP

                # Encontrar contato no sistema
                contact_id = None
                contact_name = relevance.get('recipient_name', '')

                # Extrair email limpo do "to"
                import re
                email_match = re.search(r'[\w.-]+@[\w.-]+', to_email)
                recipient_email = email_match.group(0) if email_match else to_email

                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, nome FROM contacts WHERE emails::text ILIKE %s LIMIT 1",
                        (f"%{recipient_email}%",)
                    )
                    contact = cursor.fetchone()
                    if contact:
                        contact_id = contact['id']
                        contact_name = contact['nome']

                # Criar FUP action proposal
                urgency = relevance.get('urgency', 'medium')
                fup_days = FUP_RULES.get(urgency, 5)

                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO action_proposals (
                            contact_id, action_type, title, description, trigger_text,
                            ai_reasoning, confidence, urgency, status, options, expires_at
                        ) VALUES (%s, 'follow_up_alert', %s, %s, %s, %s, %s, %s, 'pending', %s, NOW() + INTERVAL '%s days')
                    """, (
                        contact_id,
                        f"Follow-up: {contact_name or recipient_email}",
                        f"{relevance.get('reason', 'Email sem resposta')} | Assunto: {subject[:80]} | Enviado ha {days_waiting} dias",
                        f"thread:{thread_id} | {body_text[:150]}",
                        f"FUP inteligente: {relevance.get('reason', 'email sem resposta ha ' + str(days_waiting) + ' dias')}",
                        relevance.get('confidence', 0.7),
                        urgency,
                        json.dumps([
                            {'id': 'respond_now', 'label': 'Dar follow-up', 'action': 'open_conversation'},
                            {'id': 'create_task', 'label': 'Criar tarefa', 'action': 'create_task'},
                            {'id': 'dismiss', 'label': 'Ignorar', 'action': 'dismiss'}
                        ]),
                        fup_days + 3  # Expira alguns dias apos o lembrete
                    ))
                    conn.commit()

                results["fups_created"] += 1
                results["details"].append({
                    "to": recipient_email,
                    "subject": subject[:50],
                    "days_waiting": days_waiting,
                    "urgency": urgency,
                    "reason": relevance.get('reason', '')
                })

            except Exception as e:
                logger.warning(f"Erro ao processar thread {thread_id}: {e}")

    except Exception as e:
        logger.error(f"Erro no check_pending_fups: {e}")
        results["error"] = str(e)

    return results


async def _classify_email_relevance(api_key: str, subject: str, body: str, to_email: str) -> Dict:
    """
    Usa IA para classificar se o email enviado precisa de follow-up.
    """
    prompt = f"""Analise este email ENVIADO e determine se precisa de follow-up caso nao receba resposta.

ASSUNTO: {subject}
PARA: {to_email}
CORPO: {body[:400]}

Retorne APENAS JSON:
{{
  "needs_fup": true/false,
  "urgency": "high" / "medium" / "low",
  "reason": "motivo curto em portugues",
  "confidence": 0.0-1.0,
  "recipient_name": "nome do destinatario extraido do email"
}}

Regras:
- needs_fup=true: pedidos de reuniao, propostas comerciais, perguntas que esperam resposta, pedidos de informacao
- needs_fup=false: agradecimentos, confirmacoes simples, respostas que encerram conversa, emails internos/notificacoes
- urgency=high: reuniao agendada, proposta com prazo, cobranca
- urgency=medium: pedido de agendamento, introducao profissional
- urgency=low: networking casual, convite sem prazo"""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

        if response.status_code != 200:
            return _classify_email_relevance_keywords(subject, body)

        text = response.json().get("content", [{}])[0].get("text", "")
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])

    except Exception as e:
        logger.warning(f"Erro na classificacao IA: {e}")

    return _classify_email_relevance_keywords(subject, body)


def _classify_email_relevance_keywords(subject: str, body: str) -> Dict:
    """
    Fallback: classifica por keywords quando IA nao disponivel.
    """
    text = f"{subject} {body}".lower()

    # Emails que NAO precisam de FUP
    no_fup_signals = ['obrigado', 'agradeco', 'valeu', 'confirmado', 'recebido',
                       'ok!', 'perfeito', 'entendido', 'ciente', 'newsletter']
    if any(signal in text for signal in no_fup_signals) and len(body) < 200:
        return {"needs_fup": False}

    # Emails que PRECISAM de FUP (alta urgencia)
    high_signals = ['reuniao', 'reunião', 'agendar', 'proposta', 'convite',
                    'orcamento', 'orçamento', 'prazo', 'vencimento']
    if any(s in text for s in high_signals):
        return {"needs_fup": True, "urgency": "high", "reason": "Pedido de reuniao/proposta sem resposta",
                "confidence": 0.8, "recipient_name": ""}

    # Media urgencia
    medium_signals = ['disponibilidade', 'podemos conversar', 'gostaria de',
                      'marcar', 'indicacao', 'indicação', 'apresentar']
    if any(s in text for s in medium_signals):
        return {"needs_fup": True, "urgency": "medium", "reason": "Solicitacao pendente de resposta",
                "confidence": 0.7, "recipient_name": ""}

    # Perguntas (tem ?)
    if '?' in body:
        return {"needs_fup": True, "urgency": "low", "reason": "Pergunta sem resposta",
                "confidence": 0.6, "recipient_name": ""}

    return {"needs_fup": False}
