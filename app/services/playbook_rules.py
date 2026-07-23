"""
Playbook Andressa — extração automática de regras pós-reunião Fathom.

Fluxo (decisão 23/07, "propõe → aprova → merge"):
  1. Reunião Fathom ligada ao projeto Jabô (#28) é processada (process_fathom_meeting)
  2. `run_for_meeting` extrai REGRAS OPERACIONAIS DURÁVEIS ensinadas pelo Renato
     (export/precificação/amostra/processo/logística), deduplicadas contra o
     Playbook atual → guarda como proposta PENDENTE (project_note tipo
     'playbook_proposal' no #28) + propõe pro Renato no WhatsApp.
  3. Renato aprova → `apply_proposal` lê o Doc, mescla as regras no tema certo
     (numeração + dedup, via LLM) e reescreve o corpo do Google Doc.

Gate humano preservado: NADA escreve no Doc sem aprovação. Kill-switch
`PLAYBOOK_AUTO_EXTRACT` (default off). O write usa Drive API media (google_docs),
scope `auth/drive` completo — sem precisar de Docs API.

Fonte de verdade das regras = o próprio Google Doc (não duplicamos no DB; o DB
só guarda a proposta pendente até a aprovação).
"""

import os
import re
import json
import logging
import asyncio
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Playbook Andressa — Regras Operacionais (Drive, pasta Jabô)
PLAYBOOK_DOC_ID = "1vu_JEmDJzoupdhpKkr0R5BAPNqLuqglJ0RliXGOmLpo"
JABO_PROJECT_ID = 28

# Palavras que sinalizam reunião de operação Jabô (usadas junto do match de
# membros do #28). Início-de-palavra, unaccent no caller.
_JABO_KEYWORDS = [
    "jab", "jaboticabeira", "cafe", "café", "torref", "torra", "saca",
    "peneira", "exporta", "importad", "amostra", "guaxupe", "guaxupé",
    "orioli", "microlote", "arabica", "arábica", "specialty", "verde",
]


def is_enabled() -> bool:
    """Kill-switch. Default OFF (feature nova; liga só no Railway quando provada)."""
    return (os.getenv("PLAYBOOK_AUTO_EXTRACT", "off").strip().lower()
            in ("1", "on", "true", "yes"))


