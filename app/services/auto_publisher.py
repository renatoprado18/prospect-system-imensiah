"""
Auto Publisher - Selecao automatica e publicacao de posts no LinkedIn.

Fluxo:
1. Cron semanal (domingo): IA seleciona 3-5 posts da semana
2. Cria proposta de acao para aprovacao
3. Apos aprovacao: agenda posts com datas/horas
4. Cron diario: publica posts agendados cuja hora chegou
"""
import asyncio
import os
import json
import logging
import traceback
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)

# Per-post timeout: LinkedIn API normalmente <2s, mas damos folga generosa
# pra evitar que 1 post lento bloqueie a fila inteira no cron.
PUBLISH_TIMEOUT_SECONDS = 30.0


def _notify_token_issue(reason: str):
    """Best-effort: avisa Renato no WhatsApp que o token LinkedIn expirou.

    Why: cron silencioso e o pior dos mundos. Se o token cai, posts agendados
    nao publicam e o usuario nao sabe ate alguem perguntar 'cade o post?'.

    Usa send_intel_notification do intel_bot (Evolution API). Fire-and-forget
    e nunca levanta — falha em notificacao NAO pode quebrar o cron.
    """
    try:
        from services.intel_bot import send_intel_notification
        msg = (
            f"INTEL: token LinkedIn invalido ({reason}). "
            "Reconecte em https://intel.almeida-prado.com/api/linkedin/authorize"
        )
        # Estamos dentro de um event loop async (caller e async). Agenda como task.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(send_intel_notification(msg))
        except RuntimeError:
            # Sem loop rodando — roda sincronamente (improvavel neste caller)
            asyncio.run(send_intel_notification(msg))
    except Exception:
        logger.exception("auto_publisher: falha ao notificar token invalido")


# Cadencia semanal padrao: 3 hot_takes opinativos + 1 repost de artigo curado.
# Usuario corrigiu 2026-05-06: nao queremos 4 posts mistos aleatorios, queremos
# essa proporcao fixa pra manter a voz (hot_takes) sem perder o pulso curado.
DEFAULT_WEEKLY_MIX: Dict[str, int] = {'hot_take': 3, 'editorial': 1}


def _week_bounds(start_date: Optional[date]) -> tuple:
    """Devolve (week_start, week_end_exclusive) baseado em start_date.

    Se None, usa proxima segunda (mesmo criterio de schedule_selected_posts).
    week_end_exclusive = week_start + 7 dias (intervalo half-open).
    """
    if start_date is None:
        today = date.today()
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        start_date = today + timedelta(days=days_until_monday)
    week_start = datetime(start_date.year, start_date.month, start_date.day)
    return week_start, week_start + timedelta(days=7)


