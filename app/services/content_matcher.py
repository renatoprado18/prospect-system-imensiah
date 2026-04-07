"""
Content Matcher Service

Sugere artigos e news relevantes para compartilhar com cada contato,
baseado em:
- Tags das rodas pendentes
- Tags/setor do contato
- Histórico de interesses
"""

import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path

from database import get_db


# Mapping de tags/setores para categorias de artigos
TAG_TO_CATEGORY = {
    # Setor → Categorias de Artigo
    "financeiro": ["Governança Corporativa", "M&A", "Gestão de Riscos", "Finanças"],
    "tecnologia": ["Transformação Digital", "Inovação", "Tecnologia"],
    "consultoria": ["Estratégia", "Liderança Executiva", "NeoGovernança"],
    "juridico": ["Governança Corporativa", "Gestão de Riscos", "Compliance"],
    "industria": ["Operações", "Inovação", "Sustentabilidade"],
    "saude": ["Governança", "Inovação", "ESG"],
    "educacao": ["Liderança", "Inovação", "Transformação"],

    # Cargo → Público-alvo
    "ceo": ["ceos", "executivos", "c-level"],
    "cfo": ["executivos", "finanças", "c-level"],
    "cto": ["executivos", "tecnologia", "c-level"],
    "diretor": ["executivos", "diretores"],
    "conselheiro": ["conselheiros", "board members"],
    "c-level": ["ceos", "executivos", "c-level"],

    # Tópicos das Rodas → Categorias
    "governanca": ["Governança Corporativa", "NeoGovernança", "Governança"],
    "governança": ["Governança Corporativa", "NeoGovernança", "Governança"],
    "esg": ["ESG", "Sustentabilidade"],
    "sustentabilidade": ["ESG", "Sustentabilidade"],
    "familia": ["Empresa Familiar", "Sucessão"],
    "sucessao": ["Empresa Familiar", "Sucessão"],
    "sucessão": ["Empresa Familiar", "Sucessão"],
    "inovacao": ["Inovação", "Transformação Digital"],
    "inovação": ["Inovação", "Transformação Digital"],
    "digital": ["Transformação Digital", "Inovação"],
    "estrategia": ["Estratégia", "Liderança Executiva"],
    "estratégia": ["Estratégia", "Liderança Executiva"],
    "lideranca": ["Liderança", "Liderança Executiva"],
    "liderança": ["Liderança", "Liderança Executiva"],
    "conselho": ["Governança Corporativa", "Conselho", "Board"],
    "board": ["Governança Corporativa", "Conselho", "Board"],
    "m&a": ["M&A", "Fusões e Aquisições"],
    "fusao": ["M&A", "Fusões e Aquisições"],
    "fusão": ["M&A", "Fusões e Aquisições"],
    "complexidade": ["Complexidade", "Era do Caos"],
    "caos": ["Complexidade", "Era do Caos"],
    "diversidade": ["Diversidade", "ESG"],
    "cultura": ["Cultura Organizacional", "Liderança"],
}


