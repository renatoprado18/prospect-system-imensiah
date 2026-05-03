"""
Message Classifier — decide se uma mensagem incoming precisa resposta.

Pipeline hibrido:
  1. Cache (message_classifications table)
  2. Rule-based (regex de fechamentos / acks / so emojis / so perguntas)
  3. LLM (Claude Haiku 4.5, single-call, max_tokens=150)

Usado pelo statcard "Contatos c/ Atencao" pra eliminar falsos positivos.

Modelo: SOMENTE claude-haiku-4-5-20251001 (cheap, ~$0.0001/msg).
NAO usar Sonnet/Opus — classificacao binaria nao precisa.

Override manual: salva com method='manual' e tem precedencia (sobrescreve cache).
"""
import os
import re
import json
import logging
import asyncio
from typing import Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 150

# Patterns 100% "nao precisa resposta" — fechamento, ack, agradecimento curto.
# Aplica APENAS em mensagens curtas (<=80 chars) — texto longo com "obrigado"
# no meio pode ser uma pergunta + agradecimento, e af deve cair no LLM.
_NO_REPLY_PATTERNS = [
    # Agradecimentos / acks curtos com pontuacao/emoji opcional
    r'^\s*(obrigad[ao]|valeu|t[áa] bom|tudo bem|beleza|combinado|recebido|anotado|perfeito|legal|[óo]timo|maravilha|certo|ok|okay|blz)[\s.!…👍🙏❤️✅🎉😊👏🤝💯🔝]*$',
    # So emojis (qualquer combinacao de positivos)
    r'^\s*[👍🙏❤️✅🎉😊👏🤝💯🔝😀😂🤣👌🔥]+\s*$',
    # Confirma agendamento / acao tomada
    r'^\s*(coloquei|adicionei|agendei|coloquei na agenda|joguei na agenda|coloquei no calendario|botei na agenda)[\s.!]*$',
    r'^\s*(j[áa] fiz|j[áa] est[áa]|t[áa] feito|conclu[ií]do|feito)[\s.!]*$',
    # Concordancia simples
    r'^\s*(sim|n[ãa]o|claro|com certeza|pode ser|sem problema)[\s.!]*$',
]


def rule_based_check(text: str) -> Optional[bool]:
    """
    Returns:
        False  → nao precisa resposta (pattern match)
        True   → precisa resposta (pergunta clara em texto significativo)
        None   → ambiguo (delegar pro LLM)
    """
    if not text or len(text.strip()) == 0:
        return False
    text_lower = text.lower().strip()

    # Curta + pattern fechamento = no reply
    if len(text_lower) <= 80:
        for pat in _NO_REPLY_PATTERNS:
            if re.match(pat, text_lower, re.IGNORECASE):
                return False

    # Tem pergunta clara em texto substancial = needs reply
    if '?' in text and len(text.strip()) > 5:
        return True

    return None  # ambiguo — vai pro LLM


async def llm_classify(text: str, sender_name: str = '') -> Tuple[bool, str]:
    """
    Claude Haiku — classifica binario com 1 frase de reasoning.

    Returns:
        (requires_reply: bool, reasoning: str)
    """
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY ausente — defaulting para requires_reply=True (conservador)")
        return True, "no_api_key"

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK ausente — defaulting para requires_reply=True")
        return True, "no_sdk"

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""Voce classifica mensagens recebidas em PT-BR pra decidir se precisam de resposta.

Mensagem de {sender_name or 'contato'}:
\"\"\"
{text[:1500]}
\"\"\"

Responda SO em JSON valido:
{{"requires_reply": true|false, "reasoning": "1 frase curta em pt-br"}}

