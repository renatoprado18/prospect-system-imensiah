"""
Servico de Projetos

Gerencia projetos pessoais e profissionais com vinculos a contatos,
tarefas, calendario e mensagens.
"""

from datetime import datetime, date
from typing import Dict, List, Optional, Any
import json

from database import get_db


# Tipos de projeto
PROJECT_TYPES = {
    'pessoal': {'label': 'Pessoal', 'icon': 'house-heart', 'color': '#10b981'},
    'patrimonio': {'label': 'Patrimonio', 'icon': 'building', 'color': '#f59e0b'},
    'negocio': {'label': 'Negocio', 'icon': 'briefcase', 'color': '#6366f1'},
    'conselho': {'label': 'Conselho', 'icon': 'people', 'color': '#8b5cf6'},
}

# Status de projeto
PROJECT_STATUS = {
    'ativo': {'label': 'Ativo', 'color': '#10b981'},
    'pausado': {'label': 'Pausado', 'color': '#f59e0b'},
    'concluido': {'label': 'Concluido', 'color': '#6b7280'},
    'cancelado': {'label': 'Cancelado', 'color': '#ef4444'},
}

# Owner config - automatically added as participant to all projects
OWNER_EMAIL = "renato@almeida-prado.com"
OWNER_NAME_PATTERNS = ["Renato de Faria e Almeida Prado", "Renato Almeida Prado", "Renato de Faria", "Renato Prado"]


