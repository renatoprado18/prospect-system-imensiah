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
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx

from database import get_db
from services.editorial_actions import JANELA_HORAS, JANELAS_ORDER

logger = logging.getLogger(__name__)

LINKDAPI_BASE = "https://linkdapi.com"
PROFILE_URN_CACHE: Dict[str, str] = {}  # username -> urn (warm pra reduzir chamadas)


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


def _match_post(posts: List[Dict], target_url: str) -> Optional[Dict]:
    """Acha o post na lista que bate com a URL salva em editorial_posts.
    Match por URL exato, depois por activity URN (substring 10+ digitos)."""
    if not target_url:
        return None
    target_url = target_url.rstrip("/")
    for p in posts:
        purl = (p.get("url") or "").rstrip("/")
        if purl and purl == target_url:
            return p
    # Fallback por digit prefix (URN compartilhado entre share/activity)
    import re
    digits = re.findall(r"(\d{10,})", target_url)
    for d in digits:
        prefix = d[:10]
        for p in posts:
            purl = p.get("url") or ""
            if prefix in purl:
                return p
    return None


def _extract_metrics(post: Dict) -> Dict[str, int]:
    """Extrai contagens do dict de post da LinkdAPI.
    Estrutura observada: engagements = {reactionsCount, commentsCount, repostsCount}.
    Impressoes nao vem da API publica — fica 0 (so analytics oficial expoe)."""
    eng = (post.get("engagements") or {})
    # Field names variam — try multiple shapes
    reacoes = (
        eng.get("reactionsCount")
        or eng.get("reactions")
        or eng.get("likesCount")
        or eng.get("likes")
        or 0
    )
    comentarios = (
        eng.get("commentsCount")
        or eng.get("comments")
        or 0
    )
    compartilhamentos = (
        eng.get("repostsCount")
        or eng.get("reposts")
        or eng.get("sharesCount")
        or eng.get("shares")
        or 0
    )
    return {
        "impressoes": 0,  # LinkdAPI nao expoe — coletado manual via xlsx
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
            SELECT ep.id, ep.article_title, ep.titulo_adaptado, ep.data_publicado,
                   ep.linkedin_post_url, ep.url_publicado,
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
            matched = _match_post(posts_lookup, post_url)
            if not matched:
                summary["skipped_no_post_match"] += 1
                summary["details"].append({
                    "post_id": post["id"],
                    "status": "no_match",
                    "url": post_url,
                })
                continue
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
