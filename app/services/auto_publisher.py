"""
Auto Publisher - Selecao automatica e publicacao de posts no LinkedIn.

Fluxo:
1. Cron semanal (domingo): IA seleciona 3-5 posts da semana
2. Cria proposta de acao para aprovacao
3. Apos aprovacao: agenda posts com datas/horas
4. Cron diario: publica posts agendados cuja hora chegou
"""
import os
import json
import logging
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def select_weekly_posts(posts_per_week: int = 4) -> Dict:
    """
    IA seleciona os melhores posts para a semana.
    Mistura hot-takes e reposts de artigos para diversidade.
    Chamado pelo cron no domingo.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY nao configurada"}

    # Collect candidates from both sources
    candidates = []

    with get_db() as conn:
        cursor = conn.cursor()

        # Hot-takes com texto pronto
        cursor.execute("""
            SELECT id, 'hot_take' as source, news_title as titulo,
                   linkedin_post as conteudo, hashtags,
                   created_at as criado_em
            FROM hot_takes
            WHERE status = 'draft' AND linkedin_post IS NOT NULL
            ORDER BY created_at DESC LIMIT 20
        """)
        for r in cursor.fetchall():
            row = dict(r)
            row['conteudo_preview'] = (row['conteudo'] or '')[:300]
            candidates.append(row)

        # Editorial posts (reposts + hot_takes) com conteudo adaptado
        cursor.execute("""
            SELECT id, 'editorial' as source, COALESCE(titulo_adaptado, article_title) as titulo,
                   conteudo_adaptado as conteudo, hashtags, tipo,
                   ai_score_relevancia as score, criado_em
            FROM editorial_posts
            WHERE status = 'draft' AND conteudo_adaptado IS NOT NULL
                AND LENGTH(conteudo_adaptado) > 50
            ORDER BY ai_score_relevancia DESC NULLS LAST, criado_em DESC
            LIMIT 20
        """)
        for r in cursor.fetchall():
            row = dict(r)
            row['conteudo_preview'] = (row['conteudo'] or '')[:300]
            candidates.append(row)

    if not candidates:
        return {"error": "Nenhum post disponivel para agendar", "candidates": 0}

    # Ask AI to select the best mix
    candidates_text = "\n".join([
        f"[{i+1}] ({c['source']}) {c['titulo']}\n    Preview: {c['conteudo_preview']}"
        for i, c in enumerate(candidates)
    ])

    prompt = f"""Voce e um estrategista de conteudo LinkedIn para Renato Almeida Prado,
executivo de tecnologia e governanca corporativa.

Selecione os {posts_per_week} melhores posts para publicar esta semana.

CANDIDATOS:
{candidates_text}

CRITERIOS:
- Diversidade de tema (nao repetir assuntos similares)
- Relevancia para o perfil (IA, governanca, negocios, empreendedorismo)
- Mix de formatos (hot-takes opinativos + reposts informativos)
- Atualidade (preferir temas recentes)

Responda APENAS com JSON:
{{"selections": [
    {{"index": 1, "reason": "motivo curto"}},
    {{"index": 3, "reason": "motivo curto"}}
]}}"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}
            )

        if resp.status_code != 200:
            return {"error": f"API error: {resp.status_code}"}

        text = resp.json()["content"][0]["text"]

        # Parse JSON
        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0:
            return {"error": "IA nao retornou JSON valido"}

        result = json.loads(text[start:end])
        selections = result.get("selections", [])

        selected = []
        for s in selections:
            idx = s.get("index", 0) - 1
            if 0 <= idx < len(candidates):
                c = candidates[idx]
                c['reason'] = s.get('reason', '')
                selected.append(c)

        return {"selected": selected, "total_candidates": len(candidates)}

    except Exception as e:
        return {"error": str(e)}