def get_owner_contact_id() -> Optional[int]:
    """Find the owner's contact ID by email or name patterns."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Try by email first
        cursor.execute("""
            SELECT id FROM contacts
            WHERE emails::text ILIKE %s
            LIMIT 1
        """, (f'%{OWNER_EMAIL}%',))
        row = cursor.fetchone()
        if row:
            return row['id']

        # Try by name patterns
        for name in OWNER_NAME_PATTERNS:
            cursor.execute("""
                SELECT id FROM contacts
                WHERE nome ILIKE %s
                LIMIT 1
            """, (f'%{name}%',))
            row = cursor.fetchone()
            if row:
                return row['id']

        return None


def list_projects(
    tipo: str = None,
    status: str = None,
    limit: int = 50,
    offset: int = 0,
    include_completed: bool = False,
    search: str = None
) -> List[Dict]:
    """Lista projetos com filtros opcionais e dados de urgencia."""
    from datetime import date

    with get_db() as conn:
        cursor = conn.cursor()

        # tasks_vencidas: counts only "minhas" overdue tasks (mine = contact_id matches admin user's contact_id,
        # OR the task isn't a RACI item). RACIs assigned to other people are monitoria — they show in the
        # project but don't trigger the "Precisa de atencao" badge.
        query = """
            SELECT p.*,
                   (SELECT COUNT(*) FROM project_members WHERE project_id = p.id) as total_membros,
                   (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'pending') as tasks_pendentes,
                   (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'pending'
                        AND t.data_vencimento IS NOT NULL AND t.data_vencimento < NOW()
                        AND (t.origem IS DISTINCT FROM 'conselhoos_raci'
                             OR t.contact_id = (SELECT contact_id FROM users WHERE id = 1))
                   ) as tasks_vencidas,
                   (SELECT COUNT(*) FROM project_milestones WHERE project_id = p.id AND status = 'pendente') as marcos_pendentes
            FROM projects p
            WHERE 1=1
        """
        params = []

        if tipo:
            query += " AND p.tipo = %s"
            params.append(tipo)

        if status:
            query += " AND p.status = %s"
            params.append(status)
        elif not include_completed:
            # By default exclude completed
            query += " AND p.status != 'concluido'"

        if search:
            query += " AND (p.nome ILIKE %s OR p.descricao ILIKE %s OR p.empresa_relacionada ILIKE %s)"
            like = f"%{search}%"
            params.extend([like, like, like])

        query += " ORDER BY p.prioridade ASC, p.criado_em DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cursor.execute(query, params)
        projects = [dict(row) for row in cursor.fetchall()]

        hoje = date.today()

        # Enrich each project with urgency data
        for p in projects:
            p['tipo_info'] = PROJECT_TYPES.get(p['tipo'], PROJECT_TYPES['negocio'])
            p['status_info'] = PROJECT_STATUS.get(p['status'], PROJECT_STATUS['ativo'])

            # Get next milestone
            cursor.execute("""
                SELECT titulo, data_prevista
                FROM project_milestones
                WHERE project_id = %s AND status = 'pendente'
                AND data_prevista IS NOT NULL
                ORDER BY data_prevista ASC
                LIMIT 1
            """, (p['id'],))
            marco = cursor.fetchone()
            if marco:
                marco = dict(marco)
                data_marco = marco['data_prevista']
                if isinstance(data_marco, str):
                    from datetime import datetime
                    data_marco = datetime.strptime(data_marco, '%Y-%m-%d').date()
                elif hasattr(data_marco, 'date'):
                    # It's a datetime, convert to date
                    data_marco = data_marco.date()
                dias_ate = (data_marco - hoje).days
                p['proximo_marco'] = {
                    'titulo': marco['titulo'],
                    'data_prevista': str(marco['data_prevista']),
                    'dias_ate': dias_ate
                }

            # Get next task — excludes monitoria (RACIs assigned to others) so a project
            # doesn't get categorized as "atencao" because of someone else's overdue RACI.
            cursor.execute("""
                SELECT titulo, data_vencimento
                FROM tasks
                WHERE project_id = %s AND status = 'pending'
                  AND (origem IS DISTINCT FROM 'conselhoos_raci'
                       OR contact_id = (SELECT contact_id FROM users WHERE id = 1))
                ORDER BY
                    CASE WHEN data_vencimento IS NULL THEN 1 ELSE 0 END,
                    data_vencimento ASC
                LIMIT 1
            """, (p['id'],))
            tarefa = cursor.fetchone()
            if tarefa:
                tarefa = dict(tarefa)
                p['proxima_tarefa'] = {
                    'titulo': tarefa['titulo']
                }
                if tarefa['data_vencimento']:
                    data_tarefa = tarefa['data_vencimento']
                    if isinstance(data_tarefa, str):
                        from datetime import datetime
                        data_tarefa = datetime.strptime(data_tarefa, '%Y-%m-%d').date()
                    elif hasattr(data_tarefa, 'date'):
                        # It's a datetime, convert to date
                        data_tarefa = data_tarefa.date()
                    p['proxima_tarefa']['dias_ate'] = (data_tarefa - hoje).days

        return projects


def get_project(project_id: int) -> Optional[Dict]:
    """Retorna projeto com todos os detalhes."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get project
        cursor.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        row = cursor.fetchone()
        if not row:
            return None

        project = dict(row)
        project['tipo_info'] = PROJECT_TYPES.get(project['tipo'], PROJECT_TYPES['negocio'])
        project['status_info'] = PROJECT_STATUS.get(project['status'], PROJECT_STATUS['ativo'])

        # Get members with contact info
        cursor.execute("""
            SELECT pm.*, c.nome, c.empresa, c.cargo, c.foto_url, c.emails, c.telefones
            FROM project_members pm
            JOIN contacts c ON c.id = pm.contact_id
            WHERE pm.project_id = %s
            ORDER BY pm.adicionado_em
        """, (project_id,))
        project['membros'] = [dict(row) for row in cursor.fetchall()]

        # Get milestones
        cursor.execute("""
            SELECT * FROM project_milestones
            WHERE project_id = %s
            ORDER BY ordem, data_prevista
        """, (project_id,))
        project['marcos'] = [dict(row) for row in cursor.fetchall()]

        # Get tasks
        cursor.execute("""
            SELECT t.*, c.nome as contact_nome
            FROM tasks t
            LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.project_id = %s
            ORDER BY t.status, t.prioridade, t.data_vencimento
        """, (project_id,))
        project['tarefas'] = [dict(row) for row in cursor.fetchall()]

        # Preserve text notes field before overwriting with timeline array
        project['notas_texto'] = project.get('notas', '') or ''

        # Get notes/timeline
        cursor.execute("""
            SELECT * FROM project_notes
            WHERE project_id = %s
            ORDER BY criado_em DESC
            LIMIT 20
        """, (project_id,))
        project['notas'] = [dict(row) for row in cursor.fetchall()]

        # Get linked events
        cursor.execute("""
            SELECT pe.*, ce.summary, ce.start_datetime, ce.end_datetime
            FROM project_events pe
            JOIN calendar_events ce ON ce.id = pe.calendar_event_id
            WHERE pe.project_id = %s
            ORDER BY ce.start_datetime DESC
            LIMIT 10
        """, (project_id,))
        project['eventos'] = [dict(row) for row in cursor.fetchall()]

        return project


