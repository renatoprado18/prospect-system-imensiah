"""
(B) Roteador de sinal — cruza inbound (email + WA DM) → frente por CONTEÚDO.

Origem: o gather da camada de inteligência cruzava por MEMBERSHIP (quem está
ligado ao projeto) + grupos. Furo: mensagem de NÃO-membro nunca chegava à frente,
por mais relevante que fosse (o Renato: "se um comprador novo me manda email
sobre o Jabô mas não é membro, o agente não vê"). Este roteador fecha o furo:
cada inbound é etiquetado com o(s) projeto(s) que toca, e a frente_review passa a
ler por etiqueta — de qualquer um.

Pipeline (barato → preciso):
1. **Prefiltro por entidade** — cada projeto ativo tem tokens DISTINTIVOS (nome/
   empresa/tags/nomes de membros, filtrados por document-frequency: token que
   aparece em >2 projetos é comum demais e sai). Uma mensagem vira candidata a um
   projeto se cita um token distintivo dele. **Isso também filtra o lixo de graça**:
   spam/newsletter não cita "Jabô/Piccino/Wanelise" → zero candidato → descartado
   sem gastar LLM.
2. **Filtro de lixo do email** — reusa o `email_triage`: email classificado como
   `archive_proposed`/`silent` não entra (o Renato pediu "depois de filtrar o lixo").
3. **Confirmação LLM (FAST/Haiku)** — pra mensagem com candidatos, o LLM confirma
   qual(is) projeto(s) ela realmente toca (ou nenhum). Barato: só roda no que
   passou o prefiltro.
Resultado gravado em `message_project_links` (message_id, project_id). Read-only
sobre o conteúdo; só escreve o vínculo. NÃO age.
"""
import json
import logging
import re
import unicodedata
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

import httpx

from database import get_db
from services import llm, llm_usage

logger = logging.getLogger(__name__)

# tokens genéricos que nunca são distintivos (mesmo com DF baixo)
_STOP = {
    "prado", "renato", "almeida", "conselho", "projeto", "processos", "empresa",
    "empresas", "direta", "gestao", "para", "com", "das", "dos", "sobre",
    "novos", "novo", "grupo", "reuniao", "cafe",  # café é ambíguo (Jabô + genérico) — DF decide melhor
}
_EMAIL_TRASH = ("archive_proposed", "silent")
_MAX_CANDIDATES_PER_MSG = 4


def _norm(s: Optional[str]) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _tokens(text: str) -> Set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", _norm(text)) if w not in _STOP}


