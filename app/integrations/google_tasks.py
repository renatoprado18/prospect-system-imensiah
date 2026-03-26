"""
Google Tasks Integration for INTEL
Sync bi-direcional de tarefas com Google Tasks

Autor: INTEL
Data: 2026-03-26
"""
import os
import httpx
from datetime import datetime
from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)


class GoogleTasksIntegration:
    """
    Integration with Google Tasks API
    Uses same OAuth credentials as Gmail
    """

    TASKS_API_BASE = "https://tasks.googleapis.com/tasks/v1"

    # Scope necessario (adicionar ao OAuth existente)
    SCOPE = "https://www.googleapis.com/auth/tasks"

    def __init__(self):
        self.client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")

    async def list_task_lists(self, access_token: str) -> List[Dict[str, Any]]:
        """
        Lista todas as listas de tarefas.
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.TASKS_API_BASE}/users/@me/lists",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30.0
                )

                if response.status_code == 401:
                    return []
                elif response.status_code != 200:
                    return []

                data = response.json()
                return [
                    {
                        "id": tl.get("id"),
                        "title": tl.get("title"),
                        "updated": tl.get("updated")
                    }
                    for tl in data.get("items", [])
                ]

            except Exception as e:
                logger.error(f"Erro ao listar task lists: {e}")
                return []

    async def list_tasks(
        self,
        access_token: str,
        tasklist_id: str = "@default",
        show_completed: bool = False,
        max_results: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Lista tarefas de uma lista.
        """
        params = {
            "maxResults": max_results,
            "showCompleted": str(show_completed).lower()
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.TASKS_API_BASE}/lists/{tasklist_id}/tasks",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                    timeout=30.0
                )

                if response.status_code == 401:
                    return []
                elif response.status_code != 200:
                    return []

                data = response.json()
                return [self._format_task(t) for t in data.get("items", [])]

            except Exception as e:
                logger.error(f"Erro ao listar tasks: {e}")
                return []

    async def get_task(
        self,
        access_token: str,
        task_id: str,
        tasklist_id: str = "@default"
    ) -> Optional[Dict[str, Any]]:
        """
        Busca uma tarefa especifica.
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.TASKS_API_BASE}/lists/{tasklist_id}/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30.0
                )

                if response.status_code == 401:
                    return {"error": "token_expired"}
                elif response.status_code == 404:
                    return None
                elif response.status_code != 200:
                    return {"error": response.text}

                return self._format_task(response.json())

            except Exception as e:
                logger.error(f"Erro ao buscar task: {e}")
                return {"error": str(e)}

    async def create_task(
        self,
        access_token: str,
        title: str,
        notes: str = None,
        due: datetime = None,
        tasklist_id: str = "@default"
    ) -> Dict[str, Any]:
        """
        Cria nova tarefa.
        """
        task_data = {"title": title}

        if notes:
            task_data["notes"] = notes
        if due:
            task_data["due"] = due.strftime("%Y-%m-%dT00:00:00.000Z")

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.TASKS_API_BASE}/lists/{tasklist_id}/tasks",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    json=task_data,
                    timeout=30.0
                )

                if response.status_code == 401:
                    return {"error": "token_expired"}
                elif response.status_code not in [200, 201]:
                    return {"error": response.text}

                return self._format_task(response.json())

            except Exception as e:
                logger.error(f"Erro ao criar task: {e}")
                return {"error": str(e)}

    async def update_task(
        self,
        access_token: str,
        task_id: str,
        title: str = None,
        notes: str = None,
        due: datetime = None,
        status: str = None,
        tasklist_id: str = "@default"
    ) -> Dict[str, Any]:
        """
        Atualiza uma tarefa existente.
        """
        task_data = {}

        if title:
            task_data["title"] = title
        if notes is not None:
            task_data["notes"] = notes
        if due:
            task_data["due"] = due.strftime("%Y-%m-%dT00:00:00.000Z")
        if status:
            task_data["status"] = status

        async with httpx.AsyncClient() as client:
            try:
                response = await client.patch(
                    f"{self.TASKS_API_BASE}/lists/{tasklist_id}/tasks/{task_id}",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    json=task_data,
                    timeout=30.0
                )

                if response.status_code == 401:
                    return {"error": "token_expired"}
                elif response.status_code != 200:
                    return {"error": response.text}

                return self._format_task(response.json())

            except Exception as e:
                logger.error(f"Erro ao atualizar task: {e}")
                return {"error": str(e)}

    async def complete_task(
        self,
        access_token: str,
        task_id: str,
        tasklist_id: str = "@default"
    ) -> Dict[str, Any]:
        """
        Marca tarefa como concluida.
        """
        return await self.update_task(
            access_token=access_token,
            task_id=task_id,
            status="completed",
            tasklist_id=tasklist_id
        )

    async def delete_task(
        self,
        access_token: str,
        task_id: str,
        tasklist_id: str = "@default"
    ) -> bool:
        """
        Deleta uma tarefa.
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.delete(
                    f"{self.TASKS_API_BASE}/lists/{tasklist_id}/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30.0
                )

                return response.status_code == 204

            except Exception as e:
                logger.error(f"Erro ao deletar task: {e}")
                return False

    def _format_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Formata tarefa para exibicao na UI."""
        return {
            "id": task.get("id"),
            "title": task.get("title"),
            "notes": task.get("notes"),
            "status": task.get("status"),
            "due": task.get("due"),
            "completed": task.get("completed"),
            "updated": task.get("updated"),
            "position": task.get("position"),
            "parent": task.get("parent"),
            "links": task.get("links", [])
        }


_tasks_integration = None


def get_tasks_integration() -> GoogleTasksIntegration:
    """Retorna instancia singleton."""
    global _tasks_integration
    if _tasks_integration is None:
        _tasks_integration = GoogleTasksIntegration()
    return _tasks_integration
