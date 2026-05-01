"""
Message Filters - Deteccao de mensagens automaticas/sistema.

Usado para evitar que processadores de IA (smart_message_processor, realtime_analyzer)
interpretem notificacoes automaticas, mensagens de erro tecnico ou pingbacks como
pedidos reais do contato.

Contexto: feedback de 2026-04-25 — sistema interpretou mensagens automaticas de
portaria como pedidos de indicacao reais.
"""
import re
import logging
from typing import Optional, Dict
from database import get_db

logger = logging.getLogger(__name__)

# Padroes de mensagens automaticas / sistema
_AUTOMATED_PATTERNS = [
    re.compile(r'\[\s*(sistema|system|auto|automatico|bot)\s*\]', re.IGNORECASE),
    re.compile(r'\bnotifica[cç][aã]o\s+autom[aá]tica\b', re.IGNORECASE),
    re.compile(r'\bmensagem\s+autom[aá]tica\b', re.IGNORECASE),
    re.compile(r'\bcadastro\s+(realizado|pendente|conclu[ií]do)\b', re.IGNORECASE),
    re.compile(r'\bconfirma[cç][aã]o\s+autom[aá]tica\b', re.IGNORECASE),
    re.compile(r'\bn[aã]o\s+responda\s+(esta|essa)\s+mensagem\b', re.IGNORECASE),
    re.compile(r'\bdo\s+not\s+reply\b', re.IGNORECASE),
    re.compile(r'\b(c[oó]digo|code)\s+de\s+verifica[cç][aã]o\b', re.IGNORECASE),
    re.compile(r'\bseu\s+c[oó]digo\s+[eé]\s*[:\s]\s*\d{3,}\b', re.IGNORECASE),
    re.compile(r'\botp\b.*\d{4,}', re.IGNORECASE),
    # Erros tecnicos / pingbacks
    re.compile(r'^\s*(erro|error)\s*[:#]', re.IGNORECASE),
    re.compile(r'\b(http|https)\s*error\s+\d{3}\b', re.IGNORECASE),
    re.compile(r'\bstacktrace\b|\btraceback\b', re.IGNORECASE),
    re.compile(r'\bexception\s+(thrown|raised)\b', re.IGNORECASE),
    # Notificacoes de portaria/condominio
    re.compile(r'\bvisitante\s+(autorizado|na\s+portaria|aguardando)\b', re.IGNORECASE),
    re.compile(r'\bencomenda\s+(recebida|na\s+portaria|para\s+retirar)\b', re.IGNORECASE),
    re.compile(r'\bporteiro\s+eletr[oô]nico\b', re.IGNORECASE),
    # Bancos / transacoes automaticas
    re.compile(r'\b(pix|transfer[eê]ncia|d[eé]bito)\s+(recebid|realizad|aprovad)', re.IGNORECASE),
    re.compile(r'\bcompra\s+aprovada\s+no\s+cart[aã]o\b', re.IGNORECASE),
    # Marketing / spam
    re.compile(r'\bclique\s+(aqui|no\s+link)\s+para\b', re.IGNORECASE),
    re.compile(r'\bdescadastrar.*lista\b', re.IGNORECASE),
]

# Apelidos / nomes que indicam contato automatizado
_AUTOMATED_NAME_HINTS = [
    'portaria', 'porteiro', 'condominio', 'condomínio',
    'sistema', 'system', 'bot', 'noreply', 'no-reply',
    'notifica', 'automatico', 'automático',
    'banco do brasil', 'itau', 'bradesco', 'nubank',  # bancos costumam mandar autom.
]


def is_automated_message(
    content: str,
    contact_id: Optional[int] = None
) -> tuple[bool, Optional[str]]:
    """
    Decide se uma mensagem deve ser ignorada por processadores de IA.

    Retorna (is_automated, reason). Chamadores devem fazer log do reason.

    Heuristicas (na ordem):
    1. Padroes de texto: notificacoes automaticas, erros, pingbacks, OTPs
    2. Contato com tag/categoria 'automated'
    3. Nome do contato sugere automatizado (portaria, banco, sistema...)
    """
    if not content or not content.strip():
        return False, None

    # 1. Padroes textuais
    for pattern in _AUTOMATED_PATTERNS:
        if pattern.search(content):
            return True, f"matched pattern: {pattern.pattern[:60]}"

    # 2 + 3. Verificar contato
    if contact_id:
        info = _get_contact_filter_info(contact_id)
        if info:
            tags = info.get('tags') or []
            categorias = info.get('categorias') or []
            if 'automated' in tags or 'automated' in categorias:
                return True, f"contact tagged automated (id={contact_id})"

            nome = (info.get('nome') or '').lower()
            empresa = (info.get('empresa') or '').lower()
            for hint in _AUTOMATED_NAME_HINTS:
                if hint in nome or hint in empresa:
                    return True, f"contact name hints automated: '{hint}' in '{nome or empresa}'"

    return False, None


def _get_contact_filter_info(contact_id: int) -> Optional[Dict]:
    """Busca dados minimos do contato para classificacao."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT nome, empresa, tags, categorias
                FROM contacts
                WHERE id = %s
            """, (contact_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.warning(f"Error fetching contact {contact_id} for filter: {e}")
        return None
