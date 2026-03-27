"""
Search Service - Busca avancada de contatos
"""
from typing import List, Dict, Optional
from database import get_db


class SearchService:
    def search_contacts(
        self,
        query: str = None,
        circulo: int = None,
        tags: List[str] = None,
        health_min: int = None,
        health_max: int = None,
        has_email: bool = None,
        has_whatsapp: bool = None,
        empresa: str = None,
        contexto: str = None,
        ordem: str = "nome",
        limit: int = 50,
        offset: int = 0
    ) -> Dict:
        """Busca avancada com multiplos filtros"""
        with get_db() as conn:
            cursor = conn.cursor()

            conditions = ["1=1"]
            params = []

            if query:
                conditions.append("""
                    (nome ILIKE %s OR empresa ILIKE %s OR
                     apelido ILIKE %s OR cargo ILIKE %s OR
                     linkedin_headline ILIKE %s)
                """)
                like_query = f"%{query}%"
                params.extend([like_query, like_query, like_query, like_query, like_query])

            if circulo is not None:
                conditions.append("COALESCE(circulo, 5) = %s")
                params.append(circulo)

            if tags:
                conditions.append("tags ?| %s")
                params.append(tags)

            if health_min is not None:
                conditions.append("COALESCE(health_score, 50) >= %s")
                params.append(health_min)

            if health_max is not None:
                conditions.append("COALESCE(health_score, 50) <= %s")
                params.append(health_max)

            if has_email:
                conditions.append("jsonb_array_length(COALESCE(emails, '[]'::jsonb)) > 0")

            if has_whatsapp:
                conditions.append("jsonb_array_length(COALESCE(telefones, '[]'::jsonb)) > 0")

            if empresa:
                conditions.append("empresa ILIKE %s")
                params.append(f"%{empresa}%")

            if contexto:
                conditions.append("contexto = %s")
                params.append(contexto)

            where_clause = " AND ".join(conditions)

            order_map = {
                "nome": "nome ASC",
                "empresa": "empresa ASC NULLS LAST",
                "circulo": "circulo ASC",
                "health": "health_score DESC NULLS LAST",
                "ultimo_contato": "ultimo_contato DESC NULLS LAST",
                "recente": "atualizado_em DESC",
                "interacoes": "total_interacoes DESC NULLS LAST"
            }
            order_by = order_map.get(ordem, "nome ASC")

            cursor.execute(f"""
                SELECT COUNT(*) as total FROM contacts WHERE {where_clause}
            """, params)
            total = cursor.fetchone()["total"]

            cursor.execute(f"""
                SELECT id, nome, apelido, empresa, cargo, circulo,
                       health_score, foto_url, ultimo_contato, tags,
                       emails, telefones, linkedin, total_interacoes,
                       contexto, resumo_ai
                FROM contacts
                WHERE {where_clause}
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
            """, params + [limit, offset])

            contacts = [dict(row) for row in cursor.fetchall()]

            return {
                "contacts": contacts,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(contacts) < total
            }

    def get_search_suggestions(self, query: str, limit: int = 10) -> List[Dict]:
        """Sugestoes de autocomplete"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT ON (nome) id, nome, empresa, foto_url, circulo
                FROM contacts
                WHERE nome ILIKE %s
                ORDER BY nome, circulo ASC
                LIMIT %s
            """, (f"%{query}%", limit))
            return [dict(row) for row in cursor.fetchall()]

    def get_contacts_by_company(self, empresa: str, limit: int = 50) -> List[Dict]:
        """Busca contatos por empresa"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, cargo, circulo, foto_url, ultimo_contato
                FROM contacts
                WHERE empresa ILIKE %s
                ORDER BY circulo ASC, nome ASC
                LIMIT %s
            """, (f"%{empresa}%", limit))
            return [dict(row) for row in cursor.fetchall()]

    def get_nearby_birthdays(self, days: int = 30) -> List[Dict]:
        """Busca contatos com aniversario proximo"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, empresa, aniversario, circulo, foto_url
                FROM contacts
                WHERE aniversario IS NOT NULL
                AND (
                    (EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
                     AND EXTRACT(DAY FROM aniversario) >= EXTRACT(DAY FROM CURRENT_DATE))
                    OR
                    (EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE + INTERVAL '%s days')
                     AND EXTRACT(DAY FROM aniversario) <= EXTRACT(DAY FROM CURRENT_DATE + INTERVAL '%s days'))
                )
                ORDER BY
                    EXTRACT(MONTH FROM aniversario),
                    EXTRACT(DAY FROM aniversario)
                LIMIT 50
            """, (days, days))
            return [dict(row) for row in cursor.fetchall()]

    def get_stale_contacts(self, days: int = 90, circulo_max: int = 3) -> List[Dict]:
        """Busca contatos importantes sem interacao recente"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nome, empresa, circulo, ultimo_contato, health_score, foto_url
                FROM contacts
                WHERE COALESCE(circulo, 5) <= %s
                AND (
                    ultimo_contato IS NULL
                    OR ultimo_contato < NOW() - INTERVAL '%s days'
                )
                ORDER BY circulo ASC, ultimo_contato ASC NULLS FIRST
                LIMIT 50
            """, (circulo_max, days))
            return [dict(row) for row in cursor.fetchall()]


_search_service = None


def get_search_service() -> SearchService:
    global _search_service
    if _search_service is None:
        _search_service = SearchService()
    return _search_service
