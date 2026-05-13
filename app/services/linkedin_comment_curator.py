"""LinkedIn Comment Curator (P1 do roadmap docs/ROADMAP_LINKEDIN_COMMENT_TOOL.md).

Roda em BG diariamente (cron 7h BRT) — varre tasks "LinkedIn: Curtir post de X"
pendentes, busca o post via LinkdAPI, gera scoring (Sonnet 4.6) e, quando o
score >= threshold (default 7), gera drafts A/B + DM follow-up via Opus 4.7.

Tudo persiste em `linkedin_task_data` (sidecar ja existente, estendida na
migration 009). Drafts ad-hoc (rota /linkedin/comentar) vao pra
`linkedin_adhoc_drafts`.

Modelos:
- Scoring: claude-sonnet-4-6 (rapido + barato — ~$0.01/call)
- Drafts: claude-opus-4-7 (qualidade superior — ~$0.10/call, so chama se
  score >= threshold pra controlar custo)

Env vars:
- LINKEDIN_CURATOR_SCORE_THRESHOLD=7 (int, gating pra Opus)
- LINKEDIN_CURATOR_MODEL_SCORING=claude-sonnet-4-6
- LINKEDIN_CURATOR_MODEL_DRAFT=claude-opus-4-7
- LINKEDIN_CURATOR_MAX_DAILY=15 (cap diario de analises)
- ANTHROPIC_API_KEY (obrigatorio)
- LINKDAPI_KEY (obrigatorio pra fetch_post_via_linkdapi)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from psycopg2.extras import Json

from database import get_db
from services.tz import now_utc

logger = logging.getLogger(__name__)

# ---- Config ----
LINKDAPI_BASE = "https://linkdapi.com"
SCORE_THRESHOLD = int((os.getenv("LINKEDIN_CURATOR_SCORE_THRESHOLD") or "7").strip() or 7)
MODEL_SCORING = (os.getenv("LINKEDIN_CURATOR_MODEL_SCORING") or "claude-sonnet-4-6").strip()
MODEL_DRAFT = (os.getenv("LINKEDIN_CURATOR_MODEL_DRAFT") or "claude-opus-4-7").strip()
MAX_DAILY = int((os.getenv("LINKEDIN_CURATOR_MAX_DAILY") or "15").strip() or 15)

# ---- Cached prompt assets ----
_BASE_DIR = Path(__file__).resolve().parent.parent.parent  # prospect-system/
_CV_CACHE_PATH = _BASE_DIR / "data" / "curator" / "cv_renato.txt"
_FRAMEWORK_PATH = (
    Path.home()
    / ".claude"
    / "projects"
    / "-Users-rap-prospect-system"
    / "memory"
    / "reference_linkedin_comment_framework.md"
)

# Lazy-loaded cache
_cached_cv: Optional[str] = None
_cached_framework: Optional[str] = None


def _load_cv() -> str:
    """Carrega CV do Renato cacheado em disco. Extrai do PDF na primeira vez."""
    global _cached_cv
    if _cached_cv is not None:
        return _cached_cv

    if _CV_CACHE_PATH.exists():
        try:
            _cached_cv = _CV_CACHE_PATH.read_text(encoding="utf-8")
            return _cached_cv
        except Exception as e:
            logger.warning(f"_load_cv: leitura do cache falhou: {e}")

    # Fallback: tenta extrair do PDF
    pdf_path = Path(
        "/Users/rap/Library/CloudStorage/GoogleDrive-renato@almeida-prado.com/"
        "My Drive/APConselhos/Marketing/Curriculum/"
        "Curriculum_Renato_de_Faria_e_Almeida_Prado.pdf"
    )
    if not pdf_path.exists():
        logger.warning(f"_load_cv: PDF nao encontrado em {pdf_path}, usando fallback")
        _cached_cv = _CV_FALLBACK
        return _cached_cv

    try:
        import PyPDF2

        with pdf_path.open("rb") as f:
            reader = PyPDF2.PdfReader(f)
            text = "\n".join(p.extract_text() for p in reader.pages)
        _CV_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CV_CACHE_PATH.write_text(text, encoding="utf-8")
        _cached_cv = text
        return text
    except Exception as e:
        logger.warning(f"_load_cv: extracao do PDF falhou: {e}")
        _cached_cv = _CV_FALLBACK
        return _cached_cv


def _load_framework() -> str:
    """Carrega o framework de comentarios da memory."""
    global _cached_framework
    if _cached_framework is not None:
        return _cached_framework

    if _FRAMEWORK_PATH.exists():
        try:
            _cached_framework = _FRAMEWORK_PATH.read_text(encoding="utf-8")
            return _cached_framework
        except Exception as e:
            logger.warning(f"_load_framework: leitura falhou: {e}")

    _cached_framework = _FRAMEWORK_FALLBACK
    return _cached_framework


# Fallbacks pra caso os arquivos nao estejam disponiveis (prod via Vercel
# nao tem acesso ao Google Drive local nem a memory do usuario)
_CV_FALLBACK = """
Renato de Faria e Almeida Prado — Conselheiro Independente, fundador imensIAH.

