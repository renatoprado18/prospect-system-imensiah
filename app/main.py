"""
Sistema de Gestão de Prospects - ImensIAH
API Backend com FastAPI

Deploy: Vercel (Serverless)
Domínio: prospects.almeida-prado.com
"""
import os
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from models import (
    Prospect, Meeting, ProspectStatus, ProspectTier,
    MeetingOutcome, UserRole, init_db
)
from scoring import DynamicScorer
from integrations.google_calendar import GoogleCalendarIntegration, create_calendar_link
from integrations.fathom import FathomIntegration, handle_fathom_webhook
from auth import (
    oauth, get_current_user, require_auth, require_admin, require_operador,
    google_login, google_callback, logout, ALLOWED_USERS, SECRET_KEY
)

# Config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", "/tmp/prospects.db")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
FATHOM_API_KEY = os.getenv("FATHOM_API_KEY")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "..", "data")

# Lifespan handler
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    os.makedirs("/tmp", exist_ok=True)
    try:
        init_db(DB_PATH)
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

# Session middleware (required for OAuth)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

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
scorer = DynamicScorer(DB_PATH)
calendar = GoogleCalendarIntegration()
fathom = FathomIntegration()


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


# ============== Database Helpers ==============

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def row_to_dict(row):
    return dict(row) if row else None


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

    # Admin vai para /admin
    if user["role"] == "admin":
        return RedirectResponse(url="/admin", status_code=302)

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

