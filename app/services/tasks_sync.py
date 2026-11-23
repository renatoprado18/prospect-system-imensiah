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

# Estados que existem SÓ no INTEL. O Google Tasks tem apenas needsAction|completed,
# então qualquer um destes volta do Google como needsAction — e o pull traduzia isso
# como 'pending', REBAIXANDO o estado local. Foi o que apagou o parqueio de 4 tasks
# em 30/07 às 05:00:23 (o daily-sync rodou 05:00:02): `on_hold` deixou de existir e
# elas reapareceram como vencidas, desfazendo a convenção ratificada em 29/07
# (memória feedback_aguardar_terceiro_on_hold).
#
# `completed` NÃO entra aqui de propósito: desmarcar a task no Google tem que poder
# reabri-la no INTEL. O que não pode é o Google, que não conhece o conceito, decidir
# que uma task parqueada/em-andamento/cancelada voltou a ser pendente.
LOCAL_ONLY_STATUSES = frozenset({"on_hold", "in_progress", "cancelled", "delegated"})


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
            # `LIMIT 1` sem ORDER BY escolhia a conta pela ordem do heap: caía na
            # profissional por acaso, e um VACUUM FULL poderia inverter isso calado.
            # O Google Tasks e' profissional por decisao, entao a decisao vai escrita.
            cursor.execute(
                "SELECT * FROM google_accounts "
                "WHERE conectado = TRUE AND tipo = 'professional' "
                "ORDER BY id LIMIT 1")
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

    # O Google Tasks recusa `notes` acima de 8192 caracteres com um 400
    # "Request contains an invalid argument." que nao diz qual argumento e nem
    # cita o tamanho. Limite medido em 22/11/26 contra a API, com sonda
    # descartavel: 8192 passa, 8193 nao. Antes disso o push mandava a descricao
    # inteira e 5 tasks (de 9.950 a 18.553 caracteres) falhavam a CADA run, duas
    # vezes por dia desde que foram criadas — e o cron se declarava `success`.
    GOOGLE_NOTES_MAX = 8192

    @classmethod
    def _notes_para_google(cls, descricao: Optional[str], task_id: int) -> Optional[str]:
        """Corta `notes` no limite do Google, dizendo onde esta a integra.

        Corta no ultimo fim de linha antes do teto pra nao partir palavra no
        meio, e o rodape entra DENTRO do orcamento (somar depois estouraria o
        limite outra vez, e o 400 nao diz que foi isso).
        """
        if not descricao or len(descricao) <= cls.GOOGLE_NOTES_MAX:
            return descricao

        rodape = f"\n\n[...] cortado no limite do Google Tasks — integra na task #{task_id} do INTEL"
        teto = cls.GOOGLE_NOTES_MAX - len(rodape)
        corte = descricao[:teto]
        ultima_quebra = corte.rfind("\n")
        if ultima_quebra > teto // 2:
            corte = corte[:ultima_quebra]
        return corte + rodape

    @staticmethod
    def _descricao_a_gravar(local_task: Optional[Dict], notes_do_google):
        """Qual descricao o pull grava: a do Google, ou a local se for maior.

        🔴 O DANO QUE ISTO IMPEDE (22/11/26), causado pelo conserto do push no
        mesmo dia. O push passou a cortar `notes` em 8192 (limite da API), e o
        `full_sync` roda push e PULL na mesma execucao: o pull trouxe o texto ja
        cortado de volta e gravou sobre o original. Quatro tasks perderam de
        1.900 a 3.700 caracteres -- #999932 (9.950→8.001), #1000432
        (10.140→8.014), #1000600 (10.526→8.099), #1000606 (11.762→8.036) -- e o
        rodape que prometia "integra na task #N do INTEL" passou a apontar pro
        proprio resumo. Recuperadas de um branch PITR do Neon.

        O INTEL e a fonte da descricao; o Google e superficie de leitura com
        teto menor. Encolhimento vindo de fora e perda, nao edicao
        ([[feedback_estado_local_morre_no_sync_externo]]).

        O critario e o TAMANHO, nao a marca do rodape: qualquer caminho que
        devolva texto truncado cai aqui, nao so o nosso. Edicao real no celular
        que encurte de verdade tambem e barrada -- e a troca certa, porque
        barrar uma edicao manual custa um reenvio e aceitar o truncamento apaga
        o original.

        A decisao fica em Python e nao num CASE no SQL por um motivo pratico: o
        UPDATE e lido por posicao de parametro nos testes do `on_hold`, e
        acrescentar placeholders ali desloca silenciosamente o indice do status.
        """
        local = (local_task or {}).get("descricao")
        if notes_do_google is None or notes_do_google == "":
            return local
        if local and len(local) > len(notes_do_google):
            return local
        return notes_do_google

    @staticmethod
    def _e_404(erro: Any) -> bool:
        """O erro do Google e um 404 (entidade inexistente)?

        A integracao devolve `response.text` cru, nao o status, entao a leitura
        e feita sobre o corpo. Exige as DUAS marcas (codigo e motivo) pra nao
        confundir com um 400 que por acaso mencione 'not found' no texto.
        """
        if not isinstance(erro, str):
            return False
        return '"code": 404' in erro and "not found" in erro.lower()

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
        notes = self._notes_para_google(task.get("descricao"), task_id)

        if task.get("google_task_id"):
            # Update existing
            result = await self.tasks_api.update_task(
                access_token=access_token,
                task_id=task["google_task_id"],
                title=task["titulo"],
                notes=notes,
                due=due_datetime,
                status=google_status,
                tasklist_id=tasklist_id
            )
        else:
            # Create new
            result = await self.tasks_api.create_task(
                access_token=access_token,
                title=task["titulo"],
                notes=notes,
                due=due_datetime,
                tasklist_id=tasklist_id
            )

        if "error" in result:
            # 404 no update = o `google_task_id` aponta pra task que ja nao
            # existe la (apagada no Google). Insistir nela e tentar pra sempre:
            # as #64 e #84 falharam 2x/dia de abril a 22/11/26. Soltar o id
            # orfao resolve pelas regras que ja existem — `completed` sem id sai
            # da fila do `push_all_pending` (o create nao aceita status), e
            # `pending` cai no create no run seguinte, recriando a task no
            # Google. NAO recriar aqui: ressuscitaria o que foi apagado de
            # proposito, e quem apaga no Google e o Renato.
            if task.get("google_task_id") and self._e_404(result["error"]):
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE tasks
                        SET google_task_id = NULL,
                            etag = NULL,
                            last_synced_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (task_id,))
                    conn.commit()
                logger.warning(
                    f"task_id={task_id}: google_task_id orfao (404 no update) — "
                    f"id soltado; {'sai da fila (completed)' if google_status == 'completed' else 'sera recriada no proximo run'}"
                )
                return {"error": "google_task_id_orfao", "orfao_resolvido": True}
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
            # Conclusoes PRECISAM subir pro Google (update_task manda
            # status='completed'). Antes eram excluidas aqui: o Google seguia
            # vendo needsAction e o pull seguinte reabria a task localmente,
            # deixando data_conclusao para tras (20 tasks-fantasma, 09/07).
            # Ressalva: completed sem google_task_id iria pro create_task, que
            # nao aceita status — nasceria aberta no Google. Essas ficam de fora.
            cursor.execute("""
                SELECT id FROM tasks
                WHERE sync_status IN ('local_only', 'pending_push')
                AND (status != 'completed' OR google_task_id IS NOT NULL)
            """)
            pending = cursor.fetchall()

        # `tentadas` existe porque `pushed + errors` nao fecha a conta desde que
        # o orfao passou a ser resolvido em vez de contado como falha: sem o
        # total, 0 erros e 0 pushes e indistinguivel de fila vazia.
        results = {"pushed": 0, "errors": 0, "orfaos_resolvidos": 0,
                   "tentadas": len(pending), "error_samples": []}

        for row in pending:
            result = await self.push_task_to_google(row["id"])
            if result.get("orfao_resolvido"):
                results["orfaos_resolvidos"] += 1
            elif "error" in result:
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
            local_task = dict(local_task) if local_task else None

            # Parse due date
            due_date = None
            if google_task.get("due"):
                try:
                    due_date = datetime.fromisoformat(
                        google_task["due"].replace("Z", "+00:00")
                    )
                except:
                    pass

            # Determinar status local. Ver LOCAL_ONLY_STATUSES no topo: o Google não
            # representa on_hold/in_progress/cancelled/delegated, então needsAction
            # vindo dele NÃO é evidência de que a task voltou a ser pendente.
            local_status = "pending"
            if google_task.get("status") == "completed":
                local_status = "completed"
            elif local_task and local_task.get("status") in LOCAL_ONLY_STATUSES:
                local_status = local_task["status"]

            if local_task:
                # Update existing
                # Conflict resolution: se local foi modificado depois do ultimo sync
                # (ex: user editou via UI, ou cron interno fechou task), local vence —
                # marca pending_push pra reconciliar Google na proxima push.
                # Antes desse fix, Google vencia sempre e revertia edits locais
                # (caso real 19/05: tasks 417/451 reabertas + 403 movida de volta).
                local_atualizado = local_task.get("atualizado_em")
                local_synced = local_task.get("last_synced_at")
                if local_atualizado and local_synced and local_atualizado > local_synced:
                    # Local tem mudanca pendente — nao sobrescreve. So sinaliza pending_push.
                    cursor.execute("""
                        UPDATE tasks SET sync_status = 'pending_push'
                        WHERE google_task_id = %s AND sync_status != 'pending_push'
                    """, (google_id,))
                    conn.commit()
                    return "skipped"

                # data_conclusao acompanha o status que estamos gravando: se o
                # pull reabre a task, o fossil da conclusao anterior tem que
                # sair junto. Sem isso, status='pending' convive com
                # data_conclusao preenchida e a task vira fantasma no briefing.
                cursor.execute("""
                    UPDATE tasks
                    SET titulo = %s,
                        descricao = %s,
                        data_vencimento = %s,
                        status = %s,
                        data_conclusao = CASE
                            WHEN %s = 'completed'
                            THEN COALESCE(data_conclusao, CURRENT_TIMESTAMP)
                            ELSE NULL
                        END,
                        last_synced_at = CURRENT_TIMESTAMP,
                        sync_status = 'synced',
                        etag = %s
                    WHERE google_task_id = %s
                """, (
                    google_task.get("title"),
                    self._descricao_a_gravar(local_task, google_task.get("notes")),
                    due_date,
                    local_status,
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
                        data_conclusao,
                        origem, google_task_id, google_tasklist_id,
                        last_synced_at, sync_status, etag
                    ) VALUES (
                        %s, %s, %s, %s,
                        CASE WHEN %s = 'completed' THEN CURRENT_TIMESTAMP END,
                        %s, %s, %s, CURRENT_TIMESTAMP, 'synced', %s
                    )
                    RETURNING id
                """, (
                    google_task.get("title"),
                    google_task.get("notes"),
                    due_date,
                    local_status,
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