Background:
- Engenheiro de Producao (Poli/USP) + Direito (Mackenzie) + MBA UCLA Anderson
- Founder Natique 1996-2013 (CPG/bebidas) — vendeu pra Osborne (multinacional
  espanhola 400 anos) em 2017, com 115% de growth no periodo
- Conselheiro IBGC certificado + 5 conselhos ativos atuais
- Cofundador 10xMentorAI / founder imensIAH (AI aplicada a planejamento
  estrategico, ICP founder PME)
- Penn AI Applications (2023) + AI on Board (Board Academy 2024)
- Carambola B-Corp Best of The World 2021/2022 (ESG, proposito)
- Pare.Net (M&A execution) + Suriana (internacionalizacao Brasil-Japao-EUA)
"""

_FRAMEWORK_FALLBACK = """
Framework de comentarios LinkedIn:
1. Ancorar no especifico (citar numero/caso/frase concreta do post)
2. Insight unico do background (engenheiro USP + MBA UCLA + Natique/Osborne
   + IBGC + 10xMentorAI/imensIAH + Penn AI). Background valida insight, nao
   eh o ponto.
3. NUNCA pitch publico (imensIAH/oferta) em comentario aberto. DM privada depois.
4. Convidar continuidade — pergunta nominal ou observacao que abre conversa.

Estrutura 3-4 frases:
[1] Ancoragem ao detalhe especifico do post
[2] Insight unico via background
[3] Conexao com tema/tese central
[4] Pergunta nominal ou observacao que abre conversa

