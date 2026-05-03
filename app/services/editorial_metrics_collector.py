"""Coletor automatico de metricas LinkedIn por janelas (1h/6h/24h/72h/168h).

Roda em cron horario (`/api/cron/auto-collect-linkedin-metrics`). Pra cada post
publicado nos ultimos 168h, verifica quais janelas ja se passaram E ainda nao
foram registradas em editorial_metrics_history; coleta via LinkdAPI; insere.

LinkdAPI nao expoe endpoint dedicado de "post metrics by url" — usamos
`/api/v1/posts/all?urn=<profile_urn>` (mesma rota usada em campaign_executor)
e localizamos o post pelo URL/ID. Stats vem do campo `engagements` do post.
Se nao acharmos o post na lista (window > 50 posts pra tras), pulamos.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx

from database import get_db
from services.editorial_actions import JANELA_HORAS, JANELAS_ORDER

logger = logging.getLogger(__name__)

LINKDAPI_BASE = "https://linkdapi.com"
PROFILE_URN_CACHE: Dict[str, str] = {}  # username -> urn (warm pra reduzir chamadas)
_ACTIVITY_RE = re.compile(r"activity[:\-](\d{10,})")
_SHARE_RE = re.compile(r"share[:\-](\d{10,})")


async def _get_profile_urn(client: httpx.AsyncClient, api_key: str, username: str) -> Optional[str]:
    if username in PROFILE_URN_CACHE:
        return PROFILE_URN_CACHE[username]
    try:
        resp = await client.get(
            f"{LINKDAPI_BASE}/api/v1/profile/full",
            headers={"X-linkdapi-apikey": api_key},
            params={"username": username},
        )
        if resp.status_code != 200:
            logger.warning(f"LinkdAPI profile/full {username} -> {resp.status_code}")
            return None
        urn = ((resp.json() or {}).get("data") or {}).get("urn")
        if urn:
            PROFILE_URN_CACHE[username] = urn
        return urn
    except Exception as e:
        logger.warning(f"LinkdAPI profile lookup falhou pra {username}: {e}")
        return None


async def _fetch_user_posts(client: httpx.AsyncClient, api_key: str, urn: str) -> List[Dict]:
    try:
        resp = await client.get(
            f"{LINKDAPI_BASE}/api/v1/posts/all",
            headers={"X-linkdapi-apikey": api_key},
            params={"urn": urn},
        )
        if resp.status_code != 200:
            logger.warning(f"LinkdAPI posts/all {urn} -> {resp.status_code}")
            return []
        return ((resp.json() or {}).get("data") or {}).get("posts") or []
    except Exception as e:
        logger.warning(f"LinkdAPI posts/all falhou: {e}")
        return []


def _normalize_text(s: str) -> str:
    """Normaliza texto pra fuzzy match: lowercase + colapsa whitespace."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.lower()).strip()


def _match_post(
    posts: List[Dict],
    target_url: str,
    target_text: Optional[str] = None,
    post_id: Optional[int] = None,
) -> Optional[Dict]:
    """Acha o post na lista que bate com a URL/texto salvos em editorial_posts.

    Estrategia (em ordem):
      1. Activity URN exato (digito do `activity:NNN` ou `activity-NNN`)
      2. URL completa contains (str in str dos dois lados)
      3. Texto fuzzy: primeiros 100 chars normalizados batem (substring)

    URN da DB pode vir como `share-NNN` (URN compartilhado) que nao bate com
    o `activity:MMM` da API (numeros diferentes pro mesmo post). Por isso o
    fallback por texto e essencial.
    """
    if not posts:
        return None

    # 1) Activity URN match (mais confiavel quando bate)
    activity_ids: List[str] = []
    if target_url:
        activity_ids.extend(_ACTIVITY_RE.findall(target_url))
    if activity_ids:
        for aid in activity_ids:
            for p in posts:
                purn = p.get("urn") or ""
                purl = p.get("url") or ""
                if aid in purn or aid in purl:
                    return p

    # 2) URL contains (raro casar pq formatos divergem, mas zero custo)
    if target_url:
        tnorm = target_url.rstrip("/")
        for p in posts:
            purl = (p.get("url") or "").rstrip("/")
            if purl and (purl == tnorm or purl in tnorm or tnorm in purl):
                return p

    # 3) Fuzzy text match (primeiros 100 chars do post adaptado)
    if target_text:
        needle = _normalize_text(target_text)[:100]
        if len(needle) >= 30:  # threshold pra evitar false-positive
            for p in posts:
                haystack = _normalize_text(p.get("text") or "")
                if needle and needle in haystack:
                    return p

    logger.warning(
        f"_match_post: nao achou post (id={post_id}, url={target_url[:80] if target_url else None}, "
        f"text_preview={(target_text or '')[:60]!r}) em {len(posts)} posts da API"
    )
    return None


