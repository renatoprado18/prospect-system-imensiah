"""
Sistema de Gestão de Prospects - ImensIAH
API Backend com FastAPI

Deploy: Vercel (Serverless)
Domínio: prospects.almeida-prado.com
"""
import os
import json
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models import (
    Prospect, Meeting, ProspectStatus, ProspectTier,
    MeetingOutcome, UserRole
)
from database import get_db as get_pg_db, init_db, get_connection
from scoring import DynamicScorer
from integrations.google_calendar import GoogleCalendarIntegration, create_calendar_link
from integrations.fathom import FathomIntegration, handle_fathom_webhook
from integrations.linkedin import LinkedInIntegration
from integrations.whatsapp import WhatsAppIntegration, parse_webhook_message, format_phone_display
from integrations.gmail import GmailIntegration, parse_gmail_date
from auth import (
    get_current_user, require_auth, require_admin, require_operador,
    google_login, google_callback, logout, ALLOWED_USERS, SECRET_KEY
)

# Config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
FATHOM_API_KEY = os.getenv("FATHOM_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "..", "data")

# Lifespan handler
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        init_db()
        print("PostgreSQL database initialized")
    except Exception as e:
        print(f"DB init error: {e}")
    yield
    # Shutdown
    pass

# App
app = FastAPI(
    title="Sistema de Prospects ImensIAH",
    description="Gestão e qualificação de prospects com IA",
    version="1.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Services
scorer = DynamicScorer()  # Now uses PostgreSQL
calendar = GoogleCalendarIntegration()
fathom = FathomIntegration()
linkedin = LinkedInIntegration()
whatsapp = WhatsAppIntegration()


# ============== Pydantic Models ==============

class ProspectCreate(BaseModel):
    nome: str
    empresa: Optional[str] = None
    cargo: Optional[str] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    linkedin: Optional[str] = None

class ProspectUpdate(BaseModel):
    status: Optional[ProspectStatus] = None
    meeting_outcome: Optional[MeetingOutcome] = None
    meeting_notes: Optional[str] = None
    objecoes: Optional[List[str]] = None
    interesse_features: Optional[List[str]] = None
    converted: Optional[bool] = None
    deal_value: Optional[float] = None

class MeetingCreate(BaseModel):
    prospect_id: int
    data_hora: datetime
    duracao_minutos: int = 30
    tipo: str = "discovery"

class FeedbackSubmit(BaseModel):
    prospect_id: int
    outcome: MeetingOutcome
    objecoes: List[str] = []
    features_interesse: List[str] = []
    notes: str = ""
    proximos_passos: str = ""

class ProspectApproval(BaseModel):
    aprovado: bool
    notas: str = ""
    prioridade: int = 0

class BulkApproval(BaseModel):
    prospect_ids: List[int]
    aprovado: bool
    notas: str = ""

class InteractionCreate(BaseModel):
    tipo: str  # 'reuniao', 'email', 'linkedin', 'telefone', 'evento', 'nota'
    titulo: Optional[str] = None
    descricao: Optional[str] = None
    data_interacao: Optional[datetime] = None
    fathom_link: Optional[str] = None
    fathom_summary: Optional[str] = None
    tags: Optional[List[str]] = []
    sentimento: Optional[str] = None  # 'positivo', 'neutro', 'negativo'

class InteractionUpdate(BaseModel):
    tipo: Optional[str] = None
    titulo: Optional[str] = None
    descricao: Optional[str] = None
    data_interacao: Optional[datetime] = None
    fathom_link: Optional[str] = None
    fathom_summary: Optional[str] = None
    tags: Optional[List[str]] = None
    sentimento: Optional[str] = None


# ============== Database Helpers ==============

def get_db():
    """Get PostgreSQL connection"""
    return get_connection()

def row_to_dict(row):
    """Convert RealDictRow to dict"""
    return dict(row) if row else None


# ============== Health Check ==============

@app.get("/api/health")
async def health_check():
    """Verifica status do sistema e banco de dados"""
    import os
    status = {"status": "ok", "database": "not_connected"}

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM prospects")
        count = cursor.fetchone()['count']
        conn.close()
        status["database"] = "connected"
        status["prospects_count"] = count
    except Exception as e:
        status["database"] = "error"
        status["error"] = str(e)
        status["postgres_url_set"] = bool(os.getenv("POSTGRES_URL"))

    return status

@app.post("/api/admin/reset-db")
async def reset_database():
    """Reseta o banco de dados (CUIDADO!)"""
    conn = get_db()
    cursor = conn.cursor()

    # Drop unique index if exists
    cursor.execute("DROP INDEX IF EXISTS idx_prospects_email")

    # Clear prospects table
    cursor.execute("DELETE FROM prospects")

    # Recreate non-unique index
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_prospects_email
        ON prospects(email) WHERE email IS NOT NULL AND email != ''
    ''')

    conn.commit()
    conn.close()

    return {"status": "reset", "message": "Database cleared and index recreated"}


# ============== API Routes - Auth ==============

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None, email: Optional[str] = None):
    """Página de login"""
    user = get_current_user(request)
    if user:
        # Already logged in, redirect
        return RedirectResponse(url="/admin" if user["role"] == "admin" else "/", status_code=302)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "email": email
    })

@app.get("/auth/google/login")
async def auth_google_login(request: Request):
    """Inicia login com Google"""
    return await google_login(request)

@app.get("/auth/google/callback")
async def auth_google_callback(request: Request):
    """Callback do Google OAuth"""
    return await google_callback(request)

@app.get("/logout")
async def auth_logout():
    """Logout e limpa sessão"""
    return logout()

@app.get("/api/auth/me")
async def get_me(request: Request):
    """Retorna dados do usuário logado"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")
    return user


# ============== API Routes - Pages ==============

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Dashboard - requer autenticação"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Admin e operador podem ver o dashboard
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user
    })

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    """Painel administrativo - apenas admin"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user["role"] != "admin":
        return RedirectResponse(url="/", status_code=302)

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": user
    })

@app.get("/prospect/{prospect_id}", response_class=HTMLResponse)
async def prospect_detail_page(request: Request, prospect_id: int):
    """Página de detalhe do prospect"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse("prospect_detail.html", {
        "request": request,
        "user": user,
        "prospect_id": prospect_id
    })

# ============== RAP Pages ==============

@app.get("/rap", response_class=HTMLResponse)
async def rap_dashboard(request: Request):
    """RAP Dashboard - Assistente Pessoal"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse("rap_dashboard.html", {
        "request": request,
        "user": user
    })

@app.get("/rap/contacts", response_class=HTMLResponse)
async def rap_contacts(request: Request):
    """RAP Contacts List"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse("rap_contacts.html", {
        "request": request,
        "user": user
    })

@app.get("/rap/contacts/cleanup", response_class=HTMLResponse)
async def rap_contacts_cleanup(request: Request):
    """Page for reviewing and cleaning up contacts"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse("rap_contacts_cleanup.html", {
        "request": request,
        "user": user
    })


@app.get("/rap/contacts/linkedin", response_class=HTMLResponse)
async def rap_contacts_linkedin(request: Request):
    """Page for importing LinkedIn connections"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse("rap_linkedin_import.html", {
        "request": request,
        "user": user
    })


# NOTE: This parameterized route MUST come AFTER specific routes like /cleanup, /linkedin
@app.get("/rap/contacts/{contact_id}", response_class=HTMLResponse)
async def rap_contact_detail(request: Request, contact_id: int):
    """RAP Contact Detail Page"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse("rap_contact_detail.html", {
        "request": request,
        "user": user,
        "contact_id": contact_id
    })

@app.get("/rap/settings", response_class=HTMLResponse)
async def rap_settings(request: Request):
    """RAP Settings Page - Google Accounts Management"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.get("role") != "admin":
        return RedirectResponse(url="/rap", status_code=302)

    return templates.TemplateResponse("rap_settings.html", {
        "request": request,
        "user": user
    })


@app.get("/rap/whatsapp", response_class=HTMLResponse)
async def rap_whatsapp(request: Request):
    """RAP WhatsApp Integration Page"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse("rap_whatsapp.html", {
        "request": request,
        "user": user
    })


@app.get("/api/user/{email}")
async def get_user(email: str):
    """Obtém dados do usuário"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE email LIKE %s", (f"%{email}%",))
    user = cursor.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    return row_to_dict(user)

@app.post("/api/user/{email}/complete-tutorial")
async def complete_tutorial(email: str):
    """Marca tutorial como concluído"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE users SET tutorial_concluido = TRUE WHERE email LIKE %s",
        (f"%{email}%",)
    )
    conn.commit()
    conn.close()

    return {"status": "completed"}


# ============== API Routes - Approval (Renato Only) ==============

@app.get("/api/admin/pending")
async def list_pending_approval(
    tier: Optional[str] = None,
    limit: int = Query(100, le=500),
    offset: int = 0
):
    """Lista prospects pendentes de aprovação (para Renato)"""
    conn = get_db()
    cursor = conn.cursor()

    query = "SELECT * FROM prospects WHERE status = 'pendente_aprovacao'"
    params = []

    if tier:
        query += " AND tier = %s"
        params.append(tier)

    query += " ORDER BY score DESC, tier ASC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()

    # Contar totais por tier
    cursor.execute('''
        SELECT tier, COUNT(*) as count
        FROM prospects
        WHERE status = 'pendente_aprovacao'
        GROUP BY tier
    ''')
    tier_counts = {row['tier']: row['count'] for row in cursor.fetchall()}

    conn.close()

    return {
        "prospects": [row_to_dict(row) for row in rows],
        "tier_counts": tier_counts,
        "total": sum(tier_counts.values())
    }

@app.post("/api/admin/approve/{prospect_id}")
async def approve_prospect(prospect_id: int, approval: ProspectApproval):
    """Aprova ou rejeita um prospect (Renato)"""
    conn = get_db()
    cursor = conn.cursor()

    new_status = "novo" if approval.aprovado else "rejeitado"

    cursor.execute('''
        UPDATE prospects
        SET aprovado_por_renato = %s,
            status = %s,
            notas_renato = %s,
            prioridade_renato = %s,
            data_aprovacao = %s
        WHERE id = %s
    ''', (
        approval.aprovado,
        new_status,
        approval.notas,
        approval.prioridade,
        datetime.now().isoformat(),
        prospect_id
    ))

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (%s, 'Renato', %s, %s)
    ''', (
        prospect_id,
        'Aprovado' if approval.aprovado else 'Rejeitado',
        approval.notas
    ))

    conn.commit()
    conn.close()

    return {"status": new_status, "prospect_id": prospect_id}

@app.post("/api/admin/approve-bulk")
async def approve_bulk(bulk: BulkApproval):
    """Aprova múltiplos prospects de uma vez"""
    conn = get_db()
    cursor = conn.cursor()

    new_status = "novo" if bulk.aprovado else "rejeitado"
    approved_count = 0

    for prospect_id in bulk.prospect_ids:
        cursor.execute('''
            UPDATE prospects
            SET aprovado_por_renato = %s,
                status = %s,
                notas_renato = %s,
                data_aprovacao = %s
            WHERE id = %s AND status = 'pendente_aprovacao'
        ''', (
            bulk.aprovado,
            new_status,
            bulk.notas,
            datetime.now().isoformat(),
            prospect_id
        ))
        if cursor.rowcount > 0:
            approved_count += 1

    conn.commit()
    conn.close()

    return {
        "status": "completed",
        "approved_count": approved_count,
        "action": "aprovado" if bulk.aprovado else "rejeitado"
    }

@app.get("/api/admin/stats")
async def admin_stats():
    """Estatísticas para painel admin"""
    conn = get_db()
    cursor = conn.cursor()

    stats = {}

    # Pendentes por tier
    cursor.execute('''
        SELECT tier, COUNT(*) as count
        FROM prospects
        WHERE status = 'pendente_aprovacao'
        GROUP BY tier
        ORDER BY tier
    ''')
    stats['pendentes_por_tier'] = {row['tier']: row['count'] for row in cursor.fetchall()}
    stats['total_pendentes'] = sum(stats['pendentes_por_tier'].values())

    # Aprovados
    cursor.execute("SELECT COUNT(*) as count FROM prospects WHERE aprovado_por_renato = TRUE")
    stats['total_aprovados'] = cursor.fetchone()['count']

    # Rejeitados
    cursor.execute("SELECT COUNT(*) as count FROM prospects WHERE status = 'rejeitado'")
    stats['total_rejeitados'] = cursor.fetchone()['count']

    # Top prospects pendentes
    cursor.execute('''
        SELECT * FROM prospects
        WHERE status = 'pendente_aprovacao' AND tier IN ('A', 'B')
        ORDER BY score DESC
        LIMIT 20
    ''')
    stats['top_pendentes'] = [row_to_dict(row) for row in cursor.fetchall()]

    conn.close()
    return stats


# ============== API Routes - Prospects ==============


@app.get("/api/prospects")
async def list_prospects(
    tier: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    user_role: str = "operador",  # operador=Andressa, admin=Renato
    limit: int = Query(50, le=500),
    offset: int = 0
):
    """
    Lista prospects com filtros

    - Para Andressa (operador): só vê prospects aprovados por Renato
    - Para Renato (admin): vê todos
    """
    conn = get_db()
    cursor = conn.cursor()

    query = "SELECT * FROM prospects WHERE 1=1"
    params = []

    # Andressa só vê aprovados (não pendentes nem rejeitados)
    if user_role != "admin":
        query += " AND aprovado_por_renato = TRUE AND status != 'rejeitado' AND status != 'pendente_aprovacao'"

    if tier:
        query += " AND tier = %s"
        params.append(tier)

    if status:
        query += " AND status = %s"
        params.append(status)

    if search:
        query += " AND (nome ILIKE %s OR empresa ILIKE %s OR cargo ILIKE %s)"
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term])

    # Ordenar por prioridade de Renato primeiro, depois score
    query += " ORDER BY prioridade_renato DESC, score DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()

    # Count total
    count_query = query.replace("SELECT *", "SELECT COUNT(*)").split("ORDER BY")[0]
    cursor.execute(count_query, params[:-2] if params[:-2] else None)
    result = cursor.fetchone()
    total = result['count'] if result else 0

    conn.close()

    return {
        "prospects": [row_to_dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset
    }


@app.get("/api/prospects/{prospect_id}")
async def get_prospect(prospect_id: int):
    """Obtém detalhes de um prospect"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM prospects WHERE id = %s", (prospect_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    prospect = row_to_dict(row)

    # Buscar reuniões
    cursor.execute(
        "SELECT * FROM meetings WHERE prospect_id = %s ORDER BY data_hora DESC",
        (prospect_id,)
    )
    meetings = [row_to_dict(r) for r in cursor.fetchall()]

    # Buscar atividades
    cursor.execute(
        "SELECT * FROM activity_log WHERE prospect_id = %s ORDER BY data_hora DESC LIMIT 20",
        (prospect_id,)
    )
    activities = [row_to_dict(r) for r in cursor.fetchall()]

    # Buscar interações (timeline)
    cursor.execute(
        "SELECT * FROM interactions WHERE prospect_id = %s ORDER BY data_interacao DESC NULLS LAST, created_at DESC",
        (prospect_id,)
    )
    interactions = [row_to_dict(r) for r in cursor.fetchall()]

    conn.close()

    return {
        "prospect": prospect,
        "meetings": meetings,
        "activities": activities,
        "interactions": interactions
    }


@app.post("/api/prospects")
async def create_prospect(prospect: ProspectCreate):
    """Cria novo prospect e calcula score"""
    conn = get_db()
    cursor = conn.cursor()

    # Calcular score
    score, breakdown, reasons = scorer.calculate_score(prospect.model_dump())
    tier = scorer.determine_tier(score)

    cursor.execute('''
        INSERT INTO prospects (nome, empresa, cargo, email, telefone, linkedin,
                              score, tier, score_breakdown, reasons)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (
        prospect.nome, prospect.empresa, prospect.cargo,
        prospect.email, prospect.telefone, prospect.linkedin,
        score, tier, json.dumps(breakdown), json.dumps(reasons)
    ))

    prospect_id = cursor.fetchone()['id']
    conn.commit()
    conn.close()

    return {"id": prospect_id, "score": score, "tier": tier}


@app.patch("/api/prospects/{prospect_id}")
async def update_prospect(prospect_id: int, update: ProspectUpdate):
    """Atualiza prospect"""
    conn = get_db()
    cursor = conn.cursor()

    updates = []
    params = []

    for field, value in update.model_dump(exclude_none=True).items():
        if isinstance(value, list):
            value = json.dumps(value)
        updates.append(f"{field} = %s")
        params.append(value)

    if not updates:
        conn.close()
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")

    params.append(prospect_id)
    query = f"UPDATE prospects SET {', '.join(updates)} WHERE id = %s"

    cursor.execute(query, params)

    # Log atividade
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (%s, 'Andressa', 'Atualização', %s)
    ''', (prospect_id, json.dumps(update.model_dump(exclude_none=True))))

    conn.commit()
    conn.close()

    return {"status": "updated"}


# ============== API Routes - Interactions (Timeline) ==============

@app.get("/api/prospects/{prospect_id}/interactions")
async def list_interactions(prospect_id: int):
    """Lista todas as interações de um prospect (timeline)"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM interactions WHERE prospect_id = %s ORDER BY data_interacao DESC NULLS LAST, created_at DESC",
        (prospect_id,)
    )
    interactions = [row_to_dict(r) for r in cursor.fetchall()]
    conn.close()

    return {"interactions": interactions}


