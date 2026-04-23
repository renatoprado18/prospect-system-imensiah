"""
News Hub Service - Central de Notícias Inteligente

Sistema de curadoria de notícias com aprendizado de preferências.
Combina:
- Notícias personalizadas (baseado em interações)
- Trending (assuntos quentes independente do gosto)
- Descoberta (fora da bolha)
"""

import os
import httpx
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from collections import defaultdict

from database import get_db

logger = logging.getLogger(__name__)

# Fontes RSS - Perfil amplo do Renato: governança, IA, empreendedorismo, esporte, sustentabilidade
NEWS_SOURCES = {
    # Negócios & Economia (Brasil)
    "valor_economia": {
        "name": "Valor Econômico",
        "url": "https://pox.globo.com/rss/valor/",
        "category": "economia"
    },
    "exame": {
        "name": "Exame",
        "url": "https://exame.com/feed/",
        "category": "negocios"
    },
    # Governança & Conselhos
    "google_governanca": {
        "name": "Google News - Governança Corporativa",
        "url": "https://news.google.com/rss/search?q=governan%C3%A7a+corporativa+conselho+administra%C3%A7%C3%A3o&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "governanca"
    },
    # IA & Tecnologia
    "google_tech_br": {
        "name": "Google News - Tecnologia",
        "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FuQjBHZ0pDVWlnQVAB?hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "tecnologia"
    },
    "google_ia_br": {
        "name": "Google News - Inteligência Artificial",
        "url": "https://news.google.com/rss/search?q=intelig%C3%AAncia+artificial+empresas&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "ia"
    },
    # Empreendedorismo & Startups
    "google_startups_br": {
        "name": "Google News - Startups Brasil",
        "url": "https://news.google.com/rss/search?q=startups+empreendedorismo+brasil&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "empreendedorismo"
    },
    # Recuperação Judicial / Investimentos (case Fictor)
    "google_rj_br": {
        "name": "Google News - Recuperação Judicial",
        "url": "https://news.google.com/rss/search?q=recupera%C3%A7%C3%A3o+judicial+credores&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "juridico"
    },
    # ESG & Sustentabilidade
    "google_esg_br": {
        "name": "Google News - ESG Brasil",
        "url": "https://news.google.com/rss/search?q=ESG+sustentabilidade+empresas+brasil&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "esg"
    },
    # Esporte & Gestão Esportiva (judô, CAP)
    "google_gestao_esportiva": {
        "name": "Google News - Gestão Esportiva",
        "url": "https://news.google.com/rss/search?q=gest%C3%A3o+esportiva+clube+atletico&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "esporte"
    },
    # Agronegócio (fazendas)
    "google_agro": {
        "name": "Google News - Agronegócio",
        "url": "https://news.google.com/rss/search?q=agroneg%C3%B3cio+sustent%C3%A1vel+fazenda&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "agro"
    },
    # Internacional - IA (inglês)
    "mit_tech_review": {
        "name": "MIT Technology Review",
        "url": "https://www.technologyreview.com/feed/",
        "category": "ia"
    },
    "google_estrategia": {
        "name": "Google News - Estratégia Empresarial",
        "url": "https://news.google.com/rss/search?q=estrat%C3%A9gia+empresarial+lideran%C3%A7a&hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "estrategia"
    },
}

# Pesos para cálculo de interesse
INTERACTION_WEIGHTS = {
    "read": 1,
    "read_long": 3,      # > 30 segundos
    "shared": 5,
    "hot_take": 5,
    "article": 5,
    "contact": 4,
    "liked": 3,
    "disliked": -3,
    "ignored": -1,
    "ignored_fast": -2   # < 2 segundos
}