def _persist_activity_urn(post_id: int, matched_post: Dict) -> None:
    """Salva activity URN no editorial_posts se ainda nao tiver (ou diferente).

    Idempotente — so faz UPDATE se URN mudou. Roda dentro de transacao propria
    pra nao acoplar ao loop principal de coleta (falha aqui nao quebra metricas).
    """
    urn = (matched_post.get("urn") or "").strip()
    if not urn:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT linkedin_activity_urn FROM editorial_posts WHERE id = %s",
                (post_id,),
            )
            row = cursor.fetchone()
            if not row:
                return
            current = (dict(row).get("linkedin_activity_urn") or "").strip()
            if current == urn:
                return
            cursor.execute(
                "UPDATE editorial_posts SET linkedin_activity_urn = %s WHERE id = %s",
                (urn, post_id),
            )
            conn.commit()
            logger.info(f"URN persistido pra post {post_id}: {urn} (era {current or 'NULL'})")
    except Exception as e:
        logger.warning(f"Falha ao persistir URN pra post {post_id}: {e}")


def _extract_metrics(post: Dict) -> Dict[str, int]:
    """Extrai contagens do dict de post da LinkdAPI.

    Estrutura observada (LinkdAPI v1):
      engagements = {totalReactions, commentsCount, repostsCount, reactions: [...]}
    Mantemos aliases legacy (reactionsCount, etc) por defesa.
    Impressoes nao vem da API publica — fica 0 (so analytics oficial expoe)."""
    eng = (post.get("engagements") or {})
    reacoes = (
        eng.get("totalReactions")
        or eng.get("reactionsCount")
        or eng.get("likesCount")
        or eng.get("likes")
        or 0
    )
    comentarios = (
        eng.get("commentsCount")
        or eng.get("totalComments")
        or eng.get("comments")
        or 0
    )
    compartilhamentos = (
        eng.get("repostsCount")
        or eng.get("totalReposts")
        or eng.get("sharesCount")
        or eng.get("shares")
        or eng.get("reposts")
        or 0
    )
    impressoes = (
        eng.get("totalViews")
        or eng.get("viewsCount")
        or eng.get("impressionsCount")
        or 0
    )
    return {
        "impressoes": int(impressoes or 0),
        "reacoes": int(reacoes or 0),
        "comentarios": int(comentarios or 0),
        "compartilhamentos": int(compartilhamentos or 0),
        "salvamentos": 0,
        "visitas_perfil": 0,
        "seguidores": 0,
    }


def _username_from_linkedin_url(url: str) -> Optional[str]:
    """Pega username do user logado, derivado da env ou do post URL.
    Como linkedin_post_url eh uma activity URL (nao /in/<user>), precisamos
    do username dono — vem de env LINKEDIN_USERNAME ou primeiro perfil que
    tem posts publicados no editorial_posts."""
    return (os.getenv("LINKEDIN_USERNAME") or "").strip() or None


def _resolve_post_url(post_row: Dict) -> Optional[str]:
    return (post_row.get("linkedin_post_url") or post_row.get("url_publicado") or "").strip() or None


