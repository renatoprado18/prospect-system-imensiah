"""
Project Smart Update Service - Analisa emails/WhatsApp para sugerir atualizacoes de tarefas

Cruza mensagens recentes dos membros do projeto com tarefas pendentes
e usa IA para identificar quais tarefas podem ser marcadas como concluidas.
"""
import os
import json
import logging
from datetime import datetime, date
from typing import Dict, List, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def analyze_project_updates(project_id: int) -> Dict:
    """
    Analisa mensagens recentes dos membros do projeto e sugere
    atualizacoes de tarefas (completar, criar novas).

    Returns:
        {suggestions: [...], new_tasks_suggested: [...], summary: str}
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY nao configurada"}

    # 1. Buscar contexto do projeto
    with get_db() as conn:
        cursor = conn.cursor()

        # Projeto
        cursor.execute("SELECT id, nome, descricao FROM projects WHERE id = %s", (project_id,))
        project = cursor.fetchone()
        if not project:
            return {"error": "Projeto nao encontrado"}
        project = dict(project)

        # Tarefas pendentes (COM id)
        cursor.execute("""
            SELECT t.id, t.titulo, t.descricao, t.status, t.data_vencimento, t.prioridade,
                   c.nome as responsavel
            FROM tasks t
            LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.project_id = %s AND t.status != 'completed'
            ORDER BY t.data_vencimento NULLS LAST
        """, (project_id,))
        pending_tasks = [dict(r) for r in cursor.fetchall()]

        if not pending_tasks:
            return {"suggestions": [], "new_tasks_suggested": [], "summary": "Nenhuma tarefa pendente neste projeto."}

        # Membros
        cursor.execute("""
            SELECT pm.contact_id, c.nome, pm.papel
            FROM project_members pm
            JOIN contacts c ON c.id = pm.contact_id
            WHERE pm.project_id = %s
        """, (project_id,))
        members = [dict(r) for r in cursor.fetchall()]
        member_ids = [m['contact_id'] for m in members]

        # Mensagens recentes dos membros (ultimos 30 dias, mais mensagens para analise)
        recent_messages = []
        if member_ids:
            cursor.execute("""
                SELECT m.conteudo, m.direcao, m.enviado_em, m.recebido_em,
                       cv.canal, c.nome as contact_nome
                FROM messages m
                JOIN conversations cv ON cv.id = m.conversation_id
                JOIN contacts c ON c.id = cv.contact_id
                WHERE cv.contact_id = ANY(%s)
                  AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - INTERVAL '30 days'
                  AND m.conteudo IS NOT NULL
                  AND LENGTH(m.conteudo) > 10
                ORDER BY COALESCE(m.enviado_em, m.recebido_em) DESC
                LIMIT 30
            """, (member_ids,))
            recent_messages = [dict(r) for r in cursor.fetchall()]

    if not recent_messages:
        return {
            "suggestions": [],
            "new_tasks_suggested": [],
            "summary": "Nenhuma mensagem recente encontrada dos participantes do projeto."
        }

    # 2. Montar prompt
    tasks_text = "\n".join([
        f"- [ID:{t['id']}] {t['titulo']}"
        f"{' (responsavel: ' + t['responsavel'] + ')' if t.get('responsavel') else ''}"
        f"{' - vence: ' + str(t['data_vencimento']) if t.get('data_vencimento') else ''}"
        f"{' - ATRASADA' if t.get('data_vencimento') and str(t['data_vencimento'])[:10] < str(date.today()) else ''}"
        f"{' | ' + t['descricao'][:100] if t.get('descricao') else ''}"
        for t in pending_tasks
    ])

    messages_text = "\n".join([
        f"[{m.get('canal','?')}] {m['contact_nome']} ({m['direcao']}) "
        f"em {str(m.get('enviado_em') or m.get('recebido_em') or '?')[:10]}: "
        f"{(m.get('conteudo') or '')[:500]}"
        for m in recent_messages[:20]
    ])

    members_text = ", ".join([f"{m['nome']} ({m.get('papel', 'membro')})" for m in members])

    prompt = f"""Analise as mensagens recentes dos participantes deste projeto e identifique quais tarefas pendentes podem ser marcadas como concluidas.

PROJETO: {project['nome']}
DESCRICAO: {project.get('descricao', '')[:300]}
PARTICIPANTES: {members_text}

