"""
Apollo.io Integration Service

Enriquecimento de contatos usando a API Apollo.io.
Documentação: https://docs.apollo.io/reference/people-enrichment
"""
import os
import json
import httpx
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime
from database import get_db

logger = logging.getLogger(__name__)

APOLLO_API_BASE = "https://api.apollo.io/v1"


class ApolloService:
    """Serviço de integração com Apollo.io para enriquecimento de dados"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("APOLLO_API_KEY")
        self.credits_used = 0
        self.last_request_at = None

    def is_configured(self) -> bool:
        """Verifica se a API key está configurada"""
        return bool(self.api_key)

    async def enrich_person(
        self,
        linkedin_url: Optional[str] = None,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        organization_name: Optional[str] = None,
        domain: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Enriquece dados de uma pessoa via Apollo.io

        Args:
            linkedin_url: URL do perfil LinkedIn (melhor identificador)
            email: Email da pessoa
            first_name: Primeiro nome
            last_name: Sobrenome
            organization_name: Nome da empresa
            domain: Domínio da empresa (ex: google.com)

        Returns:
            Dict com dados enriquecidos ou erro
        """
        if not self.api_key:
            return {"success": False, "error": "API key não configurada"}

        # Montar payload
        payload = {}
        if linkedin_url:
            payload["linkedin_url"] = linkedin_url
        if email:
            payload["email"] = email
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if organization_name:
            payload["organization_name"] = organization_name
        if domain:
            payload["domain"] = domain

        if not payload:
            return {"success": False, "error": "Nenhum identificador fornecido"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{APOLLO_API_BASE}/people/match",
                    headers={
                        "Content-Type": "application/json",
                        "Cache-Control": "no-cache",
                        "X-Api-Key": self.api_key
                    },
                    json=payload
                )

                self.last_request_at = datetime.now()

                if response.status_code == 200:
                    data = response.json()
                    person = data.get("person", {})

                    if person:
                        self.credits_used += 1
                        return {
                            "success": True,
                            "matched": True,
                            "credits_used": 1,
                            "data": self._normalize_person_data(person)
                        }
                    else:
                        return {
                            "success": True,
                            "matched": False,
                            "credits_used": 0,
                            "error": "Perfil não encontrado"
                        }

                elif response.status_code == 401:
                    return {"success": False, "error": "API key inválida"}
                elif response.status_code == 429:
                    return {"success": False, "error": "Rate limit excedido"}
                else:
                    return {
                        "success": False,
                        "error": f"Erro API: {response.status_code}",
                        "detail": response.text[:500]
                    }

        except httpx.TimeoutException:
            return {"success": False, "error": "Timeout na requisição"}
        except Exception as e:
            logger.error(f"Erro ao enriquecer pessoa: {e}")
            return {"success": False, "error": str(e)}

    def _normalize_person_data(self, person: Dict) -> Dict:
        """Normaliza dados do Apollo para estrutura do INTEL"""

        # Extrair experiência atual
        employment = person.get("employment_history", [])
        current_job = employment[0] if employment else {}
        previous_job = employment[1] if len(employment) > 1 else {}

        # Extrair educação
        education = person.get("education", [])

        return {
            # Dados básicos
            "nome_completo": person.get("name"),
            "primeiro_nome": person.get("first_name"),
            "sobrenome": person.get("last_name"),
            "email": person.get("email"),
            "email_status": person.get("email_status"),  # verified, guessed, etc

            # LinkedIn
            "linkedin_url": person.get("linkedin_url"),
            "linkedin_headline": person.get("headline"),
            "linkedin_location": person.get("city") or person.get("state") or person.get("country"),
            "linkedin_about": person.get("summary"),
            "linkedin_connections": person.get("linkedin_connections"),

            # Emprego atual
            "cargo": person.get("title"),
            "empresa": person.get("organization", {}).get("name"),
            "empresa_linkedin": person.get("organization", {}).get("linkedin_url"),
            "empresa_website": person.get("organization", {}).get("website_url"),
            "empresa_tamanho": person.get("organization", {}).get("estimated_num_employees"),
            "empresa_industria": person.get("organization", {}).get("industry"),

            # Emprego anterior
            "empresa_anterior": previous_job.get("organization_name"),
            "cargo_anterior": previous_job.get("title"),

            # Telefones
            "telefones": person.get("phone_numbers", []),

            # Dados adicionais
            "cidade": person.get("city"),
            "estado": person.get("state"),
            "pais": person.get("country"),
            "seniority": person.get("seniority"),
            "departments": person.get("departments", []),

            # Educação (primeira)
            "educacao": [
                {
                    "instituicao": edu.get("school_name"),
                    "grau": edu.get("degree"),
                    "campo": edu.get("field_of_study"),
                    "ano_fim": edu.get("end_date")
                }
                for edu in education[:3]  # Limitar a 3
            ],

            # Metadados
            "apollo_id": person.get("id"),
            "enriched_at": datetime.now().isoformat(),
            "data_quality": self._calculate_data_quality(person)
        }

    def _calculate_data_quality(self, person: Dict) -> Dict:
        """Calcula score de qualidade dos dados"""
        fields = {
            "email": bool(person.get("email")),
            "email_verified": person.get("email_status") == "verified",
            "linkedin": bool(person.get("linkedin_url")),
            "cargo": bool(person.get("title")),
            "empresa": bool(person.get("organization", {}).get("name")),
            "telefone": bool(person.get("phone_numbers")),
            "localizacao": bool(person.get("city") or person.get("country")),
        }

        filled = sum(fields.values())
        total = len(fields)

        return {
            "score": round(filled / total * 100),
            "fields": fields,
            "filled": filled,
            "total": total
        }

    async def enrich_contact(self, contact_id: int) -> Dict[str, Any]:
        """
        Enriquece um contato do INTEL usando Apollo.io

        Args:
            contact_id: ID do contato no INTEL

        Returns:
            Dict com resultado do enriquecimento
        """
        # Buscar dados do contato
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, linkedin, empresa, cargo, emails
                FROM contacts
                WHERE id = %s
            """, (contact_id,))
            contact = cursor.fetchone()

        if not contact:
            return {"success": False, "error": "Contato não encontrado"}

        # Preparar identificadores para busca
        linkedin_url = contact.get("linkedin")
        emails = contact.get("emails") or []
        email = emails[0].get("email") if emails else None
        nome = contact.get("nome", "")
        nome_parts = nome.split(" ", 1)
        first_name = nome_parts[0] if nome_parts else None
        last_name = nome_parts[1] if len(nome_parts) > 1 else None
        empresa = contact.get("empresa")

        # Fazer enriquecimento
        result = await self.enrich_person(
            linkedin_url=linkedin_url,
            email=email,
            first_name=first_name,
            last_name=last_name,
            organization_name=empresa
        )

        if not result.get("success") or not result.get("matched"):
            return result

        # Atualizar contato com dados enriquecidos
        enriched = result["data"]

        with get_db() as conn:
            cursor = conn.cursor()

            # Atualizar campos do contato
            updates = []
            params = []

            # Só atualizar se o campo estiver vazio ou for mais completo
            if enriched.get("linkedin_headline"):
                updates.append("linkedin_headline = %s")
                params.append(enriched["linkedin_headline"])

            if enriched.get("linkedin_location"):
                updates.append("linkedin_location = %s")
                params.append(enriched["linkedin_location"])

            if enriched.get("linkedin_about"):
                updates.append("linkedin_about = %s")
                params.append(enriched["linkedin_about"])

            if enriched.get("linkedin_connections"):
                updates.append("linkedin_connections = %s")
                params.append(enriched["linkedin_connections"])

            if enriched.get("cargo") and not contact.get("cargo"):
                updates.append("cargo = %s")
                params.append(enriched["cargo"])

            if enriched.get("empresa") and not contact.get("empresa"):
                updates.append("empresa = %s")
                params.append(enriched["empresa"])

            if enriched.get("empresa_anterior"):
                updates.append("linkedin_previous_company = %s")
                params.append(enriched["empresa_anterior"])

            if enriched.get("cargo_anterior"):
                updates.append("linkedin_previous_title = %s")
                params.append(enriched["cargo_anterior"])

            # Marcar como enriquecido
            updates.append("linkedin_enriched_at = %s")
            params.append(datetime.now())

            updates.append("enrichment_sources = COALESCE(enrichment_sources, '[]'::jsonb) || %s::jsonb")
            params.append('["apollo.io"]')

            updates.append("atualizado_em = NOW()")

            if updates:
                params.append(contact_id)
                cursor.execute(f"""
                    UPDATE contacts
                    SET {", ".join(updates)}
                    WHERE id = %s
                    RETURNING id
                """, params)
                conn.commit()

            # Registrar histórico de enriquecimento
            cursor.execute("""
                INSERT INTO contact_enrichment_log
                (contact_id, source, data, success, credits_used)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                contact_id,
                "apollo.io",
                enriched,
                True,
                result.get("credits_used", 1)
            ))
            conn.commit()

        result["contact_id"] = contact_id
        result["fields_updated"] = len(updates) - 3  # Excluir campos de metadados

        return result

    async def get_enrichment_stats(self) -> Dict:
        """Retorna estatísticas de enriquecimento"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Total de enriquecimentos
            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE success = TRUE) as success,
                    COALESCE(SUM(credits_used), 0) as credits_used
                FROM contact_enrichment_log
                WHERE source = 'apollo.io'
            """)
            stats = dict(cursor.fetchone())

            # Contatos pendentes de enriquecimento
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM contacts
                WHERE linkedin IS NOT NULL
                  AND linkedin != ''
                  AND linkedin_enriched_at IS NULL
            """)
            stats["pending"] = cursor.fetchone()["cnt"]

            # Contatos já enriquecidos
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM contacts
                WHERE linkedin_enriched_at IS NOT NULL
            """)
            stats["enriched"] = cursor.fetchone()["cnt"]

        stats["api_configured"] = self.is_configured()

        return stats


# Singleton
_apollo_service = None


def get_apollo_service() -> ApolloService:
    """Retorna instância singleton do ApolloService"""
    global _apollo_service
    if _apollo_service is None:
        _apollo_service = ApolloService()
    return _apollo_service
