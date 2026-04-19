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

        # Mensagens recentes dos membros (ultimos 30 dias)
        # Agrupadas por conversa para manter contexto de threads
        recent_messages = []
        if member_ids:
            cursor.execute("""
                SELECT m.conteudo, m.direcao, m.enviado_em, m.recebido_em,
                       cv.canal, cv.id as conv_id, c.nome as contact_nome
                FROM messages m
                JOIN conversations cv ON cv.id = m.conversation_id
                JOIN contacts c ON c.id = cv.contact_id
                WHERE cv.contact_id = ANY(%s)
                  AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - INTERVAL '30 days'
                  AND m.conteudo IS NOT NULL
                  AND LENGTH(m.conteudo) > 10
                ORDER BY cv.contact_id, COALESCE(m.enviado_em, m.recebido_em) ASC
                LIMIT 40
            """, (member_ids,))
            recent_messages = [dict(r) for r in cursor.fetchall()]

        # Pareceres anteriores (memoria persistente para Smart Update)
        cursor.execute("""
            SELECT titulo, conteudo, criado_em FROM project_notes
            WHERE project_id = %s AND tipo = 'parecer'
            ORDER BY criado_em DESC LIMIT 2
        """, (project_id,))
        prev_analyses = [dict(r) for r in cursor.fetchall()]

        # Mensagens dos grupos de WhatsApp vinculados ao projeto
        group_messages = []
        cursor.execute("""
            SELECT group_jid, group_name FROM project_whatsapp_groups
            WHERE project_id = %s AND ativo = TRUE
        """, (project_id,))
        linked_groups = [dict(r) for r in cursor.fetchall()]

    # Buscar mensagens dos grupos via Evolution API (sync on-demand)
    if linked_groups:
        try:
            group_messages = await _fetch_group_messages(linked_groups)
        except Exception as e:
            logger.warning(f"Erro ao buscar msgs dos grupos: {e}")

    if not recent_messages and not group_messages:
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

    # Agrupar mensagens por contato para manter contexto de conversa
    from collections import defaultdict
    convos = defaultdict(list)
    for m in recent_messages:
        convos[m['contact_nome']].append(m)

    messages_text = ""
    for contact_nome, msgs in convos.items():
        messages_text += f"\n--- Conversa com {contact_nome} ---\n"
        for m in msgs[-15:]:
            sender = "RENATO" if m['direcao'] == 'outgoing' else contact_nome
            dt = str(m.get('enviado_em') or m.get('recebido_em') or '?')[:16]
            messages_text += f"[{dt}] {sender}: {(m.get('conteudo') or '')[:500]}\n"

    # Adicionar mensagens dos grupos de WhatsApp
    if group_messages:
        for gm in group_messages:
            messages_text += f"\n--- Grupo WhatsApp: {gm['group_name']} ---\n"
            for m in gm['messages'][-15:]:
                dt = str(m.get('timestamp', '?'))[:16]
                sender = m.get('sender_name', m.get('sender', '?'))
                content = m.get('content', '')[:500]
                doc_info = f" [DOCUMENTO: {m.get('doc_name', '')}]" if m.get('has_document') else ""
                messages_text += f"[{dt}] {sender}: {content}{doc_info}\n"

    members_text = ", ".join([f"{m['nome']} ({m.get('papel', 'membro')})" for m in members])

    # Memoria de pareceres anteriores
    memory_text = ""
    if prev_analyses:
        memory_text = "\n\nPARECERES ANTERIORES (memoria do projeto):\n"
        for pa in reversed(prev_analyses):
            dt = str(pa.get('criado_em', '?'))[:10]
            memory_text += f"--- Parecer de {dt} ---\n{(pa.get('conteudo') or '')[:600]}\n\n"
        memory_text += "INSTRUCAO: Considere os pareceres anteriores. Nao repita sugestoes ja feitas. Foque no que MUDOU.\n"

    prompt = f"""Analise as conversas recentes entre Renato e os participantes deste projeto.
O usuario do sistema e RENATO. Quando ele envia uma mensagem, ele e o remetente.

PROJETO SENDO ANALISADO: {project['nome']}
DESCRICAO: {project.get('descricao', '')[:300]}{memory_text}
PARTICIPANTES: {members_text}

ATENCAO: As conversas podem abordar MULTIPLOS assuntos/projetos. Considere APENAS o que e relevante para o projeto "{project['nome']}". Ignore partes das conversas sobre outros projetos ou assuntos pessoais.

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
      "reasoning": "Por que esta tarefa deveria ser criada",
      "data_vencimento": "YYYY-MM-DD ou null",
      "prioridade": 5
    }}
  ],
  "summary": "Resumo em 1-2 frases do que foi encontrado"
}}

IMPORTANTE:
- Se nenhuma tarefa pode ser concluida, retorne suggestions como lista vazia.
- Seja MUITO conservador ao sugerir completar tarefas:
  - "Entrei em contato com X para agendar" = tarefa INICIADA, NAO concluida. Agendar so esta concluido quando data/horario estao confirmados.
  - "Vou providenciar" = tarefa INICIADA, nao concluida.
  - "Enviado", "Feito", "Pronto", "Segue em anexo" = tarefa CONCLUIDA.
  - Diferencie entre ACAO INICIADA (confidence < 0.5, nao sugerir completar) e ACAO CONCLUIDA (confidence > 0.8).
- Preste atencao na DIRECAO das mensagens: "outgoing" = Renato enviou, "incoming" = contato respondeu.
  - Se Renato disse "vou fazer X" = ele iniciou, nao significa que X esta feito.
  - Se contato disse "esta feito" ou "segue em anexo" = evidencia real de conclusao.
- Quando alguem mencionar datas (ex: "primeira semana de maio", "proxima terca"), converta para data YYYY-MM-DD.
- Use EXATAMENTE as datas mencionadas nas mensagens, nao aproxime (ex: "primeira semana de maio" = 2026-05-05, NAO "proxima semana").
- Inclua data_vencimento nas novas tarefas quando a mensagem mencionar prazo ou data."""

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


