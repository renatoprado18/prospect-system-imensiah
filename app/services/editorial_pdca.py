"""
Editorial PDCA Service
Weekly briefing generation, funnel analytics, and content strategy for LinkedIn.
Pillar-based system: NeoGovernanca, IA aplicada, Bastidores/resultados reais.
"""
import os
import json
import httpx
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from database import get_db

logger = logging.getLogger(__name__)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Content pillars
PILLARS = {
    "neogovernanca": {
        "label": "NeoGovernanca",
        "description": "Atrai CEOs e conselheiros → clientes ImenSIAH",
        "keywords": ["governanca", "conselho", "board", "compliance", "esg", "neogovernanca"],
    },
    "ia_negocios": {
        "label": "IA aplicada a negocios",
        "description": "Atrai decisores → clientes consultoria",
        "keywords": ["ia", "inteligencia artificial", "ai", "automacao", "transformacao digital", "tecnologia"],
    },
    "bastidores": {
        "label": "Bastidores/resultados reais",
        "description": "Constroi confianca → conversao",
        "keywords": ["bastidor", "resultado", "case", "historia", "pessoal", "aprendizado"],
    },
}


def _classify_pillar(post: Dict) -> str:
    """Classify a post into one of the three pillars based on title/category."""
    title = (post.get("article_title") or "").lower()
    category = (post.get("ai_categoria") or "").lower()
    tags_raw = post.get("tags") or "[]"
    if isinstance(tags_raw, str):
        try:
            tags = json.loads(tags_raw)
        except (json.JSONDecodeError, TypeError):
            tags = []
    else:
        tags = tags_raw
    tags_str = " ".join(str(t).lower() for t in tags)

    text = f"{title} {category} {tags_str}"

    for pillar_key, pillar in PILLARS.items():
        for kw in pillar["keywords"]:
            if kw in text:
                return pillar_key

    # Default based on category
    if "governan" in category or "conselho" in category:
        return "neogovernanca"
    if "digital" in category or "inova" in category:
        return "ia_negocios"

    return "bastidores"


def get_last_week_performance() -> Dict:
    """Get editorial performance metrics from the last 7 days."""
    with get_db() as conn:
        cursor = conn.cursor()

        seven_days_ago = datetime.now() - timedelta(days=7)

        # Published posts from last 7 days
        cursor.execute("""
            SELECT id, article_title, ai_categoria, tags, status,
                   data_publicado, hot_take_id,
                   linkedin_impressoes, linkedin_reacoes,
                   linkedin_comentarios, linkedin_compartilhamentos,
                   linkedin_cliques
            FROM editorial_posts
            WHERE status = 'published'
              AND data_publicado >= %s
            ORDER BY data_publicado DESC
        """, (seven_days_ago,))
        posts = [dict(p) for p in cursor.fetchall()]

        total_impressions = sum(p.get("linkedin_impressoes") or 0 for p in posts)
        total_reactions = sum(p.get("linkedin_reacoes") or 0 for p in posts)
        total_comments = sum(p.get("linkedin_comentarios") or 0 for p in posts)
        total_shares = sum(p.get("linkedin_compartilhamentos") or 0 for p in posts)
        total_clicks = sum(p.get("linkedin_cliques") or 0 for p in posts)
        total_engagement = total_reactions + total_comments + total_shares

        # By pillar
        by_pillar = {}
        for p in posts:
            pillar = _classify_pillar(p)
            if pillar not in by_pillar:
                by_pillar[pillar] = {"posts": 0, "impressions": 0, "engagement": 0}
            by_pillar[pillar]["posts"] += 1
            by_pillar[pillar]["impressions"] += (p.get("linkedin_impressoes") or 0)
            by_pillar[pillar]["engagement"] += (
                (p.get("linkedin_reacoes") or 0)
                + (p.get("linkedin_comentarios") or 0)
                + (p.get("linkedin_compartilhamentos") or 0)
            )

        # Best performing post
        best = None
        if posts:
            best = max(posts, key=lambda p: (p.get("linkedin_impressoes") or 0))

        return {
            "period": f"{seven_days_ago.strftime('%d/%m')} - {datetime.now().strftime('%d/%m')}",
            "posts_published": len(posts),
            "total_impressions": total_impressions,
            "total_reactions": total_reactions,
            "total_comments": total_comments,
            "total_shares": total_shares,
            "total_clicks": total_clicks,
            "total_engagement": total_engagement,
            "by_pillar": by_pillar,
            "best_post": {
                "id": best["id"],
                "title": best.get("article_title", ""),
                "impressions": best.get("linkedin_impressoes") or 0,
            } if best else None,
            "posts": posts,
        }


