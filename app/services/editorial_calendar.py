"""
Editorial Calendar Service
Manages content scheduling for LinkedIn and Instagram
"""
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from app.database import get_db
import json


# Status constants
EDITORIAL_STATUS = {
    'draft': {'label': 'Rascunho', 'color': '#6b7280'},
    'scheduled': {'label': 'Agendado', 'color': '#3b82f6'},
    'ready': {'label': 'Pronto', 'color': '#10b981'},
    'published': {'label': 'Publicado', 'color': '#8b5cf6'},
    'archived': {'label': 'Arquivado', 'color': '#9ca3af'},
}

EDITORIAL_CANAIS = {
    'linkedin': {'label': 'LinkedIn', 'icon': 'linkedin', 'color': '#0077b5'},
    'instagram': {'label': 'Instagram', 'icon': 'instagram', 'color': '#e4405f'},
    'both': {'label': 'Ambos', 'icon': 'share', 'color': '#6366f1'},
}

EDITORIAL_TIPOS = {
    'repost': {'label': 'Repost', 'description': 'Republicar artigo existente'},
    'adaptacao': {'label': 'Adaptação', 'description': 'Adaptar para formato da rede'},
    'destaque': {'label': 'Destaque', 'description': 'Post destacando trecho'},
    'serie': {'label': 'Série', 'description': 'Parte de série de posts'},
}


def get_editorial_posts(
    status: Optional[str] = None,
    canal: Optional[str] = None,
    project_id: Optional[int] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    limit: int = 100
) -> List[Dict]:
    """Get editorial posts with filters"""
    with get_db() as conn:
        cursor = conn.cursor()

        query = """
            SELECT ep.*,
                   p.nome as project_nome,
                   t.titulo as task_titulo,
                   t.status as task_status
            FROM editorial_posts ep
            LEFT JOIN projects p ON ep.project_id = p.id
            LEFT JOIN tasks t ON ep.task_id = t.id
            WHERE 1=1
        """
        params = []

        if status:
            query += " AND ep.status = %s"
            params.append(status)

        if canal:
            query += " AND ep.canal = %s"
            params.append(canal)

        if project_id:
            query += " AND ep.project_id = %s"
            params.append(project_id)

        if from_date:
            query += " AND ep.data_publicacao >= %s"
            params.append(from_date)

        if to_date:
            query += " AND ep.data_publicacao <= %s"
            params.append(to_date)

        query += " ORDER BY COALESCE(ep.data_publicacao, ep.criado_em) ASC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        posts = cursor.fetchall()

        return [dict(p) for p in posts]


def get_editorial_post(post_id: int) -> Optional[Dict]:
    """Get single editorial post by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ep.*,
                   p.nome as project_nome,
                   t.titulo as task_titulo,
                   t.status as task_status,
                   ce.summary as event_summary,
                   ce.start_datetime as event_start
            FROM editorial_posts ep
            LEFT JOIN projects p ON ep.project_id = p.id
            LEFT JOIN tasks t ON ep.task_id = t.id
            LEFT JOIN calendar_events ce ON ep.calendar_event_id = ce.id
            WHERE ep.id = %s
        """, (post_id,))
        post = cursor.fetchone()
        return dict(post) if post else None


def create_editorial_post(data: Dict) -> Dict:
    """Create a new editorial post"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO editorial_posts (
                project_id, article_slug, article_title, article_url,
                article_description, canal, tipo, titulo_adaptado,
                conteudo_adaptado, hashtags, imagem_url, status,
                data_publicacao, prioridade, notas, tags
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) RETURNING *
        """, (
            data.get('project_id'),
            data.get('article_slug'),
            data.get('article_title'),
            data.get('article_url'),
            data.get('article_description'),
            data.get('canal', 'linkedin'),
            data.get('tipo', 'repost'),
            data.get('titulo_adaptado'),
            data.get('conteudo_adaptado'),
            json.dumps(data.get('hashtags', [])),
            data.get('imagem_url'),
            data.get('status', 'draft'),
            data.get('data_publicacao'),
            data.get('prioridade', 5),
            data.get('notas'),
            json.dumps(data.get('tags', []))
        ))

        post = cursor.fetchone()
        return dict(post)


def update_editorial_post(post_id: int, data: Dict) -> Optional[Dict]:
    """Update an editorial post"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Build dynamic update query
        updates = []
        params = []

        updatable_fields = [
            'project_id', 'article_slug', 'article_title', 'article_url',
            'article_description', 'canal', 'tipo', 'titulo_adaptado',
            'conteudo_adaptado', 'imagem_url', 'status', 'data_publicacao',
            'prioridade', 'notas', 'url_publicado', 'task_id', 'calendar_event_id'
        ]

        for field in updatable_fields:
            if field in data:
                updates.append(f"{field} = %s")
                params.append(data[field])

        # Handle JSON fields
        if 'hashtags' in data:
            updates.append("hashtags = %s")
            params.append(json.dumps(data['hashtags']))

        if 'tags' in data:
            updates.append("tags = %s")
            params.append(json.dumps(data['tags']))

        if 'metricas' in data:
            updates.append("metricas = %s")
            params.append(json.dumps(data['metricas']))

        if not updates:
            return get_editorial_post(post_id)

        updates.append("atualizado_em = CURRENT_TIMESTAMP")
        params.append(post_id)

        query = f"UPDATE editorial_posts SET {', '.join(updates)} WHERE id = %s RETURNING *"
        cursor.execute(query, params)
        post = cursor.fetchone()
        return dict(post) if post else None


def delete_editorial_post(post_id: int) -> bool:
    """Delete an editorial post"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM editorial_posts WHERE id = %s", (post_id,))
        return cursor.rowcount > 0