DM follow-up (1-2 dias depois, NAO imediato):
"[Nome], vi sua [post/entrevista/case] no [veiculo]. O ponto sobre [especifico]
ressoou bastante — [conexao curta com experiencia propria]. Recentemente venho
ajudando [perfil similar] via imensIAH (plataforma de planejamento estrategico
AI-native). Se fizer sentido, adoraria trocar uma ideia — sem agenda comercial,
so perspectiva de quem ja fez. Abraco."
"""


# ============================================================================
# URL extraction
# ============================================================================

_POST_URL_RE = re.compile(
    r"(https://www\.linkedin\.com/feed/update/urn:li:activity:\d+|"
    r"https://www\.linkedin\.com/posts/[^\s)]+)",
    re.IGNORECASE,
)


def extract_post_url_from_task(task_descricao: Optional[str]) -> Optional[str]:
    """Extrai URL do post LinkedIn de uma descricao de task.

    Padroes aceitos (campanha cria com "Abrir post: <url>"):
    - https://www.linkedin.com/feed/update/urn:li:activity:NNN
    - https://www.linkedin.com/posts/SLUG-activity-NNN-XYZ
    """
    if not task_descricao:
        return None
    m = _POST_URL_RE.search(task_descricao)
    if not m:
        return None
    return m.group(0).rstrip(".,;)\"")


def _extract_post_urn(post_url: str) -> Optional[str]:
    """Reusa logica do linkedin_outbound_monitor pra extrair urn:li:activity:NNN."""
    if not post_url:
        return None
    m = re.search(r"urn:li:activity:(\d+)", post_url)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    m = re.search(r"activity[-:](\d{15,})", post_url)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    m = re.search(r"-(\d{15,20})-[a-zA-Z0-9_]+/?(?:[?#]|$)", post_url)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    return None


# ============================================================================
# LinkdAPI fetch
# ============================================================================


def _track_linkdapi_call(endpoint: str, status_code: int) -> None:
    """Telemetria nao deve quebrar o curator."""
    try:
        from services.linkedin_funnel import track_linkdapi_call

        track_linkdapi_call(endpoint, status_code)
    except Exception:
        logger.debug(f"_track_linkdapi_call({endpoint}) telemetria offline")


def fetch_post_via_linkdapi(post_url: str) -> Dict:
    """Busca dados do post via LinkdAPI /api/v1/posts/info.

    Retorna dict:
    {
      "ok": bool,
      "post_url": str,
      "post_text": str,
      "post_posted_at": str (ISO),
      "post_engagements": dict,
      "author_name": str,
      "author_headline": str,
      "author_urn": str,
      "error": str (opcional)
    }
    """
    if not post_url:
        return {"ok": False, "error": "post_url vazio"}

    api_key = (os.getenv("LINKDAPI_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "error": "LINKDAPI_KEY ausente"}

    post_urn = _extract_post_urn(post_url)
    if not post_urn:
        return {"ok": False, "error": f"post_urn nao extraido de {post_url}"}

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                f"{LINKDAPI_BASE}/api/v1/posts/info",
                headers={"X-linkdapi-apikey": api_key},
                params={"urn": post_urn},
            )
            _track_linkdapi_call("posts/info", resp.status_code)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "error": f"LinkdAPI HTTP {resp.status_code}",
                    "post_urn": post_urn,
                }
            data = (resp.json() or {}).get("data") or {}
            post = data.get("post") or {}
            author = post.get("author") or {}
            # postedAt pode vir como string OU como dict {timestamp, fullDate, relativeDay}
            posted_at_raw = post.get("postedAt")
            if isinstance(posted_at_raw, dict):
                posted_at_str = (
                    posted_at_raw.get("fullDate")
                    or posted_at_raw.get("relativeDay")
                    or str(posted_at_raw.get("timestamp") or "")
                )
            else:
                posted_at_str = posted_at_raw or ""
            return {
                "ok": True,
                "post_url": post.get("url") or post_url,
                "post_urn": post.get("urn") or post_urn,
                "post_text": post.get("text") or "",
                "post_posted_at": posted_at_str,
                "post_engagements": post.get("engagements"),
                "author_name": author.get("name") or "",
                "author_headline": author.get("headline") or "",
                "author_urn": author.get("urn") or "",
            }
    except Exception as e:
        _track_linkdapi_call("posts/info", 0)
        logger.warning(f"fetch_post_via_linkdapi({post_url}) excecao: {e}")
        return {"ok": False, "error": str(e), "post_urn": post_urn}


# ============================================================================
# Claude — Scoring (Sonnet)
# ============================================================================


def _get_anthropic_client():
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY ausente")
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


_SCORING_SYSTEM_PROMPT = """Voce eh um curador estrategico de comentarios LinkedIn pra Renato Almeida Prado.

CONTEXTO DO RENATO:
{cv}

FRAMEWORK DE COMENTARIOS:
{framework}

OBJETIVO: avaliar se um post LinkedIn vale a pena receber comentario do Renato.