# Tópicos para classificação
TOPIC_KEYWORDS = {
    "governanca": ["governança", "conselho", "board", "compliance", "auditoria", "conselheiro",
                   "acionista", "investidor", "stakeholder", "assembleia", "diretoria", "administração"],
    "esg": ["esg", "sustentabilidade", "clima", "ambiental", "social", "carbono", "net zero",
            "água", "energia", "recursos naturais", "emissões", "impacto ambiental", "verde",
            "renovável", "reciclagem", "diversidade", "inclusão", "transparência"],
    "ma": ["fusão", "aquisição", "m&a", "merger", "takeover", "ipo", "oferta"],
    "tecnologia": ["ia", "inteligência artificial", "ai", "tech", "digital", "software", "startup"],
    "economia": ["economia", "pib", "inflação", "juros", "selic", "dólar", "mercado"],
    "lideranca": ["ceo", "liderança", "gestão", "executivo", "c-level", "diretoria"],
    "carreira": ["carreira", "emprego", "trabalho", "profissional", "salário"],
    "estrategia": ["estratégia", "planejamento", "transformação", "inovação"]
}


def strip_html(text: str) -> str:
    """Remove HTML tags e extrai apenas texto"""
    if not text:
        return ""
    import re
    # Remove tags HTML
    clean = re.sub(r'<[^>]+>', ' ', text)
    # Remove múltiplos espaços
    clean = re.sub(r'\s+', ' ', clean)
    # Remove entidades HTML comuns
    clean = clean.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    return clean.strip()


