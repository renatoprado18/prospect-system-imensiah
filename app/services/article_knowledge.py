"""
Article Knowledge Service - Fetch, summarize, and save articles to projects.

Fluxo: URL → fetch HTML → extract text → AI summary → save as project_note
"""
import os
import re
import json
import logging
from datetime import datetime
from typing import Dict, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def fetch_article(url: str) -> Dict:
    """
    Fetch article from URL and extract title + content.
    Uses httpx + basic HTML parsing (no external dependency).
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })

        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}

        html = resp.text

        # Extract title
        title = ""
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

        # Try og:title (usually cleaner)
        og_title = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        if not og_title:
            og_title = re.search(r'content="([^"]+)"\s+property="og:title"', html)
        if og_title:
            title = og_title.group(1).strip()

        # Extract description
        description = ""
        og_desc = re.search(r'property="og:description"\s+content="([^"]+)"', html)
        if not og_desc:
            og_desc = re.search(r'content="([^"]+)"\s+property="og:description"', html)
        if og_desc:
            description = og_desc.group(1).strip()

        # Extract main text content (strip HTML tags, scripts, styles)
        # Remove scripts and styles
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<nav[^>]*>.*?</nav>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<header[^>]*>.*?</header>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<footer[^>]*>.*?</footer>', '', text, flags=re.DOTALL | re.IGNORECASE)

        # Try to find article body
        article_match = re.search(r'<article[^>]*>(.*?)</article>', text, flags=re.DOTALL | re.IGNORECASE)
        if article_match:
            text = article_match.group(1)

        # Strip remaining HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Clean whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # Limit to ~5000 chars
        text = text[:5000]

        return {
            "title": title,
            "description": description,
            "content": text,
            "url": url,
            "fetched_at": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Error fetching article {url}: {e}")
        return {"error": str(e)}


async def summarize_article(article: Dict, project_context: str = "") -> str:
    """Generate AI summary of the article in the context of a project."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return article.get("description", "Sem resumo disponível")

    content = article.get("content", "")
    title = article.get("title", "")

    if not content:
        return article.get("description", "Sem conteúdo para resumir")

    context_section = f"\nCONTEXTO DO PROJETO: {project_context}" if project_context else ""

    prompt = f"""Resuma este artigo de forma objetiva e acionável.
{context_section}

TÍTULO: {title}
CONTEÚDO: {content[:3000]}

FORMATO:
- 1 parágrafo de resumo (3-4 frases)
- Pontos-chave (bullets)
- Relevância para o projeto (se contexto fornecido)

Máximo 200 palavras. Português."""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

        if resp.status_code == 200:
            return resp.json()["content"][0]["text"]

    except Exception as e:
        logger.error(f"Error summarizing article: {e}")

    return article.get("description", "Erro ao gerar resumo")


async def save_article_to_project(project_id: int, url: str) -> Dict:
    """
    Full pipeline: fetch article → summarize → save to project.
    Returns the saved note.
    """
    # Get project context
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome, descricao FROM projects WHERE id = %s", (project_id,))
        project = cursor.fetchone()
        if not project:
            return {"error": "Projeto não encontrado"}
        project = dict(project)

    # Fetch
    article = await fetch_article(url)
    if article.get("error"):
        return {"error": f"Erro ao buscar artigo: {article['error']}"}

    # Summarize
    project_context = f"{project['nome']}: {project.get('descricao', '')}"
    summary = await summarize_article(article, project_context)

    # Save as project note
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor, metadata)
            VALUES (%s, 'article', %s, %s, 'INTEL IA', %s)
            RETURNING id
        """, (
            project_id,
            article.get("title", url),
            summary,
            json.dumps({
                "url": url,
                "description": article.get("description", ""),
                "fetched_at": article.get("fetched_at", ""),
            })
        ))
        note_id = cursor.fetchone()['id']
        conn.commit()

    return {
        "status": "success",
        "note_id": note_id,
        "title": article.get("title", ""),
        "summary": summary,
        "url": url
    }
