"""Gera drafts de resposta a comentarios INBOUND nos posts do Renato.

Diferente do linkedin_comment_curator (que gera comments OUTBOUND em posts de
terceiros), aqui o Renato eh AUTOR do post e quer responder de forma que:
1. Reconheca o ponto do comentarista
2. Adicione 1 insight do CV/experiencia (sem repetir o post)
3. Convide continuidade quando faz sentido (DM, conexao, troca)

Modelo: claude-sonnet-4-6 (Opus seria overkill — o framework + voz ja estao
estabelecidos, basta combinar bem). ~$0.01/call.
"""
from __future__ import annotations

import json
from services import llm
import logging
import os
import re
from typing import Dict, List, Optional

import httpx

from database import get_db
from services.linkedin_comment_curator import _load_cv, _load_framework

logger = logging.getLogger(__name__)

MODEL = (os.getenv("LINKEDIN_REPLY_DRAFTER_MODEL") or llm.BALANCED).strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()


_SYSTEM_PROMPT = """Voce escreve respostas no LinkedIn pelo Renato de Faria e Almeida Prado.

CONTEXTO ESPECIFICO DESTE PROMPT: o Renato JA publicou um post. Alguem
comentou. Voce vai redigir 2 variantes de resposta ao comentario.

CV de Renato (use background pra ancorar insights — nao repetir, so usar):
{cv}

Framework geral de tom:
{framework}

REGRAS PRA RESPOSTAS INBOUND (este caso):
1. Reconhecer o ponto do comentarista de forma genuina — nao "great point!"
   generico. Citar o detalhe especifico do que ele disse.
2. Adicionar 1 insight DO RENATO que eleva a thread — pode ser experiencia
   propria, dado, contraponto, exemplo de caso. Background valida, nao eh foco.
3. NAO repetir o post — quem comentou ja leu. Construir EM CIMA.
4. As vezes (nao sempre) convidar continuidade: pergunta nominal, "vale
   trocar uma ideia em DM", ou observacao que abre dialogo. Avaliar pelo tom
   do comentario — se foi raso/elogio, encerrar com agradecimento real;
   se foi substantivo, abrir conversa.
5. Sem pitch comercial (imensIAH/oferta) em comment publico. DM depois.

TAMANHO: 2-4 frases. Resposta curta funciona melhor no scroll do LinkedIn.

ESTILO DOS DRAFTS (escolher tom):
- Draft A — Tom analitico/dados-ancorado: usa um numero, caso especifico ou
  framework. Pra comentarios substantivos/tecnicos.
- Draft B — Tom mais caloroso/convidativo: pra abrir conversa, pra
  comentaristas que demonstraram afinidade pessoal/curiosidade.

OUTPUT — JSON puro, sem markdown:
{{
  "draft_a": "<texto da resposta tecnica, 2-4 frases>",
  "draft_b": "<texto da resposta calorosa, 2-4 frases>",
  "recommended": "A" ou "B",
  "tradeoff": "<1 frase explicando quando escolher cada>"
}}"""


async def generate_reply_drafts(signal_id: int) -> Dict:
    """Gera 2 drafts de resposta pra um engagement_signal.

    Returns: {ok, draft_a, draft_b, recommended, tradeoff, signal_id} ou {ok:False, error}
    """
    if not ANTHROPIC_API_KEY:
        return {"ok": False, "error": "ANTHROPIC_API_KEY nao configurada"}

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.id, s.comment_text, s.profile_name, s.profile_headline,
                   s.post_id,
                   p.article_title AS post_title,
                   p.conteudo_adaptado AS post_body
            FROM linkedin_engagement_signals s
            LEFT JOIN editorial_posts p ON p.id = s.post_id
            WHERE s.id = %s
            """,
            (signal_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"ok": False, "error": f"signal {signal_id} nao encontrado"}

    sig = dict(row)
    if not (sig.get("comment_text") or "").strip():
        return {"ok": False, "error": "signal sem comment_text"}

    post_title = sig.get("post_title") or "(sem titulo)"
    post_body = (sig.get("post_body") or "")[:1500]
    profile = sig.get("profile_name") or "?"
    headline = sig.get("profile_headline") or ""
    comment = sig.get("comment_text") or ""

    user_msg = f"""POST DO RENATO (autor: voce):

TITULO: {post_title}

CORPO:
\"\"\"
{post_body}
\"\"\"

COMENTARISTA:
- Nome: {profile}
- Headline: {headline or "(sem headline)"}

COMENTARIO QUE EQUIPE VAI RESPONDER:
\"\"\"
{comment}
\"\"\"

Gere os 2 drafts em JSON."""

    system_prompt = _SYSTEM_PROMPT.format(cv=_load_cv(), framework=_load_framework())

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 1500,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_msg}],
                },
            )
        if resp.status_code != 200:
            return {"ok": False, "error": f"Claude {resp.status_code}: {resp.text[:200]}"}
        raw = resp.json()["content"][0]["text"].strip()
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            if m:
                raw = m.group(1)
        parsed = json.loads(raw)
        return {
            "ok": True,
            "signal_id": signal_id,
            "draft_a": parsed.get("draft_a") or "",
            "draft_b": parsed.get("draft_b") or "",
            "recommended": (parsed.get("recommended") or "A").upper(),
            "tradeoff": parsed.get("tradeoff") or "",
            "model": MODEL,
        }
    except Exception as e:
        logger.warning(f"generate_reply_drafts({signal_id}) falhou: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
