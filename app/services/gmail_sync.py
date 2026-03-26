"""
Gmail Sync Service
Sincroniza emails do Gmail com contatos do INTEL

Funcionalidades:
- Busca emails trocados com cada contato
- Atualiza ultimo_contato e total_interacoes
- Dispara recalculo de circulos

Autor: INTEL
Data: 2026-03-26
"""
import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from database import get_db
from integrations.gmail import GmailIntegration, parse_gmail_date
from services.circulos import calcular_score_circulo, calcular_health_score

logger = logging.getLogger(__name__)


class GmailSyncService:
    """
    Service para sincronizar interacoes do Gmail com contatos INTEL.
    """

    def __init__(self):
        self.gmail = GmailIntegration()
        self._sync_status = {
            "running": False,
            "started_at": None,
            "completed_at": None,
            "total_contacts": 0,
            "processed": 0,
            "updated": 0,
            "errors": 0,
            "last_error": None
        }

    async def get_valid_token(self, account: Dict) -> Optional[str]:
        """
        Obtem token de acesso valido, renovando se necessario.
        """
        refresh_token = account.get("refresh_token")
        if not refresh_token:
            return None

        result = await self.gmail.refresh_access_token(refresh_token)
        if "error" in result:
            logger.error(f"Erro ao refresh token: {result.get('error')}")
            return None

        return result.get("access_token")

    async def count_messages_for_email(
        self,
        access_token: str,
        email: str,
        months_back: int = 12
    ) -> Dict[str, Any]:
        """
        Conta mensagens de/para um email e retorna data mais recente.

        Returns:
            {count: int, latest_date: datetime or None}
        """
        result = {"count": 0, "latest_date": None}

        # Buscar mensagens envolvendo este email
        query = f"(from:{email} OR to:{email})"
        if months_back:
            date_after = (datetime.now() - timedelta(days=months_back * 30)).strftime("%Y/%m/%d")
            query += f" after:{date_after}"

        try:
            response = await self.gmail.list_messages(
                access_token=access_token,
                query=query,
                max_results=100
            )

            if "error" in response:
                if response["error"] == "token_expired":
                    return {"count": 0, "latest_date": None, "error": "token_expired"}
                return result

            messages = response.get("messages", [])
            result["count"] = len(messages)

            # Pegar data da mensagem mais recente
            if messages:
                msg_id = messages[0]["id"]
                msg_detail = await self.gmail.get_message(
                    access_token=access_token,
                    message_id=msg_id,
                    format="metadata"
                )

                if "error" not in msg_detail:
                    headers = self.gmail.parse_message_headers(msg_detail)
                    date_str = headers.get("date", "")
                    if date_str:
                        result["latest_date"] = parse_gmail_date(date_str)

        except Exception as e:
            logger.error(f"Erro ao contar mensagens para {email}: {e}")

        return result

    async def sync_contact_emails(
        self,
        contact_id: int,
        email: str,
        access_token: str,
        months_back: int = 12
    ) -> Dict[str, Any]:
        """
        Sincroniza emails de um contato especifico.

        Args:
            contact_id: ID do contato
            email: Email do contato
            access_token: Token de acesso Gmail
            months_back: Meses para buscar (default 12)

        Returns:
            {success: bool, count: int, latest_date: datetime, updated: bool}
        """
        result = {"success": False, "count": 0, "latest_date": None, "updated": False}

        try:
            # Contar mensagens
            msg_result = await self.count_messages_for_email(
                access_token=access_token,
                email=email,
                months_back=months_back
            )

            if msg_result.get("error") == "token_expired":
                return {"success": False, "error": "token_expired"}

            result["count"] = msg_result["count"]
            result["latest_date"] = msg_result["latest_date"]

            if msg_result["count"] > 0:
                # Atualizar contato no banco
                with get_db() as conn:
                    cursor = conn.cursor()

                    # Buscar valores atuais
                    cursor.execute(
                        "SELECT total_interacoes, ultimo_contato FROM contacts WHERE id = %s",
                        (contact_id,)
                    )
                    current = cursor.fetchone()
                    if not current:
                        return result

                    current_interactions = current["total_interacoes"] or 0
                    current_ultimo = current["ultimo_contato"]

                    # Merge valores (manter maior contagem, data mais recente)
                    new_interactions = max(current_interactions, msg_result["count"])
                    new_ultimo = msg_result["latest_date"]

                    if current_ultimo and msg_result["latest_date"]:
                        # Comparar datas (removendo timezone para comparacao)
                        try:
                            current_naive = current_ultimo.replace(tzinfo=None) if current_ultimo.tzinfo else current_ultimo
                            latest_naive = msg_result["latest_date"].replace(tzinfo=None) if msg_result["latest_date"].tzinfo else msg_result["latest_date"]
                            new_ultimo = msg_result["latest_date"] if latest_naive > current_naive else current_ultimo
                        except:
                            new_ultimo = msg_result["latest_date"] or current_ultimo
                    elif current_ultimo:
                        new_ultimo = current_ultimo

                    # Atualizar
                    cursor.execute("""
                        UPDATE contacts
                        SET total_interacoes = %s, ultimo_contato = %s
                        WHERE id = %s
                    """, (new_interactions, new_ultimo, contact_id))
                    conn.commit()

                    result["updated"] = True

            result["success"] = True

        except Exception as e:
            logger.error(f"Erro ao sincronizar contato {contact_id}: {e}")
            result["error"] = str(e)

        return result

    async def sync_all_contacts(
        self,
        account_id: int = None,
        months_back: int = 12,
        batch_size: int = 50
    ) -> Dict[str, Any]:
        """
        Sincroniza emails de todos os contatos.

        Args:
            account_id: ID da conta Gmail (None = todas)
            months_back: Meses para buscar
            batch_size: Tamanho do lote

        Returns:
            Estatisticas do sync
        """
        if self._sync_status["running"]:
            return {"error": "Sync já em execução", "status": self._sync_status}

        self._sync_status = {
            "running": True,
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
            "total_contacts": 0,
            "processed": 0,
            "updated": 0,
            "errors": 0,
            "last_error": None
        }

        try:
            # Buscar contas Gmail
            with get_db() as conn:
                cursor = conn.cursor()
                if account_id:
                    cursor.execute(
                        "SELECT * FROM google_accounts WHERE id = %s AND conectado = TRUE",
                        (account_id,)
                    )
                else:
                    cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE")
                accounts = cursor.fetchall()

            if not accounts:
                self._sync_status["running"] = False
                return {"error": "Nenhuma conta Gmail conectada"}

            # Buscar contatos com email
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, nome, emails
                    FROM contacts
                    WHERE emails IS NOT NULL AND emails::text != '[]'
                    ORDER BY id
                """)
                contacts = cursor.fetchall()

            self._sync_status["total_contacts"] = len(contacts)

            # Processar cada conta
            for account in accounts:
                account_dict = dict(account)
                account_email = account_dict["email"]

                access_token = await self.get_valid_token(account_dict)
                if not access_token:
                    self._sync_status["errors"] += 1
                    self._sync_status["last_error"] = f"Token inválido para {account_email}"
                    continue

                # Processar contatos em lotes
                for contact in contacts:
                    contact_dict = dict(contact)
                    contact_id = contact_dict["id"]

                    # Parse emails
                    emails_data = contact_dict.get("emails")
                    email_list = []
                    if isinstance(emails_data, str):
                        try:
                            email_list = json.loads(emails_data)
                        except:
                            email_list = [{"email": emails_data}]
                    elif isinstance(emails_data, list):
                        email_list = emails_data

                    # Processar cada email do contato
                    for email_obj in email_list[:3]:  # Max 3 emails
                        email = email_obj.get("email", "") if isinstance(email_obj, dict) else str(email_obj)
                        if not email or email == account_email:
                            continue

                        result = await self.sync_contact_emails(
                            contact_id=contact_id,
                            email=email.lower(),
                            access_token=access_token,
                            months_back=months_back
                        )

                        if result.get("error") == "token_expired":
                            # Tentar renovar token
                            access_token = await self.get_valid_token(account_dict)
                            if not access_token:
                                break

                        if result.get("updated"):
                            self._sync_status["updated"] += 1

                        # Rate limit
                        await asyncio.sleep(0.1)

                    self._sync_status["processed"] += 1

                # Atualizar timestamp de sync da conta
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE google_accounts SET ultima_sync = CURRENT_TIMESTAMP WHERE id = %s",
                        (account_dict["id"],)
                    )
                    conn.commit()

        except Exception as e:
            logger.error(f"Erro no sync: {e}")
            self._sync_status["errors"] += 1
            self._sync_status["last_error"] = str(e)

        finally:
            self._sync_status["running"] = False
            self._sync_status["completed_at"] = datetime.now().isoformat()

        return self._sync_status

    async def recalculate_circles_after_sync(self) -> Dict[str, Any]:
        """
        Recalcula circulos de todos os contatos apos sync.
        """
        stats = {"processed": 0, "changed": 0}

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Buscar contatos nao manuais
                cursor.execute("""
                    SELECT id, nome, tags, total_interacoes, ultimo_contato,
                           aniversario, linkedin, empresa, cargo, foto_url,
                           contexto, score, circulo, frequencia_ideal_dias
                    FROM contacts
                    WHERE circulo_manual IS NOT TRUE OR circulo_manual IS NULL
                """)
                contacts = cursor.fetchall()

                for row in contacts:
                    contact = dict(row)
                    circulo_anterior = contact.get("circulo") or 5

                    # Calcular novo circulo e health
                    circulo, score, reasons = calcular_score_circulo(contact)
                    health = calcular_health_score(contact, circulo)

                    # Atualizar
                    cursor.execute("""
                        UPDATE contacts
                        SET circulo = %s, health_score = %s, ultimo_calculo_circulo = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (circulo, health, contact["id"]))

                    stats["processed"] += 1
                    if circulo != circulo_anterior:
                        stats["changed"] += 1

                conn.commit()

        except Exception as e:
            logger.error(f"Erro ao recalcular circulos: {e}")
            stats["error"] = str(e)

        return stats

    def get_sync_status(self) -> Dict[str, Any]:
        """Retorna status atual do sync."""
        return self._sync_status.copy()


# Singleton instance
_gmail_sync_service = None


def get_gmail_sync_service() -> GmailSyncService:
    """Retorna instancia singleton do servico."""
    global _gmail_sync_service
    if _gmail_sync_service is None:
        _gmail_sync_service = GmailSyncService()
    return _gmail_sync_service
