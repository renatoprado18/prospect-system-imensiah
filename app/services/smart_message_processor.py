"""
Smart Message Processor - Deteccao inteligente de conteudo acionavel

Analisa mensagens WhatsApp recebidas para detectar:
- Emails mencionados (com analise de contexto para atribuir ao contato correto)
- Propostas de reuniao / datas
- Numeros de telefone

Auto-executa acoes claras e cria propostas para casos ambiguos.
Notifica Renato via intel-bot para todas as propostas criadas.
"""
import os
import re
import logging
import httpx
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from database import get_db

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
OWNER_CONTACT_ID = 14911

# ==================== REGEX PATTERNS ====================

EMAIL_PATTERN = re.compile(
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE
)

PHONE_PATTERN = re.compile(
    r'(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\s?)?\d{4}[\s-]?\d{4}'
)

# Padroes de mencao a reuniao/data
MEETING_PATTERNS = [
    re.compile(r'pode ser (?:às?|as) (\d{1,2}(?:[:h]\d{2})?)', re.IGNORECASE),
    re.compile(r'(?:dia|no dia) (\d{1,2}(?:/\d{1,2})?)', re.IGNORECASE),
    re.compile(r'(\d{1,2}(?:[:h]\d{2})?)\s*(?:horas?|hrs?)', re.IGNORECASE),
    re.compile(r'(segunda|terca|terça|quarta|quinta|sexta|sabado|sábado|domingo)(?:\s+(?:que vem|feira))?', re.IGNORECASE),
    re.compile(r'(amanha|amanhã)', re.IGNORECASE),
    re.compile(r'semana que vem', re.IGNORECASE),
    re.compile(r'(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(?:às?|as)\s+(\d{1,2}(?:[:h]\d{2})?)', re.IGNORECASE),
]

# Palavras que indicam pedido de email em mensagens anteriores
EMAIL_REQUEST_KEYWORDS = ['email', 'e-mail', 'contato', 'endereço', 'endereco', 'mail']

# Emails a ignorar (assinaturas, dominios comuns em footers)
IGNORE_EMAIL_DOMAINS = ['example.com', 'teste.com', 'test.com']


# ==================== MAIN ENTRY POINT ====================

async def process_message_intelligence(
    message_id: int,
    contact_id: int,
    content: str,
    direction: str
):
    """
    Post-processing para mensagens recebidas — detecta conteudo acionavel.

    Args:
        message_id: ID da mensagem no banco
        contact_id: ID do contato remetente
        content: Texto da mensagem
        direction: 'incoming' ou 'outgoing'
    """
    # Apenas mensagens recebidas, nunca do proprio Renato
    if direction != "incoming":
        return
    if contact_id == OWNER_CONTACT_ID:
        return
    if not content or len(content.strip()) < 3:
        return

    # Filtrar mensagens automaticas/sistema (portaria, bancos, OTP, erros tecnicos)
    # Why: feedback 2026-04-25 — mensagens automaticas viraram "pedidos de indicacao" reais
    from services.message_filters import is_automated_message
    is_auto, reason = is_automated_message(content, contact_id)
    if is_auto:
        logger.info(f"Skipping smart processor for msg {message_id} (contact {contact_id}): {reason}")
        return

    results = []

    try:
        # 1. Detectar emails
        emails = extract_emails(content)
        if emails:
            email_results = await handle_detected_emails(
                emails, contact_id, message_id, content
            )
            results.extend(email_results)

        # 2. Detectar propostas de reuniao
        meeting_info = detect_meeting_proposal(content)
        if meeting_info:
            meeting_results = await handle_meeting_proposal(
                meeting_info, contact_id, message_id, content
            )
            results.extend(meeting_results)

        # 3. Detectar telefones
        phones = extract_phones(content)
        if phones:
            phone_results = await handle_detected_phones(
                phones, contact_id, message_id, content
            )
            results.extend(phone_results)

        # 4. Notificar Renato para cada proposta criada
        for result in results:
            if result.get('proposal'):
                await notify_renato_proposal(result['proposal'])
            elif result.get('notification'):
                await notify_renato_text(result['notification'])

    except Exception as e:
        logger.error(f"Error in smart message processor for msg {message_id}: {e}", exc_info=True)