async def _fetch_group_messages(linked_groups: List[Dict], limit_per_group: int = 20) -> List[Dict]:
    """
    Busca mensagens recentes dos grupos de WhatsApp vinculados ao projeto
    via Evolution API.
    """
    import os
    import httpx as hx
    from integrations.whatsapp import WhatsAppIntegration
    wa = WhatsAppIntegration()

    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    results = []
    for group in linked_groups:
        jid = group['group_jid']
        name = group.get('group_name', jid)

        try:
            # Buscar mensagens do grupo direto pela API (JID ja e @g.us)
            async with hx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/findMessages/{instance}",
                    headers={'apikey': api_key, 'Content-Type': 'application/json'},
                    json={"where": {"key": {"remoteJid": jid}}, "limit": limit_per_group}
                )
                resp_data = resp.json() if resp.status_code == 200 else {}
                # Evolution API v2: {messages: {total, pages, records: [...]}}
                msgs_container = resp_data.get('messages', resp_data)
                if isinstance(msgs_container, dict):
                    raw_msgs = msgs_container.get('records', [])
                elif isinstance(msgs_container, list):
                    raw_msgs = msgs_container
                else:
                    raw_msgs = []
            if not raw_msgs:
                continue

            parsed = []
            for m in raw_msgs:
                msg_data = m.get('message') or {}
                key = m.get('key') or {}
                msg_type = m.get('messageType', '')

                # Ignorar reacoes, stickers, etc
                if msg_type in ('reactionMessage', 'stickerMessage', 'protocolMessage'):
                    continue

                # Extrair conteudo
                content = ''
                has_document = False
                doc_name = ''

                if 'conversation' in msg_data:
                    content = msg_data['conversation']
                elif 'extendedTextMessage' in msg_data:
                    content = msg_data['extendedTextMessage'].get('text', '')
                elif 'documentMessage' in msg_data:
                    doc_name = msg_data['documentMessage'].get('fileName', 'documento')
                    content = msg_data['documentMessage'].get('caption', f'[Documento: {doc_name}]')
                    has_document = True
                elif 'imageMessage' in msg_data:
                    content = msg_data['imageMessage'].get('caption', '[Imagem]')
                    has_document = True
                    doc_name = 'imagem'

                if not content or len(content) < 3:
                    continue

                # Sender
                sender = key.get('participant', key.get('participantAlt', '')).replace('@s.whatsapp.net', '').replace('@lid', '')
                sender_name = m.get('pushName', sender)
                if key.get('fromMe'):
                    sender_name = 'RENATO'

                # Timestamp
                timestamp = m.get('messageTimestamp', m.get('updatedAt', ''))
                if isinstance(timestamp, (int, float)):
                    try:
                        from datetime import datetime as dt_cls
                        timestamp = dt_cls.fromtimestamp(int(timestamp)).isoformat()
                    except Exception:
                        pass

                parsed.append({
                    'content': content,
                    'sender': sender,
                    'sender_name': sender_name,
                    'timestamp': timestamp,
                    'has_document': has_document,
                    'doc_name': doc_name
                })

            if parsed:
                results.append({
                    'group_name': name,
                    'group_jid': jid,
                    'messages': parsed
                })

        except Exception as e:
            logger.warning(f"Erro ao buscar msgs do grupo {name}: {e}")

    return results


