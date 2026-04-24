"""
ConselhoOS Pre-Meeting Briefing Service

Coleta dados do ConselhoOS e INTEL, envia ao Claude para gerar
um briefing executivo adaptativo pre-reuniao.

Author: INTEL
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from database import get_db

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CONSELHOOS_DATABASE_URL = os.getenv("CONSELHOOS_DATABASE_URL")
CONSELHOOS_USER_ID = os.getenv("CONSELHOOS_USER_ID", "")


def _get_conselhoos_conn():
    """Get connection to ConselhoOS database."""
    if not CONSELHOOS_DATABASE_URL:
        raise ValueError("CONSELHOOS_DATABASE_URL not configured")
    return psycopg2.connect(
        CONSELHOOS_DATABASE_URL,
        cursor_factory=RealDictCursor
    )


def _truncate(text: Optional[str], max_len: int = 500) -> str:
    """Truncate text safely."""
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ==================== ConselhoOS Data Collection ====================

def _collect_reuniao_info(cos_cursor, reuniao_id: str) -> Optional[Dict]:
    """Fetch reuniao basic info + empresa."""
    cos_cursor.execute("""
        SELECT
            r.id, r.titulo, r.data, r.status,
            r.pauta_md, r.dossie_md,
            e.id as empresa_id, e.nome as empresa_nome,
            e.setor as empresa_setor, e.descricao as empresa_descricao,
            e.contexto_md as empresa_contexto_md
        FROM reunioes r
        JOIN empresas e ON e.id = r.empresa_id
        WHERE r.id = %s::uuid
    """, (reuniao_id,))
    row = cos_cursor.fetchone()
    return dict(row) if row else None


def _collect_checklist(cos_cursor, reuniao_id: str) -> List[Dict]:
    """Fetch preparacao checklist status."""
    cos_cursor.execute("""
        SELECT
            ci.codigo, ci.nome, ci.obrigatorio,
            pr.status,
            pr.notas
        FROM preparacao_reunioes pr
        JOIN checklist_items ci ON ci.id = pr.checklist_item_id
        WHERE pr.reuniao_id = %s::uuid
        ORDER BY ci.ordem
    """, (reuniao_id,))
    return [dict(row) for row in cos_cursor.fetchall()]


def _collect_documentos(cos_cursor, reuniao_id: str) -> List[Dict]:
    """Fetch documents linked to the reuniao."""
    cos_cursor.execute("""
        SELECT
            d.nome, d.tipo, d.resumo,
            d.briefing_md, d.conteudo_extraido
        FROM documentos d
        WHERE d.reuniao_id = %s::uuid
        ORDER BY d.created_at DESC
    """, (reuniao_id,))
    docs = []
    for row in cos_cursor.fetchall():
        doc = dict(row)
        doc['briefing_md'] = _truncate(doc.get('briefing_md'), 1000)
        doc['conteudo_extraido'] = _truncate(doc.get('conteudo_extraido'), 500)
        docs.append(doc)
    return docs


def _collect_temas(cos_cursor, reuniao_id: str) -> List[Dict]:
    """Fetch temas da reuniao."""
    cos_cursor.execute("""
        SELECT
            tr.titulo, tr.descricao, tr.tempo_estimado,
            tr.status, tr.notas, tr.decisao,
            ta.titulo as tema_anual_titulo
        FROM temas_reuniao tr
        LEFT JOIN temas_anuais ta ON ta.id = tr.tema_anual_id
        WHERE tr.reuniao_id = %s::uuid
        ORDER BY tr.ordem
    """, (reuniao_id,))
    return [dict(row) for row in cos_cursor.fetchall()]


def _collect_raci_pendentes(cos_cursor, empresa_id: str) -> List[Dict]:
    """Fetch pending RACI items for the empresa."""
    cos_cursor.execute("""
        SELECT
            area, acao, prazo, status,
            responsavel_r, responsavel_a,
            CASE
                WHEN prazo < CURRENT_DATE THEN 'ATRASADO'
                WHEN prazo <= CURRENT_DATE + INTERVAL '7 days' THEN 'URGENTE'
                ELSE 'normal'
            END as urgencia
        FROM raci_itens
        WHERE empresa_id = %s::uuid
          AND status NOT IN ('concluido', 'cancelado')
        ORDER BY prazo ASC
        LIMIT 30
    """, (empresa_id,))
    return [dict(row) for row in cos_cursor.fetchall()]


def _collect_previous_reuniao(cos_cursor, empresa_id: str, data_reuniao) -> Optional[Dict]:
    """Fetch the most recent previous reuniao ata and decisoes."""
    cos_cursor.execute("""
        SELECT
            r.id, r.titulo, r.data, r.ata_md
        FROM reunioes r
        WHERE r.empresa_id = %s::uuid
          AND r.data < %s
          AND r.status = 'concluida'
        ORDER BY r.data DESC
        LIMIT 1
    """, (empresa_id, data_reuniao))
    row = cos_cursor.fetchone()
    if not row:
        return None

    result = dict(row)
    result['ata_md'] = _truncate(result.get('ata_md'), 2000)

    # Fetch decisoes from previous reuniao
    cos_cursor.execute("""
        SELECT codigo, titulo, descricao, urgencia, opcao_escolhida, status
        FROM decisoes
        WHERE reuniao_id = %s::uuid
        ORDER BY created_at
    """, (str(result['id']),))
    result['decisoes'] = [dict(r) for r in cos_cursor.fetchall()]

    return result


def _collect_pessoas(cos_cursor, empresa_id: str) -> List[Dict]:
    """Fetch pessoas da empresa."""
    cos_cursor.execute("""
        SELECT
            id, nome, email, cargo, papel,
            intel_contact_id, linkedin_url
        FROM pessoas
        WHERE empresa_id = %s::uuid
          AND ativo = true
        ORDER BY nome
    """, (empresa_id,))
    return [dict(row) for row in cos_cursor.fetchall()]


# ==================== INTEL Data Collection ====================

def _collect_intel_contact_data(contact_id: int) -> Dict:
    """Collect INTEL data for a single contact."""
    data = {
        "whatsapp_messages": [],
        "memories": [],
        "linkedin": {},
        "ultimo_contato": None,
        "health_score": None,
        "pending_tasks": []
    }

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Contact basic info
            cursor.execute("""
                SELECT nome, empresa, cargo, linkedin_headline, linkedin_about,
                       ultimo_contato, health_score, circulo
                FROM contacts
                WHERE id = %s
            """, (contact_id,))
            contact = cursor.fetchone()
            if contact:
                contact = dict(contact)
                data['linkedin'] = {
                    'headline': contact.get('linkedin_headline'),
                    'about': _truncate(contact.get('linkedin_about'), 300)
                }
                data['ultimo_contato'] = str(contact.get('ultimo_contato')) if contact.get('ultimo_contato') else None
                data['health_score'] = contact.get('health_score')

            # Last 5 WhatsApp messages
            cursor.execute("""
                SELECT conteudo, direcao, enviado_em
                FROM messages
                WHERE contact_id = %s
                  AND canal = 'whatsapp'
                ORDER BY enviado_em DESC
                LIMIT 5
            """, (contact_id,))
            for row in cursor.fetchall():
                msg = dict(row)
                msg['conteudo'] = _truncate(msg.get('conteudo'), 200)
                msg['enviado_em'] = str(msg['enviado_em']) if msg.get('enviado_em') else None
                data['whatsapp_messages'].append(msg)

            # Last 3 contact memories
            cursor.execute("""
                SELECT tipo, conteudo, data_ocorrencia, importancia
                FROM contact_memories
                WHERE contact_id = %s
                ORDER BY data_ocorrencia DESC
                LIMIT 3
            """, (contact_id,))
            for row in cursor.fetchall():
                mem = dict(row)
                mem['conteudo'] = _truncate(mem.get('conteudo'), 300)
                mem['data_ocorrencia'] = str(mem['data_ocorrencia']) if mem.get('data_ocorrencia') else None
                data['memories'].append(mem)

            # Pending tasks for this contact
            cursor.execute("""
                SELECT titulo, descricao, data_vencimento, prioridade, status
                FROM tasks
                WHERE contact_id = %s
                  AND status NOT IN ('completed', 'cancelled')
                ORDER BY data_vencimento ASC
                LIMIT 5
            """, (contact_id,))
            for row in cursor.fetchall():
                task = dict(row)
                task['descricao'] = _truncate(task.get('descricao'), 150)
                task['data_vencimento'] = str(task['data_vencimento']) if task.get('data_vencimento') else None
                data['pending_tasks'].append(task)

    except Exception as e:
        logger.error(f"Error collecting INTEL data for contact {contact_id}: {e}")

    return data


def _collect_intel_project_data(empresa_nome: str) -> Dict:
    """Collect INTEL project data matching the empresa."""
    data = {
        "overdue_tasks": [],
        "action_proposals": []
    }

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Find matching project
            cursor.execute("""
                SELECT id FROM projects
                WHERE LOWER(nome) LIKE LOWER(%s)
                LIMIT 1
            """, (f"%{empresa_nome}%",))
            project = cursor.fetchone()
            if not project:
                return data

            project_id = project['id']

            # Overdue tasks
            cursor.execute("""
                SELECT t.titulo, t.descricao, t.data_vencimento, t.prioridade,
                       c.nome as contact_nome
                FROM tasks t
                LEFT JOIN contacts c ON c.id = t.contact_id
                WHERE t.project_id = %s
                  AND t.status NOT IN ('completed', 'cancelled')
                  AND t.data_vencimento < CURRENT_DATE
                ORDER BY t.data_vencimento ASC
                LIMIT 10
            """, (project_id,))
            for row in cursor.fetchall():
                task = dict(row)
                task['descricao'] = _truncate(task.get('descricao'), 150)
                task['data_vencimento'] = str(task['data_vencimento']) if task.get('data_vencimento') else None
                data['overdue_tasks'].append(task)

            # Recent action proposals for contacts in this project
            cursor.execute("""
                SELECT ap.tipo, ap.titulo, ap.descricao, ap.status,
                       c.nome as contact_nome
                FROM action_proposals ap
                JOIN project_members pm ON pm.contact_id = ap.contact_id
                JOIN contacts c ON c.id = ap.contact_id
                WHERE pm.project_id = %s
                  AND ap.created_at >= NOW() - INTERVAL '30 days'
                ORDER BY ap.created_at DESC
                LIMIT 10
            """, (project_id,))
            for row in cursor.fetchall():
                prop = dict(row)
                prop['descricao'] = _truncate(prop.get('descricao'), 200)
                data['action_proposals'].append(prop)

    except Exception as e:
        logger.error(f"Error collecting INTEL project data for {empresa_nome}: {e}")

    return data


# ==================== Claude Synthesis ====================

def _format_section(title: str, data: Any) -> str:
    """Format a data section for the prompt."""
    if not data:
        return f"\n### {title}\n(sem dados disponíveis)\n"

    if isinstance(data, list):
        if len(data) == 0:
            return f"\n### {title}\n(nenhum item)\n"
        items = json.dumps(data, ensure_ascii=False, default=str, indent=2)
        return f"\n### {title}\n```json\n{items}\n```\n"

    if isinstance(data, dict):
        items = json.dumps(data, ensure_ascii=False, default=str, indent=2)
        return f"\n### {title}\n```json\n{items}\n```\n"

    return f"\n### {title}\n{str(data)}\n"


async def _call_claude(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Call Claude API and return response text."""
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not configured")
        return None

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 2000,
                    "system": system_prompt,
                    "messages": [
                        {"role": "user", "content": user_prompt}
                    ]
                }
            )

            if response.status_code != 200:
                logger.error(f"Claude API error: {response.status_code} - {response.text[:300]}")
                return None

            result = response.json()
            return result.get("content", [{}])[0].get("text", "")

    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        return None