# ==================== EMAIL DETECTION ====================

def extract_emails(content: str) -> List[str]:
    """Extrai emails de um texto, filtrando falsos positivos."""
    emails = EMAIL_PATTERN.findall(content)
    # Filtrar dominios ignorados
    filtered = []
    for email in emails:
        domain = email.split('@')[1].lower()
        if domain not in IGNORE_EMAIL_DOMAINS:
            filtered.append(email.lower())
    return filtered


async def handle_detected_emails(
    emails: List[str],
    contact_id: int,
    message_id: int,
    content: str
) -> List[Dict]:
    """Processa emails detectados na mensagem."""
    results = []

    for email in emails:
        # Verificar se o email ja existe em algum contato
        existing_contact = _find_contact_by_email(email)
        if existing_contact:
            logger.info(f"Email {email} ja existe no contato {existing_contact['nome']} (id={existing_contact['id']})")
            continue

        # Buscar contexto: ultimas mensagens enviadas para este contato
        context = _get_outgoing_context(contact_id, limit=3)
        sender_name = _get_contact_name(contact_id)

        # Verificar se alguma mensagem anterior pedia um email
        asked_for_email = _context_asked_for_email(context)

        if asked_for_email:
            # Caso claro: Renato pediu o email e alguem respondeu
            owner_name = await _identify_email_owner(context, email, content)

            if owner_name:
                # Auto-executar: encontrar ou criar contato e atualizar email
                result = await _auto_update_email(owner_name, email, contact_id, message_id)
                results.append(result)
            else:
                # Nao conseguiu identificar o dono — criar proposta
                result = _create_email_proposal(
                    email, sender_name, contact_id, message_id, content,
                    ambiguous=True
                )
                results.append(result)
        else:
            # Email apareceu sem contexto de pedido — criar proposta
            result = _create_email_proposal(
                email, sender_name, contact_id, message_id, content,
                ambiguous=False
            )
            results.append(result)

    return results


def _find_contact_by_email(email: str) -> Optional[Dict]:
    """Verifica se um email ja existe em algum contato."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, email FROM contacts
                WHERE LOWER(email) = LOWER(%s)
                LIMIT 1
            """, (email,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error checking email {email}: {e}")
        return None


def _get_outgoing_context(contact_id: int, limit: int = 3) -> List[Dict]:
    """Busca ultimas mensagens enviadas (outgoing) para este contato."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.content, m.criado_em
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.contact_id = %s
                  AND m.direction = 'outgoing'
                  AND m.content IS NOT NULL
                  AND m.criado_em > NOW() - INTERVAL '48 hours'
                ORDER BY m.criado_em DESC
                LIMIT %s
            """, (contact_id, limit))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error getting outgoing context for contact {contact_id}: {e}")
        return []


