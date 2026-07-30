"""
Email Triage Service - Triagem automatica de emails

Classifica emails que precisam de atenção baseado em:
- Remetente importante (C1/C2/C3)
- Palavras-chave (urgente, proposta, reunião, pagamento)
- Emails não respondidos há X dias

Oferece ações: responder, agendar reunião, criar tarefa, criar projeto

Sweep cron (07/06/2026):
- sweep_email_triage(hours): roda 30min, multi-conta, idempotente por
  messages.external_id (gmail_id). Triga classificacao por regras e
  insere em email_triage. Aplicacao de label !!Renato e shadow mode
  delegados pros Commits 4 e 5.
"""
import os
import json
import re
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from database import get_db
from services.tz import iso_utc, now_utc

logger = logging.getLogger(__name__)

# Captura de corpo de email (F-2 raw email, 11/07/2026).
# Antes o sync gravava so `body["text"][:5000]` e descartava o HTML: 32% dos
# emails ficavam com conteudo vazio (HTML-only, sem parte text/plain) e emails
# longos truncavam em 5000. Agora: conteudo = texto (fallback HTML->texto) ate
# EMAIL_BODY_MAX_CHARS; conteudo_html = HTML cru (fidelidade pra leitura futura).
EMAIL_BODY_MAX_CHARS = 20000
# HTML cru guardado pra fidelidade, mas capado pra nao inchar a row (newsletter
# patologica) nem estressar o stripper regex. 200k chars cobre email real.
EMAIL_HTML_MAX_CHARS = 200000

_HTML_STRIP_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WS_RE = re.compile(r"[ \t]*\n[ \t]*")
_HTML_MULTINL_RE = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    """Converte HTML em texto legivel sem dependencia externa (so `re`).

    Remove script/style, troca <br>/<p>/<div> por quebra de linha, tira as tags
    restantes, desescapa entidades comuns e colapsa espaco em branco. Best-effort
    — nao e um parser, mas recupera o corpo de emails HTML-only pra leitura.
    """
    if not html:
        return ""
    txt = _HTML_STRIP_RE.sub(" ", html)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", txt, flags=re.IGNORECASE)
    txt = _HTML_TAG_RE.sub("", txt)
    import html as _html_mod
    txt = _html_mod.unescape(txt)
    txt = txt.replace("\xa0", " ").replace("\r", "")
    txt = _HTML_WS_RE.sub("\n", txt)
    txt = _HTML_MULTINL_RE.sub("\n\n", txt)
    return txt.strip()


def extract_email_body(body: Dict) -> Tuple[str, str]:
    """Extrai (conteudo_texto, conteudo_html) de um dict {"text","html"}.

    - conteudo_texto: prefere text/plain; se vazio, faz fallback do HTML->texto.
      Capado em EMAIL_BODY_MAX_CHARS.
    - conteudo_html: HTML cru completo (sem cap) pra fidelidade.
    Resolve o blind spot: emails HTML-only paravam de ter corpo.
    """
    body = body or {}
    text = (body.get("text") or "").strip()
    html = (body.get("html") or "")[:EMAIL_HTML_MAX_CHARS]
    if not text and html:
        text = _html_to_text(html)
    return text[:EMAIL_BODY_MAX_CHARS], html


# Cap diario pra aplicacao de label !!Renato pelo CoS (Commit 4).
# Hardcoded pra protecao contra bug em loop. Estado mantido em
# agent_actions.action_type='gmail_label_add'.
MAX_LABELS_PER_DAY = 50

# Triagem 4-bucket (20/07/2026): confianca minima pra AGIR nos buckets que
# REMOVEM da inbox (Arquivar via R4, !!Deletar via R5/R6). Abaixo disso o
# email fica no inbox (bucket "incerto" — Renato ve). Labels que MANTEM no
# inbox (!!Renato / !Andressa) usam o gate de 0.85 ja existente no must_read.
ARCHIVE_ACTION_MIN_CONFIDENCE = 0.85


# =============================================================================
# Kill-switches (INBOX-ZERO + calibracao Renato 24/07) — GATE HUMANO
# =============================================================================
# Dois flags, AMBOS default OFF (comportamento preservador/legado byte-a-byte).
# Renato liga cada um manualmente apos validar em dry_run. Lidos com .strip()
# porque Vercel/Railway as vezes colam \n na env var ([[feedback_env_var_whitespace]]).
#
#   INBOX_ZERO_ENABLED               -> apply_triage_to_inbox vira "corredor":
#                                       TUDO sai do inbox e cai em 1 dos 4 baldes.
#                                       OFF = comportamento legado (must_read fica
#                                       no inbox; incerto/silent ficam no inbox).
#   EMAIL_TRIAGE_CALIBRATION_ENABLED -> liga as regras calibradas Renato 24/07
#                                       (RC1-RC5, roteamento por ACAO-REQUERIDA)
#                                       em classify_email_cos. OFF = classificador
#                                       legado byte-a-byte (nao muda o prod sweep).