def build_project_index(cursor) -> List[Dict[str, Any]]:
    """Projetos ativos com seus tokens DISTINTIVOS (DF<=2)."""
    cursor.execute("""
        SELECT id, nome, COALESCE(empresa_relacionada, '') AS empresa,
               COALESCE(tags, '[]'::jsonb) AS tags, COALESCE(descricao, '') AS descricao
        FROM projects WHERE status = 'ativo'
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    pids = [r["id"] for r in rows]

    members: Dict[int, List[str]] = defaultdict(list)
    if pids:
        cursor.execute("""
            SELECT pm.project_id, c.nome FROM project_members pm
            JOIN contacts c ON c.id = pm.contact_id WHERE pm.project_id = ANY(%s)
        """, (pids,))
        for r in cursor.fetchall():
            members[r["project_id"]].append(r["nome"])

    raw: List[Dict[str, Any]] = []
    for r in rows:
        toks: Set[str] = set()
        toks |= _tokens(r["nome"])
        toks |= _tokens(r["empresa"])
        tags = r["tags"]
        if isinstance(tags, list):
            for t in tags:
                toks |= _tokens(str(t))
        for mn in members.get(r["id"], []):
            toks |= _tokens(mn)
        raw.append({"id": r["id"], "nome": r["nome"], "empresa": r["empresa"],
                    "descricao": (r["descricao"] or "")[:160], "tokens": toks})

    # document-frequency: token que aparece em >2 projetos = comum demais, sai
    df: Dict[str, int] = defaultdict(int)
    for p in raw:
        for t in p["tokens"]:
            df[t] += 1
    for p in raw:
        p["distinct"] = {t for t in p["tokens"] if df[t] <= 2}
    return raw


def _prefilter(text_norm: str, projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Projetos cujo token distintivo aparece como palavra inteira no texto."""
    hits = []
    for p in projects:
        if not p["distinct"]:
            continue
        for t in p["distinct"]:
            if re.search(r"\b" + re.escape(t) + r"\b", text_norm):
                hits.append(p)
                break
    return hits[:_MAX_CANDIDATES_PER_MSG]


def _candidate_inbound(cursor, since_hours: int, limit: int = 200) -> List[Dict[str, Any]]:
    """Inbound recente (email+WA DM) ainda não etiquetado, fora do lixo do email."""
    cursor.execute("""
        SELECT m.id, cv.canal, c.nome AS sender, m.conteudo,
               COALESCE(m.enviado_em, m.recebido_em) AS ts
        FROM messages m
        JOIN conversations cv ON cv.id = m.conversation_id
        JOIN contacts c ON c.id = cv.contact_id
        LEFT JOIN email_triage et ON et.message_id = m.id
        WHERE m.direcao = 'incoming'
          AND cv.canal IN ('email', 'whatsapp')
          AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - (%s || ' hours')::interval
          AND m.conteudo IS NOT NULL AND LENGTH(m.conteudo) > 20
          AND NOT (cv.canal = 'email' AND et.classification = ANY(%s))
          AND NOT EXISTS (SELECT 1 FROM message_project_links l WHERE l.message_id = m.id)
        ORDER BY ts DESC
        LIMIT %s
    """, (since_hours, list(_EMAIL_TRASH), limit))
    return [dict(r) for r in cursor.fetchall()]


async def _confirm_links(msg: Dict[str, Any], candidates: List[Dict[str, Any]], api_key: str) -> List[int]:
    """LLM (FAST) confirma qual(is) candidato(s) a mensagem realmente toca."""
    lst = "\n".join(f"[{p['id']}] {p['nome']} — {p['empresa'] or p['descricao']}" for p in candidates)
    prompt = (
        f"Mensagem recebida ({msg['canal']}, de {msg['sender']}):\n\"\"\"\n{(msg['conteudo'] or '')[:1200]}\n\"\"\"\n\n"
        f"Projetos candidatos (o texto citou um termo de cada):\n{lst}\n\n"
        "Quais desses projetos a mensagem REALMENTE trata (assunto, não menção casual)? "
        "Pode ser nenhum (citação coincidente). Retorne SÓ JSON: {\"project_ids\": [ids]}."
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": llm.FAST, "max_tokens": 120,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        if resp.status_code != 200:
            logger.warning("signal_router confirm %s: %s", resp.status_code, resp.text[:150])
            return []
        result = resp.json()
        llm_usage.record_response("cos.signal_router", llm.FAST, result)
        text = result.get("content", [{}])[0].get("text", "")
        s, e = text.find("{"), text.rfind("}") + 1
        if s < 0 or e <= s:
            return []
        ids = json.loads(text[s:e]).get("project_ids", [])
        valid = {p["id"] for p in candidates}
        return [int(i) for i in ids if int(i) in valid]
    except Exception as ex:
        logger.warning("signal_router confirm erro msg %s: %s", msg.get("id"), ex)
        return []


def _persist_links(cursor, message_id: int, project_ids: List[int]) -> None:
    """Grava os vínculos. Lista vazia → sentinela (project_id=0, source='none')
    pra marcar 'processado, sem vínculo' e não re-rodar o LLM na próxima varredura
    (o candidate query exclui msg que já tem QUALQUER linha em links)."""
    rows = [(pid, "router", 0.85) for pid in project_ids] or [(0, "none", 0.0)]
    for pid, source, conf in rows:
        cursor.execute("""
            INSERT INTO message_project_links (message_id, project_id, source, confidence)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (message_id, project_id) DO NOTHING
        """, (message_id, pid, source, conf))


async def run_router_sweep(since_hours: int = 48) -> Dict[str, Any]:
    """
    Varre o inbound recente, etiqueta cada mensagem com o(s) projeto(s) que toca.
    Read-only sobre conteúdo; escreve só os vínculos. Retorna telemetria.
    """
    import os
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY ausente"}

    with get_db() as conn:
        cur = conn.cursor()
        projects = build_project_index(cur)
        candidates_msgs = _candidate_inbound(cur, since_hours)

    scanned = len(candidates_msgs)
    prefiltered = 0
    linked = 0
    links_total = 0

    with get_db() as conn:
        cur = conn.cursor()
        for msg in candidates_msgs:
            cand = _prefilter(_norm(msg["conteudo"]), projects)
            if not cand:
                # Marca como "visto, sem vínculo" — evita reprocessar toda varredura.
                _persist_links(cur, msg["id"], [])
                continue
            prefiltered += 1
            confirmed = await _confirm_links(msg, cand, api_key)
            if confirmed:
                _persist_links(cur, msg["id"], confirmed)
                linked += 1
                links_total += len(confirmed)
            else:
                _persist_links(cur, msg["id"], [])
        conn.commit()

    return {"scanned": scanned, "prefiltered": prefiltered, "linked_msgs": linked,
            "links_total": links_total, "since_hours": since_hours}
