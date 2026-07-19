"""
Message Classifier — decide se uma mensagem incoming precisa resposta.

Pipeline hibrido:
  1. Cache (message_classifications table)
  2. Rule-based (regex de fechamentos / acks / so emojis / so perguntas)
  3. LLM via ADVISOR (services.llm.classify_with_advisor): Haiku decide; com a
     flag TRIAGE_ADVISOR ON, escala pro Sonnet quando o Haiku esta em duvida
     (confidence < 0.7) OU o contato e circulo 1-2 (falso-negativo caro). OFF =
     so Haiku, comportamento anterior byte-a-byte.

Usado pelo statcard "Contatos c/ Atencao" pra eliminar falsos positivos.

Custo base: Haiku ~$0.0001/msg. Advisor OFF nao adiciona custo; ON so paga
Sonnet nas msgs duvidosas/criticas (a maioria resolve no Haiku).

Override manual: salva com method='manual' e tem precedencia (sobrescreve cache).
"""
from services import llm
import re
import json
import logging
from typing import Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)

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

# Digests automatizados do proprio bot do INTEL — comecam com emoji especifico
# seguido de marcadores tipicos. Sao auto-DMs do sistema pro Renato; ele nunca
# responde isso. Identificado em prod: contato APCE (id 25597) era principal
# fonte de falso positivo "Responder msg" no statcard.
_BOT_DIGEST_PATTERNS = [
    r'^\s*📊\s*coleta de m[ée]tricas',
    r'^\s*📧\s*\*?digest de emails',
    r'^\s*📱\s*\*?digest dos grupos',
    r'^\s*📰\s*\*?clipping',
    r'^\s*🌅\s*\*?bom dia',
    r'^\s*🌙\s*\*?boa noite',
]

# Placeholders de midia sem texto util — viram ruido se classificados como
# "precisa resposta" (ex: "[Áudio]" sem transcricao).
_MEDIA_PLACEHOLDER_PATTERNS = [
    r'^\s*\[(áudio|audio|imagem|image|v[íi]deo|video|sticker|figurinha|gif|documento|document|arquivo|file)\]\s*$',
]

# Regex pra remover URLs antes de checar se ha pergunta real (?).
# Antes: 'https://youtu.be/...?si=abc' disparava rule_question_mark porque '?'
# tava na query string. Strippar URLs primeiro e depois checar '?' no texto restante.
_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)


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

    # Digest do bot — sempre false (auto-DM do sistema)
    for pat in _BOT_DIGEST_PATTERNS:
        if re.match(pat, text_lower, re.IGNORECASE):
            return False

    # Placeholder de midia sem texto — false (sem conteudo pra classificar)
    for pat in _MEDIA_PLACEHOLDER_PATTERNS:
        if re.match(pat, text_lower, re.IGNORECASE):
            return False

    # Curta + pattern fechamento = no reply
    if len(text_lower) <= 80:
        for pat in _NO_REPLY_PATTERNS:
            if re.match(pat, text_lower, re.IGNORECASE):
                return False

    # Tem pergunta clara em texto substancial = needs reply.
    # Stripa URLs antes pra evitar falso positivo em query strings (?si=, ?utm=, etc).
    text_sem_url = _URL_RE.sub('', text).strip()
    if '?' in text_sem_url and len(text_sem_url) > 5:
        return True

    return None  # ambiguo — vai pro LLM


def _build_llm_prompt(text: str, sender_name: str = '') -> str:
    """Monta o prompt do classificador binario (requires_reply).

    ADITIVO: pede tambem `confidence` (0.0-1.0). Quando o advisor esta OFF o
    campo e ignorado (comportamento byte-a-byte); quando ON alimenta a decisao
    de escalar Haiku→Sonnet.
    """
    return f"""Voce classifica mensagens recebidas em PT-BR pra decidir se precisam de resposta acionavel do destinatario.

Mensagem de {sender_name or 'contato'}:
\"\"\"
{text[:1500]}
\"\"\"

Responda SO em JSON valido:
{{"requires_reply": true|false, "confidence": 0.0-1.0, "reasoning": "1 frase curta em pt-br"}}

`confidence` = quao seguro voce esta da classificacao (1.0 = certeza total, 0.5 = em cima do muro, ambiguo).

Marque true SO quando ha pedido/pergunta/decisao explicita esperando resposta:
- Perguntas diretas com expectativa de resposta ("voce topa?", "qual prazo?")
- Pedidos de acao ou decisao explicitos ("me confirma X", "preciso saber se Y")
- Propostas/convites concretos com data ou call-to-action

Marque false em todo o resto:
- Agradecimentos, acks, confirmacoes ("ok", "obrigada", "coloquei na agenda", "ja fiz")
- Atualizacoes informacionais sem CTA ("fyi", "te mandei o doc", relatos)
- Mensagens muito curtas/ambiguas sem contexto claro ("FGV?", "?", "humm")
- Placeholders de midia sem texto util ("[Audio]", "[Imagem]", "[Video]", "[Sticker]")
- Digests/resumos automatizados (comecam com emojis tipo 📊 📧 📱 e listam stats)
- Reclamacoes/desabafos sem pergunta direta
- Quando em duvida = false (preferimos perder do que poluir o inbox), mas baixe o confidence
"""