TAREFAS PENDENTES:
{tasks_text}

MENSAGENS RECENTES (emails e WhatsApp dos participantes):
{messages_text}

INSTRUCOES:
1. Para cada tarefa pendente, verifique se alguma mensagem indica que a tarefa foi concluida (ex: "enviado", "feito", "pronto", "segue em anexo", comprovantes, etc)
2. Atribua um nivel de confianca (0.0 a 1.0)
3. Sugira novas tarefas se necessario (ex: follow-up, proximos passos)
4. Hoje e {date.today().isoformat()}

Retorne APENAS JSON valido (sem markdown):
{{
  "suggestions": [
    {{
      "task_id": 123,
      "task_titulo": "titulo da tarefa",
      "action": "complete",
      "confidence": 0.9,
      "reasoning": "Explicacao curta de por que a tarefa pode ser concluida",
      "evidence_snippet": "Trecho da mensagem que evidencia a conclusao",
      "evidence_date": "YYYY-MM-DD",
      "evidence_from": "Nome da pessoa"
    }}
  ],
  "new_tasks_suggested": [
    {{
      "titulo": "Nova tarefa sugerida",
      "responsavel": "Nome",
      "reasoning": "Por que esta tarefa deveria ser criada"
    }}
  ],
  "summary": "Resumo em 1-2 frases do que foi encontrado"
}}

Se nenhuma tarefa pode ser concluida, retorne suggestions como lista vazia.
Seja conservador: so sugira completar se ha evidencia clara nas mensagens."""

    # 3. Chamar Claude
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

        if response.status_code != 200:
            logger.error(f"Claude API error: {response.status_code} - {response.text[:300]}")
            return {"error": f"Erro na API: {response.status_code}", "detail": response.text[:200]}

        result = response.json()
        text = result.get("content", [{}])[0].get("text", "")

        # Extrair JSON
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end])
            # Validar que task_ids existem
            valid_ids = {t['id'] for t in pending_tasks}
            parsed['suggestions'] = [
                s for s in parsed.get('suggestions', [])
                if s.get('task_id') in valid_ids
            ]
            return parsed

        return {"error": "Nao foi possivel interpretar resposta da IA", "raw": text[:200]}

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return {"error": "Erro ao interpretar resposta da IA"}
    except Exception as e:
        logger.error(f"Smart update error: {e}")
        return {"error": str(e)}


async def apply_smart_updates(project_id: int, task_ids: List[int] = None,
                               new_tasks: List[Dict] = None) -> Dict:
    """
    Aplica as sugestoes: marca tarefas como concluidas e cria novas.
    """
    results = {"completed": 0, "created": 0, "errors": []}

    with get_db() as conn:
        cursor = conn.cursor()

        # Completar tarefas
        for task_id in (task_ids or []):
            try:
                cursor.execute(
                    "SELECT id FROM tasks WHERE id = %s AND project_id = %s AND status != 'completed'",
                    (task_id, project_id)
                )
                if not cursor.fetchone():
                    results["errors"].append(f"Tarefa {task_id} nao encontrada ou ja concluida")
                    continue

                cursor.execute("""
                    UPDATE tasks SET status = 'completed', data_conclusao = NOW()
                    WHERE id = %s
                """, (task_id,))
                results["completed"] += 1
            except Exception as e:
                results["errors"].append(f"Erro tarefa {task_id}: {str(e)}")

        # Criar novas tarefas
        for task in (new_tasks or []):
            try:
                titulo = task.get('titulo', '').strip()
                if not titulo:
                    continue

                # Buscar contact_id do responsavel se fornecido
                contact_id = None
                responsavel = task.get('responsavel', '')
                if responsavel:
                    cursor.execute(
                        "SELECT id FROM contacts WHERE nome ILIKE %s LIMIT 1",
                        (f"%{responsavel}%",)
                    )
                    row = cursor.fetchone()
                    if row:
                        contact_id = row['id']

                cursor.execute("""
                    INSERT INTO tasks (project_id, titulo, status, contact_id, prioridade)
                    VALUES (%s, %s, 'pending', %s, 5)
                """, (project_id, titulo, contact_id))
                results["created"] += 1
            except Exception as e:
                results["errors"].append(f"Erro ao criar '{task.get('titulo', '?')}': {str(e)}")

        conn.commit()

    return results
