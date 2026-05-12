"""
Editorial × Clipping match (v1).

Cruza noticias do clipping diario com drafts do estoque (editorial_posts).
Match barato em Python: overlap de keywords + bonus por categoria.

Endpoint REST consome match_clipping_with_inventory() — sem Claude.
Validacao semantica com Claude fica no endpoint legado /api/news/{id}/details
(usado pelos widgets inline em /clipping e drill-down).
"""
import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import get_db

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das", "dos",
    "e", "ou", "em", "na", "no", "nas", "nos", "por", "para", "com", "sem", "sob",
    "ao", "aos", "a", "se", "que", "porque", "como", "quando", "onde", "qual", "quais",
    "ser", "estar", "ter", "haver", "fazer", "ir", "vir", "dar", "the", "of", "and",
    "to", "in", "for", "on", "at", "by", "is", "are", "was", "were", "be", "been",
    "mais", "menos", "muito", "pouco", "tao", "tanto", "outro", "outra", "este", "esta",
    "esse", "essa", "isto", "isso", "aquele", "aquela", "aquilo", "seu", "sua", "seus",
    "suas", "meu", "minha", "nosso", "nossa", "voce", "voces", "ele", "ela", "eles",
    "elas", "nao", "sim", "ja", "ainda", "tambem", "so", "apenas", "entre", "ate",
    "sobre", "contra", "sem", "ano", "anos", "mes", "meses", "dia", "dias", "hoje",
    "ontem", "amanha",
}


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ASCII", "ignore").decode("ASCII")
    return s.lower().strip()


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    text = _norm(text)
    words = re.findall(r"[a-z0-9]{4,}", text)
    return [w for w in words if w not in _STOPWORDS]


def _draft_terms(draft: Dict[str, Any]) -> List[str]:
    """Junta keywords + titulo do draft em um conjunto de termos normalizados."""
    terms: List[str] = []
    kws = draft.get("ai_keywords")
    if isinstance(kws, str):
        try:
            kws = json.loads(kws)
        except Exception:
            kws = []
    if isinstance(kws, list):
        for k in kws:
            terms.extend(_tokenize(str(k)))
    terms.extend(_tokenize(draft.get("article_title", "")))
    return list(set(terms))


def _news_terms(item: Dict[str, Any]) -> List[str]:
    parts = [
        item.get("titulo_resumido") or item.get("title") or "",
        item.get("resumo") or item.get("description") or "",
        item.get("sugestao_post") or "",
    ]
    return list(set(_tokenize(" ".join(parts))))


def _score_pair(news_terms: List[str], draft: Dict[str, Any], news_cat: str) -> int:
    draft_terms = _draft_terms(draft)
    overlap = len(set(news_terms) & set(draft_terms))
    score = overlap * 10
    draft_cat = _norm(draft.get("ai_categoria") or "")
    if news_cat and draft_cat:
        if news_cat == draft_cat:
            score += 8
        elif news_cat in draft_cat or draft_cat in news_cat:
            score += 4
    rel = draft.get("ai_score_relevancia")
    if isinstance(rel, (int, float)):
        score += int(rel / 20)
    return score


def _load_drafts(conn) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, article_title, article_url, ai_categoria, ai_keywords,
               ai_gancho_linkedin, ai_score_relevancia, status, criado_em
        FROM editorial_posts
        WHERE status IN ('draft', 'scheduled', 'pending_approval')
          AND article_title IS NOT NULL
        """
    )
    return [dict(row) for row in cur.fetchall()]


def _load_today_clipping(conn) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT conteudo FROM news_clippings
        WHERE DATE(gerado_em) = CURRENT_DATE
        ORDER BY gerado_em DESC LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        return []
    content = row["conteudo"]
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return []
    return content if isinstance(content, list) else []


def match_clipping_with_inventory(min_score: int = 12, top_per_news: int = 1) -> Dict[str, Any]:
    """Retorna pares (noticia, top-N drafts) pro clipping de hoje.

    min_score: filtro de score minimo pra considerar "match"
    top_per_news: quantos drafts retornar por noticia
    """
    with get_db() as conn:
        drafts = _load_drafts(conn)
        clipping = _load_today_clipping(conn)

    out: List[Dict[str, Any]] = []
    for it in clipping:
        news_id = it.get("news_id")
        if not news_id:
            continue
        n_terms = _news_terms(it)
        n_cat = _norm(it.get("categoria") or "")
        scored = []
        for d in drafts:
            s = _score_pair(n_terms, d, n_cat)
            if s >= min_score:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        top = [
            {
                "article_id": d["id"],
                "article_title": d["article_title"],
                "article_url": d.get("article_url"),
                "ai_categoria": d.get("ai_categoria"),
                "ai_gancho_linkedin": d.get("ai_gancho_linkedin"),
                "score": s,
                "status": d.get("status"),
            }
            for s, d in scored[:top_per_news]
        ]
        out.append(
            {
                "news_id": news_id,
                "news_title": it.get("titulo_resumido") or it.get("title"),
                "news_categoria": it.get("categoria"),
                "news_relevancia": it.get("relevancia"),
                "news_fonte": it.get("source") or it.get("fonte"),
                "news_link": it.get("link"),
                "matches": top,
                "has_match": len(top) > 0,
            }
        )
    return {
        "date": datetime.now().date().isoformat(),
        "total_news": len(clipping),
        "total_with_match": sum(1 for x in out if x["has_match"]),
        "inventory_size": len(drafts),
        "min_score": min_score,
        "pairs": out,
    }