# ==================== Main Function ====================

async def generate_pre_meeting_briefing(reuniao_id: str) -> Dict:
    """
    Generate an adaptive pre-meeting briefing combining ConselhoOS
    ritual data with INTEL relational intelligence.

    Args:
        reuniao_id: UUID of the reuniao in ConselhoOS

    Returns:
        Dict with briefing_md, generated_at, and metadata
    """
    logger.info(f"Generating pre-meeting briefing for reuniao {reuniao_id}")

    # ---- Phase 1: Collect ConselhoOS data ----
    try:
        cos_conn = _get_conselhoos_conn()
        cos_cursor = cos_conn.cursor()
    except Exception as e:
        return {"error": f"Erro ao conectar ao ConselhoOS: {str(e)}"}

    try:
        # Reuniao + Empresa info
        reuniao = _collect_reuniao_info(cos_cursor, reuniao_id)
        if not reuniao:
            cos_conn.close()
            return {"error": f"Reuniao {reuniao_id} nao encontrada"}

        empresa_id = str(reuniao['empresa_id'])
        empresa_nome = reuniao['empresa_nome']
        data_reuniao = reuniao['data']

        # Checklist
        checklist = _collect_checklist(cos_cursor, reuniao_id)

        # Documentos
        documentos = _collect_documentos(cos_cursor, reuniao_id)

        # Temas
        temas = _collect_temas(cos_cursor, reuniao_id)

        # RACI pendentes
        raci_pendentes = _collect_raci_pendentes(cos_cursor, empresa_id)

        # Previous reuniao
        previous = _collect_previous_reuniao(cos_cursor, empresa_id, data_reuniao)

        # Pessoas
        pessoas = _collect_pessoas(cos_cursor, empresa_id)

        cos_conn.close()

    except Exception as e:
        cos_conn.close()
        logger.error(f"Error collecting ConselhoOS data: {e}")
        return {"error": f"Erro ao coletar dados ConselhoOS: {str(e)}"}

    # ---- Phase 2: Collect INTEL data for each pessoa ----
    pessoas_intel = []
    for pessoa in pessoas:
        intel_id = pessoa.get('intel_contact_id')
        intel_data = {}
        if intel_id:
            intel_data = _collect_intel_contact_data(intel_id)
        pessoas_intel.append({
            "nome": pessoa.get('nome'),
            "email": pessoa.get('email'),
            "cargo": pessoa.get('cargo'),
            "papel": pessoa.get('papel'),
            "intel_data": intel_data if intel_id else None
        })

    # INTEL project data
    project_data = _collect_intel_project_data(empresa_nome)

    # ---- Phase 3: Build prompt and call Claude ----
    system_prompt = """Voce e o preparador de briefings para reunioes de conselho do Renato.
Analise TODOS os dados disponíveis e gere um briefing executivo pre-reuniao.
Seja direto e acionavel. Nao repita dados — sintetize.
Use markdown para formatacao. Seja conciso mas completo.
Se alguma secao nao tiver dados, mencione brevemente e siga em frente.
Nunca invente dados — se nao ha informacao, diga "sem dados disponíveis"."""

    # Build user prompt with all data
    prompt_parts = []
    prompt_parts.append(f"# BRIEFING PRE-REUNIAO\n")
    prompt_parts.append(f"**REUNIAO:** {reuniao.get('titulo', 'Reuniao de Conselho')} - {data_reuniao}")
    prompt_parts.append(f"**EMPRESA:** {empresa_nome} ({reuniao.get('empresa_setor', 'N/A')})")

    if reuniao.get('empresa_descricao'):
        prompt_parts.append(f"**DESCRICAO:** {_truncate(reuniao['empresa_descricao'], 500)}")
    if reuniao.get('empresa_contexto_md'):
        prompt_parts.append(f"**CONTEXTO ESTRATEGICO:**\n{_truncate(reuniao['empresa_contexto_md'], 1000)}")

    prompt_parts.append(_format_section("PAUTA DA REUNIAO", reuniao.get('pauta_md') or 'Nao definida'))
    prompt_parts.append(_format_section("CHECKLIST DE PREPARACAO", checklist))
    prompt_parts.append(_format_section("DOCUMENTOS DA REUNIAO", documentos))
    prompt_parts.append(_format_section("TEMAS DA REUNIAO", temas))
    prompt_parts.append(_format_section("RACI PENDENTES DA EMPRESA", raci_pendentes))

    if previous:
        prompt_parts.append(_format_section("REUNIAO ANTERIOR", {
            "titulo": previous.get('titulo'),
            "data": str(previous.get('data')),
            "ata_resumo": previous.get('ata_md', ''),
            "decisoes": previous.get('decisoes', [])
        }))

    prompt_parts.append(_format_section("PARTICIPANTES (com dados INTEL)", pessoas_intel))

    if project_data.get('overdue_tasks'):
        prompt_parts.append(_format_section("TASKS ATRASADAS (INTEL)", project_data['overdue_tasks']))
    if project_data.get('action_proposals'):
        prompt_parts.append(_format_section("PROPOSTAS DE ACAO RECENTES (INTEL)", project_data['action_proposals']))

    prompt_parts.append("""
---

GERE UM BRIEFING com estas secoes:
1. **VISAO GERAL**: 2-3 frases sobre o estado atual da empresa e o que esperar da reuniao
2. **PREPARACAO**: checklist items pendentes, documentos faltando
3. **PAUTA**: temas previstos com contexto relevante
4. **PARTICIPANTES**: pra cada pessoa, um paragrafo com:
   - Ultima interacao (WhatsApp/email)
   - Pendencias (RACIs atrasados, tasks)
   - Contexto pessoal relevante
5. **PONTOS DE ATENCAO**: RACIs atrasados, decisoes nao implementadas, riscos
6. **SUGESTOES**: o que Renato deveria abordar, perguntar ou cobrar

Seja direto e acionavel. Nao repita dados — sintetize.
""")

    user_prompt = "\n".join(prompt_parts)

    # Truncate total prompt if too large (Claude has limits on practical input)
    if len(user_prompt) > 15000:
        user_prompt = user_prompt[:15000] + "\n\n[... dados truncados por limite de tamanho ...]"

    briefing_md = await _call_claude(system_prompt, user_prompt)

    if not briefing_md:
        return {"error": "Falha ao gerar briefing via Claude AI"}

    generated_at = datetime.now().isoformat()

    return {
        "briefing_md": briefing_md,
        "generated_at": generated_at,
        "reuniao_id": reuniao_id,
        "empresa_nome": empresa_nome,
        "metadata": {
            "pessoas_count": len(pessoas),
            "documentos_count": len(documentos),
            "raci_pendentes_count": len(raci_pendentes),
            "temas_count": len(temas),
            "has_previous_reuniao": previous is not None,
            "intel_contacts_linked": sum(1 for p in pessoas if p.get('intel_contact_id'))
        }
    }


