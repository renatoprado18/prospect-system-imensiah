"""
Batch Operations Service - Operacoes em lote para contatos
"""
from typing import List, Dict, Optional
from datetime import datetime
from database import get_db


class BatchOperationsService:
    def update_tags_batch(
        self,
        contact_ids: List[int],
        add_tags: List[str] = None,
        remove_tags: List[str] = None
    ) -> Dict:
        """Adiciona ou remove tags de multiplos contatos"""
        with get_db() as conn:
            cursor = conn.cursor()
            stats = {"updated": 0, "errors": 0}

            for contact_id in contact_ids:
                try:
                    # Get current tags
                    cursor.execute(
                        "SELECT tags FROM contacts WHERE id = %s",
                        (contact_id,)
                    )
                    row = cursor.fetchone()
                    if not row:
                        stats["errors"] += 1
                        continue

                    current_tags = row["tags"] or []
                    if isinstance(current_tags, str):
                        import json
                        current_tags = json.loads(current_tags)

                    # Add new tags
                    if add_tags:
                        for tag in add_tags:
                            if tag not in current_tags:
                                current_tags.append(tag)

                    # Remove tags
                    if remove_tags:
                        current_tags = [t for t in current_tags if t not in remove_tags]

                    # Update
                    cursor.execute("""
                        UPDATE contacts
                        SET tags = %s::jsonb, atualizado_em = NOW()
                        WHERE id = %s
                    """, (str(current_tags).replace("'", '"'), contact_id))
                    stats["updated"] += 1

                except Exception as e:
                    stats["errors"] += 1

            conn.commit()
            return stats

    def update_circle_batch(
        self,
        contact_ids: List[int],
        circulo: int
    ) -> Dict:
        """Atualiza circulo de multiplos contatos"""
        if circulo < 1 or circulo > 5:
            return {"error": "Circulo deve ser entre 1 e 5"}

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE contacts
                SET circulo = %s,
                    circulo_manual = TRUE,
                    atualizado_em = NOW()
                WHERE id = ANY(%s)
            """, (circulo, contact_ids))

            updated = cursor.rowcount
            conn.commit()

            return {"updated": updated}

    def update_context_batch(
        self,
        contact_ids: List[int],
        contexto: str
    ) -> Dict:
        """Atualiza contexto de multiplos contatos"""
        valid_contexts = ["personal", "professional", "both"]
        if contexto not in valid_contexts:
            return {"error": f"Contexto deve ser: {', '.join(valid_contexts)}"}

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE contacts
                SET contexto = %s, atualizado_em = NOW()
                WHERE id = ANY(%s)
            """, (contexto, contact_ids))

            updated = cursor.rowcount
            conn.commit()

            return {"updated": updated}

    def merge_contacts(
        self,
        primary_id: int,
        secondary_ids: List[int]
    ) -> Dict:
        """Merge multiplos contatos em um principal"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Get primary contact
            cursor.execute("SELECT * FROM contacts WHERE id = %s", (primary_id,))
            primary = cursor.fetchone()
            if not primary:
                return {"error": "Contato principal nao encontrado"}

            primary = dict(primary)
            merged_count = 0

            for secondary_id in secondary_ids:
                if secondary_id == primary_id:
                    continue

                cursor.execute("SELECT * FROM contacts WHERE id = %s", (secondary_id,))
                secondary = cursor.fetchone()
                if not secondary:
                    continue

                secondary = dict(secondary)

                # Merge emails
                primary_emails = primary.get("emails") or []
                secondary_emails = secondary.get("emails") or []
                if isinstance(primary_emails, str):
                    import json
                    primary_emails = json.loads(primary_emails)
                if isinstance(secondary_emails, str):
                    import json
                    secondary_emails = json.loads(secondary_emails)

                for email in secondary_emails:
                    if email not in primary_emails:
                        primary_emails.append(email)

                # Merge phones
                primary_phones = primary.get("telefones") or []
                secondary_phones = secondary.get("telefones") or []
                if isinstance(primary_phones, str):
                    import json
                    primary_phones = json.loads(primary_phones)
                if isinstance(secondary_phones, str):
                    import json
                    secondary_phones = json.loads(secondary_phones)

                for phone in secondary_phones:
                    if phone not in primary_phones:
                        primary_phones.append(phone)

                # Merge tags
                primary_tags = primary.get("tags") or []
                secondary_tags = secondary.get("tags") or []
                if isinstance(primary_tags, str):
                    import json
                    primary_tags = json.loads(primary_tags)
                if isinstance(secondary_tags, str):
                    import json
                    secondary_tags = json.loads(secondary_tags)

                for tag in secondary_tags:
                    if tag not in primary_tags:
                        primary_tags.append(tag)

                # Merge interactions count
                total_interacoes = (primary.get("total_interacoes") or 0) + (secondary.get("total_interacoes") or 0)

                # Take most recent ultimo_contato
                ultimo_contato = primary.get("ultimo_contato")
                if secondary.get("ultimo_contato"):
                    if not ultimo_contato or secondary["ultimo_contato"] > ultimo_contato:
                        ultimo_contato = secondary["ultimo_contato"]

                # Fill in missing fields from secondary
                empresa = primary.get("empresa") or secondary.get("empresa")
                cargo = primary.get("cargo") or secondary.get("cargo")
                linkedin = primary.get("linkedin") or secondary.get("linkedin")
                foto_url = primary.get("foto_url") or secondary.get("foto_url")

                # Update primary
                cursor.execute("""
                    UPDATE contacts
                    SET emails = %s::jsonb,
                        telefones = %s::jsonb,
                        tags = %s::jsonb,
                        total_interacoes = %s,
                        ultimo_contato = %s,
                        empresa = COALESCE(empresa, %s),
                        cargo = COALESCE(cargo, %s),
                        linkedin = COALESCE(linkedin, %s),
                        foto_url = COALESCE(foto_url, %s),
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (
                    str(primary_emails).replace("'", '"'),
                    str(primary_phones).replace("'", '"'),
                    str(primary_tags).replace("'", '"'),
                    total_interacoes,
                    ultimo_contato,
                    empresa,
                    cargo,
                    linkedin,
                    foto_url,
                    primary_id
                ))

                # Reassign messages to primary
                cursor.execute("""
                    UPDATE messages
                    SET contact_id = %s
                    WHERE contact_id = %s
                """, (primary_id, secondary_id))

                # Reassign conversations to primary
                cursor.execute("""
                    UPDATE conversations
                    SET contact_id = %s
                    WHERE contact_id = %s
                """, (primary_id, secondary_id))

                # Delete secondary
                cursor.execute("DELETE FROM contacts WHERE id = %s", (secondary_id,))
                merged_count += 1

            conn.commit()

            return {
                "primary_id": primary_id,
                "merged_count": merged_count,
                "deleted_ids": secondary_ids
            }

    def delete_contacts_batch(
        self,
        contact_ids: List[int],
        confirm: bool = False
    ) -> Dict:
        """Deleta multiplos contatos (requer confirmacao)"""
        if not confirm:
            return {"error": "Confirmacao necessaria (confirm=true)"}

        with get_db() as conn:
            cursor = conn.cursor()

            # Delete related data first
            cursor.execute("DELETE FROM messages WHERE contact_id = ANY(%s)", (contact_ids,))
            cursor.execute("DELETE FROM conversations WHERE contact_id = ANY(%s)", (contact_ids,))
            cursor.execute("DELETE FROM contact_facts WHERE contact_id = ANY(%s)", (contact_ids,))
            cursor.execute("DELETE FROM contact_memories WHERE contact_id = ANY(%s)", (contact_ids,))

            # Delete contacts
            cursor.execute("DELETE FROM contacts WHERE id = ANY(%s)", (contact_ids,))
            deleted = cursor.rowcount

            conn.commit()

            return {"deleted": deleted}

    def recalculate_health_batch(
        self,
        contact_ids: List[int] = None,
        circulo_max: int = 5
    ) -> Dict:
        """Recalcula health score para contatos"""
        from services.circulos import calcular_health_score

        with get_db() as conn:
            cursor = conn.cursor()

            if contact_ids:
                cursor.execute("""
                    SELECT id FROM contacts WHERE id = ANY(%s)
                """, (contact_ids,))
            else:
                cursor.execute("""
                    SELECT id FROM contacts WHERE COALESCE(circulo, 5) <= %s
                """, (circulo_max,))

            ids = [row["id"] for row in cursor.fetchall()]
            updated = 0

            for contact_id in ids:
                try:
                    health = calcular_health_score(contact_id)
                    cursor.execute("""
                        UPDATE contacts
                        SET health_score = %s, atualizado_em = NOW()
                        WHERE id = %s
                    """, (health, contact_id))
                    updated += 1
                except:
                    pass

            conn.commit()

            return {"updated": updated, "total": len(ids)}


_batch_service = None


def get_batch_service() -> BatchOperationsService:
    global _batch_service
    if _batch_service is None:
        _batch_service = BatchOperationsService()
    return _batch_service