def schedule_post(post_id: int, data_publicacao: datetime, create_task: bool = True, create_event: bool = True) -> Dict:
    """Schedule a post for publication with optional task and calendar event"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get the post
        cursor.execute("SELECT * FROM editorial_posts WHERE id = %s", (post_id,))
        post = cursor.fetchone()
        if not post:
            raise ValueError(f"Post {post_id} not found")

        task_id = post['task_id']
        event_id = post['calendar_event_id']

        # Create task if requested
        if create_task and not task_id:
            cursor.execute("""
                INSERT INTO tasks (
                    titulo, descricao, project_id, data_vencimento,
                    status, prioridade, tags, contexto
                ) VALUES (%s, %s, %s, %s, 'pending', %s, %s, 'professional')
                RETURNING id
            """, (
                f"Publicar: {post['article_title'][:50]}",
                f"Publicar post no {post['canal']}: {post['article_title']}",
                post['project_id'],
                data_publicacao,
                post['prioridade'],
                json.dumps(['editorial', post['canal']])
            ))
            task_id = cursor.fetchone()['id']

        # Create calendar event if requested
        if create_event and not event_id:
            cursor.execute("""
                INSERT INTO calendar_events (
                    summary, description, start_datetime, end_datetime,
                    status, source, local_only
                ) VALUES (%s, %s, %s, %s, 'confirmed', 'editorial', TRUE)
                RETURNING id
            """, (
                f"📱 Publicar: {post['article_title'][:40]}",
                f"Canal: {post['canal']}\nTipo: {post['tipo']}\n\n{post['article_url'] or ''}",
                data_publicacao,
                data_publicacao + timedelta(minutes=30)
            ))
            event_id = cursor.fetchone()['id']

        # Update the post
        cursor.execute("""
            UPDATE editorial_posts
            SET status = 'scheduled',
                data_publicacao = %s,
                task_id = %s,
                calendar_event_id = %s,
                atualizado_em = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING *
        """, (data_publicacao, task_id, event_id, post_id))

        return dict(cursor.fetchone())


def mark_as_published(post_id: int, url_publicado: Optional[str] = None, metricas: Optional[Dict] = None) -> Dict:
    """Mark a post as published"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE editorial_posts
            SET status = 'published',
                data_publicado = CURRENT_TIMESTAMP,
                url_publicado = COALESCE(%s, url_publicado),
                metricas = COALESCE(%s, metricas),
                atualizado_em = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING *
        """, (url_publicado, json.dumps(metricas) if metricas else None, post_id))

        post = cursor.fetchone()

        # Mark associated task as completed
        if post and post['task_id']:
            cursor.execute("""
                UPDATE tasks
                SET status = 'completed', data_conclusao = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (post['task_id'],))

        return dict(post) if post else None


def import_articles_from_site(articles: List[Dict], project_id: Optional[int] = None) -> Dict:
    """Import articles from the website as draft editorial posts"""
    with get_db() as conn:
        cursor = conn.cursor()

        imported = 0
        skipped = 0

        for article in articles:
            # Check if already imported
            cursor.execute(
                "SELECT id FROM editorial_posts WHERE article_slug = %s",
                (article.get('slug'),)
            )
            if cursor.fetchone():
                skipped += 1
                continue

            # Create draft post
            cursor.execute("""
                INSERT INTO editorial_posts (
                    project_id, article_slug, article_title, article_url,
                    article_description, canal, tipo, status, hashtags, tags
                ) VALUES (%s, %s, %s, %s, %s, 'linkedin', 'repost', 'draft', %s, %s)
            """, (
                project_id,
                article.get('slug'),
                article.get('title'),
                f"https://almeida-prado.com/blog/{article.get('slug')}",
                article.get('description'),
                json.dumps(article.get('tags', [])),
                json.dumps([article.get('category', 'Artigo')])
            ))
            imported += 1

        return {'imported': imported, 'skipped': skipped}


def get_calendar_view(year: int, month: int) -> Dict:
    """Get posts organized by day for calendar view"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get first and last day of month
        first_day = datetime(year, month, 1)
        if month == 12:
            last_day = datetime(year + 1, 1, 1)
        else:
            last_day = datetime(year, month + 1, 1)

        cursor.execute("""
            SELECT ep.*,
                   p.nome as project_nome
            FROM editorial_posts ep
            LEFT JOIN projects p ON ep.project_id = p.id
            WHERE ep.data_publicacao >= %s AND ep.data_publicacao < %s
            ORDER BY ep.data_publicacao ASC
        """, (first_day, last_day))

        posts = cursor.fetchall()

        # Organize by day
        calendar = {}
        for post in posts:
            if post['data_publicacao']:
                day = post['data_publicacao'].day
                if day not in calendar:
                    calendar[day] = []
                calendar[day].append(dict(post))

        return {
            'year': year,
            'month': month,
            'posts': calendar,
            'total': len(posts)
        }


def get_stats() -> Dict:
    """Get editorial calendar statistics"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'draft') as drafts,
                COUNT(*) FILTER (WHERE status = 'scheduled') as scheduled,
                COUNT(*) FILTER (WHERE status = 'published') as published,
                COUNT(*) FILTER (WHERE canal = 'linkedin') as linkedin,
                COUNT(*) FILTER (WHERE canal = 'instagram') as instagram,
                COUNT(*) FILTER (WHERE canal = 'both') as both,
                COUNT(*) as total
            FROM editorial_posts
        """)

        stats = dict(cursor.fetchone())

        # Get next scheduled posts
        cursor.execute("""
            SELECT * FROM editorial_posts
            WHERE status = 'scheduled' AND data_publicacao >= NOW()
            ORDER BY data_publicacao ASC
            LIMIT 5
        """)
        stats['upcoming'] = [dict(p) for p in cursor.fetchall()]

        return stats