async def fetch_rss_feed(url: str, source_name: str) -> List[Dict]:
    """Busca e parseia um feed RSS"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=15.0, follow_redirects=True)
            response.raise_for_status()

        root = ET.fromstring(response.text)
        items = []

        # Namespaces comuns em RSS
        namespaces = {
            'content': 'http://purl.org/rss/1.0/modules/content/',
            'dc': 'http://purl.org/dc/elements/1.1/'
        }

        for item in root.findall('.//item'):
            title = item.find('title')
            link = item.find('link')
            pub_date = item.find('pubDate')
            description = item.find('description')

            # Tentar pegar conteúdo completo de content:encoded
            content_encoded = item.find('content:encoded', namespaces)
            if content_encoded is None:
                content_encoded = item.find('{http://purl.org/rss/1.0/modules/content/}encoded')

            if title is not None and title.text:
                # Priorizar content:encoded para descrição mais completa
                if content_encoded is not None and content_encoded.text:
                    full_text = strip_html(content_encoded.text)
                elif description is not None and description.text:
                    full_text = strip_html(description.text)
                else:
                    full_text = ""

                items.append({
                    "source": source_name,
                    "title": title.text.strip(),
                    "link": link.text.strip() if link is not None and link.text else "",
                    "published_at": pub_date.text if pub_date is not None else None,
                    "description": full_text[:1000]  # Aumentado para 1000 chars
                })

        return items[:30]  # Limite por fonte

    except Exception as e:
        logger.error(f"Erro ao buscar RSS {source_name}: {e}")
        return []


def extract_topics(text: str) -> List[str]:
    """Extrai tópicos do texto baseado em keywords"""
    if not text:
        return []

    text_lower = text.lower()
    found_topics = []

    for topic, keywords in TOPIC_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text_lower:
                found_topics.append(topic)
                break

    return found_topics


def calculate_relevance_score(news_item: Dict, user_interests: Dict) -> float:
    """Calcula score de relevância baseado nos interesses do usuário"""
    if not user_interests or not user_interests.get("topics"):
        return 0.5  # Score neutro se não há histórico

    score = 0.0
    topics = news_item.get("topics", [])
    user_topics = user_interests.get("topics", {})

    for topic in topics:
        if topic in user_topics:
            score += user_topics[topic]

    # Normalizar para 0-1
    if topics:
        score = min(score / len(topics), 1.0)

    # Boost para fontes preferidas
    source = news_item.get("source", "")
    user_sources = user_interests.get("sources", {})
    if source in user_sources:
        score = score * 0.7 + user_sources[source] * 0.3

    return round(score, 3)


async def collect_news() -> Dict[str, Any]:
    """Coleta notícias de todas as fontes e salva no banco"""
    all_news = []

    for source_id, source_config in NEWS_SOURCES.items():
        items = await fetch_rss_feed(source_config["url"], source_config["name"])
        for item in items:
            item["source_id"] = source_id
            item["category"] = source_config["category"]
            item["topics"] = extract_topics(f"{item['title']} {item['description']}")
        all_news.extend(items)

    # Salvar no banco
    saved = 0
    with get_db() as conn:
        cursor = conn.cursor()

        for item in all_news:
            try:
                cursor.execute('''
                    INSERT INTO news_items (source, source_url, title, description, link,
                                           category, topics, published_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (link) DO NOTHING
                ''', (
                    item["source"],
                    item.get("source_id", ""),
                    item["title"],
                    item["description"],
                    item["link"],
                    item["category"],
                    json.dumps(item["topics"]),
                    item.get("published_at")
                ))
                if cursor.rowcount > 0:
                    saved += 1
            except Exception as e:
                logger.error(f"Erro salvando notícia: {e}")

        conn.commit()

    return {"collected": len(all_news), "saved": saved}


def get_user_interests(user_id: int) -> Dict:
    """Retorna perfil de interesses do usuário"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT topics, sources, keywords_positive, keywords_negative
            FROM user_interests
            WHERE user_id = %s
        ''', (user_id,))

        row = cursor.fetchone()
        if row:
            return {
                "topics": row["topics"] or {},
                "sources": row["sources"] or {},
                "keywords_positive": row["keywords_positive"] or [],
                "keywords_negative": row["keywords_negative"] or []
            }

        return {"topics": {}, "sources": {}, "keywords_positive": [], "keywords_negative": []}


def update_user_interests(user_id: int, news_item: Dict, action: str, time_spent: int = 0):
    """Atualiza interesses do usuário baseado na interação"""
    weight = INTERACTION_WEIGHTS.get(action, 0)

    # Ajuste por tempo de leitura
    if action == "read" and time_spent > 30:
        weight = INTERACTION_WEIGHTS["read_long"]
    elif action == "ignored" and time_spent < 2:
        weight = INTERACTION_WEIGHTS["ignored_fast"]

    if weight == 0:
        return

    topics = news_item.get("topics", [])
    source = news_item.get("source", "")

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar interesses atuais
        cursor.execute('SELECT topics, sources FROM user_interests WHERE user_id = %s', (user_id,))
        row = cursor.fetchone()

        if row:
            current_topics = row["topics"] or {}
            current_sources = row["sources"] or {}
        else:
            current_topics = {}
            current_sources = {}

        # Atualizar tópicos (decay + novo peso)
        for topic in topics:
            current = current_topics.get(topic, 0.5)
            # Fórmula: média ponderada com decay
            new_value = current * 0.9 + (0.1 if weight > 0 else -0.1) * abs(weight) / 5
            current_topics[topic] = max(0, min(1, new_value))  # Clamp 0-1

        # Atualizar fonte
        if source:
            current = current_sources.get(source, 0.5)
            new_value = current * 0.9 + (0.1 if weight > 0 else -0.1) * abs(weight) / 5
            current_sources[source] = max(0, min(1, new_value))

        # Salvar
        cursor.execute('''
            INSERT INTO user_interests (user_id, topics, sources, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                topics = EXCLUDED.topics,
                sources = EXCLUDED.sources,
                updated_at = NOW()
        ''', (user_id, json.dumps(current_topics), json.dumps(current_sources)))

        conn.commit()


def record_interaction(user_id: int, news_id: int, action: str, time_spent: int = 0, metadata: Dict = None):
    """Registra interação do usuário com uma notícia"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO news_interactions (news_id, user_id, action, time_spent, metadata)
            VALUES (%s, %s, %s, %s, %s)
        ''', (news_id, user_id, action, time_spent, json.dumps(metadata or {})))

        # Buscar notícia para atualizar interesses
        cursor.execute('SELECT * FROM news_items WHERE id = %s', (news_id,))
        news_item = cursor.fetchone()

        conn.commit()

    # Atualizar interesses do usuário
    if news_item:
        update_user_interests(user_id, dict(news_item), action, time_spent)