def count_committed_posts_in_week(start_date: Optional[date] = None) -> int:
    """Conta posts ja comprometidos pra semana (idempotencia guard).

    Why COALESCE: o slot vive em colunas diferentes conforme o status:
      - pending_approval -> data_publicacao_planejada (proposta da IA)
      - scheduled        -> data_publicacao (slot promovido pos-approve)
      - published        -> data_publicado (slot real pos-publicacao)
    Pegar so uma das tres dava falso negativo e duplicava posts (cron 2x
    em 2026-05-03 -> 8 pending_approval em vez de 4).
    """
    week_start, week_end = _week_bounds(start_date)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) AS c FROM editorial_posts
            WHERE status IN ('pending_approval', 'scheduled', 'published')
              AND COALESCE(data_publicacao_planejada, data_publicacao, data_publicado) >= %s
              AND COALESCE(data_publicacao_planejada, data_publicacao, data_publicado) <  %s
        """, (week_start, week_end))
        row = cursor.fetchone()
    return int(row['c'] if row else 0)


async def select_weekly_posts(
    posts_per_week: int = 4,
    mix: Optional[Dict[str, int]] = None,
) -> Dict:
    """
    IA seleciona os melhores posts para a semana.

    Cadencia padrao: 3 hot_takes + 1 editorial repost (DEFAULT_WEEKLY_MIX).
    Pode ser sobrescrita via mix={'hot_take': N, 'editorial': M}; nesse caso
    posts_per_week e ignorado (vira sum(mix.values())).

    Chamado pelo cron no domingo.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY nao configurada"}

    if mix is None:
        mix = dict(DEFAULT_WEEKLY_MIX)
    n_hot_takes = max(0, int(mix.get('hot_take', 0)))
    n_editorials = max(0, int(mix.get('editorial', 0)))
    total_target = n_hot_takes + n_editorials
    if total_target == 0:
        return {"error": "mix vazio: especifique hot_take e/ou editorial > 0"}

    # Collect candidates from both sources (separados — selecao por bucket)
    hot_take_candidates: List[Dict] = []
    editorial_candidates: List[Dict] = []

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
            hot_take_candidates.append(row)

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
            editorial_candidates.append(row)

    total_candidates = len(hot_take_candidates) + len(editorial_candidates)
    if total_candidates == 0:
        return {"error": "Nenhum post disponivel para agendar", "candidates": 0}

    # Feedback loop: top/bottom posts reais para guiar selecao
    from services.editorial_pdca import get_top_bottom_examples, format_examples_for_prompt
    examples = get_top_bottom_examples(n=3)
    examples_block = format_examples_for_prompt(examples)

    async def _ask_ai(bucket_name: str, items: List[Dict], n: int) -> List[Dict]:
        """Pede pra IA escolher N melhores de uma lista. Sem itens ou n<=0 -> []."""
        if n <= 0 or not items:
            return []
        cands_text = "\n".join([
            f"[{i+1}] {c['titulo']}\n    Preview: {c['conteudo_preview']}"
            for i, c in enumerate(items)
        ])
        prompt = f"""Voce e um estrategista de conteudo LinkedIn para Renato Almeida Prado,
executivo de tecnologia e governanca corporativa.

Selecione os {n} melhores {bucket_name} para publicar esta semana.
{examples_block}
CANDIDATOS:
{cands_text}

CRITERIOS:
- Padroes dos posts top: provocacao + numeros especificos + tom direto + IA aplicada
- Evitar padroes dos bottom: tom institucional, economia macro genérica, sem call-to-action
- Diversidade de tema (nao repetir assuntos similares)
- Relevancia para o perfil (IA, governanca, negocios, empreendedorismo)

Responda APENAS com JSON:
{{"selections": [
    {{"index": 1, "reason": "motivo curto"}}
]}}"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}
                )
            if resp.status_code != 200:
                logger.warning(f"select_weekly_posts {bucket_name}: API {resp.status_code}")
                return []
            text = resp.json()["content"][0]["text"]
            start = text.find("{"); end = text.rfind("}") + 1
            if start < 0:
                return []
            parsed = json.loads(text[start:end])
            chosen: List[Dict] = []
            for s in parsed.get("selections", []):
                idx = s.get("index", 0) - 1
                if 0 <= idx < len(items):
                    c = items[idx]
                    c['reason'] = s.get('reason', '')
                    chosen.append(c)
                if len(chosen) >= n:
                    break
            return chosen
        except Exception as e:
            logger.warning(f"select_weekly_posts {bucket_name} fallback: {e}")
            return []

    try:
        selected_hot = await _ask_ai("hot-takes opinativos", hot_take_candidates, n_hot_takes)
        selected_edit = await _ask_ai("reposts editoriais", editorial_candidates, n_editorials)

        # Hot-takes primeiro (formato dominante), repost por ultimo
        selected = selected_hot + selected_edit
        return {
            "selected": selected,
            "total_candidates": total_candidates,
            "mix_target": {'hot_take': n_hot_takes, 'editorial': n_editorials},
            "mix_actual": {'hot_take': len(selected_hot), 'editorial': len(selected_edit)},
        }

    except Exception as e:
        return {"error": str(e)}


def schedule_selected_posts(selected: List[Dict], start_date: date = None) -> Dict:
    """
    Marca posts selecionados pela IA como pending_approval com slots planejados.
    Ter/Qua/Qui/Sex as 9h ou 12h.

    Workflow novo: IA seleciona -> pending_approval (slot em data_publicacao_planejada).
    User aprova via /api/editorial/{id}/approve -> scheduled (slot promovido pra data_publicacao).
    """
    if start_date is None:
        # Next Monday
        today = date.today()
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        start_date = today + timedelta(days=days_until_monday)

    # Guard idempotente: se a semana ja tem posts comprometidos suficientes,
    # nao criar duplicatas. Cron 2x em 2026-05-03 criou 8 pending_approval
    # em vez de 4. Defesa em profundidade — handler do cron tambem checa antes
    # de gastar tokens em select_weekly_posts.
    committed = count_committed_posts_in_week(start_date)
    if committed >= len(selected):
        return {
            "scheduled": [],
            "skipped": True,
            "reason": f"semana ja tem {committed} posts comprometidos",
            "committed": committed,
            "week_start": start_date.isoformat(),
        }

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
                # Create/link editorial_post for unified pipeline
                ht_id = post['id']
                cursor.execute("SELECT editorial_post_id FROM hot_takes WHERE id = %s", (ht_id,))
                ht_row = cursor.fetchone()
                ep_id = ht_row['editorial_post_id'] if ht_row and ht_row.get('editorial_post_id') else None

                if not ep_id:
                    # Create editorial_post from hot_take — slot vai pra data_publicacao_planejada
                    cursor.execute("""
                        INSERT INTO editorial_posts (article_title, article_url, conteudo_adaptado, hashtags,
                            tipo, canal, status, data_publicacao_planejada, hot_take_id)
                        VALUES (%s, %s, %s, %s, 'hot_take', 'linkedin', 'pending_approval', %s, %s)
                        RETURNING id
                    """, (post.get('titulo', ''), post.get('news_link', ''),
                          post.get('conteudo', ''), json.dumps(post.get('hashtags', [])),
                          slot, ht_id))
                    ep_id = cursor.fetchone()['id']
                    cursor.execute("UPDATE hot_takes SET editorial_post_id = %s, status = 'pending_approval', scheduled_for = %s WHERE id = %s",
                                  (ep_id, slot, ht_id))
                else:
                    cursor.execute("UPDATE editorial_posts SET status = 'pending_approval', data_publicacao_planejada = %s WHERE id = %s", (slot, ep_id))
                    cursor.execute("UPDATE hot_takes SET status = 'pending_approval', scheduled_for = %s WHERE id = %s", (slot, ht_id))

                scheduled.append({
                    'id': ep_id,
                    'hot_take_id': ht_id,
                    'source': post['source'],
                    'titulo': post['titulo'],
                    'data_publicacao_planejada': slot.isoformat(),
                    'reason': post.get('reason', '')
                })
            else:
                cursor.execute("""
                    UPDATE editorial_posts SET status = 'pending_approval', data_publicacao_planejada = %s
                    WHERE id = %s
                """, (slot, post['id']))

                scheduled.append({
                    'id': post['id'],
                    'source': post['source'],
                    'titulo': post['titulo'],
                    'data_publicacao_planejada': slot.isoformat(),
                    'reason': post.get('reason', '')
                })

        conn.commit()

    return {"scheduled": scheduled}


async def select_replacement_post(slot_datetime: datetime, exclude_ids: List[int] = None) -> Optional[Dict]:
    """
    Apos user descartar uma proposta da IA, escolhe o proximo melhor candidate
    do estoque de drafts e marca como pending_approval no MESMO slot.

    Returns dict com {id, titulo, source, data_publicacao_planejada} ou None se sem candidatos.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    exclude_ids = exclude_ids or []

    candidates: List[Dict] = []
    with get_db() as conn:
        cursor = conn.cursor()

        # Hot-takes drafts (pool bruto)
        cursor.execute("""
            SELECT id, 'hot_take' as source, news_title as titulo,
                   linkedin_post as conteudo, news_link, hashtags,
                   created_at as criado_em
            FROM hot_takes
            WHERE status = 'draft' AND linkedin_post IS NOT NULL
              AND editorial_post_id IS NULL
            ORDER BY created_at DESC LIMIT 15
        """)
        for r in cursor.fetchall():
            row = dict(r)
            row['conteudo_preview'] = (row['conteudo'] or '')[:300]
            candidates.append(row)

        # Editorial drafts (curados)
        excl_clause = ""
        params: List = []
        if exclude_ids:
            excl_clause = "AND id NOT IN %s"
            params.append(tuple(exclude_ids))
        cursor.execute(f"""
            SELECT id, 'editorial' as source, COALESCE(titulo_adaptado, article_title) as titulo,
                   conteudo_adaptado as conteudo, article_url as news_link, hashtags, tipo,
                   ai_score_relevancia as score, criado_em
            FROM editorial_posts
            WHERE status = 'draft' AND conteudo_adaptado IS NOT NULL
              AND LENGTH(conteudo_adaptado) > 50
              {excl_clause}
            ORDER BY ai_score_relevancia DESC NULLS LAST, criado_em DESC
            LIMIT 15
        """, params)
        for r in cursor.fetchall():
            row = dict(r)
            row['conteudo_preview'] = (row['conteudo'] or '')[:300]
            candidates.append(row)

    if not candidates:
        return None

    # Tenta IA pra ranquear; fallback: pega top
    chosen = None
    if api_key:
        try:
            from services.editorial_pdca import get_top_bottom_examples, format_examples_for_prompt
            examples = get_top_bottom_examples(n=2)
            examples_block = format_examples_for_prompt(examples)
            cands_text = "\n".join([
                f"[{i+1}] ({c['source']}) {c['titulo']}\n    Preview: {c['conteudo_preview']}"
                for i, c in enumerate(candidates)
            ])
            prompt = f"""Voce e estrategista de conteudo LinkedIn de Renato Almeida Prado.
Escolha 1 unico post como SUBSTITUTO de uma proposta descartada pelo user.
{examples_block}
CANDIDATOS:
{cands_text}

Responda APENAS JSON: {{"index": <numero>, "reason": "motivo curto"}}"""

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]}
                )
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"]
                start = text.find("{"); end = text.rfind("}") + 1
                if start >= 0:
                    parsed = json.loads(text[start:end])
                    idx = parsed.get("index", 0) - 1
                    if 0 <= idx < len(candidates):
                        chosen = candidates[idx]
                        chosen['reason'] = parsed.get('reason', '')
        except Exception as e:
            logger.warning(f"select_replacement_post AI fallback: {e}")

    if not chosen:
        chosen = candidates[0]
        chosen.setdefault('reason', 'Top do estoque (fallback sem IA)')

    # Schedule chosen no slot
    with get_db() as conn:
        cursor = conn.cursor()
        if chosen['source'] == 'hot_take':
            ht_id = chosen['id']
            cursor.execute("""
                INSERT INTO editorial_posts (article_title, article_url, conteudo_adaptado, hashtags,
                    tipo, canal, status, data_publicacao_planejada, hot_take_id)
                VALUES (%s, %s, %s, %s, 'hot_take', 'linkedin', 'pending_approval', %s, %s)
                RETURNING id
            """, (chosen.get('titulo', ''), chosen.get('news_link', ''),
                  chosen.get('conteudo', ''), json.dumps(chosen.get('hashtags', [])),
                  slot_datetime, ht_id))
            ep_id = cursor.fetchone()['id']
            cursor.execute("UPDATE hot_takes SET editorial_post_id = %s, status = 'pending_approval', scheduled_for = %s WHERE id = %s",
                          (ep_id, slot_datetime, ht_id))
            conn.commit()
            return {
                'id': ep_id,
                'hot_take_id': ht_id,
                'source': 'hot_take',
                'titulo': chosen['titulo'],
                'data_publicacao_planejada': slot_datetime.isoformat(),
                'reason': chosen.get('reason', '')
            }
        else:
            cursor.execute("""
                UPDATE editorial_posts SET status = 'pending_approval', data_publicacao_planejada = %s
                WHERE id = %s
            """, (slot_datetime, chosen['id']))
            conn.commit()
            return {
                'id': chosen['id'],
                'source': 'editorial',
                'titulo': chosen['titulo'],
                'data_publicacao_planejada': slot_datetime.isoformat(),
                'reason': chosen.get('reason', '')
            }


