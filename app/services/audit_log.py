"""
Audit Log - Registro centralizado de writes do sistema.

Uso:
    from services.audit_log import log

    log("create_task", entity_type="task", entity_id=task_id,
        actor="intel_bot", details={"titulo": titulo, "contact_id": cid})

    log("proposal_accepted", entity_type="action_proposal", entity_id=pid,
        actor="user", details={"option": option_id})

Consultas:
    get_recent(limit=50)
    get_for_entity("task", 123)
    get_by_action("create_task", limit=20)

Diseño:
- Falhas no log NUNCA quebram a operacao principal (try/except + warning).
- Schema generico: action obrigatorio, resto opcional.
- details e jsonb — jogue qualquer dict relevante.
"""
import json
import logging
from typing import Optional, Dict, List, Any
from database import get_db

logger = logging.getLogger(__name__)


def log(
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    actor: str = "system",
    details: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """
    Grava um evento no audit_log. Retorna o id criado, ou None em caso de falha.
    Falhas sao logadas como warning mas nao propagadas.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO audit_log (action, entity_type, entity_id, actor, details)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                action,
                entity_type,
                entity_id,
                actor,
                json.dumps(_sanitize(details or {})),
            ))
            row = cursor.fetchone()
            conn.commit()
            return row["id"] if row else None
    except Exception as e:
        logger.warning(f"audit_log.log failed for action={action}: {e}")
        return None


def _sanitize(details: Dict) -> Dict:
    """Remove chaves obviamente sensiveis e trunca strings longas."""
    SENSITIVE_KEYS = {"password", "token", "api_key", "secret", "authorization"}
    result = {}
    for k, v in details.items():
        if k.lower() in SENSITIVE_KEYS:
            result[k] = "***"
            continue
        if isinstance(v, str) and len(v) > 2000:
            result[k] = v[:2000] + "...[truncated]"
        elif isinstance(v, dict):
            result[k] = _sanitize(v)
        else:
            result[k] = v
    return result


def get_recent(limit: int = 50) -> List[Dict]:
    """Eventos mais recentes."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, action, entity_type, entity_id, actor, details, criado_em
                FROM audit_log
                ORDER BY criado_em DESC
                LIMIT %s
            """, (limit,))
            return [_serialize(dict(r)) for r in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"audit_log.get_recent failed: {e}")
        return []


def get_for_entity(entity_type: str, entity_id: int, limit: int = 50) -> List[Dict]:
    """Historico de eventos de uma entidade especifica."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, action, entity_type, entity_id, actor, details, criado_em
                FROM audit_log
                WHERE entity_type = %s AND entity_id = %s
                ORDER BY criado_em DESC
                LIMIT %s
            """, (entity_type, entity_id, limit))
            return [_serialize(dict(r)) for r in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"audit_log.get_for_entity failed: {e}")
        return []


def get_by_action(action: str, limit: int = 50) -> List[Dict]:
    """Historico de uma acao especifica (ex: todos os create_task)."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, action, entity_type, entity_id, actor, details, criado_em
                FROM audit_log
                WHERE action = %s
                ORDER BY criado_em DESC
                LIMIT %s
            """, (action, limit))
            return [_serialize(dict(r)) for r in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"audit_log.get_by_action failed: {e}")
        return []


def _serialize(row: Dict) -> Dict:
    """Normaliza linha para JSON-friendly."""
    if row.get("criado_em") and hasattr(row["criado_em"], "isoformat"):
        row["criado_em"] = row["criado_em"].isoformat()
    if isinstance(row.get("details"), str):
        try:
            row["details"] = json.loads(row["details"])
        except (json.JSONDecodeError, TypeError):
            pass
    return row