def detect_trending(hours: int = 24) -> None:
    """Detecta notícias trending - usa as mais recentes se não há duplicatas"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Resetar trending anterior
        cursor.execute('UPDATE news_items SET is_trending = FALSE, trending_score = 0')

        # Primeiro: tentar encontrar notícias cobertas por múltiplas fontes
        cursor.execute('''
            WITH recent_news AS (
                SELECT id, title, source, collected_at,
                       LOWER(REGEXP_REPLACE(title, '[^a-záàâãéèêíìîóòôõúùûç\\s]', '', 'gi')) as clean_title
                FROM news_items
                WHERE collected_at > NOW() - INTERVAL '%s hours'
            ),
            title_counts AS (
                SELECT clean_title, COUNT(DISTINCT source) as source_count,
                       COUNT(*) as total_count,
                       MAX(collected_at) as latest
                FROM recent_news
                WHERE LENGTH(clean_title) > 20
                GROUP BY clean_title
                HAVING COUNT(DISTINCT source) >= 2
            )
            UPDATE news_items n
            SET is_trending = TRUE,
                trending_score = tc.source_count * 10 + tc.total_count
            FROM title_counts tc, recent_news rn
            WHERE rn.clean_title = tc.clean_title
              AND n.id = rn.id
        ''', (hours,))

        # Se não encontrou nenhuma, pegar as 4 mais recentes como trending
        cursor.execute('SELECT COUNT(*) as cnt FROM news_items WHERE is_trending = TRUE')
        row = cursor.fetchone()
        trending_count = row['cnt'] if row else 0

        if trending_count == 0:
            cursor.execute('''
                UPDATE news_items
                SET is_trending = TRUE, trending_score = 10
                WHERE id IN (
                    SELECT id FROM news_items
                    WHERE collected_at > NOW() - INTERVAL '%s hours'
                    ORDER BY collected_at DESC
                    LIMIT 4
                )
            ''', (hours,))

        conn.commit()


def get_news_feed(user_id: int, limit: int = 15) -> Dict[str, List[Dict]]:
    """Retorna feed de notícias organizado por seções"""
    import time
    t0 = time.time()

    user_interests = get_user_interests(user_id)
    logger.info(f"[PERF] get_user_interests: {time.time()-t0:.2f}s")

    t1 = time.time()
    with get_db() as conn:
        cursor = conn.cursor()

        # Trending (3-4 itens) - query simplificada
        cursor.execute('''
            SELECT *
            FROM news_items
            WHERE is_trending = TRUE
              AND collected_at > NOW() - INTERVAL '48 hours'
            ORDER BY trending_score DESC, collected_at DESC
            LIMIT 4
        ''')
        trending = [dict(row) for row in cursor.fetchall()]
        logger.info(f"[PERF] trending query: {time.time()-t1:.2f}s")
        t2 = time.time()

        # Para você - query simplificada sem subquery
        cursor.execute('''
            SELECT *
            FROM news_items
            WHERE collected_at > NOW() - INTERVAL '72 hours'
              AND is_trending = FALSE
            ORDER BY collected_at DESC
            LIMIT 30
        ''')

        all_news = [dict(row) for row in cursor.fetchall()]
        logger.info(f"[PERF] for_you query: {time.time()-t2:.2f}s")
        t3 = time.time()

        # Calcular relevância e ordenar
        for item in all_news:
            item["topics"] = item.get("topics") or []
            if isinstance(item["topics"], str):
                item["topics"] = json.loads(item["topics"])
            item["relevance_score"] = calculate_relevance_score(item, user_interests)

        all_news.sort(key=lambda x: x["relevance_score"], reverse=True)
        for_you = all_news[:6]

        # Descoberta (fora da bolha - tópicos com baixo score)
        discovery = [n for n in all_news[6:] if n["relevance_score"] < 0.4][:2]

        # Estatísticas de interesse
        interest_stats = []
        for topic, score in sorted(user_interests.get("topics", {}).items(), key=lambda x: -x[1])[:5]:
            interest_stats.append({"topic": topic, "score": round(score * 100)})

        # Digest rápido (sem chamar função pesada)
        topic_counts = defaultdict(int)
        for item in trending + for_you:
            topics = item.get('topics') or []
            if isinstance(topics, str):
                try:
                    topics = json.loads(topics)
                except:
                    topics = []
            for t in topics:
                topic_counts[t] += 1

        top_topics = ', '.join([t for t, c in sorted(topic_counts.items(), key=lambda x: -x[1])[:3]])
        digest = f"Hoje: {len(trending + for_you)} notícias" + (f" sobre {top_topics}" if top_topics else "")

        return {
            "trending": trending,
            "for_you": for_you,
            "discovery": discovery,
            "interests": interest_stats,
            "related_articles": [],  # Carregado sob demanda no modal
            "digest": digest,
            "total": len(trending) + len(for_you) + len(discovery)
        }


def get_news_item(news_id: int) -> Optional[Dict]:
    """Retorna uma notícia específica"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM news_items WHERE id = %s', (news_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_related_articles(topics: List[str], limit: int = 6) -> List[Dict]:
    """Busca artigos candidatos diversos para avaliação pela IA.

    Retorna artigos de diferentes categorias para a IA decidir relevância.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar os melhores artigos de cada categoria para ter diversidade
        cursor.execute('''
            WITH ranked AS (
                SELECT id, article_title, article_url, ai_categoria, ai_score_relevancia,
                       ai_gancho_linkedin, ai_keywords,
                       ROW_NUMBER() OVER (PARTITION BY ai_categoria ORDER BY ai_score_relevancia DESC) as rn
                FROM editorial_posts
                WHERE article_url IS NOT NULL
                  AND ai_score_relevancia IS NOT NULL
                  AND ai_categoria IS NOT NULL
            )
            SELECT id, article_title, article_url, ai_categoria, ai_score_relevancia,
                   ai_gancho_linkedin, ai_keywords
            FROM ranked
            WHERE rn = 1
            ORDER BY ai_score_relevancia DESC
            LIMIT %s
        ''', (limit,))

        articles = [dict(row) for row in cursor.fetchall()]
        return articles


def generate_daily_digest(news_items: List[Dict]) -> str:
    """Gera um resumo rápido das notícias do dia"""
    if not news_items:
        return "Sem notícias relevantes hoje."

    # Agrupar por tópico
    topic_counts = defaultdict(int)
    titles = []

    for item in news_items[:10]:
        topics = item.get('topics') or []
        if isinstance(topics, str):
            try:
                topics = json.loads(topics)
            except:
                topics = []
        for t in topics:
            topic_counts[t] += 1
        titles.append(item.get('title', '')[:50])

    # Top tópicos
    top_topics = sorted(topic_counts.items(), key=lambda x: -x[1])[:3]
    topic_str = ', '.join([t[0] for t in top_topics]) if top_topics else 'diversos assuntos'

    # Resumo
    digest = f"Hoje: {len(news_items)} notícias sobre {topic_str}. "

    if titles:
        digest += f"Destaque: {titles[0]}..."

    return digest


def match_contacts_for_news(news_id: int) -> List[Dict]:
    """Encontra contatos que podem se interessar pela notícia"""
    news = get_news_item(news_id)
    if not news:
        return []

    topics = news.get("topics", [])
    if isinstance(topics, str):
        topics = json.loads(topics)

    if not topics:
        return []

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contatos com tags ou setores relacionados
        # Simplificado - pode ser melhorado com matching mais sofisticado
        cursor.execute('''
            SELECT id, nome, empresa, cargo, tags, circulo
            FROM contacts
            WHERE circulo IS NOT NULL AND circulo <= 3
            ORDER BY circulo, nome
            LIMIT 20
        ''')

        contacts = []
        for row in cursor.fetchall():
            contact = dict(row)
            contact_tags = contact.get("tags") or []
            if isinstance(contact_tags, str):
                contact_tags = json.loads(contact_tags)

            # Score simples de match
            match_score = 0
            for topic in topics:
                for tag in contact_tags:
                    if topic.lower() in tag.lower() or tag.lower() in topic.lower():
                        match_score += 1

            if match_score > 0 or contact.get("circulo", 5) <= 2:
                contact["match_score"] = match_score
                contacts.append(contact)

        contacts.sort(key=lambda x: (-x.get("match_score", 0), x.get("circulo", 5)))
        return contacts[:5]


async def evaluate_article_relevance(news_title: str, news_desc: str, article_title: str, article_hook: str) -> Optional[str]:
    """Avalia se há conexão relevante entre notícia e artigo. Retorna conexão ou None."""
    import os
    import anthropic

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        prompt = f"""Avalie se existe uma conexão INTERESSANTE e RELEVANTE entre esta notícia e este artigo de blog.