def is_inbox_zero_enabled() -> bool:
    """Kill-switch do modo INBOX-ZERO (apply_triage_to_inbox). Default OFF."""
    return (os.getenv("INBOX_ZERO_ENABLED") or "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def is_calibration_enabled() -> bool:
    """Kill-switch das regras calibradas Renato 24/07 (RC1-RC5). Default OFF."""
    return (os.getenv("EMAIL_TRIAGE_CALIBRATION_ENABLED") or "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def is_smart_layer_enabled() -> bool:
    """Kill-switch da CAMADA ESPERTA do inbox (25/07/2026). Default OFF.

    Liga o comportamento AGE + REVERSÍVEL sobre o roteamento do apply_triage_to_inbox:
      - !Andressa  -> encaminha o email pra Andressa (idempotente por label Gmail).
      - !!Renato   -> triagem inteligente: cruza a frente (signal_router), julga
                      (1 LLM) e, se RESOLVIDO-CLARO (conf>=0.85 + task casada),
                      FECHA/REMARCA a task (caminho task_reconciler: undo+audit),
                      grava nota durável, tira !!Renato e NÃO pinga. Senão mantém
                      !!Renato (a fila visual dele) e NÃO pinga — quem cobre o
                      que ela não resolveu é o urgent-tick/briefing (27/07).
      - resumo     -> no máximo 1 WhatsApp por dia BRT, e só quando ela AGIU
                      (fechou/remarcou task). Ver is_smart_needs_you_wa_enabled,
                      smart_summary_max_per_day, is_smart_auto_sender_guard_enabled.

    Com OFF o apply_triage_to_inbox roda byte-a-byte como antes (aditivo, gate
    humano). Lido com .strip()/lower ([[feedback_env_var_whitespace]])."""
    return (os.getenv("INBOX_SMART_LAYER") or "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def _smart_flag(name: str, default: bool) -> bool:
    """Kill-switch booleano por env com DEFAULT EXPLÍCITO (os outros switches do
    arquivo têm default OFF hardcoded; estes aqui nascem ON). Env vazia/ausente
    => default. .strip()/lower porque a Vercel cola '\\n' no valor
    ([[feedback_env_var_whitespace]])."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "on", "yes", "sim")


def is_smart_needs_you_wa_enabled() -> bool:
    """Kill-switch do bloco "Precisa de você" no WhatsApp. Default OFF (27/07).

    MEDIÇÃO 26-27/07: dos 10 itens que saíram nesse bloco, 6 não deviam ter
    interrompido — 3 eram robô (Jusbrasil nao-responda, IBGC marketing 22:30,
    QuintoAndar promo), 1 era duplicata literal e 2 eram o MESMO e-mail do pai
    que o urgent-tick (`📧 Email urgente (P9)`, sweep_email_triage) já tinha
    mandado 30-60min antes. Repassar o que a camada NÃO resolveu é trabalho do
    urgent-tick/briefing — aqui só nascia a segunda interrupção.

    A informação NÃO some ao desligar (ver comentário do gatilho em
    apply_triage_to_inbox): o e-mail continua com !!Renato no Gmail, continua
    com row em email_triage (sweep) e continua chegando pelo
    `get_pending_email_triage` do cos_investigator -> briefing das 8h.

    ON = comportamento pré-27/07 (bloco volta ao WhatsApp)."""
    return _smart_flag("INBOX_SMART_NEEDS_YOU_WA", False)


def smart_summary_max_per_day() -> int:
    """Teto de resumos da camada esperta por dia BRT. Default 1 (27/07).

    O cron inbox-zero-scan roda a cada 2h entre 7h e 23h BRT => até 9 runs/dia.
    O que a camada FEZ não precisa de 9 avisos: forward e resolução já são
    duráveis em agent_actions e entram no digest "Fiz por você" do briefing
    matinal (main.py cron daily-morning-briefing -> summarize_for_digest).

    0 (ou negativo) = sem teto (comportamento pré-27/07, 1 resumo por run)."""
    raw = (os.getenv("INBOX_SMART_SUMMARY_MAX_PER_DAY") or "").strip()
    if not raw:
        return 1
    try:
        return int(raw)
    except ValueError:
        return 1


def is_smart_auto_sender_guard_enabled() -> bool:
    """Kill-switch do guard de REMETENTE AUTOMÁTICO (27/07). Default ON.

    Robô (noreply@/nao-responda@/notificacao@/newsletter@/mkt@) não escala pra
    "precisa de você" — no máximo fica etiquetado. Mecânica igual à dos guards
    de campanha já existentes (_vendor_bulk_marketing / _financial_bulk_campaign):
    escada de travas conservadoras ANTES do rebaixamento. OFF = robô volta a
    poder escalar."""
    return _smart_flag("INBOX_SMART_AUTO_SENDER_GUARD", True)


def is_smart_undo_hint_enabled() -> bool:
    """Kill-switch da linha 'responde "desfaz N"' no resumo. Default OFF (27/07).

    A promessa não tinha receptor: `desfaz N` só existe em
    intel_bot.handle_bot_message (instância intel-bot-v2), e este resumo sai
    pela rap-whatsapp em self-chat, cujo caminho de entrada
    (evolution_api.process_incoming_message) não tem branch de bot. Auditoria:
    agent_actions em 10 dias = 12 linhas 'done' e ZERO 'undone'.

    ON quando o roteamento do bot voltar (mudança de infra, fora deste arquivo)."""
    return _smart_flag("INBOX_SMART_UNDO_HINT", False)


def is_email_urgent_wa_enabled() -> bool:
    """Kill-switch do WhatsApp de e-mail urgente (P>=9) do sweep. Default ON.

    27/07 — o sweep (`email-triage-sweep`, minute 7,37 => 48 runs/dia) e a
    camada esperta (9 runs/dia) mandavam WhatsApp sobre o MESMO e-mail sem
    supressão cruzada (dedup keys `email_urgent:{gmail_id}` vs `inbox_smart:*`,
    sources diferentes). Aconteceu com "Re: Regularização contábil das minhas 7
    empresas" (do pai) em 26/07 e de novo em 27/07.

    QUEM CALA: o bloco "Precisa de você" do resumo (ver
    is_smart_needs_you_wa_enabled), NÃO este push. Razão: este é ONE-SHOT por
    e-mail (dedup por gmail_id, nunca repete) e cobre só a faixa P>=9
    (imprensa/C1); o bloco do resumo era N-SHOT — o acumulador é reconstruído a
    cada run e o mesmo e-mail não-resolvido reaparecia a cada 2h enquanto
    ficasse em !!Renato. A repetição nascia lá, não aqui.

    Ganha kill-switch mesmo assim porque não tinha nenhum (condição hardcoded
    em `if classification == "must_read" and priority_val >= 9`). Default ON
    preserva o comportamento atual — desligar é decisão do Renato."""
    return _smart_flag("EMAIL_URGENT_WA_ENABLED", True)


# Camada esperta (25/07/2026) — destino do encaminhamento + controle de idempotência.
ANDRESSA_FORWARD_TO = "andressa@almeida-prado.com"
# Label Gmail próprio do forward: checado ANTES de reenviar (o cron das 2h NÃO
# pode re-encaminhar). Aplicado após o forward; a partir daí o email aparece em
# label_ids e o forward vira no-op nas runs seguintes.
ANDRESSA_FORWARD_LABEL = "andressa-encaminhado"
# Barra pra AGIR sozinho na task (fechar/remarcar). Mesma do task_reconciler.
SMART_RESOLVE_MIN_CONFIDENCE = 0.85
# tipo da nota durável de "resolvido pelo email" — DISTINTO de estado_cos (que o
# frente_review UPSERTa por DELETE+INSERT); aqui é append, não clobbera aquilo.
SMART_NOTE_TIPO = "email_resolvido"

# Auto-archive gate (24/06/2026) — substitui o flag global por config
# per conta em google_accounts.auto_archive_enabled. Lido a cada sweep.
# Criterio destrava: FP rate < 1% em 14d (manual via Renato).
def is_auto_archive_enabled(account_email: str) -> bool:
    """Le google_accounts.auto_archive_enabled pra conta."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT auto_archive_enabled FROM google_accounts WHERE email = %s",
                (account_email,),
            )
            row = cur.fetchone()
            return bool(row and row.get("auto_archive_enabled"))
    except Exception as e:
        logger.warning(f"is_auto_archive_enabled falhou: {e}")
        return False


# Gate: parametros do criterio
AUTO_ARCHIVE_GATE = {
    "window_days": 14,
    "min_decisions": 30,         # precisa amostra minima pra decidir
    "max_fp_rate": 0.01,         # 1% de FP libera
    "high_fp_alert_rate": 0.05,  # >5% gera alerta de "desabilitar"
}


def compute_auto_archive_gate(account_email: str, window_days: int = None) -> Dict:
    """Computa status do gate de auto-archive pra uma conta.

    FP rate = rejected / decided (decided = archived + approved + rejected).
    Eligible quando: decided >= min_decisions AND fp_rate < max_fp_rate.

    Returns:
        {
          account_email, window_days,
          total_proposed, archived, approved, rejected, shadow,
          decided, fp_rate, eligible,
          current_enabled, recommendation
        }
    """
    days = window_days or AUTO_ARCHIVE_GATE["window_days"]

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, COUNT(*) as n
            FROM email_archive_proposals
            WHERE account_email = %s
              AND criado_em > NOW() - INTERVAL '%s days'
            GROUP BY status
            """,
            (account_email, days),
        )
        by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute(
            "SELECT auto_archive_enabled FROM google_accounts WHERE email = %s",
            (account_email,),
        )
        acc = cur.fetchone()

    archived = by_status.get("archived", 0)
    approved = by_status.get("approved", 0)
    rejected = by_status.get("rejected", 0)
    shadow = by_status.get("shadow", 0)
    decided = archived + approved + rejected
    total = decided + shadow

    fp_rate = (rejected / decided) if decided > 0 else None
    eligible = (
        decided >= AUTO_ARCHIVE_GATE["min_decisions"]
        and fp_rate is not None
        and fp_rate < AUTO_ARCHIVE_GATE["max_fp_rate"]
    )

    current_enabled = bool(acc and acc.get("auto_archive_enabled"))

    # Recomendacao
    if current_enabled:
        if fp_rate is not None and fp_rate >= AUTO_ARCHIVE_GATE["high_fp_alert_rate"]:
            rec = "disable_high_fp"
        else:
            rec = "keep_enabled"
    else:
        if decided < AUTO_ARCHIVE_GATE["min_decisions"]:
            rec = "need_more_data"
        elif eligible:
            rec = "ready_to_enable"
        else:
            rec = "fp_too_high"

    return {
        "account_email": account_email,
        "window_days": days,
        "total_proposed": total,
        "archived": archived,
        "approved": approved,
        "rejected": rejected,
        "shadow": shadow,
        "decided": decided,
        "fp_rate": fp_rate,
        "eligible": eligible,
        "current_enabled": current_enabled,
        "recommendation": rec,
    }


def record_gate_eval(status: Dict) -> None:
    """Salva snapshot em auto_archive_gate_evals."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO auto_archive_gate_evals
                  (account_email, window_days, total_proposed, decided,
                   archived, rejected, fp_rate, eligible, recommendation)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    status["account_email"], status["window_days"],
                    status["total_proposed"], status["decided"],
                    status["archived"], status["rejected"],
                    status["fp_rate"], status["eligible"],
                    status["recommendation"],
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"record_gate_eval falhou: {e}")


def _normalize_email_dates(item: Dict) -> Dict:
    """Garante ISO-8601 UTC com 'Z' nos timestamps consumidos pelo front
    (rap_emails.html chama formatTimeAgo em enviado_em||criado_em). Sem o Z,
    JS parseia como local — mesmo bug do action proposals (fix 08384bc)."""
    for k in ("enviado_em", "criado_em", "atualizado_em", "lido_em", "expires_at"):
        v = item.get(k)
        if hasattr(v, "isoformat"):
            item[k] = iso_utc(v)
    return item

# Keywords para detecção
URGENT_KEYWORDS = ['urgente', 'urgent', 'asap', 'imediato', 'hoje', 'agora', 'crítico', 'critical']
MEETING_KEYWORDS = ['reunião', 'meeting', 'call', 'agenda', 'agendar', 'marcar', 'conversa', 'encontro']
MONEY_KEYWORDS = ['proposta', 'orçamento', 'pagamento', 'invoice', 'fatura', 'preço', 'valor', 'budget', 'contrato']
QUESTION_KEYWORDS = ['?', 'você pode', 'poderia', 'consegue', 'como', 'quando', 'onde', 'qual']
FOLLOWUP_KEYWORDS = ['aguardo', 'retorno', 'resposta', 'pending', 'waiting', 'follow up', 'follow-up']

# Regras default
DEFAULT_RULES = [
    {
        "nome": "Remetente C1 - Urgente",
        "conditions": {"sender_circles": [1]},
        "auto_classify": "urgent",
        "auto_tags": ["!!Renato"],
        "auto_priority": 10,
        "requires_approval": False
    },
    {
        "nome": "Remetente C2 - Importante",
        "conditions": {"sender_circles": [2]},
        "auto_classify": "important",
        "auto_tags": ["!!Renato"],
        "auto_priority": 8,
        "requires_approval": True
    },
    {
        "nome": "Remetente C3",
        "conditions": {"sender_circles": [3]},
        "auto_classify": "important",
        "auto_tags": [],
        "auto_priority": 6,
        "requires_approval": True
    },
    {
        "nome": "Keyword Urgente",
        "conditions": {"keywords": URGENT_KEYWORDS[:5]},
        "auto_classify": "urgent",
        "auto_tags": ["urgente"],
        "auto_priority": 9,
        "requires_approval": True
    },
    {
        "nome": "Proposta/Orçamento",
        "conditions": {"keywords": MONEY_KEYWORDS[:5]},
        "auto_classify": "important",
        "auto_tags": ["proposta", "!!Renato"],
        "auto_priority": 8,
        "requires_approval": True
    },
    {
        "nome": "Sem resposta 3+ dias",
        "conditions": {"unanswered_days": 3, "sender_circles": [1, 2, 3]},
        "auto_classify": "followup",
        "auto_tags": ["followup"],
        "auto_priority": 7,
        "requires_approval": True
    },
    {
        "nome": "Menção de reunião",
        "conditions": {"keywords": MEETING_KEYWORDS[:5]},
        "auto_classify": "important",
        "auto_tags": ["agendar"],
        "auto_priority": 6,
        "requires_approval": True
    }
]


class EmailTriageService:
    def __init__(self):
        self.rules = None  # Carregadas do banco

    # =========================================================================
    # CLASSIFICAÇÃO
    # =========================================================================

    def analyze_email(self, message_id: int) -> Optional[Dict]:
        """Analisa um email e retorna classificação + sugestões"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar email com dados do contato
            cursor.execute("""
                SELECT
                    m.id, m.conversation_id, m.contact_id, m.conteudo, m.direcao,
                    m.metadata, m.enviado_em, m.criado_em,
                    c.assunto, c.canal,
                    ct.id as contact_id, ct.nome as contact_nome, ct.empresa,
                    ct.circulo, ct.circulo_pessoal, ct.circulo_profissional,
                    ct.emails as contact_emails,
                    ga.tipo as account_type
                FROM messages m
                LEFT JOIN conversations c ON c.id = m.conversation_id
                LEFT JOIN contacts ct ON ct.id = m.contact_id
                LEFT JOIN google_accounts ga ON ga.email = (m.metadata->>'account')
                WHERE m.id = %s
            """, (message_id,))

            row = cursor.fetchone()
            if not row:
                return None

            email_data = dict(row)

            # Extrair informações
            subject = email_data.get('assunto') or ''
            body = email_data.get('conteudo') or ''
            sender_circle = self._get_effective_circle(email_data)
            account_type = email_data.get('account_type') or 'professional'

            # Aplicar regras
            rules = self._get_rules(cursor)
            classification = self._apply_rules(email_data, rules, cursor)

            # Detectar keywords
            detected_keywords = self.detect_keywords(subject, body)

            # Verificar se thread sem resposta
            unanswered_days = self._check_unanswered(cursor, email_data.get('conversation_id'))

            # Construir razões
            reasons = []
            if sender_circle and sender_circle <= 3:
                reasons.append(f"Remetente C{sender_circle}")
            if detected_keywords:
                reasons.append(f"Contém: {', '.join(detected_keywords[:3])}")
            if unanswered_days and unanswered_days >= 3:
                reasons.append(f"Sem resposta há {unanswered_days} dias")

            # Sugerir ações
            suggested_actions = self._suggest_actions(subject, body, detected_keywords)

            return {
                "message_id": message_id,
                "conversation_id": email_data.get('conversation_id'),
                "contact_id": email_data.get('contact_id'),
                "contact_nome": email_data.get('contact_nome'),
                "empresa": email_data.get('empresa'),
                "subject": subject,
                "account_type": account_type,
                "sender_circle": sender_circle,
                "classification": classification.get('classify', 'fyi'),
                "priority": classification.get('priority', 5),
                "suggested_tags": classification.get('tags', []),
                "classification_reasons": reasons,
                "suggested_actions": suggested_actions,
                "requires_approval": classification.get('requires_approval', True),
                "detected_keywords": detected_keywords,
                "unanswered_days": unanswered_days
            }

    def process_new_emails(self, account_type: str = None, limit: int = 50) -> Dict:
        """Processa emails novos da inbox e cria registros de triagem"""
        stats = {
            "processed": 0,
            "urgent": 0,
            "important": 0,
            "followup": 0,
            "skipped": 0,
            "errors": 0,
            "error_details": []
        }

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Diagnóstico: contar emails disponíveis
                cursor.execute("""
                    SELECT COUNT(*) as total FROM messages m
                    LEFT JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.direcao = 'incoming' AND c.canal = 'email'
                """)
                total_emails = cursor.fetchone()['total']
                stats["total_emails_incoming"] = total_emails

                cursor.execute("""
                    SELECT COUNT(*) as total FROM messages m
                    LEFT JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.direcao = 'incoming' AND c.canal = 'email'
                    AND m.criado_em > NOW() - INTERVAL '30 days'
                """)
                recent_emails = cursor.fetchone()['total']
                stats["emails_last_30_days"] = recent_emails

                # Buscar emails não processados (incoming, últimos 30 dias)
                query = """
                    SELECT m.id
                    FROM messages m
                    LEFT JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.direcao = 'incoming'
                    AND c.canal = 'email'
                    AND m.criado_em > NOW() - INTERVAL '30 days'
                    AND m.id NOT IN (SELECT message_id FROM email_triage WHERE message_id IS NOT NULL)
                """
                params = []

                if account_type:
                    query += " AND (m.metadata->>'account') IN (SELECT email FROM google_accounts WHERE tipo = %s)"
                    params.append(account_type)

                query += " ORDER BY m.criado_em DESC LIMIT %s"
                params.append(limit)

                cursor.execute(query, params)
                message_ids = [row['id'] for row in cursor.fetchall()]
                stats["found_to_process"] = len(message_ids)

                for msg_id in message_ids:
                    try:
                        analysis = self.analyze_email(msg_id)
                        if not analysis:
                            stats["skipped"] += 1
                            continue

                        # Verificar se precisa de atenção
                        if not analysis.get('classification') or analysis['classification'] == 'fyi':
                            if analysis.get('sender_circle') and analysis['sender_circle'] > 3:
                                stats["skipped"] += 1
                                continue

                        # Criar registro de triagem
                        cursor.execute("""
                            INSERT INTO email_triage (
                                message_id, conversation_id, contact_id,
                                needs_attention, priority, classification,
                                classification_reasons, suggested_tags, suggested_actions,
                                status, account_type, ai_confidence,
                                expires_at
                            ) VALUES (
                                %s, %s, %s,
                                %s, %s, %s,
                                %s, %s, %s,
                                %s, %s, %s,
                                NOW() + INTERVAL '7 days'
                            )
                            RETURNING id
                        """, (
                            analysis['message_id'],
                            analysis.get('conversation_id'),
                            analysis.get('contact_id'),
                            True,
                            analysis.get('priority', 5),
                            analysis.get('classification'),
                            json.dumps(analysis.get('classification_reasons', [])),
                            json.dumps(analysis.get('suggested_tags', [])),
                            json.dumps(analysis.get('suggested_actions', [])),
                            'approved' if not analysis.get('requires_approval') else 'pending',
                            analysis.get('account_type'),
                            0.85
                        ))

                        stats["processed"] += 1
                        classification = analysis.get('classification', '')
                        if classification == 'urgent':
                            stats["urgent"] += 1
                        elif classification == 'important':
                            stats["important"] += 1
                        elif classification == 'followup':
                            stats["followup"] += 1

                    except Exception as e:
                        print(f"Error processing message {msg_id}: {e}")
                        stats["errors"] += 1
                        stats["error_details"].append(f"msg {msg_id}: {str(e)}")

                conn.commit()

                return stats
        except Exception as e:
            import traceback
            stats["error_details"].append(str(e))
            stats["traceback"] = traceback.format_exc()
            print(f"Error in process_new_emails: {traceback.format_exc()}")
            return stats

    def _get_effective_circle(self, email_data: Dict) -> Optional[int]:
        """Retorna o círculo efetivo do contato"""
        circulo = email_data.get('circulo')
        if circulo:
            return circulo

        cp = email_data.get('circulo_pessoal')
        cr = email_data.get('circulo_profissional')
        if cp and cr:
            return min(cp, cr)
        return cp or cr

    def _get_rules(self, cursor) -> List[Dict]:
        """Carrega regras do banco ou usa defaults"""
        cursor.execute("""
            SELECT * FROM email_triage_rules
            WHERE ativo = TRUE
            ORDER BY ordem ASC
        """)
        rules = cursor.fetchall()

        if not rules:
            return DEFAULT_RULES

        return [dict(r) for r in rules]

    def _apply_rules(self, email_data: Dict, rules: List[Dict], cursor) -> Dict:
        """Aplica regras de classificação ao email"""
        subject = email_data.get('assunto') or ''
        body = email_data.get('conteudo') or ''
        sender_circle = self._get_effective_circle(email_data)
        conversation_id = email_data.get('conversation_id')

        for rule in rules:
            conditions = rule.get('conditions', {})
            match = True

            # Verificar círculo do remetente
            if 'sender_circles' in conditions:
                if not sender_circle or sender_circle not in conditions['sender_circles']:
                    match = False

            # Verificar keywords
            if match and 'keywords' in conditions:
                text = f"{subject} {body}".lower()
                keyword_match = any(kw.lower() in text for kw in conditions['keywords'])
                if not keyword_match:
                    match = False

            # Verificar dias sem resposta
            if match and 'unanswered_days' in conditions:
                unanswered = self._check_unanswered(cursor, conversation_id)
                if not unanswered or unanswered < conditions['unanswered_days']:
                    # Mas só aplica se também passar no filtro de círculo
                    if 'sender_circles' in conditions:
                        if not sender_circle or sender_circle not in conditions['sender_circles']:
                            match = False
                    else:
                        match = False

            if match:
                return {
                    "classify": rule.get('auto_classify', 'important'),
                    "tags": rule.get('auto_tags', []),
                    "priority": rule.get('auto_priority', 5),
                    "requires_approval": rule.get('requires_approval', True),
                    "rule_name": rule.get('nome')
                }

        # Default se nenhuma regra aplicar
        return {
            "classify": "fyi",
            "tags": [],
            "priority": 3,
            "requires_approval": True
        }

    # =========================================================================
    # CoS-aware classification (sweep cron)
    # =========================================================================

    def classify_email_cos(
        self,
        headers: Dict,
        body_text: str,
        gmail_label_ids: List[str],
        account_email: str,
        account_type: str,
        contact_id: Optional[int] = None,
        enable_calibration: Optional[bool] = None,
    ) -> Dict:
        """Classifica um email pelo modelo CoS (must_read | archive_proposed | silent).

        Retorna:
            {
                classification: 'must_read' | 'archive_proposed' | 'silent',
                priority: int 0-10,
                ai_confidence: float 0.0-1.0,
                reasons: [str],
                suggested_tags: [str],
                suggested_actions: [Dict],
                escalation: bool (M7 imprensa),
                rule_hits: [str],
            }

        Regras (Commit 2 — ordem importa, primeiro forte vence pra
        must_read; archive_proposed acumula sinais):
            R0  Label !!Renato JA aplicada (Andressa) -> must_read p10 conf=0.99 SKIP
            R1  Imprensa (PRESS_DOMAINS + regex) -> must_read p10 conf=0.95 escalation
            R2  From C0/C1 (circulo 1) -> must_read p9 conf=0.92
            R3  Frente keyword no subject -> must_read p8 conf=0.85
            R4  Sender no-reply/notifications/system -> archive_proposed conf=0.95
            R5  List-Unsubscribe + dominio comercial -> archive_proposed conf=0.92
            R6  Cold vendor (zero historico + pitch keywords) -> archive_proposed conf=0.88
            R7  Default -> silent p3 conf=0.60
        """
        from services.cos_keywords import is_frente_keyword

        subject = (headers.get("subject") or "").strip()
        from_header = (headers.get("from") or "").strip()
        list_unsubscribe = headers.get("list-unsubscribe", "") or headers.get("List-Unsubscribe", "")
        # Gmail headers may be only in payload — passar headers ja parseados
        # mas list-unsubscribe nem sempre vem. Buscamos no msg payload tbm.
        # Computado AQUI (nao mais so no R5) porque o bloco RC — que roda antes —
        # usa List-Unsubscribe como marcador de disparo em massa (guard de vendor).
        headers_lower = {k.lower(): v for k, v in (headers or {}).items()}
        has_unsubscribe = bool(list_unsubscribe or headers_lower.get("list-unsubscribe"))

        # Extrai email do remetente
        from_email = ""
        if "<" in from_header:
            m = re.search(r"<([^>]+)>", from_header)
            if m:
                from_email = m.group(1).lower()
        else:
            from_email = from_header.strip().lower()
        sender_domain = from_email.split("@")[1] if "@" in from_email else ""

        reasons: List[str] = []
        rule_hits: List[str] = []
        confidence_signals: List[float] = []

        # R0: label !!Renato ja aplicada (idempotencia entre CoS e Andressa)
        renato_label_present = False
        try:
            # gmail_label_ids vem como IDs, nao nomes — entao a checagem
            # real precisa cruzar com /users/me/labels. Aqui, conservador:
            # se Gmail label list incluir o ID conhecido ou se o name vier
            # injetado por upstream, retornamos must_read direto.
            # Como nao temos o nome aqui, usamos heuristica via headers
            # X-Gmail-Labels (raro) — pra ser preciso, deixamos a checagem
            # no caller via gmail_label_ids comparado ao label_id cacheado
            # em outro lugar. Por simplicidade, MVP: nao pula, deixa o flow
            # normal classificar e o cap idempotente de modify_message_labels
            # garante que nao re-aplica.
            renato_label_present = False
        except Exception:
            pass

        # R1: Imprensa
        is_press = False
        if sender_domain:
            from services.notification_router import _PRESS_DOMAINS as PRESS
            for pd in PRESS:
                if sender_domain == pd or sender_domain.endswith("." + pd):
                    is_press = True
                    break
        if not is_press:
            # Regex no subject + body
            from services.notification_router import _PRESS_REGEX
            haystack = f"{subject}\n{body_text[:2000]}"
            if _PRESS_REGEX.search(haystack):
                is_press = True

        if is_press:
            return {
                "classification": "must_read",
                "priority": 10,
                "ai_confidence": 0.95,
                "reasons": ["Imprensa/jornalista detectado (M7 escalation)"],
                "suggested_tags": ["!!Renato", "imprensa"],
                "suggested_actions": [{"type": "respond", "reason": "M7 imprensa"}],
                "escalation": True,
                "rule_hits": ["R1_press"],
            }

        # R2: C0/C1 contato (circulo 1)
        if contact_id:
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT circulo, circulo_pessoal, circulo_profissional, nome
                        FROM contacts WHERE id = %s
                        """,
                        (contact_id,),
                    )
                    cinfo = cur.fetchone()
                if cinfo:
                    circulo = cinfo.get("circulo") or min(
                        x for x in [cinfo.get("circulo_pessoal"), cinfo.get("circulo_profissional"), 99] if x
                    )
                    if circulo and circulo == 1:
                        reasons.append(f"Contato C1 ({cinfo.get('nome')})")
                        rule_hits.append("R2_c1")
                        confidence_signals.append(0.92)
                        return {
                            "classification": "must_read",
                            "priority": 9,
                            "ai_confidence": 0.92,
                            "reasons": reasons,
                            "suggested_tags": ["!!Renato"],
                            "suggested_actions": [{"type": "respond", "reason": "C1"}],
                            "escalation": False,
                            "rule_hits": rule_hits,
                        }
                    if circulo and circulo == 2:
                        reasons.append(f"Contato C2 ({cinfo.get('nome')})")
                        rule_hits.append("R2b_c2")
                        return {
                            "classification": "must_read",
                            "priority": 7,
                            "ai_confidence": 0.80,
                            "reasons": reasons,
                            "suggested_tags": ["!!Renato"],
                            "suggested_actions": [{"type": "respond", "reason": "C2"}],
                            "escalation": False,
                            "rule_hits": rule_hits,
                        }
            except Exception as e:
                logger.warning(f"classify_email_cos R2 falhou: {e}")

        # Extrai from_name uma vez — reusado em R3, R4 (LinkedIn DM exc.) e R4 (drive-share exc.)
        from_name = ""
        if "<" in from_header:
            from_name = from_header.split("<", 1)[0].strip().strip('"')
        else:
            from_name = from_header

        # RC (calibracao Renato 24/07 + recalibracao 25/07): roteamento por
        # ACAO-REQUERIDA. Gate humano (EMAIL_TRIAGE_CALIBRATION_ENABLED, default
        # OFF). RODA ANTES do R3 (frente keyword) de proposito: recibos/alertas-
        # de-dispositivo/newsletters que MENCIONAM uma frente ("Poli Angels",
        # "IBGC", "Anthropic") batiam R3_frente -> !!Renato indevidamente (ping
        # WA de falso-positivo). O RC classifica pela ACAO: recibo -> Financeiro,
        # device-alert -> arquivar, marketing -> arquivar. So o que EXIGE decisao
        # (RC1 convocacao, RC5 juridico de frente ativa) segue pro Renato. Com o
        # gate OFF este bloco e no-op e o fluxo cai byte-a-byte no R3 legado.
        _calib = enable_calibration if enable_calibration is not None else is_calibration_enabled()
        if _calib:
            rc = classify_calibrated(
                subject=subject,
                body_text=body_text,
                from_email=from_email,
                from_name=from_name,
                sender_domain=sender_domain,
                account_type=account_type,
                has_unsubscribe=has_unsubscribe,
            )
            if rc is not None:
                return rc

        # R3: Frente keyword em subject, from_name OU dominio do remetente.
        # Cobre ASSESPRO-SP (from_name) + "Planejamento Estratégico" no subject
        # (com acento, agora normalizado em is_frente_keyword).
        try:
            frente = (
                is_frente_keyword(subject)
                or is_frente_keyword(from_name)
                or is_frente_keyword(sender_domain)
            )
            if frente:
                reasons.append(f"Frente {frente} keyword (subject/from/domain)")
                rule_hits.append("R3_frente")
                return {
                    "classification": "must_read",
                    "priority": 8,
                    "ai_confidence": 0.85,
                    "reasons": reasons,
                    "suggested_tags": ["!!Renato", f"frente_{frente}"],
                    "suggested_actions": [{"type": "respond", "reason": f"frente {frente}"}],
                    "escalation": False,
                    "rule_hits": rule_hits,
                }
        except Exception as e:
            logger.warning(f"classify_email_cos R3 falhou: {e}")

        # R3.5: Financeiro/gov real (whitelist conservadora) — DEVE vir ANTES
        # de R4 (noreply) pq emails fiscais/bancarios SEMPRE vem de noreply
        # mas sao must_read. Cobre Agilize TFE (Andressa deixou passar) +
        # boletos legitimos no personal (vs promo "FATURA DIGITAL" da Claro
        # que NAO bate whitelist).
        # Lista hoisted pra modulo (_FINANCIAL_DOMAINS) em 25/07 — fonte unica
        # tambem do guard de campanha (_is_financial_bulk_domain deriva dela).
        FINANCIAL_DOMAINS = _FINANCIAL_DOMAINS
        GOV_DOMAINS = _GOV_DOMAINS
        financial_keywords = (
            "fatura", "pagamento devido", "boleto", "cobranca",
            "vencimento", "imposto", "irpf", "darf", "guia tfe",
            "extrato", "transferencia confirmada", "nota fiscal", "nf-e",
            "invoice", "receipt",
        )
        is_financial_domain = bool(sender_domain) and any(sender_domain.endswith(d) for d in FINANCIAL_DOMAINS)
        is_gov_domain = bool(sender_domain) and any(sender_domain.endswith(d) for d in GOV_DOMAINS)
        text_lc_fin = f"{subject} {body_text[:1500]}".lower()
        has_financial_kw = any(kw in text_lc_fin for kw in financial_keywords)

        # Guard de vendor de massa (telecom/utility). Estes dominios entram no
        # FINANCIAL_DOMAINS porque a fatura real e legitima — mas o DOMINIO
        # SOZINHO nao prova cobranca, so diz quem mandou. Recalibracao 25/07
        # (run real 13:25 BRT): "Veja seu contrato dos servicos Vivo" e "Vai de
        # App Vivo! Beneficios." caiam no whitelist -> !!Renato+financeiro ->
        # bucket Andressa. Nem decisao do Renato, nem execucao da Andressa.
        #
        # Inversao do gate (era opt-out por keyword de promo, agora e opt-IN por
        # evidencia de cobranca): sem "fatura/boleto/2a via/vencimento/codigo de
        # barras" o email NAO e financeiro. Estritamente mais amplo que a regra
        # antiga (promo sem fatura continua saindo), e fatura real nao regride.
        is_vendor_bulk = _is_vendor_bulk_domain(sender_domain)
        if is_vendor_bulk and is_financial_domain:
            text_norm_fin = _norm_txt(text_lc_fin)
            has_billing_evidence = (
                _has_phrase(text_norm_fin, _BILLING_EVIDENCE_PHRASES)
                or _is_billing_sender(from_email)   # caixa suafatura@/boleto@
            )
            if not has_billing_evidence:
                # Vendor de massa sem prova de cobranca — fluir pra R4/R5/R7
                # (ou, com calibracao ON, ja arquivado pelo RC8 sinal (c)).
                is_financial_domain = False

        # Guard de CAMPANHA em dominio financeiro nao-telecom (extensao 25/07).
        # Casos reais: QuintoAndar NPS ("Ainda da tempo de responder a pesquisa!",
        # "Queremos melhorar sua experiencia") e Cora indicacao ("Sua indicacao
        # vale ate R$2.000") entravam no whitelist -> !!Renato+financeiro ->
        # balde Andressa. Aqui o gate e opt-IN por evidencia POSITIVA de campanha
        # NO ASSUNTO (ver _financial_bulk_campaign): cobranca, aviso de seguranca,
        # mudanca contratual e extrato continuam na trilha atual. GOV nao entra.
        # Com calibracao ON o RC8 ja devolveu 'arquivar' antes de chegar aqui;
        # este gate e o caminho de calibracao OFF.
        is_financial_campaign = _financial_bulk_campaign(
            _norm_txt(subject), _norm_txt(body_text[:3000]),
            sender_domain, from_email=from_email,
        )
        if is_financial_campaign:
            # Nao e financeiro (R3.5) NEM admin da Andressa (R3.6) — flui pra
            # R4/R5/R7 e sai do inbox pelo caminho de arquivar.
            is_financial_domain = False

        # Whitelist domain = must_read (alta priority com keyword, media sem).
        # Whitelist e altamente seletiva — Agilize/banco/Receita so manda
        # comunicacoes legitimas operacionais. Priority menor sem keyword
        # marca shadow pra calibracao se acumular FP.
        if is_financial_domain or is_gov_domain:
            tag_kind = "gov" if is_gov_domain else "financeiro"
            prio = 8 if (has_financial_kw or is_gov_domain) else 6
            conf = 0.90 if (has_financial_kw or is_gov_domain) else 0.78
            reasons.append(
                f"Whitelist {tag_kind} ({sender_domain})"
                + (" + keyword financeira" if has_financial_kw else " (sem keyword — priority menor)")
            )
            rule_hits.append("R3_5_financial_gov")
            return {
                "classification": "must_read",
                "priority": prio,
                "ai_confidence": conf,
                "reasons": reasons,
                "suggested_tags": ["!!Renato", tag_kind],
                "suggested_actions": [{"type": "respond", "reason": "financeiro/gov"}],
                "escalation": False,
                "rule_hits": rule_hits,
            }

        # R3.6 (25/06/2026): Andressa admin senders. Roteia direto pra
        # label !Andressa sem precisar !!Renato. Caracteristica: dominios
        # que ela tradicionalmente cuida.
        ANDRESSA_DOMAINS = (
            "agilize.com.br",
            "cora.com.br",
            "fireflies.ai",  # transcripts de calls que ela organiza
        )
        ANDRESSA_SENDER_PATTERNS = ("andressa@",)  # andressa@almeida-prado.com etc
        is_andressa_domain = bool(sender_domain) and any(
            sender_domain.endswith(d) for d in ANDRESSA_DOMAINS
        )
        is_andressa_sender = any(p in (from_email or "").lower() for p in ANDRESSA_SENDER_PATTERNS)
        # cora.com.br esta em ANDRESSA_DOMAINS: sem este `and not`, a campanha de
        # indicacao barrada no R3.5 cairia aqui e iria pro balde da Andressa do
        # mesmo jeito (o bug so mudaria de porta).
        if (is_andressa_domain and not is_financial_campaign) or is_andressa_sender:
            reasons.append(f"Andressa admin sender ({sender_domain or from_email})")
            rule_hits.append("R3_6_andressa")
            return {
                "classification": "must_read",
                "priority": 6,
                "ai_confidence": 0.88,
                "reasons": reasons,
                "suggested_tags": ["!Andressa", "admin"],
                "suggested_actions": [{"type": "delegate", "to": "andressa"}],
                "escalation": False,
                "rule_hits": rule_hits,
            }

        # R3.5b (25/06/2026): financial alert SUBJECT de qualquer sender.
        # Caso Méres 25/06: "Sua fatura vence em N dias" + "Pagamento Confirmado"
        # cairam em archive_proposed pq meres.com.br nao estava no whitelist.
        # FINANCIAL_DOMAINS agora inclui Méres+Comgas+QuintoAndar como cinto-suspensorio.
        # Esta regra cobre o caso geral: qualquer sender com PHRASES financeiras
        # explicitas no subject -> must_read p7. Phrases (nao single keywords)
        # pra reduzir FP em copy promo.
        FINANCIAL_SUBJECT_PHRASES = (
            "fatura vence", "boleto vence", "fatura venc",
            "sua fatura", "pagamento devido", "pagamento confirmado",
            "pagamento recebido", "fatura em aberto",
            "segunda via", "2a via",
            "your invoice", "invoice for",
            "fatura disponível", "boleto disponível",
            "receipt for", "your receipt",
            "vencimento da", "vence em", "vence amanha", "vence amanhã",
            "fatura de ", "boleto de ",
        )
        subject_lc_finsubj = subject.lower().strip()
        has_strong_fin_subj = any(p in subject_lc_finsubj for p in FINANCIAL_SUBJECT_PHRASES)
        if has_strong_fin_subj:
            reasons.append(f"Financial alert subject (qualquer sender — {sender_domain or 'unknown'})")
            rule_hits.append("R3_5b_financial_subject")
            return {
                "classification": "must_read",
                "priority": 7,
                "ai_confidence": 0.85,
                "reasons": reasons,
                "suggested_tags": ["!!Renato", "financeiro"],
                "suggested_actions": [{"type": "respond", "reason": "fatura/pagamento subject"}],
                "escalation": False,
                "rule_hits": rule_hits,
            }

        # R3.55: Critical infra/billing keyword override.
        # Caso 13/06/26: GCP "Ação necessária / conta de faturamento vencida"
        # foi classificado como archive_proposed pelo R4 noreply.
        # Memory feedback_triage_gcp_billing_fp.md. Risco: serviços suspensos
        # silenciosamente. DEVE vir ANTES de R4 (noreply) pra sobrepor.
        #
        # Estratégia: subject com keyword critica (alta precisao) -> must_read.
        # OU body com 2+ infra keywords E remetente plataforma -> must_read.
        KW_INFRA_CRITICAL_SUBJECT = (
            "ação necessária", "acao necessaria", "action required", "action needed",
            "conta vencida", "billing overdue", "past due", "fatura vencida",
            "pagamento inválid", "pagamento invalido", "invalid payment", "payment failed",
            "serviços afetad", "service affected", "serviços suspens",
            "encerramento da", "account closed", "account suspended",
            "cobrança falh", "cobranca falh", "charge failed",
            "corre o risco de ser suspenso", "risk of suspension",
            "subscription canceled", "assinatura cancelad",
        )
        KW_INFRA_BODY = (
            "billing", "faturamento",
            "vencid", "overdue",
            "suspens", "suspend",
            "atraso de pagamento", "pagamento em atraso",
            "informações de pagamento inválid",
            "encerrar", "encerramento",
            "serviços não sejam interrompidos",
            "servicos nao sejam interrompidos",
        )
        PLATFORM_SENDERS = (
            "google.com", "cloud.google.com", "cloudplatform-noreply",
            "amazonaws.com", "aws.amazon.com",
            "microsoft.com", "azure.com",
            "vercel.com", "stripe.com", "stripe-noreply",
            "anthropic.com", "anyscale.com", "supabase.io",
            "fly.io", "railway.app", "neon.tech", "hetzner.com",
            "openai.com", "groq.com", "linkdapi.com",
        )
        subject_lc_crit = subject.lower().strip()
        body_lc_crit = body_text[:3000].lower()
        from_email_lc = (from_email or "").lower()
        has_critical_subject_kw = any(kw in subject_lc_crit for kw in KW_INFRA_CRITICAL_SUBJECT)
        body_kw_hits = sum(1 for kw in KW_INFRA_BODY if kw in body_lc_crit)
        is_platform_sender = any(p in from_email_lc for p in PLATFORM_SENDERS)

        if has_critical_subject_kw or (body_kw_hits >= 2 and is_platform_sender):
            reasons.append(
                "Critical infra/billing keyword "
                + ("(subject)" if has_critical_subject_kw else f"(body kw={body_kw_hits} + platform sender)")
            )
            rule_hits.append("R3_55_critical_infra")
            return {
                "classification": "must_read",
                "priority": 9,
                "ai_confidence": 0.93,
                "reasons": reasons,
                "suggested_tags": ["!!Renato", "infra-critico", "billing"],
                "suggested_actions": [{"type": "respond", "reason": "infra/billing critico"}],
                "escalation": True,
                "rule_hits": rule_hits,
            }

        # R3.6: Calendar invite — Andressa deixou passar Poli Angels Round 47
        # Google Calendar invites tem subject "Invitation:" (ou "Convite:")
        # e/ou attachment .ics. Convite real = decisao de agenda pendente.
        subject_lower = subject.lower().strip()
        if (
            subject_lower.startswith("invitation:")
            or subject_lower.startswith("convite:")
            or subject_lower.startswith("updated invitation:")
            or subject_lower.startswith("convite atualizado:")
            or subject_lower.startswith("invite:")
            or subject_lower.startswith("re: invitation:")
            or subject_lower.startswith("canceled event:")
            or subject_lower.startswith("evento cancelado:")
        ):
            reasons.append(f"Calendar invite: {subject[:60]}")
            rule_hits.append("R3_6_calendar_invite")
            return {
                "classification": "must_read",
                "priority": 8,
                "ai_confidence": 0.92,
                "reasons": reasons,
                "suggested_tags": ["!!Renato", "calendar-invite"],
                "suggested_actions": [{"type": "rsvp", "reason": "calendar invite"}],
                "escalation": False,
                "rule_hits": rule_hits,
            }

        # (bloco RC de calibracao movido pra ANTES do R3 — ver acima. Rodava
        # aqui ate 24/07 mas o R3_frente pre-emptava recibo/device-alert/
        # marketing que mencionava frente. Recalibracao 25/07.)

        # R4: no-reply / notifications / system
        if from_email:
            local = from_email.split("@")[0]
            noreply_patterns = (
                "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
                "notifications", "notification", "system", "automated",
                "mailer-daemon", "postmaster",
            )
            if any(p in local for p in noreply_patterns):
                # R4 exception A: LinkedIn DM real vem de messaging-noreply
                # mas com nome de pessoa no from_name.
                # Exemplo: "Rodrigo Clausen via LinkedIn" <messaging-noreply@linkedin.com>
                # Detecta: dominio linkedin.com + from_name diferente de "LinkedIn"
                # genérico/alertas/jobs/notifications.
                if sender_domain and sender_domain.endswith("linkedin.com"):
                    from_name_lower = (from_name or "").lower().strip()
                    is_real_person_dm = (
                        from_name_lower
                        and from_name_lower not in ("linkedin", "linkedin learning")
                        and not any(k in from_name_lower for k in (
                            "alertas de vaga", "jobs", "notifications",
                            "notificações", "notificacoes",
                        ))
                        and "via linkedin" in from_name_lower  # heurística forte
                    )
                    if is_real_person_dm:
                        reasons.append(f"LinkedIn DM real: {from_name}")
                        rule_hits.append("R4_linkedin_dm_exception")
                        return {
                            "classification": "must_read",
                            "priority": 7,
                            "ai_confidence": 0.85,
                            "reasons": reasons,
                            "suggested_tags": ["!!Renato", "linkedin-dm"],
                            "suggested_actions": [{"type": "respond", "reason": "LinkedIn DM"}],
                            "escalation": False,
                            "rule_hits": rule_hits,
                        }

                # R4 exception B: Google Docs/Drive share — checar se
                # subject/from_name tem keyword frente (Vallen, RACI, etc).
                if (
                    sender_domain == "google.com"
                    or "drive-shares-noreply" in from_email
                    or "docs-noreply" in from_email
                ):
                    combined = f"{subject} {from_name or ''}"
                    frente_hit = is_frente_keyword(combined)
                    if frente_hit:
                        reasons.append(f"Google Docs share + frente {frente_hit}")
                        rule_hits.append("R4_drive_share_frente_exception")
                        return {
                            "classification": "must_read",
                            "priority": 8,
                            "ai_confidence": 0.88,
                            "reasons": reasons,
                            "suggested_tags": ["!!Renato", f"frente_{frente_hit}", "drive-share"],
                            "suggested_actions": [{"type": "review", "reason": "drive share frente"}],
                            "escalation": False,
                            "rule_hits": rule_hits,
                        }

                reasons.append(f"Remetente automatico ({local})")
                rule_hits.append("R4_noreply")
                return {
                    "classification": "archive_proposed",
                    "priority": 2,
                    "ai_confidence": 0.95,
                    "reasons": reasons,
                    "suggested_tags": ["auto-archive-shadow"],
                    "suggested_actions": [{"type": "archive", "reason": "noreply"}],
                    "escalation": False,
                    "rule_hits": rule_hits,
                }

        # R5: List-Unsubscribe + dominio comercial
        # (headers_lower/has_unsubscribe ja computados no topo — o bloco RC usa)
        # Tambem chega via lookup direto no message.payload.headers — caller
        # pode injetar via headers dict ja com lowercase
        if has_unsubscribe and sender_domain:
            # Dominios pessoais/relevantes NAO sao "comercial"
            commercial = not any(
                sender_domain.endswith(d)
                for d in ("gmail.com", "almeida-prado.com", "outlook.com", "icloud.com", "yahoo.com")
            )
            if commercial:
                reasons.append(f"List-Unsubscribe + dominio comercial ({sender_domain})")
                rule_hits.append("R5_unsub")
                return {
                    "classification": "archive_proposed",
                    "priority": 2,
                    "ai_confidence": 0.92,
                    "reasons": reasons,
                    "suggested_tags": ["auto-archive-shadow", "newsletter"],
                    "suggested_actions": [{"type": "archive", "reason": "newsletter"}],
                    "escalation": False,
                    "rule_hits": rule_hits,
                }

        # R6: Cold vendor (zero historico de mensagens + pitch keywords)
        cold_pitch_kw = [
            "investimento", "parceria estrategica", "consultoria especializada",
            "seu interesse", "agendar uma demonstracao", "demo gratuita",
            "oportunidade unica", "proposta exclusiva", "lead qualificado",
            "vamos marcar uma conversa", "sales pitch", "outbound",
        ]
        text_lc = f"{subject} {body_text[:2000]}".lower()
        pitch_matches = [kw for kw in cold_pitch_kw if kw in text_lc]
        if not contact_id and pitch_matches:
            reasons.append(f"Cold vendor: zero historico + {len(pitch_matches)} pitch kw")
            rule_hits.append("R6_cold")
            return {
                "classification": "archive_proposed",
                "priority": 1,
                "ai_confidence": 0.88,
                "reasons": reasons,
                "suggested_tags": ["auto-archive-shadow", "cold-vendor"],
                "suggested_actions": [{"type": "archive", "reason": "cold-vendor"}],
                "escalation": False,
                "rule_hits": rule_hits,
            }

        # R7: Default diferenciado por account_type
        # Decisao 09/06/26 apos analise das 2 caixas com user: personal tem
        # 12.496 promo + 11.921 update + 19/dia nao lidas predominantemente
        # noise — default archive_proposed agressivo. Professional tem ~8-30
        # nao lidas/sem com mistura signal+noise + label !!Renato pela
        # Andressa — default silent conservador (so archive quando sinal
        # forte tipo R4/R5/R6).
        if (account_type or "professional").lower() == "personal":
            return {
                "classification": "archive_proposed",
                "priority": 2,
                "ai_confidence": 0.78,
                "reasons": ["Conta personal: default archive_proposed (shadow) — must_read exige sinal claro"],
                "suggested_tags": ["auto-archive-shadow", "personal-default"],
                "suggested_actions": [{"type": "archive", "reason": "personal-default"}],
                "escalation": False,
                "rule_hits": ["R7_personal_default"],
            }

        return {
            "classification": "silent",
            "priority": 3,
            "ai_confidence": 0.60,
            "reasons": ["Conta professional: sem sinais fortes — silent"],
            "suggested_tags": [],
            "suggested_actions": [],
            "escalation": False,
            "rule_hits": ["R7_professional_default"],
        }

    def _check_unanswered(self, cursor, conversation_id: int) -> Optional[int]:
        """Verifica se thread está sem resposta e retorna quantos dias"""
        if not conversation_id:
            return None

        cursor.execute("""
            SELECT
                MAX(CASE WHEN direcao = 'incoming' THEN enviado_em END) as last_incoming,
                MAX(CASE WHEN direcao = 'outgoing' THEN enviado_em END) as last_outgoing
            FROM messages
            WHERE conversation_id = %s
        """, (conversation_id,))

        row = cursor.fetchone()
        if not row or not row['last_incoming']:
            return None

        last_incoming = row['last_incoming']
        last_outgoing = row['last_outgoing']

        # Se não tem resposta ou última resposta é antes do último incoming
        if not last_outgoing or last_outgoing < last_incoming:
            days = (datetime.now() - last_incoming).days
            return days

        return None

    # =========================================================================
    # KEYWORDS
    # =========================================================================

    def detect_keywords(self, subject: str, body: str) -> List[str]:
        """Detecta keywords relevantes no email"""
        text = f"{subject} {body}".lower()
        detected = []

        all_keywords = {
            'urgente': URGENT_KEYWORDS,
            'reunião': MEETING_KEYWORDS,
            'proposta': MONEY_KEYWORDS,
            'pergunta': QUESTION_KEYWORDS,
            'followup': FOLLOWUP_KEYWORDS
        }

        for category, keywords in all_keywords.items():
            for kw in keywords:
                if kw.lower() in text:
                    if category not in detected:
                        detected.append(category)
                    break

        return detected

    def _suggest_actions(self, subject: str, body: str, keywords: List[str]) -> List[Dict]:
        """Sugere ações baseadas no conteúdo do email"""
        actions = []
        text = f"{subject} {body}".lower()

        # Sempre sugerir responder para emails importantes
        actions.append({
            "type": "respond",
            "reason": "Responder ao email"
        })

        # Se menciona reunião, sugerir agendar
        if 'reunião' in keywords or any(kw in text for kw in MEETING_KEYWORDS):
            actions.append({
                "type": "schedule",
                "reason": "Menciona reunião ou agenda"
            })

        # Se é proposta ou orçamento, sugerir criar projeto
        if 'proposta' in keywords or any(kw in text for kw in MONEY_KEYWORDS[:3]):
            actions.append({
                "type": "project",
                "reason": "Relacionado a proposta/negócio"
            })

        # Sempre sugerir criar tarefa como opção
        actions.append({
            "type": "task",
            "reason": "Criar lembrete/tarefa"
        })

        return actions

    # =========================================================================
    # LISTAGEM E FILTROS
    # =========================================================================

    def get_triage_list(
        self,
        status: str = 'pending',
        account_type: str = None,
        classification: str = None,
        limit: int = 50,
        offset: int = 0
    ) -> Dict:
        """Lista emails para triagem com filtros"""
        with get_db() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    et.*,
                    m.conteudo, m.metadata, m.enviado_em,
                    m.metadata->>'from_name' as from_name_meta,
                    m.metadata->>'from' as from_email_meta,
                    c.assunto, c.ultimo_mensagem,
                    ct.nome as contact_nome, ct.empresa, ct.foto_url,
                    ct.circulo, ct.circulo_pessoal, ct.circulo_profissional
                FROM email_triage et
                LEFT JOIN messages m ON m.id = et.message_id
                LEFT JOIN conversations c ON c.id = et.conversation_id
                LEFT JOIN contacts ct ON ct.id = et.contact_id
                WHERE 1=1
            """
            params = []

            if status:
                query += " AND et.status = %s"
                params.append(status)

            if account_type:
                query += " AND et.account_type = %s"
                params.append(account_type)

            if classification:
                query += " AND et.classification = %s"
                params.append(classification)

            query += " ORDER BY et.priority DESC, et.criado_em DESC"
            query += " LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            cursor.execute(query, params)
            items = [_normalize_email_dates(dict(row)) for row in cursor.fetchall()]

            # Contar total
            count_query = """
                SELECT COUNT(*) as total FROM email_triage et WHERE 1=1
            """
            count_params = []
            if status:
                count_query += " AND et.status = %s"
                count_params.append(status)
            if account_type:
                count_query += " AND et.account_type = %s"
                count_params.append(account_type)
            if classification:
                count_query += " AND et.classification = %s"
                count_params.append(classification)

            cursor.execute(count_query, count_params)
            result = cursor.fetchone()
            total = result['total'] if result else 0

            return {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset
            }

    def get_stats(self) -> Dict:
        """Retorna estatísticas da triagem"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Counts by status (all)
            cursor.execute("""
                SELECT status, COUNT(*) as count
                FROM email_triage
                WHERE criado_em > NOW() - INTERVAL '30 days'
                GROUP BY status
            """)
            status_rows = cursor.fetchall()

            # Counts by classification (only pending - actionable items)
            cursor.execute("""
                SELECT classification, COUNT(*) as count
                FROM email_triage
                WHERE status = 'pending'
                GROUP BY classification
            """)
            class_rows = cursor.fetchall()

            stats = {
                "by_status": {},
                "by_classification": {},
                "total_pending": 0,
                "total_approved": 0,
                "total_actioned": 0
            }

            for row in status_rows:
                stats["by_status"][row['status']] = row['count']

            for row in class_rows:
                if row['classification']:
                    stats["by_classification"][row['classification']] = row['count']

            stats["total_pending"] = stats["by_status"].get("pending", 0)
            stats["total_approved"] = stats["by_status"].get("approved", 0)
            stats["total_actioned"] = stats["by_status"].get("actioned", 0)

            return stats

    # =========================================================================
    # APROVAÇÃO E AÇÕES
    # =========================================================================

    def approve_batch(self, triage_ids: List[int], tags: List[str] = None) -> Dict:
        """Aprova múltiplos emails em lote"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE email_triage
                SET status = 'approved',
                    approved_tags = %s,
                    approved_at = NOW()
                WHERE id = ANY(%s)
                AND status = 'pending'
                RETURNING id
            """, (json.dumps(tags) if tags else None, triage_ids))

            updated = cursor.fetchall()
            conn.commit()

            return {
                "approved": len(updated),
                "ids": [r['id'] for r in updated]
            }

    def dismiss_batch(self, triage_ids: List[int], reason: str = None) -> Dict:
        """Descarta múltiplos emails"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE email_triage
                SET status = 'dismissed',
                    dismissed_at = NOW()
                WHERE id = ANY(%s)
                AND status = 'pending'
                RETURNING id
            """, (triage_ids,))

            updated = cursor.fetchall()
            conn.commit()

            return {
                "dismissed": len(updated),
                "ids": [r['id'] for r in updated]
            }

    def mark_actioned(self, triage_id: int, action: str) -> Dict:
        """Marca email como tendo ação tomada"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE email_triage
                SET status = 'actioned',
                    action_taken = %s,
                    actioned_at = NOW()
                WHERE id = %s
                RETURNING id, contact_id
            """, (action, triage_id))

            row = cursor.fetchone()

            # Atualizar ultimo_contato do contato se tiver ação
            if row and row['contact_id']:
                cursor.execute("""
                    UPDATE contacts
                    SET ultimo_contato = NOW()
                    WHERE id = %s
                """, (row['contact_id'],))

            conn.commit()

            return {"success": bool(row), "id": triage_id}

    # =========================================================================
    # REGRAS
    # =========================================================================

    def get_rules_list(self) -> List[Dict]:
        """Lista todas as regras de classificação"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM email_triage_rules
                ORDER BY ordem ASC, criado_em ASC
            """)
            return [dict(r) for r in cursor.fetchall()]

    def create_rule(self, data: Dict) -> Dict:
        """Cria nova regra de classificação"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO email_triage_rules (
                    nome, descricao, conditions,
                    auto_classify, auto_tags, auto_priority,
                    requires_approval, ativo, ordem
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                data.get('nome'),
                data.get('descricao'),
                json.dumps(data.get('conditions', {})),
                data.get('auto_classify'),
                json.dumps(data.get('auto_tags', [])),
                data.get('auto_priority', 5),
                data.get('requires_approval', True),
                data.get('ativo', True),
                data.get('ordem', 100)
            ))

            rule = dict(cursor.fetchone())
            conn.commit()

            return rule

    def update_rule(self, rule_id: int, data: Dict) -> Dict:
        """Atualiza regra existente"""
        with get_db() as conn:
            cursor = conn.cursor()

            updates = []
            params = []

            for field in ['nome', 'descricao', 'auto_classify', 'auto_priority', 'requires_approval', 'ativo', 'ordem']:
                if field in data:
                    updates.append(f"{field} = %s")
                    params.append(data[field])

            if 'conditions' in data:
                updates.append("conditions = %s")
                params.append(json.dumps(data['conditions']))

            if 'auto_tags' in data:
                updates.append("auto_tags = %s")
                params.append(json.dumps(data['auto_tags']))

            if not updates:
                return {"error": "No fields to update"}

            params.append(rule_id)

            cursor.execute(f"""
                UPDATE email_triage_rules
                SET {', '.join(updates)}
                WHERE id = %s
                RETURNING *
            """, params)

            rule = cursor.fetchone()
            conn.commit()

            return dict(rule) if rule else {"error": "Rule not found"}

    def delete_rule(self, rule_id: int) -> Dict:
        """Deleta regra"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM email_triage_rules WHERE id = %s RETURNING id", (rule_id,))
            deleted = cursor.fetchone()
            conn.commit()
            return {"deleted": bool(deleted)}

    def init_default_rules(self) -> Dict:
        """Inicializa regras default se não existirem"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) as total FROM email_triage_rules")
            result = cursor.fetchone()
            count = result['total'] if result else 0

            if count > 0:
                return {"message": "Rules already exist", "count": count}

            created = 0
            for i, rule in enumerate(DEFAULT_RULES):
                cursor.execute("""
                    INSERT INTO email_triage_rules (
                        nome, conditions, auto_classify, auto_tags,
                        auto_priority, requires_approval, ordem
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    rule['nome'],
                    json.dumps(rule['conditions']),
                    rule.get('auto_classify'),
                    json.dumps(rule.get('auto_tags', [])),
                    rule.get('auto_priority', 5),
                    rule.get('requires_approval', True),
                    (i + 1) * 10
                ))
                created += 1

            conn.commit()
            return {"created": created}

    async def sync_labeled_emails(self, label_name: str = "!!Renato") -> Dict:
        """
        Importa emails que já têm uma label específica no Gmail
        para a triagem do sistema.
        """
        from integrations.gmail import GmailIntegration

        stats = {
            "accounts_checked": 0,
            "emails_found": 0,
            "emails_imported": 0,
            "emails_skipped": 0,
            "errors": []
        }

        gmail = GmailIntegration()

        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar todas as contas Google
            cursor.execute("""
                SELECT email, access_token, refresh_token, tipo
                FROM google_accounts
                WHERE access_token IS NOT NULL
            """)
            accounts = cursor.fetchall()

            for account in accounts:
                stats["accounts_checked"] += 1
                account_email = account['email']
                access_token = account['access_token']
                account_type = account.get('tipo', 'professional')

                try:
                    # Buscar emails com a label
                    query = f"label:{label_name}"
                    result = await gmail.list_messages(access_token, query=query, max_results=100)

                    if "error" in result:
                        # Tentar refresh token
                        if result.get("error") == "token_expired":
                            refresh_result = await gmail.refresh_access_token(account['refresh_token'])
                            if "access_token" in refresh_result:
                                access_token = refresh_result["access_token"]
                                # Atualizar token no banco
                                cursor.execute("""
                                    UPDATE google_accounts SET access_token = %s WHERE email = %s
                                """, (access_token, account_email))
                                conn.commit()
                                result = await gmail.list_messages(access_token, query=query, max_results=100)
                            else:
                                stats["errors"].append(f"{account_email}: Token refresh failed")
                                continue
                        else:
                            stats["errors"].append(f"{account_email}: {result.get('error')}")
                            continue

                    messages = result.get("messages", [])
                    stats["emails_found"] += len(messages)

                    for msg_ref in messages:
                        gmail_id = msg_ref.get("id")

                        # Verificar se já existe na triagem
                        cursor.execute("""
                            SELECT et.id, et.message_id FROM email_triage et
                            JOIN messages m ON m.id = et.message_id
                            WHERE m.external_id = %s
                        """, (gmail_id,))
                        existing = cursor.fetchone()

                        if existing:
                            # Atualizar metadata se from_name está faltando
                            cursor.execute("""
                                SELECT metadata->>'from_name' as from_name FROM messages WHERE id = %s
                            """, (existing['message_id'],))
                            msg_meta = cursor.fetchone()
                            if not msg_meta or not msg_meta.get('from_name'):
                                # Buscar detalhes do email para obter from_name
                                msg_details = await gmail.get_message(access_token, gmail_id)
                                if "error" not in msg_details:
                                    headers = gmail.parse_message_headers(msg_details)
                                    from_header = headers.get("from", "")
                                    from_email = gmail.extract_email_address(from_header)
                                    from_name = from_header.split('<')[0].strip().strip('"') if '<' in from_header else from_email
                                    cursor.execute("""
                                        UPDATE messages
                                        SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                                        WHERE id = %s
                                    """, (
                                        json.dumps({"from": from_email, "from_name": from_name}),
                                        existing['message_id']
                                    ))
                            stats["emails_skipped"] += 1
                            continue

                        # Buscar detalhes do email
                        msg_details = await gmail.get_message(access_token, gmail_id)
                        if "error" in msg_details:
                            continue

                        headers = gmail.parse_message_headers(msg_details)
                        body_data = gmail.parse_message_body(msg_details)

                        from_header = headers.get("from", "")
                        from_email = gmail.extract_email_address(from_header)
                        # Extrair nome do remetente (parte antes do <email>)
                        from_name = from_header.split('<')[0].strip().strip('"') if '<' in from_header else from_email
                        subject = headers.get("subject", "(Sem assunto)")
                        date_str = headers.get("date", "")

                        # Buscar contato pelo email (formato: [{'email': 'addr@example.com'}])
                        cursor.execute("""
                            SELECT id, nome, circulo, circulo_pessoal, circulo_profissional
                            FROM contacts
                            WHERE emails @> %s::jsonb
                            LIMIT 1
                        """, (json.dumps([{'email': from_email}]),))
                        contact = cursor.fetchone()

                        # Se não encontrou, tentar busca mais flexível
                        if not contact and from_email:
                            cursor.execute("""
                                SELECT id, nome, circulo, circulo_pessoal, circulo_profissional
                                FROM contacts
                                WHERE emails::text ILIKE %s
                                LIMIT 1
                            """, (f'%{from_email}%',))
                            contact = cursor.fetchone()

                        contact_id = contact['id'] if contact else None

                        # Verificar se mensagem já existe
                        cursor.execute("""
                            SELECT id, conversation_id FROM messages WHERE external_id = %s
                        """, (gmail_id,))
                        existing_msg = cursor.fetchone()

                        if existing_msg:
                            message_id = existing_msg['id']
                            conversation_id = existing_msg['conversation_id']
                            # Atualizar metadata com from_name se não existir
                            cursor.execute("""
                                UPDATE messages
                                SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                                WHERE id = %s AND (metadata->>'from_name' IS NULL)
                            """, (
                                json.dumps({"from": from_email, "from_name": from_name}),
                                message_id
                            ))
                        else:
                            # Criar conversa
                            cursor.execute("""
                                INSERT INTO conversations (contact_id, canal, assunto, status)
                                VALUES (%s, 'email', %s, 'active')
                                RETURNING id
                            """, (contact_id, subject))
                            conversation_id = cursor.fetchone()['id']

                            # Criar mensagem
                            from email.utils import parsedate_to_datetime
                            try:
                                sent_at = parsedate_to_datetime(date_str) if date_str else None
                            except:
                                sent_at = None

                            body_text, body_html = extract_email_body(body_data)
                            cursor.execute("""
                                INSERT INTO messages (
                                    conversation_id, contact_id, external_id, direcao,
                                    conteudo, conteudo_html, metadata, enviado_em
                                ) VALUES (%s, %s, %s, 'incoming', %s, %s, %s, %s)
                                RETURNING id
                            """, (
                                conversation_id,
                                contact_id,
                                gmail_id,
                                body_text,
                                body_html or None,
                                json.dumps({
                                    "account": account_email,
                                    "from": from_email,
                                    "from_name": from_name,
                                    "subject": subject
                                }),
                                sent_at
                            ))
                            message_id = cursor.fetchone()['id']

                        # Determinar classificação
                        classification = "important"
                        priority = 7
                        if contact:
                            circulo = contact.get('circulo')
                            if circulo == 1:
                                classification = "urgent"
                                priority = 10
                            elif circulo == 2:
                                classification = "important"
                                priority = 8

                        # Criar registro de triagem
                        cursor.execute("""
                            INSERT INTO email_triage (
                                message_id, conversation_id, contact_id,
                                needs_attention, priority, classification,
                                classification_reasons, suggested_tags,
                                status, account_type
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                        """, (
                            message_id,
                            conversation_id,
                            contact_id,
                            True,
                            priority,
                            classification,
                            json.dumps([f"Importado do Gmail (label {label_name})"]),
                            json.dumps([label_name]),
                            'pending',
                            account_type
                        ))

                        stats["emails_imported"] += 1

                    conn.commit()

                except Exception as e:
                    import traceback
                    stats["errors"].append(f"{account_email}: {str(e)}")
                    print(f"Error syncing {account_email}: {traceback.format_exc()}")

        return stats