def _parse_reply_classification(raw) -> Optional[dict]:
    """Extrai {requires_reply, confidence, reasoning} do texto do LLM. Robusto
    a prefixo/sufixo (Haiku as vezes adiciona). None se nao parseou."""
    if not raw:
        return None
    try:
        start = raw.index('{')
        end = raw.rindex('}') + 1
        data = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(f"_parse_reply_classification parse error: {e} | raw={str(raw)[:200]}")
        return None
    return {
        'requires_reply': bool(data.get('requires_reply', True)),
        'confidence': data.get('confidence'),
        'reasoning': str(data.get('reasoning', ''))[:280],
    }


async def llm_classify(
    text: str, sender_name: str = '', sender_circle: Optional[int] = None
) -> Tuple[bool, str]:
    """
    Classifica binario com 1 frase de reasoning.

    Roteia pelo ADVISOR (services.llm.classify_with_advisor): Haiku decide; com
    a flag TRIAGE_ADVISOR ON, se o Haiku esta em duvida (confidence < 0.7) OU o
    contato e circulo 1-2 (gate critico — falso-negativo caro), re-roda no
    Sonnet, que substitui o veredito. OFF = so Haiku (comportamento anterior).

    Args:
        sender_circle: circulo do contato (1=familia..5=cold). Gate critico se 1-2.

    Returns:
        (requires_reply: bool, reasoning: str)
    """
    prompt = _build_llm_prompt(text or '', sender_name)
    critical = sender_circle in (1, 2)

    res = await llm.classify_with_advisor(
        prompt,
        _parse_reply_classification,
        decision_key='requires_reply',
        critical_gate=critical,
        max_tokens=MAX_TOKENS,
        label='reply_needed',
    )
    parsed = res.get('parsed')
    if parsed is None:
        # Sem key/SDK/parse — conservador (preserva comportamento anterior).
        logger.warning("llm_classify: sem veredito do LLM — defaulting requires_reply=True")
        return True, 'no_result'

    reasoning = parsed.get('reasoning') or ''
    # Telemetria leve no reasoning quando o Sonnet decidiu (nao muda `method`).
    if res.get('escalated') and res.get('tier') == 'strong':
        suffix = f" [advisor→sonnet{'*' if res.get('changed') else ''}]"
        reasoning = (reasoning + suffix)[:280]

    return bool(parsed.get('requires_reply', True)), reasoning


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
    sender_circle: Optional[int] = None,
) -> dict:
    """
    Orquestra classificacao com cache + rule + LLM.

    Args:
        message_id: id na tabela source_table
        source_table: 'messages', 'gmail_messages', etc
        text: conteudo da mensagem
        sender_name: nome do contato (opcional, ajuda contexto)
        force: ignora cache e reclassifica
        sender_circle: circulo do contato (1-5). Ativa gate critico do advisor
                       (circulo 1-2 = falso-negativo caro) quando TRIAGE_ADVISOR ON.

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

    # 3. LLM (via advisor: Haiku → talvez Sonnet se em duvida / gate critico)
    requires, reasoning = await llm_classify(text or '', sender_name, sender_circle)
    _save_classification(message_id, source_table, requires, reasoning, 'llm')
    return {
        'requires_reply': requires,
        'reasoning': reasoning,
        'method': 'llm',
        'cached': False,
    }


async def classify_pending_batch(limit: int = 500, days: int = 7) -> dict:
    """Classifica mensagens incoming ainda nao classificadas (rule -> LLM).

    Deteccao de alertas operacionais (cirurgia funcionaria-chave, etc) foi
    migrada do operational_alerts.py rule-based pro CoS Sensor Agent
    (services.cos_sensor) em 13/06/26 — 48h observacao confirmaram cobertura
    superior (9 proposals Sensor vs 1 detector velho + FP rate 11%).
    """
    processed = {
        "rule": 0,
        "llm": 0,
        "errors": 0,
        "skipped": 0,
    }
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.id, m.contact_id, m.conteudo,
                       c.nome AS sender_name,
                       c.empresa AS sender_company,
                       c.circulo AS sender_circle
                FROM messages m
                JOIN contacts c ON c.id = m.contact_id
                LEFT JOIN message_classifications mc
                    ON mc.message_id = m.id AND mc.source_table = 'messages'
                WHERE m.direcao = 'incoming'
                  AND m.enviado_em > NOW() - (%s || ' days')::interval
                  AND mc.message_id IS NULL
                  AND m.conteudo IS NOT NULL
                  AND length(trim(m.conteudo)) > 0
                ORDER BY m.enviado_em DESC
                LIMIT %s
                """,
                (str(days), limit),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        for row in rows:
            try:
                res = await classify(
                    message_id=row["id"],
                    source_table="messages",
                    text=row["conteudo"] or "",
                    sender_name=row.get("sender_name") or "",
                    sender_circle=row.get("sender_circle"),
                )
                if res.get("cached"):
                    processed["skipped"] += 1
                elif res.get("method") == "rule":
                    processed["rule"] += 1
                elif res.get("method") == "llm":
                    processed["llm"] += 1
            except Exception as e:
                processed["errors"] += 1
                logger.warning(f"classify msg={row['id']} err: {e}")
    except Exception as e:
        logger.error(f"classify_pending_batch fatal: {e}")
    return processed


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