NOTÍCIA:
{news_title}
{news_desc[:400]}

ARTIGO DO BLOG:
{article_title}
{article_hook[:200] if article_hook else ''}

Conexões interessantes podem ser: temas complementares, perspectivas diferentes sobre um fenômeno, lições de um setor aplicáveis a outro, tendências que se cruzam.

REGRAS DE RESPOSTA:
- Se NÃO houver conexão relevante: responda apenas "NAO"
- Se HOUVER conexão: escreva DIRETAMENTE 2-3 frases explicando a conexão (sem prefixos como "SIM" ou "Conexão:")

Resposta:"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )

        result = response.content[0].text.strip()

        # Verificar se não há conexão
        if result.upper().startswith("NAO") or result.upper().startswith("NÃO") or result.upper() == "NAO" or result.upper() == "NÃO":
            return None

        # Limpar prefixos comuns que a IA pode adicionar
        for prefix in ["SIM", "SIM.", "SIM:", "SIM\n", "CONEXÃO:", "Conexão:"]:
            if result.upper().startswith(prefix.upper()):
                result = result[len(prefix):].strip()

        return result

    except Exception as e:
        logger.error(f"Erro ao avaliar relevância: {e}")
        return None


async def get_news_with_insights(news_id: int) -> Optional[Dict]:
    """Retorna notícia com insights conectando aos artigos do usuário (usa IA para validar)"""
    import asyncio

    news = get_news_item(news_id)
    if not news:
        return None

    topics = news.get("topics", [])
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except:
            topics = []

    # Buscar artigos candidatos
    related = get_related_articles(topics, limit=5)

    if not related:
        news["insights"] = []
        news["related_articles"] = []
        return news

    news_title = news.get('title', '')
    news_desc = news.get('description', '')

    # Avaliar cada artigo em paralelo
    async def evaluate_article(article):
        connection = await evaluate_article_relevance(
            news_title,
            news_desc,
            article.get('article_title', ''),
            article.get('ai_gancho_linkedin', '')
        )
        if connection:
            return {
                "article_id": article.get("id"),
                "article_title": article.get("article_title", ""),
                "article_url": article.get("article_url", ""),
                "connection": connection,
                "hook": article.get("ai_gancho_linkedin", "")
            }
        return None

    # Executar avaliações em paralelo
    tasks = [evaluate_article(article) for article in related[:4]]
    results = await asyncio.gather(*tasks)

    # Filtrar resultados válidos
    insights = [r for r in results if r is not None]

    news["insights"] = insights[:3]  # Máximo 3 conexões
    news["related_articles"] = related

    return news