CRITERIOS DE SCORING (1-10):
- 10: tema central da tese imensIAH (AI aplicada a estrategia/PME/founders), Renato tem credito unico, autor eh prospect alvo
- 8-9: tema relevante, Renato tem credito raro, autor pode virar relacionamento
- 6-7: tema interessante mas credito nao eh unico, ou autor pouco aderente
- 4-5: tema ok mas comentario seria generico (sem ancoragem unica)
- 2-3: tema fora do escopo (esporte casual, comida, viagem, festas, etc)
- 1: post sensivel (politica partidaria, religiao, crise pessoal, luto) — pula

SINALIZE BAIXO PRA:
- Self-promotion puro de produto sem substancia
- Posts genericos "motivacionais"
- Politica partidaria explicita
- Tragedias pessoais / luto / crise

Responda APENAS um JSON valido com este shape exato:
{{
  "score": <inteiro 1-10>,
  "vale_comentar": <true|false>,
  "ai_verdict": "<comment|like_only|skip>",
  "ai_angle": "<frase curta com angulo recomendado, ou null se nao vale>",
  "ai_rationale": "<2-3 frases explicando o score>"
}}
"""


def score_post(
    post_data: Dict,
    author_dossier: Optional[str] = None,
    framework_md: Optional[str] = None,
    cv_md: Optional[str] = None,
) -> Dict:
    """Scoring rapido via Sonnet. Retorna dict com score, verdict, etc."""
    cv = cv_md or _load_cv()
    framework = framework_md or _load_framework()
    system_prompt = _SCORING_SYSTEM_PROMPT.format(cv=cv, framework=framework)

    author_section = ""
    if author_dossier:
        author_section = f"\n\nDossie do autor (se relevante):\n{author_dossier}\n"

    eng = post_data.get("post_engagements") or {}
    eng_str = json.dumps(eng, ensure_ascii=False) if eng else "n/a"

    user_msg = f"""POST PARA AVALIAR:

Autor: {post_data.get("author_name") or "desconhecido"}
Headline: {post_data.get("author_headline") or "n/a"}
Postado em: {post_data.get("post_posted_at") or "n/a"}
Engajamento: {eng_str}

TEXTO DO POST:
\"\"\"
{post_data.get("post_text") or "(sem texto)"}
\"\"\"
{author_section}

Avalie e retorne o JSON."""

    try:
        client = _get_anthropic_client()
        msg = client.messages.create(
            model=MODEL_SCORING,
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
        # Extract JSON if wrapped in markdown
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            if m:
                raw = m.group(1)
        parsed = json.loads(raw)
        return {
            "ok": True,
            "score": int(parsed.get("score") or 0),
            "vale_comentar": bool(parsed.get("vale_comentar")),
            "ai_verdict": parsed.get("ai_verdict") or "skip",
            "ai_angle": parsed.get("ai_angle"),
            "ai_rationale": parsed.get("ai_rationale") or "",
            "model": MODEL_SCORING,
        }
    except Exception as e:
        logger.warning(f"score_post falhou: {e}")
        return {"ok": False, "error": str(e), "score": 0, "vale_comentar": False}


# ============================================================================
# Claude — Generate Drafts (Opus)
# ============================================================================

_DRAFT_SYSTEM_PROMPT = """Voce gera drafts de comentario LinkedIn pra Renato Almeida Prado.

CONTEXTO DO RENATO:
{cv}

FRAMEWORK (obrigatorio seguir):
{framework}

REGRAS:
- 3-4 frases por draft, max ~600 caracteres
- Ancorar em detalhe ESPECIFICO do post (numero, caso, frase concreta)
- Usar 1 credito do background do Renato como CONTEXTO, NUNCA como pitch
- Terminar com pergunta nominal ("[Nome], ...?") ou observacao que abre conversa
- NUNCA mencionar imensIAH/10xMentorAI/oferta de servico no comentario publico
- Tom: par tecnico, nao fan, nao bajulador

