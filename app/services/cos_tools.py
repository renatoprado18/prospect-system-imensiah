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
import re
import time
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)


# ============== RACI parser utility ==============

# Captura o campo R: até virgula/quebra de linha/'A:'/'C:'/'I:'.
# Ex: "R: Amadeo, A: Renato, C: Dra. Thalita" -> "Amadeo"
#     "R: Renato\nA: Thalita"                  -> "Renato"
#     "R: Renata, Amadeo\nA: Thalita"          -> "Renata, Amadeo" (lista R)
_RACI_PATTERN = re.compile(
    r"R\s*:\s*([^\n]+?)(?=\s*[,;]\s*[ACI]\s*:|\s*[ACI]\s*:|\n|$)",
    re.IGNORECASE,
)


def parse_raci_responsible(descricao: Optional[str]) -> Optional[str]:
    """Retorna o R (responsavel) parseado do RACI no descricao da task.
    Returns None se sem RACI."""
    if not descricao or "RACI" not in descricao.upper():
        return None
    m = _RACI_PATTERN.search(descricao)
    if m:
        return m.group(1).strip().rstrip(",;").strip()
    return None


def is_renato_responsible(descricao: Optional[str]) -> Optional[bool]:
    """True se R inclui Renato. False se R explicitamente outro. None se sem RACI."""
    r = parse_raci_responsible(descricao)
    if r is None:
        return None
    return "renato" in r.lower()


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