async def generate_smart_summary(news_id: int) -> Optional[str]:
    """Gera resumo inteligente usando Claude Sonnet"""
    import os
    import anthropic

    news = get_news_item(news_id)
    if not news:
        return None

    # Verificar se já tem resumo salvo
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ai_summary FROM news_items WHERE id = %s",
            (news_id,)
        )
        row = cursor.fetchone()
        if row and row.get('ai_summary'):
            return row['ai_summary']

    title = news.get('title', '')
    description = news.get('description', '')

    if not title:
        return None

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        prompt = f"""Analise esta notícia e forneça:
1. RESUMO (2-3 frases explicando o essencial da notícia)
2. IMPACTO (1 frase sobre o impacto para executivos/empresários)

Notícia:
Título: {title}
Descrição: {description}

Responda de forma direta, sem rótulos. Formato:
[Resumo em 2-3 frases]

💡 [Impacto em 1 frase]"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )

        summary = response.content[0].text.strip()

        # Salvar no banco
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE news_items SET ai_summary = %s WHERE id = %s",
                (summary, news_id)
            )
            conn.commit()

        return summary

    except Exception as e:
        logger.error(f"Erro ao gerar resumo: {e}")
        return None


async def generate_article_connection(news_id: int, article_id: int) -> Optional[Dict]:
    """Gera conexão inteligente entre notícia e artigo do blog"""
    import os
    import anthropic

    news = get_news_item(news_id)
    if not news:
        return None

    # Buscar artigo
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, article_title, article_url, ai_categoria, ai_gancho_linkedin, ai_keywords
            FROM editorial_posts
            WHERE id = %s
        ''', (article_id,))
        row = cursor.fetchone()
        if not row:
            return None
        article = dict(row)

    news_title = news.get('title', '')
    news_desc = news.get('description', '')[:500]
    article_title = article.get('article_title', '')
    article_hook = article.get('ai_gancho_linkedin', '')

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        prompt = f"""Você é um consultor de conteúdo. Analise a notícia e o artigo do blog abaixo e crie uma conexão estratégica.