@app.post("/api/prospects/{prospect_id}/interactions")
async def create_interaction(prospect_id: int, interaction: InteractionCreate):
    """Cria nova interação na timeline do prospect"""
    conn = get_db()
    cursor = conn.cursor()

    # Verificar se prospect existe
    cursor.execute("SELECT id FROM prospects WHERE id = %s", (prospect_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    # Se não tiver data, usar agora
    data_interacao = interaction.data_interacao or datetime.now()

    cursor.execute('''
        INSERT INTO interactions (prospect_id, tipo, titulo, descricao, data_interacao,
                                  fathom_link, fathom_summary, tags, sentimento)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (
        prospect_id,
        interaction.tipo,
        interaction.titulo,
        interaction.descricao,
        data_interacao.isoformat(),
        interaction.fathom_link,
        interaction.fathom_summary,
        json.dumps(interaction.tags or []),
        interaction.sentimento
    ))

    interaction_id = cursor.fetchone()['id']

    # Atualizar data_ultimo_contato do prospect
    cursor.execute('''
        UPDATE prospects SET data_ultimo_contato = %s WHERE id = %s
    ''', (data_interacao.isoformat(), prospect_id))

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (%s, 'Sistema', 'Nova Interação', %s)
    ''', (prospect_id, f"Tipo: {interaction.tipo} - {interaction.titulo or 'Sem título'}"))

    conn.commit()
    conn.close()

    return {"id": interaction_id, "status": "created"}


@app.put("/api/interactions/{interaction_id}")
async def update_interaction(interaction_id: int, update: InteractionUpdate):
    """Atualiza uma interação existente"""
    conn = get_db()
    cursor = conn.cursor()

    updates = []
    params = []

    for field, value in update.model_dump(exclude_none=True).items():
        if field == 'tags' and isinstance(value, list):
            value = json.dumps(value)
        if field == 'data_interacao' and value:
            value = value.isoformat()
        updates.append(f"{field} = %s")
        params.append(value)

    if not updates:
        conn.close()
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")

    params.append(interaction_id)
    query = f"UPDATE interactions SET {', '.join(updates)} WHERE id = %s"

    cursor.execute(query, params)

    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Interação não encontrada")

    conn.commit()
    conn.close()

    return {"status": "updated"}


@app.delete("/api/interactions/{interaction_id}")
async def delete_interaction(interaction_id: int):
    """Remove uma interação"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM interactions WHERE id = %s", (interaction_id,))

    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Interação não encontrada")

    conn.commit()
    conn.close()

    return {"status": "deleted"}


@app.post("/api/prospects/{prospect_id}/convert")
async def mark_converted(
    prospect_id: int,
    deal_value: float,
    notes: str = ""
):
    """Marca prospect como convertido e atualiza learning"""
    conn = get_db()
    cursor = conn.cursor()

    # Atualizar prospect
    cursor.execute('''
        UPDATE prospects
        SET converted = TRUE, deal_value = %s, conversion_notes = %s, status = 'convertido'
        WHERE id = %s
    ''', (deal_value, notes, prospect_id))

    # Buscar dados do prospect para learning
    cursor.execute("SELECT * FROM prospects WHERE id = %s", (prospect_id,))
    prospect = row_to_dict(cursor.fetchone())

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (%s, 'Sistema', 'Conversão', %s)
    ''', (prospect_id, f"Deal: R$ {deal_value}"))

    conn.commit()
    conn.close()

    # Atualizar weights do scorer
    scorer.update_weights_from_conversion(prospect, True, deal_value)

    return {"status": "converted", "deal_value": deal_value}


# ============== API Routes - Meetings ==============

@app.post("/api/meetings")
async def schedule_meeting(meeting: MeetingCreate, background_tasks: BackgroundTasks):
    """Agenda reunião com integração Google Calendar"""
    conn = get_db()
    cursor = conn.cursor()

    # Buscar prospect
    cursor.execute("SELECT * FROM prospects WHERE id = %s", (meeting.prospect_id,))
    prospect = row_to_dict(cursor.fetchone())

    if not prospect:
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    # Criar evento no Google Calendar
    calendar_event = None
    meet_link = None

    try:
        if calendar.authenticate():
            calendar_event = calendar.create_meeting(
                prospect_name=prospect['nome'],
                prospect_email=prospect.get('email'),
                date_time=meeting.data_hora,
                duration_minutes=meeting.duracao_minutos,
                meeting_type=meeting.tipo
            )
            if calendar_event:
                meet_link = calendar_event.get('meet_link')
    except Exception as e:
        print(f"Calendar integration error: {e}")

    # Fallback: criar link manual
    if not calendar_event:
        calendar_link = create_calendar_link(
            prospect['nome'],
            meeting.data_hora,
            meeting.duracao_minutos,
            meeting.tipo
        )
    else:
        calendar_link = calendar_event.get('link')

    # Salvar reunião
    cursor.execute('''
        INSERT INTO meetings (prospect_id, google_event_id, data_hora, duracao_minutos, tipo)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    ''', (
        meeting.prospect_id,
        calendar_event.get('id') if calendar_event else None,
        meeting.data_hora.isoformat(),
        meeting.duracao_minutos,
        meeting.tipo
    ))

    meeting_id = cursor.fetchone()['id']

    # Atualizar status do prospect
    cursor.execute('''
        UPDATE prospects
        SET status = 'reuniao_agendada', data_reuniao = %s
        WHERE id = %s
    ''', (meeting.data_hora.isoformat(), meeting.prospect_id))

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (%s, 'Andressa', 'Reunião Agendada', %s)
    ''', (meeting.prospect_id, f"Tipo: {meeting.tipo}, Data: {meeting.data_hora}"))

    conn.commit()
    conn.close()

    return {
        "meeting_id": meeting_id,
        "calendar_link": calendar_link,
        "meet_link": meet_link
    }


@app.get("/api/meetings/slots")
async def get_available_slots(
    start_date: Optional[str] = None,
    days: int = 7
):
    """Retorna horários disponíveis para reunião"""
    if start_date:
        start = datetime.fromisoformat(start_date)
    else:
        start = datetime.now() + timedelta(days=1)

    try:
        if calendar.authenticate():
            slots = calendar.get_available_slots(start, days)
            return {"slots": slots}
    except:
        pass

    # Fallback: gerar slots genéricos
    slots = []
    current = start.replace(hour=9, minute=0, second=0)
    for day in range(days):
        if current.weekday() < 5:  # Seg-Sex
            for hour in [9, 10, 11, 14, 15, 16, 17]:
                slot_time = current.replace(hour=hour)
                slots.append({
                    "start": slot_time.isoformat(),
                    "end": (slot_time + timedelta(minutes=30)).isoformat(),
                    "formatted": slot_time.strftime('%d/%m/%Y %H:%M')
                })
        current += timedelta(days=1)

    return {"slots": slots}


# ============== API Routes - Feedback & Learning ==============

@app.post("/api/feedback")
async def submit_feedback(feedback: FeedbackSubmit):
    """Submete feedback de reunião para learning"""
    conn = get_db()
    cursor = conn.cursor()

    # Atualizar prospect
    cursor.execute('''
        UPDATE prospects
        SET status = 'reuniao_realizada',
            meeting_outcome = %s,
            meeting_notes = %s,
            objecoes = %s,
            interesse_features = %s,
            data_ultimo_contato = %s
        WHERE id = %s
    ''', (
        feedback.outcome.value,
        feedback.notes,
        json.dumps(feedback.objecoes),
        json.dumps(feedback.features_interesse),
        datetime.now().isoformat(),
        feedback.prospect_id
    ))

    # Atualizar meeting (PostgreSQL doesn't support ORDER BY in UPDATE, use subquery)
    cursor.execute('''
        UPDATE meetings
        SET realizada = TRUE, outcome = %s, objecoes_identificadas = %s, pontos_interesse = %s, proximos_passos = %s
        WHERE id = (
            SELECT id FROM meetings
            WHERE prospect_id = %s AND realizada = FALSE
            ORDER BY data_hora DESC LIMIT 1
        )
    ''', (
        feedback.outcome.value,
        json.dumps(feedback.objecoes),
        json.dumps(feedback.features_interesse),
        feedback.proximos_passos,
        feedback.prospect_id
    ))

    # Registrar objeções para análise
    for objecao in feedback.objecoes:
        cursor.execute('''
            INSERT INTO sales_arguments (argumento, categoria, objecao_relacionada)
            VALUES (%s, 'objecao', %s)
            ON CONFLICT DO NOTHING
        ''', (f"Resposta para: {objecao}", objecao))

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (%s, 'Andressa', 'Feedback Reunião', %s)
    ''', (feedback.prospect_id, f"Outcome: {feedback.outcome.value}"))

    conn.commit()
    conn.close()

    # Determinar próximo status
    next_status_map = {
        MeetingOutcome.MUITO_INTERESSADO: "negociando",
        MeetingOutcome.INTERESSADO: "nurturing",
        MeetingOutcome.NEUTRO: "nurturing",
        MeetingOutcome.POUCO_INTERESSE: "nurturing",
        MeetingOutcome.SEM_INTERESSE: "perdido",
        MeetingOutcome.NAO_COMPARECEU: "contatado"
    }

    return {
        "status": "recorded",
        "suggested_next_status": next_status_map.get(feedback.outcome, "nurturing")
    }


@app.post("/api/webhooks/fathom")
async def fathom_webhook(request: Request):
    """Webhook para receber dados do Fathom automaticamente"""
    payload = await request.json()
    result = await handle_fathom_webhook(payload)
    return result


# ============== WhatsApp Integration ==============

@app.post("/api/webhooks/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Webhook para receber eventos da Evolution API (WhatsApp)

    Events:
    - messages.upsert: Nova mensagem recebida/enviada
    - connection.update: Status da conexao mudou
    """
    try:
        payload = await request.json()
        parsed = parse_webhook_message(payload)

        if not parsed:
            return {"status": "ignored", "reason": "unsupported event"}

        # Handle connection updates
        if parsed.get("event") == "connection_update":
            return {"status": "ok", "connection_state": parsed.get("state")}

        # Process message
        phone = parsed.get("phone")
        direction = parsed.get("direction")
        content = parsed.get("content")
        timestamp = parsed.get("timestamp")
        push_name = parsed.get("push_name")

        if not phone or not content:
            return {"status": "ignored", "reason": "no content"}

        conn = get_connection()
        cursor = conn.cursor()

        try:
            # Find contact by phone number (search in telefones JSONB array)
            cursor.execute("""
                SELECT id, nome, telefones
                FROM contacts
                WHERE telefones::text ILIKE %s
                LIMIT 1
            """, (f'%{phone[-8:]}%',))  # Match last 8 digits

            contact = cursor.fetchone()
            contact_id = None
            contact_name = push_name or "Desconhecido"

            if contact:
                contact_id = contact['id']
                contact_name = contact['nome']

                # Update ultimo_contato
                cursor.execute("""
                    UPDATE contacts
                    SET ultimo_contato = %s
                    WHERE id = %s
                """, (timestamp, contact_id))

            # Find or create conversation
            conversation_id = None
            if contact_id:
                cursor.execute("""
                    SELECT id FROM conversations
                    WHERE contact_id = %s AND canal = 'whatsapp'
                    LIMIT 1
                """, (contact_id,))
                conv = cursor.fetchone()

                if conv:
                    conversation_id = conv['id']
                    # Update last message time
                    cursor.execute("""
                        UPDATE conversations
                        SET ultimo_mensagem = %s, total_mensagens = total_mensagens + 1
                        WHERE id = %s
                    """, (timestamp, conversation_id))
                else:
                    # Create new conversation
                    cursor.execute("""
                        INSERT INTO conversations (contact_id, canal, ultimo_mensagem, total_mensagens)
                        VALUES (%s, 'whatsapp', %s, 1)
                        RETURNING id
                    """, (contact_id, timestamp))
                    conversation_id = cursor.fetchone()['id']

            # Save message
            cursor.execute("""
                INSERT INTO messages (conversation_id, contact_id, direcao, conteudo, enviado_em, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (conversation_id, contact_id, direction, content, timestamp,
                  json.dumps({"phone": phone, "push_name": push_name})))

            message_id = cursor.fetchone()['id']
            conn.commit()

            return {
                "status": "ok",
                "message_id": message_id,
                "contact_id": contact_id,
                "contact_name": contact_name,
                "direction": direction
            }

        except Exception as e:
            conn.rollback()
            print(f"WhatsApp webhook error: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            cursor.close()
            conn.close()

    except Exception as e:
        print(f"WhatsApp webhook parse error: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/whatsapp/status")
async def whatsapp_status():
    """Get WhatsApp connection status with stats"""
    status = await whatsapp.get_connection_status()

    # Add WhatsApp stats from database
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Messages today (Brazil timezone)
        cursor.execute("""
            SELECT COUNT(*) as count FROM messages
            WHERE metadata->>'is_group' IS NOT NULL
            AND enviado_em >= (CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo')::date
        """)
        messages_today = cursor.fetchone()['count']

        # Active conversations (with WhatsApp messages)
        cursor.execute("""
            SELECT COUNT(DISTINCT c.id) as count
            FROM conversations c
            WHERE c.canal = 'whatsapp'
            AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
        """)
        active_conversations = cursor.fetchone()['count']

        # Contacts with WhatsApp messages
        cursor.execute("""
            SELECT COUNT(DISTINCT contact_id) as count
            FROM messages
            WHERE metadata->>'is_group' IS NOT NULL
        """)
        contacts_with_whatsapp = cursor.fetchone()['count']

        # Recent activity (last 10 messages) - convert to Brazil time
        cursor.execute("""
            SELECT m.id, m.direcao, m.conteudo,
                   (m.enviado_em AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo') as enviado_em_local,
                   c.nome as contact_name, c.id as contact_id,
                   m.metadata->>'phone' as phone,
                   m.metadata->>'is_group' as is_group
            FROM messages m
            LEFT JOIN contacts c ON m.contact_id = c.id
            WHERE m.metadata->>'is_group' IS NOT NULL
            ORDER BY m.enviado_em DESC
            LIMIT 10
        """)
        recent_messages = []
        for row in cursor.fetchall():
            recent_messages.append({
                "id": row['id'],
                "direction": row['direcao'],
                "content": row['conteudo'][:100] if row['conteudo'] else None,
                "sent_at": row['enviado_em_local'].isoformat() if row['enviado_em_local'] else None,
                "contact_name": row['contact_name'],
                "contact_id": row['contact_id'],
                "phone": row['phone'],
                "is_group": row['is_group'] == 'true'
            })

        cursor.close()
        conn.close()

        status['stats'] = {
            'messages_today': messages_today,
            'active_conversations': active_conversations,
            'contacts_with_whatsapp': contacts_with_whatsapp
        }
        status['recent_activity'] = recent_messages

    except Exception as e:
        status['stats_error'] = str(e)

    return status


@app.post("/api/whatsapp/send")
async def send_whatsapp_message(request: Request):
    """
    Send a WhatsApp message

    Body:
    - phone: Phone number (any format)
    - message: Text message
    """
    data = await request.json()
    phone = data.get("phone")
    message = data.get("message")

    if not phone or not message:
        raise HTTPException(status_code=400, detail="phone and message required")

    result = await whatsapp.send_text(phone, message)

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return {"status": "sent", "result": result}


@app.get("/api/whatsapp/qr")
async def get_whatsapp_qr():
    """Get QR code for WhatsApp connection"""
    import httpx

    base_url = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
    api_key = os.getenv("EVOLUTION_API_KEY", "")
    instance = os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp")

    if not base_url or not api_key:
        raise HTTPException(status_code=500, detail="Evolution API not configured")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{base_url}/instance/connect/{instance}",
                headers={"apikey": api_key},
                timeout=10.0
            )
            data = response.json()
            return {
                "qr_base64": data.get("base64"),
                "pairing_code": data.get("pairingCode"),
                "count": data.get("count")
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/whatsapp/disconnect")
async def disconnect_whatsapp():
    """Disconnect WhatsApp instance"""
    import httpx

    base_url = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
    api_key = os.getenv("EVOLUTION_API_KEY", "")
    instance = os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp")

    if not base_url or not api_key:
        raise HTTPException(status_code=500, detail="Evolution API not configured")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.delete(
                f"{base_url}/instance/logout/{instance}",
                headers={"apikey": api_key},
                timeout=10.0
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# ============== WHATSAPP SEARCH ==============

@app.get("/api/whatsapp/search")
async def search_whatsapp_messages(
    q: str,
    contact_id: int = None,
    limit: int = 50
):
    """
    Search WhatsApp messages by content.

    Args:
        q: Search query (min 3 characters)
        contact_id: Optional - filter by specific contact
        limit: Max results (default 50)
    """
    if not q or len(q) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters")

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Build query
        if contact_id:
            cursor.execute("""
                SELECT m.id, m.conversation_id, m.contact_id, m.direcao, m.conteudo,
                       m.enviado_em, m.metadata, c.nome as contact_name
                FROM messages m
                LEFT JOIN contacts c ON m.contact_id = c.id
                JOIN conversations conv ON m.conversation_id = conv.id
                WHERE conv.canal = 'whatsapp'
                  AND m.contact_id = %s
                  AND m.conteudo ILIKE %s
                ORDER BY m.enviado_em DESC
                LIMIT %s
            """, (contact_id, f"%{q}%", limit))
        else:
            cursor.execute("""
                SELECT m.id, m.conversation_id, m.contact_id, m.direcao, m.conteudo,
                       m.enviado_em, m.metadata, c.nome as contact_name
                FROM messages m
                LEFT JOIN contacts c ON m.contact_id = c.id
                JOIN conversations conv ON m.conversation_id = conv.id
                WHERE conv.canal = 'whatsapp'
                  AND m.conteudo ILIKE %s
                ORDER BY m.enviado_em DESC
                LIMIT %s
            """, (f"%{q}%", limit))

        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row["id"],
                "contact_id": row["contact_id"],
                "contact_name": row["contact_name"] or "Desconhecido",
                "direction": row["direcao"],
                "content": row["conteudo"],
                "sent_at": row["enviado_em"].isoformat() if row["enviado_em"] else None,
                "metadata": row["metadata"] if isinstance(row["metadata"], dict) else {}
            })

        return {
            "query": q,
            "total": len(results),
            "results": results
        }

    finally:
        conn.close()


@app.get("/api/whatsapp/export/{contact_id}")
async def export_whatsapp_conversation(
    contact_id: int,
    format: str = "csv"
):
    """
    Export WhatsApp conversation history for a contact.

    Args:
        contact_id: Contact ID to export conversation for
        format: Export format - 'csv' (default) or 'json'
    """
    from fastapi.responses import StreamingResponse
    import io
    import csv

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Get contact info
        cursor.execute("SELECT nome, telefone FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")

        # Get all messages for this contact
        cursor.execute("""
            SELECT m.direcao, m.conteudo, m.enviado_em, m.metadata
            FROM messages m
            JOIN conversations conv ON m.conversation_id = conv.id
            WHERE conv.canal = 'whatsapp' AND m.contact_id = %s
            ORDER BY m.enviado_em ASC
        """, (contact_id,))

        messages = cursor.fetchall()

        if format == "json":
            import json
            data = {
                "contact": {
                    "id": contact_id,
                    "name": contact["nome"],
                    "phone": contact["telefone"]
                },
                "messages": [
                    {
                        "direction": msg["direcao"],
                        "content": msg["conteudo"],
                        "sent_at": msg["enviado_em"].isoformat() if msg["enviado_em"] else None,
                        "status": msg["metadata"].get("status") if isinstance(msg["metadata"], dict) else None
                    }
                    for msg in messages
                ],
                "total": len(messages),
                "exported_at": datetime.now().isoformat()
            }
            return data

        # Default: CSV format
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Data/Hora", "Direcao", "Mensagem", "Status"])

        for msg in messages:
            sent_at = msg["enviado_em"].strftime("%Y-%m-%d %H:%M:%S") if msg["enviado_em"] else ""
            direction = "Enviada" if msg["direcao"] == "outgoing" else "Recebida"
            content = msg["conteudo"] or "[midia]"
            status = ""
            if isinstance(msg["metadata"], dict):
                status = msg["metadata"].get("status", "")
            writer.writerow([sent_at, direction, content, status])

        output.seek(0)
        contact_name = contact["nome"].replace(" ", "_") if contact["nome"] else str(contact_id)
        filename = f"whatsapp_{contact_name}_{datetime.now().strftime('%Y%m%d')}.csv"

        return StreamingResponse(
            io.BytesIO(output.getvalue().encode('utf-8-sig')),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    finally:
        conn.close()


@app.post("/api/whatsapp/sync")
async def sync_whatsapp_history(include_groups: bool = False, limit: int = 50, offset: int = 0):
    """
    Sync WhatsApp message history from Evolution API.
    Fetches all chats and their messages, linking to existing contacts.

    Args:
        include_groups: If True, also sync group messages where user participated
        limit: Maximum number of chats to process (default 50 to avoid timeout)
        offset: Number of chats to skip (for pagination)
    """
    # User's phone number for filtering group interactions
    MY_PHONE = "5511984153337"

    try:
        all_chats = await whatsapp.get_all_chats(include_groups=include_groups)
    except Exception as e:
        return {"status": "error", "message": f"Erro ao buscar chats: {str(e)}"}

    if not all_chats:
        return {"status": "no_chats", "message": "Nenhum chat encontrado"}

    total_chats = len(all_chats)

    # Apply offset and limit for pagination
    chats = all_chats[offset:offset + limit]

    if not chats:
        return {"status": "complete", "message": "Todos os chats foram processados", "total_chats": total_chats}

    conn = get_connection()
    cursor = conn.cursor()

    stats = {
        "individual_chats": 0,
        "groups_processed": 0,
        "messages_imported": 0,
        "messages_skipped": 0,
        "contacts_matched": 0,
        "errors": []
    }

    try:
        # PRE-LOAD: All contacts with phones into memory for fast lookup
        cursor.execute("""
            SELECT id, nome, telefones
            FROM contacts
            WHERE telefones IS NOT NULL AND telefones::text != '[]'
        """)
        all_contacts = cursor.fetchall()

        # Build phone lookup dict: last 8 digits -> contact
        phone_to_contact = {}
        for c in all_contacts:
            phones = c['telefones'] if isinstance(c['telefones'], list) else []
            for p in phones:
                phone_num = p.get('number', '') if isinstance(p, dict) else str(p)
                # Normalize: keep only digits, take last 8
                digits = ''.join(filter(str.isdigit, phone_num))
                if len(digits) >= 8:
                    phone_to_contact[digits[-8:]] = {'id': c['id'], 'nome': c['nome']}

        # PRE-LOAD: All existing message IDs to avoid duplicates
        cursor.execute("SELECT metadata->>'message_id' as msg_id FROM messages WHERE metadata->>'message_id' IS NOT NULL")
        existing_msg_ids = {row['msg_id'] for row in cursor.fetchall()}

        # PRE-LOAD: All existing conversations
        cursor.execute("SELECT id, contact_id FROM conversations WHERE canal = 'whatsapp'")
        contact_to_conversation = {row['contact_id']: row['id'] for row in cursor.fetchall()}

        for chat in chats:
            is_group = chat.get("_is_group", False)

            if is_group:
                # Process group - only my interactions
                group_id = chat.get("_group_id")
                group_name = chat.get("_group_name", "Grupo")

                if not group_id:
                    continue

                # Fetch only messages where I participated
                messages = await whatsapp.get_group_messages(group_id, MY_PHONE, limit=200)

                if not messages:
                    continue

                stats["groups_processed"] += 1

                # Process each group message
                for msg in messages:
                    parsed = whatsapp.parse_group_message(msg, group_name)
                    if not parsed:
                        continue

                    # For outgoing messages (fromMe), skip - we want to track interactions with others
                    if parsed.get("direction") == "outgoing":
                        # Still save the message but linked to the context
                        pass

                    # For incoming messages, find the contact who sent it
                    participant_phone = parsed.get("phone")
                    if not participant_phone:
                        continue

                    # Fast in-memory contact lookup for groups
                    part_digits = ''.join(filter(str.isdigit, participant_phone))
                    part_key = part_digits[-8:] if len(part_digits) >= 8 else part_digits

                    contact = phone_to_contact.get(part_key)
                    if not contact:
                        # Skip messages from unknown contacts in groups
                        continue

                    contact_id = contact['id']
                    stats["contacts_matched"] += 1

                    # Fast in-memory message existence check
                    message_id_ext = parsed.get("message_id")
                    if message_id_ext in existing_msg_ids:
                        stats["messages_skipped"] += 1
                        continue

                    # Fast in-memory conversation lookup
                    if contact_id in contact_to_conversation:
                        conversation_id = contact_to_conversation[contact_id]
                    else:
                        cursor.execute("""
                            INSERT INTO conversations (contact_id, canal, ultimo_mensagem, total_mensagens)
                            VALUES (%s, 'whatsapp', NOW(), 0)
                            RETURNING id
                        """, (contact_id,))
                        conversation_id = cursor.fetchone()['id']
                        contact_to_conversation[contact_id] = conversation_id

                    # Insert group message
                    cursor.execute("""
                        INSERT INTO messages (conversation_id, contact_id, direcao, conteudo, enviado_em, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        conversation_id,
                        contact_id,
                        parsed.get("direction"),
                        parsed.get("content"),
                        parsed.get("timestamp"),
                        json.dumps({
                            "phone": participant_phone,
                            "push_name": parsed.get("push_name"),
                            "message_id": message_id_ext,
                            "message_type": parsed.get("message_type"),
                            "is_group": True,
                            "group_name": group_name
                        })
                    ))
                    stats["messages_imported"] += 1
                    existing_msg_ids.add(message_id_ext)  # Track for this run

            else:
                # Process individual chat (existing logic)
                phone = chat.get("_phone")
                if not phone:
                    continue

                push_name = chat.get("name") or chat.get("pushName") or ""

                # Fast in-memory lookup instead of database query
                phone_digits = ''.join(filter(str.isdigit, phone))
                phone_key = phone_digits[-8:] if len(phone_digits) >= 8 else phone_digits

                contact = phone_to_contact.get(phone_key)
                if not contact:
                    # Skip chats without matching contact
                    continue

                contact_id = contact['id']
                stats["contacts_matched"] += 1

                # Fetch messages for this chat (reduced limit for speed)
                messages = await whatsapp.get_messages_for_chat(phone, limit=50)

                if not messages:
                    continue

                stats["individual_chats"] += 1

                # Fast in-memory conversation lookup
                if contact_id in contact_to_conversation:
                    conversation_id = contact_to_conversation[contact_id]
                else:
                    cursor.execute("""
                        INSERT INTO conversations (contact_id, canal, ultimo_mensagem, total_mensagens)
                        VALUES (%s, 'whatsapp', NOW(), 0)
                        RETURNING id
                    """, (contact_id,))
                    conversation_id = cursor.fetchone()['id']
                    contact_to_conversation[contact_id] = conversation_id

                # Process each message
                for msg in messages:
                    parsed = whatsapp.parse_stored_message(msg)
                    if not parsed:
                        continue

                    message_id_ext = parsed.get("message_id")
                    content = parsed.get("content")
                    direction = parsed.get("direction")
                    timestamp = parsed.get("timestamp")

                    # Fast in-memory check instead of database query
                    if message_id_ext in existing_msg_ids:
                        stats["messages_skipped"] += 1
                        continue

                    # Insert message
                    cursor.execute("""
                        INSERT INTO messages (conversation_id, contact_id, direcao, conteudo, enviado_em, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        conversation_id,
                        contact_id,
                        direction,
                        content,
                        timestamp,
                        json.dumps({
                            "phone": phone,
                            "push_name": push_name,
                            "message_id": message_id_ext,
                            "message_type": parsed.get("message_type"),
                            "is_group": False
                        })
                    ))
                    stats["messages_imported"] += 1
                    existing_msg_ids.add(message_id_ext)  # Track for this run

                # Update stats with simpler incremental updates (avoid deadlocks)
                if stats["messages_imported"] > 0 and conversation_id:
                    cursor.execute("""
                        UPDATE conversations
                        SET total_mensagens = total_mensagens + 1,
                            ultimo_mensagem = NOW()
                        WHERE id = %s
                    """, (conversation_id,))

        conn.commit()

        # Update contact ultimo_contato in a separate transaction (avoid deadlocks)
        conn2 = get_connection()
        cursor2 = conn2.cursor()
        try:
            # Get distinct contact IDs that were updated
            updated_contacts = set()
            for chat in chats:
                phone = chat.get("_phone")
                if phone:
                    digits = ''.join(filter(str.isdigit, phone))
                    key = digits[-8:] if len(digits) >= 8 else digits
                    contact = phone_to_contact.get(key)
                    if contact:
                        updated_contacts.add(contact['id'])

            for cid in updated_contacts:
                cursor2.execute("""
                    UPDATE contacts
                    SET ultimo_contato = NOW()
                    WHERE id = %s
                """, (cid,))
            conn2.commit()
        except Exception:
            conn2.rollback()
        finally:
            cursor2.close()
            conn2.close()

    except Exception as e:
        conn.rollback()
        stats["errors"].append(str(e))
    finally:
        cursor.close()
        conn.close()

    return {
        "status": "ok",
        "total_chats_available": total_chats,
        "chats_processed_this_run": len(chats),
        "offset_used": offset,
        "next_offset": offset + len(chats),
        "remaining": max(0, total_chats - offset - len(chats)),
        **stats
    }


@app.post("/api/whatsapp/relink")
async def relink_whatsapp_messages():
    """
    Re-link WhatsApp messages to contacts based on phone number.
    Useful when contacts were updated after messages were synced.
    """
    conn = get_connection()
    cursor = conn.cursor()

    stats = {
        "messages_checked": 0,
        "messages_linked": 0,
        "conversations_created": 0,
        "errors": []
    }

    try:
        # Build phone lookup dict from all contacts
        cursor.execute("""
            SELECT id, nome, telefones
            FROM contacts
            WHERE telefones IS NOT NULL AND telefones::text != '[]'
        """)
        all_contacts = cursor.fetchall()

        phone_to_contact = {}
        for c in all_contacts:
            phones = c['telefones'] if isinstance(c['telefones'], list) else []
            for p in phones:
                phone_num = p.get('number', '') if isinstance(p, dict) else str(p)
                digits = ''.join(filter(str.isdigit, phone_num))
                if len(digits) >= 8:
                    phone_to_contact[digits[-8:]] = {'id': c['id'], 'nome': c['nome']}

        # Get existing conversations
        cursor.execute("SELECT id, contact_id FROM conversations WHERE canal = 'whatsapp'")
        contact_to_conversation = {row['contact_id']: row['id'] for row in cursor.fetchall()}

        # Find all unlinked WhatsApp messages (contact_id is NULL)
        cursor.execute("""
            SELECT id, metadata->>'phone' as phone
            FROM messages
            WHERE metadata->>'is_group' IS NOT NULL
            AND contact_id IS NULL
        """)
        unlinked_messages = cursor.fetchall()

        stats["messages_checked"] = len(unlinked_messages)

        for msg in unlinked_messages:
            phone = msg['phone']
            if not phone:
                continue

            # Normalize phone
            digits = ''.join(filter(str.isdigit, phone))
            phone_key = digits[-8:] if len(digits) >= 8 else digits

            contact = phone_to_contact.get(phone_key)
            if not contact:
                continue

            contact_id = contact['id']

            # Find or create conversation
            if contact_id in contact_to_conversation:
                conversation_id = contact_to_conversation[contact_id]
            else:
                cursor.execute("""
                    INSERT INTO conversations (contact_id, canal, ultimo_mensagem, total_mensagens)
                    VALUES (%s, 'whatsapp', NOW(), 0)
                    RETURNING id
                """, (contact_id,))
                conversation_id = cursor.fetchone()['id']
                contact_to_conversation[contact_id] = conversation_id
                stats["conversations_created"] += 1

            # Update the message
            cursor.execute("""
                UPDATE messages
                SET contact_id = %s, conversation_id = %s
                WHERE id = %s
            """, (contact_id, conversation_id, msg['id']))

            stats["messages_linked"] += 1

        # Update conversation stats
        cursor.execute("""
            UPDATE conversations c
            SET total_mensagens = (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id),
                ultimo_mensagem = (SELECT MAX(enviado_em) FROM messages m WHERE m.conversation_id = c.id)
            WHERE c.canal = 'whatsapp'
        """)

        conn.commit()

    except Exception as e:
        conn.rollback()
        stats["errors"].append(str(e))
    finally:
        cursor.close()
        conn.close()

    return {"status": "ok", **stats}


@app.post("/api/whatsapp/fix-contact")
async def fix_whatsapp_contact(request: Request):
    """
    Fix messages linked to wrong contact.
    Moves all messages with a specific phone to the correct contact.

    Body:
    - phone: Phone number (last 8 digits used for matching)
    - correct_contact_id: The correct contact ID to link messages to
    """
    body = await request.json()
    phone = body.get('phone', '')
    correct_contact_id = body.get('correct_contact_id')

    if not phone or not correct_contact_id:
        raise HTTPException(status_code=400, detail="phone e correct_contact_id são obrigatórios")

    # Normalize phone to last 8 digits
    phone_digits = ''.join(filter(str.isdigit, phone))
    phone_suffix = phone_digits[-8:] if len(phone_digits) >= 8 else phone_digits

    conn = get_connection()
    cursor = conn.cursor()

    stats = {"messages_fixed": 0, "conversation_created": False}

    try:
        # Find or create conversation for correct contact
        cursor.execute("""
            SELECT id FROM conversations
            WHERE contact_id = %s AND canal = 'whatsapp'
        """, (correct_contact_id,))
        conv = cursor.fetchone()

        if conv:
            conversation_id = conv['id']
        else:
            cursor.execute("""
                INSERT INTO conversations (contact_id, canal, ultimo_mensagem, total_mensagens)
                VALUES (%s, 'whatsapp', NOW(), 0)
                RETURNING id
            """, (correct_contact_id,))
            conversation_id = cursor.fetchone()['id']
            stats["conversation_created"] = True

        # Update all messages with this phone number
        cursor.execute("""
            UPDATE messages
            SET contact_id = %s, conversation_id = %s
            WHERE metadata->>'phone' LIKE %s
            RETURNING id
        """, (correct_contact_id, conversation_id, f'%{phone_suffix}%'))

        stats["messages_fixed"] = cursor.rowcount

        # Update conversation stats
        cursor.execute("""
            UPDATE conversations
            SET total_mensagens = (SELECT COUNT(*) FROM messages WHERE conversation_id = %s),
                ultimo_mensagem = (SELECT MAX(enviado_em) FROM messages WHERE conversation_id = %s)
            WHERE id = %s
        """, (conversation_id, conversation_id, conversation_id))

        # Clean up old empty conversations
        cursor.execute("""
            DELETE FROM conversations
            WHERE canal = 'whatsapp' AND id != %s
            AND NOT EXISTS (SELECT 1 FROM messages WHERE conversation_id = conversations.id)
        """, (conversation_id,))

        conn.commit()

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()

    return {"status": "ok", **stats}


@app.get("/api/whatsapp/chats")
async def get_whatsapp_chats():
    """
    List all WhatsApp chats/conversations from Evolution API.
    """
    chats = await whatsapp.get_all_chats()

    # Format for display
    formatted = []
    for chat in chats:
        chat_id = chat.get("id", "")
        if not chat_id.endswith("@s.whatsapp.net"):
            continue

        phone = chat_id.replace("@s.whatsapp.net", "")
        formatted.append({
            "phone": phone,
            "phone_display": format_phone_display(phone),
            "name": chat.get("name") or chat.get("pushName") or "",
            "unread_count": chat.get("unreadCount", 0)
        })

    return {"chats": formatted, "total": len(formatted)}


@app.post("/api/contacts/{contact_id}/extract-facts")
async def extract_contact_facts(contact_id: int):
    """
    Use AI to extract relevant facts from a contact's messages.
    Requires ANTHROPIC_API_KEY environment variable.
    """
    import httpx

    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Get contact info
        cursor.execute("SELECT id, nome FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")

        contact_name = contact['nome']

        # Get recent messages for this contact
        cursor.execute("""
            SELECT conteudo, direcao, enviado_em, metadata
            FROM messages
            WHERE contact_id = %s
            ORDER BY enviado_em DESC
            LIMIT 50
        """, (contact_id,))

        messages = cursor.fetchall()
        if not messages:
            return {"status": "no_messages", "facts": []}

        # Format messages for AI
        messages_text = []
        for msg in messages:
            direction = "Eu" if msg['direcao'] == 'outgoing' else contact_name
            date = msg['enviado_em'].strftime("%d/%m/%Y") if msg['enviado_em'] else ""
            metadata = msg['metadata'] or {}
            group_name = metadata.get('group_name', '')
            source = f" (grupo: {group_name})" if group_name else ""
            messages_text.append(f"[{date}] {direction}{source}: {msg['conteudo']}")

        conversation = "\n".join(messages_text)

        # Call Anthropic API
        prompt = f"""Analise as mensagens abaixo entre mim e {contact_name} e extraia fatos relevantes sobre essa pessoa.

Fatos relevantes incluem:
- Informacoes profissionais (cargo, empresa, projetos)
- Informacoes pessoais (familia, viagens, hobbies)
- Interesses e preferencias
- Pedidos ou compromissos mencionados
- Eventos importantes na vida da pessoa

Ignore:
- Mensagens genericas (bom dia, ok, etc)
- Links sem contexto
- Memes e figurinhas

Retorne em formato JSON com a estrutura:
{{
  "facts": [
    {{"categoria": "professional|personal|interest|commitment", "fato": "descricao do fato", "confianca": 0.0-1.0}}
  ]
}}

Mensagens:
{conversation}

Retorne APENAS o JSON, sem explicacoes."""

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30.0
            )

            if response.status_code != 200:
                raise HTTPException(status_code=500, detail=f"AI API error: {response.text}")

            ai_response = response.json()
            content = ai_response.get("content", [{}])[0].get("text", "{}")

            # Parse JSON response
            try:
                facts_data = json.loads(content)
                facts = facts_data.get("facts", [])
            except json.JSONDecodeError:
                # Try to extract JSON from response
                import re
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    facts_data = json.loads(json_match.group())
                    facts = facts_data.get("facts", [])
                else:
                    facts = []

        # Save facts to database
        saved_facts = []
        for fact in facts:
            categoria = fact.get("categoria", "general")
            fato = fact.get("fato", "")
            confianca = fact.get("confianca", 0.8)

            if not fato:
                continue

            # Check if fact already exists
            cursor.execute("""
                SELECT id FROM contact_facts
                WHERE contact_id = %s AND fato = %s
                LIMIT 1
            """, (contact_id, fato))

            if cursor.fetchone():
                continue

            cursor.execute("""
                INSERT INTO contact_facts (contact_id, categoria, fato, fonte, confianca)
                VALUES (%s, %s, %s, 'whatsapp_ai', %s)
                RETURNING id
            """, (contact_id, categoria, fato, confianca))

            fact_id = cursor.fetchone()['id']
            saved_facts.append({
                "id": fact_id,
                "categoria": categoria,
                "fato": fato,
                "confianca": confianca
            })

        conn.commit()

        return {
            "status": "ok",
            "contact_name": contact_name,
            "messages_analyzed": len(messages),
            "facts_extracted": len(facts),
            "facts_saved": len(saved_facts),
            "facts": saved_facts
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()


@app.get("/api/contacts/{contact_id}/facts")
async def get_contact_facts(contact_id: int):
    """Get all facts for a contact"""
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT id, categoria, fato, fonte, confianca, verificado, criado_em
            FROM contact_facts
            WHERE contact_id = %s
            ORDER BY criado_em DESC
        """, (contact_id,))

        facts = [dict(row) for row in cursor.fetchall()]

        return {"contact_id": contact_id, "facts": facts}

    finally:
        cursor.close()
        conn.close()


@app.delete("/api/contacts/facts/{fact_id}")
async def delete_contact_fact(fact_id: int):
    """Delete a specific fact"""
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM contact_facts WHERE id = %s", (fact_id,))
        conn.commit()
        return {"status": "deleted", "fact_id": fact_id}
    finally:
        cursor.close()
        conn.close()


@app.get("/api/fathom/sync")
async def sync_fathom_meetings():
    """Sincroniza reuniões recentes do Fathom"""
    if not FATHOM_API_KEY:
        return {"status": "Fathom API key not configured"}

    try:
        processed = await fathom.process_recent_meetings(since_hours=48)
        return {
            "status": "synced",
            "meetings_processed": len(processed),
            "meetings": processed
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


class FathomLinkRequest(BaseModel):
    fathom_url: str
    titulo: Optional[str] = None
    descricao: Optional[str] = None
    data_interacao: Optional[datetime] = None


@app.post("/api/prospects/{prospect_id}/fathom/link")
async def link_fathom_meeting(prospect_id: int, request: FathomLinkRequest):
    """
    Vincula uma reunião do Fathom ao prospect e cria interação na timeline

    Aceita URL de compartilhamento do Fathom e extrai dados automaticamente
    """
    conn = get_db()
    cursor = conn.cursor()

    # Verificar se prospect existe
    cursor.execute("SELECT id, nome FROM prospects WHERE id = %s", (prospect_id,))
    prospect = cursor.fetchone()
    if not prospect:
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    # Extrair dados do Fathom
    fathom_data = await fathom.extract_from_share_link(request.fathom_url)

    titulo = request.titulo or (fathom_data.get("title") if fathom_data else "Reunião Fathom")
    descricao = request.descricao or (fathom_data.get("summary") if fathom_data else "")
    data_interacao = request.data_interacao or datetime.now()

    if fathom_data and fathom_data.get("date"):
        try:
            data_interacao = datetime.fromisoformat(fathom_data["date"].replace("Z", "+00:00"))
        except:
            pass

    # Criar interação na timeline
    cursor.execute('''
        INSERT INTO interactions (prospect_id, tipo, titulo, descricao, data_interacao,
                                  fathom_link, fathom_summary, tags, sentimento)
        VALUES (%s, 'reuniao', %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (
        prospect_id,
        titulo,
        descricao,
        data_interacao.isoformat() if hasattr(data_interacao, 'isoformat') else str(data_interacao),
        request.fathom_url,
        fathom_data.get("summary", "") if fathom_data else "",
        json.dumps(["fathom"]),
        "positivo" if fathom_data else None
    ))

    interaction_id = cursor.fetchone()['id']

    # Atualizar data_ultimo_contato do prospect
    cursor.execute('''
        UPDATE prospects SET data_ultimo_contato = %s, fathom_meeting_id = %s
        WHERE id = %s
    ''', (
        data_interacao.isoformat() if hasattr(data_interacao, 'isoformat') else str(data_interacao),
        fathom_data.get("call_id") if fathom_data else None,
        prospect_id
    ))

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (%s, 'Sistema', 'Fathom Vinculado', %s)
    ''', (prospect_id, f"Reunião: {titulo}"))

    conn.commit()
    conn.close()

    return {
        "status": "linked",
        "interaction_id": interaction_id,
        "fathom_data": fathom_data
    }


@app.get("/api/fathom/unlinked")
async def get_unlinked_fathom_meetings():
    """Lista reuniões do Fathom que ainda não foram vinculadas a prospects"""
    if not FATHOM_API_KEY:
        return {"status": "Fathom API key not configured", "meetings": []}

    conn = get_db()
    cursor = conn.cursor()

    # Buscar IDs de reuniões já vinculadas
    cursor.execute('''
        SELECT DISTINCT fathom_meeting_id FROM prospects
        WHERE fathom_meeting_id IS NOT NULL
    ''')
    linked_ids = [row['fathom_meeting_id'] for row in cursor.fetchall()]

    # Buscar emails de prospects para sugestão de match
    cursor.execute('SELECT id, nome, email FROM prospects WHERE email IS NOT NULL')
    prospects_emails = [row_to_dict(row) for row in cursor.fetchall()]

    conn.close()

    try:
        unlinked = await fathom.get_unlinked_meetings(linked_ids)

        # Adicionar sugestões de match
        for meeting in unlinked:
            suggestion = await fathom.suggest_prospect_match(meeting, prospects_emails)
            meeting['suggested_prospect'] = suggestion

        return {"meetings": unlinked}
    except Exception as e:
        return {"status": "error", "message": str(e), "meetings": []}


# ============== API Routes - LinkedIn & Enrichment ==============

class LinkedInUpdate(BaseModel):
    linkedin_url: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    connections: Optional[int] = None
    notes: Optional[str] = None

class LinkedInPostAdd(BaseModel):
    post_url: str
    post_text: str
    post_date: Optional[str] = None
    engagement: Optional[int] = None

class RelacionamentoUpdate(BaseModel):
    tipo: Optional[str] = None  # 'colega_board_academy', 'ex_cliente', 'indicacao', etc
    conhece_desde: Optional[str] = None
    conexoes_comuns: Optional[List[str]] = None
    notas: Optional[str] = None


@app.put("/api/prospects/{prospect_id}/linkedin")
async def update_prospect_linkedin(prospect_id: int, data: LinkedInUpdate):
    """Atualiza dados do LinkedIn do prospect"""
    conn = get_db()
    cursor = conn.cursor()

    # Buscar dados atuais
    cursor.execute("SELECT dados_enriquecidos, linkedin FROM prospects WHERE id = %s", (prospect_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    # Parse dados existentes
    try:
        enriched = json.loads(row['dados_enriquecidos']) if row['dados_enriquecidos'] else {}
    except:
        enriched = {}

    # Atualizar dados LinkedIn
    linkedin_data = enriched.get("linkedin", {})
    if data.linkedin_url:
        linkedin_data["url"] = linkedin.normalize_linkedin_url(data.linkedin_url)
        linkedin_data["username"] = linkedin.extract_linkedin_username(data.linkedin_url)
    if data.headline:
        linkedin_data["headline"] = data.headline
    if data.location:
        linkedin_data["location"] = data.location
    if data.connections:
        linkedin_data["connections"] = data.connections
    if data.notes:
        linkedin_data["notes"] = data.notes

    linkedin_data["last_updated"] = datetime.now().isoformat()
    enriched["linkedin"] = linkedin_data

    # Atualizar prospect
    cursor.execute('''
        UPDATE prospects
        SET dados_enriquecidos = %s,
            linkedin = COALESCE(%s, linkedin)
        WHERE id = %s
    ''', (
        json.dumps(enriched),
        linkedin.normalize_linkedin_url(data.linkedin_url) if data.linkedin_url else None,
        prospect_id
    ))

    conn.commit()
    conn.close()

    return {"status": "updated", "linkedin_data": linkedin_data}


@app.post("/api/prospects/{prospect_id}/linkedin/posts")
async def add_linkedin_post(prospect_id: int, post: LinkedInPostAdd):
    """Adiciona uma publicação relevante do LinkedIn"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT dados_enriquecidos FROM prospects WHERE id = %s", (prospect_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    try:
        enriched = json.loads(row['dados_enriquecidos']) if row['dados_enriquecidos'] else {}
    except:
        enriched = {}

    linkedin_data = enriched.get("linkedin", {})
    linkedin_data = linkedin.add_post(
        linkedin_data,
        post.post_url,
        post.post_text,
        post.post_date,
        post.engagement
    )

    enriched["linkedin"] = linkedin_data

    cursor.execute('''
        UPDATE prospects SET dados_enriquecidos = %s WHERE id = %s
    ''', (json.dumps(enriched), prospect_id))

    conn.commit()
    conn.close()

    return {"status": "added", "posts_count": len(linkedin_data.get("posts", []))}


@app.put("/api/prospects/{prospect_id}/relacionamento")
async def update_prospect_relacionamento(prospect_id: int, data: RelacionamentoUpdate):
    """Atualiza informações de relacionamento com o prospect"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT dados_enriquecidos FROM prospects WHERE id = %s", (prospect_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    try:
        enriched = json.loads(row['dados_enriquecidos']) if row['dados_enriquecidos'] else {}
    except:
        enriched = {}

    relacionamento = enriched.get("relacionamento", {})

    if data.tipo:
        relacionamento["tipo"] = data.tipo
    if data.conhece_desde:
        relacionamento["conhece_desde"] = data.conhece_desde
    if data.conexoes_comuns:
        relacionamento["conexoes_comuns"] = data.conexoes_comuns
    if data.notas:
        relacionamento["notas"] = data.notas

    relacionamento["last_updated"] = datetime.now().isoformat()
    enriched["relacionamento"] = relacionamento

    cursor.execute('''
        UPDATE prospects SET dados_enriquecidos = %s WHERE id = %s
    ''', (json.dumps(enriched), prospect_id))

    conn.commit()
    conn.close()

    return {"status": "updated", "relacionamento": relacionamento}


@app.get("/api/prospects/{prospect_id}/followup-suggestions")
async def get_followup_suggestions(prospect_id: int):
    """Retorna sugestões de follow-up baseadas no contexto do prospect"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM prospects WHERE id = %s", (prospect_id,))
    prospect = row_to_dict(cursor.fetchone())

    if not prospect:
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    # Buscar interações recentes
    cursor.execute('''
        SELECT * FROM interactions
        WHERE prospect_id = %s
        ORDER BY data_interacao DESC LIMIT 5
    ''', (prospect_id,))
    interactions = [row_to_dict(r) for r in cursor.fetchall()]

    conn.close()

    suggestions = []

    # 1. Baseado em tempo desde último contato
    if prospect.get('data_ultimo_contato'):
        try:
            last_contact = datetime.fromisoformat(str(prospect['data_ultimo_contato']).replace('Z', '+00:00'))
            days_since = (datetime.now(last_contact.tzinfo) - last_contact).days if last_contact.tzinfo else (datetime.now() - last_contact).days
        except:
            days_since = 999

        if days_since > 30:
            suggestions.append({
                "priority": "high",
                "type": "reengagement",
                "reason": f"Sem contato há {days_since} dias",
                "action": "Reengajar contato - enviar mensagem personalizada"
            })
        elif days_since > 7:
            suggestions.append({
                "priority": "medium",
                "type": "followup",
                "reason": f"{days_since} dias sem contato",
                "action": "Fazer follow-up da última conversa"
            })
    else:
        suggestions.append({
            "priority": "high",
            "type": "first_contact",
            "reason": "Novo prospect",
            "action": "Fazer primeiro contato"
        })

    # 2. Baseado no outcome da reunião
    if prospect.get('meeting_outcome'):
        outcome = prospect['meeting_outcome']
        if outcome == 'muito_interessado':
            suggestions.append({
                "priority": "high",
                "type": "proposal",
                "reason": "Alto interesse demonstrado",
                "action": "Enviar proposta comercial"
            })
        elif outcome == 'interessado':
            suggestions.append({
                "priority": "medium",
                "type": "nurture",
                "reason": "Interesse moderado",
                "action": "Enviar material adicional ou case relevante"
            })

    # 3. Baseado em dados do LinkedIn
    try:
        enriched = json.loads(prospect.get('dados_enriquecidos', '{}'))
        linkedin_data = enriched.get('linkedin', {})

        if linkedin_data.get('posts'):
            recent_posts = [p for p in linkedin_data['posts'][:3]]
            if recent_posts:
                suggestions.append({
                    "priority": "medium",
                    "type": "engagement",
                    "reason": "Publicações recentes no LinkedIn",
                    "action": f"Comentar publicação: {recent_posts[0].get('text', '')[:50]}...",
                    "url": recent_posts[0].get('url')
                })

        # Baseado em relacionamento
        relacionamento = enriched.get('relacionamento', {})
        if relacionamento.get('tipo'):
            suggestions.append({
                "priority": "low",
                "type": "relationship",
                "reason": f"Tipo: {relacionamento['tipo']}",
                "action": f"Mencionar conexão ({relacionamento['tipo']}) no contato"
            })
    except:
        pass

    # 4. Baseado em interações recentes
    if interactions:
        last_interaction = interactions[0]
        if last_interaction.get('fathom_link') and not last_interaction.get('fathom_summary'):
            suggestions.append({
                "priority": "low",
                "type": "documentation",
                "reason": "Reunião sem resumo",
                "action": "Adicionar resumo e próximos passos da reunião"
            })

    # Ordenar por prioridade
    priority_order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 3))

    return {"suggestions": suggestions}


# ============== API Routes - Analytics & ICP ==============

@app.get("/api/analytics/dashboard")
async def get_dashboard_stats(user_role: str = "operador"):
    """Estatísticas para dashboard (filtra por aprovados para Andressa)"""
    conn = get_db()
    cursor = conn.cursor()

    stats = {}

    # Filtro base para Andressa
    aprovado_filter = "AND aprovado_por_renato = TRUE" if user_role != "admin" else ""

    # Total por tier (só aprovados)
    cursor.execute(f'''
        SELECT tier, COUNT(*) as count FROM prospects
        WHERE 1=1 {aprovado_filter}
        GROUP BY tier
    ''')
    stats['por_tier'] = {row['tier']: row['count'] for row in cursor.fetchall()}

    # Total por status (só aprovados)
    cursor.execute(f'''
        SELECT status, COUNT(*) as count FROM prospects
        WHERE status NOT IN ('pendente_aprovacao', 'rejeitado') {aprovado_filter}
        GROUP BY status
    ''')
    stats['por_status'] = {row['status']: row['count'] for row in cursor.fetchall()}

    # Conversões
    cursor.execute(f'SELECT COUNT(*) as count FROM prospects WHERE converted = TRUE {aprovado_filter}')
    stats['total_convertidos'] = cursor.fetchone()['count']

    cursor.execute(f'SELECT COALESCE(SUM(deal_value), 0) as total FROM prospects WHERE converted = TRUE {aprovado_filter}')
    stats['receita_total'] = float(cursor.fetchone()['total'])

    # Reuniões
    cursor.execute('SELECT COUNT(*) as count FROM meetings WHERE realizada = TRUE')
    stats['reunioes_realizadas'] = cursor.fetchone()['count']

    cursor.execute('''
        SELECT COUNT(*) as count FROM meetings
        WHERE data_hora > %s AND realizada = FALSE
    ''', (datetime.now().isoformat(),))
    stats['reunioes_agendadas'] = cursor.fetchone()['count']

    # Top prospects para contato (só aprovados para Andressa)
    cursor.execute(f'''
        SELECT * FROM prospects
        WHERE status IN ('novo', 'contatado') AND tier IN ('A', 'B')
        {aprovado_filter}
        ORDER BY prioridade_renato DESC, score DESC
        LIMIT 10
    ''')
    stats['top_prospects'] = [row_to_dict(row) for row in cursor.fetchall()]

    conn.close()
    return stats


@app.get("/api/analytics/icp")
async def get_icp_analysis():
    """Análise do Perfil Ideal de Cliente"""
    return scorer.analyze_icp()


@app.get("/api/analytics/arguments")
async def get_sales_arguments():
    """Argumentos de venda otimizados"""
    return {"arguments": scorer.generate_sales_arguments()}


@app.get("/api/analytics/funnel")
async def get_sales_funnel():
    """Funil de vendas"""
    conn = get_db()
    cursor = conn.cursor()

    funnel_stages = [
        ('Novos', 'novo'),
        ('Contatados', 'contatado'),
        ('Reunião Agendada', 'reuniao_agendada'),
        ('Reunião Realizada', 'reuniao_realizada'),
        ('Negociando', 'negociando'),
        ('Convertidos', 'convertido'),
    ]

    funnel = []
    for label, status in funnel_stages:
        cursor.execute(
            'SELECT COUNT(*) as count FROM prospects WHERE status = %s',
            (status,)
        )
        count = cursor.fetchone()['count']
        funnel.append({"stage": label, "count": count})

    conn.close()
    return {"funnel": funnel}


# ============== Import de dados ==============

class BulkImportData(BaseModel):
    prospects: List[dict]

class BulkNameUpdate(BaseModel):
    updates: List[dict]  # [{email: str, nome: str, empresa: str}]

@app.post("/api/admin/update-names")
async def update_prospect_names(data: BulkNameUpdate):
    """
    Atualiza nomes dos prospects em massa baseado no email
    """
    conn = get_db()
    cursor = conn.cursor()

    updated = 0
    not_found = 0

    for item in data.updates:
        email = item.get('email', '').lower().strip()
        nome = item.get('nome', '').strip()
        empresa = item.get('empresa', '').strip()

        if not email or not nome:
            continue

        # Update by email match
        cursor.execute('''
            UPDATE prospects
            SET nome = %s,
                empresa = COALESCE(NULLIF(%s, ''), empresa)
            WHERE LOWER(email) = %s
        ''', (nome, empresa, email))

        if cursor.rowcount > 0:
            updated += cursor.rowcount
        else:
            not_found += 1

    conn.commit()
    conn.close()

    return {
        "status": "completed",
        "updated": updated,
        "not_found": not_found
    }

@app.post("/api/import/bulk")
async def import_bulk(data: BulkImportData):
    """
    Importa prospects via JSON
    """
    conn = get_db()
    cursor = conn.cursor()

    imported = 0
    errors = 0

    for row in data.prospects:
        try:
            email = row.get('email') or row.get('Email') or None
            nome = row.get('nome') or row.get('Nome', '')
            # Clean nome - remove brackets if present
            if nome.startswith('[') and ']' in nome:
                nome = nome[1:nome.index(']')]

            cursor.execute('''
                INSERT INTO prospects
                (nome, empresa, cargo, email, telefone, score, tier, reasons, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pendente_aprovacao')
            ''', (
                nome,
                row.get('empresa') or row.get('Empresa', ''),
                row.get('cargo') or row.get('Cargo', ''),
                email if email else None,
                row.get('telefone') or row.get('Telefone', ''),
                int(row.get('score') or row.get('Score', 0)),
                (row.get('tier') or row.get('Tier', 'E')).split()[0],
                row.get('reasons') or row.get('Razões de Qualificação', '')
            ))
            imported += 1
        except Exception as e:
            errors += 1
            continue

    conn.commit()
    conn.close()

    return {"status": "imported", "count": imported, "errors": errors}


# ============== API Routes - Contacts (RAP) ==============

class ContactCreate(BaseModel):
    nome: str
    apelido: Optional[str] = None
    empresa: Optional[str] = None
    cargo: Optional[str] = None
    emails: Optional[List[dict]] = []
    telefones: Optional[List[dict]] = []
    linkedin: Optional[str] = None
    contexto: Optional[str] = 'professional'
    categorias: Optional[List[str]] = []
    tags: Optional[List[str]] = []
    aniversario: Optional[str] = None
    google_contact_id: Optional[str] = None
    origem: Optional[str] = 'manual'

class ContactsImportData(BaseModel):
    contacts: List[dict]


@app.get("/api/contacts")
async def list_contacts(
    search: Optional[str] = None,
    q: Optional[str] = None,  # Alias for search
    contexto: Optional[str] = None,
    limit: int = Query(50, le=500),
    offset: int = 0
):
    """Lista todos os contatos com busca"""
    conn = get_db()
    cursor = conn.cursor()

    # Support both 'search' and 'q' parameters
    search_term_raw = search or q

    query = "SELECT * FROM contacts WHERE 1=1"
    params = []

    if search_term_raw:
        # Search in name, company, cargo, and phone numbers
        query += " AND (nome ILIKE %s OR empresa ILIKE %s OR cargo ILIKE %s OR telefones::text ILIKE %s)"
        search_term = f"%{search_term_raw}%"
        params.extend([search_term, search_term, search_term, search_term])

    if contexto:
        query += " AND contexto = %s"
        params.append(contexto)

    query += " ORDER BY nome ASC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()

    # Count total
    count_query = "SELECT COUNT(*) as count FROM contacts WHERE 1=1"
    count_params = []
    if search_term_raw:
        count_query += " AND (nome ILIKE %s OR empresa ILIKE %s OR cargo ILIKE %s OR telefones::text ILIKE %s)"
        count_params.extend([f"%{search_term_raw}%", f"%{search_term_raw}%", f"%{search_term_raw}%", f"%{search_term_raw}%"])
    if contexto:
        count_query += " AND contexto = %s"
        count_params.append(contexto)

    cursor.execute(count_query, count_params if count_params else None)
    total = cursor.fetchone()['count']

    conn.close()

    return {
        "contacts": [row_to_dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset
    }


@app.get("/api/contacts/stats")
async def contacts_stats():
    """Estatísticas dos contatos"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as count FROM contacts")
    total = cursor.fetchone()['count']

    cursor.execute('''
        SELECT contexto, COUNT(*) as count FROM contacts
        GROUP BY contexto
    ''')
    by_context = {row['contexto']: row['count'] for row in cursor.fetchall()}

    cursor.execute('''
        SELECT COUNT(*) as count FROM contacts
        WHERE foto_url IS NOT NULL AND foto_url != ''
    ''')
    with_photo = cursor.fetchone()['count']

    cursor.execute('''
        SELECT COUNT(*) as count FROM contacts
        WHERE linkedin IS NOT NULL AND linkedin != ''
    ''')
    with_linkedin = cursor.fetchone()['count']

    conn.close()

    return {
        "total": total,
        "by_context": by_context,
        "with_photo": with_photo,
        "with_linkedin": with_linkedin
    }


# ============== Contact Deduplication & Normalization ==============
# NOTE: These routes MUST be defined BEFORE /api/contacts/{contact_id}
# to prevent "analyze" etc. from being matched as a contact_id

from services.contact_dedup import (
    analyze_contacts,
    normalize_name,
    apply_name_fixes,
    merge_duplicate_contacts,
    find_duplicates,
    merge_contacts,
    apply_name_fixes_with_propagation,
    merge_duplicate_contacts_with_propagation,
    propagate_contact_to_google
)
import integrations.google_contacts as google_contacts_module


@app.get("/api/contacts/analyze")
async def analyze_contacts_issues(request: Request):
    """Analyze contacts for duplicates, name issues, etc."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, nome, empresa, cargo, emails, telefones, foto_url,
               linkedin, contexto, google_contact_id, origem
        FROM contacts
        ORDER BY nome
    ''')

    contacts = [row_to_dict(row) for row in cursor.fetchall()]
    conn.close()

    analysis = analyze_contacts(contacts)

    return {
        "total_contacts": analysis['total_contacts'],
        "issues_count": analysis['issues_count'],
        "duplicates": len(analysis['duplicates']),
        "caps_lock_names": len(analysis['caps_lock_names']),
        "lowercase_names": len(analysis['lowercase_names']),
        "no_phone": len(analysis['no_phone']),
        "no_email": len(analysis['no_email']),
        "no_name": len(analysis['no_name']),
        "details": {
            "duplicates": analysis['duplicates'][:20],  # Limit for response size
            "caps_lock_names": analysis['caps_lock_names'][:50],
            "lowercase_names": analysis['lowercase_names'][:50]
        }
    }


@app.post("/api/contacts/fix-names")
async def fix_contact_names(request: Request, propagate: bool = True):
    """
    Fix ALL CAPS and lowercase names.
    If propagate=True, also updates Google Contacts.
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin")

    conn = get_db()
    cursor = conn.cursor()

    # Find contacts with name issues
    cursor.execute('''
        SELECT id, nome, empresa, cargo, emails, telefones, google_contact_id, contexto
        FROM contacts
        WHERE nome = UPPER(nome) OR nome = LOWER(nome)
    ''')

    contacts = [row_to_dict(row) for row in cursor.fetchall()]

    if propagate:
        # Use async propagation function
        stats = await apply_name_fixes_with_propagation(
            contacts, conn, google_contacts_module
        )
        conn.close()
        return {
            "fixed": stats['fixed'],
            "total_checked": len(contacts),
            "google_updates": stats.get('google_updates', 0),
            "google_errors": stats.get('google_errors', 0)
        }
    else:
        # Just fix locally
        fixed = 0
        for contact in contacts:
            name = contact.get('nome', '')
            if name:
                new_name = normalize_name(name)
                if new_name != name:
                    cursor.execute(
                        "UPDATE contacts SET nome = %s, atualizado_em = CURRENT_TIMESTAMP WHERE id = %s",
                        (new_name, contact['id'])
                    )
                    fixed += 1

        conn.commit()
        conn.close()
        return {"fixed": fixed, "total_checked": len(contacts)}


@app.post("/api/contacts/merge")
async def merge_contacts_endpoint(request: Request):
    """
    Merge duplicate contacts.
    Also propagates changes to Google Contacts (updates merged, deletes duplicates).
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin")

    body = await request.json()
    contact_ids = body.get('contact_ids', [])
    propagate = body.get('propagate', True)

    if len(contact_ids) < 2:
        raise HTTPException(status_code=400, detail="Precisa de pelo menos 2 contatos para merge")

    conn = get_db()
    cursor = conn.cursor()

    # Get contacts
    cursor.execute(
        "SELECT * FROM contacts WHERE id = ANY(%s)",
        (contact_ids,)
    )
    contacts = [row_to_dict(row) for row in cursor.fetchall()]

    if len(contacts) < 2:
        conn.close()
        raise HTTPException(status_code=404, detail="Contatos nao encontrados")

    if propagate:
        result = await merge_duplicate_contacts_with_propagation(
            contacts, conn, google_contacts_module
        )
    else:
        result = merge_duplicate_contacts(contacts, conn)

    conn.close()
    return result


@app.post("/api/contacts/move-data")
async def move_contact_data(request: Request):
    """
    Move emails and/or phones from one contact to another.
    Used to fix incorrectly merged contacts.

    Body:
    - from_contact_id: Source contact ID
    - to_contact_id: Destination contact ID
    - emails_to_move: List of email addresses to move (optional)
    - phones_to_move: List of phone numbers to move (optional)
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin")

    body = await request.json()
    from_id = body.get('from_contact_id')
    to_id = body.get('to_contact_id')
    emails_to_move = body.get('emails_to_move', [])
    phones_to_move = body.get('phones_to_move', [])

    if not from_id or not to_id:
        raise HTTPException(status_code=400, detail="from_contact_id e to_contact_id são obrigatórios")

    if not emails_to_move and not phones_to_move:
        raise HTTPException(status_code=400, detail="Especifique emails_to_move e/ou phones_to_move")

    conn = get_db()
    cursor = conn.cursor()

    try:
        # Get both contacts
        cursor.execute("SELECT id, nome, emails, telefones FROM contacts WHERE id IN (%s, %s)", (from_id, to_id))
        contacts = {row['id']: row_to_dict(row) for row in cursor.fetchall()}

        if from_id not in contacts or to_id not in contacts:
            raise HTTPException(status_code=404, detail="Contato não encontrado")

        from_contact = contacts[from_id]
        to_contact = contacts[to_id]

        moved = {"emails": [], "phones": []}

        # Move emails
        from_emails = from_contact.get('emails', []) or []
        to_emails = to_contact.get('emails', []) or []

        new_from_emails = []
        for email in from_emails:
            email_addr = email.get('email', '') if isinstance(email, dict) else email
            if email_addr in emails_to_move:
                to_emails.append(email)
                moved["emails"].append(email_addr)
            else:
                new_from_emails.append(email)

        # Move phones
        from_phones = from_contact.get('telefones', []) or []
        to_phones = to_contact.get('telefones', []) or []

        new_from_phones = []
        for phone in from_phones:
            phone_num = phone.get('number', '') if isinstance(phone, dict) else phone
            # Normalize for comparison
            phone_digits = ''.join(filter(str.isdigit, phone_num))
            move_match = any(phone_digits.endswith(p[-8:]) or p[-8:] in phone_digits
                           for p in [''.join(filter(str.isdigit, pn)) for pn in phones_to_move])
            if move_match:
                to_phones.append(phone)
                moved["phones"].append(phone_num)
            else:
                new_from_phones.append(phone)

        # Update contacts
        cursor.execute("""
            UPDATE contacts SET emails = %s, telefones = %s WHERE id = %s
        """, (json.dumps(new_from_emails), json.dumps(new_from_phones), from_id))

        cursor.execute("""
            UPDATE contacts SET emails = %s, telefones = %s WHERE id = %s
        """, (json.dumps(to_emails), json.dumps(to_phones), to_id))

        conn.commit()

        return {
            "status": "ok",
            "from_contact": from_contact.get('nome'),
            "to_contact": to_contact.get('nome'),
            "moved": moved
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()


@app.post("/api/contacts/auto-merge-all")
async def auto_merge_all_duplicates(request: Request):
    """
    Automatically merge duplicates in batches to avoid timeout.
    Use batch_size and offset for pagination.
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin")

    body = await request.json() if request.headers.get('content-type') == 'application/json' else {}
    propagate = body.get('propagate', False)  # Default to False for speed
    batch_size = body.get('batch_size', 50)  # Process 50 groups at a time
    offset = body.get('offset', 0)

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, nome, empresa, cargo, emails, telefones, foto_url,
               linkedin, contexto, google_contact_id
        FROM contacts
    ''')
    contacts = [row_to_dict(row) for row in cursor.fetchall()]

    all_duplicates = find_duplicates(contacts)
    duplicate_keys = list(all_duplicates.keys())
    total_groups = len(duplicate_keys)

    # Get batch of duplicates to process
    batch_keys = duplicate_keys[offset:offset + batch_size]

    merged_count = 0
    deleted_count = 0
    google_updates = 0
    google_errors = 0

    for key in batch_keys:
        dup_contacts = all_duplicates[key]
        if len(dup_contacts) >= 2:
            if propagate:
                result = await merge_duplicate_contacts_with_propagation(
                    dup_contacts, conn, google_contacts_module
                )
                google_prop = result.get('google_propagation', {})
                for account, status in google_prop.get('updates', {}).items():
                    if status.get('status') in ['updated', 'created']:
                        google_updates += 1
                    elif status.get('status') == 'error':
                        google_errors += 1
            else:
                result = merge_duplicate_contacts(dup_contacts, conn)

            if result.get('status') == 'merged':
                merged_count += 1
                deleted_count += len(result.get('deleted_ids', []))

    conn.close()

    next_offset = offset + batch_size
    has_more = next_offset < total_groups

    response = {
        "merged_groups": merged_count,
        "deleted_contacts": deleted_count,
        "batch_processed": len(batch_keys),
        "total_groups": total_groups,
        "offset": offset,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "progress_percent": min(100, int((next_offset / total_groups) * 100)) if total_groups > 0 else 100
    }

    if propagate:
        response["google_updates"] = google_updates
        response["google_errors"] = google_errors

    return response


# ============== LinkedIn Import ==============

@app.post("/api/contacts/linkedin/analyze")
async def analyze_linkedin_csv(request: Request):
    """
    Analisa CSV do LinkedIn e retorna preview das ações.
    Não faz alterações, apenas mostra o que será feito.
    """
    from services.linkedin_import import analyze_linkedin_import

    form = await request.form()
    file = form.get('file')

    if not file:
        raise HTTPException(status_code=400, detail="Arquivo CSV não fornecido")

    content = await file.read()

    # Tentar decodificar (LinkedIn usa UTF-8 ou UTF-16)
    try:
        csv_content = content.decode('utf-8')
    except UnicodeDecodeError:
        try:
            csv_content = content.decode('utf-16')
        except UnicodeDecodeError:
            csv_content = content.decode('latin-1')

    # Buscar contatos existentes
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, empresa, cargo, linkedin, emails FROM contacts")
    existing_contacts = [row_to_dict(r) for r in cursor.fetchall()]
    conn.close()

    # Analisar
    analysis = await analyze_linkedin_import(csv_content, existing_contacts)

    return analysis


@app.post("/api/contacts/linkedin/import")
async def import_linkedin_csv(request: Request):
    """
    Executa importação do CSV do LinkedIn.
    Atualiza contatos existentes e/ou cria novos.
    """
    from services.linkedin_import import parse_linkedin_csv, find_matching_contact, get_updates_needed

    form = await request.form()
    file = form.get('file')
    update_existing = form.get('update_existing', 'true').lower() == 'true'
    create_new = form.get('create_new', 'true').lower() == 'true'

    if not file:
        raise HTTPException(status_code=400, detail="Arquivo CSV não fornecido")

    content = await file.read()

    # Decodificar
    try:
        csv_content = content.decode('utf-8')
    except UnicodeDecodeError:
        try:
            csv_content = content.decode('utf-16')
        except UnicodeDecodeError:
            csv_content = content.decode('latin-1')

    # Parsear conexões
    connections = parse_linkedin_csv(csv_content)

    # Buscar contatos existentes
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, empresa, cargo, linkedin, emails FROM contacts")
    existing_contacts = [row_to_dict(r) for r in cursor.fetchall()]

    results = {
        'updated': 0,
        'created': 0,
        'skipped': 0,
        'errors': [],
        'details': []
    }

    for linkedin_conn in connections:
        try:
            match, score, match_type = find_matching_contact(linkedin_conn, existing_contacts)

            if match and update_existing:
                updates = get_updates_needed(linkedin_conn, match)

                if updates:
                    # Construir UPDATE
                    set_parts = []
                    values = []

                    for upd in updates:
                        field = upd['field']
                        set_parts.append(f"{field} = %s")
                        values.append(upd['new'])

                    if set_parts:
                        values.append(match['id'])
                        cursor.execute(
                            f"UPDATE contacts SET {', '.join(set_parts)} WHERE id = %s",
                            values
                        )

                        results['updated'] += 1
                        results['details'].append({
                            'action': 'updated',
                            'name': match['nome'],
                            'updates': {u['field']: u['new'] for u in updates}
                        })
                else:
                    results['skipped'] += 1

            elif not match and create_new and linkedin_conn['full_name']:
                # Criar novo contato
                emails_json = None
                if linkedin_conn.get('email'):
                    emails_json = json.dumps([{
                        'type': 'linkedin',
                        'email': linkedin_conn['email'],
                        'primary': True
                    }])

                cursor.execute('''
                    INSERT INTO contacts (nome, empresa, cargo, linkedin, emails, origem, contexto, criado_em)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ''', (
                    linkedin_conn['full_name'],
                    linkedin_conn.get('company'),
                    linkedin_conn.get('position'),
                    linkedin_conn.get('linkedin_url'),
                    emails_json,
                    'linkedin',
                    'professional'
                ))

                results['created'] += 1
                results['details'].append({
                    'action': 'created',
                    'name': linkedin_conn['full_name'],
                    'company': linkedin_conn.get('company')
                })

            else:
                results['skipped'] += 1

        except Exception as e:
            results['errors'].append({
                'name': linkedin_conn.get('full_name', 'Unknown'),
                'error': str(e)
            })

    conn.commit()
    conn.close()

    return results


# NOTE: This parameterized route MUST come AFTER the specific routes above
@app.get("/api/contacts/{contact_id}")
async def get_contact(contact_id: int):
    """Obtém detalhes de um contato com timeline"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM contacts WHERE id = %s", (contact_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Contato não encontrado")

    contact = row_to_dict(row)

    # Buscar memórias/interações
    cursor.execute('''
        SELECT * FROM contact_memories
        WHERE contact_id = %s
        ORDER BY data_ocorrencia DESC
        LIMIT 50
    ''', (contact_id,))
    memories = [row_to_dict(r) for r in cursor.fetchall()]

    # Buscar fatos
    cursor.execute('''
        SELECT * FROM contact_facts
        WHERE contact_id = %s
        ORDER BY criado_em DESC
    ''', (contact_id,))
    facts = [row_to_dict(r) for r in cursor.fetchall()]

    # Buscar conversas recentes
    cursor.execute('''
        SELECT * FROM conversations
        WHERE contact_id = %s
        ORDER BY ultimo_mensagem DESC
        LIMIT 10
    ''', (contact_id,))
    conversations = [row_to_dict(r) for r in cursor.fetchall()]

    conn.close()

    return {
        "contact": contact,
        "memories": memories,
        "facts": facts,
        "conversations": conversations
    }


@app.put("/api/contacts/{contact_id}")
async def update_contact(contact_id: int, request: Request):
    """Atualiza um contato"""
    body = await request.json()

    conn = get_db()
    cursor = conn.cursor()

    # Check if contact exists
    cursor.execute("SELECT id FROM contacts WHERE id = %s", (contact_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Contato não encontrado")

    # Build update query dynamically
    allowed_fields = ['nome', 'apelido', 'empresa', 'cargo', 'emails', 'telefones',
                      'linkedin', 'contexto', 'categorias', 'tags', 'aniversario']

    updates = []
    values = []
    for field in allowed_fields:
        if field in body:
            value = body[field]
            # Convert lists/dicts to JSON
            if isinstance(value, (list, dict)):
                value = json.dumps(value)
            updates.append(f"{field} = %s")
            values.append(value)

    if not updates:
        conn.close()
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")

    values.append(contact_id)
    query = f"UPDATE contacts SET {', '.join(updates)}, atualizado_em = NOW() WHERE id = %s RETURNING *"

    cursor.execute(query, values)
    updated = row_to_dict(cursor.fetchone())
    conn.commit()
    conn.close()

    return {"status": "ok", "contact": updated}


@app.post("/api/contacts")
async def create_contact(contact: ContactCreate):
    """Cria novo contato"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO contacts (nome, apelido, empresa, cargo, emails, telefones,
                             linkedin, contexto, categorias, tags, aniversario,
                             google_contact_id, origem)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (
        contact.nome,
        contact.apelido,
        contact.empresa,
        contact.cargo,
        json.dumps(contact.emails),
        json.dumps(contact.telefones),
        contact.linkedin,
        contact.contexto,
        json.dumps(contact.categorias),
        json.dumps(contact.tags),
        contact.aniversario,
        contact.google_contact_id,
        contact.origem
    ))

    contact_id = cursor.fetchone()['id']
    conn.commit()
    conn.close()

    return {"id": contact_id, "status": "created"}


@app.post("/api/contacts/import")
async def import_contacts(data: ContactsImportData):
    """
    Importa contatos do Google Contacts CSV (formato JSON)
    """
    conn = get_db()
    cursor = conn.cursor()

    imported = 0
    errors = 0
    duplicates = 0

    for row in data.contacts:
        try:
            # Build name
            first = row.get('First Name', '').strip()
            middle = row.get('Middle Name', '').strip()
            last = row.get('Last Name', '').strip()
            nome = f"{first} {middle} {last}".strip()
            nome = ' '.join(nome.split())  # Remove extra spaces

            if not nome:
                nome = row.get('Organization Name', '').strip()

            if not nome:
                errors += 1
                continue

            # Build emails array
            emails = []
            for i in range(1, 5):
                email_val = row.get(f'E-mail {i} - Value', '').strip()
                email_type = row.get(f'E-mail {i} - Label', 'other').strip()
                if email_val and '@' in email_val:
                    emails.append({
                        'type': email_type.lower().replace('* ', ''),
                        'email': email_val.lower(),
                        'primary': i == 1
                    })

            # Build phones array
            telefones = []
            for i in range(1, 5):
                phone_val = row.get(f'Phone {i} - Value', '').strip()
                phone_type = row.get(f'Phone {i} - Label', 'other').strip()
                if phone_val:
                    # Clean phone - take first number if multiple
                    phone_val = phone_val.split(':::')[0].strip()
                    telefones.append({
                        'type': phone_type.lower(),
                        'number': phone_val,
                        'whatsapp': 'mobile' in phone_type.lower()
                    })

            # Other fields
            empresa = row.get('Organization Name', '').strip()
            cargo = row.get('Organization Title', '').strip()
            birthday = row.get('Birthday', '').strip()
            notes = row.get('Notes', '').strip()

            # Check for duplicate by google_contact_id or email
            google_id = None  # CSV doesn't have this
            if emails:
                cursor.execute(
                    "SELECT id FROM contacts WHERE emails @> %s::jsonb",
                    (json.dumps([{'email': emails[0]['email']}]),)
                )
                if cursor.fetchone():
                    duplicates += 1
                    continue

            cursor.execute('''
                INSERT INTO contacts (nome, empresa, cargo, emails, telefones,
                                     contexto, origem, aniversario)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                nome,
                empresa,
                cargo,
                json.dumps(emails),
                json.dumps(telefones),
                'professional',
                'google_contacts',
                birthday if birthday and birthday != '0/0/00' else None
            ))
            imported += 1

        except Exception as e:
            errors += 1
            continue

    conn.commit()
    conn.close()

    return {
        "status": "imported",
        "imported": imported,
        "duplicates": duplicates,
        "errors": errors
    }


@app.post("/api/contacts/{contact_id}/enrich")
async def enrich_contact(contact_id: int, request: Request):
    """
    Enriquece contato com AI analisando emails e WhatsApp.
    Gera resumo, fatos importantes e insights do relacionamento.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    conn = get_db()
    cursor = conn.cursor()

    # Verify contact exists
    cursor.execute('SELECT id, nome FROM contacts WHERE id = %s', (contact_id,))
    contact = cursor.fetchone()
    if not contact:
        conn.close()
        raise HTTPException(status_code=404, detail="Contato nao encontrado")

    # Mark as enriching
    cursor.execute('''
        UPDATE contacts SET enriquecimento_status = 'pending'
        WHERE id = %s
    ''', (contact_id,))
    conn.commit()

    try:
        # Import and run enrichment service
        from services.contact_enrichment import enrich_and_save
        result = await enrich_and_save(contact_id, conn)

        if result.get("status") == "success":
            return {
                "status": "success",
                "resumo": result.get("enrichment", {}).get("resumo", ""),
                "fatos": result.get("enrichment", {}).get("fatos", []),
                "insights": result.get("enrichment", {}).get("insights", {}),
                "sugestoes": result.get("enrichment", {}).get("sugestoes", []),
                "save_stats": result.get("save_stats", {})
            }
        else:
            # Mark as failed
            cursor.execute('''
                UPDATE contacts SET enriquecimento_status = 'failed'
                WHERE id = %s
            ''', (contact_id,))
            conn.commit()
            return {
                "status": "error",
                "error": result.get("error", "Erro desconhecido no enriquecimento")
            }
    except Exception as e:
        cursor.execute('''
            UPDATE contacts SET enriquecimento_status = 'failed'
            WHERE id = %s
        ''', (contact_id,))
        conn.commit()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


# ============== Google Accounts Integration ==============

from integrations.google_contacts import (
    get_connect_url,
    exchange_code_for_tokens,
    refresh_access_token,
    get_user_email,
    sync_contacts_from_google,
    sync_contacts_incremental,
    CONTACTS_SCOPES
)


@app.get("/api/google/accounts")
async def list_google_accounts(request: Request):
    """Lista contas Google conectadas"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, email, tipo, conectado, ultima_sync, criado_em
        FROM google_accounts
        ORDER BY tipo
    ''')
    accounts = [row_to_dict(row) for row in cursor.fetchall()]
    conn.close()

    return {"accounts": accounts}


@app.get("/api/google/connect/{account_type}")
async def connect_google_account(request: Request, account_type: str):
    """
    Inicia OAuth para conectar uma conta Google
    account_type: 'professional' ou 'personal'
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode conectar contas")

    if account_type not in ["professional", "personal"]:
        raise HTTPException(status_code=400, detail="Tipo deve ser 'professional' ou 'personal'")

    auth_url = get_connect_url(account_type)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/api/google/callback")
async def google_accounts_callback(request: Request):
    """Callback do OAuth para contas Google"""
    code = request.query_params.get("code")
    state = request.query_params.get("state", "professional")  # account_type
    error = request.query_params.get("error")

    if error:
        return RedirectResponse(
            url=f"/rap/settings?error={error}",
            status_code=302
        )

    if not code:
        return RedirectResponse(
            url="/rap/settings?error=no_code",
            status_code=302
        )

    try:
        # Exchange code for tokens
        tokens = await exchange_code_for_tokens(code)
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        expires_in = tokens.get("expires_in", 3600)

        if not access_token:
            return RedirectResponse(
                url="/rap/settings?error=no_token",
                status_code=302
            )

        # Get user email
        email = await get_user_email(access_token)

        # Save to database
        conn = get_db()
        cursor = conn.cursor()

        token_expiry = (datetime.now() + timedelta(seconds=expires_in)).isoformat()

        cursor.execute('''
            INSERT INTO google_accounts (email, tipo, access_token, refresh_token, token_expiry, scopes, conectado)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (email) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = COALESCE(EXCLUDED.refresh_token, google_accounts.refresh_token),
                token_expiry = EXCLUDED.token_expiry,
                scopes = EXCLUDED.scopes,
                conectado = TRUE
        ''', (
            email,
            state,  # 'professional' or 'personal'
            access_token,
            refresh_token,
            token_expiry,
            json.dumps(CONTACTS_SCOPES)
        ))

        conn.commit()
        conn.close()

        return RedirectResponse(
            url=f"/rap/settings?success=connected&email={email}",
            status_code=302
        )

    except Exception as e:
        return RedirectResponse(
            url=f"/rap/settings?error={str(e)[:100]}",
            status_code=302
        )


@app.post("/api/google/sync/{account_id}")
async def sync_google_contacts(request: Request, account_id: int, background_tasks: BackgroundTasks):
    """
    Sincroniza contatos de uma conta Google
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode sincronizar")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT * FROM google_accounts WHERE id = %s
    ''', (account_id,))
    account = cursor.fetchone()

    if not account:
        conn.close()
        raise HTTPException(status_code=404, detail="Conta nao encontrada")

    account = row_to_dict(account)

    if not account.get("conectado"):
        conn.close()
        raise HTTPException(status_code=400, detail="Conta desconectada")

    try:
        stats = await sync_contacts_from_google(
            access_token=account["access_token"],
            refresh_token=account["refresh_token"],
            account_email=account["email"],
            db_connection=conn
        )
        conn.close()

        return {
            "status": "synced",
            "account": account["email"],
            "stats": stats
        }

    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/google/sync-all")
async def sync_all_google_contacts(request: Request):
    """Sincroniza contatos de todas as contas conectadas"""
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode sincronizar")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT * FROM google_accounts WHERE conectado = TRUE
    ''')
    accounts = [row_to_dict(row) for row in cursor.fetchall()]

    results = []
    for account in accounts:
        try:
            stats = await sync_contacts_from_google(
                access_token=account["access_token"],
                refresh_token=account["refresh_token"],
                account_email=account["email"],
                db_connection=conn
            )
            results.append({
                "account": account["email"],
                "status": "success",
                "stats": stats
            })
        except Exception as e:
            results.append({
                "account": account["email"],
                "status": "error",
                "error": str(e)
            })

    conn.close()

    return {"results": results}


@app.get("/api/cron/sync-contacts")
async def cron_sync_contacts(request: Request):
    """
    Cron endpoint for incremental contact sync.
    Called by Vercel Cron every 30 minutes.
    Uses sync tokens for efficient delta sync.
    """
    # Verify cron authorization (Vercel sets this header)
    auth_header = request.headers.get("authorization", "")
    cron_secret = os.getenv("CRON_SECRET", "")

    # In production, Vercel Cron sets Authorization header
    # For local testing, allow without auth
    is_vercel_cron = request.headers.get("x-vercel-cron") == "true"
    is_authorized = (
        is_vercel_cron or
        auth_header == f"Bearer {cron_secret}" or
        os.getenv("VERCEL_ENV") != "production"
    )

    if not is_authorized and cron_secret:
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    conn = get_db()
    cursor = conn.cursor()

    # Get all connected accounts
    cursor.execute('''
        SELECT * FROM google_accounts WHERE conectado = TRUE
    ''')
    accounts = [row_to_dict(row) for row in cursor.fetchall()]

    results = []
    total_changes = 0

    for account in accounts:
        try:
            stats = await sync_contacts_incremental(
                access_token=account["access_token"],
                refresh_token=account["refresh_token"],
                account_email=account["email"],
                sync_token=account.get("sync_token"),
                db_connection=conn
            )

            # If full sync is required (sync token expired), flag it
            if stats.get("full_sync_required"):
                results.append({
                    "account": account["email"],
                    "status": "full_sync_required",
                    "message": "Sync token expired, full sync needed"
                })
            else:
                changes = stats["imported"] + stats["updated"] + stats["deleted"]
                total_changes += changes
                results.append({
                    "account": account["email"],
                    "status": "success",
                    "imported": stats["imported"],
                    "updated": stats["updated"],
                    "deleted": stats["deleted"],
                    "errors": stats["errors"]
                })

        except Exception as e:
            results.append({
                "account": account["email"],
                "status": "error",
                "error": str(e)
            })

    conn.close()

    return {
        "status": "completed",
        "timestamp": datetime.now().isoformat(),
        "accounts_processed": len(accounts),
        "total_changes": total_changes,
        "results": results
    }


@app.delete("/api/google/accounts/{account_id}")
async def disconnect_google_account(request: Request, account_id: int):
    """Desconecta uma conta Google"""
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode desconectar")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        UPDATE google_accounts
        SET conectado = FALSE, access_token = NULL
        WHERE id = %s
    ''', (account_id,))

    conn.commit()
    conn.close()

    return {"status": "disconnected"}


# ============== Gmail Integration ==============

gmail = GmailIntegration()


@app.get("/api/gmail/sync/{account_id}")
async def sync_gmail_messages(request: Request, account_id: int, days: int = 30, max_messages: int = 100):
    """
    Sync Gmail messages for a connected account.
    Links messages to existing contacts when email matches.

    Args:
        account_id: Google account ID
        days: How many days back to sync (default 30)
        max_messages: Maximum messages to fetch (default 100)
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode sincronizar")

    conn = get_connection()
    cursor = conn.cursor()

    # Get account
    cursor.execute("SELECT * FROM google_accounts WHERE id = %s", (account_id,))
    account = cursor.fetchone()

    if not account:
        conn.close()
        raise HTTPException(status_code=404, detail="Conta nao encontrada")

    account = dict(account)

    if not account.get("conectado"):
        conn.close()
        raise HTTPException(status_code=400, detail="Conta desconectada")

    access_token = account.get("access_token")
    refresh_token = account.get("refresh_token")
    account_email = account.get("email")

    # Check if token needs refresh
    token_expiry = account.get("token_expiry")
    if token_expiry and isinstance(token_expiry, str):
        token_expiry = datetime.fromisoformat(token_expiry.replace("Z", "+00:00"))

    if token_expiry and datetime.now() > token_expiry:
        # Refresh token
        try:
            new_tokens = await gmail.refresh_access_token(refresh_token)
            access_token = new_tokens.get("access_token")
            expires_in = new_tokens.get("expires_in", 3600)

            cursor.execute('''
                UPDATE google_accounts
                SET access_token = %s, token_expiry = %s
                WHERE id = %s
            ''', (
                access_token,
                (datetime.now() + timedelta(seconds=expires_in)).isoformat(),
                account_id
            ))
            conn.commit()
        except Exception as e:
            conn.close()
            raise HTTPException(status_code=401, detail=f"Falha ao renovar token: {str(e)}")

    # Build query for recent messages
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    query = f"after:{from_date}"

    stats = {"fetched": 0, "linked": 0, "saved": 0, "errors": 0}

    try:
        # List messages
        result = await gmail.list_messages(access_token, query=query, max_results=max_messages)

        if "error" in result:
            if result["error"] == "token_expired":
                raise HTTPException(status_code=401, detail="Token expirado")
            raise HTTPException(status_code=500, detail=result["error"])

        messages = result.get("messages", [])
        stats["fetched"] = len(messages)

        # Build email -> contact_id lookup
        cursor.execute("""
            SELECT id, emails FROM contacts WHERE emails != '[]'::jsonb
        """)
        email_to_contact = {}
        for row in cursor.fetchall():
            contact_emails = row["emails"] if isinstance(row["emails"], list) else json.loads(row["emails"] or "[]")
            for e in contact_emails:
                if isinstance(e, dict):
                    email_to_contact[e.get("email", "").lower()] = row["id"]
                elif isinstance(e, str):
                    email_to_contact[e.lower()] = row["id"]

        # Process each message
        for msg_ref in messages:
            try:
                msg_id = msg_ref.get("id")

                # Check if already exists
                cursor.execute(
                    "SELECT id FROM messages WHERE external_id = %s",
                    (f"gmail:{msg_id}",)
                )
                if cursor.fetchone():
                    continue

                # Get full message
                full_msg = await gmail.get_message(access_token, msg_id)
                if "error" in full_msg:
                    stats["errors"] += 1
                    continue

                headers = gmail.parse_message_headers(full_msg)
                body = gmail.parse_message_body(full_msg)

                from_header = headers.get("from", "")
                to_header = headers.get("to", "")
                subject = headers.get("subject", "")
                date_str = headers.get("date", "")
                message_date = parse_gmail_date(date_str) or datetime.now()

                # Determine direction based on account email
                from_email = gmail.extract_email_address(from_header)
                to_email = gmail.extract_email_address(to_header)

                if from_email == account_email.lower():
                    direction = "outgoing"
                    other_email = to_email
                else:
                    direction = "incoming"
                    other_email = from_email

                # Try to link to contact
                contact_id = email_to_contact.get(other_email)
                conversation_id = None

                if contact_id:
                    stats["linked"] += 1

                    # Find or create conversation
                    thread_id = full_msg.get("threadId")
                    cursor.execute("""
                        SELECT id FROM conversations
                        WHERE contact_id = %s AND canal = 'email' AND external_id = %s
                    """, (contact_id, f"gmail:{thread_id}"))
                    conv = cursor.fetchone()

                    if conv:
                        conversation_id = conv["id"]
                    else:
                        cursor.execute("""
                            INSERT INTO conversations (contact_id, canal, external_id, assunto, ultimo_mensagem)
                            VALUES (%s, 'email', %s, %s, %s)
                            RETURNING id
                        """, (contact_id, f"gmail:{thread_id}", subject, message_date))
                        conversation_id = cursor.fetchone()["id"]

                # Save message
                cursor.execute("""
                    INSERT INTO messages (
                        conversation_id, contact_id, external_id, direcao,
                        conteudo, conteudo_html, metadata, enviado_em
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    conversation_id,
                    contact_id,
                    f"gmail:{msg_id}",
                    direction,
                    body.get("text", "")[:10000],  # Limit text length
                    body.get("html", "")[:50000],  # Limit HTML length
                    json.dumps({
                        "from": from_header,
                        "to": to_header,
                        "subject": subject,
                        "thread_id": full_msg.get("threadId"),
                        "account": account_email
                    }),
                    message_date
                ))
                stats["saved"] += 1

                # Update conversation timestamp
                if conversation_id:
                    cursor.execute("""
                        UPDATE conversations
                        SET ultimo_mensagem = GREATEST(ultimo_mensagem, %s),
                            total_mensagens = total_mensagens + 1
                        WHERE id = %s
                    """, (message_date, conversation_id))

                # Update contact last contact
                if contact_id:
                    cursor.execute("""
                        UPDATE contacts
                        SET ultimo_contato = GREATEST(ultimo_contato, %s),
                            total_interacoes = total_interacoes + 1
                        WHERE id = %s
                    """, (message_date, contact_id))

            except Exception as e:
                stats["errors"] += 1
                continue

        conn.commit()

        # Update last sync
        cursor.execute("""
            UPDATE google_accounts SET ultima_sync = CURRENT_TIMESTAMP WHERE id = %s
        """, (account_id,))
        conn.commit()

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()

    return {
        "status": "ok",
        "account": account_email,
        "stats": stats
    }


@app.post("/api/gmail/send")
async def send_gmail_message(request: Request):
    """
    Send an email via Gmail API.

    Body:
    - account_id: Google account ID to send from
    - to: Recipient email
    - subject: Email subject
    - body: Plain text body
    - html_body: Optional HTML body
    - thread_id: Optional thread ID for replies
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode enviar")

    body = await request.json()
    account_id = body.get("account_id")
    to = body.get("to")
    subject = body.get("subject")
    text_body = body.get("body", "")
    html_body = body.get("html_body")
    thread_id = body.get("thread_id")

    if not all([account_id, to, subject]):
        raise HTTPException(status_code=400, detail="account_id, to, subject sao obrigatorios")

    conn = get_connection()
    cursor = conn.cursor()

    # Get account
    cursor.execute("SELECT * FROM google_accounts WHERE id = %s", (account_id,))
    account = cursor.fetchone()

    if not account:
        conn.close()
        raise HTTPException(status_code=404, detail="Conta nao encontrada")

    account = dict(account)
    access_token = account.get("access_token")

    try:
        result = await gmail.send_message(
            access_token=access_token,
            to=to,
            subject=subject,
            body=text_body,
            html_body=html_body,
            thread_id=thread_id
        )

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        conn.close()
        return {"status": "sent", "message_id": result.get("id")}

    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gmail/threads/{contact_id}")
async def get_gmail_threads_for_contact(request: Request, contact_id: int, limit: int = 20):
    """
    Get Gmail threads for a specific contact.
    Returns conversations linked to this contact.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Get email conversations for this contact
        cursor.execute("""
            SELECT c.id, c.assunto, c.ultimo_mensagem, c.total_mensagens, c.external_id
            FROM conversations c
            WHERE c.contact_id = %s AND c.canal = 'email'
            ORDER BY c.ultimo_mensagem DESC
            LIMIT %s
        """, (contact_id, limit))

        conversations = [dict(row) for row in cursor.fetchall()]

        # Get messages for each conversation
        for conv in conversations:
            cursor.execute("""
                SELECT id, direcao, conteudo, metadata, enviado_em
                FROM messages
                WHERE conversation_id = %s
                ORDER BY enviado_em DESC
                LIMIT 5
            """, (conv["id"],))
            conv["messages"] = [dict(row) for row in cursor.fetchall()]

        return {"contact_id": contact_id, "conversations": conversations}

    finally:
        cursor.close()
        conn.close()


# ============== SCORING API ==============
# Endpoints para sistema de scoring dinâmico v2.0
# Adicionado por INST-3

@app.post("/api/scoring/recalculate")
async def api_scoring_recalculate(
    batch_size: int = 200,
    offset: int = 0,
    user: dict = Depends(require_admin)
):
    """
    Recalcula os scores dos prospects em batches.
    Use batch_size e offset para processar em partes.
    Requer permissão de admin.
    """
    try:
        stats = scorer.recalculate_all_scores(batch_size=batch_size, offset=offset)
        return {
            "success": True,
            "message": f"Batch processado: {stats['total_processados']} prospects",
            "stats": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao recalcular scores: {str(e)}")


@app.get("/api/scoring/stats")
async def api_scoring_stats(user: dict = Depends(require_admin)):
    """
    Retorna estatísticas do sistema de scoring atual.
    Inclui: total de pesos, multiplicadores aprendidos, high value indicators.
    Requer permissão de admin.
    """
    try:
        stats = scorer.get_scoring_stats()
        return {
            "success": True,
            "stats": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao obter stats: {str(e)}")


@app.get("/api/scoring/icp")
async def api_scoring_icp(user: dict = Depends(require_admin)):
    """
    Retorna análise completa do ICP (Ideal Customer Profile).
    Inclui: taxas de conversão, cargos top, insights acionáveis, recomendações.
    Requer permissão de admin.
    """
    try:
        analysis = scorer.analyze_icp()
        return {
            "success": True,
            "analysis": analysis
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na análise ICP: {str(e)}")


# Vercel handler
app_handler = app