GERE 3 ARTEFATOS:
1. **draft_a**: versao "mais ousada" — angulo mais especifico/raro do background
2. **draft_b**: versao "mais segura" — angulo mais consensual mas ainda unico
3. **draft_dm**: mensagem privada de follow-up D+2 (segue padrao da framework — inclui soft mention imensIAH, mas sem agenda comercial)

Tambem recomende qual versao usar (A ou B) e por que.

Responda APENAS JSON valido com este shape:
{{
  "draft_a": "<texto>",
  "draft_b": "<texto>",
  "draft_dm": "<texto>",
  "draft_recommended": "<A|B>",
  "tradeoff_explanation": "<1-2 frases sobre o tradeoff A vs B>"
}}
"""


def generate_drafts(
    post_data: Dict,
    scoring_result: Dict,
    author_dossier: Optional[str] = None,
    framework_md: Optional[str] = None,
    cv_md: Optional[str] = None,
) -> Dict:
    """Gera drafts A/B + DM via Opus. So chamar quando score >= threshold."""
    cv = cv_md or _load_cv()
    framework = framework_md or _load_framework()
    system_prompt = _DRAFT_SYSTEM_PROMPT.format(cv=cv, framework=framework)

    angulo = scoring_result.get("ai_angle") or "angulo livre"
    rationale = scoring_result.get("ai_rationale") or ""

    author_section = ""
    if author_dossier:
        author_section = f"\nDossie do autor (use pra personalizar): {author_dossier}\n"

    user_msg = f"""POST:

Autor: {post_data.get("author_name") or "desconhecido"}
Headline: {post_data.get("author_headline") or "n/a"}

TEXTO:
\"\"\"
{post_data.get("post_text") or ""}
\"\"\"

ANGULO RECOMENDADO (da analise previa): {angulo}
RAZAO: {rationale}
{author_section}