async def download_group_documents(project_id: int) -> Dict:
    """
    Baixa documentos dos grupos WhatsApp vinculados ao projeto
    e salva no Google Drive (pasta do projeto).
    """
    import base64 as b64
    import httpx as hx

    evo_url = os.getenv('EVOLUTION_API_URL', '')
    evo_key = os.getenv('EVOLUTION_API_KEY', '')
    evo_instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not evo_url:
        return {"error": "Evolution API nao configurada"}

    results = {"downloaded": 0, "uploaded": 0, "errors": [], "files": []}

    with get_db() as conn:
        cursor = conn.cursor()

        # Grupos vinculados
        cursor.execute("""
            SELECT group_jid, group_name FROM project_whatsapp_groups
            WHERE project_id = %s AND ativo = TRUE
        """, (project_id,))
        groups = [dict(r) for r in cursor.fetchall()]

        if not groups:
            return {"error": "Nenhum grupo vinculado"}

        # Documentos ja indexados (evitar duplicados)
        cursor.execute("""
            SELECT d.nome FROM documento_links dl
            JOIN documentos d ON d.id = dl.documento_id
            WHERE dl.entidade_tipo = 'projeto' AND dl.entidade_id = %s
        """, (project_id,))
        existing_docs = {r['nome'] for r in cursor.fetchall()}

    for group in groups:
        jid = group['group_jid']
        try:
            async with hx.AsyncClient(timeout=60.0) as client:
                # Buscar mensagens com documentos
                resp = await client.post(
                    f"{evo_url}/chat/findMessages/{evo_instance}",
                    headers={'apikey': evo_key, 'Content-Type': 'application/json'},
                    json={"where": {"key": {"remoteJid": jid}}, "limit": 50}
                )
                msgs_data = resp.json().get('messages', {})
                records = msgs_data.get('records', []) if isinstance(msgs_data, dict) else []

                for r in records:
                    msg = r.get('message') or {}
                    key = r.get('key') or {}

                    # Detectar documentos e imagens com caption
                    doc_msg = msg.get('documentMessage') or msg.get('imageMessage')
                    if not doc_msg:
                        continue

                    file_name = doc_msg.get('fileName', doc_msg.get('caption', 'documento'))
                    mimetype = doc_msg.get('mimetype', 'application/octet-stream')

                    # Pular se ja existe
                    if file_name in existing_docs:
                        continue

                    # Baixar via Evolution API
                    try:
                        dl_resp = await client.post(
                            f"{evo_url}/chat/getBase64FromMediaMessage/{evo_instance}",
                            headers={'apikey': evo_key, 'Content-Type': 'application/json'},
                            json={"message": {"key": key}, "convertToMp4": False},
                            timeout=30.0
                        )
                        if dl_resp.status_code not in (200, 201):
                            continue

                        dl_data = dl_resp.json()
                        file_b64 = dl_data.get('base64', '')
                        if not file_b64:
                            continue

                        file_bytes = b64.b64decode(file_b64)
                        results["downloaded"] += 1

                        # Upload para Google Drive
                        try:
                            from integrations.google_drive import upload_file, get_valid_token, create_folder

                            with get_db() as conn2:
                                access_token = await get_valid_token(conn2, 'professional')

                            if access_token:
                                # Buscar/criar pasta do projeto no Drive
                                with get_db() as conn2:
                                    cursor2 = conn2.cursor()
                                    cursor2.execute("SELECT google_drive_folder_id FROM projects WHERE id = %s", (project_id,))
                                    proj = cursor2.fetchone()
                                    folder_id = proj['google_drive_folder_id'] if proj else None

                                # Tentar upload na pasta do projeto, fallback para raiz
                                try:
                                    upload_result = await upload_file(access_token, file_bytes, file_name, mimetype, folder_id)
                                except Exception:
                                    # Pasta nao existe, upload na raiz
                                    upload_result = await upload_file(access_token, file_bytes, file_name, mimetype, None)
                                drive_id = upload_result.get('id', '')
                                drive_url = f"https://drive.google.com/file/d/{drive_id}/view"

                                # Indexar no sistema
                                from integrations.google_drive import index_document_to_db, link_document_to_entity
                                with get_db() as conn2:
                                    doc_id = index_document_to_db(
                                        conn2, file_name, drive_id, drive_url,
                                        mimetype, len(file_bytes), folder_id
                                    )
                                    if doc_id:
                                        link_document_to_entity(conn2, doc_id, 'projeto', project_id)

                                results["uploaded"] += 1
                                results["files"].append({"name": file_name, "drive_url": drive_url})
                                existing_docs.add(file_name)

                        except Exception as e:
                            results["errors"].append(f"Upload {file_name}: {str(e)}")

                    except Exception as e:
                        results["errors"].append(f"Download {file_name}: {str(e)}")

        except Exception as e:
            results["errors"].append(f"Grupo {group['group_name']}: {str(e)}")

    return results


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

                data_vencimento = task.get('data_vencimento')
                prioridade = task.get('prioridade', 5)
                cursor.execute("""
                    INSERT INTO tasks (project_id, titulo, status, contact_id, prioridade, data_vencimento)
                    VALUES (%s, %s, 'pending', %s, %s, %s)
                """, (project_id, titulo, contact_id, prioridade, data_vencimento))
                results["created"] += 1
            except Exception as e:
                results["errors"].append(f"Erro ao criar '{task.get('titulo', '?')}': {str(e)}")

        conn.commit()

    return results


