#!/usr/bin/env python3
"""
Import Fathom meeting → projects, project_notes, tasks, project_members.

Workflow:
  1. Generate proposal:
       python scripts/import_fathom_meeting.py <share_url>
     -> Fetches meeting from Fathom (tries pessoal + profissional accounts)
     -> Calls Claude to map summary/action_items to projects/tasks
     -> Saves a proposal JSON to /tmp/fathom_proposal_<recording_id>.json
     -> Prints a readable summary

  2. Review the JSON. Edit if needed (delete items, change owner, etc).

  3. Apply:
       python scripts/import_fathom_meeting.py --apply /tmp/fathom_proposal_<id>.json

Idempotent: if a project_note with the same fathom_recording_id already
exists, the apply step skips creating duplicates.
"""
import os
import re
import sys
import json
import argparse
import asyncio
from datetime import datetime
from pathlib import Path

# --- env loading (must be before importing app modules) ---
PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_DIR / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'").rstrip("\\n"))

# Default to local DB unless --remote is passed (decided in main()).
# We pre-scan argv here because get_db() reads the env var at import time.
if "--remote" not in sys.argv:
    os.environ["USE_LOCAL_DB"] = "1"
sys.path.insert(0, str(PROJECT_DIR / "app"))

import httpx
from database import get_db


FATHOM_BASE_URL = "https://api.fathom.ai/external/v1"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"

ACCOUNTS = {
    "pessoal": os.environ.get("FATHOM_API_KEY_PESSOAL", ""),
    "profissional": os.environ.get("FATHOM_API_KEY_PROFISSIONAL", ""),
}

# System user (the human operating the CRM). Used as default owner for tasks
# attributed to "Renato" in meeting transcripts.
SYSTEM_USER_NAMES = ("Renato de Faria e Almeida Prado", "Renato")


# ============================================================================
# 1. Fetch meeting from Fathom
# ============================================================================

async def fetch_meeting_from_fathom(share_url: str) -> dict:
    """
    Find a meeting by share_url in either Fathom account.

    Iterates through paginated meetings until a match is found.
    Returns the full meeting dict (with transcript, summary, action_items)
    plus a `_account` key indicating which API key it was fetched from.
    """
    target_share_id_match = re.search(r"fathom\.video/share/([A-Za-z0-9_-]+)", share_url)
    if not target_share_id_match:
        raise ValueError(f"Invalid Fathom share URL: {share_url}")

    for account_name, api_key in ACCOUNTS.items():
        if not api_key:
            print(f"  ⚠️  Sem API key para conta '{account_name}', pulando")
            continue

        print(f"🔍 Buscando em Fathom conta '{account_name}'...")
        cursor = None
        page = 0
        max_pages = 10  # safety: ~200 most recent meetings

        async with httpx.AsyncClient(timeout=60.0) as client:
            while page < max_pages:
                params = {
                    "limit": 20,
                    "include_transcript": "true",
                    "include_summary": "true",
                    "include_action_items": "true",
                }
                if cursor:
                    params["cursor"] = cursor

                resp = await client.get(
                    f"{FATHOM_BASE_URL}/meetings",
                    headers={"X-Api-Key": api_key},
                    params=params,
                )
                if resp.status_code != 200:
                    print(f"  ❌ HTTP {resp.status_code}: {resp.text[:200]}")
                    break

                data = resp.json()
                items = data.get("items") or []

                for m in items:
                    if (m.get("share_url") or "") == share_url:
                        m["_account"] = account_name
                        print(f"  ✅ Encontrada! recording_id={m.get('recording_id')}")
                        return m

                cursor = data.get("next_cursor")
                if not cursor:
                    break
                page += 1

    raise RuntimeError(
        f"Reunião com share_url={share_url} não encontrada em nenhuma conta Fathom. "
        f"Verifique se a URL está correta e se a meeting está dentro das últimas ~200 reuniões."
    )


# ============================================================================
# 2. Gather DB context (existing contacts, projects, members)
# ============================================================================