# =============================================================================
# SWEEP CRON (multi-account, 30min)
# =============================================================================

def _short_account(email: str) -> str:
    """Render conta como 'pro' (renato@almeida-prado.com) ou 'pess' (gmail)."""
    if not email:
        return "?"
    if "almeida-prado.com" in email:
        return "pro"
    if "gmail.com" in email:
        return "pess"
    return email.split("@")[0][:6]


async def _fetch_recent_messages_for_account(
    gmail_integration,
    account: Dict,
    hours: int,
) -> List[Dict]:
    """Busca Gmail message refs das ultimas N horas pra UMA conta.

    Retorna [{gmail_id, account_email, account_type, access_token}, ...].
    Em erro, retorna [] (toleranca: 1 conta nao derruba outra).
    """
    refs: List[Dict] = []
    account_email = account.get("email")
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        logger.warning(f"sweep: conta {account_email} sem refresh_token")
        return refs

    # Refresh token sempre (mais seguro que tentar usar access_token
    # potencialmente expirado).
    try:
        refresh_result = await gmail_integration.refresh_access_token(refresh_token)
    except Exception as e:
        logger.warning(f"sweep: refresh {account_email} falhou: {e}")
        return refs

    if "access_token" not in refresh_result:
        logger.warning(f"sweep: refresh {account_email} sem access_token: {refresh_result}")
        return refs
    access_token = refresh_result["access_token"]

    # Persistir access_token novo (best-effort, nao bloqueia)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE google_accounts SET access_token = %s WHERE email = %s",
                (access_token, account_email),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"sweep: persist access_token {account_email} falhou: {e}")

    # Gmail query: SO inbox, ultimas N horas (25/06/2026: in:inbox filtra
    # emails que filtros nativos do Gmail ja arquivaram, evitando classificar
    # ruido conhecido). newer_than:Xh tem granularidade de horas; >24h vai pra dias.
    if hours <= 1:
        time_q = "newer_than:1h"
    elif hours <= 24:
        time_q = f"newer_than:{hours}h"
    else:
        days = max(1, hours // 24)
        time_q = f"newer_than:{days}d"
    query = f"in:inbox {time_q}"

    try:
        result = await gmail_integration.list_messages(
            access_token=access_token,
            query=query,
            max_results=100,
        )
    except Exception as e:
        logger.warning(f"sweep: list_messages {account_email} falhou: {e}")
        return refs

    if "error" in result:
        logger.warning(f"sweep: list_messages {account_email} error: {result.get('error')[:200] if isinstance(result.get('error'), str) else result.get('error')}")
        return refs

    for msg_ref in result.get("messages", []) or []:
        refs.append({
            "gmail_id": msg_ref.get("id"),
            "thread_id": msg_ref.get("threadId"),
            "account_email": account_email,
            "account_type": account.get("tipo", "professional"),
            "access_token": access_token,
        })
    return refs


def _ensure_message_row(
    cursor,
    gmail_id: str,
    headers: Dict,
    body: Dict,
    account_email: str,
    gmail_integration,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Garante que existe linha em messages pra esse gmail_id.

    Retorna (message_id, conversation_id, contact_id) ou (None,...) se falhar.
    Idempotente: se ja existe, retorna os ids existentes (NAO duplica).
    """
    cursor.execute(
        "SELECT id, conversation_id, contact_id FROM messages WHERE external_id = %s",
        (gmail_id,),
    )
    existing = cursor.fetchone()
    if existing:
        return existing["id"], existing["conversation_id"], existing["contact_id"]

    from_header = headers.get("from", "") or ""
    from_email = gmail_integration.extract_email_address(from_header)
    from_name = (
        from_header.split("<")[0].strip().strip('"')
        if "<" in from_header else from_email
    )
    subject = headers.get("subject", "(Sem assunto)")
    date_str = headers.get("date", "")

    # Resolver contato por email (formato JSON [{'email': '...'}])
    contact_id = None
    if from_email:
        cursor.execute(
            """
            SELECT id FROM contacts
            WHERE emails @> %s::jsonb
            LIMIT 1
            """,
            (json.dumps([{"email": from_email}]),),
        )
        c = cursor.fetchone()
        if c:
            contact_id = c["id"]
        else:
            # Busca menos estrita
            cursor.execute(
                "SELECT id FROM contacts WHERE emails::text ILIKE %s LIMIT 1",
                (f"%{from_email}%",),
            )
            c = cursor.fetchone()
            if c:
                contact_id = c["id"]

    # Criar conversation
    cursor.execute(
        """
        INSERT INTO conversations (contact_id, canal, assunto, status)
        VALUES (%s, 'email', %s, 'active')
        RETURNING id
        """,
        (contact_id, subject[:500]),
    )
    conversation_id = cursor.fetchone()["id"]

    # Parse data
    sent_at = None
    if date_str:
        try:
            from email.utils import parsedate_to_datetime
            sent_at = parsedate_to_datetime(date_str)
            if sent_at and sent_at.tzinfo:
                sent_at = sent_at.replace(tzinfo=None)
        except Exception:
            pass

    body_text, body_html = extract_email_body(body)
    cursor.execute(
        """
        INSERT INTO messages (
            conversation_id, contact_id, external_id, direcao,
            conteudo, conteudo_html, metadata, enviado_em
        ) VALUES (%s, %s, %s, 'incoming', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            conversation_id,
            contact_id,
            gmail_id,
            body_text,
            body_html or None,
            json.dumps({
                "account": account_email,
                "from": from_email,
                "from_name": from_name,
                "subject": subject,
            }),
            sent_at,
        ),
    )
    message_id = cursor.fetchone()["id"]
    return message_id, conversation_id, contact_id


async def sweep_email_triage(hours: int = 1) -> Dict:
    """Sweep emails recentes de TODAS contas Google conectadas, classifica
    via regras e insere em email_triage (idempotente por external_id).

    Roteia cada email pros 4 buckets (20/07/2026, age-ja):
      - must_read       -> !!Renato / !Andressa / Financeiro (MANTEM inbox).
      - R4 archive       -> Arquivar: remove INBOX, sem label (conf>=0.85).
      - R5/R6 archive    -> !!Deletar: label + remove INBOX (conf>=0.85).
      - silent / incerto -> no-op (fica no inbox pro Renato ver).

    Args:
        hours: janela de busca pra tras (default 1h pra cron 30min).

    Returns:
        {
            ok: bool,
            processed: int,
            by_account: {email: count},
            by_classification: {must_read: N, archive_proposed: N, silent: N},
            label_applied: int,
            auto_archived: int,          # emails removidos do inbox (arquivar+deletar)
            bucket_arquivar: int,        # R4 arquivados
            bucket_deletar: int,         # R5/R6 -> !!Deletar
            shadow_proposals: int,       # archive_proposed incerto (nao agido)
            label_skipped_cap: int,
            errors: [str],
            duration_ms: int
        }
    """
    import time
    started = time.time()
    from integrations.gmail import GmailIntegration

    stats: Dict = {
        "ok": True,
        "processed": 0,
        "by_account": {},
        "by_classification": {},
        "label_applied": 0,
        "label_skipped_cap": 0,
        "shadow_proposals": 0,
        "errors": [],
        "duration_ms": 0,
    }

    gmail = GmailIntegration()
    service = get_email_triage_service()

    # 1. Lista contas conectadas
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, email, tipo, access_token, refresh_token
                FROM google_accounts
                WHERE conectado = TRUE
                """
            )
            accounts = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        stats["ok"] = False
        stats["errors"].append(f"list_accounts: {e}")
        stats["duration_ms"] = int((time.time() - started) * 1000)
        return stats

    if not accounts:
        stats["errors"].append("no_connected_accounts")
        stats["duration_ms"] = int((time.time() - started) * 1000)
        return stats

    # 2. Fetch refs por conta (toleranca: 1 falha nao trava outra)
    all_refs: List[Dict] = []
    for acc in accounts:
        try:
            refs = await _fetch_recent_messages_for_account(gmail, acc, hours)
            stats["by_account"][acc["email"]] = len(refs)
            all_refs.extend(refs)
        except Exception as e:
            stats["errors"].append(f"fetch_{acc['email']}: {e}")
            stats["by_account"][acc["email"]] = 0

    # 3. Cap diario de labels (estado em agent_actions)
    labels_today = 0
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM agent_actions
                WHERE action_type = 'gmail_label_add'
                  AND criado_em > NOW() - INTERVAL '1 day'
                """
            )
            r = cur.fetchone()
            labels_today = (r["n"] if r else 0) or 0
    except Exception:
        pass

    # 4. Processa cada ref
    for ref in all_refs:
        gmail_id = ref["gmail_id"]
        if not gmail_id:
            continue

        try:
            # 4a. Idempotencia: ja temos triage pra esse gmail_id?
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT et.id FROM email_triage et
                    JOIN messages m ON m.id = et.message_id
                    WHERE m.external_id = %s
                    LIMIT 1
                    """,
                    (gmail_id,),
                )
                if cursor.fetchone():
                    continue  # ja processado, skip

            # 4b. Fetch detalhes do email
            msg_details = await gmail.get_message(ref["access_token"], gmail_id)
            if "error" in msg_details:
                stats["errors"].append(f"get_msg {gmail_id[:12]}: {str(msg_details.get('error'))[:80]}")
                continue

            headers = gmail.parse_message_headers(msg_details)
            body = gmail.parse_message_body(msg_details)
            label_ids = msg_details.get("labelIds") or []

            # 4b-bis. Thread-aware (feedback 21/07 — action-blindness): se a
            # ULTIMA msg do thread ja e do Renato (ele respondeu), o item esta
            # resolvido. Nao sugere resposta/must_read; arquiva + tira !!Renato.
            thread_resolved = await _thread_last_is_outbound(
                gmail, ref["access_token"], msg_details.get("threadId"),
                ref["account_email"],
            )

            # 4c. Garante row em messages + resolve contact
            with get_db() as conn:
                cursor = conn.cursor()
                msg_id, conv_id, contact_id = _ensure_message_row(
                    cursor, gmail_id, headers, body, ref["account_email"], gmail,
                )
                if not msg_id:
                    conn.rollback()
                    continue
                conn.commit()

            # 4d. Classificar (CoS-aware — Commit 2 estende isso)
            decision = service.classify_email_cos(
                headers=headers,
                body_text=body.get("text") or "",
                gmail_label_ids=label_ids,
                account_email=ref["account_email"],
                account_type=ref["account_type"],
                contact_id=contact_id,
            )

            classification = decision["classification"]
            confidence = float(decision.get("ai_confidence") or 0.5)
            stats["by_classification"][classification] = \
                stats["by_classification"].get(classification, 0) + 1

            # 4e. Dedup por (sender, subject_norm) em 24h. Caso 16/06 Vallen
            # Pauta Conselho: 8 cópias identicas com priority=10. Mata burst.
            sender_for_dedup = (headers.get("from") or "").strip()
            subj_norm = re.sub(
                r"^\s*(re|fwd?|res|fw)\s*:\s*",
                "",
                (headers.get("subject") or "").strip().lower(),
                flags=re.IGNORECASE,
            )
            dup_status = "pending"
            dup_reason = None
            if sender_for_dedup and subj_norm:
                with get_db() as _conn:
                    _cur = _conn.cursor()
                    _cur.execute(
                        """
                        SELECT et.id
                        FROM email_triage et
                        LEFT JOIN messages m ON m.id = et.message_id
                        LEFT JOIN conversations c ON c.id = et.conversation_id
                        WHERE et.account_email = %s
                          AND et.criado_em > NOW() - INTERVAL '24 hours'
                          AND (m.metadata->>'from') = %s
                          AND REGEXP_REPLACE(LOWER(COALESCE(c.assunto, '')), '^\\s*(re|fwd?|res|fw)\\s*:\\s*', '', 'i') = %s
                          AND et.status != 'dismissed'
                        LIMIT 1
                        """,
                        (ref["account_email"], sender_for_dedup, subj_norm),
                    )
                    dup_row = _cur.fetchone()
                    if dup_row:
                        dup_status = "dismissed"
                        dup_reason = f"duplicate_of_{dup_row['id']}_in_24h"
                        stats["dedup_skipped"] = stats.get("dedup_skipped", 0) + 1

            # 4e-bis. Inserir em email_triage (UPSERT por message_id)
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO email_triage (
                        message_id, conversation_id, contact_id,
                        needs_attention, priority, classification,
                        classification_reasons, suggested_tags, suggested_actions,
                        status, account_type, account_email, ai_confidence,
                        action_taken, expires_at
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, NOW() + INTERVAL '14 days'
                    )
                    ON CONFLICT (message_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        msg_id,
                        conv_id,
                        contact_id,
                        classification in ("must_read",) and dup_status == "pending" and not thread_resolved,
                        decision.get("priority", 5),
                        classification,
                        json.dumps(decision.get("reasons", [])),
                        json.dumps(decision.get("suggested_tags", [])),
                        json.dumps(decision.get("suggested_actions", [])),
                        dup_status,
                        ref["account_type"],
                        ref["account_email"],
                        confidence,
                        dup_reason,
                    ),
                )
                row = cursor.fetchone()
                triage_id = row["id"] if row else None
                conn.commit()

            # Skip downstream actions se for duplicata
            if dup_status == "dismissed":
                continue

            if not triage_id:
                continue  # conflict — outra conta processou primeiro

            stats["processed"] += 1

            # 4f-pre. THREAD-AWARE: se o Renato ja respondeu (ultima msg do
            # thread e outbound), o item esta RESOLVIDO. Nao aplica !!Renato
            # nem propoe resposta — arquiva e tira !!Renato (reuse do caminho
            # arquivar+strip). So age quando _thread_last_is_outbound CONFIRMOU
            # o outbound; nunca mexe em thread cujo ultimo turno e do terceiro.
            if thread_resolved:
                try:
                    archived_ok = await gmail.archive_message(
                        ref["access_token"], gmail_id
                    )
                    await _remove_renato_label(gmail, ref["access_token"], gmail_id)
                    if archived_ok:
                        stats["thread_resolved_archived"] = (
                            stats.get("thread_resolved_archived", 0) + 1
                        )
                except Exception as e:
                    stats["errors"].append(
                        f"thread_resolved {gmail_id[:12]}: {str(e)[:80]}"
                    )
                continue

            # 4f. Aplicar label apropriado se must_read + conf >= 0.85
            # 25/06/2026: roteamento por suggested_tags em vez de sempre !!Renato.
            # - tags inclui !Andressa/andressa/admin -> !Andressa
            # - tags inclui financeiro/billing/infra-critico -> !Recibos/Financeiro
            #   (ou 7-Financeiro/Recibos profissional). infra-critico ALEM disso ganha !!Renato.
            # - resto must_read -> !!Renato (default)
            if classification == "must_read" and confidence >= 0.85:
                labels_to_apply = route_mustread_labels(
                    decision.get("suggested_tags"), ref["account_email"]
                )

                for label_name in labels_to_apply:
                    if labels_today >= MAX_LABELS_PER_DAY:
                        stats["label_skipped_cap"] += 1
                        break
                    if label_name in [l for l in label_ids if isinstance(l, str)]:
                        continue  # ja aplicado
                    try:
                        label_result = await _apply_generic_label(
                            gmail, ref["access_token"], gmail_id,
                            ref["account_email"], triage_id, msg_id, label_name,
                        )
                        if label_result.get("applied"):
                            stats["label_applied"] += 1
                            labels_today += 1
                    except Exception as e:
                        stats["errors"].append(f"label {label_name} {gmail_id[:12]}: {str(e)[:80]}")

            # 4f-bis. WA push pra urgents (24/06/2026) — priority>=9 sao
            # imprensa/jornalista (R1) ou C1 contato (R2). Notifica Renato
            # imediatamente via notification_router (score 8 => WhatsApp na
            # instancia rap-whatsapp). O comentario antigo dizia "intel-bot-v2
            # (instancia de notificacoes CoS)" — caminho que nao existe mais;
            # quem entrega e o `notify` abaixo (corrigido 27/07).
            #
            # ESTE e o caminho canonico do e-mail URGENTE (one-shot por
            # gmail_id). A camada esperta deixou de repassar "precisa de voce"
            # justamente pra nao duplicar isto — ver is_email_urgent_wa_enabled.
            # Kill-switch adicionado 27/07 (antes a condicao era hardcoded).
            priority_val = int(decision.get("priority", 5))
            if (classification == "must_read" and priority_val >= 9
                    and is_email_urgent_wa_enabled()):
                try:
                    from services.notification_router import notify
                    sender_short = (headers.get("from") or "")[:60]
                    subj_short = (headers.get("subject") or "(sem assunto)")[:70]
                    reasons_short = "; ".join(decision.get("reasons", [])[:2])[:120]
                    notif_text = (
                        f"📧 Email urgente (P{priority_val})\n"
                        f"De: {sender_short}\n"
                        f"Assunto: {subj_short}\n"
                        f"Por que: {reasons_short}"
                    )
                    # Score conforme prioridade: urgente(P>=9)->8 (WA), importante->6, senao 4.
                    # Na pratica este branch so roda com P>=9 => 8 => WhatsApp (preserva atual).
                    _urg = 8 if priority_val >= 9 else (6 if priority_val >= 7 else 4)
                    # topic = assunto INTEIRO do header, nao o `subj_short`:
                    # cada produtor trunca num tamanho diferente (70 aqui, 60 no
                    # _send_smart_summary), e comparar texto ja cortado faria o
                    # MESMO e-mail gerar chaves diferentes. O router normaliza e
                    # corta em 48, abaixo do menor corte em uso.
                    pushed = await notify(
                        "email_triage", f"Email urgente (P{priority_val})", notif_text, _urg,
                        msg_type="email_urgent", dedup=f"email_urgent:{gmail_id}",
                        topic=headers.get("subject"),
                    )
                    if pushed:
                        stats["wa_urgents_pushed"] = stats.get("wa_urgents_pushed", 0) + 1
                except Exception as e:
                    stats["errors"].append(f"wa_push {gmail_id[:12]}: {str(e)[:80]}")

            # 4g. Archive_proposed -> roteia pros buckets 4-bucket (age-ja,
            # 20/07/2026). Substitui o gate per-conta is_auto_archive_enabled:
            #   - R4 (noreply)       -> Arquivar: remove INBOX, SEM label.
            #   - R5/R6 (unsub/cold) -> !!Deletar: label !!Deletar + remove
            #                           INBOX (fila de exclusao; NUNCA deleta
            #                           de verdade — o Renato apaga em lote).
            #   - resto / conf<0.85  -> no-op (incerto): fica no inbox pro
            #                           Renato ver; registra shadow pra auditoria.
            # Idempotencia: shadow proposal por message_id + label idempotente.
            # Rede de seguranca: MAX_LABELS_PER_DAY limita acoes destrutivas/dia.
            if classification == "archive_proposed":
                acct_email = ref["account_email"]
                bucket = route_archive_bucket(decision.get("rule_hits"), confidence)
                try:
                    if bucket == "noop":
                        # Incerto — NAO mexe na inbox. Registro shadow p/ auditoria.
                        prop_result = await _create_shadow_proposal(
                            triage_id=triage_id,
                            gmail_id=gmail_id,
                            account_email=acct_email,
                            headers=headers,
                            decision=decision,
                        )
                        if prop_result.get("created"):
                            stats["shadow_proposals"] += 1
                    elif labels_today >= MAX_LABELS_PER_DAY:
                        # Cap diario batido — nao age (rede de seguranca).
                        stats["label_skipped_cap"] += 1
                    else:
                        # AGE: remove da inbox (+ label !!Deletar se bucket deletar).
                        if bucket == "deletar":
                            try:
                                lr = await _apply_generic_label(
                                    gmail, ref["access_token"], gmail_id,
                                    acct_email, triage_id, msg_id, "!!Deletar",
                                )
                                if lr.get("applied"):
                                    stats["label_applied"] += 1
                            except Exception as e:
                                stats["errors"].append(
                                    f"deletar_label {gmail_id[:12]}: {str(e)[:80]}"
                                )
                        try:
                            archived_ok = await gmail.archive_message(
                                ref["access_token"], gmail_id
                            )
                        except Exception as e:
                            archived_ok = False
                            stats["errors"].append(
                                f"archive {gmail_id[:12]}: {str(e)[:80]}"
                            )
                        # Feedback Renato (21/07): arquivar/deletar tem que TIRAR
                        # o email da lista de atencao — remove !!Renato tambem
                        # (no-op se ausente). Aditivo, nao muda a classificacao.
                        try:
                            await _remove_renato_label(
                                gmail, ref["access_token"], gmail_id
                            )
                        except Exception as e:
                            stats["errors"].append(
                                f"unlabel_renato {gmail_id[:12]}: {str(e)[:80]}"
                            )
                        if archived_ok:
                            labels_today += 1  # conta como acao destrutiva do dia
                            stats["auto_archived"] = stats.get("auto_archived", 0) + 1
                            stats[f"bucket_{bucket}"] = stats.get(f"bucket_{bucket}", 0) + 1
                        # Registro de auditoria (proposta -> archived).
                        prop_result = await _create_shadow_proposal(
                            triage_id=triage_id,
                            gmail_id=gmail_id,
                            account_email=acct_email,
                            headers=headers,
                            decision=decision,
                        )
                        if prop_result.get("created") and archived_ok:
                            with get_db() as _conn:
                                _cur = _conn.cursor()
                                _cur.execute(
                                    """
                                    UPDATE email_archive_proposals
                                    SET status = 'archived',
                                        ratified_at = NOW(),
                                        ratified_by = %s
                                    WHERE id = %s
                                    """,
                                    (f"auto_bucket_{bucket}", prop_result.get("proposal_id")),
                                )
                                _conn.commit()
                except Exception as e:
                    stats["errors"].append(f"bucket {gmail_id[:12]}: {str(e)[:80]}")

        except Exception as e:
            stats["errors"].append(f"process {gmail_id[:12]}: {str(e)[:120]}")
            logger.exception(f"sweep process {gmail_id}")

    stats["duration_ms"] = int((time.time() - started) * 1000)
    return stats


# Labels de financeiro (ambas contas) — usado pra mapear o bucket "financeiro"
# no apply_triage_to_inbox (route_mustread_labels devolve o nome por conta).
_FINANCEIRO_LABEL_NAMES = {"!Recibos/Financeiro", "7-Financeiro/Recibos"}


async def _fetch_inbox_messages_for_account(
    gmail_integration,
    account: Dict,
    limit: int,
) -> List[Dict]:
    """Busca refs de emails ATUALMENTE no INBOX (SEM janela de tempo) pra UMA
    conta. Diferente de _fetch_recent_messages_for_account (que filtra por
    newer_than) — aqui a query e so `in:inbox`, pra agir no BACKLOG do inbox.

    Retorna [{gmail_id, thread_id, account_email, account_type, access_token}, ...],
    ate `limit` (pagina de 100 em 100). Em erro, retorna [] (1 conta nao
    derruba a outra).
    """
    refs: List[Dict] = []
    account_email = account.get("email")
    refresh_token = account.get("refresh_token")
    if not refresh_token:
        logger.warning(f"inbox-scan: conta {account_email} sem refresh_token")
        return refs

    try:
        refresh_result = await gmail_integration.refresh_access_token(refresh_token)
    except Exception as e:
        logger.warning(f"inbox-scan: refresh {account_email} falhou: {e}")
        return refs

    if "access_token" not in refresh_result:
        logger.warning(f"inbox-scan: refresh {account_email} sem access_token: {refresh_result}")
        return refs
    access_token = refresh_result["access_token"]

    # Persistir access_token novo (best-effort)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE google_accounts SET access_token = %s WHERE email = %s",
                (access_token, account_email),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"inbox-scan: persist access_token {account_email} falhou: {e}")

    query = "in:inbox"
    page_token = None
    account_type = account.get("tipo", "professional")
    while len(refs) < limit:
        try:
            result = await gmail_integration.list_messages(
                access_token=access_token,
                query=query,
                max_results=min(100, limit - len(refs)),
                page_token=page_token,
            )
        except Exception as e:
            logger.warning(f"inbox-scan: list_messages {account_email} falhou: {e}")
            break
        if "error" in result:
            logger.warning(f"inbox-scan: list_messages {account_email} error: {result.get('error')}")
            break
        msgs = result.get("messages", []) or []
        for msg_ref in msgs:
            refs.append({
                "gmail_id": msg_ref.get("id"),
                "thread_id": msg_ref.get("threadId"),
                "account_email": account_email,
                "account_type": account_type,
                "access_token": access_token,
            })
            if len(refs) >= limit:
                break
        page_token = result.get("nextPageToken")
        if not page_token or not msgs:
            break
    return refs[:limit]


def _resolve_contact_id_by_email(from_email: str) -> Optional[int]:
    """Resolve contact_id pelo email do remetente (READ-ONLY). Espelha a
    query de _ensure_message_row SEM os side-effects (nao cria conversation
    nem messages) — o inbox-scan varre o backlog, nao quer poluir o DB.
    Precisa do contact_id pra R2 (circulo 1 -> !!Renato) classificar certo.
    """
    if not from_email:
        return None
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM contacts WHERE emails @> %s::jsonb LIMIT 1",
                (json.dumps([{"email": from_email}]),),
            )
            c = cur.fetchone()
            if c:
                return c["id"]
            cur.execute(
                "SELECT id FROM contacts WHERE emails::text ILIKE %s LIMIT 1",
                (f"%{from_email}%",),
            )
            c = cur.fetchone()
            if c:
                return c["id"]
    except Exception as e:
        logger.warning(f"inbox-scan: resolve_contact {from_email} falhou: {e}")
    return None


def _mustread_bucket_name(labels: List[str]) -> str:
    """Mapeia os labels de um must_read pro nome do bucket (stats)."""
    if "!Andressa" in labels:
        return "andressa"
    if any(l in _FINANCEIRO_LABEL_NAMES for l in labels):
        return "financeiro"
    return "renato"


async def apply_triage_to_inbox(
    account_email: Optional[str] = None,
    limit: int = 40,
    dry_run: bool = False,
    inbox_zero: Optional[bool] = None,
    calibration: Optional[bool] = None,
    smart: Optional[bool] = None,
) -> Dict:
    """Aplica a triagem 4-bucket no INBOX ATUAL (backlog), nao so em email novo.

    O sweep_email_triage age em email das ultimas N horas E pula quem ja tem
    row em email_triage (idempotencia). Resultado: o inbox EXISTENTE do Renato
    (classificado sob o fluxo antigo, que nao agia) nunca recebe roteamento.
    Esta funcao varre `in:inbox` (sem janela de tempo), classifica CADA email
    via classify_email_cos e roteia via resolve_inbox_action (puro/testavel),
    reusando as MESMAS chamadas Gmail (_apply_generic_label / archive_message).

    Dois modos (kill-switch `inbox_zero` / env INBOX_ZERO_ENABLED, default OFF):

    LEGADO (inbox_zero=False, preservador):
      - must_read       -> !!Renato / !Andressa / Financeiro (MANTEM inbox).
      - R4 (noreply)     -> arquivar: remove INBOX, sem label (conf>=0.85).
      - R5/R6 (unsub/cold)-> deletar: label !!Deletar + remove INBOX (conf>=0.85).
      - silent / incerto -> no-op (fica no inbox).

    INBOX-ZERO (inbox_zero=True — "corredor", tudo sai do inbox):
      - must_read       -> label do balde + ARQUIVA (sai do inbox). Financeiro
                           FUNDE em Andressa.
      - R4/R7-personal/RC4/RC5-sem-frente -> arquivar (sai, sem label).
      - R5/R6           -> !!Deletar + arquiva.
      - silent / incerto -> !!Renato + arquiva (vira FILA de revisao do Renato).
      - Novo !!Renato   -> aviso WhatsApp (so esse balde interrompe).

    NAO pula ja-triados (o alvo E o backlog). Idempotencia vem do Gmail:
    label ja presente = no-op, arquivo ja fora do inbox = skip.

    Args:
        account_email: se None, roda nas 2 contas conectadas; senao so nessa.
        limit: max de emails por conta (default 40).
        dry_run: se True, NAO age — devolve so o PREVIEW (per_email) do que faria.
        inbox_zero: None => le env INBOX_ZERO_ENABLED (default OFF); bool força.
        calibration: None => le env EMAIL_TRIAGE_CALIBRATION_ENABLED (default
                     OFF) dentro do classify; bool força (pra preview em dry_run).
        smart: None => le env INBOX_SMART_LAYER (default OFF); bool força. Liga a
                     CAMADA ESPERTA (aditiva): forward Andressa + triagem
                     inteligente do !!Renato (fecha/remarca task com undo+audit) +
                     resumo WhatsApp (<=1/dia BRT desde 27/07). Em dry_run NÃO
                     age — só reporta o que encaminharia/fecharia. Com OFF, roda
                     byte-a-byte como antes — atenção: OFF NÃO é "modo
                     silencioso", ele RESSUSCITA o ping individual por e-mail
                     (_notify_new_renato, suprimido pelo `and not sl` abaixo).

    Returns:
        {ok, processed, by_bucket:{renato,andressa,financeiro,arquivar,deletar,noop},
         acted, dry_run, inbox_zero_mode, per_email:[...], errors, by_account,
         label_skipped_cap, wa_renato_pushed, wa_renato_would_push, duration_ms}
    """
    import time
    started = time.time()
    from integrations.gmail import GmailIntegration

    iz = is_inbox_zero_enabled() if inbox_zero is None else bool(inbox_zero)
    sl = is_smart_layer_enabled() if smart is None else bool(smart)

    stats: Dict = {
        "ok": True,
        "processed": 0,
        "by_bucket": {
            "renato": 0, "andressa": 0, "financeiro": 0,
            "arquivar": 0, "deletar": 0, "noop": 0,
        },
        "acted": 0,
        "dry_run": bool(dry_run),
        "inbox_zero_mode": iz,
        "per_email": [],
        "errors": [],
        "by_account": {},
        "label_skipped_cap": 0,
        "wa_renato_pushed": 0,
        "wa_renato_would_push": 0,
        "duration_ms": 0,
    }
    # Camada esperta: SÓ adiciona chaves ao stats quando ligada (OFF => dict
    # de retorno byte-a-byte idêntico ao legado). Acumuladores por-run.
    smart_forwarded = 0
    smart_resolved_items: List[Dict] = []   # {task_id, action_label, reason, ...}
    smart_needs_you: List[Dict] = []        # {from, subject, reason}
    smart_project_index: List[Dict] = []
    smart_api_key = ""
    if sl:
        stats["smart_mode"] = True
        stats["smart_forwarded"] = 0
        stats["smart_would_forward"] = 0
        stats["smart_resolved"] = 0
        stats["smart_would_resolve"] = 0
        stats["smart_needs_you"] = 0
        smart_api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        # Índice de frentes 1x/run (reuso do signal_router — cruza email->projeto).
        try:
            from services.signal_router import build_project_index
            with get_db() as _conn:
                smart_project_index = build_project_index(_conn.cursor())
        except Exception as e:
            stats["errors"].append(f"smart_index: {str(e)[:80]}")

    gmail = GmailIntegration()
    service = get_email_triage_service()

    # 1. Contas (uma especifica ou todas conectadas)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            if account_email:
                cursor.execute(
                    """
                    SELECT id, email, tipo, access_token, refresh_token
                    FROM google_accounts
                    WHERE conectado = TRUE AND email = %s
                    """,
                    (account_email,),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, email, tipo, access_token, refresh_token
                    FROM google_accounts
                    WHERE conectado = TRUE
                    """
                )
            accounts = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        stats["ok"] = False
        stats["errors"].append(f"list_accounts: {e}")
        stats["duration_ms"] = int((time.time() - started) * 1000)
        return stats

    if not accounts:
        stats["errors"].append(
            f"account_not_connected:{account_email}" if account_email
            else "no_connected_accounts"
        )
        stats["duration_ms"] = int((time.time() - started) * 1000)
        return stats

    # 2. Cap diario de acoes (COMPARTILHADO com o sweep via agent_actions —
    # mesma rede de seguranca; inbox-scan nao explode alem de MAX_LABELS_PER_DAY).
    labels_today = 0
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM agent_actions
                WHERE action_type = 'gmail_label_add'
                  AND criado_em > NOW() - INTERVAL '1 day'
                """
            )
            r = cur.fetchone()
            labels_today = (r["n"] if r else 0) or 0
    except Exception:
        pass

    # 3. Processa por conta
    for acc in accounts:
        acct_email = acc["email"]
        acct_type = (acc.get("tipo") or "professional")
        try:
            refs = await _fetch_inbox_messages_for_account(gmail, acc, limit)
        except Exception as e:
            stats["errors"].append(f"fetch_{acct_email}: {e}")
            stats["by_account"][acct_email] = 0
            continue
        stats["by_account"][acct_email] = len(refs)
        if not refs:
            continue

        access_token = refs[0]["access_token"]
        # Mapa name->id da conta (idempotencia real: label ja presente = skip).
        try:
            labels_list = await gmail.list_labels(access_token)
            name_to_id = {l.get("name"): l.get("id") for l in (labels_list or []) if l.get("name")}
        except Exception:
            name_to_id = {}

        for ref in refs:
            gmail_id = ref["gmail_id"]
            if not gmail_id:
                continue
            entry: Dict = {"account": _short_account(acct_email)}
            try:
                msg_details = await gmail.get_message(ref["access_token"], gmail_id)
                if "error" in msg_details:
                    stats["errors"].append(
                        f"get_msg {gmail_id[:12]}: {str(msg_details.get('error'))[:80]}"
                    )
                    continue

                headers = gmail.parse_message_headers(msg_details)
                # parse_message_headers DROPA List-Unsubscribe (so mantem
                # from/to/subject/date). O classificador precisa dele pra R5
                # (unsub+comercial -> !!Deletar). Injetamos do payload cru —
                # NAO muda o mapeamento, so alimenta o header que faltava (o
                # sweep tem a mesma lacuna latente; aqui e o backlog que
                # precisa da precisao do bucket deletar).
                try:
                    for _h in (msg_details.get("payload", {}).get("headers", []) or []):
                        if (_h.get("name") or "").lower() == "list-unsubscribe":
                            headers["list-unsubscribe"] = _h.get("value", "")
                            break
                except Exception:
                    pass
                body = gmail.parse_message_body(msg_details)
                label_ids = [l for l in (msg_details.get("labelIds") or []) if isinstance(l, str)]

                # Thread-aware (feedback 21/07 — action-blindness): se a ultima
                # msg do thread ja e do Renato (ele respondeu), o item esta
                # resolvido — arquiva + tira !!Renato em vez de propor resposta.
                thread_resolved = await _thread_last_is_outbound(
                    gmail, ref["access_token"], msg_details.get("threadId"),
                    acct_email,
                )

                from_hdr = (headers.get("from") or "").strip()
                from_email = gmail.extract_email_address(from_hdr) if from_hdr else ""
                contact_id = _resolve_contact_id_by_email(from_email)

                decision = service.classify_email_cos(
                    headers=headers,
                    body_text=body.get("text") or "",
                    gmail_label_ids=label_ids,
                    account_email=acct_email,
                    account_type=acct_type,
                    contact_id=contact_id,
                    enable_calibration=calibration,
                )
                classification = decision["classification"]
                confidence = float(decision.get("ai_confidence") or 0.5)

                entry.update({
                    "from": from_hdr[:60],
                    "subject": (headers.get("subject") or "(sem assunto)")[:60],
                    "classification": classification,
                    "confidence": round(confidence, 2),
                    # Diagnosticabilidade (calibracao): POR QUE caiu nesse balde.
                    # Ganho permanente pro preview — quem auditar ve a regra que
                    # disparou (rule_hits) + a justificativa (reasons).
                    "rule_hits": list(decision.get("rule_hits") or []),
                    "reasons": list(decision.get("reasons") or []),
                })
                stats["processed"] += 1

                # ---- THREAD-AWARE: Renato ja respondeu -> RESOLVIDO ----
                # Se a ultima msg do thread e outbound, nao propoe resposta nem
                # !!Renato: arquiva + tira !!Renato (feedback 21/07). Precede o
                # roteamento normal; so age quando o outbound foi CONFIRMADO.
                if thread_resolved:
                    entry["bucket"] = "arquivar"
                    if "INBOX" not in label_ids:
                        entry["action"] = "skip:not_in_inbox"
                        stats["by_bucket"]["noop"] += 1
                    elif dry_run:
                        entry["action"] = "thread_resolved:archive+unlabel_renato"
                        stats["by_bucket"]["arquivar"] += 1
                    else:
                        try:
                            archived_ok = await gmail.archive_message(
                                ref["access_token"], gmail_id
                            )
                            await _remove_renato_label(
                                gmail, ref["access_token"], gmail_id,
                                renato_label_id=name_to_id.get("!!Renato"),
                            )
                        except Exception as e:
                            archived_ok = False
                            stats["errors"].append(
                                f"thread_resolved {gmail_id[:12]}: {str(e)[:80]}"
                            )
                        entry["action"] = "thread_resolved:archive+unlabel_renato"
                        stats["by_bucket"]["arquivar"] += 1
                        if archived_ok:
                            stats["acted"] += 1
                    stats["per_email"].append(entry)
                    continue

                # ---- Rota + acao (roteamento PURO via resolve_inbox_action) ----
                # Kill-switch iz decide LEGADO vs INBOX-ZERO. Executor unico:
                # aplica label(s) faltantes -> arquiva (se pedido) -> remove
                # !!Renato (arquivar/deletar) -> WA push se novo !!Renato.
                action = resolve_inbox_action(decision, acct_email, iz)
                bucket = action["bucket"]
                labels = action["labels"]
                entry["bucket"] = bucket

                # ================= CAMADA ESPERTA (aditiva, gated por sl) =========
                # A) !Andressa -> encaminha (idempotente por label). Aditivo: o
                #    roteamento normal (!Andressa/arquiva) segue depois.
                if sl and bucket == "andressa":
                    try:
                        fwd = await _smart_forward_to_andressa(
                            gmail, ref["access_token"], gmail_id, headers, body,
                            acct_email, label_ids, name_to_id, dry_run,
                        )
                    except Exception as e:
                        fwd = {"status": f"erro:{str(e)[:40]}"}
                        stats["errors"].append(f"smart_fwd {gmail_id[:12]}: {str(e)[:80]}")
                    entry["smart_forward"] = fwd.get("status")
                    if fwd.get("forwarded"):
                        smart_forwarded += 1
                        stats["smart_forwarded"] += 1
                    elif fwd.get("would"):
                        stats["smart_would_forward"] += 1

                # B) !!Renato -> triagem inteligente (o coração). Julga se o email
                #    RESOLVE uma task; se sim (conf>=barra + task casada), AGE
                #    (fecha/remarca via task_reconciler + nota) e tira !!Renato.
                #    Senão, mantém !!Renato e escala no resumo consolidado.
                if sl and bucket == "renato":
                    try:
                        outcome = await _smart_triage_renato(
                            gmail_id=gmail_id, headers=headers,
                            from_email=from_email, contact_id=contact_id,
                            body_text=(body.get("text") or ""),
                            project_index=smart_project_index,
                            api_key=smart_api_key,
                        )
                    except Exception as e:
                        stats["errors"].append(f"smart_renato {gmail_id[:12]}: {str(e)[:80]}")
                        outcome = {"resolved": False, "record": {
                            "from": from_hdr[:40],
                            "subject": (headers.get("subject") or "")[:60],
                            "reason": f"erro:{str(e)[:40]}",
                        }}

                    if outcome.get("resolved"):
                        rec = outcome["record"]
                        entry["smart"] = "resolved"
                        entry["smart_action"] = rec.get("action_label")
                        entry["smart_task_id"] = rec.get("task_id")
                        entry["bucket"] = "resolved"
                        if dry_run:
                            stats["smart_would_resolve"] += 1
                            smart_resolved_items.append(rec)
                            entry["action"] = (
                                f"would:{rec.get('action_label')} "
                                f"#{rec.get('task_id')}+unlabel_renato"
                            )
                            stats["per_email"].append(entry)
                            continue
                        # ---- AGE: fecha/remarca a task + nota durável ----
                        try:
                            _smart_apply_resolution(rec)
                            stats["smart_resolved"] += 1
                            stats["acted"] += 1
                            smart_resolved_items.append(rec)
                        except Exception as e:
                            stats["errors"].append(
                                f"smart_apply {gmail_id[:12]}: {str(e)[:80]}"
                            )
                        # Tira o email da fila !!Renato (não pinga).
                        try:
                            await _remove_renato_label(
                                gmail, ref["access_token"], gmail_id,
                                renato_label_id=name_to_id.get("!!Renato"),
                            )
                        except Exception as e:
                            stats["errors"].append(
                                f"smart_unlabel {gmail_id[:12]}: {str(e)[:80]}"
                            )
                        # Modo corredor (inbox-zero): resolvido também sai do inbox.
                        if iz and "INBOX" in label_ids:
                            try:
                                await gmail.archive_message(ref["access_token"], gmail_id)
                            except Exception:
                                pass
                        entry["action"] = (
                            f"resolved:{rec.get('action_label')} "
                            f"#{rec.get('task_id')}+unlabel_renato"
                        )
                        stats["per_email"].append(entry)
                        continue
                    else:
                        # Precisa de decisão / ambíguo / sem match: mantém !!Renato
                        # (fluxo normal abaixo). Ping individual é suprimido
                        # (consolidado no fim do run).
                        #
                        # GUARD 27/07 — REMETENTE AUTOMÁTICO NÃO ESCALA. Roda
                        # DEPOIS do julgamento de propósito: o julgamento pode
                        # FECHAR task (e para robô ele custa ~zero, porque
                        # _smart_triage_renato já curto-circuita sem task casada,
                        # antes do LLM). O guard só decide se o item pode
                        # INTERROMPER — nunca muda label/arquivamento, que são
                        # domínio do classificador (RC4-RC8).
                        _auto_reason = (
                            smart_automated_no_escalate(
                                from_email,
                                headers.get("subject") or "",
                                body.get("text") or "",
                                has_unsubscribe=bool(headers.get("list-unsubscribe")),
                            ) if is_smart_auto_sender_guard_enabled() else None
                        )
                        if _auto_reason:
                            entry["smart"] = "auto_sender_no_escalate"
                            entry["smart_reason"] = _auto_reason
                            stats["smart_auto_sender_skipped"] = (
                                stats.get("smart_auto_sender_skipped", 0) + 1
                            )
                        else:
                            entry["smart"] = "needs_you"
                            smart_needs_you.append(outcome["record"])
                            stats["smart_needs_you"] += 1
                            # O motivo e o estágio TÊM que sair na telemetria da
                            # run, não só no resumo: o resumo é WhatsApp, e o
                            # gate de silêncio por produtor (58e6ec1) rebaixou o
                            # inbox_smart pra push — desde 27/07 nenhum resumo
                            # dele chega ao Renato, então o "por quê" morria em
                            # memória. `cron_runs.result_json` é o que sobrevive.
                            _rec = outcome.get("record") or {}
                            entry["smart_reason"] = (_rec.get("reason") or "")[:160]
                            _stage = _rec.get("stage") or "unknown"
                            entry["smart_stage"] = _stage
                            _by_stage = stats.setdefault("smart_declined_by_stage", {})
                            _by_stage[_stage] = _by_stage.get(_stage, 0) + 1

                if bucket == "noop":
                    entry["action"] = (
                        "noop:uncertain" if classification == "archive_proposed"
                        else "noop:silent"
                    )
                    stats["by_bucket"]["noop"] += 1
                    stats["per_email"].append(entry)
                    continue

                renato_label_id = name_to_id.get("!!Renato")
                missing = [
                    ln for ln in labels
                    if not (name_to_id.get(ln) and name_to_id[ln] in label_ids)
                ]
                needs_archive = bool(action["archive"]) and ("INBOX" in label_ids)
                needs_unlabel = bool(action["remove_renato"]) and bool(
                    renato_label_id and renato_label_id in label_ids
                )

                # Preview do que faria (dry_run) / nada a fazer.
                planned = []
                if missing:
                    planned.append("label:" + ",".join(missing))
                if needs_archive:
                    planned.append("archive")
                if needs_unlabel:
                    planned.append("unlabel_renato")

                if not planned:
                    entry["action"] = (
                        "skip:not_in_inbox"
                        if (action["archive"] and "INBOX" not in label_ids)
                        else "skip:already_labeled"
                    )
                    stats["by_bucket"]["noop"] += 1
                    stats["per_email"].append(entry)
                    continue

                if dry_run:
                    entry["action"] = "+".join(planned)
                    # Camada esperta suprime o ping individual — o resumo
                    # consolidado (1 WA/run) cobre os needs-you.
                    if action["notify_renato"] and "!!Renato" in missing and not sl:
                        entry["would_notify_renato"] = True
                        stats["wa_renato_would_push"] += 1
                    stats["by_bucket"][bucket] += 1
                    stats["per_email"].append(entry)
                    continue

                # ---- AGE ----
                acted_this = False
                new_renato_applied = False
                for ln in missing:
                    if labels_today >= MAX_LABELS_PER_DAY:
                        stats["label_skipped_cap"] += 1
                        break
                    try:
                        res = await _apply_generic_label(
                            gmail, ref["access_token"], gmail_id,
                            acct_email, None, None, ln,
                        )
                        if res.get("applied"):
                            acted_this = True
                            labels_today += 1
                            if ln == "!!Renato":
                                new_renato_applied = True
                    except Exception as e:
                        stats["errors"].append(
                            f"label {ln} {gmail_id[:12]}: {str(e)[:80]}"
                        )
                if needs_archive:
                    try:
                        archived_ok = await gmail.archive_message(
                            ref["access_token"], gmail_id
                        )
                        if archived_ok:
                            acted_this = True
                    except Exception as e:
                        stats["errors"].append(
                            f"archive {gmail_id[:12]}: {str(e)[:80]}"
                        )
                # Feedback Renato (21/07): arquivar/deletar tem que TIRAR o email
                # da lista de atencao — remove !!Renato (no-op se ausente).
                if action["remove_renato"]:
                    try:
                        await _remove_renato_label(
                            gmail, ref["access_token"], gmail_id,
                            renato_label_id=renato_label_id,
                        )
                    except Exception as e:
                        stats["errors"].append(
                            f"unlabel_renato {gmail_id[:12]}: {str(e)[:80]}"
                        )

                entry["action"] = "+".join(planned)
                stats["by_bucket"][bucket] += 1
                if acted_this:
                    stats["acted"] += 1

                # WA push SO quando um NOVO !!Renato entra (so esse balde
                # interrompe — Renato 24/07). Dedup por gmail_id no notify.
                # Camada esperta suprime o ping individual — resumo consolidado
                # (1 WA/run) cobre os needs-you.
                if new_renato_applied and action["notify_renato"] and not sl:
                    pushed = await _notify_new_renato(headers, gmail_id, decision)
                    if pushed:
                        stats["wa_renato_pushed"] += 1

            except Exception as e:
                stats["errors"].append(f"process {gmail_id[:12]}: {str(e)[:120]}")
                logger.exception(f"inbox-scan process {gmail_id}")
                entry.setdefault("action", f"error:{str(e)[:40]}")
            stats["per_email"].append(entry)

    # Resumo consolidado da camada esperta (1 WA/run). Em dry_run vira preview no
    # stats (usa a contagem de would-forward pra o preview refletir o que faria).
    #
    # GATILHO — recalibração 25/07 (decisão do Renato, opção (a)): ENCAMINHAR PRA
    # ANDRESSA NÃO INTERROMPE. Medido hoje: 2 WhatsApps em 2 runs só pra anunciar
    # 5 encaminhamentos (3 errados), com smart_resolved=0 nas 8 runs do dia. O
    # forward é execução mecânica — continua acontecendo e continua REGISTRADO
    # (label andressa-encaminhado + agent_actions + per_email/stats), só some do
    # WhatsApp.
    #
    # RECALIBRAÇÃO 27/07 — needs_you SAI DO GATILHO (kill-switch
    # INBOX_SMART_NEEDS_YOU_WA=1 devolve). O comentário de 25/07 acima dizia que
    # tirá-lo "deixaria o balde !!Renato mudo". FALSO — auditado antes de mexer:
    #
    #   a) urgent-tick: sweep_email_triage (cron email-triage-sweep, minute 7,37
    #      => 48 runs/dia) manda "📧 Email urgente (P{N})" por conta própria,
    #      one-shot por gmail_id. Foi ele que mandou o e-mail do pai 30-60min
    #      ANTES do resumo, nos dias 26 e 27/07 — a duplicata que motivou isto.
    #   b) briefing das 7h da tonIAH: le copilot.emails direto (status='pending'
    #      AND needs_attention AND priority>=7, janela 72h) — repo tonia,
    #      services/emails.py + briefing.py. Vivo e independente deste arquivo.
    #   c) Gmail: o e-mail continua com !!Renato (este ramo NÃO mexe em label —
    #      ver o `else` do bucket renato acima), então a fila visual fica igual.
    #
    # ⚠️ DOIS CAMINHOS QUE **NÃO** COBREM (verificado 27/07 — não os cite como
    # garantia, foi o erro que este proprio comentario veio corrigir):
    #   - `get_pending_email_triage` (cos_tools.py) so e chamada pelo
    #     `cos_investigator`, cujo cron esta COMENTADO no worker desde 22/06
    #     (workers/audio-transcriber/main.py:232). O cron `cos-daily-review` que
    #     roda de verdade executa `frente_review.run_and_persist()`, que nao usa
    #     as tools do agente. Ultima run do cos-investigator em cron_runs: 22/06.
    #   - `daily-morning-briefing` tambem esta COMENTADO no worker (linha 176),
    #     junto com `daily-evening-debriefing` (177). Sao os unicos chamadores de
    #     `consume_pending_for_digest`, entao a fila `pending_notifications` nao
    #     e drenada desde 13/07 (medido: 4 itens com sent_at IS NULL).
    #   O `summarize_for_digest` de agent_actions, portanto, tambem nao chega
    #   hoje. O que sustenta a decisao sao (a) e (b), que sao one-shot e vivos.
    #
    # Ou seja: o bloco "Precisa de você" era a SEGUNDA leitura do mesmo e-mail, e
    # a única N-SHOT (o acumulador é por run; e-mail não resolvido reaparecia a
    # cada 2h enquanto ficasse em !!Renato). Quem cala é ele; o urgent-tick, que
    # é one-shot, fica — e ganhou kill-switch próprio (is_email_urgent_wa_enabled).
    #
    # O resumo sai quando a camada mexeu no MUNDO do Renato:
    #   (1) fechou/remarcou task (parte B) — ação autônoma dela; ou
    #   (2) needs_you, SÓ com INBOX_SMART_NEEDS_YOU_WA=1 (default OFF).
    # Se o resumo sai, o encaminhamento É citado no texto — ele só deixa de ser
    # motivo pra tocar o telefone, não vira invisível.
    # Teto: 1 resumo/dia BRT (INBOX_SMART_SUMMARY_MAX_PER_DAY), dentro do _send.
    if sl:
        fwd_for_summary = (
            smart_forwarded if not dry_run else stats.get("smart_would_forward", 0)
        )
        _needs_you_triggers = bool(smart_needs_you) and is_smart_needs_you_wa_enabled()
        if smart_resolved_items or _needs_you_triggers:
            try:
                await _send_smart_summary(
                    stats, fwd_for_summary, smart_resolved_items,
                    smart_needs_you, dry_run,
                )
            except Exception as e:
                stats["errors"].append(f"smart_summary: {str(e)[:80]}")
        elif fwd_for_summary or smart_needs_you:
            # Telemetria: a run agiu e ficou calada de propósito — fica no
            # cron_runs pra auditoria não achar que a camada não rodou.
            stats["smart_summary_skipped"] = (
                "needs_you_only" if smart_needs_you and not fwd_for_summary
                else ("forward_and_needs_you" if smart_needs_you else "forward_only")
            )

    stats["duration_ms"] = int((time.time() - started) * 1000)
    return stats


def _label_name_financeiro(account_email: str) -> str:
    """Label de financeiro por conta. Nomes diferentes nas 2 contas."""
    if account_email == "renato.almeida.prado@gmail.com":
        return "!Recibos/Financeiro"
    return "7-Financeiro/Recibos"


def route_mustread_labels(suggested_tags: List[str], account_email: str) -> List[str]:
    """Roteia um email must_read pros label(s) do bucket certo (MANTEM inbox).

    Buckets que mantem no inbox:
      - !Andressa  : R3.6 admin senders (tag !Andressa/admin). Ela acessa a
                     inbox e filtra. Label EXISTENTE (1 '!') — NAO renomear.
      - !!Renato   : default must_read (circulo 1, imprensa, frente, infra
                     critica, financeiro subject, calendar invite, etc).
      - Financeiro : whitelist financeiro/gov/billing ganha o label de
                     financeiro por conta (7-Financeiro/Recibos ou
                     !Recibos/Financeiro). infra-critico ganha !!Renato ALEM.

    Extraido do sweep (era inline no 4f) pra ficar testavel e explicito. NAO
    muda a decisao do classificador — so mapeia suggested_tags -> label(s).
    """
    tags_lower = [str(t).lower() for t in (suggested_tags or [])]
    labels: List[str] = []
    if any(t in tags_lower for t in ("!andressa", "andressa", "admin")):
        labels.append("!Andressa")
    elif any(t in tags_lower for t in ("financeiro", "billing", "gov", "infra-critico")):
        labels.append(_label_name_financeiro(account_email))
        if "infra-critico" in tags_lower:
            labels.append("!!Renato")
    else:
        labels.append("!!Renato")
    return labels


def route_archive_bucket(rule_hits: List[str], confidence: float) -> str:
    """Mapeia um email archive_proposed pro bucket de ACAO (age-ja, 20/07/26).

    Retorna:
      - 'arquivar' : R4 (no-reply/notifications/system). Vale guardar, so
                     tirar da inbox -> archive padrao (remove INBOX, SEM label).
      - 'deletar'  : R5 (unsubscribe+comercial) ou R6 (cold vendor). Fila de
                     exclusao -> label !!Deletar + remove INBOX. O Renato apaga
                     em lote revisando o label. NUNCA deleta de verdade aqui.
      - 'noop'     : qualquer outra coisa (R7 default) OU confianca < 0.85.
                     Incerto -> fica no inbox pro Renato ver (nao mexe).

    Gate: 'arquivar'/'deletar' REMOVEM da inbox, entao so agem com
    confidence >= ARCHIVE_ACTION_MIN_CONFIDENCE (0.85). Discrimina R4 vs
    R5/R6 pelos rule_hits que o classificador ja retorna — sem tocar a
    decisao must_read/archive/silent.
    """
    hits = set(rule_hits or [])
    if (confidence or 0.0) < ARCHIVE_ACTION_MIN_CONFIDENCE:
        return "noop"
    if "R5_unsub" in hits or "R6_cold" in hits:
        return "deletar"
    # R4 noreply + calibracao Renato que vale ARQUIVAR (nao deletar):
    # RC4 ML transacional, RC5 juridico sem frente, RC7 device-alert, RC8
    # marketing (recalibracao 25/07).
    if hits & {"R4_noreply", "RC4_ml_transacional", "RC5_juridico_sem_frente",
               "RC7_device_notify", "RC8_vendor_marketing"}:
        return "arquivar"
    return "noop"


# =============================================================================
# Calibracao Renato (24/07/2026) — roteamento por ACAO-REQUERIDA
# =============================================================================
# PRINCIPIO-MAE (ratificado 24/07): roteia pelo que o email PEDE, nao por
# categoria/remetente. "Exige DECISAO/escolha do Renato?" -> Renato.
# "Execucao mecanica de algo ja decidido?" -> Andressa.
#
# Ex.: Club Athletico "convocacao p/ compra de titulo" PARECE administrativo mas
# exige a DECISAO do Renato -> Renato (RC1). Comgas/Zoom/LastPass = executar ->
# Andressa. Regras de alta precisao (FRASES, nao keyword solta) que devolvem um
# decision dict completo — ou None (nao se aplica -> segue o fluxo R4+).
#
# Todas gated por EMAIL_TRIAGE_CALIBRATION_ENABLED (caller checa). Puras e
# testaveis (classify_calibrated nao toca DB, exceto RC5 que cruza frente ativa).

# RC1 — DECISAO do Renato (convocacao/assembleia/edital/subscricao/titulo).
_RC1_DECISION_PHRASES = (
    "convocação", "convocacao", "edital de convocação", "edital de convocacao",
    "assembleia", "compra de título", "compra de titulo", "subscrição",
    "subscricao", "chamada de capital", "capital call", "deliberação",
    "deliberacao", "aprovação necessária", "aprovacao necessaria",
    "sua decisão", "sua decisao", "aguardamos sua deliberação",
    "aguardamos sua deliberacao", "requer sua aprovação", "requer sua aprovacao",
    "ordem do dia",
)
# RC2 — Tabeliao / cancelamento de protesto -> Andressa (execucao mecanica).
_RC2_TABELIAO_PHRASES = (
    "cancelamento de protesto", "cancelamento do protesto", "tabelião",
    "tabeliao", "tabelionato", "cartório de protesto", "cartorio de protesto",
    "instrumento de protesto", "carta de anuência", "carta de anuencia",
    "3º tabelião", "3o tabeliao",
)
# RC3 — LastPass / seguranca-de-conta -> Andressa (+ possivel phishing).
_RC3_SECURITY_DOMAINS = (
    "lastpass.com", "1password.com", "dashlane.com", "bitwarden.com",
)
_RC3_SECURITY_PHRASES = (
    "lastpass", "1password", "senha mestra", "master password",
    "alerta de segurança", "alerta de seguranca", "security alert",
    "verificação de segurança", "verificacao de seguranca",
    "atividade suspeita", "suspicious activity", "confirme sua conta",
    "confirm your account", "redefinir sua senha", "reset your password",
)
# RC4 — Mercado Livre transacional (compra chegou / devolucao) -> Arquivar.
_RC4_ML_DOMAINS = (
    "mercadolivre.com.br", "mercadolivre.com", "mercadolibre.com",
)
_RC4_ML_TXN_PHRASES = (
    "seu pacote", "sua compra", "seu pedido", "a caminho", "foi entregue",
    "chegou", "devolução", "devolucao", "reembolso", "acompanhe seu envio",
    "código de rastreamento", "codigo de rastreamento", "produto enviado",
    "avaliação do produto", "avaliacao do produto", "compra realizada",
)
# RC5 — Monitoramento juridico (Jusbrasil/Escavador) — cruza FRENTE ATIVA.
_RC5_LEGAL_DOMAINS = (
    "jusbrasil.com.br", "jusbrasil.com", "escavador.com", "escavador.com.br",
    "digesto.com.br",
)
_RC5_LEGAL_PHRASES = (
    "nova publicação", "nova publicacao", "publicações do seu",
    "publicacoes do seu", "monitoramento", "diário oficial", "diario oficial",
    "intimação", "intimacao", "andamento processual", "novo processo",
    "movimentação processual", "movimentacao processual", "novas publicações",
    "novas publicacoes",
)

# RC6 — Recibo/comprovante/fatura -> Financeiro (funde em Andressa no inbox-zero).
# Recalibracao 25/07: recibos ("Your receipt from Anthropic", "Recibo Poli Angels")
# batiam R3_frente -> !!Renato. Sao EXECUCAO (registro contabil), nao decisao.
# FRASES de alta precisao (nao keyword solta) pra nao capturar copy generica.
_RC6_RECIBO_PHRASES = (
    "your receipt", "seu recibo", "recibo de pagamento", "recibo de compra",
    "recibo do pagamento", "comprovante de pagamento", "comprovante de transferencia",
    "comprovante de pix", "payment receipt", "receipt for", "receipt from",
    "your invoice", "invoice for", "invoice from", "nota fiscal", "nf-e",
    "pagamento confirmado", "pagamento recebido", "payment confirmation",
    "payment received", "your payment", "fatura", "boleto",
)
# RC7 — Alerta de DISPOSITIVO/login/servico que NAO exige decisao -> arquivar.
# Recalibracao 25/07: "Anthropic: Security alert: new trusted device added" e
# "gov.br: acesso em novo dispositivo" sao NOTIFICACOES (nada a fazer). Rodam
# ANTES do RC3 (seguranca->Andressa) mas cedem pro RC3 se houver frase-de-ACAO
# (redefinir senha / confirme conta / atividade suspeita).
_RC7_DEVICE_NOTIFY_PHRASES = (
    "new device", "novo dispositivo", "new trusted device", "trusted device",
    "dispositivo confiavel", "new sign-in", "new sign in", "novo acesso",
    "acesso em novo dispositivo", "acesso em um novo", "new login", "novo login",
    "you signed in", "was accessed", "foi acessada", "login realizado",
    "acesso realizado", "sign-in from", "seu codigo de acesso",
    "verification code", "codigo de verificacao", "codigo de seguranca",
    "your verification code", "codigo de acesso",
    # 25/07: "new passkey added" caiu no balde da Andressa por
    # RC3_seguranca_andressa enquanto os irmaos ("new trusted device") foram
    # arquivados aqui — na MESMA run. Passkey e notificacao de dispositivo,
    # mesma classe.
    "passkey", "chave de acesso", "security key", "chave de seguranca",
)
_RC7_ACTION_OVERRIDE_PHRASES = (
    "redefinir sua senha", "redefinir a senha", "reset your password",
    "confirme sua conta", "confirm your account", "atividade suspeita",
    "suspicious activity", "verifique sua conta", "verify your account",
    "senha comprometida", "password compromised", "confirme que foi voce",
    "confirm it was you", "was this you", "confirmar sua identidade",
)
# RC8 — Newsletter/marketing de vendor -> arquivar. Recalibracao 25/07: IBGC
# "Ultimas vagas | Masterclass" e Railway/QuintoAndar bulk news vazavam pro
# Renato (via R3_frente ou R7 professional-default->silent). RODA POR ULTIMO
# (menor precisao). Dois sinais: (a) frase de marketing no ASSUNTO; (b) sub-
# dominio de disparo em massa (news./email./mkt.). RC6 roda ANTES entao recibo/
# fatura real nunca cai aqui.
_RC8_MARKETING_SUBJECT_PHRASES = (
    "ultimas vagas", "garanta sua vaga", "vagas abertas", "masterclass",
    "webinar", "inscreva-se", "inscricoes abertas", "inscricao aberta",
    "promocao", "oferta imperdivel", "oferta exclusiva", "desconto", "cupom",
    "black friday", "pre-aprovado", "cartao de credito disponivel",
    "cartao de credito empresarial", "free friday", "imperdivel", "saldao",
    "ultimas horas", "% da parcela", "% off", "off!", "conheca o consorcio",
    "aumentamos seu", "garanta ja", "aproveite a oferta",
    # "novidade:" com dois-pontos (prefixo de copy de marketing). NAO captura
    # "Novidades da semana" (sem ':') — preserva o teste RC4 ML-sem-txn.
    "novidade:",
)
_RC8_BULK_SUBDOMAIN_LABELS = ("news", "newsletter", "mkt", "marketing", "email")

# RC8 sinal (c) — VENDOR DE MASSA (telecom/utilities). Recalibracao 25/07 (run
# real 13:25 BRT): "Vivo - Contrato do Cliente <contratoeletronico@vivo.com.br>
# — Veja seu contrato dos servicos Vivo" e "Vai de App Vivo! Beneficios." foram
# pro balde Andressa via R3_5_financial_gov (vivo.com.br esta no FINANCIAL_
# DOMAINS por causa da fatura legitima). Causa-raiz: o DOMINIO sozinho estava
# sendo tratado como prova de cobranca. Ele so diz QUEM mandou.
#
# Principio-mae: rotear por ACAO-REQUERIDA. Comunicacao promocional/transacional
# -de-massa de telecom/utility (contrato eletronico, campanha de app, beneficio,
# oferta) NAO exige decisao do Renato NEM execucao da Andressa -> arquivar.
# Cobranca de verdade (fatura/boleto/2a via/codigo de barras) traz evidencia
# EXPLICITA e continua indo pro Financeiro -> Andressa (RC6 roda antes, e o
# guard abaixo exige evidencia negativa antes de arquivar). Generico: a lista e
# de CATEGORIA de fornecedor, nao de marca ("Vivo" nao aparece em regra nenhuma
# alem desta lista de dominios, igual claro/tim/comgas ja apareciam).
_VENDOR_BULK_DOMAINS = (
    # telecom
    "vivo.com.br", "claro.com.br", "mi.claro.com.br", "tim.com.br",
    "oi.com.br", "algartelecom.com.br", "sky.com.br", "nextel.com.br",
    "telefonica.com", "telefonica.com.br", "net.com.br",
    # utilities (energia/gas/agua)
    "comgas.com.br", "enel.com", "enel.com.br", "cpfl.com.br",
    "sabesp.com.br", "light.com.br", "cemig.com.br", "copel.com",
    "neoenergia.com", "equatorialenergia.com.br", "eletropaulo.com.br",
)

# Evidencia POSITIVA de cobranca. Enquanto ela aparecer o email NAO e tratado
# como marketing — volta pra trilha financeira (Financeiro/Andressa). Esta e a
# trava que impede o guard de comer boleto de verdade.
_BILLING_EVIDENCE_PHRASES = (
    "fatura", "boleto", "vencimento", "vence em", "vence amanha", "venceu",
    "pagamento devido", "pagamento em atraso", "em atraso", "cobranca",
    "segunda via", "2a via", "nota fiscal", "nf-e", "codigo de barras",
    "linha digitavel", "valor a pagar", "total a pagar", "debito automatico",
    "conta de consumo", "conta a pagar", "invoice", "amount due",
    "pagamento confirmado", "pagamento recebido", "comprovante", "recibo",
    "pix copia e cola",
)

# Copy promocional NO ASSUNTO — aplicada SO a vendor de massa (blast radius
# limitado; nao entra no _RC8_MARKETING_SUBJECT_PHRASES global, que vale pra
# qualquer remetente). Fatura de verdade nunca traz "oferta"/"beneficios"/
# "baixe o app" no assunto.
_VENDOR_PROMO_SUBJECT_PHRASES = (
    "oferta", "promocao", "desconto", "beneficio", "beneficios", "vantagens",
    "aproveite", "ganhe", "contrate", "assine", "clube de vantagens",
    "baixe o app", "no app", "vai de app", "upgrade", "migre", "portabilidade",
    "conheca", "novidade", "campanha", "combo", "planos a partir de",
    "dobro de internet", "wifi mais", "wi-fi mais", "pontos",
)


# Caixa de DISPARO de cobranca (local-part do remetente). Achado na auditoria
# do dia 1: "Comgas <suafatura@comgas.com.br> — 'Conta Comgas'" e uma fatura de
# gas REAL cujo assunto nao diz "fatura" e cujo corpo nao trouxe keyword
# financeira (run 11:02, R3_5_financial_gov "sem keyword"). Sem este sinal o
# guard arquivaria a conta de gas. O local-part e evidencia forte e generica:
# vendor separa a caixa de cobranca (suafatura@/boleto@/cobranca@) da caixa de
# campanha (mkt@/ofertas@/news@/contratoeletronico@).
_BILLING_SENDER_LOCALPARTS = (
    "fatura", "faturas", "faturamento", "suafatura", "minhafatura",
    "boleto", "boletos", "cobranca", "cobrancas", "financeiro", "contas",
    "billing", "invoice", "invoices", "pagamento", "pagamentos",
    "arrecadacao", "recebiveis",
)


# -----------------------------------------------------------------------------
# EXTENSAO 25/07 — CAMPANHA DE MASSA em dominio FINANCEIRO nao-telecom
# -----------------------------------------------------------------------------
# Mesma classe de erro do guard de vendor, medida hoje nas runs reais (todos
# R3_5_financial_gov conf 0.78 -> !!Renato+financeiro -> balde Andressa):
#   - QuintoAndar "Ainda da tempo de responder a pesquisa!" e "Queremos melhorar
#     sua experiencia" (NPS)
#   - Cora "Sua indicacao vale ate R$2.000 em limite" (campanha de indicacao)
# Pesquisa/NPS/indicacao/convite promocional nao exigem DECISAO do Renato nem
# EXECUCAO da Andressa -> arquivar (principio-mae: roteia por ACAO-REQUERIDA).
#
# POR QUE NAO FUNDE COM _VENDOR_BULK_DOMAINS: o default e o OPOSTO. Em telecom/
# utility, a AUSENCIA de evidencia de cobranca ja basta pra arquivar ("Veja seu
# contrato dos servicos Vivo"). Aqui nao basta — "Alteracao dos Termos e
# Condicoes do Cartao de Credito Cora" (medido 24/07, mesma caixa nao-responda@)
# nao tem uma keyword financeira sequer e MESMO ASSIM tem que continuar indo pro
# Renato. Num dominio financeiro o custo do erro e maior (boleto de aluguel,
# aviso do banco), entao aqui a regra e invertida: exige evidencia POSITIVA de
# campanha NO ASSUNTO, e qualquer sinal de cobranca/seguranca/mudanca contratual
# desliga o guard. Fundir as duas listas apagaria essa diferenca de default.
#
# Selecao de dominio: e derivada de _FINANCIAL_DOMAINS (fonte unica) MENOS o que
# ja tem guard proprio (telecom/utility) e MENOS gov (.gov.br/.jus.br, intocados
# por decisao explicita do Renato — Receita/governo/cartorio nao entram).

# Pesquisa de satisfacao / NPS. Frases (nao keyword solta: "pesquisa" sozinho
# pega pesquisa de mercado/juridica). Zero acao requerida por definicao.
_SURVEY_NPS_SUBJECT_PHRASES = (
    "responder a pesquisa", "responda a pesquisa", "responder nossa pesquisa",
    "responda nossa pesquisa", "responder a nossa pesquisa",
    "pesquisa de satisfacao", "pesquisa de opiniao", "pesquisa rapida",
    "queremos melhorar sua experiencia", "queremos saber sua opiniao",
    "avalie sua experiencia", "avalie seu atendimento", "avalie o atendimento",
    "avalie sua compra", "avalie seu pedido", "avalie nosso",
    "quantas estrelas", "como foi sua experiencia", "conte pra gente como foi",
    "sua opiniao e muito importante", "sua opiniao importa",
    "deixe sua avaliacao", "nos avalie", "net promoter score",
)
# Campanha de indicacao / member-get-member.
_REFERRAL_CAMPAIGN_SUBJECT_PHRASES = (
    "indique e ganhe", "indique um amigo", "indique amigos", "indique e receba",
    "sua indicacao vale", "programa de indicacao", "convide amigos",
    "convide um amigo", "ganhe indicando", "cupom de indicacao",
)
# Evidencia POSITIVA de campanha de massa — vale pra qualquer remetente (entra
# tambem no sinal (a) do RC8, ver comentario do Printi/trustvox la embaixo).
_BULK_CAMPAIGN_SUBJECT_PHRASES = (
    _SURVEY_NPS_SUBJECT_PHRASES + _REFERRAL_CAMPAIGN_SUBJECT_PHRASES
)

# Assuntos que, num dominio financeiro, NUNCA sao campanha descartavel: mudanca
# contratual, tarifa, extrato, rendimento, renegociacao. Checado so no ASSUNTO
# (rodape de peca promocional cita "termos e condicoes" o tempo todo).
_FINANCIAL_KEEP_SUBJECT_PHRASES = (
    "termos e condicoes", "termos de uso", "alteracao contratual",
    "alteracao do contrato", "alteracao dos termos", "atualizacao dos termos",
    "mudanca contratual", "novas condicoes", "aditivo", "contrato",
    "politica de privacidade", "tarifa", "tarifas",
    "aviso importante", "comunicado importante", "aviso legal",
    "extrato", "posicao consolidada", "informe de rendimentos",
    "imposto de renda", "renegociacao", "acordo", "limite de credito",
    "bloqueio", "desbloqueio", "encerramento da conta",
)

_GOV_DOMAINS = (".gov.br", ".jus.br")

# Fonte unica do whitelist financeiro (era local em classify_email_cos; hoisted
# 25/07 pra que o guard de campanha derive dela em vez de duplicar a lista).
_FINANCIAL_DOMAINS = (
    "agilize.com.br", "serasa.com.br", "serasaexperian.com.br",
    "scpc.org.br", "boletobancario.com",
    "itau.com.br", "bradesco.com.br", "santander.com.br",
    "bb.com.br", "nubank.com.br", "inter.co",
    "btg.com", "btgpactual.com", "xpinc.com",
    "stone.com.br", "pagseguro.uol.com.br", "wise.com",
    # NOVOS (calibracao 10/06): bancos + cartoes + telco + AI billing
    "cora.com.br",                # banco PJ
    "c6bank.com.br",              # boleto
    "c6.com.br",
    "esfera.com.vc",              # Santander Esfera
    "santander.com.vc",
    "claro.com.br",               # fatura real (filtro telco abaixo)
    "mi.claro.com.br",            # subdominio fatura Claro
    "vivo.com.br",
    "tim.com.br",
    "claude.ai",                  # Anthropic billing
    "anthropic.com",
    # 25/06/2026: faturas locais que cairam em archive_proposed
    "meres.com.br",               # fatura Méres
    "comgas.com.br",              # fatura gás
    "quintoandar.com.br",         # fatura aluguel
)


def _is_vendor_bulk_domain(dom: str) -> bool:
    """Dominio (ou subdominio) de fornecedor telecom/utility de disparo em massa."""
    d = (dom or "").lower()
    if not d:
        return False
    return any(d == v or d.endswith("." + v) for v in _VENDOR_BULK_DOMAINS)


def _is_financial_bulk_domain(dom: str) -> bool:
    """Dominio financeiro do whitelist que NAO e telecom/utility nem gov.

    Derivado de _FINANCIAL_DOMAINS (nao duplica lista). Telecom/utility ja tem
    guard proprio (_vendor_bulk_marketing, default oposto); gov fica intocado.
    """
    d = (dom or "").lower()
    if not d:
        return False
    if _is_vendor_bulk_domain(d):
        return False
    if any(d.endswith(g) for g in _GOV_DOMAINS):
        return False
    return any(d == f or d.endswith("." + f) for f in _FINANCIAL_DOMAINS)


def _is_billing_sender(from_email: str) -> bool:
    """Local-part do remetente indica caixa de cobranca (suafatura@, boleto@...)."""
    local = _norm_txt((from_email or "").split("@")[0]).replace(".", "").replace("-", "")
    if not local:
        return False
    return any(tok in local for tok in _BILLING_SENDER_LOCALPARTS)


def _vendor_bulk_marketing(subj_n: str, body_n: str, dom: str,
                           has_unsubscribe: bool = False,
                           from_email: str = "") -> bool:
    """True = comunicacao de MASSA de telecom/utility SEM prova de cobranca.

    Precedencia (conservadora — na duvida NAO e marketing):
      1. remetente e caixa de cobranca (suafatura@/boleto@/cobranca@) -> NUNCA
         marketing (cobre fatura cujo assunto nao diz "fatura": "Conta Comgas").
      2. assunto com evidencia de cobranca ("fatura", "2a via", "vence em")
         -> NUNCA marketing (fatura real segue pra Andressa/Financeiro).
      3. corpo com evidencia de cobranca -> so vira marketing se o ASSUNTO for
         promocional explicito ou o email trouxer List-Unsubscribe (rodape
         "consulte sua fatura no app" em peca de campanha).
      4. zero evidencia de cobranca -> massa/institucional ("Veja seu contrato
         dos servicos Vivo") -> marketing -> arquivar.
    """
    if not _is_vendor_bulk_domain(dom):
        return False
    if _is_billing_sender(from_email):
        return False
    if _has_phrase(subj_n, _BILLING_EVIDENCE_PHRASES):
        return False
    promo = _has_phrase(subj_n, _VENDOR_PROMO_SUBJECT_PHRASES) or bool(has_unsubscribe)
    if _has_phrase(body_n, _BILLING_EVIDENCE_PHRASES):
        return promo
    return True


def _financial_bulk_campaign(subj_n: str, body_n: str, dom: str,
                             from_email: str = "") -> bool:
    """True = campanha de massa (pesquisa/NPS/indicacao) de dominio FINANCEIRO
    nao-telecom -> arquivar. Extensao 25/07 do guard de vendor, com a MESMA
    mecanica (_BILLING_EVIDENCE_PHRASES + _is_billing_sender + precedencia
    conservadora), mas com o default invertido: aqui so vira marketing com
    evidencia POSITIVA de campanha NO ASSUNTO.

    Precedencia (6 travas antes do True — na duvida NAO e marketing):
      1. dominio nao e financeiro-nao-telecom -> False (gov/Receita/cartorio e
         telecom/utility nunca chegam aqui).
      2. remetente e caixa de cobranca (suafatura@/boleto@/cobranca@) -> False.
      3. evidencia de cobranca no ASSUNTO **ou no CORPO** -> False. Mais restrito
         que o guard de telecom (que tolera fatura so no rodape): boleto de
         aluguel/cobranca de banco custa caro demais pra arquivar por engano.
      4. sinal de SEGURANCA de conta (reusa _RC3_SECURITY_PHRASES) -> False.
      5. tabeliao/protesto (reusa _RC2_TABELIAO_PHRASES) -> False.
      6. assunto de mudanca contratual/tarifa/extrato/rendimento -> False
         ("Alteracao dos Termos e Condicoes do Cartao de Credito Cora").
    Só então: assunto com pesquisa/NPS/indicacao -> True.
    """
    if not _is_financial_bulk_domain(dom):
        return False
    if _is_billing_sender(from_email):
        return False
    hay = f"{subj_n}\n{body_n}"
    if _has_phrase(hay, _BILLING_EVIDENCE_PHRASES):
        return False
    if _has_phrase(hay, _RC3_SECURITY_PHRASES):
        return False
    if _has_phrase(hay, _RC2_TABELIAO_PHRASES):
        return False
    if _has_phrase(subj_n, _FINANCIAL_KEEP_SUBJECT_PHRASES):
        return False
    # Evidencia POSITIVA de campanha no assunto: pesquisa/NPS/indicacao +
    # a copy de marketing que o RC8 ja reconhece ("conheca o consorcio",
    # "% da parcela", "pre-aprovado", "cartao de credito empresarial" — todas
    # medidas em Cora/QuintoAndar). Reusar a lista do RC8 mantem calibracao
    # ON e OFF com o mesmo veredito.
    return (
        _has_phrase(subj_n, _BULK_CAMPAIGN_SUBJECT_PHRASES)
        or _has_phrase(subj_n, _RC8_MARKETING_SUBJECT_PHRASES)
    )


_ACCENT_MAP = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")


def _norm_txt(s: str) -> str:
    """lower + sem acento + espaco colapsado — comparacao estavel de frase."""
    return re.sub(r"\s+", " ", (s or "").lower().translate(_ACCENT_MAP)).strip()


def _has_phrase(text_norm: str, phrases) -> bool:
    return any(_norm_txt(p) in text_norm for p in phrases)


# -----------------------------------------------------------------------------
# GUARD 27/07 — REMETENTE AUTOMATICO NAO ESCALA (camada esperta)
# -----------------------------------------------------------------------------
# Medicao 26-27/07 no bloco "Precisa de voce": 3 dos 6 falsos-positivos eram
# robo puro — Jusbrasil <nao-responda@> ("Verifique a situacao atual do
# processo"), IBGC <notif...> (marketing as 22:30) e QuintoAndar <noreply@> (
# promocao). Nenhum exigia decisao do Renato.
#
# Local-part normalizado igual ao _is_billing_sender (lower, sem acento, sem
# '.'/'-'): "no-reply" -> "noreply", "nao-responda" -> "naoresponda",
# "notificacoes" -> "notificacoes". Substring: "notif" cobre notif/notifica/
# notification/notificacao numa entrada so.
_AUTOMATED_SENDER_LOCALPARTS = (
    "noreply", "naoresponda", "naoresponder", "donotreply", "dontreply",
    "notif",          # notif@/notifica@/notification@/notificacao@/notifications@
    "newsletter", "newsletters", "mailer", "mailing", "mailman",
    "marketing", "mkt", "campanha", "campaign", "promo", "ofertas",
    "bounce", "postmaster", "automatico", "automatica", "robo", "bot",
)

# Assunto (SO no assunto) que, num remetente automatico, NUNCA e descartavel:
# ato judicial/fiscal com prazo. Deliberadamente SEM "processo"/"acao"/"prazo"
# genericos — o alerta do Jusbrasil ("situacao atual do processo") e monitoramento
# de rotina, nao intimacao, e e exatamente ele que precisa parar de interromper.
# So no ASSUNTO pelo mesmo motivo do _FINANCIAL_KEEP_SUBJECT_PHRASES: rodape de
# peca automatica cita "intimacao"/"citacao" o tempo todo.
_JUDICIAL_KEEP_SUBJECT_PHRASES = (
    "intimacao", "intimado", "citacao", "citado", "mandado", "oficio",
    "audiencia", "sentenca", "despacho judicial", "penhora", "bloqueio judicial",
    "execucao fiscal", "auto de infracao", "notificacao fiscal", "termo de",
    "certidao de divida ativa", "divida ativa", "cda", "protocolo judicial",
)


def _is_automated_sender(from_email: str) -> bool:
    """Local-part do remetente indica DISPARO AUTOMATICO (noreply@/nao-responda@/
    notificacao@/newsletter@/mkt@). Mesma normalizacao do _is_billing_sender."""
    local = _norm_txt((from_email or "").split("@")[0]).replace(".", "").replace("-", "").replace("_", "")
    if not local:
        return False
    return any(tok in local for tok in _AUTOMATED_SENDER_LOCALPARTS)


def smart_automated_no_escalate(
    from_email: str,
    subject: str,
    body_text: str = "",
    has_unsubscribe: bool = False,
) -> Optional[str]:
    """REBAIXA (tira de "precisa de voce") e-mail de remetente automatico.

    Retorna o MOTIVO (string curta pra telemetria/entry) quando o item nao deve
    escalar, ou None quando deve continuar escalando.

    Mesma mecanica dos guards de campanha ja existentes neste arquivo
    (_vendor_bulk_marketing / _financial_bulk_campaign): escada de travas
    conservadoras ANTES do rebaixamento. O falso-positivo INVERSO e caro —
    fatura, cobranca, codigo de seguranca, movimentacao bancaria e intimacao
    tambem chegam de no-reply@ — entao cada trava abaixo devolve None (escala).

    Escada (na duvida NAO rebaixa):
      0. remetente nao e automatico -> None (guard nao opina).
      1. dominio .gov.br/.jus.br (_GOV_DOMAINS) -> None. Robo de orgao publico
         nunca e marketing.
      2. caixa de cobranca (_is_billing_sender: suafatura@/boleto@/cobranca@)
         -> None. Precedencia identica a do _vendor_bulk_marketing.
      3. evidencia de cobranca (_BILLING_EVIDENCE_PHRASES) no assunto OU no
         corpo -> None. Mais restrito que o guard de telecom (que tolera fatura
         so no rodape) pelo mesmo motivo do _financial_bulk_campaign: boleto
         soterrado custa caro demais.
      4. seguranca de conta (_RC3_SECURITY_PHRASES) -> None.
      5. notificacao de dispositivo/codigo (_RC7_DEVICE_NOTIFY_PHRASES) ou frase
         de acao de seguranca (_RC7_ACTION_OVERRIDE_PHRASES) -> None. Aqui a
         lista RC7 entra com polaridade INVERTIDA a do RC8: la ela prova
         "notificacao, nada a fazer -> arquivar"; aqui ela prova "transacional/
         seguranca -> nao e robo de campanha", e a pergunta em jogo e outra
         (pode um robo interromper o Renato?), onde o conservador e MANTER.
      6. tabeliao/protesto (_RC2_TABELIAO_PHRASES) -> None.
      7. ato judicial/fiscal no ASSUNTO (_JUDICIAL_KEEP_SUBJECT_PHRASES) -> None.
      8. mudanca contratual/tarifa/extrato no ASSUNTO
         (_FINANCIAL_KEEP_SUBJECT_PHRASES) -> None.
    Sobreviveu a escada => robo sem nada transacional => rebaixa.
    """
    if not _is_automated_sender(from_email):
        return None
    dom = (from_email or "").split("@")[-1].strip().lower()
    if dom and any(dom == g.lstrip(".") or dom.endswith(g) for g in _GOV_DOMAINS):
        return None
    if _is_billing_sender(from_email):
        return None

    subj_n = _norm_txt(subject)
    body_n = _norm_txt((body_text or "")[:3000])
    hay = f"{subj_n}\n{body_n}"

    if _has_phrase(hay, _BILLING_EVIDENCE_PHRASES):
        return None
    if _has_phrase(hay, _RC3_SECURITY_PHRASES):
        return None
    if _has_phrase(hay, _RC7_DEVICE_NOTIFY_PHRASES):
        return None
    if _has_phrase(hay, _RC7_ACTION_OVERRIDE_PHRASES):
        return None
    if _has_phrase(hay, _RC2_TABELIAO_PHRASES):
        return None
    if _has_phrase(subj_n, _JUDICIAL_KEEP_SUBJECT_PHRASES):
        return None
    if _has_phrase(subj_n, _FINANCIAL_KEEP_SUBJECT_PHRASES):
        return None

    # Evidencia POSITIVA de campanha reforca o motivo (nao e condicao: robo sem
    # nada transacional ja nao tem porque tocar o telefone do Renato).
    if has_unsubscribe:
        return "remetente automatico + list-unsubscribe"
    if (_has_phrase(subj_n, _RC8_MARKETING_SUBJECT_PHRASES)
            or _has_phrase(subj_n, _BULK_CAMPAIGN_SUBJECT_PHRASES)):
        return "remetente automatico + assunto de campanha"
    return "remetente automatico sem sinal transacional"


def _decision(classification, priority, conf, reasons, tags, actions, rule_hit,
              escalation=False):
    """Monta o decision dict no formato de classify_email_cos."""
    return {
        "classification": classification,
        "priority": priority,
        "ai_confidence": conf,
        "reasons": reasons if isinstance(reasons, list) else [reasons],
        "suggested_tags": tags,
        "suggested_actions": actions,
        "escalation": escalation,
        "rule_hits": [rule_hit],
    }


def classify_calibrated(
    subject: str,
    body_text: str,
    from_email: str,
    from_name: str,
    sender_domain: str,
    account_type: str = "professional",
    has_unsubscribe: bool = False,
) -> Optional[Dict]:
    """Regras calibradas Renato 24/07 — roteamento por ACAO-REQUERIDA.

    Ordem = precisao decrescente. Retorna decision dict (mesmo shape do
    classify_email_cos) ou None (nenhuma se aplica). Puro exceto RC5 (que cruza
    frente ativa via _email_juridico_matches_active_front). Gate no caller.
    """
    subj_n = _norm_txt(subject)
    body_n = _norm_txt(body_text[:3000])
    hay = f"{subj_n}\n{body_n}"
    dom = (sender_domain or "").lower()
    # Guard de vendor de massa (telecom/utility) — computado uma vez, consumido
    # pelo RC6 (nao deixa copy promocional entrar na trilha financeira) e pelo
    # RC8 (sinal (c): arquiva a peca de massa).
    vendor_bulk_mkt = _vendor_bulk_marketing(
        subj_n, body_n, dom, has_unsubscribe, from_email=from_email
    )
    # Extensao 25/07: campanha de massa (pesquisa/NPS/indicacao) vinda de dominio
    # FINANCEIRO nao-telecom. Consumido SO pelo RC8 — de proposito nao mexe no
    # RC6: se aparecer qualquer frase de recibo/fatura, a trilha financeira vence
    # (na duvida NAO e marketing, e aqui o custo do erro e maior).
    financial_bulk_mkt = _financial_bulk_campaign(
        subj_n, body_n, dom, from_email=from_email
    )

    # RC1 — DECISAO do Renato (subject prevalece; body confirma). ACAO: decidir.
    if _has_phrase(subj_n, _RC1_DECISION_PHRASES) or _has_phrase(body_n, _RC1_DECISION_PHRASES):
        return _decision(
            "must_read", 8, 0.85,
            ["RC1: exige DECISAO do Renato (convocacao/assembleia/deliberacao)"],
            ["!!Renato", "decisao"],
            [{"type": "decide", "reason": "convocacao/deliberacao"}],
            "RC1_decisao_renato",
        )

    # RC6 — Recibo/comprovante/fatura -> Financeiro (funde em Andressa). ACAO:
    # registrar (execucao), nao decidir. Roda cedo (antes de RC2/RC3/marketing)
    # pra que fatura/boleto real NUNCA seja capturado por marketing (RC8) ou
    # device-alert (RC7). Tag "financeiro" -> route_mustread_labels -> bucket
    # financeiro -> funde em Andressa no inbox-zero (Renato 24/07).
    # Excecao 25/07: vendor de massa cujo unico sinal financeiro esta no RODAPE
    # ("consulte sua fatura no Meu Vivo") com assunto promocional -> NAO e
    # cobranca, e campanha. Cede pro RC8. Se a cobranca aparece no ASSUNTO,
    # _vendor_bulk_marketing ja devolve False e o RC6 vale normalmente.
    if (
        (_has_phrase(subj_n, _RC6_RECIBO_PHRASES) or _has_phrase(body_n, _RC6_RECIBO_PHRASES))
        and not vendor_bulk_mkt
    ):
        return _decision(
            "must_read", 6, 0.85,
            ["RC6: recibo/comprovante/fatura -> Financeiro (Andressa), nao decisao"],
            ["!!Renato", "financeiro"],
            [{"type": "delegate", "to": "andressa", "reason": "recibo/fatura"}],
            "RC6_recibo_financeiro",
        )

    # RC2 — Tabeliao / cancelamento de protesto -> Andressa (mecanico).
    if _has_phrase(hay, _RC2_TABELIAO_PHRASES):
        return _decision(
            "must_read", 6, 0.85,
            ["RC2: tabeliao/cancelamento de protesto -> Andressa (execucao)"],
            ["!Andressa", "admin", "protesto"],
            [{"type": "delegate", "to": "andressa", "reason": "protesto/tabeliao"}],
            "RC2_tabeliao_andressa",
        )

    # RC7 — Alerta de DISPOSITIVO/login/servico SEM acao -> arquivar. Roda ANTES
    # do RC3 mas cede pra ele se houver frase-de-ACAO (reset/confirme/suspeita):
    # "new trusted device"/"acesso em novo dispositivo" = notificacao (nada a
    # fazer); "reset your password" = execucao -> RC3/Andressa.
    if (
        _has_phrase(hay, _RC7_DEVICE_NOTIFY_PHRASES)
        and not _has_phrase(hay, _RC7_ACTION_OVERRIDE_PHRASES)
    ):
        return _decision(
            "archive_proposed", 2, 0.85,
            ["RC7: alerta de dispositivo/login/servico sem acao -> arquivar"],
            ["arquivar-direto", "device-alert"],
            [{"type": "archive", "reason": "device/login notify"}],
            "RC7_device_notify",
        )

    # RC3 — LastPass / seguranca-de-conta -> Andressa (+ possivel phishing).
    is_sec_dom = bool(dom) and any(dom == d or dom.endswith("." + d) for d in _RC3_SECURITY_DOMAINS)
    if is_sec_dom or _has_phrase(hay, _RC3_SECURITY_PHRASES):
        return _decision(
            "must_read", 6, 0.85,
            ["RC3: seguranca-de-conta (LastPass etc) -> Andressa. "
             "POSSIVEL PHISHING: nao clicar link, verificar remetente"],
            ["!Andressa", "seguranca-conta", "possivel-phishing"],
            [{"type": "delegate", "to": "andressa", "reason": "seguranca-conta"}],
            "RC3_seguranca_andressa",
        )

    # RC4 — Mercado Livre transacional -> Arquivar (nao deletar, nao Renato).
    is_ml_dom = bool(dom) and any(dom == d or dom.endswith("." + d) for d in _RC4_ML_DOMAINS)
    if is_ml_dom and _has_phrase(hay, _RC4_ML_TXN_PHRASES):
        return _decision(
            "archive_proposed", 2, 0.88,
            ["RC4: Mercado Livre transacional (compra/entrega/devolucao) -> arquivar"],
            ["arquivar-direto", "mercadolivre"],
            [{"type": "archive", "reason": "ML transacional"}],
            "RC4_ml_transacional",
        )

    # RC5 — Monitoramento juridico: cruza FRENTE ATIVA. Escala pro Renato SO se
    # bate projeto/contato ativo; senao arquiva. NAO e regra fixa (Renato 24/07).
    is_legal_dom = bool(dom) and any(dom == d or dom.endswith("." + d) for d in _RC5_LEGAL_DOMAINS)
    if is_legal_dom or _has_phrase(hay, _RC5_LEGAL_PHRASES):
        match = _email_juridico_matches_active_front(f"{subject}\n{body_text[:3000]}")
        if match:
            return _decision(
                "must_read", 8, 0.82,
                [f"RC5: monitoramento juridico de FRENTE ATIVA "
                 f"({match.get('project_name')} #{match.get('project_id')})"],
                ["!!Renato", "juridico", f"frente_{match.get('project_id')}"],
                [{"type": "review", "reason": "juridico frente ativa"}],
                "RC5_juridico_frente",
            )
        return _decision(
            "archive_proposed", 2, 0.85,
            ["RC5: monitoramento juridico SEM frente ativa casada -> arquivar"],
            ["arquivar-direto", "juridico"],
            [{"type": "archive", "reason": "juridico sem frente"}],
            "RC5_juridico_sem_frente",
        )

    # RC8 — Newsletter/marketing de vendor -> arquivar (menor precisao, por
    # ultimo). Sinal (a): frase de marketing no ASSUNTO. Sinal (b): subdominio
    # de disparo em massa (news./email./mkt.). RC6 ja tirou recibo/fatura real
    # do caminho, entao aqui e ruido comercial. Arquivar (nao deletar): sai do
    # inbox e do Renato, mas fica recuperavel.
    first_label = dom.split(".")[0] if dom else ""
    is_bulk_subdomain = first_label in _RC8_BULK_SUBDOMAIN_LABELS
    # Sinal (a) ganhou as frases de pesquisa/NPS/indicacao (25/07). Vale pra
    # QUALQUER remetente de proposito: "Printi <coleta@info.trustvox.com.br> —
    # 'quantas estrelas o seu pedido merece?'" caiu em !!Renato via R7 professio-
    # nal-default, e trustvox nao e dominio financeiro (nao da pra tratar pelo
    # guard financeiro). NPS e a MESMA classe de acao-requerida: nenhuma. R1
    # (imprensa) e R2 (C1/C2) rodam ANTES do bloco RC, entao pesquisa de contato
    # do circulo 1 continua protegida; e arquivar e reversivel.
    is_campaign_subject = (
        _has_phrase(subj_n, _RC8_MARKETING_SUBJECT_PHRASES)
        or _has_phrase(subj_n, _BULK_CAMPAIGN_SUBJECT_PHRASES)
    )
    if is_campaign_subject or is_bulk_subdomain or vendor_bulk_mkt or financial_bulk_mkt:
        if vendor_bulk_mkt:
            reason = (
                "RC8: comunicacao de massa de telecom/utility SEM evidencia de "
                f"cobranca ({dom}) -> arquivar (nem decisao do Renato, nem "
                "execucao da Andressa)"
            )
        elif financial_bulk_mkt:
            reason = (
                f"RC8: campanha de massa (pesquisa/NPS/indicacao) de {dom} "
                "SEM evidencia de cobranca/seguranca/mudanca contratual -> "
                "arquivar (nem decisao do Renato, nem execucao da Andressa)"
            )
        else:
            reason = "RC8: newsletter/marketing de vendor -> arquivar (fora do Renato)"
        return _decision(
            "archive_proposed", 2, 0.85,
            [reason],
            ["arquivar-direto", "marketing"],
            [{"type": "archive", "reason": "vendor marketing/newsletter"}],
            "RC8_vendor_marketing",
        )

    return None


# Tokens genericos que NAO identificam entidade (reaproveita ideia do
# detector_cruzamentos — mantido local pra nao acoplar ao detector, que esta
# desativado — [[project_signals_deactivated]]).
_JURIDICO_STOP = {
    "grupo", "clinica", "consultoria", "associacao", "brasil", "group",
    "holding", "company", "empresa", "conselho", "conselhos", "empresarial",
    "foundation", "ltda", "sociedade", "participacoes", "servicos", "solucoes",
    "tecnologia", "digital", "projeto", "onboarding", "processo", "publicacao",
    "publicacoes", "diario", "oficial", "intimacao", "andamento", "juridico",
    "jusbrasil", "escavador", "monitoramento", "recuperacao",
}


def _entity_tokens_local(*names: str) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for name in names:
        for t in re.findall(r"[a-z0-9]+", _norm_txt(name)):
            if len(t) >= 4 and t not in _JURIDICO_STOP and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _email_juridico_matches_active_front(text: str) -> Optional[Dict]:
    """Cruza o texto de um email juridico com FRENTES ATIVAS (projetos + contatos).

    Retorna {project_id, project_name} do 1o match, ou None. Deterministico,
    sem LLM/signal (signals OFF — [[project_signals_deactivated]]): so um lookup
    sincrono. So e chamado quando RC5 ja detectou um email juridico (raro), entao
    o custo por-email e amortizado. Match = token significativo (>=4 chars) da
    empresa_relacionada/nome do projeto (ou empresa de um contato ligado ao
    projeto) aparece NOMEADO no texto do email (word-start).
    """
    text_n = _norm_txt(text)
    if not text_n:
        return None
    try:
        with get_db() as conn:
            cur = conn.cursor()
            # Projetos ativos (exclui os terminais). status default 'ativo'.
            cur.execute(
                """
                SELECT id, nome, empresa_relacionada
                FROM projects
                WHERE COALESCE(status, 'ativo') NOT IN
                      ('concluido', 'concluído', 'cancelado', 'arquivado', 'pausado')
                """
            )
            projects = cur.fetchall()
    except Exception as e:
        logger.warning(f"_email_juridico_matches_active_front falhou: {e}")
        return None

    for p in projects:
        tokens = _entity_tokens_local(p.get("empresa_relacionada") or "", p.get("nome") or "")
        for t in tokens:
            if re.search(r"\b" + re.escape(t), text_n):
                return {"project_id": p.get("id"), "project_name": p.get("nome")}
    return None


# =============================================================================
# INBOX-ZERO — roteamento por ACAO (puro, testavel sem Gmail)
# =============================================================================
# rule_hits de archive_proposed que valem ARQUIVAR direto (bucket 'arquivar'):
# R4 noreply, R7 personal-default (promo pessoal), RC4 ML transacional, RC5
# juridico sem frente. NAO inclui R7_personal_default no route_archive_bucket
# LEGADO (mudaria o prod sweep) — so aqui, no modo INBOX-ZERO (gated).
_ARQUIVAR_RULE_HITS = {
    "R4_noreply", "R7_personal_default",
    "RC4_ml_transacional", "RC5_juridico_sem_frente",
    # Recalibracao 25/07: device-alert e marketing viram arquivar (nao Renato).
    "RC7_device_notify", "RC8_vendor_marketing",
}


def resolve_inbox_action(
    decision: Dict,
    account_email: str,
    inbox_zero: bool,
) -> Dict:
    """Roteamento PURO (testavel sem Gmail): decisao do classificador -> acao
    concreta no inbox. Kill-switch: inbox_zero=False reproduz o comportamento
    LEGADO byte-a-byte; True liga o modo INBOX-ZERO ("corredor": tudo sai).

    Returns:
        {bucket, labels, archive, remove_renato, notify_renato}
          bucket        : 'renato'|'andressa'|'financeiro'|'arquivar'|'deletar'|'noop'
          labels        : label(s) a ADICIONAR
          archive       : remove da INBOX
          remove_renato : tira !!Renato (arquivar/deletar)
          notify_renato : WA interrompe (novo balde !!Renato) — so bucket renato
    """
    classification = decision.get("classification")
    confidence = float(decision.get("ai_confidence") or 0.5)
    rule_hits = decision.get("rule_hits") or []
    suggested_tags = decision.get("suggested_tags") or []

    # ---------------- LEGADO (preservador) — comportamento pre-24/07 ----------
    if not inbox_zero:
        if classification == "must_read":
            labels = route_mustread_labels(suggested_tags, account_email)
            return {
                "bucket": _mustread_bucket_name(labels), "labels": labels,
                "archive": False, "remove_renato": False, "notify_renato": False,
            }
        if classification == "archive_proposed":
            bucket = route_archive_bucket(rule_hits, confidence)
            if bucket == "deletar":
                return {"bucket": "deletar", "labels": ["!!Deletar"], "archive": True,
                        "remove_renato": True, "notify_renato": False}
            if bucket == "arquivar":
                return {"bucket": "arquivar", "labels": [], "archive": True,
                        "remove_renato": True, "notify_renato": False}
            return {"bucket": "noop", "labels": [], "archive": False,
                    "remove_renato": False, "notify_renato": False}
        # silent
        return {"bucket": "noop", "labels": [], "archive": False,
                "remove_renato": False, "notify_renato": False}

    # ---------------- INBOX-ZERO (corredor) — TUDO sai do inbox ---------------
    if classification == "must_read":
        labels = route_mustread_labels(suggested_tags, account_email)
        bucket = _mustread_bucket_name(labels)
        # Financeiro FUNDE em Andressa (nao e balde proprio) — Renato 24/07.
        if bucket == "financeiro":
            bucket, labels = "andressa", ["!Andressa"]
        return {
            "bucket": bucket, "labels": labels, "archive": True,
            "remove_renato": False, "notify_renato": (bucket == "renato"),
        }

    if classification == "archive_proposed":
        hits = set(rule_hits)
        if "R5_unsub" in hits or "R6_cold" in hits:
            return {"bucket": "deletar", "labels": ["!!Deletar"], "archive": True,
                    "remove_renato": True, "notify_renato": False}
        if hits & _ARQUIVAR_RULE_HITS:
            return {"bucket": "arquivar", "labels": [], "archive": True,
                    "remove_renato": True, "notify_renato": False}
        # Incerto/no-op -> Renato (fila de revisao). MUDANCA 1 (24/07): incerto
        # NAO fica no inbox nem vira lixo; vira fila do Renato.
        return {"bucket": "renato", "labels": ["!!Renato"], "archive": True,
                "remove_renato": False, "notify_renato": True}

    # silent (professional default, sem sinal forte) -> Renato review queue.
    # MUDANCA 2 (24/07): silent tambem SAI do inbox pro balde certo.
    return {"bucket": "renato", "labels": ["!!Renato"], "archive": True,
            "remove_renato": False, "notify_renato": True}


async def _notify_new_renato(
    headers: Dict,
    gmail_id: str,
    decision: Dict,
) -> bool:
    """WA push quando um NOVO !!Renato entra na fila (so esse balde interrompe).

    Renato 24/07: aviso por WhatsApp quando chegar um NOVO !!Renato. Dedup por
    gmail_id (nao repete). Score 8 => WA imediato ([[porta_voz_signal_routing]]).
    Defensivo: engole excecao (notificacao nunca derruba a triagem).
    """
    try:
        from services.notification_router import notify
        sender = (headers.get("from") or "")[:60]
        subj = (headers.get("subject") or "(sem assunto)")[:70]
        why = "; ".join((decision.get("reasons") or [])[:2])[:120]
        txt = (
            f"⭐ Novo !!Renato (inbox-zero)\n"
            f"De: {sender}\n"
            f"Assunto: {subj}\n"
            f"Por que: {why}"
        )
        return await notify(
            "inbox_triage", "Novo !!Renato", txt, 8,
            msg_type="inbox_renato", dedup=f"inbox_renato:{gmail_id}",
            topic=headers.get("subject"),
        )
    except Exception:
        return False


async def _remove_renato_label(
    gmail_integration,
    access_token: str,
    gmail_id: str,
    renato_label_id: Optional[str] = None,
) -> bool:
    """Remove o label !!Renato de UMA mensagem (no-op se ausente/inexistente).

    Feedback recorrente do Renato (21/07/2026): ao ARQUIVAR ou DELETAR um email,
    ele tem que sair da lista de atencao !!Renato — hoje o email saia do inbox
    mas continuava aparecendo em "Todos com !!Renato". Aditivo/defensivo: nao
    muda a classificacao; so garante que arquivar/deletar tambem tira o !!Renato.
    Idempotente: se o label nao existe na conta ou nao esta aplicado, e no-op.

    Args:
        renato_label_id: id ja resolvido (name->id) pra evitar 1 chamada extra;
                         se None, resolve via list_labels.
    Retorna True se a chamada de modify foi enviada; False em ausencia/erro.
    """
    try:
        if not renato_label_id:
            labels = await gmail_integration.list_labels(access_token)
            renato_label_id = next(
                (l.get("id") for l in (labels or []) if l.get("name") == "!!Renato"),
                None,
            )
        if not renato_label_id:
            return False  # conta sem o label — nada a remover
        return await gmail_integration.modify_message_labels(
            access_token, gmail_id, remove_label_ids=[renato_label_id]
        )
    except Exception:
        return False


async def _thread_last_is_outbound(
    gmail_integration,
    access_token: str,
    thread_id: Optional[str],
    account_email: str,
) -> bool:
    """True se a ULTIMA mensagem do thread e do proprio Renato (outbound/SENT).

    Torna a atencao THREAD-AWARE (feedback 21/07 — action-blindness): se o
    ultimo turno de uma conversa ja e a RESPOSTA do Renato, o item esta
    RESOLVIDO e o sistema nao deve sugerir "responder"/must_read. Exemplo real:
    thread com Orestes cuja ultima msg ja e do Renato, mas a triagem via so a
    msg inbound isolada (ainda no inbox) e pedia resposta.

    Defensivo: exige >=2 mensagens (back-and-forth de verdade) e, em qualquer
    erro/ambiguidade, retorna False — NUNCA arquiva sem confirmar o outbound.
    Detecta outbound por label SENT; fallback = From header == conta.
    """
    if not thread_id:
        return False
    try:
        th = await gmail_integration.get_thread(
            access_token, thread_id, format="metadata"
        )
        if not th or "error" in th:
            return False
        msgs = th.get("messages") or []
        if len(msgs) < 2:
            return False  # thread de 1 turno nao e "ja respondido"
        last = msgs[-1]
        lids = last.get("labelIds") or []
        if "SENT" in lids:
            return True
        for h in (last.get("payload", {}).get("headers", []) or []):
            if (h.get("name") or "").lower() == "from":
                return account_email.lower() in (h.get("value") or "").lower()
        return False
    except Exception:
        return False


async def _apply_generic_label(
    gmail_integration,
    access_token: str,
    gmail_id: str,
    account_email: str,
    triage_id: int,
    message_id: int,
    label_name: str,
) -> Dict:
    """Aplica qualquer label via gmail.add_gmail_label + log em agent_actions.

    Idempotente (helper le current labels e skip se ja aplicado).
    """
    try:
        result = await gmail_integration.add_gmail_label(
            access_token=access_token,
            message_id_gmail=gmail_id,
            label_name=label_name,
        )
    except Exception as e:
        return {"applied": False, "reason": f"call_exc: {e}"}

    if not result.get("applied"):
        if result.get("reason") == "already_labeled":
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        UPDATE email_triage
                        SET action_taken = 'label_pre_existing'
                        WHERE id = %s AND action_taken IS NULL
                        """,
                        (triage_id,),
                    )
                    conn.commit()
            except Exception:
                pass
        return result

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE email_triage
                SET action_taken = %s, actioned_at = NOW()
                WHERE id = %s
                """,
                (f"label_applied:{label_name}", triage_id),
            )
            cursor.execute(
                """
                INSERT INTO agent_actions
                    (action_type, category, title, details, scope_ref, source, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "gmail_label_add",
                    "email_triage",
                    f"Label {label_name} em email [{_short_account(account_email)}]",
                    f"triage_id={triage_id} gmail_id={gmail_id}",
                    json.dumps({
                        "triage_id": triage_id,
                        "message_id": message_id,
                        "gmail_id": gmail_id,
                        "account_email": account_email,
                        "label": label_name,
                        "label_id": result.get("label_id"),
                    }),
                    "email_triage_sweep",
                    "done",
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"_apply_generic_label log falhou: {e}")

    return result


