"""
CoS Investigator Tools — Onda 2 (10/jun/2026).

8 tools que o Investigator agent usa pra descobrir relevantes e agir:

Read (não modifica DB, retorna JSON compacto):
    1. search_messages(query, hours, contact_id) -> List[Dict]
    2. get_messages_with(contact_id_or_name, hours) -> Dict
    3. get_overdue_tasks(limit, project_filter) -> List[Dict]
    4. get_calendar(date_start, date_end) -> List[Dict]
    5. get_proposals(status) -> List[Dict]

Write (escreve em DB, retorna ID/ack):
    6. create_draft_response(contact_id, channel, text_draft, motivo) -> Dict
    7. record_observation(texto, frente, refs) -> Dict
    8. escalate_to_user(texto, motivo, refs, prioridade) -> Dict

Toda tool chama log_tool_call() no fim (success ou error) pra registrar
em cos_action_log — auditoria + custo + debug.

Tool definitions em formato Anthropic Tool Use exportadas em COS_TOOLS.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

from database import get_db

logger = logging.getLogger(__name__)


# ============== Internal helpers ==============

def log_tool_call(
    cycle_id: str,
    tool_name: str,
    params: Dict[str, Any],
    result: Any,
    iteration: int,
    duration_ms: int,
    error: Optional[str] = None,
) -> None:
    """Insere row em cos_action_log. Nunca raise — log de auditoria não
    pode bloquear o agent loop."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cos_action_log
                    (cycle_id, tool_name, params, result, iteration, duration_ms, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cycle_id,
                    tool_name,
                    json.dumps(params, default=str)[:8000],
                    json.dumps(result, default=str)[:8000] if result is not None else None,
                    iteration,
                    duration_ms,
                    (error[:4000] if error else None),
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"cos_tools.log_tool_call falhou: {e}")


def register_feito(
    cycle_id: str,
    texto: str,
    frente: Optional[int],
    refs: Optional[Dict],
) -> Optional[int]:
    """Helper interno: insere item categoria='feito' em cos_briefing_items.
    Usado como side-effect quando write tools concluem com sucesso
    (ex: create_draft_response cria um ✅ correspondente)."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cos_briefing_items
                    (cycle_id, categoria, frente, texto, refs, prioridade)
                VALUES (%s, 'feito', %s, %s, %s, 5)
                RETURNING id
                """,
                (cycle_id, frente, texto, json.dumps(refs or {}, default=str)),
            )
            r = cursor.fetchone()
            conn.commit()
            return r["id"] if r else None
    except Exception as e:
        logger.warning(f"cos_tools.register_feito falhou: {e}")
        return None


def _truncate(s: Optional[str], n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n - 1] + "…"


def _peso_estimado_task(contact_id: Optional[int], projeto: Optional[str]) -> int:
    """Heurística simples pra estimar peso de uma task.
    3 = alta (contato C0 tipo Vallen ou projeto crítico).
    2 = média (default).
    1 = baixa (sem contexto)."""
    if not projeto and not contact_id:
        return 1
    if projeto:
        p_lower = (projeto or "").lower()
        if any(k in p_lower for k in ("vallen", "imensiah", "alba", "wadhwani")):
            return 3
    return 2


# ============== READ TOOLS ==============

