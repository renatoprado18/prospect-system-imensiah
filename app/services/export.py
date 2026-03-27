"""
Export Service - Exportacao de dados de contatos
"""
import csv
import json
import io
from typing import List, Dict, Optional
from datetime import datetime
from database import get_db


class ExportService:
    def export_contacts_csv(
        self,
        circulo: int = None,
        tags: List[str] = None,
        empresa: str = None,
        fields: List[str] = None
    ) -> str:
        """Exporta contatos para CSV"""
        with get_db() as conn:
            cursor = conn.cursor()

            conditions = ["1=1"]
            params = []

            if circulo is not None:
                conditions.append("COALESCE(circulo, 5) = %s")
                params.append(circulo)

            if tags:
                conditions.append("tags ?| %s")
                params.append(tags)

            if empresa:
                conditions.append("empresa ILIKE %s")
                params.append(f"%{empresa}%")

            where_clause = " AND ".join(conditions)

            # Default fields
            default_fields = [
                "id", "nome", "apelido", "empresa", "cargo", "circulo",
                "health_score", "emails", "telefones", "linkedin",
                "aniversario", "contexto", "tags", "ultimo_contato",
                "total_interacoes", "criado_em"
            ]

            selected_fields = fields if fields else default_fields

            cursor.execute(f"""
                SELECT {', '.join(selected_fields)}
                FROM contacts
                WHERE {where_clause}
                ORDER BY nome ASC
            """, params)

            contacts = cursor.fetchall()

            # Create CSV
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=selected_fields)
            writer.writeheader()

            for contact in contacts:
                row = dict(contact)
                # Serialize JSON fields
                for key in ['emails', 'telefones', 'tags']:
                    if key in row and row[key]:
                        if isinstance(row[key], (list, dict)):
                            row[key] = json.dumps(row[key], ensure_ascii=False)
                # Format dates
                for key in ['aniversario', 'ultimo_contato', 'criado_em']:
                    if key in row and row[key]:
                        if hasattr(row[key], 'strftime'):
                            row[key] = row[key].strftime('%Y-%m-%d')
                writer.writerow(row)

            return output.getvalue()

    def export_contacts_json(
        self,
        circulo: int = None,
        tags: List[str] = None,
        empresa: str = None,
        include_messages: bool = False,
        include_insights: bool = True
    ) -> List[Dict]:
        """Exporta contatos para JSON"""
        with get_db() as conn:
            cursor = conn.cursor()

            conditions = ["1=1"]
            params = []

            if circulo is not None:
                conditions.append("COALESCE(circulo, 5) = %s")
                params.append(circulo)

            if tags:
                conditions.append("tags ?| %s")
                params.append(tags)

            if empresa:
                conditions.append("empresa ILIKE %s")
                params.append(f"%{empresa}%")

            where_clause = " AND ".join(conditions)

            fields = """
                id, nome, apelido, empresa, cargo, circulo,
                health_score, emails, telefones, linkedin,
                linkedin_headline, foto_url, aniversario, contexto,
                tags, ultimo_contato, total_interacoes,
                origem, criado_em, atualizado_em
            """

            if include_insights:
                fields += ", resumo_ai, insights_ai"

            cursor.execute(f"""
                SELECT {fields}
                FROM contacts
                WHERE {where_clause}
                ORDER BY nome ASC
            """, params)

            contacts = []
            for row in cursor.fetchall():
                contact = dict(row)

                # Convert dates to ISO format
                for key in ['aniversario', 'ultimo_contato', 'criado_em', 'atualizado_em']:
                    if contact.get(key) and hasattr(contact[key], 'isoformat'):
                        contact[key] = contact[key].isoformat()

                # Include messages if requested
                if include_messages:
                    cursor.execute("""
                        SELECT direcao, conteudo, enviado_em
                        FROM messages
                        WHERE contact_id = %s
                        ORDER BY enviado_em DESC
                        LIMIT 50
                    """, (contact['id'],))
                    messages = []
                    for msg in cursor.fetchall():
                        msg_dict = dict(msg)
                        if msg_dict.get('enviado_em'):
                            msg_dict['enviado_em'] = msg_dict['enviado_em'].isoformat()
                        messages.append(msg_dict)
                    contact['messages'] = messages

                contacts.append(contact)

            return contacts

    def export_statistics(self) -> Dict:
        """Exporta estatisticas gerais"""
        with get_db() as conn:
            cursor = conn.cursor()

            stats = {
                "generated_at": datetime.now().isoformat(),
                "totals": {},
                "by_circle": {},
                "by_context": {},
                "top_companies": [],
                "health_distribution": {}
            }

            # Total contacts
            cursor.execute("SELECT COUNT(*) as total FROM contacts")
            stats["totals"]["contacts"] = cursor.fetchone()["total"]

            # Total with interactions
            cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE total_interacoes > 0")
            stats["totals"]["with_interactions"] = cursor.fetchone()["total"]

            # Total with email
            cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE jsonb_array_length(COALESCE(emails, '[]'::jsonb)) > 0")
            stats["totals"]["with_email"] = cursor.fetchone()["total"]

            # Total with phone
            cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE jsonb_array_length(COALESCE(telefones, '[]'::jsonb)) > 0")
            stats["totals"]["with_phone"] = cursor.fetchone()["total"]

            # By circle
            cursor.execute("""
                SELECT COALESCE(circulo, 5) as circulo, COUNT(*) as count
                FROM contacts
                GROUP BY COALESCE(circulo, 5)
                ORDER BY circulo
            """)
            for row in cursor.fetchall():
                stats["by_circle"][f"C{row['circulo']}"] = row["count"]

            # By context
            cursor.execute("""
                SELECT COALESCE(contexto, 'unknown') as contexto, COUNT(*) as count
                FROM contacts
                GROUP BY contexto
                ORDER BY count DESC
            """)
            for row in cursor.fetchall():
                stats["by_context"][row["contexto"]] = row["count"]

            # Top companies
            cursor.execute("""
                SELECT empresa, COUNT(*) as count
                FROM contacts
                WHERE empresa IS NOT NULL AND empresa != ''
                GROUP BY empresa
                ORDER BY count DESC
                LIMIT 20
            """)
            stats["top_companies"] = [dict(row) for row in cursor.fetchall()]

            # Health distribution
            cursor.execute("""
                SELECT
                    CASE
                        WHEN health_score >= 80 THEN 'excellent'
                        WHEN health_score >= 60 THEN 'good'
                        WHEN health_score >= 40 THEN 'fair'
                        WHEN health_score >= 20 THEN 'poor'
                        ELSE 'critical'
                    END as health_level,
                    COUNT(*) as count
                FROM contacts
                WHERE health_score IS NOT NULL
                GROUP BY health_level
                ORDER BY count DESC
            """)
            for row in cursor.fetchall():
                stats["health_distribution"][row["health_level"]] = row["count"]

            return stats


_export_service = None


def get_export_service() -> ExportService:
    global _export_service
    if _export_service is None:
        _export_service = ExportService()
    return _export_service