async def _apply_renato_label(
    gmail_integration,
    access_token: str,
    gmail_id: str,
    account_email: str,
    triage_id: int,
    message_id: int,
) -> Dict:
    """Aplica label !!Renato via helper de alto nivel + log em agent_actions.

    Idempotencia: gmail.add_gmail_label() le current labels e skip se ja
    aplicada. Isso previne dupla aplicacao quando Andressa marcou primeiro
    (CoS detecta e nao age). Cap diario fica no caller (sweep_email_triage).
    """
    try:
        result = await gmail_integration.add_gmail_label(
            access_token=access_token,
            message_id_gmail=gmail_id,
            label_name="!!Renato",
        )
    except Exception as e:
        return {"applied": False, "reason": f"call_exc: {e}"}

    if not result.get("applied"):
        # Pode ser already_labeled (Andressa ja marcou) — log discreto
        if result.get("reason") == "already_labeled":
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        UPDATE email_triage
                        SET action_taken = 'label_pre_existing'
                        WHERE id = %s AND action_taken IS NULL
                        """,
                        (triage_id,),
                    )
                    conn.commit()
            except Exception:
                pass
        return result

    # Aplicou: marca triage + log agent_actions
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE email_triage
                SET action_taken = 'label_applied', actioned_at = NOW()
                WHERE id = %s
                """,
                (triage_id,),
            )
            cursor.execute(
                """
                INSERT INTO agent_actions
                    (action_type, category, title, details, scope_ref, source, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "gmail_label_add",
                    "email_triage",
                    f"Label !!Renato em email [{_short_account(account_email)}]",
                    f"triage_id={triage_id} gmail_id={gmail_id}",
                    json.dumps({
                        "triage_id": triage_id,
                        "message_id": message_id,
                        "gmail_id": gmail_id,
                        "account_email": account_email,
                        "label": "!!Renato",
                        "label_id": result.get("label_id"),
                    }),
                    "email_triage_sweep",
                    "done",
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"_apply_renato_label log falhou: {e}")

    return result


async def _create_shadow_proposal(
    triage_id: int,
    gmail_id: str,
    account_email: str,
    headers: Dict,
    decision: Dict,
) -> Dict:
    """Cria email_archive_proposals shadow (NAO arquiva). Detalhamento Commit 5."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            # Skip se ja temos proposta pra esse message_id
            cur.execute(
                "SELECT id FROM email_archive_proposals WHERE message_id = %s LIMIT 1",
                (gmail_id,),
            )
            if cur.fetchone():
                return {"created": False, "reason": "already_proposed"}

            from_h = (headers.get("from") or "")[:200]
            subj = (headers.get("subject") or "")[:200]
            reasons = decision.get("reasons", []) or []
            reason_text = "; ".join(reasons)[:500]

            cur.execute(
                """
                INSERT INTO email_archive_proposals
                    (email_triage_id, message_id, account_email, sender, subject,
                     classification_reason, ai_confidence, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'shadow')
                RETURNING id
                """,
                (
                    triage_id, gmail_id, account_email, from_h, subj,
                    reason_text, float(decision.get("ai_confidence") or 0.0),
                ),
            )
            row = cur.fetchone()

            # Marca triage como shadow proposal
            cur.execute(
                """
                UPDATE email_triage
                SET status = 'archive_proposed_shadow'
                WHERE id = %s
                """,
                (triage_id,),
            )
            conn.commit()
            return {"created": True, "proposal_id": row["id"] if row else None}
    except Exception as e:
        logger.warning(f"_create_shadow_proposal {gmail_id}: {e}")
        return {"created": False, "error": str(e)}


def compute_archive_fp_rate(days: int = 14) -> Dict:
    """Telemetria FP do shadow mode. Usado pra decidir quando ligar
    AUTO_ARCHIVE_ENABLED. FP rate = rejected / (approved+rejected)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM email_archive_proposals
                WHERE criado_em > NOW() - INTERVAL '%s days'
                GROUP BY status
                """,
                (days,),
            )
            counts = {r["status"]: r["n"] for r in cur.fetchall()}
    except Exception as e:
        return {"error": str(e)}

    total = sum(counts.values())
    approved = counts.get("approved", 0) + counts.get("archived", 0)
    rejected = counts.get("rejected", 0)
    decided = approved + rejected
    fp_rate = (rejected / decided) if decided else None
    return {
        "days": days,
        "total_proposed": total,
        "by_status": counts,
        "approved": approved,
        "rejected": rejected,
        "decided": decided,
        "fp_rate": fp_rate,
        # auto_archive_enabled deprecated aqui — agora per-conta em
        # google_accounts.auto_archive_enabled. Use compute_auto_archive_gate().
        "auto_archive_enabled_global_deprecated": False,
    }


