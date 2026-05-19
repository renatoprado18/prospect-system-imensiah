#!/usr/bin/env python3
"""
Generate `conteudo_adaptado` (LinkedIn post body) for editorial_posts of type='repost'
that currently have NULL body. Uses Claude to draft Renato's voice from
article_title + article_description + ai_gancho_linkedin + ai_keywords.

Usage:
  python scripts/generate_repost_adaptations.py                  # local, dry run, top 5
  python scripts/generate_repost_adaptations.py --apply --limit 10
  python scripts/generate_repost_adaptations.py --apply --remote --limit 20

Idempotent: only processes rows where conteudo_adaptado IS NULL.
Prioritizes ai_score_relevancia DESC NULLS LAST (same order as weekly selector).
"""
import os
import sys
import json
import argparse
import asyncio
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_DIR / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'").rstrip("\\n"))

if "--remote" not in sys.argv:
    os.environ["USE_LOCAL_DB"] = "1"
sys.path.insert(0, str(PROJECT_DIR / "app"))

import httpx
from bs4 import BeautifulSoup
from database import get_db

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; INTELRepostAdapter/1.0)"}
ARTICLE_MIN_CHARS = 500  # below this, skip article and fallback to description-only

PROMPT = """Voce escreve no LinkedIn pelo Renato de Faria e Almeida Prado: conselheiro de
empresas, especialista em governanca corporativa, IA aplicada a lideranca estrategica,
cofundador da imensIAH e 10XMentorAI.

VOZ: direta, executiva, sem floreio. Provoca reflexao com exemplos concretos
e perguntas. Nada de "navegando por incertezas" ou jargao consultivo generico.
Frases curtas. Quebra de linha pra respirar.

Voce vai adaptar este artigo do blog do Renato para um post de LinkedIn.

METADADOS:
- Titulo: {title}
- Categoria: {categoria}
- Gancho sugerido: {gancho}
- Keywords: {keywords}

CONTEUDO DO ARTIGO (FONTE DE VERDADE):
{article_content}

REGRA CRITICA DE FATOS:
- Use APENAS numeros, estatisticas e exemplos que aparecem no conteudo do artigo acima.
- Se o artigo nao tem stats numericas, NAO INVENTE. Use afirmacoes qualitativas
  ("a maioria das empresas", "uma tendencia crescente", "muitos conselhos").
- Se o artigo tem stats, pode citar — sem source ("Segundo o estudo X..."). Renato
  e o autor; ele sabe a fonte.

ESTRUTURA OBRIGATORIA:
1. Abre com o gancho (ou variante). Linha de impacto, max 150 chars.
2. Linha em branco. Uma frase de contexto/aposta.
3. Linha em branco. Bloco de 3-5 bullets com "→" (insights/consequencias praticas
   tiradas do artigo).
4. Linha em branco. Uma pergunta provocativa pra conselheiros/lideres.
5. Linha em branco. CTA suave: "Escrevi sobre isso no blog 👇" (sem colar URL — o LinkedIn vai mostrar o card).

NAO inclua hashtags — sao adicionadas depois pelo publicador.
NAO inclua a URL do artigo no corpo — vira card automatico.
TAMANHO ALVO: 600-900 chars (sem hashtags).

Responda APENAS com o texto do post, sem aspas envolvendo, sem markdown."""


async def fetch_article_content(client: httpx.AsyncClient, url: str) -> str | None:
    """Fetch HTML and extract <article> body text. Returns None on failure."""
    if not url:
        return None
    try:
        resp = await client.get(url, headers=HTTP_HEADERS, timeout=20.0, follow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        el = soup.select_one("article") or soup.select_one("main")
        if not el:
            return None
        text = el.get_text(separator="\n", strip=True)
        return text if len(text) >= ARTICLE_MIN_CHARS else None
    except Exception:
        return None


async def generate_body(client: httpx.AsyncClient, post: dict, article_content: str | None) -> str | None:
    keywords = post.get("ai_keywords") or "[]"
    if isinstance(keywords, str):
        try:
            keywords = ", ".join(json.loads(keywords))
        except Exception:
            keywords = ""
    elif isinstance(keywords, list):
        keywords = ", ".join(keywords)

    content_block = article_content[:6000]

    prompt = PROMPT.format(
        title=post.get("article_title") or "",
        categoria=post.get("ai_categoria") or "(sem categoria)",
        gancho=post.get("ai_gancho_linkedin") or "(sem gancho — invente um)",
        keywords=keywords,
        article_content=content_block,
    )

    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60.0,
    )
    if resp.status_code != 200:
        print(f"  ❌ Claude API {resp.status_code}: {resp.text[:200]}")
        return None
    text = resp.json()["content"][0]["text"].strip()
    text = text.strip('"').strip("'").strip()
    return text


async def main_async(args):
    if not ANTHROPIC_API_KEY:
        sys.exit("ERRO: ANTHROPIC_API_KEY nao configurada no .env")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, article_title, article_description, article_url,
                   ai_categoria, ai_gancho_linkedin, ai_keywords,
                   ai_score_relevancia
            FROM editorial_posts
            WHERE status = 'draft'
              AND tipo = 'repost'
              AND conteudo_adaptado IS NULL
              AND article_title IS NOT NULL
              AND article_url IS NOT NULL
              AND article_url <> ''
            ORDER BY ai_score_relevancia DESC NULLS LAST, criado_em DESC
            LIMIT %s
            """,
            (args.limit,),
        )
        posts = [dict(r) for r in cur.fetchall()]

    if not posts:
        print("Nenhum repost elegivel pra adaptar.")
        return

    print(
        f"📝 {len(posts)} reposts pra adaptar "
        f"({'DRY-RUN' if not args.apply else 'APLICANDO'}, "
        f"{'REMOTE/Neon' if args.remote else 'local'})"
    )
    print()

    ok = 0
    fail = 0
    async with httpx.AsyncClient() as client:
        for i, post in enumerate(posts, 1):
            score = post.get("ai_score_relevancia") or "?"
            print(f"[{i}/{len(posts)}] id={post['id']} (score={score}) {post['article_title'][:60]}")
            article_content = await fetch_article_content(client, post.get("article_url") or "")
            if not article_content:
                print(f"  ⏭️  pulado: article_url retorna 404 ou body curto (LinkedIn card quebraria)")
                fail += 1
                continue
            print(f"  📄 article fetched: {len(article_content)} chars")
            body = await generate_body(client, post, article_content)
            if not body:
                fail += 1
                continue
            print(f"  ✅ {len(body)} chars")
            if args.verbose:
                print("  ---")
                for line in body.split("\n"):
                    print(f"  | {line}")
                print("  ---")

            if args.apply:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        UPDATE editorial_posts
                        SET conteudo_adaptado = %s, atualizado_em = NOW()
                        WHERE id = %s AND conteudo_adaptado IS NULL
                        """,
                        (body, post["id"]),
                    )
                    conn.commit()
            ok += 1

    print()
    print(f"Total ok: {ok} | fail: {fail}")
    if not args.apply:
        print("(dry-run — rode com --apply pra persistir)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5, help="Quantos reposts processar (default 5)")
    parser.add_argument("--apply", action="store_true", help="Persiste no banco. Sem isso, dry-run.")
    parser.add_argument("--remote", action="store_true", help="Aplica direto no Neon (prod). Default: local.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print full generated body")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
