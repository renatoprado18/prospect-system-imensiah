"""
Editorial Calendar Service
Manages content scheduling for LinkedIn and Instagram
Includes AI-powered content analysis and categorization
"""
import os
import httpx
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from database import get_db
import json

logger = logging.getLogger(__name__)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


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
    """Get editorial posts with filters - OTIMIZADO"""
    with get_db() as conn:
        cursor = conn.cursor()

        # OTIMIZAÇÃO: Selecionar apenas colunas necessárias
        query = """
            SELECT ep.id, ep.article_title, ep.article_url, ep.article_description,
                   ep.canal, ep.tipo, ep.status, ep.data_publicacao, ep.data_publicado,
                   ep.ai_categoria, ep.ai_score_relevancia, ep.ai_gancho_linkedin,
                   ep.hot_take_id, ep.linkedin_post_url,
                   p.nome as project_nome
            FROM editorial_posts ep
            LEFT JOIN projects p ON ep.project_id = p.id
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
            'prioridade', 'notas', 'url_publicado', 'task_id', 'calendar_event_id',
            'linkedin_post_url', 'linkedin_impressoes', 'linkedin_reacoes',
            'linkedin_comentarios', 'linkedin_compartilhamentos', 'linkedin_cliques'
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

        # Set linkedin_metricas_em timestamp when metrics are saved
        metrics_fields = ['linkedin_impressoes', 'linkedin_reacoes', 'linkedin_comentarios',
                          'linkedin_compartilhamentos', 'linkedin_cliques']
        if any(data.get(f) for f in metrics_fields):
            updates.append("linkedin_metricas_em = CURRENT_TIMESTAMP")

        # Set data_publicado when LinkedIn URL is provided and post becomes published
        if data.get('linkedin_post_url') and data.get('status') == 'published':
            updates.append("data_publicado = COALESCE(data_publicado, CURRENT_TIMESTAMP)")

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


def bulk_schedule_posts(
    post_ids: List[int],
    start_date: datetime,
    frequency_per_week: int = 3,
    preferred_days: List[int] = None,  # 0=Mon, 1=Tue, etc
    preferred_hours: List[int] = None,
    create_tasks: bool = True,
    create_events: bool = True
) -> Dict:
    """
    Schedule multiple posts in bulk with optimal timing.

    Args:
        post_ids: List of post IDs to schedule
        start_date: Starting date for scheduling
        frequency_per_week: Posts per week (1-5)
        preferred_days: Days of week (default: Tue=1, Wed=2, Thu=3)
        preferred_hours: Hours to post (default: 9, 12)
        create_tasks: Create tasks for each post
        create_events: Create calendar events for each post

    Returns:
        Dict with scheduled posts and any errors
    """
    from datetime import date as date_type

    # LinkedIn optimal defaults
    if preferred_days is None:
        preferred_days = [1, 2, 3]  # Tue, Wed, Thu
    if preferred_hours is None:
        preferred_hours = [9, 12]  # 9am, 12pm

    scheduled = []
    errors = []

    with get_db() as conn:
        cursor = conn.cursor()

        # Get posts to schedule (only drafts)
        cursor.execute("""
            SELECT * FROM editorial_posts
            WHERE id = ANY(%s) AND status = 'draft'
            ORDER BY criado_em ASC
        """, (post_ids,))
        posts = cursor.fetchall()

        if not posts:
            return {'scheduled': [], 'errors': ['No draft posts found']}

        # Calculate scheduling slots
        current_date = start_date.date() if isinstance(start_date, datetime) else start_date
        hour_index = 0
        day_index = 0
        posts_this_week = 0
        week_start = current_date

        for post in posts:
            # Find next available slot
            while True:
                # Check if we've exceeded posts per week
                if posts_this_week >= frequency_per_week:
                    # Move to next week
                    days_until_next_week = 7 - current_date.weekday()
                    current_date = current_date + timedelta(days=days_until_next_week)
                    week_start = current_date
                    posts_this_week = 0
                    hour_index = 0
                    day_index = 0

                # Find next preferred day
                target_day = preferred_days[day_index % len(preferred_days)]
                days_until_target = (target_day - current_date.weekday()) % 7

                if days_until_target == 0 and current_date == week_start:
                    # If we're on the target day already, use it
                    pass
                elif days_until_target == 0:
                    # Already used this day, go to next
                    day_index += 1
                    continue
                else:
                    current_date = current_date + timedelta(days=days_until_target)

                # Check if still in same week
                if (current_date - week_start).days >= 7:
                    posts_this_week = frequency_per_week  # Force week change
                    continue

                break

            # Set the time
            hour = preferred_hours[hour_index % len(preferred_hours)]
            pub_datetime = datetime.combine(current_date, datetime.min.time().replace(hour=hour, minute=0))

            try:
                # Schedule the post
                result = schedule_post(
                    post['id'],
                    pub_datetime,
                    create_task=create_tasks,
                    create_event=create_events
                )
                scheduled.append({
                    'post_id': post['id'],
                    'title': post['article_title'],
                    'scheduled_for': pub_datetime.isoformat()
                })
            except Exception as e:
                errors.append({
                    'post_id': post['id'],
                    'error': str(e)
                })

            # Move to next slot
            posts_this_week += 1
            hour_index += 1
            day_index += 1

            # If we've used all hours for this day, move to next day
            if hour_index >= len(preferred_hours):
                hour_index = 0
                current_date = current_date + timedelta(days=1)

    return {
        'scheduled': scheduled,
        'errors': errors,
        'total_scheduled': len(scheduled),
        'total_errors': len(errors)
    }


def get_stats() -> Dict:
    """Get editorial calendar statistics - OTIMIZADO"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Query 1: Stats (usa índices status e canal)
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

        # Query 2: Upcoming (SELECT específico, usa índice scheduled)
        cursor.execute("""
            SELECT id, article_title, canal, tipo, status,
                   data_publicacao, ai_categoria, hot_take_id
            FROM editorial_posts
            WHERE status = 'scheduled' AND data_publicacao >= NOW()
            ORDER BY data_publicacao ASC
            LIMIT 5
        """)
        stats['upcoming'] = [dict(p) for p in cursor.fetchall()]

        return stats


