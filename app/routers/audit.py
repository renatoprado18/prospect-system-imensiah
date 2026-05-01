"""
Audit Log Router - UI e API para visualizar audit_log.

Rotas:
- GET /auditoria — pagina HTML
- GET /api/audit/recent — lista paginada com filtros
- GET /api/audit/entity/{entity_type}/{entity_id} — historico de entidade
- GET /api/audit/distinct — listas de actions/entity_types/actors para filtros
"""
from typing import Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import os

from database import get_db
from auth import get_current_user

router = APIRouter()

# Templates relative to caller (main.py mounts these globally,
# but we re-instantiate here so the router is self-contained)
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "templates"))


@router.get("/auditoria", response_class=HTMLResponse)
async def audit_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("audit.html", {"request": request, "user": user})


@router.get("/api/audit/recent")
async def api_audit_recent(
    request: Request,
    action: Optional[str] = Query(None, description="Filtra por action (prefixo permitido com %)"),
    entity_type: Optional[str] = None,
    actor: Optional[str] = None,
    since_hours: Optional[int] = Query(None, ge=1, le=24 * 30),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    user = get_current_user(request)
    if not user:
        return {"error": "unauthorized"}, 401

    where = []
    params = []

    if action:
        if "%" in action:
            where.append("action LIKE %s")
        else:
            where.append("action = %s")
        params.append(action)

    if entity_type:
        where.append("entity_type = %s")
        params.append(entity_type)

    if actor:
        where.append("actor = %s")
        params.append(actor)

    if since_hours:
        where.append("criado_em > %s")
        params.append(datetime.now() - timedelta(hours=since_hours))

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) AS total FROM audit_log {where_sql}", params)
        total = cursor.fetchone()["total"]

        cursor.execute(f"""
            SELECT id, action, entity_type, entity_id, actor, details, criado_em
            FROM audit_log
            {where_sql}
            ORDER BY criado_em DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = [_serialize(dict(r)) for r in cursor.fetchall()]

    return {"total": total, "limit": limit, "offset": offset, "items": rows}


@router.get("/api/audit/entity/{entity_type}/{entity_id}")
async def api_audit_entity(request: Request, entity_type: str, entity_id: int, limit: int = 100):
    user = get_current_user(request)
    if not user:
        return {"error": "unauthorized"}, 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, action, entity_type, entity_id, actor, details, criado_em
            FROM audit_log
            WHERE entity_type = %s AND entity_id = %s
            ORDER BY criado_em DESC
            LIMIT %s
        """, (entity_type, entity_id, limit))
        rows = [_serialize(dict(r)) for r in cursor.fetchall()]

    return {"items": rows}


@router.get("/api/audit/distinct")
async def api_audit_distinct(request: Request):
    """Listas distintas (actions, entity_types, actors) para popular filtros."""
    user = get_current_user(request)
    if not user:
        return {"error": "unauthorized"}, 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
        actions = [r["action"] for r in cursor.fetchall()]
        cursor.execute("SELECT DISTINCT entity_type FROM audit_log WHERE entity_type IS NOT NULL ORDER BY entity_type")
        entity_types = [r["entity_type"] for r in cursor.fetchall()]
        cursor.execute("SELECT DISTINCT actor FROM audit_log WHERE actor IS NOT NULL ORDER BY actor")
        actors = [r["actor"] for r in cursor.fetchall()]

    return {"actions": actions, "entity_types": entity_types, "actors": actors}


def _serialize(row: dict) -> dict:
    if row.get("criado_em") and hasattr(row["criado_em"], "isoformat"):
        row["criado_em"] = row["criado_em"].isoformat()
    if isinstance(row.get("details"), str):
        import json
        try:
            row["details"] = json.loads(row["details"])
        except (json.JSONDecodeError, TypeError):
            pass
    return row