async def generate_project_analysis(project_id: int, custom_prompt: str = None) -> Dict:
    """
    Gera um parecer/análise IA sobre o projeto, cruzando:
    - Descrição e tarefas do projeto
    - Mensagens dos membros (email/WhatsApp)
    - Mensagens dos grupos de WhatsApp vinculados
    - Documentos disponíveis

    Args:
        project_id: ID do projeto
        custom_prompt: Prompt customizado (opcional, ex: "analise os riscos jurídicos")

    Returns:
        {analysis: str, sources: [...], saved_note_id: int}
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY nao configurada"}

    with get_db() as conn:
        cursor = conn.cursor()

        # Projeto
        cursor.execute("SELECT id, nome, descricao, tipo, status FROM projects WHERE id = %s", (project_id,))
        project = cursor.fetchone()
        if not project:
            return {"error": "Projeto nao encontrado"}
        project = dict(project)

        # Tarefas
        cursor.execute("""
            SELECT t.titulo, t.status, t.data_vencimento, c.nome as responsavel
            FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.project_id = %s ORDER BY t.data_vencimento NULLS LAST
        """, (project_id,))
        tasks = [dict(r) for r in cursor.fetchall()]

        # Membros
        cursor.execute("""
            SELECT c.nome, pm.papel FROM project_members pm
            JOIN contacts c ON c.id = pm.contact_id WHERE pm.project_id = %s
        """, (project_id,))
        members = [dict(r) for r in cursor.fetchall()]
        member_ids = [m['contact_id'] for m in cursor.execute("""
            SELECT contact_id FROM project_members WHERE project_id = %s
        """, (project_id,)) or []]

        # Re-query member IDs properly
        cursor.execute("SELECT contact_id FROM project_members WHERE project_id = %s", (project_id,))
        member_ids = [r['contact_id'] for r in cursor.fetchall()]

        # Mensagens individuais
        individual_msgs = []
        if member_ids:
            cursor.execute("""
                SELECT m.conteudo, m.direcao, m.enviado_em, cv.canal, c.nome as contact_nome
                FROM messages m
                JOIN conversations cv ON cv.id = m.conversation_id
                JOIN contacts c ON c.id = cv.contact_id
                WHERE cv.contact_id = ANY(%s)
                  AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - INTERVAL '30 days'
                  AND m.conteudo IS NOT NULL AND LENGTH(m.conteudo) > 10
                ORDER BY COALESCE(m.enviado_em, m.recebido_em) DESC LIMIT 20
            """, (member_ids,))
            individual_msgs = [dict(r) for r in cursor.fetchall()]

        # Grupos WhatsApp vinculados
        cursor.execute("""
            SELECT group_jid, group_name FROM project_whatsapp_groups
            WHERE project_id = %s AND ativo = TRUE
        """, (project_id,))
        linked_groups = [dict(r) for r in cursor.fetchall()]

        # Documentos
        cursor.execute("""
            SELECT d.nome, d.google_drive_url FROM documento_links dl
            JOIN documentos d ON d.id = dl.documento_id
            WHERE dl.entidade_tipo = 'projeto' AND dl.entidade_id = %s
        """, (project_id,))
        docs = [dict(r) for r in cursor.fetchall()]

        # Pareceres anteriores (memoria persistente)
        cursor.execute("""
            SELECT titulo, conteudo, criado_em FROM project_notes
            WHERE project_id = %s AND tipo = 'parecer'
            ORDER BY criado_em DESC LIMIT 3
        """, (project_id,))
        previous_analyses = [dict(r) for r in cursor.fetchall()]

        # Notas regulares do projeto
        cursor.execute("""
            SELECT titulo, conteudo, tipo, criado_em FROM project_notes
            WHERE project_id = %s AND (tipo != 'parecer' OR tipo IS NULL)
            ORDER BY criado_em DESC LIMIT 5
        """, (project_id,))
        notes = [dict(r) for r in cursor.fetchall()]

    # Buscar mensagens dos grupos
    group_msgs = []
    if linked_groups:
        try:
            group_msgs = await _fetch_group_messages(linked_groups, limit_per_group=30)
        except Exception as e:
            logger.warning(f"Erro ao buscar msgs grupos: {e}")

    # Montar contexto
    context_parts = []

    context_parts.append(f"PROJETO: {project['nome']}")
    context_parts.append(f"TIPO: {project.get('tipo', 'negocio')}")
    context_parts.append(f"DESCRICAO: {project.get('descricao', '')[:500]}")
    context_parts.append(f"HOJE: {date.today().isoformat()}")

    if members:
        context_parts.append(f"\nPARTICIPANTES: {', '.join(m['nome'] + ' (' + (m.get('papel') or '') + ')' for m in members)}")

    if tasks:
        context_parts.append("\nTAREFAS:")
        for t in tasks:
            status = 'concluida' if t['status'] == 'completed' else 'pendente'
            context_parts.append(f"- [{status}] {t['titulo']} | resp: {t.get('responsavel', '-')} | prazo: {t.get('data_vencimento', '-')}")

    if docs:
        context_parts.append(f"\nDOCUMENTOS DISPONIVEIS ({len(docs)}):")
        for d in docs:
            context_parts.append(f"- {d['nome']}")

    if individual_msgs:
        context_parts.append("\nMENSAGENS RECENTES DOS MEMBROS:")
        for m in individual_msgs[:10]:
            sender = "RENATO" if m['direcao'] == 'outgoing' else m['contact_nome']
            dt = str(m.get('enviado_em', '?'))[:10]
            context_parts.append(f"[{dt}] {sender}: {(m.get('conteudo') or '')[:400]}")

    if group_msgs:
        for gm in group_msgs:
            context_parts.append(f"\nGRUPO WHATSAPP: {gm['group_name']}")
            for m in gm['messages'][-20:]:
                dt = str(m.get('timestamp', '?'))[:10]
                sender = m.get('sender_name', '?')
                doc_tag = f" [DOC: {m['doc_name']}]" if m.get('has_document') else ""
                context_parts.append(f"[{dt}] {sender}: {m.get('content', '')[:400]}{doc_tag}")

    # Adicionar pareceres anteriores como memoria
    if previous_analyses:
        context_parts.append(f"\n{'='*60}")
        context_parts.append(f"MEMORIA: {len(previous_analyses)} PARECER(ES) ANTERIOR(ES)")
        context_parts.append(f"{'='*60}")
        for pa in reversed(previous_analyses):  # Mais antigo primeiro
            dt = str(pa.get('criado_em', '?'))[:10]
            context_parts.append(f"\n--- Parecer de {dt}: {pa.get('titulo', '')} ---")
            # Incluir resumo (primeiros 800 chars) para nao estourar contexto
            content = (pa.get('conteudo') or '')[:800]
            context_parts.append(content)
            if len(pa.get('conteudo', '')) > 800:
                context_parts.append("(...)")

    full_context = "\n".join(context_parts)

    # Prompt
    if custom_prompt:
        user_instruction = custom_prompt
    else:
        user_instruction = """Gere um PARECER completo e PRÁTICO sobre este projeto. Inclua:

