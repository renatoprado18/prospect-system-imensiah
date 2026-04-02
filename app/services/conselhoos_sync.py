"""
ConselhoOS Sync Service
Sincroniza dados entre INTEL e ConselhoOS (app separado).

ConselhoOS usa Neon PostgreSQL com Drizzle ORM.
Conexao direta ao banco do ConselhoOS para leitura.

Autor: INTEL
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

import psycopg2
from psycopg2.extras import RealDictCursor

from database import get_db

logger = logging.getLogger(__name__)


class ConselhoOSSyncService:
    """
    Service para sincronizar dados entre INTEL e ConselhoOS.

    ConselhoOS tem seu proprio banco Neon. Conectamos diretamente
    para leitura e exibimos dados no INTEL.
    """

    def __init__(self):
        # ConselhoOS database URL (separada do INTEL)
        self.conselhoos_url = os.getenv("CONSELHOOS_DATABASE_URL")
        self._cached_empresas = None
        self._cache_time = None

    def _get_conselhoos_conn(self):
        """Get connection to ConselhoOS database."""
        if not self.conselhoos_url:
            raise ValueError("CONSELHOOS_DATABASE_URL not configured")

        return psycopg2.connect(
            self.conselhoos_url,
            cursor_factory=RealDictCursor
        )

    def get_empresas(self, force_refresh: bool = False) -> List[Dict]:
        """
        Busca empresas do ConselhoOS.

        Returns:
            List of empresas with basic info
        """
        # Cache for 5 minutes
        if not force_refresh and self._cached_empresas and self._cache_time:
            if datetime.now() - self._cache_time < timedelta(minutes=5):
                return self._cached_empresas

        try:
            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        id,
                        nome,
                        setor,
                        descricao,
                        cor_hex,
                        logo_url,
                        created_at
                    FROM empresas
                    WHERE user_id = %s
                    ORDER BY nome
                """, (os.getenv("CONSELHOOS_USER_ID", ""),))

                empresas = [dict(row) for row in cursor.fetchall()]

                self._cached_empresas = empresas
                self._cache_time = datetime.now()

                return empresas

        except Exception as e:
            logger.error(f"Erro ao buscar empresas ConselhoOS: {e}")
            return []

    def get_proximas_reunioes(self, limit: int = 10) -> List[Dict]:
        """
        Busca proximas reunioes de todas as empresas.

        Returns:
            List of upcoming meetings
        """
        try:
            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        r.id,
                        r.titulo,
                        r.data,
                        r.status,
                        r.calendar_link,
                        e.id as empresa_id,
                        e.nome as empresa_nome,
                        e.cor_hex
                    FROM reunioes r
                    JOIN empresas e ON e.id = r.empresa_id
                    WHERE r.data >= NOW()
                    AND r.status != 'cancelada'
                    ORDER BY r.data ASC
                    LIMIT %s
                """, (limit,))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Erro ao buscar reunioes ConselhoOS: {e}")
            return []

    def get_raci_pendentes(self, limit: int = 20) -> List[Dict]:
        """
        Busca tarefas RACI pendentes/atrasadas.

        Returns:
            List of pending RACI items
        """
        try:
            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        r.id,
                        r.area,
                        r.acao,
                        r.prazo,
                        r.status,
                        r.responsavel_r,
                        e.id as empresa_id,
                        e.nome as empresa_nome,
                        e.cor_hex,
                        CASE
                            WHEN r.prazo < CURRENT_DATE THEN 'atrasado'
                            WHEN r.prazo <= CURRENT_DATE + INTERVAL '3 days' THEN 'urgente'
                            ELSE 'normal'
                        END as urgencia
                    FROM raci_itens r
                    JOIN empresas e ON e.id = r.empresa_id
                    WHERE r.status IN ('pendente', 'em_andamento')
                    ORDER BY r.prazo ASC
                    LIMIT %s
                """, (limit,))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Erro ao buscar RACI ConselhoOS: {e}")
            return []

    def get_empresa_detail(self, empresa_id: str) -> Optional[Dict]:
        """
        Busca detalhes de uma empresa.
        """
        try:
            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT *
                    FROM empresas
                    WHERE id = %s
                """, (empresa_id,))

                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None

        except Exception as e:
            logger.error(f"Erro ao buscar empresa {empresa_id}: {e}")
            return None

    def link_contact_to_empresa(
        self,
        contact_id: int,
        empresa_id: str,
        empresa_nome: str,
        role: str = "stakeholder"
    ) -> Dict:
        """
        Vincula um contato INTEL a uma empresa do ConselhoOS.

        Args:
            contact_id: ID do contato no INTEL
            empresa_id: UUID da empresa no ConselhoOS
            empresa_nome: Nome da empresa
            role: Papel (membro_conselho, executivo, stakeholder)

        Returns:
            Result dict
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Check if link exists
                cursor.execute("""
                    SELECT id FROM conselhoos_links
                    WHERE contact_id = %s AND conselhoos_empresa_id = %s
                """, (contact_id, empresa_id))

                if cursor.fetchone():
                    # Update
                    cursor.execute("""
                        UPDATE conselhoos_links
                        SET role = %s, synced_at = NOW()
                        WHERE contact_id = %s AND conselhoos_empresa_id = %s
                    """, (role, contact_id, empresa_id))
                else:
                    # Insert
                    cursor.execute("""
                        INSERT INTO conselhoos_links
                        (contact_id, conselhoos_empresa_id, conselhoos_empresa_nome, role, synced_at)
                        VALUES (%s, %s, %s, %s, NOW())
                    """, (contact_id, empresa_id, empresa_nome, role))

                conn.commit()
                return {"success": True}

        except Exception as e:
            logger.error(f"Erro ao vincular contato: {e}")
            return {"error": str(e)}

    def get_contact_empresas(self, contact_id: int) -> List[Dict]:
        """
        Busca empresas vinculadas a um contato.
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT
                        conselhoos_empresa_id,
                        conselhoos_empresa_nome,
                        role,
                        synced_at
                    FROM conselhoos_links
                    WHERE contact_id = %s
                """, (contact_id,))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Erro ao buscar empresas do contato: {e}")
            return []

    def get_reunioes_by_contact(self, contact_id: int, limit: int = 20) -> List[Dict]:
        """
        Busca reuniões das empresas vinculadas a um contato.

        Args:
            contact_id: ID do contato no INTEL
            limit: Número máximo de reuniões

        Returns:
            List of reuniões with empresa info
        """
        try:
            # First get the empresas linked to this contact
            empresas = self.get_contact_empresas(contact_id)
            if not empresas:
                return []

            empresa_ids = [e['conselhoos_empresa_id'] for e in empresas]

            with self._get_conselhoos_conn() as conn:
                cursor = conn.cursor()

                # Query reuniões for all linked empresas
                # Cast text array to UUID array for comparison
                cursor.execute("""
                    SELECT
                        r.id,
                        r.titulo,
                        r.data,
                        r.status,
                        r.calendar_link,
                        e.id as empresa_id,
                        e.nome as empresa_nome,
                        e.cor_hex
                    FROM reunioes r
                    JOIN empresas e ON e.id = r.empresa_id
                    WHERE r.empresa_id = ANY(%s::uuid[])
                    ORDER BY r.data DESC
                    LIMIT %s
                """, (empresa_ids, limit))

                reunioes = [dict(row) for row in cursor.fetchall()]

                # Add empresa_role from links
                empresa_roles = {e['conselhoos_empresa_id']: e.get('role', 'stakeholder') for e in empresas}
                for r in reunioes:
                    r['contact_role'] = empresa_roles.get(str(r['empresa_id']), 'stakeholder')

                return reunioes

        except Exception as e:
            logger.error(f"Erro ao buscar reuniões do contato {contact_id}: {e}")
            return []

    def get_dashboard_summary(self) -> Dict:
        """
        Resumo para exibir no dashboard INTEL.

        Returns:
            Dict with counts and highlights
        """
        try:
            empresas = self.get_empresas()
            reunioes = self.get_proximas_reunioes(limit=5)
            raci_pendentes = self.get_raci_pendentes(limit=10)

            # Count urgencies
            raci_atrasados = len([r for r in raci_pendentes if r.get("urgencia") == "atrasado"])
            raci_urgentes = len([r for r in raci_pendentes if r.get("urgencia") == "urgente"])

            # Next meeting
            proxima_reuniao = reunioes[0] if reunioes else None

            return {
                "total_empresas": len(empresas),
                "proximas_reunioes": len(reunioes),
                "raci_pendentes": len(raci_pendentes),
                "raci_atrasados": raci_atrasados,
                "raci_urgentes": raci_urgentes,
                "proxima_reuniao": proxima_reuniao,
                "empresas": empresas[:5],
                "reunioes": reunioes[:3],
                "raci_destaque": raci_pendentes[:3]
            }

        except Exception as e:
            logger.error(f"Erro ao buscar resumo ConselhoOS: {e}")
            return {
                "error": str(e),
                "total_empresas": 0,
                "proximas_reunioes": 0,
                "raci_pendentes": 0
            }


# Singleton
_conselhoos_sync_service = None


def get_conselhoos_sync_service() -> ConselhoOSSyncService:
    """Get singleton instance."""
    global _conselhoos_sync_service
    if _conselhoos_sync_service is None:
        _conselhoos_sync_service = ConselhoOSSyncService()
    return _conselhoos_sync_service
