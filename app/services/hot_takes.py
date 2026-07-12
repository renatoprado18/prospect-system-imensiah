"""
Hot Takes Service - Conecta notícias quentes com conteúdo de governança
Version: 2.0 - With comprehensive error handling

Fluxo:
1. Busca notícias de fontes RSS (Google News, etc)
2. Filtra por relevância usando IA
3. Gera "hot takes" conectando com artigos existentes
4. Salva para revisão e agendamento
"""

import httpx
from services import llm
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional
import os
import re

from database import get_db

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Fontes de notícias RSS
NEWS_SOURCES = {
    "google_business": {
        "name": "Google News - Business",
        "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FuQjBHZ0pDVWlnQVAB?hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "business"
    },
    "google_tech": {
        "name": "Google News - Technology",
        "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FuQjBHZ0pDVWlnQVAB?hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "tech"
    },
    "google_world": {
        "name": "Google News - World",
        "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp0Y1RjU0FuQjBHZ0pDVWlnQVAB?hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "world"
    }
}

# Tópicos de interesse para filtro
TOPICS_OF_INTEREST = [
    "governança", "conselho", "CEO", "empresa", "corporativo",
    "fusão", "aquisição", "M&A", "IPO", "ações",
    "inteligência artificial", "IA", "AI", "tecnologia", "digital",
    "ESG", "sustentabilidade", "clima", "meio ambiente",
    "Trump", "Biden", "Lula", "economia", "mercado",
    "startup", "inovação", "disrupção",
    "crise", "recuperação judicial", "falência",
    "liderança", "gestão", "estratégia"
]

# Flag para evitar inicialização repetida
_table_initialized = False