1. **SITUAÇÃO ATUAL**: Resumo do estado do projeto (2-3 frases)
2. **PRAZOS CRÍTICOS**: Datas e deadlines identificados nas mensagens/tarefas
3. **AÇÕES NECESSÁRIAS**: Lista priorizada do que precisa ser feito, por quem, e até quando
4. **RISCOS**: O que pode dar errado se não agir
5. **OPORTUNIDADES**: Como maximizar resultados
6. **PRÓXIMOS PASSOS IMEDIATOS**: 3 ações concretas para esta semana

Seja direto, prático, em português. Use linguagem acessível.
Baseie-se nos FATOS das mensagens e documentos, não em suposições."""

    memory_instruction = ""
    if previous_analyses:
        memory_instruction = f"""
## MEMORIA (PARECERES ANTERIORES)
Você tem acesso a {len(previous_analyses)} parecer(es) anterior(es) deste projeto.
REGRAS sobre a memória:
- NAO repita análises já feitas. Foque no que MUDOU desde o último parecer.
- Referencie conclusões anteriores quando relevante (ex: "Conforme identificado anteriormente...")
- Se uma ação sugerida no parecer anterior foi executada, reconheça.
- Se um risco alertado se concretizou, destaque.
- EVOLUA a análise: aprofunde, atualize, identifique tendências.
"""

    prompt = f"""Você é o assistente inteligente do CRM pessoal do Renato. Analise o projeto abaixo e gere um parecer detalhado.