NOTÍCIA:
Título: {news_title}
Resumo: {news_desc}

ARTIGO DO MEU BLOG:
Título: {article_title}
Gancho: {article_hook}

Gere:
1. CONEXÃO (2-3 frases explicando como a notícia se conecta com o artigo - seja específico)
2. AÇÃO (1 frase sugerindo como usar essa conexão para engajamento - ex: compartilhar no LinkedIn, enviar para contato, criar post)

Responda em português, de forma direta e prática. Formato:
[Conexão]

💡 Ação: [sugestão]"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )

        connection_text = response.content[0].text.strip()

        return {
            "news_id": news_id,
            "article_id": article_id,
            "article_title": article_title,
            "article_url": article.get('article_url', ''),
            "connection": connection_text
        }

    except Exception as e:
        logger.error(f"Erro ao gerar conexão: {e}")
        return None


# ==================== CLIPPING DIARIO ====================

async def generate_daily_clipping(limit: int = 10) -> Dict:
    """
    Gera clipping diário: coleta noticias, filtra com IA, gera resumo.

    Fluxo:
    1. Coleta noticias de todas as fontes RSS
    2. IA (Haiku) ranqueia por relevancia ao perfil do Renato
    3. Retorna top N com resumo de 1 linha cada
    """
    import httpx as hx

    # 1. Coletar noticias
    collection = await collect_news()
    logger.info(f"Clipping: coletadas {collection['collected']} noticias, {collection['saved']} novas")

    # 2. Buscar noticias das ultimas 24h nao processadas
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, description, link, source, category
            FROM news_items
            WHERE collected_at > NOW() - INTERVAL '48 hours'
            ORDER BY collected_at DESC
            LIMIT 50
        """)
        news = [dict(r) for r in cursor.fetchall()]

        # Buscar interesses do usuario para contexto
        interests = get_user_interests(1)

    if not news:
        return {"clipping": [], "total_collected": collection['collected'], "summary": "Nenhuma noticia coletada"}

    # 3. IA filtra e ranqueia
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # Fallback sem IA: usar relevance_score existente
        for n in news:
            n['relevance'] = calculate_relevance_score(n, interests)
        news.sort(key=lambda x: x.get('relevance', 0), reverse=True)
        return {
            "clipping": news[:limit],
            "total_collected": collection['collected'],
            "summary": f"Top {limit} noticias por relevancia (sem IA)"
        }

    # Preparar noticias para IA
    news_text = "\n".join([
        f"[{i+1}] {n['title']} ({n['source']}) - {(n.get('description') or '')[:150]}"
        for i, n in enumerate(news[:40])
    ])

    # Interesses aprendidos
    top_interests = ""
    if interests and interests.get('topics'):
        topics = interests['topics']
        sorted_topics = sorted(topics.items(), key=lambda x: x[1], reverse=True)[:10]
        top_interests = f"\nInteresses aprendidos: {', '.join(t[0] for t in sorted_topics)}"

    prompt = f"""Voce e o curador de noticias pessoal do Renato Prado.