def ensure_table_exists():
    """Garante que a tabela hot_takes existe (executa apenas uma vez)"""
    global _table_initialized
    if _table_initialized:
        return
    _table_initialized = True
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_takes (
                id SERIAL PRIMARY KEY,
                news_title TEXT,
                news_link TEXT,
                hook TEXT,
                body TEXT,
                cta TEXT,
                linkedin_post TEXT,
                article_slug TEXT,
                hashtags JSONB DEFAULT '[]',
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                scheduled_for TIMESTAMP,
                published_at TIMESTAMP,
                editorial_post_id INTEGER,
                linkedin_url TEXT,
                metrics JSONB DEFAULT '{}',
                ai_categoria TEXT
            )
        ''')
        # Add columns if they don't exist (for existing tables)
        for column, col_type in [
            ('editorial_post_id', 'INTEGER'),
            ('linkedin_url', 'TEXT'),
            ('metrics', "JSONB DEFAULT '{}'"),
            ('ai_categoria', 'TEXT'),
        ]:
            try:
                cursor.execute(f'ALTER TABLE hot_takes ADD COLUMN IF NOT EXISTS {column} {col_type}')
            except Exception:
                pass
        conn.commit()


# Categorias oficiais — fonte unica em editorial_calendar.CATEGORIAS_PRINCIPAIS.
# Era duplicada aqui (12 cats hardcoded) mas drift de taxonomia ja causou bug
# 18/05 quando expansao pra 16 cats nao refletiu no classifier. Agora importa.
from services.editorial_calendar import CATEGORIAS_PRINCIPAIS as HOT_TAKE_CATEGORIAS


async def classify_hot_take_category(news_title: str, body_or_post: str = "") -> Optional[str]:
    """Classifica hot_take numa das 12 categorias oficiais via Claude Haiku.

    Por que Haiku e nao Sonnet: classificacao 1-de-12 e tarefa simples,
    Haiku custa ~1/8 e responde em <2s. Sonnet so vale pra analise multi-campo
    (analyze_article_with_ai usa Sonnet pra 10 campos).

    Returns:
        String categoria (uma de HOT_TAKE_CATEGORIAS) ou None se classificacao
        falhou (API down, JSON invalido, etc). Caller decide se trata None como
        "passa pelo filtro" ou "bloqueia conservadoramente".
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("classify_hot_take_category: ANTHROPIC_API_KEY ausente")
        return None
    if not news_title:
        return None

    prompt = f"""Voce classifica conteudo de governanca corporativa e negocios em UMA das 12 categorias abaixo.

TITULO: {news_title}

CONTEXTO ADICIONAL: {(body_or_post or '')[:500]}

CATEGORIAS POSSIVEIS (escolha exatamente uma):
{chr(10).join('- ' + c for c in HOT_TAKE_CATEGORIAS)}

Responda APENAS com JSON: {{"categoria": "<nome exato de uma das categorias>"}}
"""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": llm.FAST,
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            logger.warning(f"classify_hot_take_category: API {resp.status_code}")
            return None
        text = resp.json()["content"][0]["text"]
        m = re.search(r'\{[\s\S]*\}', text)
        if not m:
            return None
        parsed = json.loads(m.group())
        cat = parsed.get("categoria", "").strip()
        # Valida que ta na lista; senao retorna None pra nao corromper dados.
        if cat in HOT_TAKE_CATEGORIAS:
            return cat
        # Tenta match case-insensitive como fallback (Claude as vezes capitaliza errado)
        for c in HOT_TAKE_CATEGORIAS:
            if c.lower() == cat.lower():
                return c
        logger.warning(f"classify_hot_take_category: categoria invalida '{cat}'")
        return None
    except Exception as e:
        logger.warning(f"classify_hot_take_category falhou: {type(e).__name__}: {e}")
        return None


def set_hot_take_categoria(hot_take_id: int, categoria: str) -> None:
    """Grava ai_categoria num hot_take existente. Idempotente."""
    if not categoria:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE hot_takes SET ai_categoria = %s WHERE id = %s",
                (categoria, hot_take_id),
            )
            conn.commit()
    except Exception:
        logger.exception(f"set_hot_take_categoria falhou pra {hot_take_id}")


async def ensure_hot_take_categoria(hot_take_id: int) -> Optional[str]:
    """Garante que hot_take tem ai_categoria — classifica on-the-fly se NULL.

    Usado pelo auto_publisher antes de aplicar filtro de blocklist. Permite
    backfill lazy: hot_takes antigos (criados antes desta feature) ganham
    categoria so quando entram no funil de selecao.

    Returns:
        Categoria gravada (existente ou recem-classificada). None se DB read
        falhou ou Claude falhou e a categoria continua NULL.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ai_categoria, news_title, body, linkedin_post FROM hot_takes WHERE id = %s",
                (hot_take_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        if row.get("ai_categoria"):
            return row["ai_categoria"]
        cat = await classify_hot_take_category(
            row.get("news_title") or "",
            row.get("body") or row.get("linkedin_post") or "",
        )
        if cat:
            set_hot_take_categoria(hot_take_id, cat)
        return cat
    except Exception:
        logger.exception(f"ensure_hot_take_categoria falhou pra {hot_take_id}")
        return None


async def fetch_rss_feed(url: str) -> list[dict]:
    """Busca e parseia um feed RSS"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()

        # Parse XML
        root = ET.fromstring(response.text)

        items = []
        for item in root.findall('.//item'):
            title = item.find('title')
            link = item.find('link')
            pub_date = item.find('pubDate')
            description = item.find('description')

            if title is not None:
                items.append({
                    "title": title.text,
                    "link": link.text if link is not None else "",
                    "pub_date": pub_date.text if pub_date is not None else "",
                    "description": description.text if description is not None else ""
                })

        return items[:20]  # Limita a 20 itens por fonte

    except Exception as e:
        logger.error(f"Erro ao buscar RSS {url}: {e}")
        return []


async def fetch_all_news() -> list[dict]:
    """Busca notícias de todas as fontes"""
    all_news = []

    for source_id, source in NEWS_SOURCES.items():
        items = await fetch_rss_feed(source["url"])
        for item in items:
            item["source"] = source["name"]
            item["source_id"] = source_id
            item["category"] = source["category"]
        all_news.extend(items)

    logger.info(f"Buscadas {len(all_news)} notícias de {len(NEWS_SOURCES)} fontes")
    return all_news


def pre_filter_news(news_items: list[dict]) -> list[dict]:
    """Pré-filtro rápido por keywords antes de usar IA"""
    filtered = []

    for item in news_items:
        text = f"{item.get('title', '')} {item.get('description', '')}".lower()

        # Verifica se contém algum tópico de interesse
        for topic in TOPICS_OF_INTEREST:
            if topic.lower() in text:
                item["matched_topic"] = topic
                filtered.append(item)
                break

    logger.info(f"Pré-filtro: {len(filtered)}/{len(news_items)} notícias relevantes")
    return filtered


async def filter_news_with_ai(news_items: list[dict], limit: int = 10) -> list[dict]:
    """Usa IA para filtrar e rankear notícias por relevância para governança"""

    if not news_items:
        return []

    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY não configurada")
        return news_items[:limit]

    # Prepara lista de notícias para análise
    news_list = "\n".join([
        f"{i+1}. [{item.get('category', 'geral')}] {item.get('title', '')}"
        for i, item in enumerate(news_items[:30])  # Analisa até 30
    ])

    prompt = f"""Voce seleciona noticias pra hot takes do Renato Almeida Prado — conselheiro independente + founder do imensIAH (SaaS de AI agentic pra execucao estrategica de PMEs).

POSICIONAMENTO ATUAL (pivot Q2-Q3 2026):
- Audiencia primaria: founder/CEO de PME
- Audiencia secundaria: conselheiros independentes, executivos senior, investidores
- Mensagem central: "AI acelera execucao estrategica"
- Temas-chave da marca: AI aplicada · agentes AI · estrategia + AI · IA pra PMEs · governanca nascente · board governance · conselheiro independente · IA etica

CRITERIOS DE SELECAO (selecione {limit} noticias):
PRIORIZAR fortemente:
- Casos de empresas usando AI pra acelerar decisao/execucao estrategica
- Lancamentos/mudancas em AI agentic, LLMs, ferramentas pra founders
- Founder de PME tomando decisao corajosa (entrar/sair de mercado, M&A, capital)
- Conselhos consultivos/administracao de PMEs em mudanca
- Falhas de governanca nascente em scaleups (lesson learned acionavel)

DESPRIORIZAR (esses geraram dismiss alto historicamente):
- Macro/politica pura (Selic, eleicoes, BC) sem angulo de execucao
- Regulacao setorial densa sem leitura pra founder
- Crises de estatais/grandes corporacoes longe da realidade PME
- "Governanca corporativa" generica sem caso concreto

NOTICIAS:
{news_list}

Responda APENAS com JSON:
{{
  "selected": [1, 5, 8, ...],
  "reasoning": "breve explicacao da selecao (mencione angulo de marca pra cada uma)"
}}"""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01"
                },
                json={
                    "model": llm.BALANCED,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30.0
            )
            response.raise_for_status()

        result = response.json()

        # Validate response structure
        if "content" not in result or not isinstance(result["content"], list) or len(result["content"]) == 0:
            logger.error(f"Invalid filter response: {list(result.keys())}")
            return news_items[:limit]

        content = result["content"][0]["text"]

        # Parse JSON da resposta
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            data = json.loads(json_match.group())
            selected_indices = data.get("selected", [])

            # Retorna notícias selecionadas na ordem
            filtered = []
            for idx in selected_indices:
                if 1 <= idx <= len(news_items):
                    news_items[idx-1]["ai_selected"] = True
                    filtered.append(news_items[idx-1])

            logger.info(f"IA selecionou {len(filtered)} notícias")
            return filtered

    except Exception as e:
        logger.error(f"Erro no filtro IA: {e}")

    return news_items[:limit]


async def generate_hot_take(news_item: dict, articles: list[dict] = None) -> dict:
    """Gera um hot take conectando notícia com artigos de governança"""

    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY não configurada"}

    logger.info(f"API key present: {bool(ANTHROPIC_API_KEY)}, length: {len(ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else 0}")

    # Lista de artigos disponíveis - with error handling
    try:
        articles_text = ""
        if articles:
            safe_articles = []
            for a in articles[:20]:
                try:
                    title = a.get('title', '') if isinstance(a, dict) else str(a)
                    desc = a.get('description', '') if isinstance(a, dict) else ''
                    desc_preview = desc[:100] if desc else ''
                    safe_articles.append(f"- {title}: {desc_preview}...")
                except Exception as ae:
                    logger.warning(f"Error processing article: {ae}")
                    continue
            articles_text = "\n".join(safe_articles)
        else:
            articles_text = "(usar conhecimento geral sobre NeoGovernança e Governança na Complexidade)"
    except Exception as e:
        logger.error(f"Error building articles_text: {e}")
        articles_text = "(erro ao processar artigos)"

    # Build prompt with error handling
    try:
        news_title = news_item.get('title', '') if isinstance(news_item, dict) else str(news_item)
        news_desc = news_item.get('description', '') if isinstance(news_item, dict) else ''
    except Exception as pe:
        logger.error(f"Error accessing news_item: {type(pe).__name__}: {pe}, news_item type: {type(news_item)}")
        return {"error": f"Error accessing news_item: {pe}"}

    prompt = f"""Você é Renato Almeida Prado: conselheiro independente, ex-CEO, founder do imensIAH (SaaS de AI agentic pra execução estratégica de PMEs). Sua audiência principal no LinkedIn é founder/CEO de PME; secundária, conselheiros e executivos sênior.

NOTÍCIA:
Título: {news_title}
Descrição: {news_desc}

ARTIGOS DISPONÍVEIS:
{articles_text}

POSICIONAMENTO (use como ângulo):
- "AI acelera execução estratégica" é a tese central
- Conecte: AI aplicada · agentes AI · decisão de founder · governança nascente · board governance
- Evite: jargão "NeoGovernança", textão institucional, tom de manifesto teórico

TAREFA:
Escreva um hot take pra LinkedIn que:
1. Lê a notícia pelo ângulo de quem decide (founder PME ou conselheiro)
2. Conecta — quando couber — com AI/execução acelerada
3. Entrega 1 insight prático que muda como o leitor age amanhã
4. Termina com pergunta provocativa OU link pra artigo (se relevante)

ESTILO:
- Voz: ombro a ombro, experiente, sem ser professoral
- Provocativo mas concreto (ex: nome empresa, número, decisão observável)
- 150-200 palavras
- Português com acentuação correta (não use "tambem", "decisao" — escreva "também", "decisão")
- Emojis com moderação (0-2, só quando agregam)

Responda em JSON:
{{
  "hook": "frase de abertura impactante (1 linha)",
  "body": "desenvolvimento (2-3 parágrafos curtos)",
  "cta": "call to action final (pergunta ou convite)",
  "article_slug": "slug do artigo relacionado ou null",
  "hashtags": ["hashtag1", "hashtag2", "hashtag3"],
  "linkedin_post": "post completo formatado para LinkedIn"
}}"""

    try:
        logger.info(f"Calling Claude API for hot take...")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01"
                },
                json={
                    "model": llm.BALANCED,
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=60.0
            )
        logger.info(f"Claude API response status: {response.status_code}")

        if response.status_code != 200:
            error_text = response.text[:500]
            logger.error(f"API error {response.status_code}: {error_text}")
            return {"error": f"API error {response.status_code}: {error_text}"}

        result = response.json()

        # Debug: log full response structure
        logger.info(f"Response keys: {list(result.keys())}")

        # Check for API error
        if "error" in result:
            error_info = result.get("error", {})
            error_msg = error_info.get("message", str(error_info)) if isinstance(error_info, dict) else str(error_info)
            logger.error(f"API returned error: {error_msg}")
            return {"error": f"API error: {error_msg}"}

        # Validate response structure
        if "content" not in result:
            logger.error(f"No 'content' in response: {list(result.keys())}")
            return {"error": f"Invalid response structure: {list(result.keys())}"}

        # Check content is a list
        if not isinstance(result["content"], list):
            logger.error(f"content is not a list: {type(result['content'])}")
            return {"error": f"content is not a list: {type(result['content']).__name__}"}

        if len(result["content"]) == 0:
            logger.error("Empty content array in response")
            return {"error": "Empty content array"}

        first_content = result["content"][0]
        if not isinstance(first_content, dict):
            logger.error(f"content[0] is not a dict: {type(first_content)}")
            return {"error": f"content[0] is not a dict: {type(first_content).__name__}"}

        if "text" not in first_content:
            logger.error(f"No 'text' in content[0]: {first_content}")
            return {"error": f"No text in response: {first_content.get('type', 'unknown')}"}

        content = first_content["text"]
        logger.info(f"Claude response length: {len(content)}")
        logger.info(f"Claude response preview: {content[:500]}")

        # Parse JSON
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            try:
                parsed_json = json.loads(json_match.group())
                logger.info(f"Parsed JSON keys: {list(parsed_json.keys())}")

                # Check if Claude returned an error in JSON
                if "error" in parsed_json and not all(k in parsed_json for k in ["hook", "body"]):
                    error_val = parsed_json.get("error")
                    logger.error(f"Claude returned error in JSON: {error_val}")
                    return {"error": f"Claude JSON error: {error_val}"}

                hot_take = parsed_json
                hot_take["news_title"] = news_item.get("title", "")
                hot_take["news_link"] = news_item.get("link", "")
                hot_take["generated_at"] = datetime.now().isoformat()
                return hot_take
            except json.JSONDecodeError as je:
                logger.error(f"JSON parse error: {je}")
                return {"error": f"JSON parse error: {je}"}
        else:
            logger.error(f"No JSON found in response: {content[:200]}")
            return {"error": "No JSON in response"}

    except httpx.TimeoutException:
        logger.error("Timeout calling Claude API")
        return {"error": "Timeout calling Claude API"}
    except KeyError as ke:
        logger.error(f"KeyError in generate_hot_take: {ke} (type: {type(ke.args[0]) if ke.args else 'no args'})")
        return {"error": f"KeyError: {ke}"}
    except Exception as e:
        logger.error(f"Exception in generate_hot_take: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


def save_hot_take(hot_take: dict, status: str = "draft") -> int:
    """Salva hot take no banco de dados.

    Aceita `ai_categoria` opcional no dict — se presente, grava direto;
    senao caller (ou ensure_hot_take_categoria) classifica depois.
    """
    ensure_table_exists()
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO hot_takes (news_title, news_link, hook, body, cta, linkedin_post,
                                   article_slug, hashtags, status, ai_categoria)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            hot_take.get("news_title", ""),
            hot_take.get("news_link", ""),
            hot_take.get("hook", ""),
            hot_take.get("body", ""),
            hot_take.get("cta", ""),
            hot_take.get("linkedin_post", ""),
            hot_take.get("article_slug"),
            json.dumps(hot_take.get("hashtags", [])),
            status,
            hot_take.get("ai_categoria"),
        ))

        hot_take_id = cursor.fetchone()['id']
        conn.commit()

        return hot_take_id


def get_hot_takes(status: str = None, limit: int = 20) -> list[dict]:
    """Lista hot takes salvos"""
    ensure_table_exists()
    with get_db() as conn:
        cursor = conn.cursor()

        # OTIMIZAÇÃO: Selecionar apenas colunas necessárias
        columns = """id, news_title, news_link, hook, body, cta,
                     linkedin_post, article_slug, hashtags, status,
                     created_at, scheduled_for, published_at,
                     editorial_post_id, linkedin_url"""

        if status:
            cursor.execute(f'''
                SELECT {columns} FROM hot_takes
                WHERE status = %s
                ORDER BY created_at DESC
                LIMIT %s
            ''', (status, limit))
        else:
            cursor.execute(f'''
                SELECT {columns} FROM hot_takes
                ORDER BY created_at DESC
                LIMIT %s
            ''', (limit,))

        results = []
        for row in cursor.fetchall():
            item = dict(row)
            # Convert datetime to string for JSON serialization
            for key in ['created_at', 'scheduled_for', 'published_at']:
                if item.get(key) and hasattr(item[key], 'isoformat'):
                    item[key] = item[key].isoformat()
            results.append(item)
        return results


def get_weekly_digest_stats() -> dict:
    """Estatísticas do digest semanal"""
    ensure_table_exists()
    with get_db() as conn:
        cursor = conn.cursor()

        # Hot takes desta semana
        cursor.execute('''
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'draft') as drafts,
                COUNT(*) FILTER (WHERE status = 'scheduled') as scheduled,
                COUNT(*) FILTER (WHERE status = 'published') as published
            FROM hot_takes
            WHERE created_at >= NOW() - INTERVAL '7 days'
        ''')

        row = cursor.fetchone()
        return dict(row) if row else {"total": 0, "drafts": 0, "scheduled": 0, "published": 0}


async def generate_weekly_digest(limit: int = 5) -> dict:
    """Gera digest semanal de hot takes"""

    try:
        # 1. Busca notícias
        all_news = await fetch_all_news()
        logger.info(f"Buscadas {len(all_news)} notícias")

        # 2. Pré-filtro por keywords
        filtered_news = pre_filter_news(all_news)
        logger.info(f"Filtradas {len(filtered_news)} notícias por keywords")

        if not filtered_news:
            # Se não há notícias filtradas, usa as primeiras 10
            filtered_news = all_news[:10]

        # 3. Filtro IA para selecionar as melhores
        selected_news = await filter_news_with_ai(filtered_news, limit=limit)
        logger.info(f"IA selecionou {len(selected_news)} notícias")

        if not selected_news:
            selected_news = filtered_news[:limit]

        # 4. Busca artigos disponíveis (opcional - não falha se não existir)
        articles = []
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT article_title as title, article_description as description, article_slug as slug
                    FROM editorial_posts
                    WHERE status != 'archived'
                    ORDER BY created_at DESC
                    LIMIT 50
                ''')
                articles = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"Não foi possível buscar artigos: {e}")
            articles = []

        # 5. Gera hot takes para cada notícia selecionada
        hot_takes = []
        errors = []
        for i, news in enumerate(selected_news):
            try:
                logger.info(f"Processing news {i}: type={type(news).__name__}, keys={list(news.keys()) if isinstance(news, dict) else 'N/A'}")
                news_title = news.get('title', '') if isinstance(news, dict) else str(news)
                logger.info(f"Gerando hot take para: {news_title[:50]}")
                hot_take = await generate_hot_take(news, articles)
                if "error" not in hot_take:
                    # Classifica antes de salvar pra ja gravar ai_categoria.
                    # Se falhar, save_hot_take grava NULL e o auto_publisher
                    # vai re-tentar via ensure_hot_take_categoria. Best-effort.
                    try:
                        cat = await classify_hot_take_category(
                            hot_take.get("news_title", ""),
                            hot_take.get("linkedin_post") or hot_take.get("body", ""),
                        )
                        if cat:
                            hot_take["ai_categoria"] = cat
                    except Exception:
                        logger.exception("classify_hot_take_category falhou durante digest")
                    hot_take_id = save_hot_take(hot_take)
                    hot_take["id"] = hot_take_id
                    hot_takes.append(hot_take)
                    logger.info(f"Hot take gerado com sucesso: {hot_take_id} (cat={hot_take.get('ai_categoria')})")
                else:
                    error_msg = hot_take.get('error', 'Unknown error')
                    logger.warning(f"Erro ao gerar hot take: {error_msg} (type: {type(error_msg).__name__})")
                    # Include full hot_take keys for debugging
                    error_detail = f"{str(error_msg)} [keys: {list(hot_take.keys())}]"
                    errors.append(error_detail)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error(f"Erro ao processar notícia: {type(e).__name__}: {e}\n{tb}")
                errors.append(f"{type(e).__name__}: {e} | Traceback: {tb[-200:]}")
                continue

        return {
            "news_fetched": len(all_news),
            "news_filtered": len(filtered_news),
            "news_selected": len(selected_news),
            "hot_takes_generated": len(hot_takes),
            "hot_takes": hot_takes,
            "errors": errors if errors else None,
            "debug_news": [{"title": n.get("title", "")[:50]} for n in selected_news[:3]] if selected_news else None
        }

    except Exception as e:
        logger.error(f"Erro no generate_weekly_digest: {e}")
        return {
            "error": str(e),
            "news_fetched": 0,
            "news_filtered": 0,
            "news_selected": 0,
            "hot_takes_generated": 0,
            "hot_takes": []
        }