async def check_and_generate_briefings_tomorrow() -> List[Dict]:
    """
    Cron function: check if any reuniao is happening tomorrow,
    auto-generate briefing if not yet generated.

    Returns list of generated briefings.
    """
    results = []

    try:
        cos_conn = _get_conselhoos_conn()
        cos_cursor = cos_conn.cursor()

        # Find reunioes happening tomorrow
        cos_cursor.execute("""
            SELECT r.id, r.titulo, r.data, r.dossie_md,
                   e.nome as empresa_nome
            FROM reunioes r
            JOIN empresas e ON e.id = r.empresa_id
            WHERE r.data >= CURRENT_DATE + INTERVAL '1 day'
              AND r.data < CURRENT_DATE + INTERVAL '2 days'
              AND r.status = 'agendada'
        """)
        reunioes_amanha = [dict(row) for row in cos_cursor.fetchall()]
        cos_conn.close()

        if not reunioes_amanha:
            logger.info("No reunioes happening tomorrow")
            return results

        for reuniao in reunioes_amanha:
            reuniao_id = str(reuniao['id'])
            titulo = reuniao.get('titulo', 'Reuniao')
            empresa = reuniao.get('empresa_nome', '')

            # Skip if dossie already exists (generated in last 24h)
            if reuniao.get('dossie_md'):
                logger.info(f"Briefing already exists for {titulo} ({empresa}), skipping")
                continue

            logger.info(f"Auto-generating briefing for tomorrow: {titulo} ({empresa})")

            result = await generate_pre_meeting_briefing(reuniao_id)

            if result.get('briefing_md'):
                # Save to ConselhoOS dossie_md
                try:
                    cos_conn2 = _get_conselhoos_conn()
                    cos_cursor2 = cos_conn2.cursor()
                    cos_cursor2.execute("""
                        UPDATE reunioes
                        SET dossie_md = %s, dossie_gerado_em = NOW(), updated_at = NOW()
                        WHERE id = %s::uuid
                    """, (result['briefing_md'], reuniao_id))
                    cos_conn2.commit()
                    cos_conn2.close()
                    result['saved_to_conselhoos'] = True
                except Exception as e:
                    logger.error(f"Error saving briefing to ConselhoOS: {e}")
                    result['saved_to_conselhoos'] = False

            results.append({
                "reuniao_id": reuniao_id,
                "titulo": titulo,
                "empresa": empresa,
                "success": 'briefing_md' in result,
                "error": result.get('error')
            })

    except Exception as e:
        logger.error(f"Error in check_and_generate_briefings_tomorrow: {e}")
        results.append({"error": str(e)})

    return results