Gere os drafts. Retorne o JSON."""

    try:
        client = _get_anthropic_client()
        msg = client.messages.create(
            model=MODEL_DRAFT,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            if m:
                raw = m.group(1)
        parsed = json.loads(raw)
        return {
            "ok": True,
            "draft_a": parsed.get("draft_a") or "",
            "draft_b": parsed.get("draft_b") or "",
            "draft_dm": parsed.get("draft_dm") or "",
            "draft_recommended": (parsed.get("draft_recommended") or "A").upper(),
            "tradeoff_explanation": parsed.get("tradeoff_explanation") or "",
            "model": MODEL_DRAFT,
        }
    except Exception as e:
        logger.warning(f"generate_drafts falhou: {e}")
        return {"ok": False, "error": str(e)}


# ============================================================================
# Orchestrator — analyze_task
# ============================================================================


def _fetch_task_descricao(task_id: int) -> Optional[Dict]:
    """Retorna {descricao, contact_id, contact_nome, dossie_linkedin} ou None."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT t.id, t.titulo, t.descricao, t.contact_id,
                   c.nome AS contact_nome, c.linkedin AS contact_linkedin,
                   c.dossie_linkedin AS contact_dossier
            FROM tasks t
            LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.id = %s
            """,
            (task_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def _persist_task_data(task_id: int, post: Dict, scoring: Dict, drafts: Optional[Dict]) -> None:
    """Upsert em linkedin_task_data com dados completos da analise."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO linkedin_task_data (
                task_id, post_url, post_text, post_posted_at, post_engagements,
                post_author_name, post_author_headline, post_author_urn,
                ai_verdict, ai_rationale, ai_angle, ai_ran_at,
                score_numeric, draft_a, draft_b, draft_dm, draft_recommended,
                fetched_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, NOW(),
                %s, %s, %s, %s, %s,
                NOW()
            )
            ON CONFLICT (task_id) DO UPDATE SET
                post_url = EXCLUDED.post_url,
                post_text = COALESCE(NULLIF(EXCLUDED.post_text, ''), linkedin_task_data.post_text),
                post_posted_at = COALESCE(EXCLUDED.post_posted_at, linkedin_task_data.post_posted_at),
                post_engagements = COALESCE(EXCLUDED.post_engagements, linkedin_task_data.post_engagements),
                post_author_name = COALESCE(EXCLUDED.post_author_name, linkedin_task_data.post_author_name),
                post_author_headline = COALESCE(EXCLUDED.post_author_headline, linkedin_task_data.post_author_headline),
                post_author_urn = COALESCE(EXCLUDED.post_author_urn, linkedin_task_data.post_author_urn),
                ai_verdict = EXCLUDED.ai_verdict,
                ai_rationale = EXCLUDED.ai_rationale,
                ai_angle = EXCLUDED.ai_angle,
                ai_ran_at = NOW(),
                score_numeric = EXCLUDED.score_numeric,
                draft_a = COALESCE(EXCLUDED.draft_a, linkedin_task_data.draft_a),
                draft_b = COALESCE(EXCLUDED.draft_b, linkedin_task_data.draft_b),
                draft_dm = COALESCE(EXCLUDED.draft_dm, linkedin_task_data.draft_dm),
                draft_recommended = COALESCE(EXCLUDED.draft_recommended, linkedin_task_data.draft_recommended)
            """,
            (
                task_id,
                post.get("post_url") or "",
                post.get("post_text") or "",
                post.get("post_posted_at") or None,
                Json(post.get("post_engagements")) if post.get("post_engagements") else None,
                post.get("author_name") or None,
                post.get("author_headline") or None,
                post.get("author_urn") or None,
                scoring.get("ai_verdict"),
                scoring.get("ai_rationale"),
                scoring.get("ai_angle"),
                scoring.get("score"),
                (drafts or {}).get("draft_a"),
                (drafts or {}).get("draft_b"),
                (drafts or {}).get("draft_dm"),
                (drafts or {}).get("draft_recommended"),
            ),
        )
        conn.commit()


def analyze_task(task_id: int, force: bool = False) -> Dict:
    """Pipeline completo: extrai URL → fetch LinkdAPI → score → drafts (se score>=threshold) → persiste.

    Args:
        task_id: id da task
        force: se True, re-analisa mesmo se ja tem ai_ran_at
    """
    task = _fetch_task_descricao(task_id)
    if not task:
        return {"ok": False, "error": f"task {task_id} nao encontrada"}

    # Verifica se ja foi analisada (idempotencia)
    if not force:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ai_ran_at, score_numeric FROM linkedin_task_data WHERE task_id = %s",
                (task_id,),
            )
            existing = cursor.fetchone()
            if existing and existing["ai_ran_at"]:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_analyzed",
                    "task_id": task_id,
                    "score": existing.get("score_numeric"),
                }

    # 1) Extract URL
    post_url = extract_post_url_from_task(task.get("descricao"))
    if not post_url:
        return {"ok": False, "error": "url do post nao extraida da descricao", "task_id": task_id}

    # 2) Fetch via LinkdAPI
    post = fetch_post_via_linkdapi(post_url)
    if not post.get("ok"):
        # Fallback: pode ter dados no sidecar existente
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT post_url, post_text, post_posted_at, post_engagements FROM linkedin_task_data WHERE task_id = %s",
                (task_id,),
            )
            sidecar = cursor.fetchone()
        if sidecar and sidecar.get("post_text"):
            post = {
                "ok": True,
                "post_url": sidecar["post_url"],
                "post_text": sidecar["post_text"],
                "post_posted_at": sidecar.get("post_posted_at"),
                "post_engagements": sidecar.get("post_engagements"),
                "author_name": task.get("contact_nome") or "",
                "author_headline": "",
                "author_urn": "",
            }
            logger.info(f"analyze_task({task_id}): usando sidecar como fallback")
        else:
            return {
                "ok": False,
                "error": f"LinkdAPI falhou ({post.get('error')}) e sem sidecar",
                "task_id": task_id,
            }

    # Author dossier (se contact tem)
    author_dossier = task.get("contact_dossier")

    # 3) Scoring
    scoring = score_post(post, author_dossier=author_dossier)
    if not scoring.get("ok"):
        # Persiste pelo menos os dados do post + erro
        _persist_task_data(task_id, post, {"score": 0, "ai_verdict": "skip", "ai_rationale": f"scoring_error: {scoring.get('error')}"}, None)
        return {"ok": False, "error": f"scoring falhou: {scoring.get('error')}", "task_id": task_id}

    # 4) Drafts (so se score >= threshold)
    drafts = None
    if scoring.get("score", 0) >= SCORE_THRESHOLD and scoring.get("vale_comentar"):
        drafts = generate_drafts(post, scoring, author_dossier=author_dossier)
        if not drafts.get("ok"):
            logger.warning(f"analyze_task({task_id}): drafts falharam: {drafts.get('error')}")
            drafts = None  # persiste sem drafts; nao bloqueia o flow

    # 5) Persiste
    _persist_task_data(task_id, post, scoring, drafts)

    return {
        "ok": True,
        "task_id": task_id,
        "score": scoring.get("score"),
        "vale_comentar": scoring.get("vale_comentar"),
        "ai_verdict": scoring.get("ai_verdict"),
        "ai_angle": scoring.get("ai_angle"),
        "drafts_generated": bool(drafts and drafts.get("ok")),
    }


# ============================================================================
# Daily runner
# ============================================================================


def fetch_pending_linkedin_tasks(limit: int = 50) -> List[Dict]:
    """Lista tasks LinkedIn pendentes sem analise (ai_ran_at NULL) ou stale (>24h).

    Filtra tasks que tem URL real do post na descricao — tasks tipo "Buscar
    no LinkedIn" (sem URL especifica) nao podem ser analisadas automaticamente.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT t.id AS task_id, t.titulo, t.descricao
            FROM tasks t
            LEFT JOIN linkedin_task_data ltd ON ltd.task_id = t.id
            WHERE t.titulo ILIKE 'LinkedIn: Curtir post de%%'
              AND t.status = 'pending'
              AND (ltd.ai_ran_at IS NULL OR ltd.ai_ran_at < NOW() - INTERVAL '24 hours')
              AND t.descricao ~ 'linkedin\\.com/(feed/update|posts)'
            ORDER BY t.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]


