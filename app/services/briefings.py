"""
Servico de Briefings Inteligentes

Gera resumos contextuais sobre contatos para preparacao de reunioes.
Inclui historico, health score, fatos importantes e sugestoes de pauta.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import json
import os
import httpx

from database import get_db
from services.circulos import (
    CIRCULO_CONFIG,
    calcular_health_score,
    calcular_dias_sem_contato
)
import re

# Configuracao AI
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


# ============== PERSISTENCIA DE BRIEFINGS ==============

def parse_briefing_sections(briefing_text: str) -> Dict:
    """
    Extrai secoes estruturadas do texto do briefing.
    Suporta formatos: **TITULO**, ## TITULO, ## 1. TITULO

    Returns:
        Dict com summary, opportunities, next_steps, talking_points
    """
    result = {
        "summary": "",
        "opportunities": [],
        "next_steps": [],
        "talking_points": []
    }

    if not briefing_text:
        return result

    text = briefing_text

    # Pattern flexivel para headers: **TITULO**, ## TITULO, ## 1. TITULO
    def find_section(patterns, text):
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    # Extrair resumo
    resumo_patterns = [
        r'(?:\*\*|##\s*\d*\.?\s*)RESUMO[*\s]*\n(.+?)(?=(?:\*\*|##)\s*\d*\.?\s*[A-Z]|\Z)',
        r'RESUMO[:\s]*\n(.+?)(?=\n##|\n\*\*|\Z)'
    ]
    resumo = find_section(resumo_patterns, text)
    if resumo:
        result["summary"] = resumo[:500]

    # Extrair oportunidades
    oport_patterns = [
        r'(?:\*\*|##\s*\d*\.?\s*)OPORTUNIDADES?[*\s]*\n(.+?)(?=(?:\*\*|##)\s*\d*\.?\s*[A-Z]|\Z)',
        r'OPORTUNIDADES?[:\s]*\n(.+?)(?=\n##|\n\*\*|\Z)'
    ]
    oport_text = find_section(oport_patterns, text)
    if oport_text:
        # Extrair items com bullets, asteriscos ou numeracao
        items = re.findall(r'(?:[-•*]\s*\*?\*?|\d+\.\s*)([^\n]+)', oport_text)
        result["opportunities"] = [item.strip().rstrip('*')[:200] for item in items if item.strip() and len(item.strip()) > 5][:5]

    # Extrair sugestoes de pauta
    pauta_patterns = [
        r'(?:\*\*|##\s*\d*\.?\s*)SUGEST[ÕO]ES DE PAUTA[*\s]*\n(.+?)(?=(?:\*\*|##)\s*\d*\.?\s*[A-Z]|\Z)',
        r'SUGEST[ÕO]ES[:\s]*\n(.+?)(?=\n##|\n\*\*|\Z)'
    ]
    pauta_text = find_section(pauta_patterns, text)
    if pauta_text:
        items = re.findall(r'(?:[-•*]\s*\*?\*?|\d+\.\s*)([^\n]+)', pauta_text)
        result["talking_points"] = [item.strip().rstrip('*')[:200] for item in items if item.strip() and len(item.strip()) > 5][:5]

    # Extrair pontos de atencao
    atencao_patterns = [
        r'(?:\*\*|##\s*\d*\.?\s*)PONTOS DE ATEN[ÇC][ÃA]O[*\s]*\n(.+?)(?=(?:\*\*|##)\s*\d*\.?\s*[A-Z]|\Z)',
        r'ATEN[ÇC][ÃA]O[:\s]*\n(.+?)(?=\n##|\n\*\*|\Z)'
    ]
    atencao_text = find_section(atencao_patterns, text)
    if atencao_text:
        items = re.findall(r'(?:[-•*]\s*\*?\*?|\d+\.\s*)([^\n]+)', atencao_text)
        result["next_steps"] = [item.strip().rstrip('*')[:200] for item in items if item.strip() and len(item.strip()) > 5][:5]

    return result


def save_briefing_to_db(
    contact_id: int,
    briefing_text: str,
    health_score: int = None,
    circulo: int = None
) -> Optional[int]:
    """
    Salva briefing no banco de dados.
    Marca briefings anteriores como nao-atuais.

    Returns:
        ID do briefing criado ou None se erro
    """
    try:
        parsed = parse_briefing_sections(briefing_text)

        with get_db() as conn:
            cursor = conn.cursor()

            # Marcar briefings anteriores como nao-atuais
            cursor.execute("""
                UPDATE contact_briefings
                SET is_current = FALSE
                WHERE contact_id = %s AND is_current = TRUE
            """, (contact_id,))

            # Inserir novo briefing
            cursor.execute("""
                INSERT INTO contact_briefings (
                    contact_id, content, summary, opportunities,
                    next_steps, talking_points, health_at_generation,
                    circulo_at_generation, is_current
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id
            """, (
                contact_id,
                briefing_text,
                parsed["summary"],
                json.dumps(parsed["opportunities"]),
                json.dumps(parsed["next_steps"]),
                json.dumps(parsed["talking_points"]),
                health_score,
                circulo
            ))

            result = cursor.fetchone()
            conn.commit()
            return result['id'] if result else None

    except Exception as e:
        print(f"Erro ao salvar briefing: {e}")
        return None


def get_current_briefing(contact_id: int) -> Optional[Dict]:
    """
    Retorna o briefing atual (mais recente) de um contato.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, content, summary, opportunities, next_steps,
                   talking_points, generated_at, feedback, actions_taken,
                   health_at_generation, circulo_at_generation
            FROM contact_briefings
            WHERE contact_id = %s AND is_current = TRUE
            ORDER BY generated_at DESC
            LIMIT 1
        """, (contact_id,))

        row = cursor.fetchone()
        if row:
            briefing = dict(row)
            # Parse JSONB fields
            for field in ['opportunities', 'next_steps', 'talking_points', 'actions_taken']:
                if briefing.get(field) and isinstance(briefing[field], str):
                    try:
                        briefing[field] = json.loads(briefing[field])
                    except:
                        briefing[field] = []
            return briefing
        return None


def get_briefing_history(contact_id: int, limit: int = 5) -> List[Dict]:
    """
    Retorna historico de briefings de um contato.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, summary, generated_at, health_at_generation,
                   circulo_at_generation, feedback, is_current
            FROM contact_briefings
            WHERE contact_id = %s
            ORDER BY generated_at DESC
            LIMIT %s
        """, (contact_id, limit))

        return [dict(row) for row in cursor.fetchall()]


def add_briefing_feedback(briefing_id: int, feedback: str) -> bool:
    """
    Adiciona feedback a um briefing (util para melhorar AI).
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE contact_briefings
                SET feedback = %s
                WHERE id = %s
            """, (feedback, briefing_id))
            conn.commit()
            return True
    except:
        return False


def record_briefing_action(briefing_id: int, action: Dict) -> bool:
    """
    Registra uma acao tomada baseada no briefing.

    action = {"type": "whatsapp", "timestamp": "...", "result": "sent"}
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar acoes existentes
            cursor.execute("""
                SELECT actions_taken FROM contact_briefings WHERE id = %s
            """, (briefing_id,))
            row = cursor.fetchone()

            if row:
                actions = row['actions_taken'] or []
                if isinstance(actions, str):
                    actions = json.loads(actions)
                actions.append(action)

                cursor.execute("""
                    UPDATE contact_briefings
                    SET actions_taken = %s
                    WHERE id = %s
                """, (json.dumps(actions), briefing_id))
                conn.commit()
                return True
        return False
    except:
        return False


def get_contact_data(contact_id: int) -> Optional[Dict]:
    """
    Busca dados completos do contato para briefing.
    Inclui: dados basicos, mensagens, fatos, memorias, tasks.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Dados basicos do contato
        cursor.execute("""
            SELECT id, nome, apelido, empresa, cargo, emails, telefones,
                   linkedin, foto_url, contexto, categorias, tags,
                   aniversario, circulo, health_score, ultimo_contato,
                   total_interacoes, resumo_ai, insights_ai,
                   frequencia_ideal_dias
            FROM contacts
            WHERE id = %s
        """, (contact_id,))

        contact = cursor.fetchone()
        if not contact:
            return None

        contact = dict(contact)

        # Ultimas mensagens (WhatsApp + Email)
        cursor.execute("""
            SELECT m.conteudo, m.direcao, m.enviado_em, c.canal
            FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE m.contact_id = %s
            ORDER BY m.enviado_em DESC
            LIMIT 15
        """, (contact_id,))
        contact['ultimas_mensagens'] = [dict(r) for r in cursor.fetchall()]

        # Fatos importantes
        cursor.execute("""
            SELECT categoria, fato, fonte, confianca
            FROM contact_facts
            WHERE contact_id = %s
            ORDER BY confianca DESC, criado_em DESC
            LIMIT 15
        """, (contact_id,))
        contact['fatos'] = [dict(r) for r in cursor.fetchall()]

        # Memorias relevantes (se tabela existir)
        try:
            cursor.execute("""
                SELECT tipo, titulo, resumo, data_ocorrencia, importancia
                FROM contact_memories
                WHERE contact_id = %s
                ORDER BY importancia DESC, data_ocorrencia DESC
                LIMIT 10
            """, (contact_id,))
            contact['memorias'] = [dict(r) for r in cursor.fetchall()]
        except:
            contact['memorias'] = []

        # Tasks pendentes relacionadas (se tabela existir)
        try:
            cursor.execute("""
                SELECT titulo, descricao, data_vencimento, prioridade
                FROM tasks
                WHERE contact_id = %s AND status = 'pending'
                ORDER BY prioridade DESC, data_vencimento ASC
                LIMIT 5
            """, (contact_id,))
            contact['tasks_pendentes'] = [dict(r) for r in cursor.fetchall()]
        except:
            contact['tasks_pendentes'] = []

        return contact


def format_contact_context(contact: Dict) -> str:
    """
    Formata dados do contato para contexto do prompt AI.
    Organizado para maximizar relevancia do briefing.
    """
    parts = []

    # === INFO BASICA ===
    parts.append(f"CONTATO: {contact['nome']}")
    if contact.get('apelido'):
        parts.append(f"Apelido: {contact['apelido']}")
    if contact.get('empresa'):
        cargo = contact.get('cargo', '')
        parts.append(f"Trabalha: {cargo} @ {contact['empresa']}" if cargo else f"Empresa: {contact['empresa']}")

    # === CIRCULO E HEALTH ===
    circulo = contact.get('circulo') or 5
    health = contact.get('health_score') or 50
    config = CIRCULO_CONFIG.get(circulo, {})
    parts.append(f"\nCIRCULO: {circulo} ({config.get('nome', 'Arquivo')})")
    parts.append(f"Health Score: {health}%")

    # Frequencia ideal
    freq = contact.get('frequencia_ideal_dias') or config.get('frequencia_dias', 30)
    parts.append(f"Frequencia ideal: contato a cada {freq} dias")

    # Ultimo contato
    dias = calcular_dias_sem_contato(contact.get('ultimo_contato'))
    if dias is not None:
        status = "EM DIA" if dias <= freq else "ATRASADO"
        parts.append(f"Ultimo contato: {dias} dias atras ({status})")
    else:
        parts.append("Ultimo contato: nunca registrado")

    parts.append(f"Total interacoes: {contact.get('total_interacoes', 0)}")

    # === DATAS IMPORTANTES ===
    if contact.get('aniversario'):
        aniv = contact['aniversario']
        if isinstance(aniv, str):
            aniv = datetime.fromisoformat(aniv)
        hoje = datetime.now().date()
        try:
            aniv_este_ano = aniv.replace(year=hoje.year)
            if aniv_este_ano.date() < hoje:
                aniv_este_ano = aniv.replace(year=hoje.year + 1)
            dias_ate = (aniv_este_ano.date() - hoje).days
            if dias_ate <= 30:
                parts.append(f"\n*** ANIVERSARIO EM {dias_ate} DIAS ({aniv.strftime('%d/%m')}) ***")
            else:
                parts.append(f"Aniversario: {aniv.strftime('%d/%m')}")
        except:
            parts.append(f"Aniversario: {aniv.strftime('%d/%m') if hasattr(aniv, 'strftime') else aniv}")

    # === TAGS E CONTEXTO ===
    tags = contact.get('tags')
    if tags:
        if isinstance(tags, str):
            tags = json.loads(tags) if tags else []
        if tags:
            parts.append(f"Tags: {', '.join(tags[:10])}")

    if contact.get('contexto'):
        parts.append(f"Contexto: {contact['contexto']}")

    categorias = contact.get('categorias')
    if categorias:
        if isinstance(categorias, str):
            categorias = json.loads(categorias) if categorias else []
        if categorias:
            parts.append(f"Categorias: {', '.join(categorias)}")

    # === FATOS IMPORTANTES ===
    if contact.get('fatos'):
        parts.append("\n--- FATOS CONHECIDOS ---")
        for f in contact['fatos'][:10]:
            parts.append(f"- [{f['categoria']}] {f['fato']}")

    # === MEMORIAS / HISTORICO ===
    if contact.get('memorias'):
        parts.append("\n--- HISTORICO DE INTERACOES ---")
        for m in contact['memorias'][:5]:
            data = m['data_ocorrencia'].strftime('%d/%m/%Y') if m.get('data_ocorrencia') else '?'
            titulo = m.get('titulo') or (m.get('resumo', '')[:50] + '...')
            parts.append(f"- [{data}] {titulo}")

    # === ULTIMAS MENSAGENS ===
    if contact.get('ultimas_mensagens'):
        parts.append("\n--- ULTIMAS MENSAGENS ---")
        for msg in contact['ultimas_mensagens'][:7]:
            direcao = "ENVIADA" if msg['direcao'] in ['outbound', 'outgoing'] else "RECEBIDA"
            canal = msg.get('canal', '?').upper()
            conteudo = (msg.get('conteudo') or '')[:120]
            data = msg.get('enviado_em')
            if data:
                if isinstance(data, str):
                    data = data[:10]
                else:
                    data = data.strftime('%d/%m')
            parts.append(f"- [{data}] {direcao} via {canal}: {conteudo}...")

    # === TASKS PENDENTES ===
    if contact.get('tasks_pendentes'):
        parts.append("\n--- TASKS PENDENTES ---")
        for t in contact['tasks_pendentes']:
            venc = t.get('data_vencimento')
            if venc:
                venc = venc.strftime('%d/%m') if hasattr(venc, 'strftime') else str(venc)[:10]
            parts.append(f"- {t['titulo']} (vence: {venc or 'sem data'})")

    # === RESUMO AI EXISTENTE ===
    if contact.get('resumo_ai'):
        parts.append(f"\n--- RESUMO ANTERIOR (IA) ---\n{contact['resumo_ai'][:500]}")

    # === INSIGHTS EXISTENTES ===
    insights = contact.get('insights_ai')
    if insights:
        if isinstance(insights, str):
            try:
                insights = json.loads(insights)
            except:
                insights = {}
        if insights:
            parts.append("\n--- INSIGHTS EXISTENTES ---")
            if insights.get('potencial_negocio'):
                parts.append(f"Potencial negocio: {insights['potencial_negocio']}")
            if insights.get('forca_relacionamento'):
                parts.append(f"Forca relacionamento: {insights['forca_relacionamento']}")
            if insights.get('topicos_frequentes'):
                parts.append(f"Topicos frequentes: {', '.join(insights['topicos_frequentes'][:5])}")

    return "\n".join(parts)


async def generate_briefing(
    contact_id: int,
    contexto_reuniao: str = None,
    incluir_sugestoes: bool = True
) -> Dict:
    """
    Gera briefing inteligente para um contato usando Claude AI.

    Args:
        contact_id: ID do contato
        contexto_reuniao: Contexto adicional (ex: "Reuniao de conselho Vallen")
        incluir_sugestoes: Se deve incluir sugestoes de pauta/conversa

    Returns:
        Dict com briefing estruturado
    """
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY nao configurada", "contact_id": contact_id}

    # Buscar dados do contato
    contact = get_contact_data(contact_id)
    if not contact:
        return {"error": "Contato nao encontrado", "contact_id": contact_id}

    # Formatar contexto
    contact_context = format_contact_context(contact)

    # Construir prompt
    system_prompt = """Voce e um assistente pessoal que prepara briefings para reunioes e contatos.
Seu objetivo e ajudar Renato a se preparar para interacoes com seus contatos.

SOBRE RENATO (contexto):
- Fundador da ImensIAH (plataforma de Governanca Estrategica)
- Atua como conselheiro em diversas empresas
- Mentor de startups e scale-ups
- Investidor anjo
- Valoriza relacionamentos pessoais e profissionais

Seja conciso e pratico. Foque em informacoes acionaveis.
Use bullet points. Nao seja excessivamente formal.
Se houver oportunidades de negocio (conselhos, consultoria, investimento), destaque."""

    # Montar prompt do usuario
    sugestoes_section = """
4. **SUGESTOES DE PAUTA** (3-5 topicos para conversar baseado no historico)

5. **OPORTUNIDADES** (como Renato pode agregar valor ou fortalecer a relacao, incluindo potenciais negocios com ImensIAH)""" if incluir_sugestoes else ""

    user_prompt = f"""Prepare um briefing para minha proxima interacao com este contato:

{contact_context}

{f"CONTEXTO DA REUNIAO: {contexto_reuniao}" if contexto_reuniao else ""}

Por favor, gere um briefing PRATICO com:

1. **RESUMO** (2-3 frases sobre quem e a pessoa e nosso relacionamento)

2. **PONTOS DE ATENCAO** (o que devo lembrar, cuidados, alertas - seja especifico)

3. **HISTORICO RECENTE** (resumo das ultimas interacoes relevantes)
{sugestoes_section}

Seja direto e acionavel. Maximo 400 palavras.
Se nao houver dados suficientes em alguma secao, indique "Sem dados suficientes"."""

    # Chamar Claude API
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1500,
                    "system": system_prompt,
                    "messages": [
                        {"role": "user", "content": user_prompt}
                    ]
                }
            )

            if response.status_code != 200:
                return {
                    "error": f"API error: {response.status_code} - {response.text[:200]}",
                    "contact_id": contact_id
                }

            result = response.json()
            briefing_text = result.get("content", [{}])[0].get("text", "")

    except Exception as e:
        return {
            "error": f"Erro ao gerar briefing: {str(e)}",
            "contact_id": contact_id
        }

    # Calcular dias sem contato
    dias_sem_contato = calcular_dias_sem_contato(contact.get('ultimo_contato'))

    # Verificar aniversario proximo
    aniversario_proximo = None
    if contact.get('aniversario'):
        aniv = contact['aniversario']
        if isinstance(aniv, str):
            try:
                aniv = datetime.fromisoformat(aniv)
            except:
                aniv = None
        if aniv:
            hoje = datetime.now().date()
            try:
                aniv_este_ano = aniv.replace(year=hoje.year)
                if aniv_este_ano.date() < hoje:
                    aniv_este_ano = aniv.replace(year=hoje.year + 1)
                dias_ate = (aniv_este_ano.date() - hoje).days
                if dias_ate <= 14:
                    aniversario_proximo = dias_ate
            except:
                pass

    # Extrair dados estruturados do briefing
    parsed_sections = parse_briefing_sections(briefing_text)

    # Salvar briefing no banco de dados
    health = contact.get('health_score') or 50
    circulo = contact.get('circulo') or 5

    briefing_id = save_briefing_to_db(
        contact_id=contact_id,
        briefing_text=briefing_text,
        health_score=health,
        circulo=circulo
    )

    # Montar resposta estruturada
    return {
        "contact_id": contact_id,
        "briefing_id": briefing_id,  # ID do briefing salvo
        "nome": contact['nome'],
        "empresa": contact.get('empresa'),
        "cargo": contact.get('cargo'),
        "circulo": circulo,
        "circulo_nome": CIRCULO_CONFIG.get(circulo, {}).get('nome', 'Arquivo'),
        "health_score": health,
        "dias_sem_contato": dias_sem_contato,
        "aniversario_proximo": aniversario_proximo,
        "briefing": briefing_text,
        "gerado_em": datetime.now().isoformat(),
        "contexto_reuniao": contexto_reuniao,
        # Dados estruturados extraidos
        "summary": parsed_sections.get("summary", ""),
        "opportunities": parsed_sections.get("opportunities", []),
        "talking_points": parsed_sections.get("talking_points", []),
        "next_steps": parsed_sections.get("next_steps", []),
        "persisted": briefing_id is not None,  # Indica se foi salvo
        # Dados extras para UI
        "foto_url": contact.get('foto_url'),
        "linkedin": contact.get('linkedin'),
        "tags": contact.get('tags'),
        "total_interacoes": contact.get('total_interacoes', 0),
        "tasks_pendentes": len(contact.get('tasks_pendentes', [])),
        "tem_fatos": len(contact.get('fatos', [])) > 0,
        "tem_mensagens": len(contact.get('ultimas_mensagens', [])) > 0
    }


# ============== COS BRIEFING (Onda 1 — 06/jun/2026) ==============

def _format_cos_briefing_data(
    overdue_count: int,
    today_tasks: List[str],
    events: List[Dict],
    editorial_today: List[Dict],
    needs_metrics_count: int,
    proposals_count: int,
    agent_total_24h: int,
    no_post_alert: Optional[str],
    cost_mtd: Optional[Dict],
    pending_count: int,
    pending_top: List[str],
    now: datetime,
) -> str:
    """Formata os dados do dia em um bloco estruturado pra Claude."""
    dias_semana_pt = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]
    dia = dias_semana_pt[now.weekday()]
    date_str = now.strftime("%d/%m/%Y")

    lines = [f"DATA: {date_str} ({dia})", ""]

    lines.append("TAREFAS:")
    lines.append(f"- {overdue_count} vencidas" if overdue_count else "- 0 vencidas")
    if today_tasks:
        lines.append(f"- {len(today_tasks)} pra hoje:")
        for t in today_tasks:
            lines.append(f"  - {t}")
    else:
        lines.append("- 0 pra hoje")
    lines.append("")

    if events:
        lines.append(f"REUNIOES ({len(events)}):")
        for ev in events:
            hora = ev["start_datetime"].strftime("%H:%M") if ev.get("start_datetime") else "?"
            lines.append(f"- {hora} — {ev.get('summary') or '(sem titulo)'}")
    else:
        lines.append("REUNIOES: nenhuma agendada")
    lines.append("")

    ed_lines = []
    if no_post_alert:
        ed_lines.append(f"- ALERTA: {no_post_alert}")
    for ep in editorial_today:
        hora = ep['data_publicacao'].strftime('%Hh') if ep.get('data_publicacao') else ""
        ed_lines.append(f"- Post agendado hoje: {ep.get('article_title')} ({hora})")
    if needs_metrics_count:
        ed_lines.append(f"- Coletar metricas de {needs_metrics_count} post(s) de ~48h atras")
    if ed_lines:
        lines.append("EDITORIAL/LINKEDIN:")
        lines.extend(ed_lines)
        lines.append("")

    lines.append(f"PROPOSTAS DE ACAO PENDENTES: {proposals_count}")
    lines.append(f"ACOES AUTONOMAS NAS ULTIMAS 24H: {agent_total_24h}")
    lines.append("")

    if cost_mtd:
        lines.append(
            f"CUSTO MTD: ${cost_mtd['total_usd']:.2f} "
            f"({cost_mtd['budget_pct']:.0f}% do budget)"
        )
        lines.append("")

    if pending_count:
        lines.append(f"NOTIFICACOES PENDENTES DESDE ULTIMO DIGEST: {pending_count}")
        for p in pending_top[:8]:
            lines.append(f"- {p}")

    return "\n".join(lines).strip()


def _build_cos_briefing_system_prompt(cos_config_content: str) -> str:
    """Monta o system prompt do briefing CoS."""
    return f"""Voce e o Chief of Staff digital do Renato. Nao e assistente, nao e dashboard — e Chief of Staff. Sua funcao e garantir que cada hora do Renato seja gasta no problema mais alto da pilha.

A configuracao CoS abaixo define prioridades, politicas e mandato. USE como bussola pra ordenar e opinar — nao recite.

==== CONFIGURACAO COS (referencia) ====
{cos_config_content}
==== FIM DA CONFIGURACAO ====

REGRAS DO BRIEFING DAS 08H (mensagem WhatsApp diaria):

1. NAO e lista de bullets neutros. E narrativa opinativa em portugues brasileiro com acentos.
2. Estrutura sugerida (adapte ao dia):
   - Abertura curta: "Hoje e dia de X" — qual frente domina o dia (use os pesos v5)
   - 2 a 4 itens prioritarios com SUA leitura, ordenados pela frente que pesa mais
   - 1 linha de confronto: o que ele esta adiando, evitando, ou nao olhando (use o que sabe da CoS config — Alba sem acao, Wadhwani sem decisao, decisao SP/Japao sem progresso, treino abaixo do piso, etc.)
   - Fecho objetivo: numeros chave em 1 linha (overdue, reunioes, propostas)
3. Tom: executivo, direto, sem firula. Sem "espero que esteja bem". Trate o Renato pelo nome.
4. Formato WhatsApp: *asterisco* pra bold. SEM markdown headers (#, ##). SEM emojis decorativos (so use 1 ou 2 se realmente carregam significado — alerta, decisao). SEM hashtags.
5. Tamanho: 800 a 1500 caracteres. Nada mais.
6. Quando voce identificar acao que o CoS ja "rascunhou" (resposta, devolutiva, drafts), mencione em texto ("rascunhei a devolutiva", "preparei contexto pra Vallen"). Sem URLs falsos.
7. Quando nao houver nada relevante em alguma frente, IGNORE — nao force conteudo. Curto vale mais que cheio.
8. Se a frente "Vida pessoal" (3a familia / 3b saude) merece confronto (separacao recente, mudanca em decisao, treino), nao evite — Chief of Staff confronta com firmeza respeitosa.

Devolva APENAS o texto do briefing — sem cabecalho meta, sem aspas envolvendo, sem "aqui esta o briefing". So o texto que vai pro WhatsApp."""


async def generate_cos_briefing_narrative(
    overdue_count: int,
    today_tasks: List[str],
    events: List[Dict],
    editorial_today: List[Dict],
    needs_metrics_count: int,
    proposals_count: int,
    agent_total_24h: int,
    no_post_alert: Optional[str],
    cost_mtd: Optional[Dict],
    pending_count: int,
    pending_top: List[str],
    now: datetime,
) -> Optional[str]:
    """Gera o briefing 7h no formato Chief of Staff usando Claude.

    Retorna None se: sem API key, sem cos_config, ou Claude falhar.
    Caller deve cair pro template estatico nesse caso.
    """
    if not ANTHROPIC_API_KEY:
        return None

    from services.system_memory import get_active_cos_config

    cfg = get_active_cos_config()
    if not cfg or not cfg.get("conteudo"):
        return None

    data_block = _format_cos_briefing_data(
        overdue_count=overdue_count,
        today_tasks=today_tasks,
        events=events,
        editorial_today=editorial_today,
        needs_metrics_count=needs_metrics_count,
        proposals_count=proposals_count,
        agent_total_24h=agent_total_24h,
        no_post_alert=no_post_alert,
        cost_mtd=cost_mtd,
        pending_count=pending_count,
        pending_top=pending_top,
        now=now,
    )

    system_prompt = _build_cos_briefing_system_prompt(cfg["conteudo"])
    user_prompt = f"DADOS DE HOJE:\n\n{data_block}\n\nGere o briefing CoS pra Renato. Apenas o texto pro WhatsApp."

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1200,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
            if response.status_code != 200:
                return None
            result = response.json()
            text = result.get("content", [{}])[0].get("text", "").strip()
            if not text:
                return None
            # WhatsApp seguro: limite duro 1600 chars (margem do limite de 4096)
            if len(text) > 1600:
                text = text[:1597] + "..."
            return text
    except Exception:
        return None


def get_contacts_needing_briefing(limit: int = 10) -> List[Dict]:
    """
    Retorna contatos que precisam de atencao e se beneficiariam de briefing.

    Criterios (em ordem de prioridade):
    1. Circulo 1-3 com health < 50 (relacionamento esfriando)
    2. Aniversario nos proximos 7 dias
    3. Task pendente vencendo em breve
    4. Circulo 1-2 sem contato recente
    """
    with get_db() as conn:
        cursor = conn.cursor()
        results = []
        seen_ids = set()

        # 1. Contatos dos circulos proximos precisando atencao (health baixo)
        cursor.execute("""
            SELECT id, nome, empresa, cargo, circulo, health_score,
                   ultimo_contato, foto_url
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 3
              AND COALESCE(health_score, 50) < 50
              AND ultimo_contato IS NOT NULL
            ORDER BY circulo ASC, health_score ASC
            LIMIT %s
        """, (limit,))

        for row in cursor.fetchall():
            contact = dict(row)
            contact['razao'] = f"Health {contact['health_score']}% - precisa contato"
            contact['prioridade'] = 'alta'
            contact['tipo_alerta'] = 'health'
            if contact['id'] not in seen_ids:
                seen_ids.add(contact['id'])
                results.append(contact)

        # 2. Aniversarios proximos (7 dias)
        cursor.execute("""
            SELECT id, nome, empresa, cargo, circulo, health_score,
                   aniversario, foto_url
            FROM contacts
            WHERE aniversario IS NOT NULL
              AND COALESCE(circulo, 5) <= 4
        """)

        hoje = datetime.now().date()
        for row in cursor.fetchall():
            if row['id'] in seen_ids:
                continue
            contact = dict(row)
            aniv = contact['aniversario']
            try:
                if isinstance(aniv, str):
                    aniv = datetime.fromisoformat(aniv).date()
                elif hasattr(aniv, 'date'):
                    aniv = aniv.date() if callable(getattr(aniv, 'date')) else aniv
                aniv_este_ano = aniv.replace(year=hoje.year)
                if aniv_este_ano < hoje:
                    aniv_este_ano = aniv.replace(year=hoje.year + 1)
                dias_ate = (aniv_este_ano - hoje).days
                if 0 <= dias_ate <= 7:
                    contact['razao'] = f"Aniversario em {dias_ate} dias!"
                    contact['prioridade'] = 'alta' if dias_ate <= 3 else 'media'
                    contact['tipo_alerta'] = 'aniversario'
                    contact['dias_ate_aniversario'] = dias_ate
                    seen_ids.add(contact['id'])
                    results.append(contact)
            except Exception:
                continue

        # 3. Circulo 1-2 sem contato ha mais de 7 dias (mesmo com health ok)
        cursor.execute("""
            SELECT id, nome, empresa, cargo, circulo, health_score,
                   ultimo_contato, foto_url
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 2
              AND ultimo_contato < NOW() - INTERVAL '7 days'
            ORDER BY circulo ASC, ultimo_contato ASC
            LIMIT %s
        """, (limit,))

        for row in cursor.fetchall():
            if row['id'] in seen_ids:
                continue
            contact = dict(row)
            dias = calcular_dias_sem_contato(contact.get('ultimo_contato'))
            contact['razao'] = f"Circulo {contact['circulo']} sem contato ha {dias} dias"
            contact['prioridade'] = 'media'
            contact['tipo_alerta'] = 'recencia'
            seen_ids.add(contact['id'])
            results.append(contact)

        # Ordenar por prioridade
        prioridade_ordem = {'alta': 0, 'media': 1, 'baixa': 2}
        results.sort(key=lambda x: (prioridade_ordem.get(x.get('prioridade', 'baixa'), 2), x.get('circulo', 5)))

        return results[:limit]


def get_briefing_summary(contact_id: int) -> Dict:
    """
    Retorna um resumo rapido do contato sem chamar a IA.
    Util para previews ou quando nao precisa de briefing completo.
    """
    contact = get_contact_data(contact_id)
    if not contact:
        return {"error": "Contato nao encontrado"}

    dias = calcular_dias_sem_contato(contact.get('ultimo_contato'))
    circulo = contact.get('circulo') or 5

    # Verificar alertas
    alertas = []

    # Health baixo
    health = contact.get('health_score') or 50
    if health < 50:
        alertas.append(f"Health baixo ({health}%)")

    # Aniversario proximo
    if contact.get('aniversario'):
        aniv = contact['aniversario']
        hoje = datetime.now().date()
        try:
            if isinstance(aniv, str):
                aniv = datetime.fromisoformat(aniv)
            aniv_este_ano = aniv.replace(year=hoje.year)
            if aniv_este_ano.date() < hoje:
                aniv_este_ano = aniv.replace(year=hoje.year + 1)
            dias_ate = (aniv_este_ano.date() - hoje).days
            if dias_ate <= 14:
                alertas.append(f"Aniversario em {dias_ate} dias")
        except:
            pass

    # Tasks pendentes
    if contact.get('tasks_pendentes'):
        alertas.append(f"{len(contact['tasks_pendentes'])} task(s) pendente(s)")

    return {
        "contact_id": contact_id,
        "nome": contact['nome'],
        "empresa": contact.get('empresa'),
        "cargo": contact.get('cargo'),
        "circulo": circulo,
        "circulo_nome": CIRCULO_CONFIG.get(circulo, {}).get('nome', 'Arquivo'),
        "health_score": health,
        "dias_sem_contato": dias,
        "foto_url": contact.get('foto_url'),
        "alertas": alertas,
        "tem_resumo_ai": bool(contact.get('resumo_ai')),
        "total_fatos": len(contact.get('fatos', [])),
        "total_mensagens": len(contact.get('ultimas_mensagens', []))
    }