def _get_contact_name(contact_id: int) -> str:
    """Busca nome do contato."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT nome FROM contacts WHERE id = %s", (contact_id,))
            row = cursor.fetchone()
            return row['nome'] if row else "Contato desconhecido"
    except Exception:
        return "Contato desconhecido"


def _context_asked_for_email(context: List[Dict]) -> bool:
    """Verifica se alguma mensagem de contexto pedia um email."""
    for msg in context:
        text = (msg.get('content') or '').lower()
        if any(kw in text for kw in EMAIL_REQUEST_KEYWORDS):
            return True
    return False


async def _identify_email_owner(
    context: List[Dict],
    email: str,
    response_content: str
) -> Optional[str]:
    """Usa Claude para identificar de quem e o email baseado no contexto."""
    if not ANTHROPIC_API_KEY:
        return None

    # Montar contexto das mensagens anteriores
    context_text = "\n".join([
        f"Renato escreveu: {msg.get('content', '')}"
        for msg in context[:3]
    ])

    prompt = (
        f"Contexto de conversa WhatsApp:\n"
        f"{context_text}\n\n"
        f"Resposta recebida: {response_content}\n\n"
        f"O email detectado na resposta e: {email}\n\n"
        f"De quem e este email? Responda APENAS o nome da pessoa (primeiro nome e sobrenome se possivel). "
        f"Se nao for possivel determinar, responda 'INDEFINIDO'."
    )

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
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=15.0
            )
            if response.status_code == 200:
                data = response.json()
                name = data["content"][0]["text"].strip()
                if name and name.upper() != "INDEFINIDO":
                    return name
    except Exception as e:
        logger.error(f"Claude API error identifying email owner: {e}")

    return None


async def _auto_update_email(
    owner_name: str,
    email: str,
    sender_contact_id: int,
    message_id: int
) -> Dict:
    """
    Auto-executa atualizacao de email.
    Tenta encontrar contato pelo nome; se nao achar, cria proposta.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar contato pelo nome (busca fuzzy)
            cursor.execute("""
                SELECT id, nome, email FROM contacts
                WHERE LOWER(nome) ILIKE %s
                ORDER BY
                    CASE WHEN LOWER(nome) = LOWER(%s) THEN 0 ELSE 1 END,
                    total_interacoes DESC NULLS LAST
                LIMIT 1
            """, (f"%{owner_name.lower()}%", owner_name.lower()))
            contact = cursor.fetchone()

            if contact:
                contact = dict(contact)
                old_email = contact.get('email')

                # Atualizar email
                cursor.execute("""
                    UPDATE contacts SET email = %s WHERE id = %s
                """, (email, contact['id']))
                conn.commit()

                logger.info(
                    f"Auto-updated email for {contact['nome']} (id={contact['id']}): "
                    f"{old_email} -> {email}"
                )

                notification_text = (
                    f"📧 Email atualizado: {contact['nome']} → {email}"
                )
                if old_email:
                    notification_text += f"\n(anterior: {old_email})"

                return {'notification': notification_text}

            else:
                # Contato nao encontrado — criar proposta
                sender_name = _get_contact_name(sender_contact_id)
                return _create_email_proposal(
                    email, sender_name, sender_contact_id, message_id,
                    f"Email de {owner_name}: {email}",
                    ambiguous=True,
                    suggested_owner=owner_name
                )

    except Exception as e:
        logger.error(f"Error auto-updating email {email} for {owner_name}: {e}")
        return {}


def _create_email_proposal(
    email: str,
    sender_name: str,
    contact_id: int,
    message_id: int,
    content: str,
    ambiguous: bool = False,
    suggested_owner: str = None
) -> Dict:
    """Cria proposta de acao para email detectado."""
    from services.action_proposals import get_action_proposals

    if suggested_owner:
        title = f"Email detectado: {email} (possivelmente de {suggested_owner})"
    else:
        title = f"Email detectado: {email}"

    description = (
        f"Na conversa com {sender_name}, foi mencionado o email {email}. "
    )
    if ambiguous:
        description += "Nao foi possivel determinar automaticamente a quem pertence."
    else:
        description += "Verificar a quem pertence e atualizar o contato."

    proposal_data = {
        'action_type': 'update_contact_email',
        'contact_id': contact_id,
        'message_id': message_id,
        'title': title,
        'description': description,
        'trigger_text': content[:300],
        'ai_reasoning': f"Email {email} detectado na mensagem. Contexto: conversa com {sender_name}.",
        'confidence': 0.9 if not ambiguous else 0.6,
        'urgency': 'medium',
        'action_params': {
            'email': email,
            'sender_contact_id': contact_id,
            'suggested_owner': suggested_owner,
        },
        'options': [
            {'id': 'update_sender', 'label': f'Atualizar {sender_name}'},
            {'id': 'create_new', 'label': 'Criar novo contato'},
            {'id': 'ignore', 'label': 'Ignorar'},
        ]
    }

    # Se temos um dono sugerido diferente do sender, adicionar opcao
    if suggested_owner and suggested_owner.lower() != sender_name.lower():
        proposal_data['options'].insert(0, {
            'id': 'update_suggested',
            'label': f'Atualizar {suggested_owner}'
        })

    service = get_action_proposals()
    proposal = service.create_proposal(proposal_data)

    return {'proposal': proposal} if proposal else {}