def get_project_briefing_context(project_id: int) -> Optional[Dict]:
    """
    Gather all context needed for an AI briefing of a project:
    tasks, member messages (WhatsApp/email), notes, calendar events.
    Returns raw data for Claude to synthesize.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Basic project info
        cursor.execute("SELECT id, nome, descricao, tipo, status, data_previsao, criado_em FROM projects WHERE id = %s", (project_id,))
        project = cursor.fetchone()
        if not project:
            return None
        project = dict(project)

        # Tasks with status — include contact_id + origem so the briefing can flag monitoria
        cursor.execute("""
            SELECT t.titulo, t.status, t.data_vencimento, t.prioridade, t.descricao,
                   t.contact_id, t.origem,
                   c.nome as responsavel
            FROM tasks t
            LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.project_id = %s
            ORDER BY t.data_vencimento NULLS LAST
        """, (project_id,))
        tasks = [dict(r) for r in cursor.fetchall()]

        # Owner contact_id (admin user) — used to split tasks into "minhas" vs monitoria
        cursor.execute("SELECT contact_id FROM users WHERE id = 1")
        owner_row = cursor.fetchone()
        owner_contact_id = owner_row['contact_id'] if owner_row else None

        # Members
        cursor.execute("""
            SELECT pm.contact_id, c.nome, pm.papel
            FROM project_members pm
            JOIN contacts c ON c.id = pm.contact_id
            WHERE pm.project_id = %s
        """, (project_id,))
        members = [dict(r) for r in cursor.fetchall()]
        member_ids = [m['contact_id'] for m in members]

        # Recent messages from members (last 30 days, max 20)
        recent_messages = []
        if member_ids:
            cursor.execute("""
                SELECT m.conteudo, m.direcao, m.enviado_em, m.recebido_em,
                       cv.canal, cv.contact_id, c.nome as contact_nome
                FROM messages m
                JOIN conversations cv ON cv.id = m.conversation_id
                JOIN contacts c ON c.id = cv.contact_id
                WHERE cv.contact_id = ANY(%s)
                  AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - INTERVAL '30 days'
                ORDER BY COALESCE(m.enviado_em, m.recebido_em) DESC
                LIMIT 20
            """, (member_ids,))
            recent_messages = [dict(r) for r in cursor.fetchall()]

        # Recent notes
        cursor.execute("""
            SELECT titulo, conteudo, tipo, autor, criado_em
            FROM project_notes
            WHERE project_id = %s
            ORDER BY criado_em DESC
            LIMIT 5
        """, (project_id,))
        notes = [dict(r) for r in cursor.fetchall()]

        # Upcoming events
        cursor.execute("""
            SELECT ce.summary, ce.start_datetime, ce.end_datetime
            FROM project_events pe
            JOIN calendar_events ce ON ce.id = pe.calendar_event_id
            WHERE pe.project_id = %s
              AND ce.start_datetime >= NOW() - INTERVAL '7 days'
            ORDER BY ce.start_datetime
            LIMIT 5
        """, (project_id,))
        events = [dict(r) for r in cursor.fetchall()]

        # Milestones
        cursor.execute("""
            SELECT titulo, status, data_prevista, data_conclusao
            FROM project_milestones
            WHERE project_id = %s
            ORDER BY data_prevista NULLS LAST
        """, (project_id,))
        milestones = [dict(r) for r in cursor.fetchall()]

        # Recent memories from project members (calls, meetings, facts)
        member_memories = []
        if member_ids:
            cursor.execute("""
                SELECT cm.titulo, cm.resumo, cm.tipo, cm.data_ocorrencia, c.nome as contact_nome
                FROM contact_memories cm
                JOIN contacts c ON c.id = cm.contact_id
                WHERE cm.contact_id = ANY(%s)
                  AND (cm.data_ocorrencia > NOW() - INTERVAL '30 days'
                       OR cm.id IN (SELECT id FROM contact_memories WHERE contact_id = ANY(%s) ORDER BY id DESC LIMIT 5))
                ORDER BY cm.data_ocorrencia DESC NULLS LAST
                LIMIT 10
            """, (member_ids, member_ids))
            member_memories = [dict(r) for r in cursor.fetchall()]

        return {
            'project': project,
            'tasks': tasks,
            'owner_contact_id': owner_contact_id,
            'members': members,
            'recent_messages': recent_messages,
            'notes': notes,
            'events': events,
            'milestones': milestones,
            'member_memories': member_memories,
        }


def create_project(data: Dict) -> Dict:
    """Cria novo projeto."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO projects (
                nome, descricao, tipo, status, prioridade,
                data_inicio, data_previsao, cor, icone,
                empresa_relacionada, valor_estimado, notas, tags
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            data.get('nome'),
            data.get('descricao'),
            data.get('tipo', 'negocio'),
            data.get('status', 'ativo'),
            data.get('prioridade', 5),
            data.get('data_inicio'),
            data.get('data_previsao'),
            data.get('cor', PROJECT_TYPES.get(data.get('tipo', 'negocio'), {}).get('color', '#6366f1')),
            data.get('icone', PROJECT_TYPES.get(data.get('tipo', 'negocio'), {}).get('icon', 'folder')),
            data.get('empresa_relacionada'),
            data.get('valor_estimado'),
            data.get('notas'),
            json.dumps(data.get('tags', []))
        ))

        project = dict(cursor.fetchone())
        conn.commit()

        # Always add owner as participant (Renato)
        owner_id = get_owner_contact_id()
        if owner_id:
            add_project_member(project['id'], owner_id, "Responsavel")

        # Add initial members if provided
        if data.get('membros'):
            for membro in data['membros']:
                add_project_member(project['id'], membro['contact_id'], membro.get('papel'))

        return project