PERFIL DO RENATO:
- Fundador da ImensIAH (plataforma de governanca + IA para conselhos)
- Conselheiro de empresas (Board Academy, Alba Consultoria, Assespro-SP)
- Engenheiro (Poli USP), empreendedor serial (6+ empresas)
- Diretor de Governanca da Assespro-SP
- Esportista (judo, tenis - Club Athletico Paulistano)
- Gestor de patrimonio familiar (fazendas, investimentos)
- Interesses: complexidade, sistemas adaptativos, lideranca, ESG{top_interests}

NOTICIAS DE HOJE:
{news_text}

Selecione as {limit} noticias MAIS RELEVANTES para o Renato e retorne APENAS JSON:
{{
  "clipping": [
    {{
      "index": 1,
      "titulo_resumido": "titulo curto em portugues (max 80 chars)",
      "resumo": "por que essa noticia importa para o Renato (1 frase, max 120 chars)",
      "relevancia": "alta/media",
      "categoria": "governanca/ia/empreendedorismo/esg/juridico/esporte/agro/estrategia",
      "sugestao_post": "ideia de hot take em 1 frase, ou null se nao aplicavel"
    }}
  ],
  "resumo_dia": "resumo geral do dia em 2 frases para o Renato"
}}

Priorize: noticias acionaveis, que o Renato pode comentar no LinkedIn, ou que afetam seus projetos/negocios."""

    try:
        async with hx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1500,
                      "messages": [{"role": "user", "content": prompt}]}
            )

        if resp.status_code != 200:
            logger.error(f"Clipping IA error: {resp.status_code}")
            return {"error": f"API error: {resp.status_code}", "total_collected": collection['collected']}

        text = resp.json()["content"][0]["text"]
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])

            # Enriquecer com links originais
            for item in result.get('clipping', []):
                idx = item.get('index', 0) - 1
                if 0 <= idx < len(news):
                    item['link'] = news[idx].get('link', '')
                    item['source'] = news[idx].get('source', '')
                    item['news_id'] = news[idx].get('id')

            result['total_collected'] = collection['collected']
            result['total_sources'] = len(NEWS_SOURCES)

            # Salvar clipping no banco
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO news_clippings (conteudo, resumo_dia, total_noticias, gerado_em)
                        VALUES (%s, %s, %s, NOW())
                        RETURNING id
                    """, (
                        json.dumps(result.get('clipping', []), ensure_ascii=False),
                        result.get('resumo_dia', ''),
                        collection['collected']
                    ))
                    result['clipping_id'] = cursor.fetchone()['id']
                    conn.commit()
            except Exception as e:
                logger.warning(f"Erro ao salvar clipping: {e}")

            return result

    except json.JSONDecodeError:
        logger.error("Erro ao parsear JSON do clipping")
    except Exception as e:
        logger.error(f"Erro no clipping: {e}")

    return {"error": "Falha ao gerar clipping", "total_collected": collection['collected']}


def record_clipping_feedback(news_id: int, feedback: str, user_id: int = 1) -> Dict:
    """
    Registra feedback do usuario sobre uma noticia do clipping.
    feedback: 'liked', 'disliked', 'shared', 'hot_take'
    Atualiza interesses do usuario para aprendizado.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar noticia
        cursor.execute("SELECT * FROM news_items WHERE id = %s", (news_id,))
        news = cursor.fetchone()
        if not news:
            return {"error": "Noticia nao encontrada"}
        news = dict(news)

        # Registrar interacao
        record_interaction(user_id, news_id, feedback)

        # Atualizar interesses do usuario
        update_user_interests(user_id, news, feedback)

        return {"status": "ok", "feedback": feedback, "news_id": news_id}
