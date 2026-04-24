"""
ConselhoOS RACI <-> INTEL Tasks Bidirectional Sync

Syncs RACI items from ConselhoOS into INTEL tasks, and
completed INTEL tasks back to ConselhoOS as done RACIs.

Author: INTEL
"""
import os
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any

import psycopg2
from psycopg2.extras import RealDictCursor

from database import get_db

logger = logging.getLogger(__name__)


class ConselhoOSRaciSyncService:
    """
    Bidirectional sync between ConselhoOS RACI items and INTEL tasks.

    - RACI -> Task: When a pending RACI exists without an intel_task_id
    - Task -> RACI: When an INTEL task with conselhoos_raci_id is completed
    """

    def __init__(self):
        self.conselhoos_url = os.getenv("CONSELHOOS_DATABASE_URL")

    def _get_conselhoos_conn(self):
        """Get connection to ConselhoOS database."""
        if not self.conselhoos_url:
            raise ValueError("CONSELHOOS_DATABASE_URL not configured")
        return psycopg2.connect(
            self.conselhoos_url,
            cursor_factory=RealDictCursor
        )

    # ==================== RACI -> INTEL Tasks ====================

    def _find_or_create_project(self, empresa_nome: str, empresa_id: str) -> int:
        """
        Find an INTEL project matching the empresa name, or create one.

        Returns the project_id.
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Try exact match first
            cursor.execute(
                "SELECT id FROM projects WHERE LOWER(nome) = LOWER(%s) LIMIT 1",
                (empresa_nome,)
            )
            row = cursor.fetchone()
            if row:
                return row["id"]

            # Try partial match
            cursor.execute(
                "SELECT id FROM projects WHERE LOWER(nome) LIKE LOWER(%s) LIMIT 1",
                (f"%{empresa_nome}%",)
            )
            row = cursor.fetchone()
            if row:
                return row["id"]

            # Get Drive folder from ConselhoOS
            drive_folder_id = None
            try:
                cos_conn = self._get_conselhoos_conn()
                cos_cur = cos_conn.cursor()
                cos_cur.execute("SELECT drive_folder_id FROM empresas WHERE id = %s::uuid", (empresa_id,))
                row = cos_cur.fetchone()
                if row:
                    drive_folder_id = row.get("drive_folder_id")
                cos_conn.close()
            except Exception:
                pass

            # Create new project
            cursor.execute("""
                INSERT INTO projects (nome, descricao, tipo, status, google_drive_folder_id)
                VALUES (%s, %s, 'conselho', 'ativo', %s)
                RETURNING id
            """, (
                empresa_nome,
                f"Conselho consultivo vinculado ao ConselhoOS",
                drive_folder_id,
            ))
            project_id = cursor.fetchone()["id"]
            conn.commit()
            logger.info(f"Created INTEL project '{empresa_nome}' (id={project_id}) for ConselhoOS empresa {empresa_id}")
            return project_id

    def _find_contact_by_name(self, name: str, empresa_id: str = None) -> Optional[int]:
        """
        Find an INTEL contact by name.
        First checks ConselhoOS pessoas table (most reliable), then falls back to INTEL contacts.

        Returns contact_id or None.
        """
        if not name:
            return None

        name_clean = name.strip()

        # Step 1: Check ConselhoOS pessoas table (has intel_contact_id)
        try:
            conselhoos_conn = self._get_conselhoos_conn()
            conselhoos_cur = conselhoos_conn.cursor()

            query = """
                SELECT intel_contact_id FROM pessoas
                WHERE intel_contact_id IS NOT NULL
                  AND (LOWER(nome) LIKE LOWER(%s) OR LOWER(nome) = LOWER(%s))
            """
            params = [f"%{name_clean}%", name_clean]

            if empresa_id:
                query += " AND empresa_id = %s::uuid"
                params.append(empresa_id)

            query += " LIMIT 1"
            conselhoos_cur.execute(query, params)
            row = conselhoos_cur.fetchone()
            conselhoos_conn.close()

            if row and row.get("intel_contact_id"):
                return row["intel_contact_id"]
        except Exception as e:
            logger.warning(f"Could not check pessoas table: {e}")

        # Step 2: Fallback to INTEL contacts
        with get_db() as conn:
            cursor = conn.cursor()

            # Exact match
            cursor.execute(
                "SELECT id FROM contacts WHERE LOWER(nome) = LOWER(%s) LIMIT 1",
                (name_clean,)
            )
            row = cursor.fetchone()
            if row:
                return row["id"]

            # Partial match (first + last name)
            parts = name_clean.split()
            if len(parts) >= 2:
                cursor.execute(
                    "SELECT id FROM contacts WHERE LOWER(nome) LIKE LOWER(%s) AND LOWER(nome) LIKE LOWER(%s) LIMIT 1",
                    (f"%{parts[0]}%", f"%{parts[-1]}%")
                )
                row = cursor.fetchone()
                if row:
                    return row["id"]

            return None

    def sync_raci_to_tasks(self, empresa_nome: str = None) -> Dict[str, Any]:
        """
        Sync pending RACI items from ConselhoOS to INTEL tasks.

        Args:
            empresa_nome: If provided, sync only for this empresa.
                          If None, sync all empresas.

        Returns:
            Summary of sync results.
        """
        results = {
            "created": 0,
            "skipped": 0,
            "errors": [],
            "empresas_processed": 0
        }

        try:
            with self._get_conselhoos_conn() as cos_conn:
                cos_cursor = cos_conn.cursor()

                # Build query based on filter
                query = """
                    SELECT
                        r.id,
                        r.area,
                        r.acao,
                        r.prazo,
                        r.status,
                        r.responsavel_r,
                        r.responsavel_a,
                        r.responsavel_c,
                        r.responsavel_i,
                        r.intel_task_id,
                        e.id as empresa_id,
                        e.nome as empresa_nome
                    FROM raci_itens r
                    JOIN empresas e ON e.id = r.empresa_id
                    WHERE r.status IN ('pendente', 'em_andamento')
                      AND r.intel_task_id IS NULL
                """
                params = []

                if empresa_nome:
                    query += " AND LOWER(e.nome) = LOWER(%s)"
                    params.append(empresa_nome)

                query += " ORDER BY r.prazo ASC"

                cos_cursor.execute(query, params if params else None)
                raci_items = [dict(row) for row in cos_cursor.fetchall()]

                if not raci_items:
                    logger.info("No pending RACI items to sync")
                    return results

                # Group by empresa to avoid creating duplicate projects
                empresas_seen = {}

                for raci in raci_items:
                    try:
                        emp_nome = raci["empresa_nome"]
                        emp_id = str(raci["empresa_id"])
                        raci_id = str(raci["id"])

                        # Check if task already exists in INTEL (idempotency)
                        with get_db() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "SELECT id FROM tasks WHERE conselhoos_raci_id = %s",
                                (raci_id,)
                            )
                            if cursor.fetchone():
                                # Task already exists, update ConselhoOS with the link
                                cursor.execute(
                                    "SELECT id FROM tasks WHERE conselhoos_raci_id = %s",
                                    (raci_id,)
                                )
                                existing = cursor.fetchone()
                                if existing:
                                    cos_cursor.execute(
                                        "UPDATE raci_itens SET intel_task_id = %s WHERE id = %s::uuid",
                                        (existing["id"], raci_id)
                                    )
                                    cos_conn.commit()
                                results["skipped"] += 1
                                continue

                        # Find or create project
                        if emp_nome not in empresas_seen:
                            empresas_seen[emp_nome] = self._find_or_create_project(emp_nome, emp_id)
                            results["empresas_processed"] += 1

                        project_id = empresas_seen[emp_nome]

                        # Find contact matching responsavel_r (uses ConselhoOS pessoas first)
                        responsavel = raci.get("responsavel_r", "")
                        # Handle multiple responsaveis (take first)
                        if "," in str(responsavel):
                            responsavel = responsavel.split(",")[0].strip()
                        contact_id = self._find_contact_by_name(responsavel, empresa_id=raci.get("empresa_id"))

                        # Build task title and description
                        area = raci.get("area", "Geral")
                        acao = raci.get("acao", "")
                        titulo = acao  # Clean title, area tracked in descricao
                        if len(titulo) > 500:
                            titulo = titulo[:497] + "..."

                        responsavel_r = raci.get("responsavel_r") or "N/A"
                        responsavel_a = raci.get("responsavel_a") or "N/A"
                        responsavel_c = raci.get("responsavel_c") or ""
                        responsavel_i = raci.get("responsavel_i") or ""

                        descricao_parts = [
                            f"RACI da reuniao de conselho.",
                            f"R: {responsavel_r}, A: {responsavel_a}"
                        ]
                        if responsavel_c:
                            descricao_parts.append(f"C: {responsavel_c}")
                        if responsavel_i:
                            descricao_parts.append(f"I: {responsavel_i}")
                        descricao_parts.append(f"\nEmpresa: {emp_nome}")

                        descricao = "\n".join(descricao_parts)

                        # Parse prazo
                        prazo = raci.get("prazo")
                        data_vencimento = None
                        if prazo:
                            if isinstance(prazo, str):
                                try:
                                    data_vencimento = datetime.strptime(prazo, "%Y-%m-%d")
                                except ValueError:
                                    data_vencimento = None
                            else:
                                data_vencimento = prazo

                        # Create task in INTEL
                        with get_db() as conn:
                            cursor = conn.cursor()
                            cursor.execute("""
                                INSERT INTO tasks (
                                    titulo, descricao, origem, project_id, contact_id,
                                    data_vencimento, prioridade, status,
                                    conselhoos_raci_id, sync_status
                                ) VALUES (
                                    %s, %s, 'conselhoos_raci', %s, %s,
                                    %s, %s, 'pending',
                                    %s, 'local_only'
                                )
                                RETURNING id
                            """, (
                                titulo, descricao, project_id, contact_id,
                                data_vencimento, 5,
                                raci_id
                            ))
                            task_id = cursor.fetchone()["id"]
                            conn.commit()

                        # Update ConselhoOS RACI with intel_task_id
                        cos_cursor.execute(
                            "UPDATE raci_itens SET intel_task_id = %s WHERE id = %s::uuid",
                            (task_id, raci_id)
                        )
                        cos_conn.commit()

                        results["created"] += 1
                        logger.info(
                            f"Created INTEL task {task_id} for RACI {raci_id} "
                            f"([{area}] {acao[:50]})"
                        )

                    except Exception as e:
                        error_msg = f"Error syncing RACI {raci.get('id')}: {str(e)}"
                        logger.error(error_msg)
                        results["errors"].append(error_msg)

        except Exception as e:
            error_msg = f"Error connecting to ConselhoOS: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        logger.info(
            f"RACI->Tasks sync complete: "
            f"created={results['created']}, skipped={results['skipped']}, "
            f"errors={len(results['errors'])}"
        )
        return results

    # ==================== INTEL Tasks -> RACI ====================

    def sync_task_status_to_raci(self) -> Dict[str, Any]:
        """
        Sync completed INTEL tasks back to ConselhoOS RACI items.

        Finds tasks with origem='conselhoos_raci' and status='completed',
        then marks the corresponding RACI as 'concluido'.

        Returns:
            Summary of sync results.
        """
        results = {
            "updated": 0,
            "errors": []
        }

        try:
            # Find completed tasks that came from ConselhoOS
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, conselhoos_raci_id
                    FROM tasks
                    WHERE origem = 'conselhoos_raci'
                      AND status = 'completed'
                      AND conselhoos_raci_id IS NOT NULL
                """)
                completed_tasks = [dict(row) for row in cursor.fetchall()]

            if not completed_tasks:
                logger.info("No completed RACI tasks to sync back")
                return results

            with self._get_conselhoos_conn() as cos_conn:
                cos_cursor = cos_conn.cursor()

                for task in completed_tasks:
                    try:
                        raci_id = task["conselhoos_raci_id"]

                        # Check current RACI status
                        cos_cursor.execute(
                            "SELECT status FROM raci_itens WHERE id = %s::uuid",
                            (raci_id,)
                        )
                        raci = cos_cursor.fetchone()
                        if not raci:
                            logger.warning(f"RACI {raci_id} not found in ConselhoOS")
                            continue

                        if raci["status"] == "concluido":
                            # Already done
                            continue

                        # Mark RACI as concluido
                        cos_cursor.execute("""
                            UPDATE raci_itens
                            SET status = 'concluido', updated_at = NOW()
                            WHERE id = %s::uuid
                        """, (raci_id,))
                        cos_conn.commit()

                        results["updated"] += 1
                        logger.info(
                            f"Marked RACI {raci_id} as concluido "
                            f"(from INTEL task {task['id']})"
                        )

                    except Exception as e:
                        error_msg = f"Error syncing task {task.get('id')} to RACI: {str(e)}"
                        logger.error(error_msg)
                        results["errors"].append(error_msg)

        except Exception as e:
            error_msg = f"Error in task->RACI sync: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        logger.info(
            f"Tasks->RACI sync complete: "
            f"updated={results['updated']}, errors={len(results['errors'])}"
        )
        return results

    # ==================== Full Sync ====================

    def sync_pessoas_to_project_members(self) -> Dict[str, Any]:
        """
        Sync ConselhoOS pessoas → INTEL project_members.
        For each empresa with pessoas, find/create the INTEL project
        and add pessoas as project_members (linked by intel_contact_id).
        """
        result = {"added": 0, "skipped": 0, "errors": []}

        try:
            conselhoos_conn = self._get_conselhoos_conn()
            conselhoos_cur = conselhoos_conn.cursor()

            # Get all pessoas with intel_contact_id
            conselhoos_cur.execute("""
                SELECT p.nome, p.email, p.papel, p.cargo, p.intel_contact_id,
                       e.id as empresa_id, e.nome as empresa_nome
                FROM pessoas p
                JOIN empresas e ON e.id = p.empresa_id
                WHERE p.intel_contact_id IS NOT NULL AND p.ativo = true
            """)
            pessoas = conselhoos_cur.fetchall()

            if not pessoas:
                logger.info("No pessoas with intel_contact_id found")
                return result

            with get_db() as intel_conn:
                intel_cur = intel_conn.cursor()

                # Group by empresa
                empresas_map = {}
                for p in pessoas:
                    emp_nome = p['empresa_nome']
                    if emp_nome not in empresas_map:
                        empresas_map[emp_nome] = {
                            'empresa_id': p['empresa_id'],
                            'pessoas': []
                        }
                    empresas_map[emp_nome]['pessoas'].append(p)

                for emp_nome, emp_data in empresas_map.items():
                    try:
                        project_id = self._find_or_create_project(emp_nome, str(emp_data['empresa_id']))

                        for pessoa in emp_data['pessoas']:
                            contact_id = pessoa['intel_contact_id']
                            papel = pessoa['papel'] or pessoa['cargo'] or ''

                            # Check if already a member
                            intel_cur.execute("""
                                SELECT id FROM project_members
                                WHERE project_id = %s AND contact_id = %s
                            """, (project_id, contact_id))

                            if intel_cur.fetchone():
                                result["skipped"] += 1
                                continue

                            intel_cur.execute("""
                                INSERT INTO project_members (project_id, contact_id, papel)
                                VALUES (%s, %s, %s)
                                ON CONFLICT (project_id, contact_id) DO NOTHING
                            """, (project_id, contact_id, papel))
                            result["added"] += 1
                            logger.info(f"Added {pessoa['nome']} to project {emp_nome}")

                    except Exception as e:
                        error_msg = f"Error syncing pessoas for {emp_nome}: {e}"
                        logger.error(error_msg)
                        result["errors"].append(error_msg)

            conselhoos_conn.close()

        except Exception as e:
            error_msg = f"Error in sync_pessoas_to_project_members: {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

        return result

    def full_sync(self) -> Dict[str, Any]:
        """
        Run full bidirectional sync.

        1. Sync Pessoas -> Project Members
        2. Sync RACI -> Tasks (all empresas)
        3. Sync completed Tasks -> RACI

        Returns:
            Combined results.
        """
        logger.info("Starting full ConselhoOS <-> INTEL sync")

        pessoas_results = self.sync_pessoas_to_project_members()
        raci_results = self.sync_raci_to_tasks()
        task_results = self.sync_task_status_to_raci()

        return {
            "pessoas_to_members": pessoas_results,
            "raci_to_tasks": raci_results,
            "tasks_to_raci": task_results,
            "synced_at": datetime.now().isoformat()
        }


# Singleton
_raci_sync_service = None


def get_raci_sync_service() -> ConselhoOSRaciSyncService:
    """Get singleton instance."""
    global _raci_sync_service
    if _raci_sync_service is None:
        _raci_sync_service = ConselhoOSRaciSyncService()
    return _raci_sync_service