def update_project(project_id: int, data: Dict) -> Optional[Dict]:
    """Atualiza projeto existente."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Build update dynamically
        allowed = ['nome', 'descricao', 'tipo', 'status', 'prioridade',
                   'data_inicio', 'data_previsao', 'data_conclusao',
                   'cor', 'icone', 'empresa_relacionada', 'valor_estimado', 'notas', 'tags',
                   'google_drive_folder_id', 'metadata']

        updates = []
        values = []
        for field in allowed:
            if field in data:
                value = data[field]
                if isinstance(value, (list, dict)):
                    value = json.dumps(value)
                updates.append(f"{field} = %s")
                values.append(value)

        if not updates:
            return get_project(project_id)

        updates.append("atualizado_em = NOW()")
        values.append(project_id)

        query = f"UPDATE projects SET {', '.join(updates)} WHERE id = %s RETURNING *"
        cursor.execute(query, values)
        result = cursor.fetchone()
        conn.commit()

        return dict(result) if result else None


def delete_project(project_id: int) -> bool:
    """Deleta projeto e dependencias."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        conn.commit()
        return cursor.rowcount > 0


# ============== MEMBERS ==============

def add_project_member(project_id: int, contact_id: int, papel: str = None) -> Optional[Dict]:
    """Adiciona membro ao projeto."""
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO project_members (project_id, contact_id, papel)
                VALUES (%s, %s, %s)
                ON CONFLICT (project_id, contact_id) DO UPDATE SET papel = EXCLUDED.papel
                RETURNING *
            """, (project_id, contact_id, papel))
            result = cursor.fetchone()
            conn.commit()
            return dict(result) if result else None
        except Exception as e:
            print(f"Error adding member: {e}")
            return None


def remove_project_member(project_id: int, contact_id: int) -> bool:
    """Remove membro do projeto."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM project_members
            WHERE project_id = %s AND contact_id = %s
        """, (project_id, contact_id))
        conn.commit()
        return cursor.rowcount > 0


# ============== MILESTONES ==============

def add_milestone(project_id: int, data: Dict) -> Dict:
    """Adiciona marco ao projeto."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO project_milestones (project_id, titulo, descricao, data_prevista, ordem)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (
            project_id,
            data.get('titulo'),
            data.get('descricao'),
            data.get('data_prevista'),
            data.get('ordem', 0)
        ))
        result = cursor.fetchone()
        conn.commit()
        return dict(result)


def update_milestone(milestone_id: int, data: Dict) -> Optional[Dict]:
    """Atualiza marco."""
    with get_db() as conn:
        cursor = conn.cursor()

        updates = []
        values = []
        for field in ['titulo', 'descricao', 'data_prevista', 'data_conclusao', 'status', 'ordem',
                      'email_thread_id', 'email_message_id', 'metadata']:
            if field in data:
                updates.append(f"{field} = %s")
                values.append(data[field])

        if not updates:
            return None

        values.append(milestone_id)
        query = f"UPDATE project_milestones SET {', '.join(updates)} WHERE id = %s RETURNING *"
        cursor.execute(query, values)
        result = cursor.fetchone()
        conn.commit()
        return dict(result) if result else None