async def generate_weekly_briefing() -> Dict:
    """
    Generate a weekly editorial briefing using Claude AI.
    Creates tasks and saves as project note.
    """
    performance = get_last_week_performance()

    # Build prompt for Claude
    pillar_summary = ""
    for pk, pv in performance["by_pillar"].items():
        label = PILLARS.get(pk, {}).get("label", pk)
        avg_eng = pv["engagement"] / max(pv["posts"], 1)
        pillar_summary += f"- {label}: {pv['posts']} posts, {pv['impressions']} impressoes, {avg_eng:.0f} engajamento medio\n"

    if not pillar_summary:
        pillar_summary = "- Nenhum post publicado na semana anterior\n"

    best_info = ""
    if performance.get("best_post"):
        bp = performance["best_post"]
        best_info = f"Melhor post: \"{bp['title'][:60]}\" com {bp['impressions']} impressoes."

    prompt = f"""Voce e o estrategista editorial de Renato Prado, consultor de governanca corporativa e cofundador do ImenSIAH.
Ele publica no LinkedIn: 2 hot takes + 1 artigo por semana.

PERFORMANCE DA SEMANA ANTERIOR ({performance['period']}):
- Posts publicados: {performance['posts_published']}
- Impressoes totais: {performance['total_impressions']}
- Engajamento total (reacoes+comentarios+compartilhamentos): {performance['total_engagement']}
- Cliques: {performance['total_clicks']}
{best_info}

POR PILAR:
{pillar_summary}

PILARES:
1. NeoGovernanca - Atrai CEOs/conselheiros, gera clientes para ImenSIAH
2. IA aplicada a negocios - Atrai decisores, gera clientes de consultoria
3. Bastidores/resultados reais - Constroi confianca, gera conversao

GERE o plano editorial da proxima semana em JSON:
{{
    "analise_semana": "<3-4 frases analisando a performance da semana>",
    "recomendacoes": ["<recomendacao 1>", "<recomendacao 2>", "<recomendacao 3>"],
    "posts_sugeridos": [
        {{
            "tipo": "hot_take",
            "dia": "segunda",
            "pilar": "neogovernanca|ia_negocios|bastidores",
            "tema": "<tema especifico sugerido>",
            "gancho": "<primeira frase do post para gerar curiosidade, max 120 chars>",
            "horario_sugerido": "08:30"
        }},
        {{
            "tipo": "artigo",
            "dia": "quarta",
            "pilar": "neogovernanca|ia_negocios|bastidores",
            "tema": "<tema especifico sugerido>",
            "gancho": "<primeira frase do post para gerar curiosidade, max 120 chars>",
            "horario_sugerido": "09:00"
        }},
        {{
            "tipo": "hot_take",
            "dia": "quinta",
            "pilar": "neogovernanca|ia_negocios|bastidores",
            "tema": "<tema especifico sugerido>",
            "gancho": "<primeira frase do post para gerar curiosidade, max 120 chars>",
            "horario_sugerido": "08:30"
        }}
    ],
    "pilar_foco": "<qual pilar priorizar esta semana e por que>"
}}

Responda APENAS com o JSON."""

    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not configured"}

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
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

            if response.status_code != 200:
                logger.error(f"AI API error: {response.status_code} - {response.text}")
                return {"error": f"AI API error: {response.status_code}"}

            result = response.json()
            content = result.get("content", [{}])[0].get("text", "").strip()

            # Parse JSON response
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]

            briefing = json.loads(content)

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response: {e}")
        return {"error": f"JSON parse error: {e}"}
    except Exception as e:
        logger.error(f"AI briefing error: {e}")
        return {"error": str(e)}

    # Calculate next week dates
    today = datetime.now()
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_monday = today + timedelta(days=days_until_monday)

    day_map = {
        "segunda": 0, "terca": 1, "quarta": 2, "quinta": 3,
        "sexta": 4, "sabado": 5, "domingo": 6,
    }

    # Create tasks in project_id=22
    created_tasks = []
    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Create post tasks from AI suggestions
        for post_plan in briefing.get("posts_sugeridos", []):
            dia = post_plan.get("dia", "segunda").lower()
            day_offset = day_map.get(dia, 0)
            task_date = next_monday + timedelta(days=day_offset)

            # Parse suggested time
            horario = post_plan.get("horario_sugerido", "09:00")
            try:
                h, m = horario.split(":")
                task_datetime = task_date.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            except (ValueError, AttributeError):
                task_datetime = task_date.replace(hour=9, minute=0, second=0, microsecond=0)

            tipo_label = "hot take" if post_plan.get("tipo") == "hot_take" else "artigo"
            tema = post_plan.get("tema", "tema a definir")
            pilar_label = PILLARS.get(post_plan.get("pilar", ""), {}).get("label", post_plan.get("pilar", ""))

            cursor.execute("""
                INSERT INTO tasks (
                    titulo, descricao, project_id, contact_id,
                    data_vencimento, prioridade, ai_generated, origem,
                    tags, status
                ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'editorial_briefing', %s, 'pending')
                RETURNING id
            """, (
                f"Publicar {tipo_label}: {tema[:80]}",
                f"Pilar: {pilar_label}\nGancho: {post_plan.get('gancho', '')}\nHorario sugerido: {horario}",
                22,  # project_id
                14911,  # contact_id (Renato)
                task_datetime,
                8,  # high priority
                json.dumps(["editorial", post_plan.get("pilar", ""), tipo_label]),
            ))
            task = cursor.fetchone()
            created_tasks.append({"id": task["id"], "titulo": f"Publicar {tipo_label}: {tema[:80]}"})

        # 2. Create "Medir metricas" task for Saturday
        saturday = next_monday + timedelta(days=5)
        cursor.execute("""
            INSERT INTO tasks (
                titulo, descricao, project_id, contact_id,
                data_vencimento, prioridade, ai_generated, origem,
                tags, status
            ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'editorial_briefing', %s, 'pending')
            RETURNING id
        """, (
            "Medir metricas: posts da semana",
            "Acesse o LinkedIn Analytics e atualize impressoes, reacoes, comentarios e cliques de cada post da semana.",
            22, 14911,
            saturday.replace(hour=10, minute=0, second=0, microsecond=0),
            5,
            json.dumps(["editorial", "metricas"]),
        ))
        task = cursor.fetchone()
        created_tasks.append({"id": task["id"], "titulo": "Medir metricas: posts da semana"})

        # 3. Create "Responder comentarios" task for Friday
        friday = next_monday + timedelta(days=4)
        cursor.execute("""
            INSERT INTO tasks (
                titulo, descricao, project_id, contact_id,
                data_vencimento, prioridade, ai_generated, origem,
                tags, status
            ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'editorial_briefing', %s, 'pending')
            RETURNING id
        """, (
            "Responder todos os comentarios",
            "Revise todos os comentarios dos posts da semana e responda cada um. Engajamento direto gera conexoes e reunioes.",
            22, 14911,
            friday.replace(hour=17, minute=0, second=0, microsecond=0),
            6,
            json.dumps(["editorial", "engajamento"]),
        ))
        task = cursor.fetchone()
        created_tasks.append({"id": task["id"], "titulo": "Responder todos os comentarios"})

        # 4. Save briefing as project note
        briefing_content = f"""## Briefing Editorial Semanal - {next_monday.strftime('%d/%m/%Y')}

### Analise da Semana Anterior
{briefing.get('analise_semana', 'Sem dados suficientes.')}

### Recomendacoes
"""
        for rec in briefing.get("recomendacoes", []):
            briefing_content += f"- {rec}\n"

        briefing_content += f"\n### Pilar Foco\n{briefing.get('pilar_foco', 'A definir')}\n"

        briefing_content += "\n### Posts Planejados\n"
        for post_plan in briefing.get("posts_sugeridos", []):
            pilar_label = PILLARS.get(post_plan.get("pilar", ""), {}).get("label", post_plan.get("pilar", ""))
            briefing_content += f"- **{post_plan.get('dia', '?').title()}** ({post_plan.get('tipo', '?')}): {post_plan.get('tema', '?')} [{pilar_label}]\n"
            briefing_content += f"  Gancho: _{post_plan.get('gancho', '')}_\n"

        briefing_content += f"\n### Performance\n"
        briefing_content += f"- Posts: {performance['posts_published']}\n"
        briefing_content += f"- Impressoes: {performance['total_impressions']}\n"
        briefing_content += f"- Engajamento: {performance['total_engagement']}\n"

        cursor.execute("""
            INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (
            22,
            "editorial_briefing",
            f"Briefing Editorial - Semana {next_monday.strftime('%d/%m')}",
            briefing_content,
            "IA",
        ))
        note = cursor.fetchone()

        conn.commit()

    return {
        "status": "success",
        "briefing": briefing,
        "performance": {
            "period": performance["period"],
            "posts_published": performance["posts_published"],
            "total_impressions": performance["total_impressions"],
            "total_engagement": performance["total_engagement"],
        },
        "tasks_created": created_tasks,
        "note_id": note["id"] if note else None,
    }


def get_editorial_funnel() -> Dict:
    """
    Get editorial funnel data: Posts -> Impressions -> Engagement -> Messages -> Meetings.
    Returns current week, monthly trend, by pillar, best performing, and recommendations.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        today = datetime.now()
        # Current week (Monday-Sunday)
        days_since_monday = today.weekday()
        monday = (today - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

        # Current week posts
        cursor.execute("""
            SELECT id, article_title, ai_categoria, tags, status,
                   data_publicacao, data_publicado, hot_take_id,
                   linkedin_impressoes, linkedin_reacoes,
                   linkedin_comentarios, linkedin_compartilhamentos,
                   linkedin_cliques
            FROM editorial_posts
            WHERE (
                (status = 'published' AND data_publicado >= %s AND data_publicado <= %s)
                OR (status = 'scheduled' AND data_publicacao >= %s AND data_publicacao <= %s)
            )
            ORDER BY COALESCE(data_publicado, data_publicacao) ASC
        """, (monday, sunday, monday, sunday))
        week_posts = [dict(p) for p in cursor.fetchall()]

        published = [p for p in week_posts if p["status"] == "published"]
        planned = len(week_posts)
        impressions = sum(p.get("linkedin_impressoes") or 0 for p in published)
        engagement = sum(
            (p.get("linkedin_reacoes") or 0)
            + (p.get("linkedin_comentarios") or 0)
            + (p.get("linkedin_compartilhamentos") or 0)
            for p in published
        )

        # Count messages received this week (from WhatsApp/LinkedIn context)
        cursor.execute("""
            SELECT COUNT(*) as cnt FROM messages
            WHERE direction = 'incoming'
              AND criado_em >= %s AND criado_em <= %s
              AND (channel = 'linkedin' OR contexto LIKE '%%linkedin%%')
        """, (monday, sunday))
        row = cursor.fetchone()
        messages_received = row["cnt"] if row else 0

        # Meetings booked this week related to editorial/LinkedIn
        cursor.execute("""
            SELECT COUNT(*) as cnt FROM calendar_events
            WHERE start_datetime >= %s AND start_datetime <= %s
              AND (summary ILIKE '%%reuniao%%' OR summary ILIKE '%%meeting%%' OR summary ILIKE '%%call%%')
        """, (monday, sunday))
        row = cursor.fetchone()
        meetings_booked = row["cnt"] if row else 0

        current_week = {
            "week_start": monday.strftime("%Y-%m-%d"),
            "posts_published": len(published),
            "posts_planned": planned,
            "impressions": impressions,
            "engagement": engagement,
            "profile_visits": None,  # manual input for now
            "messages_received": messages_received,
            "meetings_booked": meetings_booked,
        }

        # Monthly trend: last 4 weeks
        monthly_trend = []
        for weeks_ago in range(3, -1, -1):
            w_monday = monday - timedelta(weeks=weeks_ago)
            w_sunday = w_monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

            cursor.execute("""
                SELECT
                    COUNT(*) as posts,
                    COALESCE(SUM(linkedin_impressoes), 0) as impressions,
                    COALESCE(SUM(COALESCE(linkedin_reacoes,0) + COALESCE(linkedin_comentarios,0) + COALESCE(linkedin_compartilhamentos,0)), 0) as engagement
                FROM editorial_posts
                WHERE status = 'published'
                  AND data_publicado >= %s AND data_publicado <= %s
            """, (w_monday, w_sunday))
            row = dict(cursor.fetchone())

            # Meetings for that week
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM calendar_events
                WHERE start_datetime >= %s AND start_datetime <= %s
                  AND (summary ILIKE '%%reuniao%%' OR summary ILIKE '%%meeting%%' OR summary ILIKE '%%call%%')
            """, (w_monday, w_sunday))
            meetings_row = cursor.fetchone()

            monthly_trend.append({
                "week": w_monday.strftime("%Y-%m-%d"),
                "posts": row["posts"],
                "impressions": row["impressions"],
                "engagement": row["engagement"],
                "meetings": meetings_row["cnt"] if meetings_row else 0,
            })

        # By pillar (last 30 days)
        thirty_days_ago = today - timedelta(days=30)
        cursor.execute("""
            SELECT id, article_title, ai_categoria, tags,
                   linkedin_impressoes, linkedin_reacoes,
                   linkedin_comentarios, linkedin_compartilhamentos
            FROM editorial_posts
            WHERE status = 'published' AND data_publicado >= %s
        """, (thirty_days_ago,))
        recent_posts = [dict(p) for p in cursor.fetchall()]

        by_pillar = {}
        for p in recent_posts:
            pillar = _classify_pillar(p)
            if pillar not in by_pillar:
                by_pillar[pillar] = {"posts": 0, "total_engagement": 0, "total_impressions": 0}
            by_pillar[pillar]["posts"] += 1
            by_pillar[pillar]["total_engagement"] += (
                (p.get("linkedin_reacoes") or 0)
                + (p.get("linkedin_comentarios") or 0)
                + (p.get("linkedin_compartilhamentos") or 0)
            )
            by_pillar[pillar]["total_impressions"] += (p.get("linkedin_impressoes") or 0)

        by_pillar_formatted = {}
        for pk, pv in by_pillar.items():
            by_pillar_formatted[pk] = {
                "label": PILLARS.get(pk, {}).get("label", pk),
                "posts": pv["posts"],
                "avg_engagement": round(pv["total_engagement"] / max(pv["posts"], 1), 1),
                "avg_impressions": round(pv["total_impressions"] / max(pv["posts"], 1), 1),
            }

        # Best performing post (last 30 days)
        best_performing = None
        if recent_posts:
            best = max(recent_posts, key=lambda p: (p.get("linkedin_impressoes") or 0))
            if best.get("linkedin_impressoes"):
                best_performing = {
                    "post_id": best["id"],
                    "title": best.get("article_title", ""),
                    "impressions": best.get("linkedin_impressoes") or 0,
                    "pillar": _classify_pillar(best),
                }

        # Generate simple recommendations
        recommendations = []
        if by_pillar:
            # Find best engagement pillar
            best_pillar = max(by_pillar.items(), key=lambda x: x[1]["total_engagement"] / max(x[1]["posts"], 1))
            worst_pillar = min(by_pillar.items(), key=lambda x: x[1]["total_engagement"] / max(x[1]["posts"], 1))
            best_label = PILLARS.get(best_pillar[0], {}).get("label", best_pillar[0])
            worst_label = PILLARS.get(worst_pillar[0], {}).get("label", worst_pillar[0])

            if best_pillar[0] != worst_pillar[0]:
                best_avg = best_pillar[1]["total_engagement"] / max(best_pillar[1]["posts"], 1)
                worst_avg = worst_pillar[1]["total_engagement"] / max(worst_pillar[1]["posts"], 1)
                if worst_avg > 0:
                    ratio = best_avg / worst_avg
                    recommendations.append(
                        f"Pilar '{best_label}' tem {ratio:.1f}x mais engajamento que '{worst_label}'"
                    )

        if len(monthly_trend) >= 2:
            prev = monthly_trend[-2]
            curr = monthly_trend[-1]
            if prev["impressions"] > 0 and curr["impressions"] > 0:
                change = ((curr["impressions"] - prev["impressions"]) / prev["impressions"]) * 100
                direction = "subiu" if change > 0 else "caiu"
                recommendations.append(f"Impressoes {direction} {abs(change):.0f}% esta semana vs anterior")

        if impressions > 0 and engagement > 0:
            eng_rate = (engagement / impressions) * 100
            recommendations.append(f"Taxa de engajamento atual: {eng_rate:.1f}%")

        missing_pillars = set(PILLARS.keys()) - set(by_pillar.keys())
        for mp in missing_pillars:
            label = PILLARS[mp]["label"]
            recommendations.append(f"Pilar '{label}' sem posts nos ultimos 30 dias")

        return {
            "current_week": current_week,
            "monthly_trend": monthly_trend,
            "by_pillar": by_pillar_formatted,
            "best_performing": best_performing,
            "recommendations": recommendations,
        }