# =============================================================================
# AI CONTENT ANALYSIS
# =============================================================================

CATEGORIAS_PRINCIPAIS = [
    "Governança Corporativa",
    "NeoGovernança",
    "Conselho de Administração",
    "M&A e Fusões",
    "Empresa Familiar",
    "Liderança Executiva",
    "ESG e Sustentabilidade",
    "Transformação Digital",
    "Estratégia Empresarial",
    "Gestão de Riscos",
    "Inovação",
    "Complexidade e Adaptação"
]

TIPOS_CONTEUDO = ["educativo", "opiniao", "case", "tendencia", "reflexao", "pratico"]
COMPLEXIDADES = ["iniciante", "intermediario", "avancado"]
PUBLICOS = ["ceos", "conselheiros", "empresarios_familiares", "executivos", "investidores", "consultores"]


def analyze_article_with_ai(post_id: int) -> Optional[Dict]:
    """
    Analyzes a single article using Claude AI and extracts rich metadata.

    Returns dict with:
    - categoria, subcategoria
    - publico_alvo (list)
    - tipo_conteudo
    - complexidade
    - evergreen (bool)
    - keywords (list)
    - gancho_linkedin (hook text)
    - tempo_leitura (minutes)
    - score_relevancia (1-10)
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set, skipping AI analysis")
        return None

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM editorial_posts WHERE id = %s", (post_id,))
        post = cursor.fetchone()

        if not post:
            return None

        post = dict(post)

    # Build prompt
    prompt = f"""Analise este artigo de blog sobre governança corporativa e negócios.

TÍTULO: {post.get('article_title', '')}

DESCRIÇÃO: {post.get('article_description', '')}

CATEGORIA ORIGINAL: {post.get('tags', '[]')}

URL: {post.get('article_url', '')}

---

Classifique este artigo respondendo em JSON válido:

{{
    "categoria": "<uma das: {', '.join(CATEGORIAS_PRINCIPAIS)}>",
    "subcategoria": "<subcategoria específica>",
    "publico_alvo": ["<lista de: {', '.join(PUBLICOS)}>"],
    "tipo_conteudo": "<uma das: {', '.join(TIPOS_CONTEUDO)}>",
    "complexidade": "<uma das: {', '.join(COMPLEXIDADES)}>",
    "evergreen": <true se o conteúdo é atemporal, false se datado>,
    "keywords": ["<5-8 palavras-chave principais>"],
    "gancho_linkedin": "<primeira frase impactante para LinkedIn, max 150 chars, que gere curiosidade>",
    "tempo_leitura": <estimativa em minutos baseado na descrição>,
    "score_relevancia": <1-10, considerando potencial de engajamento no LinkedIn>,
    "razao_score": "<breve explicação do score>"
}}

