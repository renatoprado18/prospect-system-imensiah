"""Auto-resolver de tasks geradas pelo briefing semanal editorial.

Toda segunda 8h o editorial weekly briefing cria 2 tasks operacionais:
- "Medir metricas: posts da semana" (sabado)
- "Responder todos os comentarios" (sexta)

Essas tasks viram zumbis: ficam pending depois que o LinkdAPI ja coletou
as metricas via auto-collect-linkedin-metrics (cron horario) ou depois
que o Renato ja respondeu os comentarios. Aqui a gente fecha
automaticamente:

- "Medir metricas: posts da semana": se TODOS os posts publicados na
  semana da task ja tem editorial_metrics_history com
  dias_apos_publicacao >= 7 (janela 168h coletada).

- "Responder todos os comentarios": NAO RESOLVIDO AQUI — nao temos
  tracking confiavel de respostas a comentarios. Permanece pra Renato.
  (Bloco backlog: Fase 2.)

Cron diario: GET /api/cron/auto-resolve-editorial (12:30 BRT = 15:30 UTC).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, List

from database import get_db

logger = logging.getLogger(__name__)


def _semana_da_task(data_criacao, data_vencimento) -> tuple:
    """Infere intervalo (segunda, domingo) da semana referenciada pela task.

    Convencao: editorial_pdca cria task com data_vencimento = sabado/sexta
    da semana corrente. Semana da task = (segunda dessa semana, domingo).
    """
    ref = data_vencimento or data_criacao
    if ref is None:
        return (None, None)
    # weekday(): segunda=0, domingo=6
    monday = ref.date() - timedelta(days=ref.weekday())
    sunday = monday + timedelta(days=6)
    return (monday, sunday)


def auto_resolve_editorial_tasks() -> Dict:
    """Fecha tasks 'Medir metricas: posts da semana' cujas metricas ja foram
    coletadas (LinkdAPI auto-collect com dias_apos_publicacao >= 7).

    Retorna {checked, resolved, resolved_ids, skipped_no_posts, skipped_partial}.
    """
    stats = {
        "checked": 0,
        "resolved": 0,
        "resolved_ids": [],
        "skipped_no_posts": 0,
        "skipped_partial": 0,
    }

    with get_db() as conn:
        cur = conn.cursor()
        # 1. Busca tasks pending 'Medir metricas%'
        cur.execute(
            """
            SELECT id, titulo, descricao, data_criacao, data_vencimento, project_id
            FROM tasks
            WHERE status = 'pending'
              AND (titulo ILIKE 'Medir metricas%' OR titulo ILIKE 'Medir m_tricas%')
            ORDER BY data_vencimento ASC NULLS LAST
            """
        )
        candidates = [dict(r) for r in cur.fetchall()]
        stats["checked"] = len(candidates)

        for task in candidates:
            monday, sunday = _semana_da_task(task["data_criacao"], task["data_vencimento"])
            if monday is None:
                stats["skipped_partial"] += 1
                continue

            # Posts publicados na semana
            cur.execute(
                """
                SELECT id, data_publicado
                FROM editorial_posts
                WHERE status IN ('published','publicado','posted')
                  AND data_publicado IS NOT NULL
                  AND data_publicado::date >= %s
                  AND data_publicado::date <= %s
                """,
                (monday, sunday),
            )
            posts = [dict(r) for r in cur.fetchall()]
            if not posts:
                stats["skipped_no_posts"] += 1
                continue

            # Pra cada post, ja tem metric com dias_apos_publicacao >= 7?
            post_ids = [p["id"] for p in posts]
            cur.execute(
                """
                SELECT DISTINCT post_id
                FROM editorial_metrics_history
                WHERE post_id = ANY(%s)
                  AND dias_apos_publicacao >= 7
                """,
                (post_ids,),
            )
            posts_with_metrics = {r["post_id"] for r in cur.fetchall()}
            missing = [pid for pid in post_ids if pid not in posts_with_metrics]
            if missing:
                stats["skipped_partial"] += 1
                logger.info(
                    f"auto_resolve_editorial: task #{task['id']} skip — "
                    f"{len(missing)}/{len(post_ids)} posts sem metrics 7d ainda"
                )
                continue

            # Fecha a task
            cur.execute(
                """
                UPDATE tasks SET status='completed', data_conclusao=NOW()
                WHERE id=%s AND status='pending'
                """,
                (task["id"],),
            )
            if cur.rowcount > 0:
                stats["resolved"] += 1
                stats["resolved_ids"].append(task["id"])
                logger.info(
                    f"auto_resolve_editorial: task #{task['id']} '{task['titulo']}' "
                    f"resolvida (semana {monday}-{sunday}, {len(post_ids)} posts c/ metrics 7d)"
                )

        conn.commit()

    return stats