def delete_milestone(milestone_id: int) -> bool:
    """Deleta marco."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM project_milestones WHERE id = %s", (milestone_id,))
        conn.commit()
        return cursor.rowcount > 0


# ============== NOTES ==============

def add_project_note(project_id: int, data: Dict) -> Dict:
    """Adiciona nota/atualizacao ao projeto."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (
            project_id,
            data.get('tipo', 'nota'),
            data.get('titulo'),
            data.get('conteudo'),
            data.get('autor', 'Renato')
        ))
        result = cursor.fetchone()
        conn.commit()
        return dict(result)


def get_project_timeline(project_id: int, limit: int = 50) -> List[Dict]:
    """Retorna timeline completa do projeto (notas + eventos + tarefas)."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get notes
        cursor.execute("""
            SELECT 'nota' as item_type, id, tipo, titulo, conteudo, criado_em as data
            FROM project_notes
            WHERE project_id = %s
        """, (project_id,))
        items = [dict(row) for row in cursor.fetchall()]

        # Get task completions
        cursor.execute("""
            SELECT 'tarefa' as item_type, id, titulo, status, data_conclusao as data
            FROM tasks
            WHERE project_id = %s AND data_conclusao IS NOT NULL
        """, (project_id,))
        items.extend([dict(row) for row in cursor.fetchall()])

        # Get milestone completions
        cursor.execute("""
            SELECT 'marco' as item_type, id, titulo, status, data_conclusao as data
            FROM project_milestones
            WHERE project_id = %s AND data_conclusao IS NOT NULL
        """, (project_id,))
        items.extend([dict(row) for row in cursor.fetchall()])

        # Sort by date descending
        items.sort(key=lambda x: x.get('data') or datetime.min, reverse=True)

        return items[:limit]


# ============== STATS ==============

def get_projects_stats() -> Dict:
    """Retorna estatisticas dos projetos."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Total by status
        cursor.execute("""
            SELECT status, COUNT(*) as total
            FROM projects
            GROUP BY status
        """)
        by_status = {row['status']: row['total'] for row in cursor.fetchall()}

        # Total by type
        cursor.execute("""
            SELECT tipo, COUNT(*) as total
            FROM projects
            GROUP BY tipo
        """)
        by_tipo = {row['tipo']: row['total'] for row in cursor.fetchall()}

        # Active with pending tasks
        cursor.execute("""
            SELECT COUNT(DISTINCT p.id) as total
            FROM projects p
            JOIN tasks t ON t.project_id = p.id
            WHERE p.status = 'ativo' AND t.status = 'pending'
        """)
        with_pending_tasks = cursor.fetchone()['total']

        # Upcoming milestones (next 30 days)
        cursor.execute("""
            SELECT COUNT(*) as total
            FROM project_milestones pm
            JOIN projects p ON p.id = pm.project_id
            WHERE p.status = 'ativo'
              AND pm.status = 'pendente'
              AND pm.data_prevista <= CURRENT_DATE + INTERVAL '30 days'
        """)
        upcoming_milestones = cursor.fetchone()['total']

        return {
            'total_ativos': by_status.get('ativo', 0),
            'total_concluidos': by_status.get('concluido', 0),
            'by_status': by_status,
            'by_tipo': by_tipo,
            'com_tarefas_pendentes': with_pending_tasks,
            'marcos_proximos': upcoming_milestones
        }


def get_active_projects_summary(limit: int = 5) -> List[Dict]:
    """Retorna resumo dos projetos ativos para o dashboard."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT p.id, p.nome, p.tipo, p.cor, p.icone,
                   (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'pending') as tasks_pendentes,
                   (SELECT MIN(data_prevista) FROM project_milestones
                    WHERE project_id = p.id AND status = 'pendente') as proximo_marco
            FROM projects p
            WHERE p.status = 'ativo'
            ORDER BY p.prioridade ASC, p.atualizado_em DESC
            LIMIT %s
        """, (limit,))

        projects = []
        for row in cursor.fetchall():
            p = dict(row)
            p['tipo_info'] = PROJECT_TYPES.get(p['tipo'], PROJECT_TYPES['negocio'])
            projects.append(p)

        return projects
