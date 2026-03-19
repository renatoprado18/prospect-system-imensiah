"""
Integração LinkedIn para Enriquecimento de Prospects

Por enquanto opera com dados manuais.
Preparado para futura integração com Proxycurl ou similar.
"""
import os
import re
from typing import Optional, Dict, List
from datetime import datetime


class LinkedInIntegration:
    """Gerencia dados de LinkedIn dos prospects"""

    def __init__(self, api_key: Optional[str] = None):
        # Proxycurl API key (futuro)
        self.api_key = api_key or os.getenv("PROXYCURL_API_KEY")

    def extract_linkedin_username(self, url: str) -> Optional[str]:
        """
        Extrai username/vanity URL do LinkedIn

        Args:
            url: URL do perfil LinkedIn

        Returns:
            Username ou None
        """
        if not url:
            return None

        patterns = [
            r'linkedin\.com/in/([^/?\s]+)',
            r'linkedin\.com/pub/([^/?\s]+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, url.lower())
            if match:
                return match.group(1)

        return None

    def validate_linkedin_url(self, url: str) -> bool:
        """Valida se é um URL de LinkedIn válido"""
        if not url:
            return False

        return bool(re.search(r'linkedin\.com/(in|pub)/', url.lower()))

    def normalize_linkedin_url(self, url: str) -> Optional[str]:
        """Normaliza URL do LinkedIn para formato padrão"""
        username = self.extract_linkedin_username(url)
        if username:
            return f"https://www.linkedin.com/in/{username}"
        return url if self.validate_linkedin_url(url) else None

    def create_enrichment_structure(
        self,
        linkedin_url: Optional[str] = None,
        headline: Optional[str] = None,
        location: Optional[str] = None,
        connections: Optional[int] = None,
        posts: Optional[List[Dict]] = None,
        notes: Optional[str] = None
    ) -> Dict:
        """
        Cria estrutura de dados de enriquecimento LinkedIn

        Returns:
            Dict com dados estruturados do LinkedIn
        """
        return {
            "url": self.normalize_linkedin_url(linkedin_url) if linkedin_url else None,
            "username": self.extract_linkedin_username(linkedin_url) if linkedin_url else None,
            "headline": headline,
            "location": location,
            "connections": connections,
            "posts": posts or [],
            "notes": notes,
            "last_updated": datetime.now().isoformat()
        }

    def add_post(
        self,
        existing_data: Dict,
        post_url: str,
        post_text: str,
        post_date: Optional[str] = None,
        engagement: Optional[int] = None
    ) -> Dict:
        """
        Adiciona um post à lista de publicações relevantes

        Args:
            existing_data: Dados LinkedIn existentes
            post_url: URL do post
            post_text: Texto/resumo do post
            post_date: Data do post (opcional)
            engagement: Número de interações (opcional)

        Returns:
            Dados atualizados
        """
        posts = existing_data.get("posts", [])

        new_post = {
            "url": post_url,
            "text": post_text,
            "date": post_date or datetime.now().strftime("%Y-%m-%d"),
            "engagement": engagement or 0,
            "added_at": datetime.now().isoformat()
        }

        # Evitar duplicatas
        if not any(p.get("url") == post_url for p in posts):
            posts.insert(0, new_post)

        existing_data["posts"] = posts[:10]  # Manter últimos 10
        existing_data["last_updated"] = datetime.now().isoformat()

        return existing_data

    def generate_engagement_suggestions(self, linkedin_data: Dict) -> List[Dict]:
        """
        Gera sugestões de engajamento baseado em publicações

        Args:
            linkedin_data: Dados do LinkedIn do prospect

        Returns:
            Lista de sugestões de ação
        """
        suggestions = []
        posts = linkedin_data.get("posts", [])

        if not posts:
            suggestions.append({
                "type": "research",
                "priority": "medium",
                "action": "Verificar publicações recentes no LinkedIn",
                "reason": "Nenhuma publicação registrada ainda"
            })
            return suggestions

        # Verificar posts recentes
        for post in posts[:3]:
            post_date = post.get("date")
            if post_date:
                try:
                    days_ago = (datetime.now() - datetime.strptime(post_date, "%Y-%m-%d")).days
                    if days_ago <= 7:
                        suggestions.append({
                            "type": "engage",
                            "priority": "high",
                            "action": f"Comentar publicação sobre: {post.get('text', '')[:50]}...",
                            "reason": f"Post publicado há {days_ago} dias",
                            "url": post.get("url")
                        })
                except:
                    pass

        return suggestions

    # Métodos para futura integração com API
    async def fetch_profile(self, linkedin_url: str) -> Optional[Dict]:
        """
        Busca dados do perfil via API (requer Proxycurl ou similar)

        TODO: Implementar quando tivermos API
        """
        if not self.api_key:
            return None

        # Placeholder para futura implementação
        return None

    async def fetch_posts(self, linkedin_url: str, limit: int = 10) -> List[Dict]:
        """
        Busca posts recentes via API (requer Proxycurl ou similar)

        TODO: Implementar quando tivermos API
        """
        if not self.api_key:
            return []

        # Placeholder para futura implementação
        return []