# =============================================================================
# CAMADA ESPERTA DO INBOX (25/07/2026) — forward Andressa + triagem inteligente
# =============================================================================
# Tudo gated por INBOX_SMART_LAYER (default OFF). AGE + REVERSÍVEL: só age quando
# há match claro; toda ação passa pelo undo+audit do agent_actions (mesmo contrato
# do task_reconciler). Reuso: signal_router (email->frente), task_reconciler
# (fechar task), agent_actions (undo/audit), gmail (forward/label), notification
# (resumo WA). NÃO reinventa auto-close.


async def _smart_forward_to_andressa(
    gmail, access_token, gmail_id, headers, body,
    account_email, label_ids, name_to_id, dry_run,
) -> Dict:
    """PARTE A — encaminha o email pra Andressa. Idempotente por label Gmail
    próprio (ANDRESSA_FORWARD_LABEL): checa ANTES de reenviar (o cron das 2h não
    re-encaminha) e aplica o label depois. dry_run => só reporta 'would_forward'.

    SILENCIOSO desde 25/07 (não entra no gatilho do resumo WhatsApp). Por isso o
    rastro é explícito: label no Gmail + linha própria em agent_actions (além do
    gmail_label_add que _apply_generic_label já grava). Sem undo_hint: e-mail
    enviado não se desenvia — o que dá pra reverter é o roteamento no Gmail."""
    fwd_label_id = name_to_id.get(ANDRESSA_FORWARD_LABEL)
    if fwd_label_id and fwd_label_id in (label_ids or []):
        return {"status": "already_forwarded", "forwarded": False}
    subject = headers.get("subject") or "(sem assunto)"
    if dry_run:
        return {"status": "would_forward", "would": True}
    orig_from = headers.get("from") or ""
    orig_date = headers.get("date") or ""
    body_text = (body.get("text") or "")[:EMAIL_BODY_MAX_CHARS]
    fwd_body = (
        "Delegado por Renato via triagem automática.\n\n"
        "---------- Mensagem encaminhada ----------\n"
        f"De: {orig_from}\nData: {orig_date}\nAssunto: {subject}\n\n{body_text}"
    )
    res = await gmail.send_message(
        access_token=access_token,
        to=ANDRESSA_FORWARD_TO,
        subject=f"Fwd: {subject}",
        body=fwd_body,
    )
    if isinstance(res, dict) and res.get("error"):
        return {"status": f"send_error:{str(res.get('error'))[:40]}", "forwarded": False}
    # Idempotência: aplica o label próprio (cria se faltar; no-op nas próximas runs).
    try:
        await _apply_generic_label(
            gmail, access_token, gmail_id, account_email, None, None,
            ANDRESSA_FORWARD_LABEL,
        )
    except Exception as e:
        logger.warning("inbox_smart forward label falhou: %s", e)
    # Auditoria explícita da ação silenciosa (surface /agent-actions).
    try:
        from services.agent_actions import log_action
        log_action(
            action_type="email_forwarded_andressa",
            category="email",
            title=f"Encaminhado p/ Andressa: {subject[:80]}",
            details=f"De: {orig_from[:80]} | conta: {_short_account(account_email)}",
            scope_ref={"gmail_id": gmail_id, "account_email": account_email,
                       "to": ANDRESSA_FORWARD_TO},
            source="inbox_smart_layer",
            payload={"subject": subject[:200], "from": orig_from[:120]},
        )
    except Exception as e:
        logger.warning("inbox_smart forward audit falhou: %s", e)
    return {"status": "forwarded", "forwarded": True}