async def generate_hot_take_from_url(url: str) -> dict:
    """Gera hot take a partir de URL fornecida manualmente"""

    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY não configurada"}

    # Busca conteúdo da URL
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0, follow_redirects=True)
            html = response.text

        # Extrai título da página
        title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        title = title_match.group(1) if title_match else url

        # Extrai meta description
        desc_match = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE)
        description = desc_match.group(1) if desc_match else ""

    except Exception as e:
        logger.error(f"Erro ao buscar URL {url}: {e}")
        title = url
        description = ""

    news_item = {
        "title": title,
        "link": url,
        "description": description
    }

    # Busca artigos
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT article_title as title, article_description as description, article_slug as slug
            FROM editorial_posts
            WHERE status != 'archived'
            LIMIT 50
        ''')
        articles = [dict(row) for row in cursor.fetchall()]

    hot_take = await generate_hot_take(news_item, articles)

    if "error" not in hot_take:
        try:
            cat = await classify_hot_take_category(
                hot_take.get("news_title", ""),
                hot_take.get("linkedin_post") or hot_take.get("body", ""),
            )
            if cat:
                hot_take["ai_categoria"] = cat
        except Exception:
            logger.exception("classify_hot_take_category falhou em generate_hot_take_from_url")
        hot_take_id = save_hot_take(hot_take)
        hot_take["id"] = hot_take_id

    return hot_take


def schedule_hot_take(hot_take_id: int, scheduled_for: str, create_editorial: bool = True) -> dict:
    """
    Agenda um hot take e opcionalmente cria entrada no calendário editorial.

    Args:
        hot_take_id: ID do hot take
        scheduled_for: Data/hora no formato ISO (YYYY-MM-DD HH:MM)
        create_editorial: Se True, cria entrada no editorial_posts

    Returns:
        dict com status e editorial_post_id se criado
    """
    from datetime import datetime
    from services.editorial_calendar import create_editorial_post

    ensure_table_exists()

    with get_db() as conn:
        cursor = conn.cursor()

        # Busca o hot take
        cursor.execute('SELECT * FROM hot_takes WHERE id = %s', (hot_take_id,))
        hot_take = cursor.fetchone()

        if not hot_take:
            return {"error": "Hot take não encontrado"}

        hot_take = dict(hot_take)

        # Parse da data
        try:
            if 'T' in scheduled_for:
                scheduled_dt = datetime.fromisoformat(scheduled_for.replace('Z', ''))
            else:
                scheduled_dt = datetime.strptime(scheduled_for, '%Y-%m-%d %H:%M')
        except ValueError:
            try:
                scheduled_dt = datetime.strptime(scheduled_for, '%Y-%m-%d')
                scheduled_dt = scheduled_dt.replace(hour=9, minute=0)  # Default 9h
            except ValueError:
                return {"error": "Formato de data inválido. Use YYYY-MM-DD HH:MM"}

        editorial_post_id = None

        # Cria entrada no calendário editorial se solicitado
        if create_editorial:
            editorial_data = {
                'article_title': hot_take.get('hook', 'Hot Take')[:100],
                'article_description': hot_take.get('body', '')[:500],
                'article_url': hot_take.get('news_link', ''),
                'canal': 'linkedin',
                'tipo': 'hot_take',
                'titulo_adaptado': hot_take.get('hook', ''),
                'conteudo_adaptado': hot_take.get('linkedin_post', ''),
                'hashtags': hot_take.get('hashtags', []),
                'status': 'scheduled',
                'data_publicacao': scheduled_dt,
                'notas': f"Hot Take #{hot_take_id} - {hot_take.get('news_title', '')[:100]}",
                'tags': ['hot-take', 'neogovernanca']
            }

            try:
                editorial_post = create_editorial_post(editorial_data)
                editorial_post_id = editorial_post.get('id')
            except Exception as e:
                logger.error(f"Erro ao criar editorial post: {e}")

        # Atualiza o hot take
        cursor.execute('''
            UPDATE hot_takes
            SET status = 'scheduled',
                scheduled_for = %s,
                editorial_post_id = %s
            WHERE id = %s
        ''', (scheduled_dt, editorial_post_id, hot_take_id))
        conn.commit()

        return {
            "status": "scheduled",
            "hot_take_id": hot_take_id,
            "editorial_post_id": editorial_post_id,
            "scheduled_for": scheduled_dt.isoformat()
        }


def mark_hot_take_published(hot_take_id: int, linkedin_url: str = None) -> dict:
    """
    Marca hot take como publicado e registra URL do LinkedIn.
    """
    from datetime import datetime

    ensure_table_exists()

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE hot_takes
            SET status = 'published',
                published_at = %s,
                linkedin_url = %s
            WHERE id = %s
            RETURNING editorial_post_id
        ''', (datetime.now(), linkedin_url, hot_take_id))

        result = cursor.fetchone()

        # Atualiza também o editorial_post se existir
        if result and result.get('editorial_post_id'):
            editorial_post_id = result['editorial_post_id']
            cursor.execute('''
                UPDATE editorial_posts
                SET status = 'published',
                    url_publicado = COALESCE(%s, url_publicado),
                    data_publicado = COALESCE(data_publicado, %s),
                    linkedin_post_url = COALESCE(%s, linkedin_post_url)
                WHERE id = %s
                RETURNING article_title, data_publicado
            ''', (linkedin_url, datetime.now(), linkedin_url, editorial_post_id))
            ep = cursor.fetchone()

            # Auto-create metrics collection task (48h after publish)
            if ep:
                from services.editorial_calendar import _create_metrics_collection_task
                _create_metrics_collection_task(
                    cursor, editorial_post_id,
                    ep.get('article_title', ''),
                    ep.get('data_publicado')
                )

        conn.commit()

        return {"status": "published", "hot_take_id": hot_take_id}


def update_hot_take_metrics(hot_take_id: int, metrics: dict) -> dict:
    """
    Atualiza métricas de engajamento do hot take.

    Args:
        hot_take_id: ID do hot take
        metrics: dict com {likes, comments, shares, impressions}
    """
    ensure_table_exists()

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE hot_takes
            SET metrics = %s
            WHERE id = %s
        ''', (json.dumps(metrics), hot_take_id))
        conn.commit()

        return {"status": "updated", "metrics": metrics}


def get_hot_take_by_id(hot_take_id: int) -> dict:
    """Busca hot take por ID"""
    ensure_table_exists()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM hot_takes WHERE id = %s', (hot_take_id,))
        row = cursor.fetchone()

        if not row:
            return None

        item = dict(row)
        for key in ['created_at', 'scheduled_for', 'published_at']:
            if item.get(key) and hasattr(item[key], 'isoformat'):
                item[key] = item[key].isoformat()
        return item