async def publish_due_posts() -> Dict:
    """
    Publica posts cuja hora de agendamento ja passou.
    Chamado pelo cron a cada hora ou no daily-sync.

    Robustez (refatorado 2026-05-03 apos cron travar 'running' sem
    finished_at — Vercel matou daily-sync no timeout 300s):
    - Per-post try/except: 1 falha NUNCA bloqueia outros posts
    - asyncio.wait_for por publish (30s) — protege contra LinkedIn lento
    - traceback completo em logger.exception
    - results inclui erro detalhado por item (debug-friendly)
    - notifica WhatsApp se token expirado
    """
    from integrations.linkedin_posting import get_stored_token, publish_post

    token = get_stored_token()
    if not token:
        logger.warning("auto_publisher: LinkedIn token ausente/expirado")
        _notify_token_issue("token ausente ou expirado em linkedin_tokens")
        return {"skipped": "LinkedIn nao conectado", "token_state": "missing"}

    now = datetime.now()
    results = {"published": 0, "errors": 0, "posts": [], "token_state": "ok"}

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Unified: ALL scheduled editorial_posts (includes hot_takes with editorial_post)
            cursor.execute("""
                SELECT ep.id, ep.conteudo_adaptado as conteudo, ep.article_url,
                       ep.hashtags, ep.hot_take_id, ep.article_title
                FROM editorial_posts ep
                WHERE ep.status = 'scheduled' AND ep.data_publicacao <= %s
                ORDER BY ep.data_publicacao ASC
            """, (now,))
            due_editorials = [dict(r) for r in cursor.fetchall()]

            # Also check orphan hot_takes (scheduled but no editorial_post)
            cursor.execute("""
                SELECT id, linkedin_post as conteudo, news_link as article_url
                FROM hot_takes
                WHERE status = 'scheduled' AND scheduled_for <= %s
                  AND editorial_post_id IS NULL
                ORDER BY scheduled_for ASC
            """, (now,))
            due_hot_takes = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.exception("auto_publisher: erro lendo posts agendados")
        return {"published": 0, "errors": 1, "fatal": f"db_read: {type(e).__name__}: {e}"}

    results["due_editorials"] = len(due_editorials)
    results["due_hot_takes"] = len(due_hot_takes)

    if not due_editorials and not due_hot_takes:
        return results

    from services.agent_actions import log_action

    async def _safe_publish(text: str, article_url: Optional[str]) -> Dict:
        """Wrapper com timeout per-post — converte timeout/exception em dict."""
        try:
            return await asyncio.wait_for(
                publish_post(text, article_url),
                timeout=PUBLISH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return {"error": f"publish_post timeout > {PUBLISH_TIMEOUT_SECONDS}s"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-400:]}

    def _autocomplete_publish_tasks(post_id: Optional[int], slot: Optional[datetime]) -> None:
        """Fecha tarefas redundantes 'Publicar...' que o cron acaba de cobrir.
        Estrategia em 2 passes:
          1. Match exato por editorial_post_id (gravado quando temos FK)
          2. Fallback heuristico pra project 22 + titulo ILIKE 'Publicar%'
             + data_vencimento::date == slot::date (cobre tasks legacy do
             weekly-briefing que nao tem FK pq foram criadas antes do post existir)
        Idempotente — UPDATE com WHERE status='pending'."""
        try:
            with get_db() as conn:
                cur = conn.cursor()
                if post_id:
                    cur.execute("""
                        UPDATE tasks SET status='completed', data_conclusao=NOW(),
                            descricao=COALESCE(descricao,'') ||
                                ' | Auto-completed: cron auto-publish em ' || NOW()::date
                        WHERE editorial_post_id = %s AND status='pending'
                          AND (titulo ILIKE 'Publicar%%' OR titulo ILIKE 'Publish%%')
                    """, (post_id,))
                if slot:
                    cur.execute("""
                        UPDATE tasks SET status='completed', data_conclusao=NOW(),
                            descricao=COALESCE(descricao,'') ||
                                ' | Auto-completed: cron auto-publish (match por data) em ' || NOW()::date
                        WHERE project_id = 22 AND status='pending'
                          AND titulo ILIKE 'Publicar%%'
                          AND editorial_post_id IS NULL
                          AND data_vencimento::date = %s::date
                    """, (slot,))
                conn.commit()
        except Exception:
            logger.exception(f"_autocomplete_publish_tasks falhou pra post {post_id}")

    # Publish orphan hot_takes first
    for ht in due_hot_takes:
        try:
            text = ht['conteudo'] or ''
            if not text:
                continue
            result = await _safe_publish(text, ht.get('article_url'))
            if result.get('success'):
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE hot_takes SET status = 'published', published_at = NOW(),
                               linkedin_url = %s
                        WHERE id = %s
                    """, (result.get('post_url', ''), ht['id']))
                    conn.commit()
                # Hot_takes orfaos nao tem editorial_post — fallback so por data
                _autocomplete_publish_tasks(post_id=None, slot=now)
                results["published"] += 1
                results["posts"].append({"id": ht['id'], "source": "hot_take", "url": result.get('post_url')})
                preview = (text or '').strip().split('\n')[0][:80]
                try:
                    log_action(
                        action_type='post_published',
                        category='editorial',
                        title=f"Post publicado no LinkedIn: {preview}",
                        scope_ref={'hot_take_id': ht['id'], 'linkedin_url': result.get('post_url')},
                        source='auto_publisher',
                        payload={'post_url': result.get('post_url')},
                    )
                except Exception:
                    logger.exception(f"auto_publisher: log_action falhou pra hot_take {ht['id']}")
            else:
                results["errors"] += 1
                err = result.get('error', 'unknown')
                results["posts"].append({"id": ht['id'], "source": "hot_take", "error": err})
                logger.error(f"Failed to publish hot_take {ht['id']}: {err}")
                # 401 = token invalido — notifica
                if isinstance(err, str) and ('401' in err or 'unauthorized' in err.lower() or 'invalid_token' in err.lower()):
                    _notify_token_issue(f"401 ao publicar hot_take {ht['id']}")
        except Exception as e:
            results["errors"] += 1
            results["posts"].append({"id": ht.get('id'), "source": "hot_take", "error": f"loop_exc: {type(e).__name__}: {e}"})
            logger.exception(f"auto_publisher: exception fatal no loop pra hot_take {ht.get('id')}")

    for ep in due_editorials:
        try:
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

            result = await _safe_publish(full_text, ep.get('article_url'))
            if result.get('success'):
                try:
                    from services.editorial_calendar import mark_as_published
                    mark_as_published(ep['id'], url_publicado=result.get('post_url', ''))
                except Exception:
                    logger.exception(f"auto_publisher: mark_as_published falhou pra ep {ep['id']}")

                # Auto-complete tarefas redundantes de "Publicar X" — cron ja fez.
                # Why: weekly-briefing cria tasks "Publicar..." sem FK pra post
                # (post nao existe ainda). Quando publicamos aqui, fechamos:
                # 1) Tasks com editorial_post_id == ep.id (futuro: quando linkamos)
                # 2) Tasks proj 22 + 'Publicar%' + data_vencimento::date == hoje
                _autocomplete_publish_tasks(post_id=ep['id'], slot=now)

                # Also update linked hot_take
                if ep.get('hot_take_id'):
                    try:
                        with get_db() as conn:
                            cursor = conn.cursor()
                            cursor.execute("UPDATE hot_takes SET status = 'published', published_at = NOW(), linkedin_url = %s WHERE id = %s",
                                          (result.get('post_url', ''), ep['hot_take_id']))
                            conn.commit()
                    except Exception:
                        logger.exception(f"auto_publisher: update hot_take {ep.get('hot_take_id')} falhou")

                results["published"] += 1
                source = "hot_take" if ep.get('hot_take_id') else "editorial"
                results["posts"].append({"id": ep['id'], "source": source, "url": result.get('post_url')})
                try:
                    log_action(
                        action_type='post_published',
                        category='editorial',
                        title=f"Post publicado no LinkedIn: {ep.get('article_title') or (text or '').strip().split(chr(10))[0][:80]}",
                        scope_ref={'editorial_post_id': ep['id'], 'hot_take_id': ep.get('hot_take_id'), 'linkedin_url': result.get('post_url')},
                        source='auto_publisher',
                        payload={'post_url': result.get('post_url')},
                    )
                except Exception:
                    logger.exception(f"auto_publisher: log_action falhou pra ep {ep['id']}")
            else:
                results["errors"] += 1
                err = result.get('error', 'unknown')
                results["posts"].append({"id": ep['id'], "source": "editorial", "error": err})
                logger.error(f"Failed to publish editorial {ep['id']}: {err}")
                if isinstance(err, str) and ('401' in err or 'unauthorized' in err.lower() or 'invalid_token' in err.lower()):
                    _notify_token_issue(f"401 ao publicar editorial {ep['id']}")
        except Exception as e:
            results["errors"] += 1
            results["posts"].append({"id": ep.get('id'), "source": "editorial", "error": f"loop_exc: {type(e).__name__}: {e}"})
            logger.exception(f"auto_publisher: exception fatal no loop pra editorial {ep.get('id')}")

    return results