# ==================== MEETING DETECTION ====================

def detect_meeting_proposal(content: str) -> Optional[Dict]:
    """Detecta mencoes a reuniao/horario/data na mensagem."""
    text_lower = content.lower()

    # Precisa ter algum indicador de tempo/data
    found_patterns = []
    for pattern in MEETING_PATTERNS:
        match = pattern.search(content)
        if match:
            found_patterns.append({
                'match': match.group(0),
                'groups': match.groups(),
            })

    if not found_patterns:
        return None

    # Extrair informacoes
    result = {
        'raw_matches': [p['match'] for p in found_patterns],
        'text': content,
    }

    # Tentar extrair horario
    time_match = re.search(r'(\d{1,2})(?:[:h](\d{2}))?', content)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        if 6 <= hour <= 22:  # Horario razoavel
            result['time'] = f"{hour:02d}:{minute:02d}"

    # Tentar extrair data
    date_match = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', content)
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = date_match.group(3)
        if year:
            year = int(year)
            if year < 100:
                year += 2000
        else:
            year = datetime.now().year
        result['date'] = f"{day:02d}/{month:02d}/{year}"

    # Detectar dia da semana
    weekday_match = re.search(
        r'(segunda|terca|terça|quarta|quinta|sexta|sabado|sábado|domingo)',
        text_lower
    )
    if weekday_match:
        result['weekday'] = weekday_match.group(1)

    if 'amanha' in text_lower or 'amanhã' in text_lower:
        result['relative'] = 'amanha'

    if 'semana que vem' in text_lower:
        result['relative'] = 'semana_que_vem'

    return result


async def handle_meeting_proposal(
    meeting_info: Dict,
    contact_id: int,
    message_id: int,
    content: str
) -> List[Dict]:
    """Cria proposta de acao para reuniao detectada."""
    from services.action_proposals import get_action_proposals

    contact_name = _get_contact_name(contact_id)

    # Montar descricao do horario/data
    time_parts = []
    if meeting_info.get('date'):
        time_parts.append(meeting_info['date'])
    if meeting_info.get('weekday'):
        time_parts.append(meeting_info['weekday'])
    if meeting_info.get('relative'):
        rel = meeting_info['relative']
        if rel == 'amanha':
            time_parts.append('amanha')
        elif rel == 'semana_que_vem':
            time_parts.append('semana que vem')
    if meeting_info.get('time'):
        time_parts.append(f"as {meeting_info['time']}")

    time_description = ", ".join(time_parts) if time_parts else "horario mencionado"

    title = f"Possivel reuniao: {time_description} com {contact_name}"
    description = (
        f"{contact_name} mencionou: \"{', '.join(meeting_info.get('raw_matches', []))}\". "
        f"Deseja criar um evento no calendario?"
    )

    proposal_data = {
        'action_type': 'create_meeting',
        'contact_id': contact_id,
        'message_id': message_id,
        'title': title[:200],
        'description': description[:500],
        'trigger_text': content[:300],
        'ai_reasoning': f"Detectada mencao a reuniao/horario na mensagem de {contact_name}.",
        'confidence': 0.7,
        'urgency': 'medium',
        'action_params': {
            'meeting_info': meeting_info,
            'contact_name': contact_name,
        },
        'options': [
            {'id': 'create_event', 'label': 'Criar evento'},
            {'id': 'respond_later', 'label': 'Responder depois'},
            {'id': 'ignore', 'label': 'Ignorar'},
        ]
    }

    service = get_action_proposals()
    proposal = service.create_proposal(proposal_data)

    return [{'proposal': proposal}] if proposal else []