class ContentMatcher:
    """Service para matching de conteúdo com contatos."""

    def __init__(self):
        self._articles_cache: Optional[List[Dict]] = None
        self._articles_loaded_at: Optional[datetime] = None
        self._editorial_cache: Optional[List[Dict]] = None
        self._editorial_loaded_at: Optional[datetime] = None
        self._merged_cache: Optional[List[Dict]] = None
        self._merged_loaded_at: Optional[datetime] = None
        self._cache_ttl = timedelta(minutes=30)

    def _get_all_articles(self) -> List[Dict]:
        """Retorna lista combinada de artigos (JSON + DB) com cache."""
        now = datetime.now()

        # Return cache if valid
        if (self._merged_cache is not None and
            self._merged_loaded_at is not None and
            now - self._merged_loaded_at < self._cache_ttl):
            return self._merged_cache

        # Load and merge
        json_articles = self._load_articles()
        db_articles = self._get_editorial_posts()

        articles_by_url = {}

        # First add JSON articles
        for art in json_articles:
            url = art.get("url")
            if url:
                articles_by_url[url] = art

        # Then overlay with DB data (has AI analysis)
        for art in db_articles:
            url = art.get("article_url")
            if url:
                existing = articles_by_url.get(url, {})
                merged = {**existing, **art}
                merged["url"] = url
                merged["title"] = art.get("article_title") or existing.get("title")
                merged["description"] = art.get("article_description") or existing.get("description")
                articles_by_url[url] = merged

        self._merged_cache = list(articles_by_url.values())
        self._merged_loaded_at = now
        return self._merged_cache

    def _load_articles(self) -> List[Dict]:
        """Carrega artigos do JSON com cache."""
        now = datetime.now()

        # Return cache if valid
        if (self._articles_cache is not None and
            self._articles_loaded_at is not None and
            now - self._articles_loaded_at < self._cache_ttl):
            return self._articles_cache

        # Load from file
        articles_path = Path(__file__).parent.parent / "static" / "articles.json"
        try:
            with open(articles_path, "r", encoding="utf-8") as f:
                self._articles_cache = json.load(f)
                self._articles_loaded_at = now
                return self._articles_cache
        except Exception as e:
            print(f"Error loading articles.json: {e}")
            return []

    def _get_editorial_posts(self) -> List[Dict]:
        """Busca artigos analisados por AI do banco (com cache)."""
        now = datetime.now()

        # Return cache if valid
        if (self._editorial_cache is not None and
            self._editorial_loaded_at is not None and
            now - self._editorial_loaded_at < self._cache_ttl):
            return self._editorial_cache

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        article_slug,
                        article_title,
                        article_url,
                        article_description,
                        ai_categoria,
                        ai_publico_alvo,
                        ai_keywords,
                        ai_score_relevancia,
                        ai_evergreen,
                        criado_em
                    FROM editorial_posts
                    WHERE article_url IS NOT NULL
                      AND ai_score_relevancia >= 6
                    ORDER BY ai_score_relevancia DESC NULLS LAST, criado_em DESC
                    LIMIT 50
                """)
                self._editorial_cache = [dict(row) for row in cursor.fetchall()]
                self._editorial_loaded_at = now
                return self._editorial_cache
        except Exception as e:
            print(f"Error loading editorial posts: {e}")
            return []

    def _normalize_tag(self, tag: str) -> str:
        """Normaliza uma tag para matching."""
        return tag.lower().strip().replace("-", " ").replace("_", " ")

    def _get_categories_for_tags(self, tags: List[str]) -> set:
        """Converte lista de tags em categorias de artigo."""
        categories = set()
        for tag in tags:
            normalized = self._normalize_tag(tag)
            if normalized in TAG_TO_CATEGORY:
                categories.update(TAG_TO_CATEGORY[normalized])
            # Also add the tag itself as potential category match
            categories.add(tag)
            categories.add(normalized)
        return categories

    def _calculate_article_score(
        self,
        article: Dict,
        roda_tags: List[str],
        contact_tags: List[str],
        contact_setor: Optional[str] = None
    ) -> float:
        """
        Calcula score de relevância de um artigo para um contato.

        Score = (tag_match * 0.4) + (sector_match * 0.3) + (recency * 0.2) + (ai_score * 0.1)
        """
        score = 0.0
        matched_tags = []

        # Combine all relevant tags
        all_tags = roda_tags + contact_tags
        if contact_setor:
            all_tags.append(contact_setor)

        target_categories = self._get_categories_for_tags(all_tags)

        # --- Tag Match (40%) ---
        article_tags = article.get("tags", [])
        article_category = article.get("category", "")
        article_keywords = article.get("ai_keywords", []) or []

        # Normalize article data
        article_items = set()
        for t in article_tags:
            article_items.add(self._normalize_tag(t))
        if article_category:
            article_items.add(self._normalize_tag(article_category))
        for kw in article_keywords:
            article_items.add(self._normalize_tag(kw))

        # Count matches
        matches = 0
        for cat in target_categories:
            normalized_cat = self._normalize_tag(cat)
            for item in article_items:
                if normalized_cat in item or item in normalized_cat:
                    matches += 1
                    matched_tags.append(cat)
                    break

        if target_categories:
            tag_score = min(matches / len(target_categories), 1.0)
        else:
            tag_score = 0.0
        score += tag_score * 0.4

        # --- Sector Match (30%) ---
        if contact_setor:
            sector_categories = TAG_TO_CATEGORY.get(self._normalize_tag(contact_setor), [])
            if any(self._normalize_tag(sc) in article_items for sc in sector_categories):
                score += 0.3

        # --- Recency (20%) ---
        pub_date = article.get("publishedAt") or article.get("criado_em")
        if pub_date:
            try:
                if isinstance(pub_date, str):
                    pub_date = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                elif isinstance(pub_date, datetime):
                    pass
                else:
                    pub_date = None

                if pub_date:
                    days_old = (datetime.now(pub_date.tzinfo) if pub_date.tzinfo else datetime.now() - pub_date).days if hasattr(pub_date, 'tzinfo') else 365
                    # Recalculate properly
                    if pub_date.tzinfo:
                        days_old = (datetime.now(pub_date.tzinfo) - pub_date).days
                    else:
                        days_old = (datetime.now() - pub_date).days

                    if days_old < 30:
                        score += 0.2
                    elif days_old < 90:
                        score += 0.14
                    elif days_old < 180:
                        score += 0.08
                    else:
                        score += 0.04
            except Exception:
                score += 0.1  # Default if can't parse

        # --- AI Score (10%) ---
        ai_score = article.get("ai_score_relevancia")
        if ai_score:
            score += (ai_score / 10.0) * 0.1

        return score, list(set(matched_tags))[:3]

    def get_content_for_contact(
        self,
        contact_id: int,
        roda: Optional[Dict] = None,
        contact_tags: Optional[List[str]] = None,
        contact_setor: Optional[str] = None,
        limit: int = 1
    ) -> Optional[Dict]:
        """
        Busca conteúdo relevante para um contato.

        Args:
            contact_id: ID do contato
            roda: Dicionário da roda pendente (com 'tags', 'tipo', 'conteudo')
            contact_tags: Tags do contato
            contact_setor: Setor/indústria do contato
            limit: Número máximo de sugestões

        Returns:
            ContentSuggestion dict ou None se não houver match
        """
        roda_tags = []
        if roda:
            roda_tags = roda.get("tags", []) or []

        contact_tags = contact_tags or []

        # Get cached articles list
        all_articles = self._get_all_articles()

        if not all_articles:
            return None

        # Score each article
        scored = []
        for article in all_articles:
            score, matched = self._calculate_article_score(
                article, roda_tags, contact_tags, contact_setor
            )
            if score > 0.15:  # Minimum threshold
                scored.append((score, matched, article))

        if not scored:
            # Fallback: return most recent evergreen article
            for article in all_articles[:5]:
                if article.get("ai_evergreen", True):
                    return {
                        "type": "article",
                        "id": article.get("slug") or article.get("article_slug"),
                        "title": article.get("title") or article.get("article_title"),
                        "url": article.get("url") or article.get("article_url"),
                        "reason": "Artigo recomendado",
                        "score": 0.3,
                        "preview": (article.get("description") or article.get("article_description") or "")[:100]
                    }
            return None

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Return top result
        top_score, top_matched, top_article = scored[0]

        # Build reason string
        if top_matched:
            reason = f"Combina com: {', '.join(top_matched)}"
        elif roda_tags:
            reason = f"Relacionado a: {', '.join(roda_tags[:2])}"
        else:
            reason = "Artigo relevante para o perfil"

        return {
            "type": "article",
            "id": top_article.get("slug") or top_article.get("article_slug"),
            "title": top_article.get("title") or top_article.get("article_title"),
            "url": top_article.get("url") or top_article.get("article_url"),
            "reason": reason,
            "score": round(top_score, 2),
            "preview": (top_article.get("description") or top_article.get("article_description") or "")[:100]
        }


# Singleton instance
_matcher: Optional[ContentMatcher] = None


def get_content_matcher() -> ContentMatcher:
    """Retorna instância singleton do ContentMatcher."""
    global _matcher
    if _matcher is None:
        _matcher = ContentMatcher()
    return _matcher