Responda APENAS com o JSON, sem explicações adicionais."""

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

            if response.status_code != 200:
                logger.error(f"AI API error: {response.status_code} - {response.text}")
                return None

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "").strip()

            # Parse JSON response
            # Handle potential markdown code blocks
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]

            analysis = json.loads(content)

            # Save to database
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE editorial_posts SET
                        ai_categoria = %s,
                        ai_subcategoria = %s,
                        ai_publico_alvo = %s,
                        ai_tipo_conteudo = %s,
                        ai_complexidade = %s,
                        ai_evergreen = %s,
                        ai_keywords = %s,
                        ai_gancho_linkedin = %s,
                        ai_tempo_leitura = %s,
                        ai_score_relevancia = %s,
                        ai_analise_completa = %s,
                        ai_analisado_em = NOW(),
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (
                    analysis.get('categoria'),
                    analysis.get('subcategoria'),
                    json.dumps(analysis.get('publico_alvo', [])),
                    analysis.get('tipo_conteudo'),
                    analysis.get('complexidade'),
                    analysis.get('evergreen', True),
                    json.dumps(analysis.get('keywords', [])),
                    analysis.get('gancho_linkedin'),
                    analysis.get('tempo_leitura'),
                    analysis.get('score_relevancia'),
                    json.dumps(analysis),
                    post_id
                ))

            return analysis

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response as JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"AI analysis error: {e}")
        return None


def analyze_all_articles(limit: int = None, force: bool = False) -> Dict:
    """
    Analyzes all editorial posts that haven't been analyzed yet.

    Args:
        limit: Max number of articles to analyze (for testing/batching)
        force: Re-analyze even if already analyzed

    Returns:
        Dict with analyzed count, errors, and results
    """
    with get_db() as conn:
        cursor = conn.cursor()

        if force:
            query = "SELECT id, article_title FROM editorial_posts ORDER BY criado_em ASC"
            params = []
        else:
            query = "SELECT id, article_title FROM editorial_posts WHERE ai_analisado_em IS NULL ORDER BY criado_em ASC"
            params = []

        if limit:
            query += " LIMIT %s"
            params.append(limit)

        cursor.execute(query, params if params else None)
        posts = cursor.fetchall()

    results = {
        'total': len(posts),
        'analyzed': 0,
        'errors': 0,
        'details': []
    }

    for post in posts:
        post_id = post['id']
        title = post['article_title']

        try:
            analysis = analyze_article_with_ai(post_id)
            if analysis:
                results['analyzed'] += 1
                results['details'].append({
                    'id': post_id,
                    'title': title[:50],
                    'categoria': analysis.get('categoria'),
                    'score': analysis.get('score_relevancia'),
                    'status': 'success'
                })
            else:
                results['errors'] += 1
                results['details'].append({
                    'id': post_id,
                    'title': title[:50],
                    'status': 'error'
                })
        except Exception as e:
            results['errors'] += 1
            results['details'].append({
                'id': post_id,
                'title': title[:50],
                'status': 'error',
                'error': str(e)
            })

    return results


def get_analysis_stats() -> Dict:
    """Get statistics about article analysis status."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(ai_analisado_em) as analyzed,
                COUNT(*) - COUNT(ai_analisado_em) as pending,
                COUNT(DISTINCT ai_categoria) as categorias,
                AVG(ai_score_relevancia) as avg_score
            FROM editorial_posts
        """)
        stats = dict(cursor.fetchone())

        # Get category distribution
        cursor.execute("""
            SELECT ai_categoria, COUNT(*) as count
            FROM editorial_posts
            WHERE ai_categoria IS NOT NULL
            GROUP BY ai_categoria
            ORDER BY count DESC
        """)
        stats['por_categoria'] = [dict(r) for r in cursor.fetchall()]

        # Get top scoring articles
        cursor.execute("""
            SELECT id, article_title, ai_categoria, ai_score_relevancia, ai_gancho_linkedin
            FROM editorial_posts
            WHERE ai_score_relevancia IS NOT NULL
            ORDER BY ai_score_relevancia DESC
            LIMIT 10
        """)
        stats['top_relevancia'] = [dict(r) for r in cursor.fetchall()]

        return stats


def get_pending_tasks() -> Dict:
    """
    Get pending editorial tasks for today and upcoming.
    Returns a structured view of what needs to be done.
    """
    from services.hot_takes import get_hot_takes

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    next_week = today + timedelta(days=7)

    tasks = {
        'hoje': [],
        'amanha': [],
        'proximos_dias': [],
        'sugestoes_repost': [],
        'resumo': {
            'publicar_hoje': 0,
            'publicar_amanha': 0,
            'total_pendente': 0,
            'artigos_para_repost': 0
        }
    }

    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Posts agendados para HOJE (precisam ser publicados no LinkedIn)
        cursor.execute("""
            SELECT ep.*, ht.linkedin_post, ht.news_title, ht.news_link
            FROM editorial_posts ep
            LEFT JOIN hot_takes ht ON ep.hot_take_id = ht.id
            WHERE ep.status = 'scheduled'
              AND DATE(ep.data_publicacao) = DATE(%s)
            ORDER BY ep.data_publicacao ASC
        """, (today,))

        for row in cursor.fetchall():
            post = dict(row)
            tasks['hoje'].append({
                'id': post['id'],
                'tipo': 'hot_take' if post.get('hot_take_id') else 'post',
                'titulo': post.get('article_title') or post.get('news_title') or 'Sem título',
                'horario': post.get('data_publicacao'),
                'canal': post.get('canal', 'linkedin'),
                'conteudo': post.get('conteudo_adaptado') or post.get('linkedin_post') or '',
                'hashtags': post.get('hashtags') or '',
                'link_original': post.get('article_url') or post.get('news_link') or '',
                'acao': 'Publicar no LinkedIn'
            })

        tasks['resumo']['publicar_hoje'] = len(tasks['hoje'])

        # 2. Posts agendados para AMANHÃ
        cursor.execute("""
            SELECT ep.*, ht.linkedin_post, ht.news_title
            FROM editorial_posts ep
            LEFT JOIN hot_takes ht ON ep.hot_take_id = ht.id
            WHERE ep.status = 'scheduled'
              AND DATE(ep.data_publicacao) = DATE(%s)
            ORDER BY ep.data_publicacao ASC
        """, (tomorrow,))

        for row in cursor.fetchall():
            post = dict(row)
            tasks['amanha'].append({
                'id': post['id'],
                'tipo': 'hot_take' if post.get('hot_take_id') else 'post',
                'titulo': post.get('article_title') or post.get('news_title') or 'Sem título',
                'horario': post.get('data_publicacao'),
                'canal': post.get('canal', 'linkedin'),
                'acao': 'Preparar para publicação'
            })

        tasks['resumo']['publicar_amanha'] = len(tasks['amanha'])

        # 3. Próximos 7 dias (exceto hoje e amanhã)
        cursor.execute("""
            SELECT ep.*, ht.news_title
            FROM editorial_posts ep
            LEFT JOIN hot_takes ht ON ep.hot_take_id = ht.id
            WHERE ep.status = 'scheduled'
              AND DATE(ep.data_publicacao) > DATE(%s)
              AND DATE(ep.data_publicacao) <= DATE(%s)
            ORDER BY ep.data_publicacao ASC
        """, (tomorrow, next_week))

        for row in cursor.fetchall():
            post = dict(row)
            tasks['proximos_dias'].append({
                'id': post['id'],
                'tipo': 'hot_take' if post.get('hot_take_id') else 'post',
                'titulo': post.get('article_title') or post.get('news_title') or 'Sem título',
                'data': post.get('data_publicacao'),
                'canal': post.get('canal', 'linkedin')
            })

        # 4. Sugestões de artigos para repost (baseado em score IA)
        cursor.execute("""
            SELECT id, article_title, article_url, ai_categoria,
                   ai_score_relevancia, ai_gancho_linkedin, ai_evergreen,
                   criado_em
            FROM editorial_posts
            WHERE status = 'draft'
              AND ai_score_relevancia >= 7
              AND ai_gancho_linkedin IS NOT NULL
            ORDER BY ai_score_relevancia DESC, ai_evergreen DESC
            LIMIT 5
        """)

        for row in cursor.fetchall():
            post = dict(row)
            tasks['sugestoes_repost'].append({
                'id': post['id'],
                'titulo': post['article_title'],
                'url': post.get('article_url'),
                'categoria': post.get('ai_categoria'),
                'score': post.get('ai_score_relevancia'),
                'gancho': post.get('ai_gancho_linkedin'),
                'evergreen': post.get('ai_evergreen', False),
                'acao': 'Agendar repost'
            })

        tasks['resumo']['artigos_para_repost'] = len(tasks['sugestoes_repost'])
        tasks['resumo']['total_pendente'] = (
            tasks['resumo']['publicar_hoje'] +
            tasks['resumo']['publicar_amanha'] +
            len(tasks['proximos_dias'])
        )

    return tasks