# ==================== PHONE DETECTION ====================

def extract_phones(content: str) -> List[str]:
    """Extrai numeros de telefone do texto."""
    phones = PHONE_PATTERN.findall(content)
    # Filtrar numeros muito curtos ou que sao so numeros soltos
    filtered = []
    for phone in phones:
        digits = ''.join(filter(str.isdigit, phone))
        if len(digits) >= 10:
            filtered.append(phone.strip())
    return filtered


async def handle_detected_phones(
    phones: List[str],
    contact_id: int,
    message_id: int,
    content: str
) -> List[Dict]:
    """Cria proposta para telefones detectados."""
    from services.action_proposals import get_action_proposals

    results = []
    contact_name = _get_contact_name(contact_id)

    for phone in phones:
        digits = ''.join(filter(str.isdigit, phone))

        # Verificar se ja existe no contato atual
        existing = _contact_has_phone(contact_id, digits)
        if existing:
            continue

        title = f"Telefone detectado: {phone}"
        description = (
            f"Na conversa com {contact_name}, foi mencionado o telefone {phone}. "
            f"Deseja adicionar ao contato?"
        )

        proposal_data = {
            'action_type': 'update_contact_phone',
            'contact_id': contact_id,
            'message_id': message_id,
            'title': title,
            'description': description,
            'trigger_text': content[:300],
            'ai_reasoning': f"Telefone {phone} detectado na mensagem de {contact_name}.",
            'confidence': 0.7,
            'urgency': 'low',
            'action_params': {
                'phone': phone,
                'phone_digits': digits,
            },
            'options': [
                {'id': 'update_contact', 'label': f'Adicionar a {contact_name}'},
                {'id': 'create_new', 'label': 'Criar novo contato'},
                {'id': 'ignore', 'label': 'Ignorar'},
            ]
        }

        service = get_action_proposals()
        proposal = service.create_proposal(proposal_data)
        if proposal:
            results.append({'proposal': proposal})

    return results


def _contact_has_phone(contact_id: int, phone_digits: str) -> bool:
    """Verifica se o contato ja tem esse telefone."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT telefone FROM contacts WHERE id = %s
            """, (contact_id,))
            row = cursor.fetchone()
            if row and row['telefone']:
                existing_digits = ''.join(filter(str.isdigit, row['telefone']))
                # Comparar ultimos 8-9 digitos
                if len(existing_digits) >= 8 and len(phone_digits) >= 8:
                    return existing_digits[-8:] == phone_digits[-8:]
            return False
    except Exception:
        return False


# ==================== NOTIFICATIONS ====================

async def notify_renato_proposal(proposal: Dict):
    """Envia notificacao WhatsApp para Renato sobre uma proposta criada."""
    if not proposal:
        return

    try:
        from services.intel_bot import send_intel_notification

        title = proposal.get('title', 'Acao sugerida')
        description = proposal.get('description', '')
        proposal_id = proposal.get('id', '')

        message = (
            f"🔔 *{title}*\n"
            f"{description[:200]}\n\n"
            f"Responda aqui:\n"
            f'• "ver" — mostro mais detalhes\n'
            f'• "executar" — executo a acao\n'
            f'• "ignorar" — descarto\n\n'
            f"Ref: #{proposal_id}"
        )

        await send_intel_notification(message)
        logger.info(f"Sent intel notification for proposal #{proposal_id}")

    except Exception as e:
        logger.error(f"Error notifying Renato about proposal: {e}")


async def notify_renato_text(text: str):
    """Envia notificacao de texto livre para Renato via intel-bot."""
    if not text:
        return

    try:
        from services.intel_bot import send_intel_notification
        await send_intel_notification(text)
    except Exception as e:
        logger.error(f"Error sending text notification to Renato: {e}")
