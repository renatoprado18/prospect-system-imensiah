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
    html = body.get("html") or ""
    if not text and html:
        text = _html_to_text(html)
    return text[:EMAIL_BODY_MAX_CHARS], html


# Cap diario pra aplicacao de label !!Renato pelo CoS (Commit 4).
# Hardcoded pra protecao contra bug em loop. Estado mantido em
# agent_actions.action_type='gmail_label_add'.
MAX_LABELS_PER_DAY = 50

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
        FINANCIAL_DOMAINS = (
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
        GOV_DOMAINS = (".gov.br", ".jus.br")
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

        # Telco exception: dominios telco entram no FINANCIAL_DOMAINS pq
        # fatura real e legitima, mas newsletters promocionais (oferta wifi,
        # multas, desconto) sao noise. Se bate promo SEM bate fatura, sai
        # do whitelist (vai cair em R4/R5 e arquivar).
        TELCO_DOMAINS = ("claro.com.br", "mi.claro.com.br", "vivo.com.br", "tim.com.br")
        is_telco = bool(sender_domain) and any(sender_domain.endswith(d) for d in TELCO_DOMAINS)
        if is_telco and is_financial_domain:
            fatura_kw = (
                "fatura", "boleto", "vencimento", "pagamento devido",
                "atraso", "nota fiscal", "segunda via",
            )
            promo_kw = (
                "oferta", "promocao", "promoção", "desconto", "ganhe",
                "contrate", "novidade", "wifi mais", "wi-fi mais",
            )
            has_fatura = any(k in text_lc_fin for k in fatura_kw)
            has_promo = any(k in text_lc_fin for k in promo_kw)
            if has_promo and not has_fatura:
                # Promo telco — nao vira must_read, fluir pra R4/R5
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
        if is_andressa_domain or is_andressa_sender:
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
        # Re-extrai do payload header (mais robusto)
        headers_lower = {k.lower(): v for k, v in (headers or {}).items()}
        has_unsubscribe = bool(list_unsubscribe or headers_lower.get("list-unsubscribe"))

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

    Args:
        hours: janela de busca pra tras (default 1h pra cron 30min).

    Returns:
        {
            ok: bool,
            processed: int,
            by_account: {email: count},
            by_classification: {must_read: N, archive_proposed: N, silent: N},
            label_applied: int,
            shadow_proposals: int,
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
                        classification in ("must_read",) and dup_status == "pending",
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

            # 4f. Aplicar label apropriado se must_read + conf >= 0.85
            # 25/06/2026: roteamento por suggested_tags em vez de sempre !!Renato.
            # - tags inclui !Andressa/andressa/admin -> !Andressa
            # - tags inclui financeiro/billing/infra-critico -> !Recibos/Financeiro
            #   (ou 7-Financeiro/Recibos profissional). infra-critico ALEM disso ganha !!Renato.
            # - resto must_read -> !!Renato (default)
            if classification == "must_read" and confidence >= 0.85:
                tags_lower = [str(t).lower() for t in (decision.get("suggested_tags") or [])]
                labels_to_apply = []
                if any(t in tags_lower for t in ("!andressa", "andressa", "admin")):
                    labels_to_apply.append("!Andressa")
                elif any(t in tags_lower for t in ("financeiro", "billing", "gov", "infra-critico")):
                    labels_to_apply.append(_label_name_financeiro(ref["account_email"]))
                    if "infra-critico" in tags_lower:
                        labels_to_apply.append("!!Renato")
                else:
                    labels_to_apply.append("!!Renato")

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
            # imediatamente via intel-bot-v2 (instancia de notificacoes CoS).
            priority_val = int(decision.get("priority", 5))
            if classification == "must_read" and priority_val >= 9:
                try:
                    from services.intel_bot import send_intel_notification
                    sender_short = (headers.get("from") or "")[:60]
                    subj_short = (headers.get("subject") or "(sem assunto)")[:70]
                    reasons_short = "; ".join(decision.get("reasons", [])[:2])[:120]
                    notif_text = (
                        f"📧 Email urgente (P{priority_val})\n"
                        f"De: {sender_short}\n"
                        f"Assunto: {subj_short}\n"
                        f"Por que: {reasons_short}"
                    )
                    pushed = await send_intel_notification(notif_text)
                    if pushed:
                        stats["wa_urgents_pushed"] = stats.get("wa_urgents_pushed", 0) + 1
                except Exception as e:
                    stats["errors"].append(f"wa_push {gmail_id[:12]}: {str(e)[:80]}")

            # 4g. Archive proposals — shadow OU real (gate per-conta, 24/06/2026)
            if classification == "archive_proposed":
                acct_email = ref["account_email"]
                acct_auto_enabled = is_auto_archive_enabled(acct_email)
                try:
                    if acct_auto_enabled:
                        # Auto-archive ATIVADO pra essa conta — arquiva real no Gmail
                        try:
                            archived_ok = await gmail.archive_message(
                                ref["access_token"], gmail_id
                            )
                        except Exception as e:
                            archived_ok = False
                            stats["errors"].append(f"auto_archive {gmail_id[:12]}: {str(e)[:80]}")
                        prop_result = await _create_shadow_proposal(
                            triage_id=triage_id,
                            gmail_id=gmail_id,
                            account_email=acct_email,
                            headers=headers,
                            decision=decision,
                        )
                        # Marca proposal como archived direto
                        if prop_result.get("created") and archived_ok:
                            with get_db() as _conn:
                                _cur = _conn.cursor()
                                _cur.execute(
                                    """
                                    UPDATE email_archive_proposals
                                    SET status = 'archived',
                                        ratified_at = NOW(),
                                        ratified_by = 'auto_gate'
                                    WHERE id = %s
                                    """,
                                    (prop_result.get("proposal_id"),),
                                )
                                _conn.commit()
                            stats["auto_archived"] = stats.get("auto_archived", 0) + 1
                    else:
                        # Shadow mode (default — gate ainda nao destravou)
                        prop_result = await _create_shadow_proposal(
                            triage_id=triage_id,
                            gmail_id=gmail_id,
                            account_email=acct_email,
                            headers=headers,
                            decision=decision,
                        )
                        if prop_result.get("created"):
                            stats["shadow_proposals"] += 1
                except Exception as e:
                    stats["errors"].append(f"shadow {gmail_id[:12]}: {str(e)[:80]}")

        except Exception as e:
            stats["errors"].append(f"process {gmail_id[:12]}: {str(e)[:120]}")
            logger.exception(f"sweep process {gmail_id}")

    stats["duration_ms"] = int((time.time() - started) * 1000)
    return stats


def _label_name_financeiro(account_email: str) -> str:
    """Label de financeiro por conta. Nomes diferentes nas 2 contas."""
    if account_email == "renato.almeida.prado@gmail.com":
        return "!Recibos/Financeiro"
    return "7-Financeiro/Recibos"


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
