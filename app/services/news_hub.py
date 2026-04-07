"""
News Hub Service - Central de Notícias Inteligente

Sistema de curadoria de notícias com aprendizado de preferências.
Combina:
- Notícias personalizadas (baseado em interações)
- Trending (assuntos quentes independente do gosto)
- Descoberta (fora da bolha)
"""

import httpx
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from collections import defaultdict

from database import get_db

logger = logging.getLogger(__name__)

# Fontes RSS (expandir conforme necessário)
NEWS_SOURCES = {
    "valor_economia": {
        "name": "Valor Econômico",
        "url": "https://pox.globo.com/rss/valor/",
        "category": "economia"
    },
    "google_business_br": {
        "name": "Google News - Negócios",
        "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FuQjBHZ0pDVWlnQVAB?hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "negocios"
    },
    "google_tech_br": {
        "name": "Google News - Tecnologia",
        "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FuQjBHZ0pDVWlnQVAB?hl=pt-BR&gl=BR&ceid=BR:pt-419",
        "category": "tecnologia"
    },
    "exame": {
        "name": "Exame",
        "url": "https://exame.com/feed/",
        "category": "negocios"
    }
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
    "governanca": ["governança", "conselho", "board", "compliance", "auditoria", "conselheiro"],
    "esg": ["esg", "sustentabilidade", "clima", "ambiental", "social", "carbono", "net zero"],
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
    user_interests = get_user_interests(user_id)

    with get_db() as conn:
        cursor = conn.cursor()

        # Trending (3-4 itens)
        cursor.execute('''
            SELECT n.*,
                   COALESCE(
                       (SELECT action FROM news_interactions
                        WHERE news_id = n.id AND user_id = %s
                        ORDER BY created_at DESC LIMIT 1),
                       NULL
                   ) as user_action
            FROM news_items n
            WHERE n.is_trending = TRUE
              AND n.collected_at > NOW() - INTERVAL '48 hours'
            ORDER BY n.trending_score DESC, n.collected_at DESC
            LIMIT 4
        ''', (user_id,))
        trending = [dict(row) for row in cursor.fetchall()]

        # Para você (baseado em interesses)
        cursor.execute('''
            SELECT n.*,
                   COALESCE(
                       (SELECT action FROM news_interactions
                        WHERE news_id = n.id AND user_id = %s
                        ORDER BY created_at DESC LIMIT 1),
                       NULL
                   ) as user_action
            FROM news_items n
            WHERE n.collected_at > NOW() - INTERVAL '72 hours'
              AND n.id NOT IN (SELECT id FROM news_items WHERE is_trending = TRUE)
            ORDER BY n.collected_at DESC
            LIMIT 50
        ''', (user_id,))

        all_news = [dict(row) for row in cursor.fetchall()]

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

        # Coletar todos os tópicos das notícias
        all_topics = []
        for item in trending + for_you:
            topics = item.get('topics') or []
            if isinstance(topics, str):
                try:
                    topics = json.loads(topics)
                except:
                    topics = []
            all_topics.extend(topics)

        # Artigos relacionados da base
        related_articles = get_related_articles(list(set(all_topics)), limit=3)

        # Digest do dia
        digest = generate_daily_digest(trending + for_you)

        return {
            "trending": trending,
            "for_you": for_you,
            "discovery": discovery,
            "interests": interest_stats,
            "related_articles": related_articles,
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


def get_related_articles(topics: List[str], limit: int = 3) -> List[Dict]:
    """Encontra artigos da base relacionados aos tópicos das notícias"""
    if not topics:
        return []

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar artigos que tenham categorias ou keywords relacionadas
        cursor.execute('''
            SELECT id, article_title, article_url, ai_categoria, ai_score_relevancia,
                   ai_gancho_linkedin, ai_keywords
            FROM editorial_posts
            WHERE article_url IS NOT NULL
              AND ai_score_relevancia IS NOT NULL
            ORDER BY ai_score_relevancia DESC
            LIMIT 50
        ''')

        articles = []
        for row in cursor.fetchall():
            article = dict(row)
            keywords = article.get('ai_keywords') or []
            if isinstance(keywords, str):
                try:
                    keywords = json.loads(keywords)
                except:
                    keywords = []

            categoria = (article.get('ai_categoria') or '').lower()

            # Score de match
            match_score = 0
            for topic in topics:
                topic_lower = topic.lower()
                if topic_lower in categoria:
                    match_score += 3
                for kw in keywords:
                    if topic_lower in kw.lower() or kw.lower() in topic_lower:
                        match_score += 1

            if match_score > 0:
                article['match_score'] = match_score
                articles.append(article)

        articles.sort(key=lambda x: -x.get('match_score', 0))
        return articles[:limit]


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


def get_news_with_insights(news_id: int) -> Optional[Dict]:
    """Retorna notícia com insights conectando aos artigos do usuário"""
    news = get_news_item(news_id)
    if not news:
        return None

    topics = news.get("topics", [])
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except:
            topics = []

    # Buscar artigos relacionados
    related = get_related_articles(topics, limit=5)

    # Gerar insights baseados em keyword matching
    insights = []
    for article in related[:3]:
        keywords = article.get('ai_keywords') or []
        if isinstance(keywords, str):
            try:
                keywords = json.loads(keywords)
            except:
                keywords = []

        # Encontrar keywords em comum
        common_themes = []
        for topic in topics:
            topic_lower = topic.lower()
            for kw in keywords:
                if topic_lower in kw.lower() or kw.lower() in topic_lower:
                    common_themes.append(kw)
                    break

        if common_themes:
            insight = {
                "article_title": article.get("article_title", ""),
                "article_url": article.get("article_url", ""),
                "connection": f"Conecta com o tema: {', '.join(common_themes[:2])}",
                "hook": article.get("ai_gancho_linkedin", "")
            }
            insights.append(insight)

    # Se não achou conexão por keywords, usar categoria
    if not insights and related:
        for article in related[:2]:
            categoria = article.get("ai_categoria", "")
            insights.append({
                "article_title": article.get("article_title", ""),
                "article_url": article.get("article_url", ""),
                "connection": f"Relacionado a: {categoria}" if categoria else "Tema próximo",
                "hook": article.get("ai_gancho_linkedin", "")
            })

    news["insights"] = insights
    news["related_articles"] = related

    return news


async def generate_smart_summary(news_id: int) -> Optional[str]:
    """Gera resumo inteligente usando Haiku (custo ~$0.0001)"""
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
            model="claude-3-5-haiku-20241022",
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