@app.get("/api/user/{email}")
async def get_user(email: str):
    """Obtém dados do usuário"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE email LIKE ?", (f"%{email}%",))
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
        "UPDATE users SET tutorial_concluido = TRUE WHERE email LIKE ?",
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
        query += " AND tier = ?"
        params.append(tier)

    query += " ORDER BY score DESC, tier ASC LIMIT ? OFFSET ?"
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
        SET aprovado_por_renato = ?,
            status = ?,
            notas_renato = ?,
            prioridade_renato = ?,
            data_aprovacao = ?
        WHERE id = ?
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
        VALUES (?, 'Renato', ?, ?)
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
            SET aprovado_por_renato = ?,
                status = ?,
                notas_renato = ?,
                data_aprovacao = ?
            WHERE id = ? AND status = 'pendente_aprovacao'
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
    cursor.execute("SELECT COUNT(*) FROM prospects WHERE aprovado_por_renato = TRUE")
    stats['total_aprovados'] = cursor.fetchone()[0]

    # Rejeitados
    cursor.execute("SELECT COUNT(*) FROM prospects WHERE status = 'rejeitado'")
    stats['total_rejeitados'] = cursor.fetchone()[0]

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
        query += " AND tier = ?"
        params.append(tier)

    if status:
        query += " AND status = ?"
        params.append(status)

    if search:
        query += " AND (nome LIKE ? OR empresa LIKE ? OR cargo LIKE ?)"
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term])

    # Ordenar por prioridade de Renato primeiro, depois score
    query += " ORDER BY prioridade_renato DESC, score DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()

    # Count total
    count_query = query.replace("SELECT *", "SELECT COUNT(*)").split("ORDER BY")[0]
    cursor.execute(count_query, params[:-2])
    total = cursor.fetchone()[0]

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

    cursor.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Prospect não encontrado")

    prospect = row_to_dict(row)

    # Buscar reuniões
    cursor.execute(
        "SELECT * FROM meetings WHERE prospect_id = ? ORDER BY data_hora DESC",
        (prospect_id,)
    )
    meetings = [row_to_dict(r) for r in cursor.fetchall()]

    # Buscar atividades
    cursor.execute(
        "SELECT * FROM activity_log WHERE prospect_id = ? ORDER BY data_hora DESC LIMIT 20",
        (prospect_id,)
    )
    activities = [row_to_dict(r) for r in cursor.fetchall()]

    conn.close()

    return {
        "prospect": prospect,
        "meetings": meetings,
        "activities": activities
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        prospect.nome, prospect.empresa, prospect.cargo,
        prospect.email, prospect.telefone, prospect.linkedin,
        score, tier, json.dumps(breakdown), json.dumps(reasons)
    ))

    prospect_id = cursor.lastrowid
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
        updates.append(f"{field} = ?")
        params.append(value)

    if not updates:
        conn.close()
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")

    params.append(prospect_id)
    query = f"UPDATE prospects SET {', '.join(updates)} WHERE id = ?"

    cursor.execute(query, params)

    # Log atividade
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (?, 'Andressa', 'Atualização', ?)
    ''', (prospect_id, json.dumps(update.model_dump(exclude_none=True))))

    conn.commit()
    conn.close()

    return {"status": "updated"}


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
        SET converted = TRUE, deal_value = ?, conversion_notes = ?, status = 'convertido'
        WHERE id = ?
    ''', (deal_value, notes, prospect_id))

    # Buscar dados do prospect para learning
    cursor.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,))
    prospect = row_to_dict(cursor.fetchone())

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (?, 'Sistema', 'Conversão', ?)
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
    cursor.execute("SELECT * FROM prospects WHERE id = ?", (meeting.prospect_id,))
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
        VALUES (?, ?, ?, ?, ?)
    ''', (
        meeting.prospect_id,
        calendar_event.get('id') if calendar_event else None,
        meeting.data_hora.isoformat(),
        meeting.duracao_minutos,
        meeting.tipo
    ))

    meeting_id = cursor.lastrowid

    # Atualizar status do prospect
    cursor.execute('''
        UPDATE prospects
        SET status = 'reuniao_agendada', data_reuniao = ?
        WHERE id = ?
    ''', (meeting.data_hora.isoformat(), meeting.prospect_id))

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (?, 'Andressa', 'Reunião Agendada', ?)
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
            meeting_outcome = ?,
            meeting_notes = ?,
            objecoes = ?,
            interesse_features = ?,
            data_ultimo_contato = ?
        WHERE id = ?
    ''', (
        feedback.outcome.value,
        feedback.notes,
        json.dumps(feedback.objecoes),
        json.dumps(feedback.features_interesse),
        datetime.now().isoformat(),
        feedback.prospect_id
    ))

    # Atualizar meeting
    cursor.execute('''
        UPDATE meetings
        SET realizada = TRUE, outcome = ?, objecoes_identificadas = ?, pontos_interesse = ?, proximos_passos = ?
        WHERE prospect_id = ? AND realizada = FALSE
        ORDER BY data_hora DESC LIMIT 1
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
            INSERT OR IGNORE INTO sales_arguments (argumento, categoria, objecao_relacionada)
            VALUES (?, 'objecao', ?)
        ''', (f"Resposta para: {objecao}", objecao))

    # Log
    cursor.execute('''
        INSERT INTO activity_log (prospect_id, usuario, acao, detalhes)
        VALUES (?, 'Andressa', 'Feedback Reunião', ?)
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
    cursor.execute(f'SELECT COUNT(*) FROM prospects WHERE converted = TRUE {aprovado_filter}')
    stats['total_convertidos'] = cursor.fetchone()[0]

    cursor.execute(f'SELECT SUM(deal_value) FROM prospects WHERE converted = TRUE {aprovado_filter}')
    result = cursor.fetchone()[0]
    stats['receita_total'] = result or 0

    # Reuniões
    cursor.execute('SELECT COUNT(*) FROM meetings WHERE realizada = TRUE')
    stats['reunioes_realizadas'] = cursor.fetchone()[0]

    cursor.execute('''
        SELECT COUNT(*) FROM meetings
        WHERE data_hora > ? AND realizada = FALSE
    ''', (datetime.now().isoformat(),))
    stats['reunioes_agendadas'] = cursor.fetchone()[0]

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
            'SELECT COUNT(*) FROM prospects WHERE status = ?',
            (status,)
        )
        count = cursor.fetchone()[0]
        funnel.append({"stage": label, "count": count})

    conn.close()
    return {"funnel": funnel}


# ============== Import de dados ==============

@app.post("/api/import/csv")
async def import_from_csv(background_tasks: BackgroundTasks):
    """
    Importa prospects do CSV processado

    Usa o arquivo gerado pelo script de análise inicial
    """
    import csv

    csv_path = os.path.join(DATA_DIR, "prospects_imensiah.csv")

    if not os.path.exists(csv_path):
        raise HTTPException(status_code=404, detail="Arquivo CSV não encontrado")

    conn = get_db()
    cursor = conn.cursor()

    imported = 0
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)

        for row in reader:
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO prospects
                    (nome, empresa, cargo, email, telefone, score, tier, reasons)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    row.get('Nome', ''),
                    row.get('Empresa', ''),
                    row.get('Cargo', ''),
                    row.get('Email', ''),
                    row.get('Telefone', ''),
                    int(row.get('Score', 0)),
                    row.get('Tier', 'E').split()[0],
                    row.get('Razões de Qualificação', '')
                ))
                imported += 1
            except Exception as e:
                print(f"Error importing row: {e}")
                continue

    conn.commit()
    conn.close()

    return {"status": "imported", "count": imported}


# Vercel handler
app_handler = app