def search_messages(
    cycle_id: str,
    iteration: int,
    query: str,
    hours: int = 48,
    contact_id: Optional[int] = None,
) -> List[Dict]:
    """Busca mensagens WA por texto (ILIKE) nas ultimas N horas. Max 30."""
    started = time.time()
    params_log = {"query": query, "hours": hours, "contact_id": contact_id}
    try:
        since = datetime.now() - timedelta(hours=hours)
        with get_db() as conn:
            cursor = conn.cursor()
            sql = """
                SELECT m.id, m.contact_id, c.nome AS contact_name,
                       m.direcao, m.conteudo, m.enviado_em
                FROM messages m
                LEFT JOIN contacts c ON c.id = m.contact_id
                WHERE m.conteudo ILIKE %s
                  AND COALESCE(m.enviado_em, m.criado_em) >= %s
            """
            args: List[Any] = [f"%{query}%", since]
            if contact_id:
                sql += " AND m.contact_id = %s"
                args.append(contact_id)
            sql += " ORDER BY COALESCE(m.enviado_em, m.criado_em) DESC LIMIT 30"
            cursor.execute(sql, args)
            rows = cursor.fetchall()
            result = [
                {
                    "id": r["id"],
                    "contact_id": r["contact_id"],
                    "contact_name": r["contact_name"],
                    "direcao": r["direcao"],
                    "conteudo": _truncate(r["conteudo"], 200),
                    "enviado_em": r["enviado_em"].isoformat() if r["enviado_em"] else None,
                }
                for r in rows
            ]
        log_tool_call(cycle_id, "search_messages", params_log, result, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "search_messages", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return []


def get_messages_with(
    cycle_id: str,
    iteration: int,
    contact_id_or_name: str,
    hours: int = 48,
) -> Dict:
    """Resolve contato por id ou nome (ILIKE) e retorna ultimas mensagens.
    Retorno: {contact_id, nome, mensagens: [...]}. Max 25 mensagens."""
    started = time.time()
    params_log = {"contact_id_or_name": contact_id_or_name, "hours": hours}
    try:
        cid: Optional[int] = None
        nome: Optional[str] = None
        with get_db() as conn:
            cursor = conn.cursor()
            # Tenta como int primeiro
            try:
                cid_int = int(contact_id_or_name)
                cursor.execute("SELECT id, nome FROM contacts WHERE id = %s", (cid_int,))
                r = cursor.fetchone()
                if r:
                    cid, nome = r["id"], r["nome"]
            except (ValueError, TypeError):
                pass

            if cid is None:
                cursor.execute(
                    "SELECT id, nome FROM contacts WHERE nome ILIKE %s ORDER BY total_interacoes DESC NULLS LAST LIMIT 1",
                    (f"%{contact_id_or_name}%",),
                )
                r = cursor.fetchone()
                if r:
                    cid, nome = r["id"], r["nome"]

            if cid is None:
                result = {"contact_id": None, "nome": None, "mensagens": [], "erro": "contato_nao_encontrado"}
                log_tool_call(cycle_id, "get_messages_with", params_log, result, iteration, int((time.time() - started) * 1000))
                return result

            since = datetime.now() - timedelta(hours=hours)
            cursor.execute(
                """
                SELECT id, direcao, conteudo, enviado_em
                FROM messages
                WHERE contact_id = %s
                  AND COALESCE(enviado_em, criado_em) >= %s
                ORDER BY COALESCE(enviado_em, criado_em) DESC
                LIMIT 25
                """,
                (cid, since),
            )
            mensagens = [
                {
                    "id": r["id"],
                    "direcao": r["direcao"],
                    "conteudo": _truncate(r["conteudo"], 300),
                    "enviado_em": r["enviado_em"].isoformat() if r["enviado_em"] else None,
                }
                for r in cursor.fetchall()
            ]

        result = {"contact_id": cid, "nome": nome, "mensagens": mensagens}
        log_tool_call(cycle_id, "get_messages_with", params_log, {"contact_id": cid, "nome": nome, "n_mensagens": len(mensagens)}, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "get_messages_with", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return {"contact_id": None, "nome": None, "mensagens": [], "erro": str(e)}


def get_overdue_tasks(
    cycle_id: str,
    iteration: int,
    limit: int = 20,
    project_filter: Optional[str] = None,
) -> List[Dict]:
    """Tasks com data_vencimento < NOW e status != 'done'. Max 20."""
    started = time.time()
    params_log = {"limit": limit, "project_filter": project_filter}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            sql = """
                SELECT t.id, t.titulo, t.data_vencimento, t.contact_id, t.project_id,
                       p.nome AS projeto, c.nome AS contact_name
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                LEFT JOIN contacts c ON c.id = t.contact_id
                WHERE t.status != 'done'
                  AND t.data_vencimento IS NOT NULL
                  AND t.data_vencimento < NOW()
            """
            args: List[Any] = []
            if project_filter:
                sql += " AND (p.nome ILIKE %s)"
                args.append(f"%{project_filter}%")
            sql += " ORDER BY t.data_vencimento ASC LIMIT %s"
            args.append(min(limit, 20))
            cursor.execute(sql, args)
            rows = cursor.fetchall()
            today = datetime.now().date()
            result = []
            for r in rows:
                dv = r["data_vencimento"]
                dias_atraso = (today - dv.date()).days if dv else 0
                result.append({
                    "id": r["id"],
                    "titulo": _truncate(r["titulo"], 80),
                    "projeto": r["projeto"],
                    "contact_id": r["contact_id"],
                    "contact_name": r["contact_name"],
                    "data_vencimento": dv.isoformat() if dv else None,
                    "dias_atraso": dias_atraso,
                    "peso_estimado": _peso_estimado_task(r["contact_id"], r["projeto"]),
                })
        log_tool_call(cycle_id, "get_overdue_tasks", params_log, {"n": len(result)}, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "get_overdue_tasks", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return []


def get_calendar(
    cycle_id: str,
    iteration: int,
    date_start: str,
    date_end: str,
) -> List[Dict]:
    """Eventos calendar_events no range [date_start, date_end] (YYYY-MM-DD). Max 30."""
    started = time.time()
    params_log = {"date_start": date_start, "date_end": date_end}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT e.id, e.summary, e.start_datetime, e.end_datetime,
                       e.location, e.attendees, e.contact_id, c.nome AS contact_name
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                WHERE e.start_datetime >= %s::date
                  AND e.start_datetime < (%s::date + INTERVAL '1 day')
                ORDER BY e.start_datetime ASC
                LIMIT 30
                """,
                (date_start, date_end),
            )
            rows = cursor.fetchall()
            result = []
            for r in rows:
                # attendees é JSONB array de objetos
                att_raw = r["attendees"] or []
                att_names = []
                if isinstance(att_raw, list):
                    for a in att_raw[:5]:
                        if isinstance(a, dict):
                            att_names.append(a.get("displayName") or a.get("email") or "?")
                result.append({
                    "id": r["id"],
                    "titulo": _truncate(r["summary"], 80),
                    "inicio": r["start_datetime"].isoformat() if r["start_datetime"] else None,
                    "fim": r["end_datetime"].isoformat() if r["end_datetime"] else None,
                    "local": _truncate(r["location"], 60),
                    "contact_id": r["contact_id"],
                    "contact_name": r["contact_name"],
                    "participantes": att_names,
                })
        log_tool_call(cycle_id, "get_calendar", params_log, {"n": len(result)}, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "get_calendar", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return []


def get_proposals(
    cycle_id: str,
    iteration: int,
    status: str = "pending",
) -> List[Dict]:
    """action_proposals por status. Max 20."""
    started = time.time()
    params_log = {"status": status}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT p.id, p.action_type, p.title, p.description,
                       p.contact_id, c.nome AS contact_name, p.criado_em, p.urgency
                FROM action_proposals p
                LEFT JOIN contacts c ON c.id = p.contact_id
                WHERE p.status = %s
                ORDER BY p.criado_em DESC
                LIMIT 20
                """,
                (status,),
            )
            rows = cursor.fetchall()
            result = [
                {
                    "id": r["id"],
                    "tipo": r["action_type"],
                    "titulo": _truncate(r["title"], 100),
                    "descricao": _truncate(r["description"], 150),
                    "contact_id": r["contact_id"],
                    "contact_name": r["contact_name"],
                    "urgency": r["urgency"],
                    "criado_em": r["criado_em"].isoformat() if r["criado_em"] else None,
                }
                for r in rows
            ]
        log_tool_call(cycle_id, "get_proposals", params_log, {"n": len(result)}, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "get_proposals", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return []


def get_pending_email_triage(
    cycle_id: str,
    iteration: int,
    limit: int = 20,
    classification: Optional[str] = None,
) -> List[Dict]:
    """Email triage pendente (sweep_email_triage popula). Max 25.

    Args:
        limit: max items (clamp 1-25).
        classification: filtro 'must_read' | 'archive_proposed' | 'silent' (None=todos).

    Retorna [{triage_id, message_id (FK), gmail_id, account_email, sender, subject,
              classification, priority, ai_confidence, suggested_tags, criado_em}, ...].
    Ordenado por priority DESC, ai_confidence DESC.
    """
    started = time.time()
    params_log = {"limit": limit, "classification": classification}
    try:
        n = max(1, min(int(limit or 20), 25))
        with get_db() as conn:
            cursor = conn.cursor()
            sql = """
                SELECT
                    et.id AS triage_id,
                    et.message_id,
                    m.external_id AS gmail_id,
                    et.account_email,
                    et.account_type,
                    et.classification,
                    et.priority,
                    et.ai_confidence,
                    et.suggested_tags,
                    et.status,
                    et.criado_em,
                    m.metadata->>'from_name' AS from_name,
                    m.metadata->>'from' AS from_email,
                    m.metadata->>'subject' AS subject_meta,
                    c.assunto AS subject,
                    ct.nome AS contact_name,
                    ct.circulo
                FROM email_triage et
                LEFT JOIN messages m ON m.id = et.message_id
                LEFT JOIN conversations c ON c.id = et.conversation_id
                LEFT JOIN contacts ct ON ct.id = et.contact_id
                WHERE et.status IN ('pending', 'archive_proposed_shadow')
            """
            args: List[Any] = []
            if classification:
                sql += " AND et.classification = %s"
                args.append(classification)
            sql += " ORDER BY et.priority DESC NULLS LAST, et.ai_confidence DESC NULLS LAST, et.criado_em DESC LIMIT %s"
            args.append(n)
            cursor.execute(sql, args)
            rows = cursor.fetchall()

            result: List[Dict] = []
            for r in rows:
                sender = r.get("from_name") or r.get("from_email") or r.get("contact_name") or "?"
                subject = r.get("subject_meta") or r.get("subject") or ""
                acc = r.get("account_email") or r.get("account_type") or "?"
                # Conta curta pra LLM
                acc_short = "pro" if (acc and "almeida-prado" in acc) else (
                    "pess" if (acc and "gmail.com" in acc) else acc
                )
                result.append({
                    "triage_id": r["triage_id"],
                    "message_id": r["message_id"],
                    "gmail_id": r.get("gmail_id"),
                    "account": acc_short,
                    "account_email": acc,
                    "sender": _truncate(sender, 60),
                    "subject": _truncate(subject, 80),
                    "classification": r["classification"],
                    "priority": r["priority"],
                    "ai_confidence": float(r["ai_confidence"]) if r["ai_confidence"] is not None else None,
                    "suggested_tags": r.get("suggested_tags") or [],
                    "status": r.get("status"),
                    "contact_circulo": r.get("circulo"),
                    "criado_em": r["criado_em"].isoformat() if r["criado_em"] else None,
                })

        log_tool_call(cycle_id, "get_pending_email_triage", params_log, {"n": len(result)}, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "get_pending_email_triage", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return []


# ============== WRITE TOOLS ==============

def create_draft_response(
    cycle_id: str,
    iteration: int,
    contact_id: int,
    channel: str,
    text_draft: str,
    motivo: str,
) -> Dict:
    """Insere rascunho em cos_draft_responses (status='pending', expira 48h).
    Side-effect: cria um item ✅ correspondente em cos_briefing_items."""
    started = time.time()
    params_log = {"contact_id": contact_id, "channel": channel, "motivo": _truncate(motivo, 100)}
    try:
        if channel not in ("whatsapp", "email", "linkedin_dm"):
            raise ValueError(f"channel inválido: {channel}")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cos_draft_responses
                    (contact_id, channel, text_draft, motivo)
                VALUES (%s, %s, %s, %s)
                RETURNING id, expires_at
                """,
                (contact_id, channel, text_draft, motivo),
            )
            row = cursor.fetchone()
            # Resolve nome do contato pro item ✅
            cursor.execute("SELECT nome FROM contacts WHERE id = %s", (contact_id,))
            cn = cursor.fetchone()
            conn.commit()

        draft_id = row["id"]
        expires = row["expires_at"]
        nome = cn["nome"] if cn else f"contato #{contact_id}"

        # Side-effect: item ✅
        feito_id = register_feito(
            cycle_id,
            f"Rascunhei resposta {channel} pra {nome} (draft #{draft_id}): {_truncate(motivo, 80)}",
            None,
            {"draft_id": draft_id, "contact_id": contact_id, "channel": channel},
        )

        result = {
            "draft_id": draft_id,
            "expires_at": expires.isoformat() if expires else None,
            "feito_item_id": feito_id,
        }
        log_tool_call(cycle_id, "create_draft_response", params_log, result, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "create_draft_response", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return {"erro": str(e)}


def record_observation(
    cycle_id: str,
    iteration: int,
    texto: str,
    frente: Optional[int] = None,
    refs: Optional[Dict] = None,
) -> Dict:
    """Insere item categoria='monitor' (👀) em cos_briefing_items."""
    started = time.time()
    params_log = {"texto": _truncate(texto, 80), "frente": frente, "refs": refs}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cos_briefing_items
                    (cycle_id, categoria, frente, texto, refs, prioridade)
                VALUES (%s, 'monitor', %s, %s, %s, 6)
                RETURNING id
                """,
                (cycle_id, frente, texto, json.dumps(refs or {}, default=str)),
            )
            row = cursor.fetchone()
            conn.commit()
        result = {"item_id": row["id"]}
        log_tool_call(cycle_id, "record_observation", params_log, result, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "record_observation", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return {"erro": str(e)}


def escalate_to_user(
    cycle_id: str,
    iteration: int,
    texto: str,
    motivo: str,
    refs: Optional[Dict] = None,
    prioridade: int = 3,
) -> Dict:
    """Insere item categoria='one_way' (🟡) em cos_briefing_items — decisão
    binária explícita pro Renato.

    Para item categoria='escalated' (crítico, sobe pra topo), use
    prioridade=1; o reporter classifica visualmente."""
    started = time.time()
    params_log = {"texto": _truncate(texto, 80), "motivo": _truncate(motivo, 80), "prioridade": prioridade, "refs": refs}
    try:
        # prioridade 1 vira escalated (critical), senao one_way
        categoria = "escalated" if prioridade <= 1 else "one_way"
        # Compõe texto com motivo
        full_texto = f"{texto}\n   Motivo: {motivo}" if motivo and motivo not in texto else texto
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cos_briefing_items
                    (cycle_id, categoria, frente, texto, refs, prioridade)
                VALUES (%s, %s, NULL, %s, %s, %s)
                RETURNING id
                """,
                (cycle_id, categoria, full_texto, json.dumps(refs or {}, default=str), prioridade),
            )
            row = cursor.fetchone()
            conn.commit()
        result = {"item_id": row["id"], "categoria": categoria}
        log_tool_call(cycle_id, "escalate_to_user", params_log, result, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "escalate_to_user", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return {"erro": str(e)}


# ============== TOOL DEFINITIONS (formato Anthropic) ==============

COS_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_messages",
        "description": (
            "Busca mensagens WhatsApp por trecho de texto (case-insensitive) nas últimas N horas. "
            "Use pra ENCONTRAR mensagens que mencionam um tema (ex: 'plano de acao', 'reuniao Vallen'). "
            "Se você sabe o contato exato, prefira get_messages_with. "
            "Retorna até 30 mensagens com id, contact_id, contact_name, direcao (incoming/outgoing), "
            "conteudo (truncado 200ch) e enviado_em."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Trecho de texto a buscar (ILIKE %query%)."},
                "hours": {"type": "integer", "description": "Janela em horas pra trás. Default 48.", "default": 48},
                "contact_id": {"type": "integer", "description": "Filtra por contato (opcional)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_messages_with",
        "description": (
            "Recupera últimas mensagens de UM contato específico nas últimas N horas. "
            "Aceita contact_id (int) ou nome (busca ILIKE). USE QUANDO uma pendência aponta pra um "
            "contato e você precisa ver o estado real da conversa antes de inferir. "
            "Retorna {contact_id, nome, mensagens: [...]} com até 25 mensagens. "
            "Se contato não encontrado: {erro: 'contato_nao_encontrado'}. "
            "Se contato sem mensagens recentes: mensagens=[] — DIGA 'sem trocas nas últimas Nh', NÃO infira sumido."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id_or_name": {"type": "string", "description": "ID numérico ou nome do contato."},
                "hours": {"type": "integer", "description": "Janela em horas. Default 48.", "default": 48},
            },
            "required": ["contact_id_or_name"],
        },
    },
    {
        "name": "get_overdue_tasks",
        "description": (
            "Lista tasks vencidas (data_vencimento < NOW, status != 'done'). Inclui projeto, "
            "contato, dias de atraso e peso_estimado (1=baixo, 2=médio, 3=alto — alto pra projetos "
            "tipo Vallen/imensIAH/Alba/Wadhwani). Use pra priorizar e identificar contatos relevantes "
            "do dia (cada task vencida sugere alguém a investigar)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max items. Default 20.", "default": 20},
                "project_filter": {"type": "string", "description": "Filtra por nome do projeto (ILIKE)."},
            },
        },
    },
    {
        "name": "get_calendar",
        "description": (
            "Eventos do calendar no range [date_start, date_end] (YYYY-MM-DD inclusive). "
            "Retorna até 30 eventos com titulo, inicio, fim, local, contato linkado e até 5 participantes. "
            "Use pra entender o que pesa no dia (reuniões viram pendência implícita)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_start": {"type": "string", "description": "YYYY-MM-DD (inicio inclusive)."},
                "date_end": {"type": "string", "description": "YYYY-MM-DD (fim inclusive)."},
            },
            "required": ["date_start", "date_end"],
        },
    },
    {
        "name": "get_proposals",
        "description": (
            "Propostas de ação pendentes (action_proposals) por status. Cada proposta foi gerada "
            "automaticamente quando o sistema detectou algo em mensagem/email. Use pra ver o que está "
            "na fila aguardando review do Renato. Default status='pending'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filtra por status. Default 'pending'.", "default": "pending"},
            },
        },
    },
    {
        "name": "get_pending_email_triage",
        "description": (
            "Lista emails pendentes na triagem CoS (email_triage). Sweep cron 30min "
            "classifica em must_read (priority 7-10, alta confianca), archive_proposed "
            "(shadow mode, NAO arquiva), ou silent. CHAME no inicio do ciclo pra ver "
            "o que chegou de email. Retorna ate 25 items com triage_id, account (pro/pess), "
            "sender, subject (truncado 80ch), classification, priority, ai_confidence, "
            "suggested_tags, contact_circulo. "
            "MAPEAMENTO PRA ACAO: "
            "- must_read + priority>=9 -> escalate_to_user prioridade 2 (one_way) com texto "
            "  '📧 [account] De [sender]: [subject]'. "
            "- must_read + priority<9 -> record_observation (monitor); se >3 must_read agrupe "
            "  em 1 linha '5 emails C2/frente pra ler'. "
            "- archive_proposed -> record_observation AGRUPADO em 1 linha so: "
            "  'X emails propostos pra arquivar (shadow mode 2sem ate auto-archive)'. "
            "  NUNCA escale archive_proposed individualmente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max items (1-25). Default 20.", "default": 20},
                "classification": {"type": "string", "description": "Filtra: must_read | archive_proposed | silent. Default todos."},
            },
        },
    },
    {
        "name": "create_draft_response",
        "description": (
            "Cria um RASCUNHO de resposta pra Renato aprovar/disparar manualmente. Não envia nada — "
            "só salva o texto em cos_draft_responses (pending, expira 48h). Channel ∈ "
            "{whatsapp, email, linkedin_dm}. USE só quando há histórico real (você leu via "
            "get_messages_with) e você sabe o que responder. SEMPRE inclua motivo (justificativa curta). "
            "Cria item ✅ FEITO automaticamente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer", "description": "ID do contato no banco INTEL."},
                "channel": {"type": "string", "enum": ["whatsapp", "email", "linkedin_dm"]},
                "text_draft": {"type": "string", "description": "Texto do rascunho."},
                "motivo": {"type": "string", "description": "Justificativa curta de por que esse draft (ex: 'Thalita pediu ETA na ultima mensagem')."},
            },
            "required": ["contact_id", "channel", "text_draft", "motivo"],
        },
    },
    {
        "name": "record_observation",
        "description": (
            "Registra uma OBSERVAÇÃO factual (👀 MONITORANDO) no briefing do ciclo. Use pra fatos "
            "derivados DIRETAMENTE de tools (não infira). frente=1..5 mapeia frente CoS. refs "
            "permite anexar IDs (task_id, contact_id, etc) pra rastreabilidade."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "Observação curta (1 linha)."},
                "frente": {"type": "integer", "description": "Frente CoS 1..5 (opcional).", "minimum": 1, "maximum": 5},
                "refs": {"type": "object", "description": "IDs relacionados (task_id, contact_id, etc)."},
            },
            "required": ["texto"],
        },
    },
    {
        "name": "escalate_to_user",
        "description": (
            "Sobe uma DECISÃO BINÁRIA (🟡 ONE-WAY) pro briefing do Renato. Use pra coisas que "
            "exigem ratificação manual: aprovar disparo de draft, bulk-defer tasks, aceitar/rejeitar "
            "proposta crítica. prioridade=1 vira 'escalated' (topo do briefing), >=2 vira 'one_way'. "
            "SEMPRE inclua motivo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "Pergunta/decisão clara (1-2 linhas)."},
                "motivo": {"type": "string", "description": "Justificativa factual."},
                "refs": {"type": "object", "description": "IDs relacionados (opcional)."},
                "prioridade": {"type": "integer", "description": "1=critico (escalated topo), 3=normal. Default 3.", "default": 3, "minimum": 1, "maximum": 9},
            },
            "required": ["texto", "motivo"],
        },
    },
]


# ============== DISPATCHER (usado pelo investigator agent loop) ==============

def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    cycle_id: str,
    iteration: int,
) -> Any:
    """Dispatcher central. O investigator chama isso pra cada tool_use block.
    Retorna o resultado JSON-serializável."""
    try:
        if tool_name == "search_messages":
            return search_messages(
                cycle_id, iteration,
                query=tool_input["query"],
                hours=tool_input.get("hours", 48),
                contact_id=tool_input.get("contact_id"),
            )
        if tool_name == "get_messages_with":
            return get_messages_with(
                cycle_id, iteration,
                contact_id_or_name=str(tool_input["contact_id_or_name"]),
                hours=tool_input.get("hours", 48),
            )
        if tool_name == "get_overdue_tasks":
            return get_overdue_tasks(
                cycle_id, iteration,
                limit=tool_input.get("limit", 20),
                project_filter=tool_input.get("project_filter"),
            )
        if tool_name == "get_calendar":
            return get_calendar(
                cycle_id, iteration,
                date_start=tool_input["date_start"],
                date_end=tool_input["date_end"],
            )
        if tool_name == "get_proposals":
            return get_proposals(
                cycle_id, iteration,
                status=tool_input.get("status", "pending"),
            )
        if tool_name == "get_pending_email_triage":
            return get_pending_email_triage(
                cycle_id, iteration,
                limit=tool_input.get("limit", 20),
                classification=tool_input.get("classification"),
            )
        if tool_name == "create_draft_response":
            return create_draft_response(
                cycle_id, iteration,
                contact_id=tool_input["contact_id"],
                channel=tool_input["channel"],
                text_draft=tool_input["text_draft"],
                motivo=tool_input["motivo"],
            )
        if tool_name == "record_observation":
            return record_observation(
                cycle_id, iteration,
                texto=tool_input["texto"],
                frente=tool_input.get("frente"),
                refs=tool_input.get("refs"),
            )
        if tool_name == "escalate_to_user":
            return escalate_to_user(
                cycle_id, iteration,
                texto=tool_input["texto"],
                motivo=tool_input["motivo"],
                refs=tool_input.get("refs"),
                prioridade=tool_input.get("prioridade", 3),
            )
        return {"erro": f"tool_desconhecida: {tool_name}"}
    except KeyError as e:
        return {"erro": f"param_faltando: {e}"}
    except Exception as e:
        logger.warning(f"execute_tool({tool_name}) falhou: {e}")
        return {"erro": str(e)}
