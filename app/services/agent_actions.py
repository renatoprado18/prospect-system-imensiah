"""
Agent Actions Audit Log

Registra TODAS as ações autônomas que o agente executa por conta própria
(sem perguntar ao usuário). Alimenta o digest matinal e o debriefing 19h.

Contrato:
    log_action(
        action_type='task_resolved' | 'post_published' | 'contact_updated' | ...,
        category='tasks' | 'editorial' | 'contacts' | 'email' | 'whatsapp' | 'conselho' | 'calendar',
        title='Tarefa concluída: Enviar ata Vallen',  # 1-liner usado no digest
        details=None,                                  # markdown opcional
        scope_ref={'task_id': 123, 'contact_id': 45},  # IDs relacionados
        source='task_auto_resolver',                   # quem disparou
        payload={...},                                 # resultado bruto
        undo_hint='UPDATE tasks SET status=pending WHERE id=123',  # NULL se não-undoable
    )
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from database import get_db

logger = logging.getLogger(__name__)


CATEGORIES = ('tasks', 'editorial', 'contacts', 'email', 'whatsapp', 'conselho', 'calendar', 'system')


def log_action(
    action_type: str,
    category: str,
    title: str,
    details: Optional[str] = None,
    scope_ref: Optional[Dict] = None,
    source: Optional[str] = None,
    payload: Optional[Dict] = None,
    undo_hint: Optional[str] = None,
) -> Optional[int]:
    """Registra uma ação autônoma. Retorna o ID ou None em caso de erro."""
    if category not in CATEGORIES:
        logger.warning(f"agent_actions: categoria desconhecida '{category}' para {action_type}")

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agent_actions
                    (action_type, category, title, details, scope_ref, source, payload, undo_hint)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                action_type,
                category,
                title,
                details,
                json.dumps(scope_ref or {}),
                source,
                json.dumps(payload) if payload else None,
                undo_hint,
            ))
            action_id = cursor.fetchone()['id']
            conn.commit()
            logger.info(f"agent_action #{action_id} registered: {action_type} — {title}")
            return action_id
    except Exception as e:
        logger.error(f"Failed to log agent_action ({action_type}): {e}")
        return None


def list_actions(
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    category: Optional[str] = None,
    status: str = 'done',
    limit: int = 200,
) -> List[Dict]:
    """Lista ações filtradas por período/categoria/status."""
    where = ["1=1"]
    params: List[Any] = []

    if since:
        where.append("criado_em >= %s")
        params.append(since)
    if until:
        where.append("criado_em < %s")
        params.append(until)
    if category:
        where.append("category = %s")
        params.append(category)
    if status:
        where.append("status = %s")
        params.append(status)

    sql = f"""
        SELECT id, action_type, category, title, details, scope_ref, source,
               status, payload, undo_hint, undone_at, criado_em
        FROM agent_actions
        WHERE {' AND '.join(where)}
        ORDER BY criado_em DESC
        LIMIT %s
    """
    params.append(limit)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [dict(r) for r in cursor.fetchall()]


def summarize_for_digest(
    since: datetime,
    until: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Agrupa ações por categoria com contagens + lista compacta.
    Output:
        {
            'total': 12,
            'by_category': {
                'tasks': {'count': 5, 'titles': ['...', '...']},
                'editorial': {'count': 2, 'titles': [...]},
                ...
            },
            'first_at': datetime, 'last_at': datetime,
        }
    """
    until = until or datetime.now()
    actions = list_actions(since=since, until=until, status='done', limit=500)

    by_category: Dict[str, Dict] = {}
    for a in actions:
        cat = a['category']
        if cat not in by_category:
            by_category[cat] = {'count': 0, 'titles': []}
        by_category[cat]['count'] += 1
        if len(by_category[cat]['titles']) < 5:
            by_category[cat]['titles'].append(a['title'])

    return {
        'total': len(actions),
        'by_category': by_category,
        'first_at': actions[-1]['criado_em'] if actions else None,
        'last_at': actions[0]['criado_em'] if actions else None,
    }


def format_digest_section(summary: Dict[str, Any], header: str = "Fiz por você") -> Optional[str]:
    """Formata summary para WhatsApp (ou retorna None se vazio)."""
    if not summary or summary.get('total', 0) == 0:
        return None

    icons = {
        'tasks': '✅',
        'editorial': '📝',
        'contacts': '👥',
        'email': '📧',
        'whatsapp': '💬',
        'conselho': '🏛️',
        'calendar': '📅',
        'system': '⚙️',
    }
    labels = {
        'tasks': 'Tarefas',
        'editorial': 'Editorial',
        'contacts': 'Contatos',
        'email': 'Email',
        'whatsapp': 'WhatsApp',
        'conselho': 'Conselho',
        'calendar': 'Calendário',
        'system': 'Sistema',
    }

    lines = [f"*{header}* ({summary['total']}):"]
    for cat, data in summary['by_category'].items():
        icon = icons.get(cat, '•')
        label = labels.get(cat, cat.title())
        lines.append(f"{icon} *{label}* ({data['count']})")
        for title in data['titles'][:3]:
            lines.append(f"  • {title}")
        if data['count'] > 3:
            lines.append(f"  • +{data['count'] - 3} outras")

    return "\n".join(lines)


def get_action(action_id: int) -> Optional[Dict]:
    """Busca uma ação específica."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, action_type, category, title, details, scope_ref, source,
                   status, payload, undo_hint, undone_at, criado_em
            FROM agent_actions WHERE id = %s
        """, (action_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def mark_undone(action_id: int) -> bool:
    """Marca ação como desfeita. Não executa o undo — apenas registra."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_actions
                SET status = 'undone', undone_at = NOW()
                WHERE id = %s AND status = 'done'
                RETURNING id
            """, (action_id,))
            row = cursor.fetchone()
            conn.commit()
            return row is not None
    except Exception as e:
        logger.error(f"Failed to mark agent_action #{action_id} undone: {e}")
        return False


def stats_today() -> Dict[str, int]:
    """Contagem rápida de ações de hoje, por status."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status, COUNT(*) AS total
            FROM agent_actions
            WHERE criado_em::date = CURRENT_DATE
            GROUP BY status
        """)
        return {r['status']: r['total'] for r in cursor.fetchall()}