def get_overdue_tasks_raci_aware(
    cycle_id: str,
    iteration: int,
    limit: int = 20,
    project_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """Igual get_overdue_tasks mas pula tasks onde R (Responsible) parseado
    do descricao NAO inclui Renato. Tasks sem RACI continuam no fluxo.

    Retorno:
        {tasks: [...], delegated_count: N, delegated_sample: [...]}
        - tasks: lista (mesmo schema de get_overdue_tasks) com tasks que sao
          R=Renato OU sem RACI.
        - delegated_count: quantas tasks foram filtradas (R != Renato).
        - delegated_sample: ate 5 das filtradas com {task_id, titulo, responsible}.
    """
    started = time.time()
    params_log = {"limit": limit, "project_filter": project_filter}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            sql = """
                SELECT t.id, t.titulo, t.descricao, t.data_vencimento,
                       t.contact_id, t.project_id,
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
            # Pega ate 2x limit pra compensar filtragem por RACI
            sql += " ORDER BY t.data_vencimento ASC LIMIT %s"
            fetch_limit = min(limit * 2, 40)
            args.append(fetch_limit)
            cursor.execute(sql, args)
            rows = cursor.fetchall()

        today = datetime.now().date()
        tasks: List[Dict[str, Any]] = []
        delegated_sample: List[Dict[str, Any]] = []
        delegated_count = 0

        for r in rows:
            is_renato = is_renato_responsible(r["descricao"])
            if is_renato is False:
                delegated_count += 1
                if len(delegated_sample) < 5:
                    delegated_sample.append({
                        "task_id": r["id"],
                        "titulo": _truncate(r["titulo"], 80),
                        "responsible": parse_raci_responsible(r["descricao"]),
                        "projeto": r["projeto"],
                    })
                continue
            if len(tasks) >= min(limit, 20):
                continue
            dv = r["data_vencimento"]
            dias_atraso = (today - dv.date()).days if dv else 0
            tasks.append({
                "id": r["id"],
                "titulo": _truncate(r["titulo"], 80),
                "projeto": r["projeto"],
                "contact_id": r["contact_id"],
                "contact_name": r["contact_name"],
                "data_vencimento": dv.isoformat() if dv else None,
                "dias_atraso": dias_atraso,
                "peso_estimado": _peso_estimado_task(r["contact_id"], r["projeto"]),
                "raci_responsible": parse_raci_responsible(r["descricao"]),
            })

        result = {
            "tasks": tasks,
            "delegated_count": delegated_count,
            "delegated_sample": delegated_sample,
        }
        log_tool_call(
            cycle_id, "get_overdue_tasks_raci_aware", params_log,
            {"n": len(tasks), "delegated_count": delegated_count},
            iteration, int((time.time() - started) * 1000),
        )
        return result
    except Exception as e:
        log_tool_call(
            cycle_id, "get_overdue_tasks_raci_aware", params_log, None,
            iteration, int((time.time() - started) * 1000), str(e),
        )
        return {"tasks": [], "delegated_count": 0, "delegated_sample": [], "erro": str(e)}


# ============== Blocking status detection (WA + email cross-check) ==============

# Keywords que indicam "Renato esta esperando" quando ele foi o ULTIMO a falar
_KW_RENATO_WAITING = (
    "aguardo", "aguardando", "aguarde", "espero", "esperando",
    "preciso de", "sem retorno", "vou esperar", "fico no aguardo",
    "qualquer retorno", "no aguardo", "aguardamos",
)

# Keywords que indicam "outro lado prometeu mandar/responder" quando ele foi o ULTIMO a falar
_KW_EXTERNAL_PROMISED = (
    "vou mandar", "te mando", "vou enviar", "envio", "te envio",
    "asap", "esta semana", "amanha", "amanhã", "ate amanha", "ate amanhã",
    "te aviso quando", "te aviso assim que", "aviso depois", "te retorno",
    "vou retornar", "retorno depois", "logo mais", "em breve", "vou marcar",
    "vou agendar", "marco com", "passo o", "deixa eu", "ja te falo",
    "te passo", "ja envio",
)


def _norm(s: str) -> str:
    """Lowercase + remove acentos pra match agnostico."""
    import unicodedata
    s = (s or "").lower()
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _extract_contact_names(task_text: str) -> List[str]:
    """Extrai possiveis nomes proprios (palavras Capitalizadas que nao sao
    inicio de frase). Heuristica simples — pega tokens com primeira maiuscula
    + tamanho >= 4 que nao sao stopwords PT-BR comuns."""
    if not task_text:
        return []
    stop = {
        "Confirmar", "Enviar", "Definir", "Implementar", "Coletar", "Wirear",
        "RACI", "Empresa", "Vallen", "Clinic", "Frente", "Conselho",
        "Reuniao", "Reunião", "Pendente", "Pendencia", "Acao", "Ação",
        "Projeto", "Cliente", "Status", "Daily", "Weekly", "Renato",
        "Cos", "CoS", "Intel", "INTEL", "Bot", "API", "DB",
        "Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
        "Brasil", "Brasileiro", "Sao", "São", "Paulo",
    }
    # Tokens com pelo menos 1 letra maiuscula e tamanho >= 4
    tokens = re.findall(r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]{3,}\b", task_text)
    seen = set()
    out = []
    for t in tokens:
        if t in stop:
            continue
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    return out[:6]  # cap 6 nomes


def get_task_blocking_status(
    cycle_id: str,
    iteration: int,
    task_id: int,
) -> Dict[str, Any]:
    """Cruzamento 1: detecta se task esta bloqueada por terceiro via
    WhatsApp/email recente.

    Estrategia:
    1. Le task (titulo + descricao + contact_id + projeto.nome).
    2. Identifica contatos relevantes: contact_id direto + nomes mencionados
       no titulo/descricao/projeto (busca por nome em contacts).
    3. Pra cada contato, pega ate 5 ultimas mensagens em 30d.
    4. Analisa a ULTIMA mensagem:
       - direcao='outgoing' + <7d + keyword aguardo/espero
         -> status='blocked_on_external' (Renato falou ultimo, esperando)
       - direcao='incoming' + <14d + keyword "vou mandar/asap/esta semana"
         -> status='waiting_external_followthrough' (outro lado prometeu)
       - caso contrario -> status='unblocked'

    Retorna:
        {
            status: 'blocked_on_external' | 'waiting_external_followthrough' | 'unblocked' | 'no_data',
            motivo: str explicativo,
            last_msg_excerpt: str (1 linha trunc 150ch),
            last_msg_at: ISO timestamp,
            contact_name: str,
            suggest_fup_in_days: int (quantos dias ate sugerir FUP soft),
        }
    """
    started = time.time()
    params_log = {"task_id": task_id}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # 1. Carrega task
            cursor.execute(
                """
                SELECT t.id, t.titulo, t.descricao, t.contact_id, t.project_id,
                       c.nome AS contact_name, p.nome AS projeto
                FROM tasks t
                LEFT JOIN contacts c ON c.id = t.contact_id
                LEFT JOIN projects p ON p.id = t.project_id
                WHERE t.id = %s
                """,
                (task_id,),
            )
            task = cursor.fetchone()
            if not task:
                result = {"status": "no_data", "motivo": "task_nao_encontrada"}
                log_tool_call(cycle_id, "get_task_blocking_status", params_log, result, iteration, int((time.time() - started) * 1000))
                return result

            # 2. Resolve contatos candidatos
            candidate_ids: List[int] = []
            candidate_names: Dict[int, str] = {}
            if task["contact_id"]:
                candidate_ids.append(task["contact_id"])
                candidate_names[task["contact_id"]] = task["contact_name"] or f"#{task['contact_id']}"

            # Extrai nomes do titulo/descricao/projeto
            search_text = " ".join(filter(None, [task["titulo"], task["descricao"], task["projeto"]]))
            names = _extract_contact_names(search_text)
            if names:
                # Busca contatos por nome (top match por interacao)
                for n in names:
                    cursor.execute(
                        """
                        SELECT id, nome FROM contacts
                        WHERE nome ILIKE %s
                        ORDER BY total_interacoes DESC NULLS LAST
                        LIMIT 1
                        """,
                        (f"%{n}%",),
                    )
                    r = cursor.fetchone()
                    if r and r["id"] not in candidate_ids:
                        candidate_ids.append(r["id"])
                        candidate_names[r["id"]] = r["nome"]

            if not candidate_ids:
                result = {
                    "status": "no_data",
                    "motivo": "task_sem_contato_identificavel",
                    "candidates_searched": names,
                }
                log_tool_call(cycle_id, "get_task_blocking_status", params_log, result, iteration, int((time.time() - started) * 1000))
                return result

            # 3. Pega ultimas mensagens em 30d pra cada contato; escolhe contato com msg mais recente
            since = datetime.now() - timedelta(days=30)
            best_cid = None
            best_msgs: List[Any] = []
            for cid in candidate_ids:
                cursor.execute(
                    """
                    SELECT direcao, conteudo, COALESCE(enviado_em, criado_em) AS ts
                    FROM messages
                    WHERE contact_id = %s
                      AND COALESCE(enviado_em, criado_em) >= %s
                    ORDER BY COALESCE(enviado_em, criado_em) DESC
                    LIMIT 5
                    """,
                    (cid, since),
                )
                rows = cursor.fetchall()
                if rows:
                    candidate_ts = rows[0]["ts"]
                    current_best_ts = best_msgs[0]["ts"] if best_msgs else None
                    if current_best_ts is None or (candidate_ts and candidate_ts > current_best_ts):
                        best_cid = cid
                        best_msgs = rows

        if not best_msgs:
            result = {
                "status": "no_data",
                "motivo": "sem_mensagens_30d",
                "candidates_searched": [candidate_names[c] for c in candidate_ids[:3]],
            }
            log_tool_call(cycle_id, "get_task_blocking_status", params_log, result, iteration, int((time.time() - started) * 1000))
            return result

        last = best_msgs[0]
        last_ts = last["ts"]
        last_dir = last["direcao"]
        last_content = last["conteudo"] or ""
        last_cid = best_cid
        excerpt = _truncate(last_content.replace("\n", " "), 150)
        norm_last = _norm(last_content)
        now = datetime.now()
        try:
            if last_ts.tzinfo is not None:
                last_ts_naive = last_ts.replace(tzinfo=None)
            else:
                last_ts_naive = last_ts
        except Exception:
            last_ts_naive = last_ts
        days_ago = (now - last_ts_naive).days if last_ts_naive else 999

        contact_label = candidate_names.get(last_cid, f"#{last_cid}")

        # Decide status — combina ultima msg + 2-3 anteriores pra detectar
        # caso "outro prometeu, Renato confirmou ok aguardo" (followthrough)
        if last_dir == "outgoing" and days_ago <= 7:
            kw_renato = next((k for k in _KW_RENATO_WAITING if k in norm_last), None)
            # Procura promessa externa nas 2-3 ultimas incoming antes desta
            kw_external = None
            external_excerpt = None
            for prev in best_msgs[1:4]:
                if prev["direcao"] == "incoming":
                    prev_norm = _norm(prev["conteudo"] or "")
                    hit = next((k for k in _KW_EXTERNAL_PROMISED if k in prev_norm), None)
                    if hit:
                        kw_external = hit
                        external_excerpt = _truncate((prev["conteudo"] or "").replace("\n", " "), 150)
                        break

            if kw_external:
                # Combo: outro lado prometeu + Renato ackmou "ok aguardo"
                # Esse e o caso Wadhwani/Tanaka — status mais preciso e followthrough
                result = {
                    "status": "waiting_external_followthrough",
                    "motivo": f"{contact_label} prometeu '{kw_external}' e Renato ackmou; aguardando material",
                    "last_msg_excerpt": excerpt,
                    "external_promise_excerpt": external_excerpt,
                    "last_msg_at": last_ts.isoformat() if last_ts else None,
                    "last_msg_direcao": last_dir,
                    "contact_name": contact_label,
                    "contact_id": last_cid,
                    "suggest_fup_in_days": 5,
                }
                log_tool_call(cycle_id, "get_task_blocking_status", params_log, {"status": result["status"], "contact": contact_label}, iteration, int((time.time() - started) * 1000))
                return result

            if kw_renato:
                result = {
                    "status": "blocked_on_external",
                    "motivo": f"Renato escreveu pra {contact_label} ha {days_ago}d com '{kw_renato}', sem resposta",
                    "last_msg_excerpt": excerpt,
                    "last_msg_at": last_ts.isoformat() if last_ts else None,
                    "last_msg_direcao": last_dir,
                    "contact_name": contact_label,
                    "contact_id": last_cid,
                    "suggest_fup_in_days": max(3, 7 - days_ago),
                }
                log_tool_call(cycle_id, "get_task_blocking_status", params_log, {"status": result["status"], "contact": contact_label}, iteration, int((time.time() - started) * 1000))
                return result

        if last_dir == "incoming" and days_ago <= 14:
            kw_hit = next((k for k in _KW_EXTERNAL_PROMISED if k in norm_last), None)
            if kw_hit:
                result = {
                    "status": "waiting_external_followthrough",
                    "motivo": f"{contact_label} prometeu '{kw_hit}' ha {days_ago}d; aguardando material/retorno",
                    "last_msg_excerpt": excerpt,
                    "last_msg_at": last_ts.isoformat() if last_ts else None,
                    "last_msg_direcao": last_dir,
                    "contact_name": contact_label,
                    "contact_id": last_cid,
                    "suggest_fup_in_days": max(5, 10 - days_ago),
                }
                log_tool_call(cycle_id, "get_task_blocking_status", params_log, {"status": result["status"], "contact": contact_label}, iteration, int((time.time() - started) * 1000))
                return result

        result = {
            "status": "unblocked",
            "motivo": f"Sem sinal de bloqueio externo (ultima msg c/ {contact_label} ha {days_ago}d, dir={last_dir})",
            "last_msg_excerpt": excerpt,
            "last_msg_at": last_ts.isoformat() if last_ts else None,
            "last_msg_direcao": last_dir,
            "contact_name": contact_label,
            "contact_id": last_cid,
            "suggest_fup_in_days": 0,
        }
        log_tool_call(cycle_id, "get_task_blocking_status", params_log, {"status": "unblocked", "contact": contact_label}, iteration, int((time.time() - started) * 1000))
        return result
    except Exception as e:
        log_tool_call(cycle_id, "get_task_blocking_status", params_log, None, iteration, int((time.time() - started) * 1000), str(e))
        return {"status": "no_data", "motivo": f"erro: {e}"}


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
            "do dia (cada task vencida sugere alguém a investigar). "
            "PREFIRA get_overdue_tasks_raci_aware (filtra tasks delegadas onde R != Renato)."
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
        "name": "get_overdue_tasks_raci_aware",
        "description": (
            "Versao Renato-aware de get_overdue_tasks. Filtra tasks onde o R (Responsible) "
            "parseado do descricao (campo 'RACI ... R: Nome') NAO inclui Renato — essas viram "
            "delegated_count, NAO entram em 'tasks'. Tasks sem RACI passam normal. "
            "USE essa pra identificar drift real do Renato (vs tasks que pertencem a outros). "
            "Retorno: {tasks: [...], delegated_count: N, delegated_sample: [{task_id,titulo,responsible}]}. "
            "Cada task em tasks inclui raci_responsible (string ou null)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max items em tasks. Default 20.", "default": 20},
                "project_filter": {"type": "string", "description": "Filtra por nome do projeto (ILIKE)."},
            },
        },
    },
    {
        "name": "get_task_blocking_status",
        "description": (
            "Cruza UMA task vencida com WhatsApp/email recente pra detectar se ela esta BLOQUEADA "
            "por terceiro (Renato esperando), em FOLLOWTHROUGH (outro lado prometeu mandar) ou "
            "UNBLOCKED (sem sinal de bloqueio externo). "
            "USE antes de tratar uma task vencida como drift do Renato. "
            "Lê task -> identifica contato (FK + nomes mencionados) -> analisa ultima mensagem em 30d. "
            "Retorno: {status, motivo, last_msg_excerpt, last_msg_at, contact_name, suggest_fup_in_days}. "
            "Status: 'blocked_on_external' (Renato falou ultimo + keyword aguardo), "
            "'waiting_external_followthrough' (outro lado prometeu material/retorno), "
            "'unblocked' (sem sinal), 'no_data' (sem contato OU sem msgs 30d)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "ID da task pra analisar."},
            },
            "required": ["task_id"],
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
        if tool_name == "get_overdue_tasks_raci_aware":
            return get_overdue_tasks_raci_aware(
                cycle_id, iteration,
                limit=tool_input.get("limit", 20),
                project_filter=tool_input.get("project_filter"),
            )
        if tool_name == "get_task_blocking_status":
            return get_task_blocking_status(
                cycle_id, iteration,
                task_id=int(tool_input["task_id"]),
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
