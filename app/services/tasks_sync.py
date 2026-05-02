"""
Tasks Sync Service
Sync bidirecional entre tasks locais e Google Tasks

Autor: INTEL
Data: 2026-03-30
"""
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from database import get_db
from integrations.google_tasks import get_tasks_integration
from integrations.gmail import GmailIntegration

logger = logging.getLogger(__name__)


class TasksSyncService:
    """
    Servico de sincronizacao bidirecional de tasks.

    Fluxo:
    1. Tasks criadas localmente -> push para Google Tasks
    2. Tasks do Google Tasks -> pull para local
    3. Atualizacoes em ambos os lados -> sync com deteccao de conflitos
    """

    def __init__(self):
        self.tasks_api = get_tasks_integration()
        self.gmail = GmailIntegration()

    async def get_access_token(self) -> Optional[str]:
        """Obtem access token valido da conta Google conectada."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE LIMIT 1")
            account = cursor.fetchone()

        if not account:
            logger.warning("Nenhuma conta Google conectada")
            return None

        tokens = await self.gmail.refresh_access_token(account["refresh_token"])
        if "error" in tokens:
            logger.error(f"Erro ao obter token: {tokens['error']}")
            return None

        return tokens.get("access_token")

    # ==================== PUSH: Local -> Google ====================

    async def push_task_to_google(self, task_id: int) -> Dict[str, Any]:
        """
        Envia task local para Google Tasks.
        Cria nova ou atualiza existente.
        """
        access_token = await self.get_access_token()
        if not access_token:
            return {"error": "Token nao disponivel"}

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            task = cursor.fetchone()

        if not task:
            return {"error": "Task nao encontrada"}

        task = dict(task)

        # Preparar dados para Google
        due_datetime = None
        if task.get("data_vencimento"):
            due_datetime = task["data_vencimento"]

        # Determinar status do Google
        google_status = "needsAction"
        if task.get("status") == "completed":
            google_status = "completed"

        tasklist_id = task.get("google_tasklist_id") or "@default"

        if task.get("google_task_id"):
            # Update existing
            result = await self.tasks_api.update_task(
                access_token=access_token,
                task_id=task["google_task_id"],
                title=task["titulo"],
                notes=task.get("descricao"),
                due=due_datetime,
                status=google_status,
                tasklist_id=tasklist_id
            )
        else:
            # Create new
            result = await self.tasks_api.create_task(
                access_token=access_token,
                title=task["titulo"],
                notes=task.get("descricao"),
                due=due_datetime,
                tasklist_id=tasklist_id
            )

        if "error" in result:
            return result

        # Atualizar task local com ID do Google
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tasks
                SET google_task_id = %s,
                    google_tasklist_id = %s,
                    last_synced_at = CURRENT_TIMESTAMP,
                    sync_status = 'synced',
                    etag = %s
                WHERE id = %s
            """, (
                result.get("id"),
                tasklist_id,
                result.get("etag"),
                task_id
            ))
            conn.commit()

        logger.info(f"Task {task_id} sincronizada com Google: {result.get('id')}")
        return {"status": "success", "google_task": result}

    async def push_all_pending(self) -> Dict[str, Any]:
        """Envia todas as tasks pendentes de sync para Google."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM tasks
                WHERE sync_status IN ('local_only', 'pending_push')
                AND status != 'completed'
            """)
            pending = cursor.fetchall()

        results = {"pushed": 0, "errors": 0, "error_samples": []}

        for row in pending:
            result = await self.push_task_to_google(row["id"])
            if "error" in result:
                results["errors"] += 1
                err_str = str(result.get("error"))[:200]
                logger.warning(f"push fail task_id={row['id']}: {err_str}")
                if len(results["error_samples"]) < 5:
                    results["error_samples"].append({"task_id": row["id"], "error": err_str})
            else:
                results["pushed"] += 1

        return results

    # ==================== PULL: Google -> Local ====================

    async def pull_tasks_from_google(self, tasklist_id: str = "@default") -> Dict[str, Any]:
        """
        Busca tasks do Google e sincroniza com local.
        """
        access_token = await self.get_access_token()
        if not access_token:
            return {"error": "Token nao disponivel"}

        google_tasks = await self.tasks_api.list_tasks(
            access_token=access_token,
            tasklist_id=tasklist_id,
            show_completed=True
        )

        results = {"created": 0, "updated": 0, "skipped": 0}

        for gtask in google_tasks:
            result = await self._sync_google_task_to_local(gtask, tasklist_id)
            results[result] += 1

        return results

    async def _sync_google_task_to_local(
        self,
        google_task: Dict[str, Any],
        tasklist_id: str
    ) -> str:
        """
        Sincroniza uma task do Google para local.
        Retorna: 'created', 'updated', ou 'skipped'
        """
        google_id = google_task.get("id")

        with get_db() as conn:
            cursor = conn.cursor()

            # Verificar se ja existe localmente
            cursor.execute(
                "SELECT * FROM tasks WHERE google_task_id = %s",
                (google_id,)
            )
            local_task = cursor.fetchone()

            # Parse due date
            due_date = None
            if google_task.get("due"):
                try:
                    due_date = datetime.fromisoformat(
                        google_task["due"].replace("Z", "+00:00")
                    )
                except:
                    pass

            # Determinar status local
            local_status = "pending"
            if google_task.get("status") == "completed":
                local_status = "completed"

            if local_task:
                # Update existing
                local_task = dict(local_task)

                # Verificar se Google tem versao mais recente
                google_updated = google_task.get("updated")
                local_synced = local_task.get("last_synced_at")

                if local_synced and google_updated:
                    # Se local foi modificado depois do sync, pode haver conflito
                    # Por enquanto, Google vence
                    pass

                cursor.execute("""
                    UPDATE tasks
                    SET titulo = %s,
                        descricao = %s,
                        data_vencimento = %s,
                        status = %s,
                        last_synced_at = CURRENT_TIMESTAMP,
                        sync_status = 'synced',
                        etag = %s
                    WHERE google_task_id = %s
                """, (
                    google_task.get("title"),
                    google_task.get("notes"),
                    due_date,
                    local_status,
                    google_task.get("etag"),
                    google_id
                ))
                conn.commit()
                return "updated"

            else:
                # Create new local task
                cursor.execute("""
                    INSERT INTO tasks (
                        titulo, descricao, data_vencimento, status,
                        origem, google_task_id, google_tasklist_id,
                        last_synced_at, sync_status, etag
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, 'synced', %s)
                    RETURNING id
                """, (
                    google_task.get("title"),
                    google_task.get("notes"),
                    due_date,
                    local_status,
                    "google_tasks",
                    google_id,
                    tasklist_id,
                    google_task.get("etag")
                ))
                conn.commit()
                return "created"

    # ==================== FULL SYNC ====================

    async def full_sync(self) -> Dict[str, Any]:
        """
        Sincronizacao completa bidirecional.

        1. Push tasks locais pendentes para Google
        2. Pull tasks do Google para local
        """
        results = {
            "push": {"pushed": 0, "errors": 0},
            "pull": {"created": 0, "updated": 0, "skipped": 0}
        }

        access_token = await self.get_access_token()
        if not access_token:
            return {"error": "Token nao disponivel"}

        # 1. Push local -> Google
        push_result = await self.push_all_pending()
        results["push"] = push_result

        # 2. Pull Google -> Local (todas as listas)
        task_lists = await self.tasks_api.list_task_lists(access_token)
        if not task_lists:
            task_lists = [{"id": "@default"}]

        for tl in task_lists:
            pull_result = await self.pull_tasks_from_google(tl.get("id", "@default"))
            if "error" not in pull_result:
                results["pull"]["created"] += pull_result.get("created", 0)
                results["pull"]["updated"] += pull_result.get("updated", 0)
                results["pull"]["skipped"] += pull_result.get("skipped", 0)

        logger.info(f"Full sync completed: {results}")
        return results

    # ==================== TASK OPERATIONS ====================

    async def create_task(
        self,
        titulo: str,
        descricao: str = None,
        data_vencimento: datetime = None,
        prioridade: int = 5,
        contact_id: int = None,
        project_id: int = None,
        sync_to_google: bool = True,
        tasklist_id: str = "@default"
    ) -> Dict[str, Any]:
        """
        Cria task localmente e opcionalmente sincroniza com Google.
        """
        with get_db() as conn:
            cursor = conn.cursor()

            sync_status = "pending_push" if sync_to_google else "local_only"

            cursor.execute("""
                INSERT INTO tasks (
                    titulo, descricao, data_vencimento, prioridade,
                    contact_id, project_id, origem,
                    google_tasklist_id, sync_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                titulo, descricao, data_vencimento, prioridade,
                contact_id, project_id, "intel",
                tasklist_id, sync_status
            ))

            task_id = cursor.fetchone()["id"]
            conn.commit()

        # Sync to Google if requested
        if sync_to_google:
            await self.push_task_to_google(task_id)

        return {"id": task_id, "status": "created"}

    async def update_task(
        self,
        task_id: int,
        titulo: str = None,
        descricao: str = None,
        data_vencimento: datetime = None,
        status: str = None,
        prioridade: int = None,
        sync_to_google: bool = True
    ) -> Dict[str, Any]:
        """
        Atualiza task localmente e opcionalmente sincroniza com Google.
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Build update query dynamically
            updates = []
            params = []

            if titulo is not None:
                updates.append("titulo = %s")
                params.append(titulo)
            if descricao is not None:
                updates.append("descricao = %s")
                params.append(descricao)
            if data_vencimento is not None:
                updates.append("data_vencimento = %s")
                params.append(data_vencimento)
            if status is not None:
                updates.append("status = %s")
                params.append(status)
                if status == "completed":
                    updates.append("data_conclusao = CURRENT_TIMESTAMP")
            if prioridade is not None:
                updates.append("prioridade = %s")
                params.append(prioridade)

            if sync_to_google:
                updates.append("sync_status = 'pending_push'")

            if not updates:
                return {"error": "Nenhum campo para atualizar"}

            params.append(task_id)

            cursor.execute(f"""
                UPDATE tasks
                SET {', '.join(updates)}
                WHERE id = %s
                RETURNING *
            """, params)

            task = cursor.fetchone()
            conn.commit()

        if not task:
            return {"error": "Task nao encontrada"}

        # Sync to Google if requested and has Google ID
        if sync_to_google and task.get("google_task_id"):
            await self.push_task_to_google(task_id)

        return {"status": "updated", "task": dict(task)}

    async def complete_task(self, task_id: int, sync_to_google: bool = True) -> Dict[str, Any]:
        """Marca task como completa."""
        return await self.update_task(
            task_id=task_id,
            status="completed",
            sync_to_google=sync_to_google
        )

    async def delete_task(self, task_id: int, delete_from_google: bool = True) -> Dict[str, Any]:
        """
        Deleta task localmente e opcionalmente do Google.
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            task = cursor.fetchone()

        if not task:
            return {"error": "Task nao encontrada"}

        task = dict(task)

        # Delete from Google if requested
        if delete_from_google and task.get("google_task_id"):
            access_token = await self.get_access_token()
            if access_token:
                await self.tasks_api.delete_task(
                    access_token=access_token,
                    task_id=task["google_task_id"],
                    tasklist_id=task.get("google_tasklist_id") or "@default"
                )

        # Delete locally
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            conn.commit()

        return {"status": "deleted"}

    # ==================== QUERY ====================

    def get_tasks(
        self,
        status: str = None,
        contact_id: int = None,
        project_id: int = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Lista tasks locais com filtros."""
        with get_db() as conn:
            cursor = conn.cursor()

            where_clauses = []
            params = []

            if status:
                where_clauses.append("status = %s")
                params.append(status)
            if contact_id:
                where_clauses.append("contact_id = %s")
                params.append(contact_id)
            if project_id:
                where_clauses.append("project_id = %s")
                params.append(project_id)

            where_sql = ""
            if where_clauses:
                where_sql = "WHERE " + " AND ".join(where_clauses)

            params.append(limit)

            cursor.execute(f"""
                SELECT t.*, c.nome as contact_name, p.nome as project_name
                FROM tasks t
                LEFT JOIN contacts c ON t.contact_id = c.id
                LEFT JOIN projects p ON t.project_id = p.id
                {where_sql}
                ORDER BY
                    CASE WHEN t.status = 'pending' THEN 0 ELSE 1 END,
                    t.data_vencimento ASC NULLS LAST,
                    t.prioridade ASC
                LIMIT %s
            """, params)

            return [dict(row) for row in cursor.fetchall()]


# Singleton
_tasks_sync_service = None


def get_tasks_sync_service() -> TasksSyncService:
    """Retorna instancia singleton do servico."""
    global _tasks_sync_service
    if _tasks_sync_service is None:
        _tasks_sync_service = TasksSyncService()
    return _tasks_sync_service