def _smart_candidate_tasks(project_ids, contact_id, limit: int = 12) -> List[Dict]:
    """Tasks pending relacionadas à(s) frente(s) confirmada(s) OU ao contato do
    email. Sem match nenhum => lista vazia (o julgamento nem roda)."""
    project_ids = [p for p in (project_ids or []) if p]
    if not project_ids and not contact_id:
        return []
    clauses, params = [], []
    if project_ids:
        clauses.append("project_id = ANY(%s)")
        params.append(list(project_ids))
    if contact_id:
        clauses.append("contact_id = %s")
        params.append(contact_id)
    where = " OR ".join(clauses)
    sql = f"""
        SELECT id, titulo, descricao, project_id, contact_id,
               data_vencimento, data_criacao
        FROM tasks
        WHERE status = 'pending' AND ({where})
        ORDER BY data_criacao DESC
        LIMIT %s
    """
    params.append(limit)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("inbox_smart candidate_tasks falhou: %s", e)
        return []


async def _smart_judge_resolution(subject, from_hdr, body_text, tasks, api_key) -> Dict:
    """1 chamada LLM (FAST/Haiku, instrumentada) — decide se o email RESOLVE
    (fecha/remarca) UMA das tasks candidatas, de forma inequívoca. Barra alta;
    na dúvida resolved=false. JSON estrito; erro => resolved=false."""
    import httpx
    from services import llm, llm_usage

    task_lines = "\n".join(
        f"[#{t['id']}] {t['titulo']}"
        + (f" — {(t.get('descricao') or '')[:120]}" if t.get("descricao") else "")
        + (f" (vence {t['data_vencimento']:%d/%m/%Y})"
           if t.get("data_vencimento") and hasattr(t["data_vencimento"], "strftime") else "")
        for t in tasks
    )
    prompt = f"""Você decide se um EMAIL recebido evidencia, de forma INEQUÍVOCA, que uma TAREFA pendente foi concluída ou precisa ser remarcada.

EMAIL:
De: {from_hdr}
Assunto: {subject}
Corpo: {(body_text or '')[:1500]}

TAREFAS PENDENTES CANDIDATAS:
{task_lines}

REGRAS (barra ALTA — na dúvida, resolved=false):
- resolved=true SÓ se o email prova claramente que UMA tarefa foi concluída/avançada (ex.: terceiro confirmou "documento enviado", "boleto pago", "assinado/protocolado", "reunião remarcada para DATA").
- action="close" quando a tarefa está concluída.
- action="reschedule" quando o email remarca uma data — informe new_due_date (YYYY-MM-DD).
- Cobrança, pergunta, pedido de decisão, menção casual, newsletter => resolved=false (o Renato decide).
- Escolha UM task_id EXATAMENTE da lista acima; nunca invente.

Responda APENAS um JSON: {{"resolved": true|false, "task_id": <id ou null>, "action": "close"|"reschedule"|"none", "new_due_date": "YYYY-MM-DD"|null, "confidence": 0.0-1.0, "reason": "1 frase curta"}}"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": llm.FAST, "max_tokens": 220,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            logger.warning("inbox_smart judge %s: %s", resp.status_code, resp.text[:150])
            return {"resolved": False, "confidence": 0.0, "reason": "llm erro"}
        result = resp.json()
        try:
            llm_usage.record_response("inbox_smart.judge", llm.FAST, result)
        except Exception:
            pass
        text = result.get("content", [{}])[0].get("text", "")
        s, e = text.find("{"), text.rfind("}") + 1
        if s < 0 or e <= s:
            return {"resolved": False, "confidence": 0.0, "reason": "parse falhou"}
        data = json.loads(text[s:e])
        raw_tid = data.get("task_id")
        tid = None
        if raw_tid not in (None, "", "null"):
            try:
                tid = int(raw_tid)
            except (TypeError, ValueError):
                tid = None
        return {
            "resolved": bool(data.get("resolved")),
            "task_id": tid,
            "action": (data.get("action") or "none"),
            "new_due_date": data.get("new_due_date"),
            "confidence": float(data.get("confidence") or 0.0),
            "reason": str(data.get("reason") or "")[:200],
        }
    except Exception as ex:
        logger.warning("inbox_smart judge erro: %s", ex)
        return {"resolved": False, "confidence": 0.0, "reason": f"erro: {ex}"}


async def _smart_triage_renato(
    *, gmail_id, headers, from_email, contact_id, body_text,
    project_index, api_key,
) -> Dict:
    """PARTE B — o coração. Cruza a frente (signal_router), acha tasks candidatas
    e julga (1 LLM). Retorna:
        {"resolved": True,  "record": {task/action/...}}  -> AGE (fecha/remarca)
        {"resolved": False, "record": {from/subject/reason}} -> mantém !!Renato

    Guardrails: só resolved com conf>=barra E task DA LISTA candidata E ação
    concreta (close/reschedule-com-data). Qualquer dúvida -> não age, escala."""
    from services.signal_router import _prefilter, _confirm_links, _norm as _sr_norm

    subject = headers.get("subject") or ""
    from_hdr = headers.get("from") or ""
    # `stage` = ONDE a camada desistiu. Sem isso a telemetria só sabia que ela não
    # agiu, nunca por quê: medido em 30/07, `smart_reason` era NULL em 51 de 52
    # itens do balde Renato (10 dias), porque o motivo ia só pro resumo de
    # WhatsApp — e o gate de silêncio por produtor (58e6ec1) rebaixou esse resumo
    # pra push, ou seja, o único lugar onde o "por quê" vivia deixou de ser lido.
    # A consequência prática: `smart_resolved` era 0 em 52 oportunidades e não
    # havia como saber se isso é natureza (recibo da Anthropic não fecha task
    # mesmo) ou defeito (o gate de candidatas nunca casando). Decidir o futuro da
    # Parte B sem esse campo seria decidir no escuro.
    needs_record = {"from": from_hdr[:40], "subject": subject[:60], "reason": "",
                    "stage": ""}
    text = f"{subject}\n{body_text[:2000]}"

    # 1. Cruza a frente: prefiltro barato -> confirma LLM só no que passou.
    project_ids: List[int] = []
    if project_index and api_key:
        try:
            cand = _prefilter(_sr_norm(text), project_index)
        except Exception:
            cand = []
        if cand:
            synth = {
                "id": gmail_id, "canal": "email", "sender": from_hdr[:80],
                "direcao": "incoming", "conteudo": text,
            }
            try:
                project_ids = await _confirm_links(synth, cand, api_key)
            except Exception:
                project_ids = []

    # 2. Tasks candidatas (por frente confirmada OU contato). Sem tasks -> escala.
    tasks = _smart_candidate_tasks(project_ids, contact_id)
    if not tasks:
        # Curto-circuito ANTES do LLM: nem projeto confirmado nem contato com
        # ficha. Era ~79% dos casos (52 no balde Renato em 10 dias contra 11
        # chamadas de inbox_smart.judge), e ficava indistinguível de "o LLM
        # analisou e recusou" na telemetria.
        needs_record["stage"] = "no_candidate_task"
        needs_record["reason"] = "sem task/frente casada — decisão sua"
        return {"resolved": False, "record": needs_record}
    if not api_key:
        needs_record["stage"] = "no_api_key"
        needs_record["reason"] = "sem API key — mantido p/ você"
        return {"resolved": False, "record": needs_record}

    # 3. Julga (1 LLM).
    verdict = await _smart_judge_resolution(subject, from_hdr, body_text, tasks, api_key)
    tid = verdict.get("task_id")
    task_map = {t["id"]: t for t in tasks}
    action = verdict.get("action")
    if (
        verdict.get("resolved")
        and float(verdict.get("confidence") or 0.0) >= SMART_RESOLVE_MIN_CONFIDENCE
        and tid in task_map
        and action in ("close", "reschedule")
    ):
        if action == "reschedule" and not verdict.get("new_due_date"):
            needs_record["stage"] = "reschedule_no_date"
            needs_record["reason"] = verdict.get("reason") or "remarcação sem data clara"
            return {"resolved": False, "record": needs_record}
        task = task_map[tid]
        rec = {
            "task_id": tid,
            "titulo": task.get("titulo"),
            "action": action,
            "action_label": "fechei" if action == "close" else "remarquei",
            "new_due_date": verdict.get("new_due_date"),
            "reason": verdict.get("reason") or "",
            "confidence": float(verdict.get("confidence") or 0.0),
            "project_id": task.get("project_id"),
            "contact_id": task.get("contact_id"),
            "task": task,
            "from": from_hdr,
            "subject": subject,
        }
        return {"resolved": True, "record": rec}

    # Chegou ao LLM e ele recusou (ou veio abaixo da barra / task fora da lista
    # candidata). É o caso que diz se a barra está calibrada — distinto de nunca
    # ter julgado.
    needs_record["stage"] = "llm_declined"
    needs_record["reason"] = verdict.get("reason") or "precisa de decisão sua"
    return {"resolved": False, "record": needs_record}


def _smart_reschedule_task(task, new_due_date, confidence, reason) -> None:
    """Remarca data de vencimento da task + log com undo (restaura a data antiga)."""
    old = task.get("data_vencimento")
    if old is None:
        old_sql = "NULL"
    else:
        old_str = old.strftime("%Y-%m-%d") if hasattr(old, "strftime") else str(old)
        old_sql = f"'{old_str}'"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET data_vencimento=%s WHERE id=%s AND status='pending'",
            (new_due_date, task["id"]),
        )
        conn.commit()
    from services.agent_actions import log_action
    log_action(
        action_type="task_rescheduled",
        category="tasks",
        title=f"Tarefa remarcada (inbox esperto): {task['titulo']}",
        details=f"Remarcada p/ {new_due_date} por email. Confiança {confidence:.2f}. {reason}",
        scope_ref={"task_id": task["id"], "contact_id": task.get("contact_id"),
                   "project_id": task.get("project_id")},
        source="inbox_smart_layer",
        payload={"confidence": confidence, "reason": reason, "new_due_date": new_due_date},
        undo_hint=f"UPDATE tasks SET data_vencimento={old_sql} WHERE id={task['id']}",
    )


def _persist_smart_note(project_id, task_id, action_label, from_hdr, subject) -> None:
    """Nota durável na frente ('resolvido pelo email'). Append (tipo próprio),
    NÃO clobbera a nota estado_cos do frente_review. Best-effort."""
    if not project_id:
        return
    try:
        from services.tz import format_brt
        conteudo = (
            f"{action_label.capitalize()} a tarefa #{task_id} a partir do email "
            f"\"{(subject or '')[:120]}\" de {(from_hdr or '')[:80]} "
            f"(triagem inteligente do inbox, {format_brt(now_utc())})."
        )
        meta = {
            "task_id": task_id, "action": action_label, "source": "inbox_smart_layer",
            "from": (from_hdr or "")[:120], "subject": (subject or "")[:200],
        }
        with get_db() as conn:
            cur = conn.cursor()
            ts = now_utc()
            cur.execute(
                """INSERT INTO project_notes
                       (project_id, tipo, titulo, conteudo, autor, metadata, criado_em, atualizado_em)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
                (project_id, SMART_NOTE_TIPO, "Resolvido pelo email (inbox esperto)",
                 conteudo, "inbox_smart_layer", json.dumps(meta, ensure_ascii=False), ts, ts),
            )
            conn.commit()
    except Exception as e:
        logger.warning("inbox_smart note falhou: %s", e)