{full_context}
{memory_instruction}
## INSTRUÇÃO
{user_instruction}"""

    # Chamar Claude
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

        if response.status_code != 200:
            return {"error": f"Erro na API: {response.status_code}", "detail": response.text[:200]}

        analysis_text = response.json().get("content", [{}])[0].get("text", "")

    except Exception as e:
        return {"error": str(e)}

    # Salvar como nota do projeto
    note_id = None
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            titulo = f"Parecer IA: {project['nome'][:50]} ({date.today().isoformat()})"
            cursor.execute("""
                INSERT INTO project_notes (project_id, conteudo, tipo, titulo, autor)
                VALUES (%s, %s, 'parecer', %s, 'INTEL IA')
                RETURNING id
            """, (project_id, analysis_text, titulo))
            note_id = cursor.fetchone()['id']
            conn.commit()
    except Exception as e:
        logger.warning(f"Erro ao salvar nota: {e}")

    sources = []
    if individual_msgs:
        sources.append(f"{len(individual_msgs)} mensagens individuais")
    if group_msgs:
        for gm in group_msgs:
            sources.append(f"Grupo: {gm['group_name']} ({len(gm['messages'])} msgs)")
    if docs:
        sources.append(f"{len(docs)} documentos no Drive")

    return {
        "analysis": analysis_text,
        "sources": sources,
        "saved_note_id": note_id,
        "project_name": project['nome']
    }
