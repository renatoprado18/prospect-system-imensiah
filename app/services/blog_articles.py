"""Single source of truth pra slugs validos do blog almeida-prado.com.

Antes, VALID_BLOG_SLUGS estava duplicado em content_matcher.py e a maioria
dos paths que gravavam article_url nao validava nada — resultado: 141 drafts
com URLs apontando pra slugs truncados/inexistentes (ver fix/editorial-broken-urls).
"""
from typing import Optional
from urllib.parse import urlparse

# Slugs que realmente existem em https://www.almeida-prado.com/blog
# Conferido contra o blog index em 18/05/2026. Atualize quando publicar artigo novo.
VALID_BLOG_SLUGS = frozenset({
    "adaptacao-continua-segredo-resiliencia",
    "adaptar-se-ao-inimaginavel-complexidade-exponencial",
    "ambidestria-organizacional-conselho",
    "cenarizacao-estrategica-antecipando-futuro",
    "como-neogovernanca-responde-desafios-era-caos",
    "confianca-pilar-estrategico-era-complexidade",
    "conselhos-encruzilhada-terceira-onda-ia",
    "curiosidade-motor-inovacao-conselho",
    "diversidade-alavanca-inovacao",
    "estamos-mesmo-no-comando-da-inovacao",
    "por-que-falar-em-neogovernanca",
    "quando-crescer-ja-nao-basta",
    "resiliencia-cibernetica-desafio-conselhos",
    "santo-grau-exemplo-neogovernanca",
    "teoria-complexidade-governanca",
})


def extract_blog_slug(url: Optional[str]) -> Optional[str]:
    """Retorna o slug se url aponta pra /blog/ em almeida-prado.com, senao None."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if "almeida-prado.com" not in (parsed.netloc or ""):
        return None
    path = parsed.path or ""
    if not path.startswith("/blog/"):
        return None
    slug = path[len("/blog/"):].strip("/")
    return slug or None


def is_valid_blog_url(url: Optional[str]) -> bool:
    """True se url aponta pra um slug que realmente existe no blog."""
    slug = extract_blog_slug(url)
    return bool(slug and slug in VALID_BLOG_SLUGS)


def is_blog_url(url: Optional[str]) -> bool:
    """True se url aponta pra /blog/ em almeida-prado.com (valido ou nao)."""
    return extract_blog_slug(url) is not None


def make_blog_url(slug: Optional[str]) -> Optional[str]:
    """Constroi URL do blog se slug for valido, senao None.

    Use isso em vez de f-string em qualquer write — slugs truncados (ex.: o
    bug original em articles.json) nao geram URL.
    """
    if not slug or slug not in VALID_BLOG_SLUGS:
        return None
    return f"https://www.almeida-prado.com/blog/{slug}"