def _smart_apply_resolution(rec) -> None:
    """AGE: fecha/remarca a task REUSANDO o caminho do task_reconciler (fechar) —
    undo+audit garantidos — + grava a nota durável na frente."""
    task = rec["task"]
    if rec["action"] == "close":
        from services.task_reconciler import _close_task
        _close_task(task, {"confidence": rec["confidence"], "reason": rec["reason"]})
    elif rec["action"] == "reschedule":
        _smart_reschedule_task(task, rec.get("new_due_date"), rec["confidence"], rec["reason"])
    _persist_smart_note(
        rec.get("project_id"), rec["task_id"], rec["action_label"],
        rec.get("from"), rec.get("subject"),
    )


def _dedup_by_subject_sender(items: List[Dict]) -> List[Dict]:
    """Dedup por (assunto, remetente) normalizados, preservando a ordem.

    Medido 27/07: "CDA's Federais - Empresas Inativas" saiu DUAS VEZES na MESMA
    mensagem. CAUSA-RAIZ (auditoria, cron_runs da run 14:25): o mesmo e-mail
    está nas DUAS contas Google e o run varre as duas — o result_json traz o
    mesmo assunto com `account: pro` e `account: pess`. Por isso a chave é
    (assunto, remetente) IGNORANDO a conta e ignorando o gmail_id: os ids são
    diferentes por definição, dedup por id não pegaria nada."""
    seen, out = set(), []
    for it in items or []:
        key = (
            _norm_txt(str(it.get("subject") or ""))[:120],
            _norm_txt(str(it.get("from") or ""))[:80],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _dedup_by_task_id(items: List[Dict]) -> List[Dict]:
    """Mesma classe de duplicata do bloco acima, do lado do 'resolvi sozinho':
    duas contas podem trazer o e-mail que fecha a MESMA task."""
    seen, out = set(), []
    for it in items or []:
        tid = it.get("task_id")
        if tid in seen:
            continue
        seen.add(tid)
        out.append(it)
    return out


def _smart_summaries_sent_today() -> int:
    """Quantos resumos da camada já saíram HOJE (dia BRT). Ledger = agent_actions
    (sem DDL): o próprio _send_smart_summary grava 'inbox_smart_summary_sent'
    depois de enviar. Falha de leitura => 0 (na dúvida deixa falar; o custo de
    um resumo a mais é menor que o de silenciar um 'desfaz')."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            # criado_em é TIMESTAMP naive em UTC — normaliza pra data BRT com o
            # padrão do projeto (CLAUDE.md, "SQL com timezone") em vez de fazer
            # a conta em Python.
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM agent_actions
                WHERE action_type = 'inbox_smart_summary_sent'
                  AND (criado_em AT TIME ZONE 'UTC'
                       AT TIME ZONE 'America/Sao_Paulo')::date
                      = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                """
            )
            r = cur.fetchone()
            return int((r["n"] if r else 0) or 0)
    except Exception as e:
        logger.warning("inbox_smart budget read falhou: %s", e)
        return 0