def run_daily_curator(max_tasks: Optional[int] = None) -> Dict:
    """Loop principal do cron diario.

    1. Lista tasks pendentes sem analise
    2. Cap em MAX_DAILY (env, default 15)
    3. Pra cada task, chama analyze_task
    4. Retorna sumario com counts por score range
    """
    cap = max_tasks if max_tasks is not None else MAX_DAILY
    tasks = fetch_pending_linkedin_tasks(limit=cap)

    summary = {
        "total_pending": 0,
        "analyzed": 0,
        "errors": 0,
        "high_score": 0,    # >=7
        "med_score": 0,     # 5-6
        "low_score": 0,     # <5
        "drafts_generated": 0,
        "task_ids_processed": [],
        "errors_detail": [],
    }

    summary["total_pending"] = len(tasks)

    for t in tasks:
        try:
            res = analyze_task(t["task_id"])
            summary["task_ids_processed"].append(t["task_id"])
            if res.get("ok"):
                summary["analyzed"] += 1
                score = res.get("score") or 0
                if score >= 7:
                    summary["high_score"] += 1
                elif score >= 5:
                    summary["med_score"] += 1
                else:
                    summary["low_score"] += 1
                if res.get("drafts_generated"):
                    summary["drafts_generated"] += 1
            else:
                summary["errors"] += 1
                summary["errors_detail"].append({"task_id": t["task_id"], "error": res.get("error")})
        except Exception as e:
            summary["errors"] += 1
            summary["errors_detail"].append({"task_id": t["task_id"], "error": str(e)})
            logger.exception(f"run_daily_curator: task {t['task_id']} falhou")

        # Rate limit modesto (LinkdAPI ~30 req/min + cuidado com Anthropic)
        time.sleep(0.5)

    summary["ran_at"] = now_utc().isoformat()
    return summary