def _unaccent_lower(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def is_jabo_meeting(title: str, summary: str, matched_contact_ids: List[int],
                    jabo_member_ids: Optional[set] = None) -> bool:
    """Heurística: a reunião é de operação Jabô?

    True se (a) algum contato casado é membro do projeto #28, OU
            (b) título/resumo batem palavra-chave de café/export.
    """
    if jabo_member_ids and any(cid in jabo_member_ids for cid in (matched_contact_ids or [])):
        return True
    blob = _unaccent_lower(f"{title} {summary}")
    return any(re.search(rf"\b{re.escape(kw)}", blob) for kw in
               (_unaccent_lower(k) for k in _JABO_KEYWORDS))


# ───────────────────────────── LLM: extração ─────────────────────────────

_EXTRACT_PROMPT = """Você é o CoPiloto do Renato. Ele mantém um PLAYBOOK de regras \
operacionais duráveis para a assistente Andressa (operação do Café Jabô: exportação, \
precificação, amostras, processo, logística).

A partir do RESUMO de uma reunião do Renato, extraia SOMENTE **regras operacionais \
duráveis** que ele ensinou/decidiu — instruções de "como fazer / o que sempre valer", \
não tarefas pontuais (essas já viram tasks). Uma regra é durável, reutilizável e vale \
pra próximas vezes.

NÃO extraia: tarefas de uma vez ("ligar pro Marcos"), fofoca, status, opinião solta, \
nada que já esteja no PLAYBOOK ATUAL abaixo (dedup semântico — se a ideia já existe, \
pule).

PLAYBOOK ATUAL (não repita o que já está aqui):
---
{current_doc}
---

RESUMO DA REUNIÃO:
---
{summary}
---

Responda APENAS com um JSON array (sem texto fora dele). Cada regra:
{{"tema": "<EXPORTAÇÃO|PRECIFICAÇÃO|AMOSTRAS|PROCESSO|LOGÍSTICA|OUTRO>", \
"titulo": "<título curto, imperativo>", "texto": "<explicação de 1-3 frases, tom de \
regra pra Andressa>"}}
Se não houver nenhuma regra nova durável, responda: []"""


def _parse_json_array(raw: Optional[str]) -> List[Dict]:
    if not raw:
        return []
    # extrai o primeiro array JSON do texto (tolera cercas ```)
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for r in data if isinstance(data, list) else []:
        if isinstance(r, dict) and r.get("titulo") and r.get("texto"):
            out.append({
                "tema": (r.get("tema") or "OUTRO").strip().upper(),
                "titulo": str(r["titulo"]).strip(),
                "texto": str(r["texto"]).strip(),
            })
    return out


def extract_rules(summary_md: str, current_doc_text: str) -> List[Dict]:
    """Extrai regras novas duráveis do resumo (LLM BALANCED). Retorna [] em falha."""
    from services import llm
    prompt = _EXTRACT_PROMPT.format(
        current_doc=(current_doc_text or "(vazio)")[:6000],
        summary=(summary_md or "")[:8000],
    )
    # nota: llm._call_model ja registra custo (rotulado 'triage.advisor'); um
    # rotulo proprio 'playbook.*' exigiria refactor do wrapper — fica pra depois.
    raw = llm._call_model(llm.BALANCED, prompt, max_tokens=1500)
    return _parse_json_array(raw)


# ───────────────────────────── LLM: merge ─────────────────────────────

_MERGE_PROMPT = """Você é o CoPiloto do Renato, editando o PLAYBOOK ANDRESSA (Google Doc \
vivo, estruturado por TEMA com dividers `====` e regras numeradas 1) 2) 3)...).

Tarefa: incorporar as NOVAS REGRAS ao documento, preservando tudo que já existe.
Regras de edição:
- Insira cada regra nova na SEÇÃO DE TEMA correta (crie a seção se não existir, no \
mesmo estilo dos dividers `====`).
- Continue a NUMERAÇÃO global existente (não renumere as antigas; as novas seguem o \
maior número atual).
- DEDUP: se uma regra nova já está coberta por uma existente, NÃO duplique (funde ou \
ignora).
- Preserve o cabeçalho, o rodapé e o estilo. Mantenha os acentos.
- Saída = o TEXTO COMPLETO E FINAL do documento, nada além dele.

DOCUMENTO ATUAL:
---
{current_doc}
---

NOVAS REGRAS (JSON):
{rules_json}

Responda com o documento final completo (texto plano, mesmo estilo)."""


def merge_doc(current_doc_text: str, rules: List[Dict]) -> Optional[str]:
    """Mescla as regras no corpo do Doc (LLM BALANCED). Retorna o texto final ou None."""
    from services import llm
    prompt = _MERGE_PROMPT.format(
        current_doc=(current_doc_text or "")[:12000],
        rules_json=json.dumps(rules, ensure_ascii=False, indent=2),
    )
    raw = llm._call_model(llm.BALANCED, prompt, max_tokens=4000)
    if not raw or not raw.strip():
        return None
    # tira cercas de código se o modelo enrolar
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt)
    return txt.strip() + "\n"


# ───────────────────── proposta pendente (project_note) ─────────────────────

def store_proposal(recording_id: Optional[str], meeting_title: str,
                   rules: List[Dict]) -> Optional[int]:
    """Guarda a proposta pendente como project_note tipo 'playbook_proposal' no #28.
    Retorna o note_id."""
    from database import get_db
    meta = {
        "recording_id": recording_id,
        "rules": rules,
        "status": "pending",
        "doc_id": PLAYBOOK_DOC_ID,
    }
    titulo = f"[Playbook] {len(rules)} regra(s) de: {meeting_title[:60]}"
    corpo = "\n".join(f"[{r['tema']}] {r['titulo']}: {r['texto']}" for r in rules)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor, metadata, criado_em)
               VALUES (%s, 'playbook_proposal', %s, %s, 'CoPiloto', %s::jsonb, NOW())
               RETURNING id""",
            (JABO_PROJECT_ID, titulo, corpo, json.dumps(meta, ensure_ascii=False)),
        )
        note_id = cur.fetchone()["id"]
        conn.commit()
    return note_id


def get_pending_proposals() -> List[Dict]:
    """Lista propostas de playbook pendentes (pro /dev + apply)."""
    from database import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, titulo, metadata, criado_em FROM project_notes
               WHERE project_id=%s AND tipo='playbook_proposal'
                 AND COALESCE(metadata->>'status','pending')='pending'
               ORDER BY criado_em DESC""",
            (JABO_PROJECT_ID,),
        )
        return [dict(r) for r in cur.fetchall()]