async def _send_smart_summary(stats, forwarded, resolved_items, needs_you, dry_run) -> None:
    """PARTE C — resumo consolidado do que a camada FEZ. O GATILHO fica no caller.

    Mudanças 27/07 (feedback do Renato, medição de 10 itens em produção):
      - "Precisa de você" SAI do WhatsApp por padrão (kill-switch
        INBOX_SMART_NEEDS_YOU_WA=1 devolve). O que a camada não resolveu já tem
        dono: !!Renato no Gmail + row em email_triage -> urgent-tick / briefing.
      - Dedup por (assunto, remetente) na montagem (o item repetido não sai 2x).
      - Teto de 1 resumo por dia BRT (INBOX_SMART_SUMMARY_MAX_PER_DAY=0 remove o
        teto). O que ela fez também entra no digest "Fiz por você" do briefing
        matinal via agent_actions — o WhatsApp aqui é o extra, não a fonte.
    dry_run => preview no stats, não envia e não consome o teto."""
    show_needs_you = is_smart_needs_you_wa_enabled()
    resolved_items = _dedup_by_task_id(resolved_items)
    needs_you = _dedup_by_subject_sender(needs_you)

    lines = ["🤖 Triagem inteligente (inbox)"]
    if forwarded:
        _fwd_verb = "Encaminharia" if dry_run else "Encaminhei"
        lines.append(f"• {_fwd_verb} p/ Andressa: {forwarded}")
    if resolved_items:
        parts = []
        for r in resolved_items:
            rr = (r.get("reason") or "")[:40]
            parts.append(
                f"{r.get('action_label')} #{r.get('task_id')}" + (f" ({rr})" if rr else "")
            )
        lines.append("• Resolvi sozinho: " + " · ".join(parts))
        # 27/07 — a promessa "responde 'desfaz N'" estava MENTINDO: o comando só
        # existe em intel_bot.handle_bot_message, alcançável pela instância
        # intel-bot-v2; este resumo sai pela rap-whatsapp em self-chat, onde a
        # resposta cai em evolution_api.process_incoming_message, que não tem
        # branch de bot. Evidência: agent_actions em 10 dias = 12 'done', ZERO
        # 'undone'. Sem receptor, a linha só ensina o Renato a desconfiar do
        # resumo. Volta por env quando o roteamento do bot for religado (decisão
        # de infra, fora deste arquivo).
        if is_smart_undo_hint_enabled():
            lines.append(f"  desfaz: responde \"desfaz {resolved_items[0].get('task_id')}\"")
        else:
            lines.append("  reverter: /agent-actions (undo por ação)")
    if needs_you and show_needs_you:
        lines.append(f"• Precisa de você ({len(needs_you)}):")
        for n in needs_you[:5]:
            subj = (n.get("subject") or "(sem assunto)")[:50]
            frm = (n.get("from") or "")[:28]
            lines.append(f"  - {subj} ({frm})")
        if len(needs_you) > 5:
            lines.append(f"  - +{len(needs_you) - 5} outros")
    elif needs_you:
        # Não repassa a lista (isso é do urgent-tick/briefing); só declara que a
        # fila !!Renato cresceu, sem virar segunda leitura do mesmo e-mail.
        lines.append(f"• Deixei {len(needs_you)} em !!Renato pra você (vem no briefing)")

    text = "\n".join(lines)
    if dry_run:
        stats["smart_summary_preview"] = text
        return

    cap = smart_summary_max_per_day()
    if cap > 0:
        already = _smart_summaries_sent_today()
        if already >= cap:
            stats["smart_summary_skipped"] = f"daily_cap:{already}/{cap}"
            return

    from services.notification_router import notify
    from services.tz import to_brt as _to_brt
    dedup = f"inbox_smart:{_to_brt(now_utc()):%Y%m%d}"
    # topics SO quando a lista de needs_you esta de fato no texto: e nesse caso
    # que este resumo pode repetir um e-mail que o urgent-tick ja mandou (caso
    # Orestes, 26 e 27/07). Com show_needs_you=False o resumo fala do que a
    # camada FEZ — mandar os assuntos ali faria o router rebaixa-lo por causa de
    # um e-mail que ele nem menciona.
    _topics = ([n.get("subject") for n in needs_you if n.get("subject")]
               if (needs_you and show_needs_you) else None)
    await notify(
        "inbox_smart", "Triagem inteligente", text, 8,
        msg_type="inbox_smart_summary", dedup=dedup,
        topics=_topics,
    )
    stats["smart_summary_sent"] = True
    # Consome o teto do dia. category='system' de propósito: o digest "Fiz por
    # você" já lista as ações REAIS (forward/task) — este row é só o ledger do
    # orçamento diário, não uma ação a mais no mundo do Renato.
    try:
        from services.agent_actions import log_action
        log_action(
            action_type="inbox_smart_summary_sent",
            category="system",
            title="Resumo da triagem inteligente enviado",
            details=f"{len(resolved_items)} resolvido(s), {forwarded} encaminhado(s), "
                    f"{len(needs_you)} em !!Renato",
            source="inbox_smart_layer",
            payload={"resolved": len(resolved_items), "forwarded": forwarded,
                     "needs_you": len(needs_you), "needs_you_in_wa": show_needs_you},
        )
    except Exception as e:
        logger.warning("inbox_smart budget write falhou: %s", e)


def run_smart_undo(task_id: int) -> Dict:
    """Desfaz a última ação da camada esperta sobre a task (reabre/restaura data).
    Reusa o undo+audit do agent_actions (mesmo contrato do task_reconciler) —
    executa o undo_hint gravado e marca a ação como 'undone'. Usado pelo comando
    WhatsApp 'desfaz N' (N = task id). Aceita também a ação do próprio reconciler
    (fonte-agnóstico por task_id) — desfazer um fechamento é sempre válido."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, undo_hint, action_type, title
                FROM agent_actions
                WHERE status = 'done'
                  AND undo_hint IS NOT NULL
                  AND action_type IN ('task_resolved', 'task_rescheduled')
                  AND (scope_ref->>'task_id') = %s
                ORDER BY criado_em DESC
                LIMIT 1
                """,
                (str(task_id),),
            )
            row = cur.fetchone()
        if not row:
            return {"undone": False, "reason": "nada recente pra desfazer"}
        hint = (row["undo_hint"] or "").strip()
        up = hint.upper()
        if not (up.startswith("UPDATE ") or up.startswith("DELETE ")):
            return {"undone": False, "reason": "undo inválido"}
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(hint)
            conn.commit()
        from services.agent_actions import mark_undone
        mark_undone(row["id"])
        return {"undone": True, "action_id": row["id"], "titulo": row.get("title")}
    except Exception as e:
        logger.warning("run_smart_undo falhou (task %s): %s", task_id, e)
        return {"undone": False, "reason": str(e)[:80]}


# Singleton
_email_triage_service = None


def get_email_triage_service() -> EmailTriageService:
    global _email_triage_service
    if _email_triage_service is None:
        _email_triage_service = EmailTriageService()
    return _email_triage_service


# =============================================================================
# AGING POLICY (24/06/2026)
# =============================================================================
# Auto-dismiss pendings que estouraram a janela. Pra `archive_proposed`,
# também arquiva no Gmail (remove INBOX). Roda 1x/dia via cron Railway worker.

AGING_POLICY = {
    "urgent": 14,
    "must_read": 14,
    "important": 7,
    "silent": 2,
    "archive_proposed": 3,
}


async def apply_aging_policy(retroactive: bool = False, dry_run: bool = False) -> Dict:
    """Aplica política de aging nos pendings.

    Args:
        retroactive: se True, processa TODOS os pendings (uso 1x pra limpar backlog).
                     se False (default), só processa quem vai estourar janela hoje.
        dry_run: se True, só conta o que seria feito, sem alterar.

    Para `archive_proposed`, também arquiva no Gmail (remove INBOX label).
    Demais classificações: apenas marca status='dismissed'.

    Returns:
        {dismissed: {silent: N, ...}, archived_gmail: N, errors: N, dry_run: bool}
    """
    from integrations.gmail import GmailIntegration

    stats = {
        "dismissed": {k: 0 for k in AGING_POLICY},
        "archived_gmail": 0,
        "gmail_errors": 0,
        "skipped_no_token": 0,
        "dry_run": dry_run,
        "retroactive": retroactive,
    }

    gmail_client = GmailIntegration()
    token_cache: Dict[str, str] = {}  # account_email -> access_token

    async def get_token(account_email: str) -> Optional[str]:
        if account_email in token_cache:
            return token_cache[account_email]
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT refresh_token FROM google_accounts WHERE email = %s",
                (account_email,),
            )
            row = cur.fetchone()
            if not row or not row.get("refresh_token"):
                return None
            rr = await gmail_client.refresh_access_token(row["refresh_token"])
            if "access_token" not in rr:
                return None
            token_cache[account_email] = rr["access_token"]
            return rr["access_token"]

    # Query targets: pendings estourados
    with get_db() as conn:
        cursor = conn.cursor()

        for classification, days in AGING_POLICY.items():
            cursor.execute(
                """
                SELECT et.id, et.classification, et.criado_em, et.account_email,
                       m.external_id as gmail_id
                FROM email_triage et
                LEFT JOIN messages m ON m.id = et.message_id
                WHERE et.status = 'pending'
                  AND et.classification = %s
                  AND et.criado_em < NOW() - INTERVAL '%s days'
                """,
                (classification, days),
            )
            targets = list(cursor.fetchall())

            if not retroactive:
                # Modo daily: só pega quem virou stale exatamente hoje
                # (idempotente — se já passou da janela e nao foi pego ontem, pega hoje)
                pass  # mesma query funciona

            for row in targets:
                triage_id = row["id"]
                gmail_id = row.get("gmail_id")
                account_email = row.get("account_email")

                # Para archive_proposed: arquivar no Gmail se possível
                if classification == "archive_proposed" and gmail_id and account_email:
                    if dry_run:
                        stats["archived_gmail"] += 1
                    else:
                        token = await get_token(account_email)
                        if not token:
                            stats["skipped_no_token"] += 1
                        else:
                            try:
                                ok = await gmail_client.archive_message(token, gmail_id)
                                if ok:
                                    stats["archived_gmail"] += 1
                                else:
                                    stats["gmail_errors"] += 1
                            except Exception as e:
                                logger.warning(f"aging archive falhou triage_id={triage_id}: {e}")
                                stats["gmail_errors"] += 1

                # Marca dismissed (todos os tipos)
                if not dry_run:
                    cursor.execute(
                        """
                        UPDATE email_triage
                        SET status = 'dismissed',
                            dismissed_at = NOW(),
                            action_taken = %s
                        WHERE id = %s
                        """,
                        (f"aging_{classification}_{days}d", triage_id),
                    )

                stats["dismissed"][classification] += 1

            if not dry_run:
                conn.commit()

    return stats