Regras:
- Agradecimentos curtos, acks ("ok", "obrigada", "coloquei na agenda") = false
- Perguntas diretas, pedidos, propostas, convites = true
- Atualizacoes informacionais sem call-to-action = false
- Quando em duvida = true (conservador)
"""

    try:
        msg = await asyncio.to_thread(
            client.messages.create,
            model=CLASSIFIER_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = msg.content[0].text if msg.content else ''
        # Extrai JSON do texto (Haiku as vezes adiciona prefixo/sufixo)
        try:
            start = raw.index('{')
            end = raw.rindex('}') + 1
            data = json.loads(raw[start:end])
            requires = bool(data.get('requires_reply', True))
            reasoning = str(data.get('reasoning', ''))[:280]
            return requires, reasoning
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(f"llm_classify parse error: {e} | raw={raw[:200]}")
            return True, 'parse_error'  # conservador
    except Exception as e:
        logger.warning(f"llm_classify api error: {e}")
        return True, f'api_error: {str(e)[:120]}'  # conservador


def _get_cached(message_id: int, source_table: str) -> Optional[dict]:
    """Busca classificacao previa. Returns dict ou None."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT requires_reply, reasoning, method, classified_at
                FROM message_classifications
                WHERE message_id = %s AND source_table = %s
                """,
                (message_id, source_table),
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
    except Exception as e:
        logger.warning(f"_get_cached err msg={message_id}: {e}")
    return None


def _save_classification(
    message_id: int,
    source_table: str,
    requires_reply: bool,
    reasoning: str,
    method: str,
) -> None:
    """Upsert classificacao. method='manual' sobrescreve qualquer previa."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO message_classifications
                  (message_id, source_table, requires_reply, reasoning, method, classified_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (message_id, source_table)
                DO UPDATE SET
                  requires_reply = EXCLUDED.requires_reply,
                  reasoning = EXCLUDED.reasoning,
                  method = EXCLUDED.method,
                  classified_at = NOW()
                """,
                (message_id, source_table, requires_reply, reasoning, method),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"_save_classification err msg={message_id}: {e}")


async def classify(
    message_id: int,
    source_table: str,
    text: str,
    sender_name: str = '',
    force: bool = False,
) -> dict:
    """
    Orquestra classificacao com cache + rule + LLM.

    Args:
        message_id: id na tabela source_table
        source_table: 'messages', 'gmail_messages', etc
        text: conteudo da mensagem
        sender_name: nome do contato (opcional, ajuda contexto)
        force: ignora cache e reclassifica

    Returns:
        {
          'requires_reply': bool,
          'reasoning': str,
          'method': 'rule'|'llm'|'manual',
          'cached': bool,
        }
    """
    # 1. Cache
    if not force:
        cached = _get_cached(message_id, source_table)
        if cached:
            return {
                'requires_reply': cached['requires_reply'],
                'reasoning': cached['reasoning'] or '',
                'method': cached['method'],
                'cached': True,
            }

    # 2. Rule-based
    rule_result = rule_based_check(text or '')
    if rule_result is not None:
        reasoning = 'rule_no_reply' if rule_result is False else 'rule_question_mark'
        _save_classification(message_id, source_table, rule_result, reasoning, 'rule')
        return {
            'requires_reply': rule_result,
            'reasoning': reasoning,
            'method': 'rule',
            'cached': False,
        }

    # 3. LLM
    requires, reasoning = await llm_classify(text or '', sender_name)
    _save_classification(message_id, source_table, requires, reasoning, 'llm')
    return {
        'requires_reply': requires,
        'reasoning': reasoning,
        'method': 'llm',
        'cached': False,
    }


def manual_override(
    message_id: int,
    source_table: str,
    requires_reply: bool,
    reasoning: str = '',
) -> dict:
    """
    Override manual (botao "✓ Nao precisa resposta" na UI).
    Sempre sobrescreve qualquer classificacao previa.
    """
    _save_classification(
        message_id,
        source_table,
        requires_reply,
        reasoning or 'manual_user_override',
        'manual',
    )
    return {
        'requires_reply': requires_reply,
        'reasoning': reasoning or 'manual_user_override',
        'method': 'manual',
        'cached': False,
    }