def _mark_proposal(note_id: int, status: str) -> None:
    from database import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE project_notes
               SET metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), '{status}', to_jsonb(%s::text)),
                   atualizado_em = NOW()
               WHERE id=%s AND tipo='playbook_proposal'""",
            (status, note_id),
        )
        conn.commit()


# ───────────────────────────── aplicar / propor ─────────────────────────────

async def _get_token():
    from database import get_db
    from integrations.google_drive import get_valid_token
    with get_db() as conn:
        return await get_valid_token(conn, "professional")


async def apply_proposal(note_id: int) -> Dict:
    """Aprova uma proposta: lê o Doc, mescla as regras, reescreve o corpo, marca applied.

    Idempotente-ish: se já applied, retorna sem reescrever.
    """
    from database import get_db
    from integrations import google_docs
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT metadata FROM project_notes WHERE id=%s AND tipo='playbook_proposal'", (note_id,))
        row = cur.fetchone()
    if not row:
        return {"status": "not_found", "note_id": note_id}
    meta = row["metadata"] or {}
    if meta.get("status") == "applied":
        return {"status": "already_applied", "note_id": note_id}
    rules = meta.get("rules") or []
    if not rules:
        return {"status": "no_rules", "note_id": note_id}
    doc_id = meta.get("doc_id") or PLAYBOOK_DOC_ID

    token = await _get_token()
    if not token:
        return {"status": "no_token", "note_id": note_id}

    current = await google_docs.read_doc_text(token, doc_id)
    merged = await asyncio.to_thread(merge_doc, current, rules)
    if not merged:
        return {"status": "merge_failed", "note_id": note_id}
    await google_docs.overwrite_doc_text(token, doc_id, merged)
    _mark_proposal(note_id, "applied")
    return {"status": "applied", "note_id": note_id, "rules_count": len(rules), "doc_id": doc_id}


def dismiss_proposal(note_id: int) -> Dict:
    _mark_proposal(note_id, "dismissed")
    return {"status": "dismissed", "note_id": note_id}


def format_proposal_wa(rules: List[Dict], meeting_title: str, note_id: int) -> str:
    """Mensagem de proposta pro Renato revisar e aprovar no WhatsApp."""
    lines = [
        f"📘 *Playbook Andressa — {len(rules)} regra(s) nova(s)*",
        f"_Da reunião: {meeting_title[:80]}_",
        "",
    ]
    for i, r in enumerate(rules, 1):
        lines.append(f"*{i}. [{r['tema']}] {r['titulo']}*")
        lines.append(f"   {r['texto']}")
    lines += [
        "",
        f"_Aprovar → grava no Playbook (tema + numeração). Proposta #{note_id}._",
        "_Responda \"aprovo playbook\" ou \"ignora playbook\"._",
    ]
    return "\n".join(lines)


async def run_for_meeting(summary_md: str, meeting_title: str,
                          recording_id: Optional[str]) -> Dict:
    """Entry do gatilho: extrai regras da reunião Jabô, guarda proposta e propõe no WA.

    Só roda se is_enabled(). Retorna stats. NÃO escreve no Doc (só na aprovação).
    """
    if not is_enabled():
        return {"status": "disabled"}
    from integrations import google_docs
    from services.intel_bot import send_intel_notification

    token = await _get_token()
    current_doc = ""
    if token:
        try:
            current_doc = await google_docs.read_doc_text(token, PLAYBOOK_DOC_ID)
        except Exception as e:
            logger.warning(f"playbook: falha lendo Doc atual: {e}")

    rules = await asyncio.to_thread(extract_rules, summary_md, current_doc)
    if not rules:
        return {"status": "no_rules"}

    note_id = store_proposal(recording_id, meeting_title, rules)
    try:
        await send_intel_notification(format_proposal_wa(rules, meeting_title, note_id))
    except Exception as e:
        logger.warning(f"playbook: falha propondo no WA: {e}")
    return {"status": "proposed", "note_id": note_id, "rules_count": len(rules)}