async def collect_metrics_for_due_windows() -> Dict:
    """Loop principal — chamado pelo cron horario.

    Returns: {coletadas: int, posts: [...], skipped: int, errors: int, mock: bool}
    """
    api_key = (os.getenv("LINKDAPI_KEY") or "").strip()
    use_mock = not api_key

    username = _username_from_linkedin_url("")
    summary = {
        "coletadas": 0,
        "skipped_no_window": 0,
        "skipped_no_url": 0,
        "skipped_no_post_match": 0,
        "errors": 0,
        "posts_processados": 0,
        "details": [],
        "mock": use_mock,
        "username": username,
    }

    # 1. Posts publicados nos ultimos 168h (7d)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ep.id, ep.article_title, ep.titulo_adaptado, ep.conteudo_adaptado,
                   ep.data_publicado, ep.linkedin_post_url, ep.url_publicado,
                   EXTRACT(EPOCH FROM (NOW() - ep.data_publicado)) / 3600 AS horas_desde_pub
            FROM editorial_posts ep
            WHERE ep.status = 'published'
              AND ep.data_publicado IS NOT NULL
              AND ep.data_publicado > NOW() - INTERVAL '8 days'
            ORDER BY ep.data_publicado DESC
        """)
        candidates = [dict(r) for r in cursor.fetchall()]

        # Janelas ja coletadas por post
        cursor.execute("""
            SELECT post_id, ARRAY_AGG(DISTINCT janela) AS janelas
            FROM editorial_metrics_history
            WHERE janela IS NOT NULL AND post_id = ANY(%s)
            GROUP BY post_id
        """, ([c["id"] for c in candidates] or [0],))
        already_collected = {r["post_id"]: set(r["janelas"] or []) for r in cursor.fetchall()}

    if not candidates:
        return summary

    # Pre-warm: busca lista de posts UMA vez por usuario (cache local)
    posts_lookup: List[Dict] = []
    if not use_mock and username:
        async with httpx.AsyncClient(timeout=20.0) as client:
            urn = await _get_profile_urn(client, api_key, username)
            if urn:
                posts_lookup = await _fetch_user_posts(client, api_key, urn)
            else:
                logger.warning(f"Sem URN pra username={username} — pulando coleta real")
                use_mock = True

    # 2. Pra cada post + cada janela vencida, coletar
    new_inserts: List[Tuple] = []  # tuplas pra batch insert
    for post in candidates:
        summary["posts_processados"] += 1
        horas = float(post["horas_desde_pub"] or 0)
        already = already_collected.get(post["id"], set())
        # Janelas que ja se passaram MAS nao foram coletadas
        due = [
            j for j in JANELAS_ORDER
            if JANELA_HORAS[j] <= horas and j not in already
        ]
        if not due:
            summary["skipped_no_window"] += 1
            continue

        post_url = _resolve_post_url(post)
        if not post_url:
            summary["skipped_no_url"] += 1
            continue

        # Resolve metricas atuais — UMA leitura por post, aplicada a todas as
        # janelas vencidas (snapshot atual conta como "as 6h" se essa janela
        # ainda nao foi coletada; melhor isso que pular).
        metrics: Optional[Dict] = None
        if use_mock:
            # Modo mock: usa ultimo snapshot existente OU zeros
            metrics = {
                "impressoes": 0, "reacoes": 0, "comentarios": 0,
                "compartilhamentos": 0, "salvamentos": 0,
                "visitas_perfil": 0, "seguidores": 0,
            }
        else:
            matched = _match_post(
                posts_lookup,
                post_url,
                target_text=post.get("conteudo_adaptado") or post.get("titulo_adaptado") or post.get("article_title"),
                post_id=post["id"],
            )
            if not matched:
                summary["skipped_no_post_match"] += 1
                summary["details"].append({
                    "post_id": post["id"],
                    "status": "no_match",
                    "url": post_url,
                })
                continue
            # Persiste URN da LinkdAPI (idempotente — so escreve se mudou)
            _persist_activity_urn(post["id"], matched)
            metrics = _extract_metrics(matched)

        for janela in due:
            new_inserts.append((
                post["id"],
                metrics["impressoes"],
                metrics["reacoes"],
                metrics["comentarios"],
                metrics["compartilhamentos"],
                metrics["visitas_perfil"],
                metrics["seguidores"],
                metrics["salvamentos"],
                JANELA_HORAS[janela],  # dias_apos_publicacao calculado em horas/24
                janela,
                "auto_linkdapi" if not use_mock else "auto_mock",
            ))
            summary["coletadas"] += 1
            summary["details"].append({
                "post_id": post["id"],
                "janela": janela,
                "metrics": metrics,
            })

    # 3. Insert batch
    if new_inserts:
        with get_db() as conn:
            cursor = conn.cursor()
            for row in new_inserts:
                # dias_apos_publicacao guardamos como horas/24 (int) pra compat com legado.
                pid, imp, reac, com, comp, vis, seg, sal, horas_w, janela, fonte = row
                dias = max(0, int(horas_w // 24))
                cursor.execute("""
                    INSERT INTO editorial_metrics_history (
                        post_id, impressoes, reacoes, comentarios, compartilhamentos,
                        visitas_perfil, seguidores, salvamentos,
                        dias_apos_publicacao, janela, fonte
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (pid, imp, reac, com, comp, vis, seg, sal, dias, janela, fonte))
                # Refresh ultimo snapshot em editorial_posts (vence pelo mais recente)
                cursor.execute("""
                    UPDATE editorial_posts
                    SET linkedin_impressoes = COALESCE(NULLIF(%s, 0), linkedin_impressoes),
                        linkedin_reacoes = %s,
                        linkedin_comentarios = %s,
                        linkedin_compartilhamentos = %s,
                        linkedin_metricas_em = CURRENT_TIMESTAMP,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (imp, reac, com, comp, pid))
            conn.commit()

    summary["timestamp"] = datetime.now().isoformat()
    return summary


async def collect_metrics_for_post(post_id: int) -> Dict:
    """Coleta metricas atuais de UM post especifico (sem janela / sem insert).

    Usado pra smoke test, debug manual e endpoints ad-hoc. Nao mexe em
    editorial_metrics_history. Retorna dict com {success, metrics, ...}.
    """
    api_key = (os.getenv("LINKDAPI_KEY") or "").strip()
    if not api_key:
        return {"success": False, "error": "LINKDAPI_KEY ausente"}

    username = _username_from_linkedin_url("")
    if not username:
        return {"success": False, "error": "LINKEDIN_USERNAME ausente no .env"}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, article_title, titulo_adaptado, conteudo_adaptado,
                   linkedin_post_url, url_publicado, status, data_publicado
            FROM editorial_posts WHERE id = %s
        """, (post_id,))
        row = cursor.fetchone()

    if not row:
        return {"success": False, "error": f"post_id={post_id} nao encontrado"}

    post = dict(row)
    post_url = _resolve_post_url(post)
    if not post_url:
        return {"success": False, "error": "post sem linkedin_post_url"}

    async with httpx.AsyncClient(timeout=20.0) as client:
        urn = await _get_profile_urn(client, api_key, username)
        if not urn:
            return {"success": False, "error": f"sem URN pra username={username}"}
        posts_lookup = await _fetch_user_posts(client, api_key, urn)

    matched = _match_post(
        posts_lookup,
        post_url,
        target_text=post.get("conteudo_adaptado") or post.get("titulo_adaptado") or post.get("article_title"),
        post_id=post["id"],
    )
    if not matched:
        return {
            "success": False,
            "error": "post nao encontrado na lista da LinkdAPI",
            "post_id": post_id,
            "url": post_url,
            "posts_in_api": len(posts_lookup),
        }

    # Persiste URN no DB pra alimentar o link "Ver Analytics" e match xlsx
    _persist_activity_urn(post["id"], matched)
    metrics = _extract_metrics(matched)
    return {
        "success": True,
        "post_id": post_id,
        "matched_urn": matched.get("urn"),
        "matched_url": matched.get("url"),
        **metrics,
    }