# ============================================================================
# Ad-hoc analysis (rota /linkedin/comentar)
# ============================================================================


def analyze_adhoc(
    post_url: Optional[str] = None,
    post_text: Optional[str] = None,
    author_name: Optional[str] = None,
    author_headline: Optional[str] = None,
    created_by_user_id: Optional[int] = None,
) -> Dict:
    """Analise ad-hoc — recebe URL OU texto colado + autor opcional.

    Se URL fornecida, busca via LinkdAPI; senao usa texto direto.
    Persiste em linkedin_adhoc_drafts.
    """
    # 1) Fetch
    post: Dict = {}
    if post_url:
        fetched = fetch_post_via_linkdapi(post_url)
        if fetched.get("ok"):
            post = fetched
        else:
            logger.warning(f"analyze_adhoc: LinkdAPI falhou pra {post_url} ({fetched.get('error')})")

    if not post.get("ok"):
        # Fallback: usa o que foi colado
        if not post_text:
            return {"ok": False, "error": "sem URL valida nem texto colado"}
        post = {
            "ok": True,
            "post_url": post_url or "",
            "post_text": post_text,
            "post_posted_at": "",
            "post_engagements": None,
            "author_name": author_name or "",
            "author_headline": author_headline or "",
            "author_urn": "",
        }

    # 2) Scoring
    scoring = score_post(post)
    if not scoring.get("ok"):
        return {"ok": False, "error": f"scoring falhou: {scoring.get('error')}"}

    # 3) Drafts (always for ad-hoc, since user paid attention manually)
    drafts = generate_drafts(post, scoring)
    if not drafts.get("ok"):
        drafts = None

    # 4) Persiste
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO linkedin_adhoc_drafts (
                post_url, post_text, post_posted_at, post_engagements,
                post_author_name, post_author_headline, post_author_urn,
                score_numeric, ai_verdict, ai_rationale, ai_angle, ai_ran_at,
                draft_a, draft_b, draft_dm, draft_recommended,
                created_by_user_id
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, NOW(),
                %s, %s, %s, %s,
                %s
            ) RETURNING id
            """,
            (
                post.get("post_url") or None,
                post.get("post_text") or "",
                post.get("post_posted_at") or None,
                Json(post.get("post_engagements")) if post.get("post_engagements") else None,
                post.get("author_name") or None,
                post.get("author_headline") or None,
                post.get("author_urn") or None,
                scoring.get("score"),
                scoring.get("ai_verdict"),
                scoring.get("ai_rationale"),
                scoring.get("ai_angle"),
                (drafts or {}).get("draft_a"),
                (drafts or {}).get("draft_b"),
                (drafts or {}).get("draft_dm"),
                (drafts or {}).get("draft_recommended"),
                created_by_user_id,
            ),
        )
        new_id = cursor.fetchone()["id"]
        conn.commit()

    return {
        "ok": True,
        "id": new_id,
        "score": scoring.get("score"),
        "ai_verdict": scoring.get("ai_verdict"),
        "ai_angle": scoring.get("ai_angle"),
        "ai_rationale": scoring.get("ai_rationale"),
        "draft_a": (drafts or {}).get("draft_a"),
        "draft_b": (drafts or {}).get("draft_b"),
        "draft_dm": (drafts or {}).get("draft_dm"),
        "draft_recommended": (drafts or {}).get("draft_recommended"),
        "tradeoff_explanation": (drafts or {}).get("tradeoff_explanation"),
        "post_text": post.get("post_text"),
        "post_url": post.get("post_url"),
        "author_name": post.get("author_name"),
        "author_headline": post.get("author_headline"),
    }