def schedule_selected_posts(selected: List[Dict], start_date: date = None) -> Dict:
    """
    Agenda os posts selecionados nos melhores horarios da semana.
    Ter/Qua/Qui/Sex as 9h ou 12h.
    """
    if start_date is None:
        # Next Monday
        today = date.today()
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        start_date = today + timedelta(days=days_until_monday)

    # LinkedIn best times: Tue-Thu 9h, 12h
    slots = []
    for day_offset in [1, 2, 3, 4]:  # Tue, Wed, Thu, Fri
        d = start_date + timedelta(days=day_offset)
        slots.append(datetime(d.year, d.month, d.day, 9, 0))
        slots.append(datetime(d.year, d.month, d.day, 12, 0))

    scheduled = []
    with get_db() as conn:
        cursor = conn.cursor()

        for i, post in enumerate(selected):
            if i >= len(slots):
                break
            slot = slots[i]

            if post['source'] == 'hot_take':
                cursor.execute("""
                    UPDATE hot_takes SET status = 'scheduled', scheduled_for = %s
                    WHERE id = %s
                """, (slot, post['id']))
            else:
                cursor.execute("""
                    UPDATE editorial_posts SET status = 'scheduled', data_publicacao = %s
                    WHERE id = %s
                """, (slot, post['id']))

            scheduled.append({
                'id': post['id'],
                'source': post['source'],
                'titulo': post['titulo'],
                'scheduled_for': slot.isoformat(),
                'reason': post.get('reason', '')
            })

        conn.commit()

    return {"scheduled": scheduled}


async def publish_due_posts() -> Dict:
    """
    Publica posts cuja hora de agendamento ja passou.
    Chamado pelo cron a cada hora ou no daily-sync.
    """
    from integrations.linkedin_posting import get_stored_token, publish_post

    token = get_stored_token()
    if not token:
        return {"skipped": "LinkedIn nao conectado"}

    now = datetime.now()
    results = {"published": 0, "errors": 0, "posts": []}

    with get_db() as conn:
        cursor = conn.cursor()

        # Hot-takes agendados cuja hora chegou
        cursor.execute("""
            SELECT id, linkedin_post as conteudo, news_link as article_url
            FROM hot_takes
            WHERE status = 'scheduled' AND scheduled_for <= %s
            ORDER BY scheduled_for ASC
        """, (now,))
        due_hot_takes = [dict(r) for r in cursor.fetchall()]

        # Editorial posts agendados cuja hora chegou
        cursor.execute("""
            SELECT id, conteudo_adaptado as conteudo, article_url,
                   hashtags
            FROM editorial_posts
            WHERE status = 'scheduled' AND data_publicacao <= %s
            ORDER BY data_publicacao ASC
        """, (now,))
        due_editorials = [dict(r) for r in cursor.fetchall()]

    # Publish each
    for ht in due_hot_takes:
        text = ht['conteudo'] or ''
        if not text:
            continue
        result = await publish_post(text, ht.get('article_url'))
        if result.get('success'):
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE hot_takes SET status = 'published', published_at = NOW(),
                           linkedin_url = %s
                    WHERE id = %s
                """, (result.get('post_url', ''), ht['id']))
                conn.commit()
            results["published"] += 1
            results["posts"].append({"id": ht['id'], "source": "hot_take", "url": result.get('post_url')})
        else:
            results["errors"] += 1
            logger.error(f"Failed to publish hot_take {ht['id']}: {result.get('error')}")

    for ep in due_editorials:
        text = ep['conteudo'] or ''
        hashtags = ep.get('hashtags', [])
        if isinstance(hashtags, list):
            hashtags = ' '.join(hashtags)
        elif isinstance(hashtags, str):
            try:
                h = json.loads(hashtags)
                hashtags = ' '.join(h) if isinstance(h, list) else hashtags
            except Exception:
                pass
        full_text = text + ('\n\n' + hashtags if hashtags else '')
        if not full_text.strip():
            continue

        result = await publish_post(full_text, ep.get('article_url'))
        if result.get('success'):
            from services.editorial_calendar import mark_as_published
            mark_as_published(ep['id'], url_publicado=result.get('post_url', ''))
            results["published"] += 1
            results["posts"].append({"id": ep['id'], "source": "editorial", "url": result.get('post_url')})
        else:
            results["errors"] += 1
            logger.error(f"Failed to publish editorial {ep['id']}: {result.get('error')}")

    return results
