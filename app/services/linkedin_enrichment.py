"""
LinkedIn Enrichment Service - RapidAPI Integration

Enriquece contatos com dados do LinkedIn via RapidAPI Fresh LinkedIn API.
Detecta mudancas de emprego e gera alertas.
"""
import os
import re
import httpx
import logging
from typing import Optional, Dict, List, Tuple
from datetime import datetime
from database import get_db

logger = logging.getLogger(__name__)

# RapidAPI Configuration
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = "fresh-linkedin-profile-data.p.rapidapi.com"
RAPIDAPI_BASE_URL = f"https://{RAPIDAPI_HOST}"


class LinkedInEnrichmentService:
    """Servico de enriquecimento LinkedIn via RapidAPI"""

    def __init__(self):
        self.api_key = RAPIDAPI_KEY
        self.headers = {
            "X-RapidAPI-Key": self.api_key or "",
            "X-RapidAPI-Host": RAPIDAPI_HOST
        }

    def is_configured(self) -> bool:
        """Verifica se a API esta configurada"""
        return bool(self.api_key)

    def extract_username(self, linkedin_url: str) -> Optional[str]:
        """Extrai username do URL do LinkedIn"""
        if not linkedin_url:
            return None
        patterns = [
            r'linkedin\.com/in/([^/?\s]+)',
            r'linkedin\.com/pub/([^/?\s]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, linkedin_url.lower())
            if match:
                return match.group(1)
        return None

    def parse_headline(self, headline: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse LinkedIn headline to extract title and company.

        Examples:
        - "Sócio Fundador na Aptus Management" -> ("Sócio Fundador", "Aptus Management")
        - "CEO at Google" -> ("CEO", "Google")
        - "Software Engineer | Microsoft" -> ("Software Engineer", "Microsoft")
        - "Diretor de Marketing - Empresa XYZ" -> ("Diretor de Marketing", "Empresa XYZ")

        Returns (cargo, empresa) tuple
        """
        if not headline:
            return None, None

        headline = headline.strip()

        # Patterns to split title from company
        # Order matters - check more specific patterns first
        separators = [
            r'\s+(?:na|no|at|@)\s+',      # "na", "no", "at", "@"
            r'\s+[-–—]\s+',                # " - ", " – ", " — "
            r'\s*\|\s*',                   # " | "
            r'\s+em\s+',                   # "em"
        ]

        for sep in separators:
            parts = re.split(sep, headline, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                cargo = parts[0].strip()
                empresa = parts[1].strip()
                # Validate - both should have reasonable length
                if len(cargo) >= 2 and len(empresa) >= 2:
                    return cargo, empresa

        # If no separator found, try to detect if it's just a title or just a company
        # For now, return None for both if we can't parse
        return None, None

    async def fetch_profile(self, linkedin_url: str) -> Dict:
        """
        Busca dados do perfil LinkedIn via RapidAPI

        Args:
            linkedin_url: URL do perfil LinkedIn

        Returns:
            Dict com dados do perfil ou erro
        """
        if not self.is_configured():
            return {"error": "API key not configured", "code": "NO_API_KEY"}

        username = self.extract_username(linkedin_url)
        if not username:
            return {"error": "Invalid LinkedIn URL", "code": "INVALID_URL"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{RAPIDAPI_BASE_URL}/get-linkedin-profile",
                    headers=self.headers,
                    params={"linkedin_url": linkedin_url}
                )

                if response.status_code == 200:
                    data = response.json()
                    return self._parse_profile_response(data)
                elif response.status_code == 404:
                    return {"error": "Profile not found", "code": "NOT_FOUND"}
                elif response.status_code == 429:
                    return {"error": "Rate limit exceeded", "code": "RATE_LIMIT"}
                else:
                    return {
                        "error": f"API error: {response.status_code}",
                        "code": "API_ERROR",
                        "details": response.text
                    }

        except httpx.TimeoutException:
            return {"error": "Request timeout", "code": "TIMEOUT"}
        except Exception as e:
            logger.error(f"LinkedIn API error: {e}")
            return {"error": str(e), "code": "EXCEPTION"}

    def _parse_profile_response(self, data: Dict) -> Dict:
        """Parseia resposta da API para formato padrao"""
        if not data or "data" not in data:
            return {"error": "Empty response", "code": "EMPTY_RESPONSE"}

        profile = data.get("data", {})

        # Extrair experiencia atual
        experiences = profile.get("experiences", [])
        current_job = experiences[0] if experiences else {}

        # Extrair educacao
        education = profile.get("education", [])

        # Extrair skills
        skills = profile.get("skills", [])

        return {
            "success": True,
            "profile": {
                "full_name": profile.get("full_name"),
                "headline": profile.get("headline"),
                "location": profile.get("location"),
                "about": profile.get("about"),
                "connections": profile.get("connections_count"),
                "followers": profile.get("followers_count"),
                "profile_picture": profile.get("profile_picture"),
                "background_image": profile.get("background_image"),
                "open_to_work": profile.get("open_to_work", False),

                # Emprego atual
                "current_company": current_job.get("company"),
                "current_title": current_job.get("title"),
                "current_company_url": current_job.get("company_linkedin_url"),
                "current_start_date": current_job.get("start_date"),

                # Historico completo
                "experiences": [
                    {
                        "company": exp.get("company"),
                        "title": exp.get("title"),
                        "location": exp.get("location"),
                        "start_date": exp.get("start_date"),
                        "end_date": exp.get("end_date"),
                        "description": exp.get("description"),
                        "company_url": exp.get("company_linkedin_url")
                    }
                    for exp in experiences[:10]  # Limita a 10
                ],

                # Educacao
                "education": [
                    {
                        "school": edu.get("school"),
                        "degree": edu.get("degree"),
                        "field": edu.get("field_of_study"),
                        "start_date": edu.get("start_date"),
                        "end_date": edu.get("end_date")
                    }
                    for edu in education[:5]
                ],

                # Skills
                "skills": [s.get("name") for s in skills[:20]] if isinstance(skills, list) else [],

                # Posts recentes (se disponivel)
                "recent_posts": profile.get("posts", [])[:5],

                # Metadata
                "last_activity": profile.get("last_activity_date"),
                "public_identifier": profile.get("public_identifier"),
                "fetched_at": datetime.now().isoformat()
            }
        }

    def detect_job_change(self, contact: Dict, new_profile: Dict) -> Optional[Dict]:
        """
        Detecta mudanca de emprego comparando dados atuais vs novos

        Args:
            contact: Dados atuais do contato no banco
            new_profile: Novos dados do perfil LinkedIn

        Returns:
            Dict com detalhes da mudanca ou None se nao houve mudanca
        """
        profile = new_profile.get("profile", {})

        old_company = contact.get("empresa") or contact.get("linkedin_previous_company")
        old_title = contact.get("cargo") or contact.get("linkedin_previous_title")

        new_company = profile.get("current_company")
        new_title = profile.get("current_title")

        # Normalizar para comparacao
        def normalize(s):
            if not s:
                return ""
            return s.lower().strip()

        old_company_norm = normalize(old_company)
        new_company_norm = normalize(new_company)
        old_title_norm = normalize(old_title)
        new_title_norm = normalize(new_title)

        # Detectar tipo de mudanca
        change_type = None
        if old_company_norm and new_company_norm and old_company_norm != new_company_norm:
            change_type = "job_change"  # Mudou de empresa
        elif old_title_norm and new_title_norm and old_title_norm != new_title_norm:
            if old_company_norm == new_company_norm:
                change_type = "promotion"  # Mesmo empresa, cargo diferente
            else:
                change_type = "job_change"

        if not change_type:
            return None

        return {
            "type": change_type,
            "old_company": old_company,
            "new_company": new_company,
            "old_title": old_title,
            "new_title": new_title,
            "old_headline": contact.get("linkedin_headline"),
            "new_headline": profile.get("headline"),
            "detected_at": datetime.now().isoformat()
        }

    async def enrich_contact(self, contact_id: int, force: bool = False) -> Dict:
        """
        Enriquece um contato com dados do LinkedIn

        Args:
            contact_id: ID do contato
            force: Se True, enriquece mesmo se ja foi feito recentemente

        Returns:
            Dict com resultado do enriquecimento
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar contato
            cursor.execute("""
                SELECT id, nome, linkedin, empresa, cargo, linkedin_headline,
                       linkedin_enriched_at, linkedin_previous_company, linkedin_previous_title
                FROM contacts
                WHERE id = %s
            """, (contact_id,))

            contact = cursor.fetchone()
            if not contact:
                return {"error": "Contact not found", "code": "NOT_FOUND"}

            contact = dict(contact)

            # Verificar se tem LinkedIn
            if not contact.get("linkedin"):
                return {"error": "No LinkedIn URL", "code": "NO_LINKEDIN"}

            # Verificar se ja foi enriquecido recentemente (7 dias)
            if not force and contact.get("linkedin_enriched_at"):
                days_since = (datetime.now() - contact["linkedin_enriched_at"]).days
                if days_since < 7:
                    return {
                        "error": f"Enriched {days_since} days ago",
                        "code": "RECENTLY_ENRICHED",
                        "last_enriched": contact["linkedin_enriched_at"].isoformat()
                    }

            # Buscar dados do LinkedIn
            result = await self.fetch_profile(contact["linkedin"])

            if "error" in result:
                # Atualizar status de erro
                cursor.execute("""
                    UPDATE contacts
                    SET enriquecimento_status = %s,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (f"error: {result['code']}", contact_id))
                return result

            profile = result.get("profile", {})

            # Detectar mudanca de emprego
            job_change = self.detect_job_change(contact, result)

            if job_change:
                # Registrar mudanca no historico
                cursor.execute("""
                    INSERT INTO linkedin_enrichment_history
                    (contact_id, empresa_anterior, cargo_anterior, empresa_nova, cargo_nova,
                     headline_anterior, headline_nova, tipo_mudanca, dados_completos)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    contact_id,
                    job_change["old_company"],
                    job_change["old_title"],
                    job_change["new_company"],
                    job_change["new_title"],
                    job_change["old_headline"],
                    job_change["new_headline"],
                    job_change["type"],
                    str(profile)
                ))

            # Extract company/title from profile or parse from headline
            current_company = profile.get("current_company")
            current_title = profile.get("current_title")

            # If company/title not provided, try to parse from headline
            if not current_company or not current_title:
                headline = profile.get("headline", "")
                parsed_title, parsed_company = self.parse_headline(headline)
                if not current_company and parsed_company:
                    current_company = parsed_company
                    logger.info(f"Parsed company from headline: {parsed_company}")
                if not current_title and parsed_title:
                    current_title = parsed_title
                    logger.info(f"Parsed title from headline: {parsed_title}")

            # Atualizar contato com novos dados
            cursor.execute("""
                UPDATE contacts
                SET empresa = COALESCE(%s, empresa),
                    cargo = COALESCE(%s, cargo),
                    linkedin_headline = %s,
                    linkedin_location = %s,
                    linkedin_about = %s,
                    linkedin_experience = %s,
                    linkedin_education = %s,
                    linkedin_skills = %s,
                    linkedin_connections = %s,
                    linkedin_open_to_work = %s,
                    linkedin_last_activity = %s,
                    linkedin_enriched_at = CURRENT_TIMESTAMP,
                    linkedin_previous_company = %s,
                    linkedin_previous_title = %s,
                    linkedin_job_changed_at = %s,
                    foto_url = COALESCE(%s, foto_url),
                    enriquecimento_status = 'success',
                    ultimo_enriquecimento = CURRENT_TIMESTAMP,
                    atualizado_em = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (
                current_company,
                current_title,
                profile.get("headline"),
                profile.get("location"),
                profile.get("about"),
                str(profile.get("experiences", [])),
                str(profile.get("education", [])),
                str(profile.get("skills", [])),
                profile.get("connections"),
                profile.get("open_to_work", False),
                profile.get("last_activity"),
                contact.get("empresa"),  # Salvar anterior
                contact.get("cargo"),    # Salvar anterior
                datetime.now() if job_change else None,
                profile.get("profile_picture"),
                contact_id
            ))

            return {
                "success": True,
                "contact_id": contact_id,
                "nome": contact["nome"],
                "profile": profile,
                "job_change": job_change
            }

    async def enrich_batch(
        self,
        limit: int = 50,
        circulo_max: int = 3,
        force: bool = False
    ) -> Dict:
        """
        Enriquece multiplos contatos em batch

        Args:
            limit: Numero maximo de contatos a processar
            circulo_max: Processar contatos ate este circulo (1-5)
            force: Se True, re-enriquece mesmo os ja processados

        Returns:
            Dict com estatisticas do batch
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar contatos para enriquecer
            query = """
                SELECT id, nome, linkedin, circulo
                FROM contacts
                WHERE linkedin IS NOT NULL
                  AND linkedin != ''
                  AND (circulo <= %s OR circulo IS NULL)
            """
            params = [circulo_max]

            if not force:
                query += """
                    AND (linkedin_enriched_at IS NULL
                         OR linkedin_enriched_at < NOW() - INTERVAL '30 days')
                """

            query += " ORDER BY circulo ASC NULLS LAST, ultimo_contato DESC NULLS LAST LIMIT %s"
            params.append(limit)

            cursor.execute(query, params)
            contacts = cursor.fetchall()

        stats = {
            "total": len(contacts),
            "success": 0,
            "errors": 0,
            "job_changes": 0,
            "results": []
        }

        for contact in contacts:
            contact = dict(contact)
            result = await self.enrich_contact(contact["id"], force=True)

            if result.get("success"):
                stats["success"] += 1
                if result.get("job_change"):
                    stats["job_changes"] += 1
            else:
                stats["errors"] += 1

            stats["results"].append({
                "contact_id": contact["id"],
                "nome": contact["nome"],
                "success": result.get("success", False),
                "error": result.get("error"),
                "job_change": result.get("job_change")
            })

            # Rate limiting - 1 request per second
            import asyncio
            await asyncio.sleep(1)

        return stats

    def get_pending_enrichments(self, limit: int = 100) -> List[Dict]:
        """Retorna contatos que precisam de enriquecimento"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, linkedin, empresa, cargo, circulo,
                       linkedin_enriched_at, ultimo_contato
                FROM contacts
                WHERE linkedin IS NOT NULL
                  AND linkedin != ''
                  AND (linkedin_enriched_at IS NULL
                       OR linkedin_enriched_at < NOW() - INTERVAL '30 days')
                ORDER BY circulo ASC NULLS LAST, ultimo_contato DESC NULLS LAST
                LIMIT %s
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_job_changes(self, days: int = 30, notified: bool = None) -> List[Dict]:
        """Retorna mudancas de emprego detectadas"""
        with get_db() as conn:
            cursor = conn.cursor()
            query = """
                SELECT h.*, c.nome, c.linkedin, c.foto_url
                FROM linkedin_enrichment_history h
                JOIN contacts c ON c.id = h.contact_id
                WHERE h.detectado_em > NOW() - INTERVAL '%s days'
            """
            params = [days]

            if notified is not None:
                query += " AND h.notificado = %s"
                params.append(notified)

            query += " ORDER BY h.detectado_em DESC"

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_enrichment_stats(self) -> Dict:
        """Retorna estatisticas de enriquecimento"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Total com LinkedIn
            cursor.execute("""
                SELECT COUNT(*) as total FROM contacts
                WHERE linkedin IS NOT NULL AND linkedin != ''
            """)
            total_with_linkedin = cursor.fetchone()["total"]

            # Enriquecidos
            cursor.execute("""
                SELECT COUNT(*) as total FROM contacts
                WHERE linkedin_enriched_at IS NOT NULL
            """)
            enriched = cursor.fetchone()["total"]

            # Pendentes
            cursor.execute("""
                SELECT COUNT(*) as total FROM contacts
                WHERE linkedin IS NOT NULL AND linkedin != ''
                  AND (linkedin_enriched_at IS NULL
                       OR linkedin_enriched_at < NOW() - INTERVAL '30 days')
            """)
            pending = cursor.fetchone()["total"]

            # Mudancas de emprego (ultimos 30 dias)
            cursor.execute("""
                SELECT COUNT(*) as total FROM linkedin_enrichment_history
                WHERE detectado_em > NOW() - INTERVAL '30 days'
            """)
            job_changes = cursor.fetchone()["total"]

            # Por circulo
            cursor.execute("""
                SELECT circulo, COUNT(*) as total
                FROM contacts
                WHERE linkedin IS NOT NULL AND linkedin != ''
                GROUP BY circulo
                ORDER BY circulo
            """)
            by_circulo = {row["circulo"]: row["total"] for row in cursor.fetchall()}

            return {
                "total_with_linkedin": total_with_linkedin,
                "enriched": enriched,
                "pending": pending,
                "job_changes_30d": job_changes,
                "by_circulo": by_circulo
            }


# Singleton
_linkedin_service = None


def get_linkedin_enrichment_service() -> LinkedInEnrichmentService:
    global _linkedin_service
    if _linkedin_service is None:
        _linkedin_service = LinkedInEnrichmentService()
    return _linkedin_service