def _resolve_contact_by_id(cur, contact_id: int) -> dict | None:
    cur.execute(
        "SELECT id, nome, empresa, cargo, circulo FROM contacts WHERE id = %s",
        (contact_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _search_contacts(cur, name: str, limit: int = 5) -> list[dict]:
    """
    Returns up to `limit` candidate contacts for a given name string,
    ranked: exact full match → all-tokens-match → first-token-match.
    """
    tokens = [t for t in re.split(r"\s+", name.strip()) if len(t) > 2]
    if not tokens:
        return []

    # Build a `nome ILIKE %t1% AND nome ILIKE %t2% AND ...` query for all-tokens
    # then UNION with single-token fallback
    all_tokens_clause = " AND ".join(["nome ILIKE %s"] * len(tokens))
    all_tokens_args = [f"%{t}%" for t in tokens]

    cur.execute(
        f"""
        SELECT id, nome, empresa, cargo, circulo,
               CASE
                   WHEN LOWER(nome) = LOWER(%s) THEN 0
                   WHEN {all_tokens_clause} THEN 1
                   WHEN nome ILIKE %s THEN 2
                   ELSE 3
               END AS score
        FROM contacts
        WHERE {all_tokens_clause} OR nome ILIKE %s
        ORDER BY score ASC, LENGTH(nome) ASC
        LIMIT %s
        """,
        (name, *all_tokens_args, f"%{tokens[0]}%", *all_tokens_args, f"%{tokens[0]}%", limit),
    )
    return [dict(r) for r in cur.fetchall()]


def gather_db_context(meeting: dict, hints: dict[str, int] | None = None) -> dict:
    """
    For each speaker/invitee/action-item-assignee/mentioned-name in the meeting,
    find matching contacts in the DB.

    Args:
        meeting: Fathom meeting dict
        hints: optional dict mapping a name substring (case-insensitive) to a
               specific contact_id to FORCE the match. Example: {"orestes": 4376}
    """
    hints = {k.lower(): v for k, v in (hints or {}).items()}
    speakers = set()

    for utterance in meeting.get("transcript") or []:
        name = (utterance.get("speaker") or {}).get("display_name") or ""
        if name:
            speakers.add(name)
    for inv in meeting.get("calendar_invitees") or []:
        nm = inv.get("name") or ""
        if nm:
            speakers.add(nm)
    for ai in meeting.get("action_items") or []:
        assignee = (ai.get("assignee") or {}).get("name") or ""
        if assignee:
            speakers.add(assignee)

    # Also extract any name mentioned in summary as "Mr. X" / "Sr. X" / "Dona X"
    summary_text = ((meeting.get("default_summary") or {}).get("markdown_formatted") or "")
    for m in re.finditer(r"\b(?:Mr\.|Sr\.|Dona|Sra\.)\s+([A-ZÀ-Ý][a-zà-ÿ]+)", summary_text):
        speakers.add(m.group(1))

    contacts_found = []
    contacts_unmatched = []

    with get_db() as conn:
        cur = conn.cursor()

        for speaker in speakers:
            forced_id = None
            speaker_lower = speaker.lower()
            for hint_name, hint_id in hints.items():
                if hint_name in speaker_lower:
                    forced_id = hint_id
                    break

            if forced_id is not None:
                forced = _resolve_contact_by_id(cur, forced_id)
                if forced:
                    contacts_found.append({
                        "speaker_name": speaker,
                        "candidates": [forced],
                        "best_match": forced,
                        "forced": True,
                    })
                    continue

            candidates = _search_contacts(cur, speaker, limit=5)
            if candidates:
                contacts_found.append({
                    "speaker_name": speaker,
                    "candidates": candidates,
                    "best_match": candidates[0],
                    "forced": False,
                })
            else:
                contacts_unmatched.append(speaker)

        # Existing projects of best-match contacts
        contact_ids = [c["best_match"]["id"] for c in contacts_found]
        existing_projects = []
        if contact_ids:
            cur.execute(
                """
                SELECT DISTINCT p.id, p.nome, p.tipo, p.status, p.descricao,
                       (SELECT array_agg(pm.contact_id) FROM project_members pm WHERE pm.project_id = p.id) AS member_ids
                FROM projects p
                LEFT JOIN project_members pm ON pm.project_id = p.id
                WHERE p.owner_contact_id = ANY(%s)
                   OR pm.contact_id = ANY(%s)
                ORDER BY p.id
                """,
                (contact_ids, contact_ids),
            )
            existing_projects = [dict(r) for r in cur.fetchall()]

    return {
        "contacts_found": contacts_found,
        "contacts_unmatched": contacts_unmatched,
        "existing_projects": existing_projects,
    }


# ============================================================================
# 3. Generate proposal with Claude
# ============================================================================

PROPOSAL_PROMPT = """Voce e um assistente que organiza dados de reunioes em projetos e tarefas de um CRM.

REUNIAO:
- Titulo: {title}
- Data: {date}
- Duracao: {duration_min} min
- Participantes: {participants}
- Share URL: {share_url}

RESUMO ESTRUTURADO (gerado pelo Fathom AI):
{summary_markdown}

ACTION ITEMS EXTRAIDOS PELO FATHOM:
{action_items}

CONTATOS IDENTIFICADOS NO BANCO (com candidatos):
{contacts_db}

USUARIO DO SISTEMA (Renato — operador do CRM):
{system_user}

PROJETOS EXISTENTES DESTES CONTATOS:
{existing_projects}

{constraints}

## SUA TAREFA

Identifique cada PROJETO/INICIATIVA distinta discutida na reuniao e gere um
plano de acao estruturado SEPARADO para cada um. Para cada projeto:

1. Se ja existe (ver "PROJETOS EXISTENTES" — match por TEMA/ASSUNTO, nao so
   por nome), use action="update" e referencia `existing_project_id`. Pode
   propor atualizar `descricao_update` (opcional).
2. Se nao existe, use action="create" e SEMPRE forneca nome, tipo, e
   `descricao_update` (1-3 frases descrevendo o escopo do projeto). Para
   action="create" o campo `descricao_update` e OBRIGATORIO.
3. Particione o resumo do Fathom: a `note.conteudo_markdown` deve conter
   APENAS as secoes relevantes ao projeto especifico. NAO copie o resumo
   inteiro em todos os projetos.
4. Distribua os action items entre os projetos pelo TEMA. Se um action item
   menciona "Eucaliptos/RL", vai para o projeto de Eucaliptos. Se menciona
   "outorga/dam/represa", vai para o projeto de Outorga.
5. Para cada task:
   - `owner` = "renato" (se Renato deve fazer) ou "contact" (se um contato)
   - Se owner=renato, `owner_contact_id` = id do usuario do sistema (Renato)
   - Se owner=contact, `owner_contact_id` = id do contato no banco
   - `prioridade` 1-10 (1 = mais alta)
   - `prazo_relativo_dias` = quantos dias a partir de hoje (null se sem prazo)

## REGRAS PARA NOMES DE CONTATOS

Quando ha multiplos candidatos com mesmo primeiro nome no banco, USE O CONTEXTO
da reuniao para escolher o correto. Considere empresa, parentesco, papel no
projeto. Se algum contato esta marcado como "FORCED" no banco, USE-O sempre.

## TIPOS DE PROJETO

`tipo` valido: negocio, patrimonio, pessoal
- patrimonio = imoveis, fazenda, recursos da familia
- negocio = empresas, vendas, parcerias
- pessoal = pessoal/familia

## OUTPUT — JSON VALIDO APENAS, SEM MARKDOWN

{{
  "projects": [
    {{
      "action": "update" | "create",
      "existing_project_id": <int> | null,
      "nome": "...",
      "tipo": "patrimonio" | "negocio" | "pessoal",
      "descricao_update": "..." | null,
      "note": {{
        "titulo": "Reuniao: ... - DD/MM/AAAA",
        "conteudo_markdown": "secoes do resumo relevantes a este projeto"
      }},
      "tasks": [
        {{
          "titulo": "...",
          "descricao": "...",
          "owner": "renato" | "contact",
          "owner_contact_id": <int> | null,
          "prioridade": 5,
          "prazo_relativo_dias": <int> | null,
          "ai_confidence": 0.0-1.0
        }}
      ],
      "members_to_add": [
        {{"contact_id": <int>, "papel": "..."}}
      ]
    }}
  ],
  "summary_oneline": "resumo de uma linha do que foi a reuniao",
  "warnings": ["lista de coisas que voce nao conseguiu mapear ou que precisam revisao manual"]
}}
"""


async def generate_proposal_with_claude(
    meeting: dict,
    db_context: dict,
    system_user: dict | None = None,
    min_projects: int | None = None,
) -> dict:
    """Calls Claude to structure the meeting into a proposal."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY nao configurada no .env")

    summary_md = ((meeting.get("default_summary") or {}).get("markdown_formatted") or "")
    action_items_text = "\n".join(
        f"- [{ai.get('recording_timestamp')}] {ai.get('description')} "
        f"(assignee: {(ai.get('assignee') or {}).get('name', 'N/A')})"
        for ai in (meeting.get("action_items") or [])
    ) or "(nenhum)"

    participants = [
        (inv.get("name") or "?") for inv in (meeting.get("calendar_invitees") or [])
    ]
    speakers = sorted({
        (u.get("speaker") or {}).get("display_name", "?")
        for u in (meeting.get("transcript") or [])
    })
    participants_str = ", ".join(set(participants + speakers)) or "(desconhecido)"

    contacts_db_lines = []
    for c in db_context["contacts_found"]:
        forced_marker = " [FORCED]" if c.get("forced") else ""
        bm = c["best_match"]
        contacts_db_lines.append(
            f"- {c['speaker_name']}{forced_marker} -> BEST: id={bm['id']} "
            f"({bm['nome']}, {bm.get('cargo') or '-'}, {bm.get('empresa') or '-'})"
        )
        # If multiple candidates and not forced, list all
        if not c.get("forced") and len(c.get("candidates") or []) > 1:
            for alt in c["candidates"][1:]:
                contacts_db_lines.append(
                    f"    candidato: id={alt['id']} ({alt['nome']}, "
                    f"{alt.get('cargo') or '-'}, {alt.get('empresa') or '-'})"
                )
    if db_context["contacts_unmatched"]:
        for u in db_context["contacts_unmatched"]:
            contacts_db_lines.append(f"- {u} -> NAO ENCONTRADO no banco")
    contacts_db_str = "\n".join(contacts_db_lines) or "(nenhum)"

    if system_user:
        system_user_str = (
            f"id={system_user['id']} ({system_user['nome']}, "
            f"{system_user.get('cargo') or '-'}, {system_user.get('empresa') or '-'})"
        )
    else:
        system_user_str = "(nao configurado — use null para owner_contact_id de tasks do Renato)"

    existing_proj_lines = []
    for p in db_context["existing_projects"]:
        existing_proj_lines.append(
            f"- id={p['id']} | {p['nome']} | tipo={p['tipo']} | status={p['status']} | "
            f"membros={p.get('member_ids') or []}"
        )
        if p.get("descricao"):
            existing_proj_lines.append(f"    descricao: {p['descricao'][:200]}")
    existing_proj_str = "\n".join(existing_proj_lines) or "(nenhum)"

    constraints = ""
    if min_projects:
        constraints = (
            f"## RESTRICAO IMPORTANTE\n"
            f"Esta reuniao DEVE ser dividida em PELO MENOS {min_projects} projetos "
            f"distintos. Se o usuario explicitou que falou de N projetos, gere N "
            f"entradas em `projects[]`. Mesmo que os temas sejam relacionados (ex: "
            f"mesma fazenda), separe-os se sao iniciativas distintas com escopo proprio.\n"
        )

    duration_min = 0
    if meeting.get("recording_start_time") and meeting.get("recording_end_time"):
        try:
            start = datetime.fromisoformat(meeting["recording_start_time"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(meeting["recording_end_time"].replace("Z", "+00:00"))
            duration_min = int((end - start).total_seconds() // 60)
        except Exception:
            pass

    prompt = PROPOSAL_PROMPT.format(
        title=meeting.get("title") or "Reuniao Fathom",
        date=meeting.get("scheduled_start_time") or meeting.get("recording_start_time") or "?",
        duration_min=duration_min,
        participants=participants_str,
        share_url=meeting.get("share_url") or "",
        summary_markdown=summary_md or "(sem summary do Fathom)",
        action_items=action_items_text,
        contacts_db=contacts_db_str,
        system_user=system_user_str,
        existing_projects=existing_proj_str,
        constraints=constraints,
    )

    print(f"🧠 Chamando Claude ({CLAUDE_MODEL})...")
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        text = data["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        return json.loads(text)


# ============================================================================
# 4. Print proposal nicely
# ============================================================================

def print_proposal(proposal: dict, meeting: dict):
    print()
    print("=" * 76)
    print(f"📋 PROPOSTA — {meeting.get('title')}")
    print("=" * 76)
    print(f"Data:        {meeting.get('scheduled_start_time')}")
    print(f"Conta:       {meeting.get('_account')}")
    print(f"Recording:   {meeting.get('recording_id')}")
    print(f"Resumo:      {proposal.get('summary_oneline', '')}")
    print()

    for i, p in enumerate(proposal.get("projects") or [], 1):
        action_label = "📂 ATUALIZAR" if p["action"] == "update" else "🆕 CRIAR"
        existing = f" (id={p['existing_project_id']})" if p.get("existing_project_id") else ""
        print(f"--- Projeto {i}: {action_label}{existing} ---")
        print(f"  Nome: {p.get('nome')}")
        print(f"  Tipo: {p.get('tipo')}")
        if p.get("descricao_update"):
            print(f"  Descricao: {p['descricao_update'][:120]}")
        if p.get("note"):
            print(f"  Nota: {p['note']['titulo']}")
            print(f"        ({len(p['note'].get('conteudo_markdown', ''))} chars de conteudo)")
        if p.get("members_to_add"):
            print(f"  Membros a adicionar:")
            for m in p["members_to_add"]:
                print(f"    - contact_id={m['contact_id']} papel={m.get('papel', '?')}")
        print(f"  Tasks ({len(p.get('tasks') or [])}):")
        for t in p.get("tasks") or []:
            owner_str = f"contact={t['owner_contact_id']}" if t["owner"] == "contact" else "Renato"
            prazo_str = f"em {t['prazo_relativo_dias']}d" if t.get("prazo_relativo_dias") else "sem prazo"
            print(f"    • [{owner_str}] [{prazo_str}] {t['titulo']}")
        print()

    if proposal.get("warnings"):
        print("⚠️  AVISOS:")
        for w in proposal["warnings"]:
            print(f"  - {w}")
        print()

    print("=" * 76)


# ============================================================================
# 5. Apply proposal to DB (idempotent)
# ============================================================================

def apply_proposal(proposal_path: Path, dry_run: bool = False):
    proposal = json.loads(proposal_path.read_text())
    meeting = proposal["_meeting"]
    recording_id = meeting.get("recording_id")
    share_url = meeting.get("share_url")

    # Resolve system user (Renato) for owner=renato tasks
    system_user = _resolve_system_user()
    system_user_id = system_user["id"] if system_user else None

    print(f"\n{'🧪 DRY-RUN' if dry_run else '✏️  APLICANDO'} proposta de {proposal_path}")
    print(f"   Reuniao: {meeting.get('title')}  ({recording_id})")
    if system_user_id:
        print(f"   System user (Renato): id={system_user_id}")

    stats = {
        "projects_created": 0,
        "projects_updated": 0,
        "notes_created": 0,
        "notes_skipped": 0,
        "tasks_created": 0,
        "members_added": 0,
    }

    with get_db() as conn:
        cur = conn.cursor()

        for p in proposal.get("projects") or []:
            project_id = p.get("existing_project_id")

            if p["action"] == "create":
                if dry_run:
                    print(f"   [DRY] CREATE project: {p['nome']}")
                    project_id = -1  # placeholder
                else:
                    cur.execute(
                        """
                        INSERT INTO projects (nome, tipo, descricao, status, metadata)
                        VALUES (%s, %s, %s, 'ativo', %s)
                        RETURNING id
                        """,
                        (
                            p["nome"],
                            p.get("tipo") or "negocio",
                            p.get("descricao_update") or "",
                            json.dumps({
                                "created_from_fathom": True,
                                "fathom_recording_id": recording_id,
                                "fathom_share_url": share_url,
                            }),
                        ),
                    )
                    project_id = cur.fetchone()["id"]
                    stats["projects_created"] += 1
                    print(f"   ✓ Projeto criado: id={project_id} {p['nome']}")

            elif p["action"] == "update":
                if not project_id:
                    print(f"   ⚠️  update sem existing_project_id, pulando: {p['nome']}")
                    continue
                if p.get("descricao_update"):
                    if dry_run:
                        print(f"   [DRY] UPDATE project {project_id} descricao")
                    else:
                        cur.execute(
                            """
                            UPDATE projects
                            SET descricao = COALESCE(descricao, '') ||
                                CASE WHEN COALESCE(descricao, '') = '' THEN '' ELSE E'\\n\\n' END
                                || %s,
                                atualizado_em = NOW()
                            WHERE id = %s
                            """,
                            (p["descricao_update"], project_id),
                        )
                        stats["projects_updated"] += 1
                        print(f"   ✓ Projeto atualizado: id={project_id}")

            # Note (idempotent: check by metadata->fathom_recording_id + project)
            note = p.get("note")
            if note and project_id:
                if not dry_run and project_id != -1:
                    cur.execute(
                        """
                        SELECT id FROM project_notes
                        WHERE project_id = %s
                          AND metadata->>'fathom_recording_id' = %s
                        """,
                        (project_id, str(recording_id)),
                    )
                    existing = cur.fetchone()
                    if existing:
                        stats["notes_skipped"] += 1
                        print(f"   ⊝ Nota ja existe (idempotencia): id={existing['id']}")
                    else:
                        cur.execute(
                            """
                            INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor, metadata)
                            VALUES (%s, 'reuniao_fathom', %s, %s, 'Fathom AI', %s)
                            RETURNING id
                            """,
                            (
                                project_id,
                                note["titulo"],
                                note["conteudo_markdown"],
                                json.dumps({
                                    "fathom_recording_id": recording_id,
                                    "fathom_share_url": share_url,
                                    "fathom_account": meeting.get("_account"),
                                    "duration_min": meeting.get("_duration_min"),
                                }),
                            ),
                        )
                        note_id = cur.fetchone()["id"]
                        stats["notes_created"] += 1
                        print(f"   ✓ Nota criada: id={note_id} \"{note['titulo']}\"")
                else:
                    print(f"   [DRY] CREATE note: {note['titulo']}")

            # Members
            for m in p.get("members_to_add") or []:
                contact_id = m["contact_id"]
                papel = m.get("papel") or ""
                if dry_run or project_id == -1:
                    print(f"   [DRY] ADD member contact={contact_id} papel={papel}")
                    continue
                cur.execute(
                    """
                    INSERT INTO project_members (project_id, contact_id, papel)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (project_id, contact_id) DO UPDATE
                        SET papel = COALESCE(EXCLUDED.papel, project_members.papel)
                    """,
                    (project_id, contact_id, papel),
                )
                stats["members_added"] += 1

            # Tasks
            for t in p.get("tasks") or []:
                vencimento = None
                if t.get("prazo_relativo_dias"):
                    from datetime import timedelta
                    vencimento = datetime.now() + timedelta(days=t["prazo_relativo_dias"])

                if dry_run or project_id == -1:
                    print(f"   [DRY] CREATE task: {t['titulo']}")
                    continue
                # Resolve owner contact_id: explicit > owner=renato → system_user > null
                if t.get("owner") == "contact":
                    task_contact_id = t.get("owner_contact_id")
                elif t.get("owner") == "renato":
                    task_contact_id = t.get("owner_contact_id") or system_user_id
                else:
                    task_contact_id = t.get("owner_contact_id")

                cur.execute(
                    """
                    INSERT INTO tasks (
                        titulo, descricao, contact_id, project_id,
                        data_vencimento, prioridade, status,
                        origem, source_table, source_id,
                        ai_generated, confianca_ai, contexto
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, 'pending',
                        'fathom', 'project_notes', %s,
                        true, %s, 'professional'
                    )
                    RETURNING id
                    """,
                    (
                        t["titulo"],
                        t.get("descricao") or "",
                        task_contact_id,
                        project_id,
                        vencimento,
                        t.get("prioridade") or 5,
                        None,  # source_id linked later if needed
                        t.get("ai_confidence") or 0.8,
                    ),
                )
                stats["tasks_created"] += 1

        if not dry_run:
            conn.commit()

    print()
    print("=" * 76)
    print("RESUMO DA APLICACAO" + (" (DRY-RUN)" if dry_run else ""))
    print("=" * 76)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()


# ============================================================================
# CLI
# ============================================================================

def _resolve_system_user() -> dict | None:
    """Find Renato as a contact in the DB so tasks attributed to him can have owner_contact_id."""
    with get_db() as conn:
        cur = conn.cursor()
        for name in SYSTEM_USER_NAMES:
            cur.execute(
                "SELECT id, nome, empresa, cargo FROM contacts WHERE nome ILIKE %s LIMIT 1",
                (name,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
    return None


def _parse_hints(hint_args: list[str]) -> dict[str, int]:
    out = {}
    for h in hint_args or []:
        if "=" not in h:
            raise SystemExit(f"--contact-hint formato invalido: {h}. Use 'nome=id'")
        name, _id = h.split("=", 1)
        out[name.strip()] = int(_id.strip())
    return out


async def main_async(args):
    if args.apply:
        apply_proposal(Path(args.apply), dry_run=args.dry_run)
        return

    if not args.share_url:
        print("ERRO: passe um share_url ou use --apply <proposal.json>")
        sys.exit(1)

    hints = _parse_hints(args.contact_hint or [])
    if hints:
        print(f"🎯 Hints aplicados: {hints}")

    meeting = await fetch_meeting_from_fathom(args.share_url)

    duration_min = 0
    if meeting.get("recording_start_time") and meeting.get("recording_end_time"):
        try:
            start = datetime.fromisoformat(meeting["recording_start_time"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(meeting["recording_end_time"].replace("Z", "+00:00"))
            duration_min = int((end - start).total_seconds() // 60)
        except Exception:
            pass
    meeting["_duration_min"] = duration_min

    print(f"📊 Meeting: {meeting.get('title')} ({duration_min}min)")

    system_user = _resolve_system_user()
    if system_user:
        print(f"👤 Usuario do sistema: {system_user['nome']} (id={system_user['id']})")
    else:
        print("⚠️  Usuario do sistema (Renato) nao encontrado nos contatos")

    db_context = gather_db_context(meeting, hints=hints)
    print(f"👥 Contatos identificados: {len(db_context['contacts_found'])}")
    for c in db_context["contacts_found"]:
        forced = " [FORCED]" if c.get("forced") else ""
        print(f"   - {c['speaker_name']}{forced} -> {c['best_match']['nome']} (id={c['best_match']['id']})")
    if db_context["contacts_unmatched"]:
        print(f"   Não encontrados: {db_context['contacts_unmatched']}")
    print(f"📁 Projetos existentes destes contatos: {len(db_context['existing_projects'])}")

    if args.min_projects:
        print(f"🎯 Forcando minimo de {args.min_projects} projetos")

    proposal = await generate_proposal_with_claude(
        meeting,
        db_context,
        system_user=system_user,
        min_projects=args.min_projects,
    )
    proposal["_meeting"] = {
        "recording_id": meeting.get("recording_id"),
        "share_url": meeting.get("share_url"),
        "title": meeting.get("title"),
        "_account": meeting.get("_account"),
        "_duration_min": duration_min,
        "scheduled_start_time": meeting.get("scheduled_start_time"),
    }

    out_path = Path(args.out or f"/tmp/fathom_proposal_{meeting.get('recording_id')}.json")
    out_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2))

    print_proposal(proposal, meeting)

    print(f"💾 Proposta salva em: {out_path}")
    print(f"\nPara aplicar:")
    print(f"  python scripts/import_fathom_meeting.py --apply {out_path}")
    print(f"\nPara dry-run (testar sem persistir):")
    print(f"  python scripts/import_fathom_meeting.py --apply {out_path} --dry-run")


def main():
    parser = argparse.ArgumentParser(description="Import Fathom meeting into projects/tasks")
    parser.add_argument("share_url", nargs="?", help="Fathom share URL (https://fathom.video/share/...)")
    parser.add_argument("--apply", help="Path to proposal JSON to apply")
    parser.add_argument("--dry-run", action="store_true", help="Apply mode without committing")
    parser.add_argument("--out", help="Where to save the proposal JSON (default: /tmp/fathom_proposal_<id>.json)")
    parser.add_argument(
        "--contact-hint",
        action="append",
        help="Force a contact match: 'name_substring=contact_id'. Repeatable. "
             "Example: --contact-hint 'Orestes=4376'",
    )
    parser.add_argument(
        "--min-projects",
        type=int,
        help="Force the proposal to contain at least N distinct projects",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Apply to PRODUCTION (Neon) instead of local DB. Default: local.",
    )
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
