"""
INTEL - Assistente Pessoal Inteligente
API Backend com FastAPI

Deploy: Vercel (Serverless)
Domínio: intel.almeida-prado.com
"""
# Load .env for local development (before any other imports)
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(env_path)

import os
import sys
import json
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request, Depends, File, UploadFile
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
from integrations.whatsapp import (
    WhatsAppIntegration, parse_webhook_message, format_phone_display,
    get_all_templates, get_template, render_template, get_templates_by_category
)
from integrations.gmail import GmailIntegration, parse_gmail_date
from services.circulos import (
    recalcular_circulo_contato,
    recalcular_circulos_dual,
    recalcular_todos_circulos,
    get_dashboard_circulos,
    get_contatos_precisando_atencao,
    get_aniversarios_proximos,
    get_contatos_por_circulo,
    definir_circulo_manual,
    calcular_score_circulo,
    calcular_health_score,
    CIRCULO_CONFIG
)
from services.briefings import (
    generate_briefing,
    get_contacts_needing_briefing,
    get_current_briefing,
    record_briefing_action
)
from services.auto_tags import (
    analisar_contato_para_tags,
    aplicar_tags_contato,
    aplicar_tags_em_lote,
    get_tag_statistics
)
from services.dashboard import (
    get_dashboard_stats,
    get_alertas,
    get_contatos_recentes,
    get_circulos_resumo
)
from services.projects import (
    list_projects,
    get_project,
    create_project,
    update_project,
    delete_project,
    add_project_member,
    remove_project_member,
    add_milestone,
    update_milestone,
    delete_milestone,
    add_project_note,
    get_project_timeline,
    get_projects_stats,
    get_active_projects_summary,
    PROJECT_TYPES,
    PROJECT_STATUS
)
from services.duplicados import (
    encontrar_duplicados,
    merge_contatos,
    get_duplicate_statistics
)
from services.veiculos import (
    get_veiculo, get_veiculo_por_placa, listar_veiculos, criar_veiculo,
    atualizar_km, get_itens_manutencao, criar_item_manutencao,
    importar_plano_manutencao_prado, get_historico_manutencoes,
    registrar_manutencao, get_dashboard_veiculo, criar_ordem_servico,
    get_ordem_servico, listar_ordens_servico, finalizar_ordem_servico,
    criar_prado_jrw5025, atualizar_notas_fabricante_prado, get_alertas_manutencao,
    get_timeline_manutencao
)
from services.briefing_context import (
    get_contexto_enriquecido,
    analisar_tom_conversas,
    identificar_topicos_recorrentes,
    sugerir_assuntos_retomar,
    detectar_promessas_pendentes
)
from services.linkedin_enrichment import get_linkedin_enrichment_service
from services.search import get_search_service
from services.rodas_service import get_rodas_service, RODA_TYPES
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
    allow_origins=[
        "https://intel.almeida-prado.com",
        "https://prospect-system.vercel.app",
        "http://localhost:8000",
        "https://conselhoos.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Service Worker - must be served from root for full scope
@app.get("/sw.js")
async def service_worker():
    """Serve service worker from root for full site scope."""
    from fastapi.responses import FileResponse
    import os
    sw_path = os.path.join(STATIC_DIR, "sw.js")
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"}
    )


# Services
scorer = DynamicScorer()  # Now uses PostgreSQL
calendar = GoogleCalendarIntegration()
fathom = FathomIntegration()
linkedin = LinkedInIntegration()
whatsapp = WhatsAppIntegration()


# Debug endpoint
@app.get("/api/debug/dashboard")
async def debug_dashboard(request: Request):
    """Debug endpoint to test dashboard functions individually."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")
    import traceback
    results = {}

    try:
        from services.dashboard import get_dashboard_stats
        results["stats"] = get_dashboard_stats()
    except Exception as e:
        results["stats_error"] = {"error": str(e), "tb": traceback.format_exc()}

    try:
        from services.dashboard import get_circulos_resumo
        results["circulos"] = get_circulos_resumo()
    except Exception as e:
        results["circulos_error"] = {"error": str(e), "tb": traceback.format_exc()}

    return results


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


@app.get("/api/bot-status")
async def bot_status():
    """Check if intel bot code is deployed"""
    import os
    return {
        "bot_code": "v2",
        "intel_bot_instance": os.getenv("INTEL_BOT_INSTANCE", "NOT_SET"),
        "intel_bot_number": os.getenv("INTEL_BOT_NUMBER", "NOT_SET"),
    }

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


# Cache do status da API Claude (evita checar a cada request)
_claude_api_status = {"status": "unknown", "checked_at": None, "detail": ""}


@app.get("/api/ai/status")
async def ai_api_status():
    """Verifica se a API Claude esta funcional (com cache de 1h)"""
    import httpx as hx

    global _claude_api_status

    # Usar cache se checado ha menos de 1h
    if _claude_api_status["checked_at"]:
        age = (datetime.now() - _claude_api_status["checked_at"]).total_seconds()
        if age < 3600:  # 1 hora
            return _claude_api_status

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        _claude_api_status = {"status": "not_configured", "checked_at": datetime.now(), "detail": "ANTHROPIC_API_KEY nao configurada"}
        return _claude_api_status

    try:
        async with hx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ok"}]
                }
            )

        if resp.status_code == 200:
            _claude_api_status = {"status": "ok", "checked_at": datetime.now(), "detail": "API funcionando"}
        elif "credit balance" in resp.text.lower():
            _claude_api_status = {"status": "no_credits", "checked_at": datetime.now(),
                                   "detail": "Saldo insuficiente. Recarregue em console.anthropic.com"}
        elif resp.status_code == 401:
            _claude_api_status = {"status": "invalid_key", "checked_at": datetime.now(), "detail": "API key invalida"}
        else:
            _claude_api_status = {"status": "error", "checked_at": datetime.now(), "detail": f"Erro {resp.status_code}"}

    except Exception as e:
        _claude_api_status = {"status": "error", "checked_at": datetime.now(), "detail": str(e)}

    return _claude_api_status

# reset-db REMOVIDO por segurança (era acessível sem auth)


# ============== API Routes - Auth ==============

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    """Privacy Policy page for LinkedIn App and integrations"""
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy - INTEL by Almeida Prado</title>
<style>body{font-family:-apple-system,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;color:#1e293b;line-height:1.7;}
h1{color:#6366f1;}h2{color:#334155;margin-top:32px;}a{color:#6366f1;}</style></head><body>
<h1>Privacy Policy</h1>
<p><strong>INTEL</strong> — Personal CRM & Intelligence Platform<br>
Operated by Renato Almeida Prado<br>
Last updated: April 24, 2026</p>

<h2>1. What We Collect</h2>
<p>INTEL collects and processes data solely for personal CRM and relationship management purposes:</p>
<ul>
<li>Contact information (name, email, phone, company, role)</li>
<li>Communication history (emails, WhatsApp messages)</li>
<li>Calendar events and tasks</li>
<li>LinkedIn profile data (public profile information via authorized API access)</li>
</ul>

<h2>2. How We Use Data</h2>
<p>Data is used exclusively to:</p>
<ul>
<li>Manage personal and professional relationships</li>
<li>Generate AI-powered insights and recommendations</li>
<li>Schedule and track communications</li>
<li>Publish content to LinkedIn on behalf of the authenticated user</li>
</ul>

<h2>3. Data Storage & Security</h2>
<p>All data is stored securely in encrypted databases (Neon PostgreSQL). Access is restricted to the authenticated account owner. No data is shared with third parties.</p>

<h2>4. Third-Party Services</h2>
<p>INTEL integrates with the following services under their respective terms:</p>
<ul>
<li>Google APIs (Calendar, Gmail, Drive, Tasks, Contacts)</li>
<li>LinkedIn API (profile reading, content publishing)</li>
<li>WhatsApp via Evolution API (messaging)</li>
<li>Anthropic Claude AI (analysis and content generation)</li>
</ul>

<h2>5. Data Retention</h2>
<p>Data is retained as long as the account is active. Users can request data deletion at any time by contacting the platform owner.</p>

<h2>6. User Rights</h2>
<p>Users have the right to access, correct, export, or delete their personal data. Contact: <a href="mailto:renato@almeida-prado.com">renato@almeida-prado.com</a></p>

<h2>7. Contact</h2>
<p>Renato Almeida Prado<br>
<a href="https://intel.almeida-prado.com">intel.almeida-prado.com</a><br>
<a href="mailto:renato@almeida-prado.com">renato@almeida-prado.com</a></p>
</body></html>""")


# ============== LINKEDIN OAUTH ==============

@app.get("/api/linkedin/authorize")
async def linkedin_authorize():
    """Redirect to LinkedIn OAuth authorization page"""
    from integrations.linkedin_posting import get_auth_url
    url = get_auth_url()
    return RedirectResponse(url=url)


@app.get("/api/linkedin/callback")
async def linkedin_callback(code: str = None, error: str = None, state: str = None):
    """LinkedIn OAuth callback - exchange code for token"""
    if error:
        return HTMLResponse(f"<h2>Erro LinkedIn</h2><p>{error}</p><a href='/editorial'>Voltar</a>")
    if not code:
        return HTMLResponse("<h2>Erro</h2><p>Codigo de autorizacao nao recebido</p>")

    from integrations.linkedin_posting import exchange_code_for_token
    result = await exchange_code_for_token(code)

    if result.get("error"):
        return HTMLResponse(f"<h2>Erro</h2><p>{result['error']}</p><a href='/editorial'>Voltar</a>")

    name = result.get("profile", {}).get("name", "")
    return HTMLResponse(f"""<html><body style="font-family:sans-serif;text-align:center;padding:60px;">
        <h2 style="color:#22c55e;">LinkedIn conectado!</h2>
        <p>Autenticado como <strong>{name}</strong></p>
        <p>Token valido por 60 dias.</p>
        <a href="/editorial" style="color:#6366f1;">Ir para Editorial</a>
    </body></html>""")


@app.get("/api/linkedin/status")
async def linkedin_status():
    """Check LinkedIn connection status"""
    from integrations.linkedin_posting import get_stored_token
    token = get_stored_token()
    if token:
        return {"connected": True, "name": token.get("name"), "expires_at": str(token.get("expires_at"))}
    return {"connected": False}


@app.post("/api/editorial/{post_id}/publish-linkedin")
async def api_publish_to_linkedin(post_id: int):
    """Publish a post directly to LinkedIn"""
    from integrations.linkedin_posting import publish_post
    from services.editorial_calendar import get_post, mark_as_published

    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post nao encontrado")

    content = post.get("conteudo_adaptado") or post.get("article_description") or ""
    hashtags = post.get("hashtags", [])
    if isinstance(hashtags, list):
        hashtags = " ".join(hashtags)
    full_text = content + ("\n\n" + hashtags if hashtags else "")

    if not full_text.strip():
        raise HTTPException(status_code=400, detail="Post sem conteudo")

    article_url = post.get("article_url") or ""

    result = await publish_post(full_text, article_url if article_url else None)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    # Mark as published with LinkedIn URL
    mark_as_published(post_id, url_publicado=result.get("post_url", ""))

    return {"status": "success", "linkedin_url": result.get("post_url"), "post_id": result.get("post_id")}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None, email: Optional[str] = None):
    """Página de login"""
    user = get_current_user(request)
    if user:
        # Already logged in, redirect
        return RedirectResponse(url="/", status_code=302)  # INTEL dashboard
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

@app.get("/prospeccao", response_class=HTMLResponse)
async def prospeccao_dashboard(request: Request):
    """Dashboard de Prospecção (sistema legado)"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

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


# ============== INTEL Pages (nova estrutura) ==============
# Rotas sem /rap para o novo dominio intel.almeida-prado.com

@app.get("/", response_class=HTMLResponse)
async def intel_home(request: Request):
    """INTEL Dashboard - Assistente Pessoal Inteligente"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_dashboard.html", {
        "request": request,
        "user": user
    })


@app.get("/circulos", response_class=HTMLResponse)
async def intel_circulos(request: Request):
    """INTEL Circulos - Classificacao de contatos"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_circulos.html", {
        "request": request,
        "user": user
    })


@app.get("/briefings", response_class=HTMLResponse)
async def intel_briefings(request: Request):
    """INTEL Briefings - Preparacao para reunioes"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_briefings.html", {
        "request": request,
        "user": user
    })


@app.get("/contatos", response_class=HTMLResponse)
async def intel_contatos(request: Request):
    """INTEL Contatos - Lista de contatos"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_contacts.html", {
        "request": request,
        "user": user
    })


@app.get("/contatos/limpeza", response_class=HTMLResponse)
async def intel_contatos_limpeza(request: Request):
    """INTEL Contatos - Limpeza e deduplicacao"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_contacts_cleanup.html", {
        "request": request,
        "user": user
    })


@app.get("/contatos/linkedin", response_class=HTMLResponse)
async def intel_contatos_linkedin(request: Request):
    """INTEL Contatos - Import LinkedIn"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_linkedin_import.html", {
        "request": request,
        "user": user
    })


@app.get("/linkedin/bookmarklet", response_class=HTMLResponse)
async def intel_linkedin_bookmarklet(request: Request):
    """INTEL - LinkedIn Bookmarklet"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_linkedin_bookmarklet.html", {
        "request": request,
        "user": user
    })


@app.get("/duplicados", response_class=HTMLResponse)
async def intel_duplicados(request: Request):
    """INTEL - Pagina de duplicados"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("intel_duplicados.html", {
        "request": request,
        "user": user
    })


# NOTE: Parameterized route MUST come AFTER specific routes
@app.get("/contatos/{contact_id}/editar", response_class=HTMLResponse)
async def intel_contato_editar(request: Request, contact_id: int):
    """INTEL Contato - Edicao completa do contato"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_contact_edit.html", {
        "request": request,
        "user": user,
        "contact_id": contact_id
    })


@app.get("/contatos/{contact_id}", response_class=HTMLResponse)
async def intel_contato_detail(request: Request, contact_id: int):
    """INTEL Contato - Detalhe do contato"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_contact_detail.html", {
        "request": request,
        "user": user,
        "contact_id": contact_id
    })


@app.get("/inbox", response_class=HTMLResponse)
async def intel_inbox(request: Request):
    """INTEL Inbox - Email e WhatsApp unificados"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_inbox.html", {
        "request": request,
        "user": user
    })


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    """INTEL Analytics - Metricas e graficos"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_analytics.html", {
        "request": request,
        "user": user
    })


@app.get("/automations", response_class=HTMLResponse)
async def automations_page(request: Request):
    """INTEL Automations - Automacoes de IA"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_automations.html", {
        "request": request,
        "user": user
    })


@app.get("/calendario", response_class=HTMLResponse)
async def calendario_page(request: Request):
    """INTEL Calendario - Eventos e reunioes"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_calendario.html", {
        "request": request,
        "user": user
    })


@app.get("/configuracoes", response_class=HTMLResponse)
async def intel_settings(request: Request):
    """INTEL Configuracoes - Contas Google"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "admin":
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("rap_settings.html", {
        "request": request,
        "user": user
    })


@app.get("/projetos", response_class=HTMLResponse)
async def projetos_page(request: Request):
    """Pagina de projetos"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_projetos.html", {"request": request})


@app.get("/projetos/{project_id}", response_class=HTMLResponse)
async def projeto_detail_page(request: Request, project_id: int):
    """Pagina de detalhe do projeto"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_projeto_detail.html", {
        "request": request,
        "project_id": project_id
    })


@app.get("/campanhas", response_class=HTMLResponse)
async def campanhas_page(request: Request):
    """INTEL Campanhas - Gestao de campanhas de relacionamento"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_campanhas.html", {"request": request})


# ============== RAP Redirects (retrocompatibilidade) ==============
# Todas as rotas /rap/* redirecionam para rotas na raiz

@app.get("/rap")
async def rap_redirect():
    return RedirectResponse(url="/", status_code=301)

@app.get("/rap/contacts")
async def rap_contacts_redirect():
    return RedirectResponse(url="/contatos", status_code=301)

@app.get("/rap/contacts/cleanup")
async def rap_contacts_cleanup_redirect():
    return RedirectResponse(url="/contatos/limpeza", status_code=301)

@app.get("/rap/contacts/linkedin")
async def rap_contacts_linkedin_redirect():
    return RedirectResponse(url="/contatos/linkedin", status_code=301)

@app.get("/rap/contacts/{contact_id}")
async def rap_contact_detail_redirect(contact_id: int):
    return RedirectResponse(url=f"/contatos/{contact_id}", status_code=301)

@app.get("/rap/settings")
async def rap_settings_redirect():
    return RedirectResponse(url="/configuracoes", status_code=301)

@app.get("/rap/whatsapp")
async def rap_whatsapp_redirect():
    return RedirectResponse(url="/configuracoes", status_code=301)


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
# NOTE: O endpoint POST /api/webhooks/whatsapp está definido mais abaixo,
# na seção "Evolution API Integration", usando handle_evolution_webhook

@app.get("/api/whatsapp/groups")
async def whatsapp_list_groups():
    """Lista todos os grupos de WhatsApp disponíveis"""
    try:
        chats = await whatsapp.get_all_chats(include_groups=True)
        groups = []
        for c in chats:
            if c.get('_is_group'):
                groups.append({
                    'jid': c.get('remoteJid', ''),
                    'name': c.get('name', c.get('pushName', '')),
                    'pushName': c.get('pushName', ''),
                })
        return sorted(groups, key=lambda g: g.get('name', '') or g.get('pushName', ''))
    except Exception as e:
        return []


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


# ============== WHATSAPP TEMPLATES ==============

@app.get("/api/whatsapp/templates")
async def list_whatsapp_templates(categoria: str = None):
    """List all available message templates"""
    if categoria:
        templates = get_templates_by_category(categoria)
    else:
        templates = get_all_templates()
    return {"templates": templates, "total": len(templates)}


@app.get("/api/whatsapp/templates/{template_id}")
async def get_whatsapp_template(template_id: str):
    """Get a specific template by ID"""
    template = get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return template


@app.post("/api/whatsapp/templates/{template_id}/preview")
async def preview_template(template_id: str, request: Request):
    """Preview a rendered template without sending"""
    data = await request.json()
    variables = data.get("variables", {})
    template = get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    rendered = render_template(template_id, variables)
    return {"template_id": template_id, "template_nome": template["nome"], "mensagem_renderizada": rendered}


@app.post("/api/whatsapp/send-template")
async def send_whatsapp_template(request: Request):
    """Send a WhatsApp message using a template"""
    data = await request.json()
    phone = data.get("phone")
    template_id = data.get("template_id")
    variables = data.get("variables", {})
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    if not template_id:
        raise HTTPException(status_code=400, detail="template_id is required")
    template = get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    result = await whatsapp.send_with_template(phone, template_id, variables)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return {"status": "sent", "template_used": template_id, "result": result}


@app.get("/api/whatsapp/search")
async def search_whatsapp_messages(
    q: str,
    contact_id: int = None,
    limit: int = 50
):
    """
    Search WhatsApp messages by content.
    """
    if not q or len(q) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters")

    conn = get_connection()
    cursor = conn.cursor()

    try:
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

        return {"query": q, "total": len(results), "results": results}
    finally:
        conn.close()


@app.get("/api/whatsapp/export/{contact_id}")
async def export_whatsapp_conversation(contact_id: int, format: str = "csv"):
    """Export WhatsApp conversation history for a contact."""
    from fastapi.responses import StreamingResponse
    import io
    import csv

    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT nome, telefone FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")

        cursor.execute("""
            SELECT m.direcao, m.conteudo, m.enviado_em, m.metadata
            FROM messages m
            JOIN conversations conv ON m.conversation_id = conv.id
            WHERE conv.canal = 'whatsapp' AND m.contact_id = %s
            ORDER BY m.enviado_em ASC
        """, (contact_id,))
        messages = cursor.fetchall()

        if format == "json":
            data = {
                "contact": {"id": contact_id, "name": contact["nome"], "phone": contact["telefone"]},
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

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Data/Hora", "Direcao", "Mensagem", "Status"])

        for msg in messages:
            sent_at = msg["enviado_em"].strftime("%Y-%m-%d %H:%M:%S") if msg["enviado_em"] else ""
            direction = "Enviada" if msg["direcao"] == "outgoing" else "Recebida"
            content = msg["conteudo"] or "[midia]"
            status = msg["metadata"].get("status", "") if isinstance(msg["metadata"], dict) else ""
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


@app.post("/api/contacts/{contact_id}/facts")
async def add_contact_fact(contact_id: int, request: Request):
    """Add a new fact manually"""
    data = await request.json()
    fato = data.get('fato')
    if not fato:
        raise HTTPException(status_code=400, detail="fato e obrigatorio")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO contact_facts (contact_id, categoria, fato, fonte, confianca)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (
            contact_id,
            data.get('categoria', 'general'),
            fato,
            'manual',
            1.0
        ))
        fact = dict(cursor.fetchone())
        conn.commit()
        return {"status": "success", "fact": fact}


@app.put("/api/contacts/facts/{fact_id}")
async def update_contact_fact(fact_id: int, request: Request):
    """Update an existing fact"""
    data = await request.json()
    fato = data.get('fato')
    if not fato:
        raise HTTPException(status_code=400, detail="fato e obrigatorio")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE contact_facts
            SET fato = %s, categoria = COALESCE(%s, categoria)
            WHERE id = %s
            RETURNING *
        """, (fato, data.get('categoria'), fact_id))
        fact = cursor.fetchone()
        if not fact:
            raise HTTPException(status_code=404, detail="Fato nao encontrado")
        conn.commit()
        return {"status": "success", "fact": dict(fact)}


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

    # Filtro seguro: admin vê tudo, outros só aprovados
    # Usa parâmetro booleano ao invés de concatenação de string
    requires_approval = user_role != "admin"

    # Total por tier (só aprovados para não-admin)
    cursor.execute('''
        SELECT tier, COUNT(*) as count FROM prospects
        WHERE (%s = FALSE OR aprovado_por_renato = TRUE)
        GROUP BY tier
    ''', (requires_approval,))
    stats['por_tier'] = {row['tier']: row['count'] for row in cursor.fetchall()}

    # Total por status (só aprovados para não-admin)
    cursor.execute('''
        SELECT status, COUNT(*) as count FROM prospects
        WHERE status NOT IN ('pendente_aprovacao', 'rejeitado')
          AND (%s = FALSE OR aprovado_por_renato = TRUE)
        GROUP BY status
    ''', (requires_approval,))
    stats['por_status'] = {row['status']: row['count'] for row in cursor.fetchall()}

    # Conversões
    cursor.execute('''
        SELECT COUNT(*) as count FROM prospects
        WHERE converted = TRUE AND (%s = FALSE OR aprovado_por_renato = TRUE)
    ''', (requires_approval,))
    stats['total_convertidos'] = cursor.fetchone()['count']

    cursor.execute('''
        SELECT COALESCE(SUM(deal_value), 0) as total FROM prospects
        WHERE converted = TRUE AND (%s = FALSE OR aprovado_por_renato = TRUE)
    ''', (requires_approval,))
    stats['receita_total'] = float(cursor.fetchone()['total'])

    # Reuniões
    cursor.execute('SELECT COUNT(*) as count FROM meetings WHERE realizada = TRUE')
    stats['reunioes_realizadas'] = cursor.fetchone()['count']

    cursor.execute('''
        SELECT COUNT(*) as count FROM meetings
        WHERE data_hora > %s AND realizada = FALSE
    ''', (datetime.now().isoformat(),))
    stats['reunioes_agendadas'] = cursor.fetchone()['count']

    # Top prospects para contato (só aprovados para não-admin)
    cursor.execute('''
        SELECT * FROM prospects
        WHERE status IN ('novo', 'contatado') AND tier IN ('A', 'B')
          AND (%s = FALSE OR aprovado_por_renato = TRUE)
        ORDER BY prioridade_renato DESC, score DESC
        LIMIT 10
    ''', (requires_approval,))
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
    datas_importantes: Optional[List[dict]] = []
    enderecos: Optional[List[dict]] = []
    relacionamentos: Optional[List[dict]] = []
    google_contact_id: Optional[str] = None
    origem: Optional[str] = 'manual'

class ContactsImportData(BaseModel):
    contacts: List[dict]


@app.get("/api/contacts")
async def list_contacts(
    search: Optional[str] = None,
    q: Optional[str] = None,  # Alias for search
    letter: Optional[str] = None,  # Filter by first letter of name
    contexto: Optional[str] = None,
    filter: Optional[str] = None,  # Special filters: needs_attention
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

    # Special filters
    if filter == 'needs_attention':
        query += " AND COALESCE(circulo, 5) <= 3 AND COALESCE(health_score, 50) < 50 AND ultimo_contato IS NOT NULL"

    # Filter by first letter of name
    if letter and len(letter) == 1:
        query += " AND UPPER(LEFT(nome, 1)) = %s"
        params.append(letter.upper())

    if search_term_raw:
        # Search in name, company, cargo, and phone numbers
        query += " AND (nome ILIKE %s OR empresa ILIKE %s OR cargo ILIKE %s OR telefones::text ILIKE %s)"
        search_term = f"%{search_term_raw}%"
        params.extend([search_term, search_term, search_term, search_term])

    if contexto:
        query += " AND contexto = %s"
        params.append(contexto)

    if filter == 'needs_attention':
        query += " ORDER BY circulo ASC, health_score ASC LIMIT %s OFFSET %s"
    else:
        query += " ORDER BY nome ASC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()

    # Count total
    count_query = "SELECT COUNT(*) as count FROM contacts WHERE 1=1"
    count_params = []
    if filter == 'needs_attention':
        count_query += " AND COALESCE(circulo, 5) <= 3 AND COALESCE(health_score, 50) < 50 AND ultimo_contato IS NOT NULL"
    if letter and len(letter) == 1:
        count_query += " AND UPPER(LEFT(nome, 1)) = %s"
        count_params.append(letter.upper())
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

    Body:
    - contact_ids: List of contact IDs to merge (first one is primary)
    - propagate: bool - Propagate to Google Contacts (default True)
    - field_choices: Optional dict mapping field -> contact_id or 'combine'
      Ex: {"nome": 123, "emails": "combine", "empresa": 456}
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin")

    body = await request.json()
    contact_ids = body.get('contact_ids', [])
    propagate = body.get('propagate', True)
    field_choices = body.get('field_choices')

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

    # When field_choices is provided and we have exactly 2 contacts,
    # use the merge_contatos function that respects user choices
    if field_choices and len(contacts) == 2:
        keep_id = contact_ids[0]  # First ID is primary
        merge_id = contact_ids[1]

        # Use merge_contatos from duplicados.py which now supports field_choices
        merge_result = merge_contatos(keep_id, merge_id, field_choices)

        if "error" in merge_result:
            conn.close()
            raise HTTPException(status_code=400, detail=merge_result["error"])

        # Propagate to Google if requested
        google_results = {}
        if propagate:
            try:
                # Get updated contact and deleted contact info
                cursor.execute("SELECT * FROM contacts WHERE id = %s", (keep_id,))
                merged_contact = row_to_dict(cursor.fetchone())

                # Find the deleted contact's Google ID for propagation
                deleted_contact = next((c for c in contacts if c['id'] == merge_id), None)
                if deleted_contact and deleted_contact.get('google_contact_id'):
                    google_results = await propagate_merge_to_google(
                        merged_contact,
                        [deleted_contact],
                        conn,
                        google_contacts_module
                    )
            except Exception as e:
                google_results = {"error": str(e)}

        conn.close()
        return {
            'status': 'merged',
            'primary_id': keep_id,
            'deleted_ids': [merge_id],
            'merged_fields': merge_result.get('merged_fields', []),
            'messages_transferred': merge_result.get('messages_transferred', 0),
            'conversations_transferred': merge_result.get('conversations_transferred', 0),
            'tasks_transferred': merge_result.get('tasks_transferred', 0),
            'google_propagation': google_results
        }

    # Original logic for automatic merge (no field_choices)
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


# ============== LINKEDIN ENRICHMENT ENDPOINTS ==============

# CORS headers for bookmarklet
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

@app.options("/api/linkedin/bookmarklet")
async def linkedin_bookmarklet_preflight():
    """Handle CORS preflight for bookmarklet"""
    return JSONResponse(content={}, headers=CORS_HEADERS)

@app.get("/api/linkedin/bookmarklet-receive")
async def linkedin_bookmarklet_receive_get(data: str):
    """
    Recebe dados do LinkedIn via GET (para bypass de CSP).
    Retorna uma pagina HTML com o resultado.
    """
    def error_page(msg, details=""):
        return HTMLResponse(content=f"""
        <html><body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#fee2e2;">
        <div style="text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);max-width:500px;">
        <div style="font-size:64px;">❌</div>
        <h2>Erro</h2>
        <p>{msg}</p>
        <pre style="text-align:left;background:#f5f5f5;padding:10px;border-radius:4px;font-size:11px;overflow:auto;max-height:200px;">{details}</pre>
        <button onclick="window.close()" style="margin-top:20px;padding:12px 40px;background:#3b82f6;color:white;border:none;border-radius:8px;cursor:pointer;font-size:16px;">Fechar</button>
        </div></body></html>
        """)

    # Log para debug
    data_length = len(data) if data else 0

    try:
        parsed_data = json.loads(data)
    except Exception as e:
        return error_page(f"Dados invalidos (len={data_length})", f"{str(e)}\n\nData preview: {data[:500] if data else 'None'}...")

    linkedin_url = parsed_data.get("linkedin_url", "").strip()
    if not linkedin_url:
        return HTMLResponse(content="""
        <html><body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#fee2e2;">
        <div style="text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);">
        <div style="font-size:64px;">❌</div>
        <h2>Erro</h2>
        <p>URL do LinkedIn nao encontrada</p>
        <button onclick="window.close()" style="margin-top:20px;padding:12px 40px;background:#3b82f6;color:white;border:none;border-radius:8px;cursor:pointer;font-size:16px;">Fechar</button>
        </div></body></html>
        """)

    # Normalizar URL e extrair username
    linkedin_url_normalized = linkedin_url.lower()
    username = None
    if "/in/" in linkedin_url_normalized:
        import re
        match = re.search(r'linkedin\.com/in/([^/?\s]+)', linkedin_url_normalized)
        if match:
            username = match.group(1)

    if not username:
        return HTMLResponse(content=f"""
        <html><body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#fee2e2;">
        <div style="text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);">
        <div style="font-size:64px;">❌</div>
        <h2>Erro</h2>
        <p>URL invalida: {linkedin_url[:50]}</p>
        <button onclick="window.close()" style="margin-top:20px;padding:12px 40px;background:#3b82f6;color:white;border:none;border-radius:8px;cursor:pointer;font-size:16px;">Fechar</button>
        </div></body></html>
        """)

    try:
        conn = get_db()
        cursor = conn.cursor()

        # Buscar contato pela URL do LinkedIn
        cursor.execute("""
            SELECT id, nome, empresa, cargo, linkedin_headline
            FROM contacts
            WHERE LOWER(linkedin) LIKE %s
            LIMIT 1
        """, (f"%{username}%",))

        contact = cursor.fetchone()

        if not contact:
            conn.close()
            full_name = parsed_data.get("full_name", "Desconhecido")
            return HTMLResponse(content=f"""
            <html><body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#fef3c7;">
            <div style="text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);max-width:400px;">
            <div style="font-size:64px;">⚠️</div>
            <h2>{full_name}</h2>
            <p style="color:#666;">Este contato nao esta cadastrado no INTEL.</p>
            <p style="font-size:12px;color:#999;margin-top:16px;">{username}</p>
            <button onclick="window.close()" style="margin-top:20px;padding:12px 40px;background:#3b82f6;color:white;border:none;border-radius:8px;cursor:pointer;font-size:16px;">Fechar</button>
            </div></body></html>
            """)

        contact = dict(contact)
        contact_id = contact["id"]

        # Detectar mudanca de emprego
        job_change = None
        old_company = contact.get("empresa")
        old_title = contact.get("cargo")
        new_company = parsed_data.get("company")
        new_title = parsed_data.get("title")

        def normalize(s):
            return (s or "").lower().strip()

        if normalize(old_company) and normalize(new_company) and normalize(old_company) != normalize(new_company):
            job_change = {"type": "job_change", "old_company": old_company, "new_company": new_company}
        elif normalize(old_title) and normalize(new_title) and normalize(old_title) != normalize(new_title):
            job_change = {"type": "promotion", "old_title": old_title, "new_title": new_title}

        # Registrar mudanca no historico
        if job_change:
            cursor.execute("""
                INSERT INTO linkedin_enrichment_history
                (contact_id, empresa_anterior, cargo_anterior, empresa_nova, cargo_nova,
                 headline_anterior, headline_nova, tipo_mudanca, dados_completos)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                contact_id, old_company, old_title, new_company, new_title,
                contact.get("linkedin_headline"), parsed_data.get("headline"),
                job_change["type"], json.dumps(parsed_data)
            ))

        # Validar foto com IA (se houver)
        photo_url = parsed_data.get("profile_picture")
        photo_validation = None
        photo_valid = False

        if photo_url:
            try:
                from services.photo_validation import validate_profile_photo
                photo_validation = await validate_profile_photo(photo_url)
                photo_valid = photo_validation.get("valid", False)
            except Exception as e:
                photo_validation = {"error": str(e)}
                photo_valid = True  # Se falhar validação, aceita a foto

        # Atualizar contato
        experience_json = json.dumps(parsed_data.get("experience", []))
        cursor.execute("""
            UPDATE contacts
            SET empresa = COALESCE(NULLIF(%s, ''), empresa),
                cargo = COALESCE(NULLIF(%s, ''), cargo),
                linkedin_headline = COALESCE(NULLIF(%s, ''), linkedin_headline),
                linkedin_location = COALESCE(NULLIF(%s, ''), linkedin_location),
                linkedin_experience = CASE WHEN %s != '[]' THEN %s::jsonb ELSE linkedin_experience END,
                linkedin_connections = COALESCE(%s, linkedin_connections),
                linkedin_enriched_at = CURRENT_TIMESTAMP,
                linkedin_previous_company = %s,
                linkedin_previous_title = %s,
                linkedin_job_changed_at = %s,
                foto_url = COALESCE(NULLIF(%s, ''), foto_url),
                enriquecimento_status = 'bookmarklet',
                ultimo_enriquecimento = CURRENT_TIMESTAMP,
                atualizado_em = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (
            parsed_data.get("company"),
            parsed_data.get("title"),
            parsed_data.get("headline"),
            parsed_data.get("location"),
            experience_json,
            experience_json,
            parsed_data.get("connections"),
            old_company if job_change else None,
            old_title if job_change else None,
            datetime.now() if job_change else None,
            photo_url if photo_valid else None,  # Só salva foto se válida
            contact_id
        ))

        conn.commit()
        conn.close()

        # Retornar pagina de sucesso
        job_change_html = ""
        if job_change:
            job_change_html = '<div style="background:#fef3c7;padding:12px;border-radius:8px;margin-top:16px;"><b>🔔 Mudança detectada!</b></div>'

        # Dados extraidos para mostrar no popup
        extracted_info = []
        if parsed_data.get("headline"): extracted_info.append(f"📝 {parsed_data.get('headline')[:60]}")
        if parsed_data.get("location"): extracted_info.append(f"📍 {parsed_data.get('location')}")
        if parsed_data.get("company"): extracted_info.append(f"🏢 {parsed_data.get('company')}")
        if parsed_data.get("title"): extracted_info.append(f"💼 {parsed_data.get('title')}")
        if parsed_data.get("connections"): extracted_info.append(f"🔗 {parsed_data.get('connections')} conexões")
        # Resultado da validação de foto
        if photo_url:
            if photo_validation:
                if photo_valid:
                    desc = photo_validation.get("description", "")[:50]
                    extracted_info.append(f"📷 Foto ✅ {desc}")
                else:
                    num_people = photo_validation.get("num_people", "?")
                    desc = photo_validation.get("description", "")[:40]
                    extracted_info.append(f"📷 Foto ❌ Rejeitada ({num_people} pessoas: {desc})")
            else:
                extracted_info.append(f"📷 Foto capturada")

        extracted_html = "<br>".join(extracted_info) if extracted_info else "<span style='color:#f59e0b;'>⚠️ Nenhum dado extra extraído</span>"
        # Debug: mostrar chaves recebidas
        keys_received = ", ".join([k for k in parsed_data.keys() if not k.startswith('_')])
        extracted_html += f"<br><small style='color:#999;'>Keys: {keys_received}</small>"
        # Debug: mostrar texto capturado
        debug_text = parsed_data.get("_debug_text", "")[:200]
        if debug_text:
            extracted_html += f"<br><details><summary style='color:#999;font-size:11px;cursor:pointer;'>Debug text</summary><pre style='font-size:10px;max-height:100px;overflow:auto;text-align:left;'>{debug_text}...</pre></details>"

        # Foto para mostrar no popup
        photo_html = ""
        if parsed_data.get("profile_picture"):
            photo_html = f'<img src="{parsed_data.get("profile_picture")}" style="width:80px;height:80px;border-radius:50%;object-fit:cover;margin-bottom:8px;">'
        else:
            photo_html = '<div style="font-size:64px;">✅</div>'

        return HTMLResponse(content=f"""
        <html><body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#dcfce7;">
        <div style="text-align:center;padding:40px;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);max-width:450px;">
        {photo_html}
        <h2 style="margin:8px 0 16px 0;">{contact['nome']}</h2>
        <div style="text-align:left;background:#f0fdf4;padding:12px;border-radius:8px;font-size:13px;margin:12px 0;">
            {extracted_html}
        </div>
        {job_change_html}
        <button onclick="fechar()" style="margin-top:16px;padding:12px 40px;background:#22c55e;color:white;border:none;border-radius:8px;cursor:pointer;font-size:16px;">Fechar</button>
        <p style="margin-top:8px;font-size:11px;color:#666;">Fechando em <span id="countdown">2</span>s...</p>
        </div>
        <script>
        // Notifica a aba do bookmarklet para atualizar
        try {{
            const bc = new BroadcastChannel('intel-linkedin');
            bc.postMessage({{type: 'enriched', contactId: {contact_id}, name: '{contact["nome"]}'}});
        }} catch(e) {{}}

        function fechar() {{
            window.close();
        }}

        // Auto-close em 2 segundos
        var count = 2;
        var timer = setInterval(function() {{
            count--;
            var el = document.getElementById('countdown');
            if (el) el.textContent = count;
            if (count <= 0) {{
                clearInterval(timer);
                fechar();
            }}
        }}, 1000);
        </script>
        </body></html>
        """)

    except Exception as e:
        import traceback
        return error_page("Erro no servidor", traceback.format_exc())

@app.post("/api/linkedin/bookmarklet")
async def linkedin_bookmarklet_receive(request: Request):
    """
    Recebe dados do LinkedIn extraidos pelo bookmarklet.
    Encontra o contato pela URL do LinkedIn e atualiza os dados.
    """
    try:
        data = await request.json()
    except:
        return JSONResponse(
            content={"success": False, "error": "Invalid JSON"},
            status_code=400,
            headers=CORS_HEADERS
        )

    linkedin_url = data.get("linkedin_url", "").strip()
    if not linkedin_url:
        return JSONResponse(
            content={"success": False, "error": "linkedin_url is required"},
            status_code=400,
            headers=CORS_HEADERS
        )

    # Normalizar URL
    linkedin_url_normalized = linkedin_url.lower()
    if "/in/" in linkedin_url_normalized:
        # Extrair username
        import re
        match = re.search(r'linkedin\.com/in/([^/?\s]+)', linkedin_url_normalized)
        if match:
            username = match.group(1)
            linkedin_url_normalized = f"https://www.linkedin.com/in/{username}"

    conn = get_db()
    cursor = conn.cursor()

    # Buscar contato pela URL do LinkedIn (busca flexivel)
    cursor.execute("""
        SELECT id, nome, empresa, cargo, linkedin_headline
        FROM contacts
        WHERE LOWER(linkedin) LIKE %s
           OR LOWER(linkedin) LIKE %s
        LIMIT 1
    """, (f"%{username}%", f"%{linkedin_url_normalized}%"))

    contact = cursor.fetchone()

    if not contact:
        conn.close()
        return JSONResponse(
            content={
                "success": False,
                "error": "contact_not_found",
                "message": f"Nenhum contato encontrado com LinkedIn: {linkedin_url}",
                "linkedin_url": linkedin_url
            },
            headers=CORS_HEADERS
        )

    contact = dict(contact)
    contact_id = contact["id"]

    # Detectar mudanca de emprego
    job_change = None
    old_company = contact.get("empresa")
    old_title = contact.get("cargo")
    new_company = data.get("company")
    new_title = data.get("title")

    def normalize(s):
        return (s or "").lower().strip()

    if normalize(old_company) and normalize(new_company) and normalize(old_company) != normalize(new_company):
        job_change = {
            "type": "job_change",
            "old_company": old_company,
            "new_company": new_company,
            "old_title": old_title,
            "new_title": new_title
        }
    elif normalize(old_title) and normalize(new_title) and normalize(old_title) != normalize(new_title):
        job_change = {
            "type": "promotion",
            "old_company": old_company,
            "new_company": new_company,
            "old_title": old_title,
            "new_title": new_title
        }

    # Registrar mudanca no historico se detectada
    if job_change:
        cursor.execute("""
            INSERT INTO linkedin_enrichment_history
            (contact_id, empresa_anterior, cargo_anterior, empresa_nova, cargo_nova,
             headline_anterior, headline_nova, tipo_mudanca, dados_completos)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            contact_id,
            old_company,
            old_title,
            new_company,
            new_title,
            contact.get("linkedin_headline"),
            data.get("headline"),
            job_change["type"],
            json.dumps(data)
        ))

    # Atualizar contato com dados do bookmarklet
    exp_json = json.dumps(data.get("experience", []))
    edu_json = json.dumps(data.get("education", []))
    cursor.execute("""
        UPDATE contacts
        SET empresa = COALESCE(NULLIF(%s, ''), empresa),
            cargo = COALESCE(NULLIF(%s, ''), cargo),
            linkedin_headline = COALESCE(NULLIF(%s, ''), linkedin_headline),
            linkedin_location = COALESCE(NULLIF(%s, ''), linkedin_location),
            linkedin_about = COALESCE(NULLIF(%s, ''), linkedin_about),
            linkedin_experience = CASE WHEN %s != '[]' THEN %s::jsonb ELSE linkedin_experience END,
            linkedin_education = CASE WHEN %s != '[]' THEN %s::jsonb ELSE linkedin_education END,
            linkedin_connections = COALESCE(%s, linkedin_connections),
            linkedin_enriched_at = CURRENT_TIMESTAMP,
            linkedin_previous_company = %s,
            linkedin_previous_title = %s,
            linkedin_job_changed_at = %s,
            foto_url = COALESCE(NULLIF(%s, ''), foto_url),
            enriquecimento_status = 'bookmarklet',
            ultimo_enriquecimento = CURRENT_TIMESTAMP,
            atualizado_em = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (
        data.get("company"),
        data.get("title"),
        data.get("headline"),
        data.get("location"),
        data.get("about"),
        exp_json, exp_json,
        edu_json, edu_json,
        data.get("connections"),
        old_company if job_change else None,
        old_title if job_change else None,
        datetime.now() if job_change else None,
        data.get("profile_picture"),
        contact_id
    ))

    conn.commit()
    conn.close()

    return JSONResponse(
        content={
            "success": True,
            "contact_id": contact_id,
            "nome": contact["nome"],
            "job_change": job_change,
            "message": f"Dados atualizados para {contact['nome']}" + (" - MUDANCA DE EMPREGO DETECTADA!" if job_change else "")
        },
        headers=CORS_HEADERS
    )


@app.get("/api/linkedin/stats")
async def linkedin_enrichment_stats():
    """Estatisticas de enriquecimento LinkedIn"""
    service = get_linkedin_enrichment_service()
    stats = service.get_enrichment_stats()
    stats["api_configured"] = service.is_configured()
    return stats


@app.get("/api/linkedin/pending")
async def linkedin_pending_enrichments(limit: int = 100):
    """Lista contatos pendentes de enriquecimento"""
    service = get_linkedin_enrichment_service()
    pending = service.get_pending_enrichments(limit)
    return {"pending": pending, "total": len(pending)}


@app.get("/api/linkedin/job-changes")
async def linkedin_job_changes(days: int = 30, notified: bool = None):
    """Lista mudancas de emprego detectadas"""
    service = get_linkedin_enrichment_service()
    changes = service.get_job_changes(days, notified)
    return {"job_changes": changes, "total": len(changes)}


@app.post("/api/contacts/{contact_id}/linkedin/enrich")
async def enrich_contact_linkedin(contact_id: int, force: bool = False):
    """Enriquece um contato com dados do LinkedIn"""
    service = get_linkedin_enrichment_service()

    if not service.is_configured():
        raise HTTPException(
            status_code=503,
            detail="LinkedIn API not configured. Set RAPIDAPI_KEY environment variable."
        )

    result = await service.enrich_contact(contact_id, force=force)

    if "error" in result and result.get("code") == "NOT_FOUND":
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@app.post("/api/linkedin/enrich/batch")
async def enrich_linkedin_batch(
    limit: int = 50,
    circulo_max: int = 3,
    force: bool = False,
    background_tasks: BackgroundTasks = None
):
    """
    Enriquece multiplos contatos em batch.

    Args:
        limit: Numero maximo de contatos (default 50)
        circulo_max: Processar contatos ate este circulo (default 3)
        force: Re-enriquecer mesmo os ja processados
    """
    service = get_linkedin_enrichment_service()

    if not service.is_configured():
        raise HTTPException(
            status_code=503,
            detail="LinkedIn API not configured. Set RAPIDAPI_KEY environment variable."
        )

    # Para batches grandes, processar em background
    if limit > 10:
        async def run_batch():
            return await service.enrich_batch(limit=limit, circulo_max=circulo_max, force=force)

        if background_tasks:
            background_tasks.add_task(run_batch)
            return {
                "status": "started",
                "message": f"Batch enrichment started for up to {limit} contacts",
                "circulo_max": circulo_max
            }

    # Para batches pequenos, processar sincrono
    result = await service.enrich_batch(limit=limit, circulo_max=circulo_max, force=force)
    return result


# ============== APOLLO.IO ENRICHMENT ==============

@app.get("/api/apollo/stats")
async def apollo_stats(request: Request):
    """Retorna estatísticas de enriquecimento Apollo.io"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.apollo_service import get_apollo_service
    service = get_apollo_service()
    stats = await service.get_enrichment_stats()
    return stats


@app.post("/api/contacts/{contact_id}/apollo/enrich")
async def apollo_enrich_contact(contact_id: int, request: Request):
    """
    Enriquece um contato usando Apollo.io API.
    Requer APOLLO_API_KEY configurada no .env
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.apollo_service import get_apollo_service
    service = get_apollo_service()

    if not service.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Apollo.io API não configurada. Configure APOLLO_API_KEY no .env"
        )

    result = await service.enrich_contact(contact_id)

    if not result.get("success"):
        if "não encontrado" in result.get("error", "").lower():
            raise HTTPException(status_code=404, detail=result["error"])
        raise HTTPException(status_code=400, detail=result.get("error", "Erro desconhecido"))

    return result


@app.post("/api/apollo/enrich/batch")
async def apollo_enrich_batch(
    request: Request,
    limit: int = 50,
    circulo_max: int = 2
):
    """
    Enriquece múltiplos contatos via Apollo.io em batch.

    Args:
        limit: Número máximo de contatos (default 50)
        circulo_max: Processar contatos até este círculo (default 2)
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.apollo_service import get_apollo_service
    service = get_apollo_service()

    if not service.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Apollo.io API não configurada. Configure APOLLO_API_KEY no .env"
        )

    # Buscar contatos pendentes de enriquecimento
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, linkedin
            FROM contacts
            WHERE linkedin IS NOT NULL
              AND linkedin != ''
              AND linkedin_enriched_at IS NULL
              AND (circulo_profissional <= %s OR circulo_pessoal <= %s)
            ORDER BY
                LEAST(COALESCE(circulo_profissional, 99), COALESCE(circulo_pessoal, 99)),
                nome
            LIMIT %s
        """, (circulo_max, circulo_max, limit))
        contacts = cursor.fetchall()

    results = {
        "total": len(contacts),
        "success": 0,
        "failed": 0,
        "not_found": 0,
        "credits_used": 0,
        "errors": []
    }

    for contact in contacts:
        result = await service.enrich_contact(contact["id"])
        if result.get("success"):
            if result.get("matched"):
                results["success"] += 1
                results["credits_used"] += result.get("credits_used", 1)
            else:
                results["not_found"] += 1
        else:
            results["failed"] += 1
            results["errors"].append({
                "contact_id": contact["id"],
                "nome": contact["nome"],
                "error": result.get("error")
            })

    return results


@app.get("/api/apollo/sample")
async def apollo_get_sample(request: Request, limit: int = 50):
    """
    Retorna amostra de contatos para teste de enriquecimento.
    20 do Círculo 1, 15 do Círculo 2, resto aleatório.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        # Círculo 1 (20)
        cursor.execute("""
            SELECT id, nome, linkedin, empresa, cargo,
                   circulo_profissional, circulo_pessoal, linkedin_enriched_at
            FROM contacts
            WHERE linkedin IS NOT NULL AND linkedin != ''
              AND (circulo_profissional = 1 OR circulo_pessoal = 1)
              AND linkedin_enriched_at IS NULL
            ORDER BY RANDOM()
            LIMIT 20
        """)
        c1 = [dict(r) for r in cursor.fetchall()]

        # Círculo 2 (15)
        cursor.execute("""
            SELECT id, nome, linkedin, empresa, cargo,
                   circulo_profissional, circulo_pessoal, linkedin_enriched_at
            FROM contacts
            WHERE linkedin IS NOT NULL AND linkedin != ''
              AND (circulo_profissional = 2 OR circulo_pessoal = 2)
              AND linkedin_enriched_at IS NULL
            ORDER BY RANDOM()
            LIMIT 15
        """)
        c2 = [dict(r) for r in cursor.fetchall()]

        # Aleatórios (resto)
        remaining = limit - len(c1) - len(c2)
        cursor.execute("""
            SELECT id, nome, linkedin, empresa, cargo,
                   circulo_profissional, circulo_pessoal, linkedin_enriched_at
            FROM contacts
            WHERE linkedin IS NOT NULL AND linkedin != ''
              AND linkedin_enriched_at IS NULL
              AND id NOT IN %s
            ORDER BY RANDOM()
            LIMIT %s
        """, (tuple([c["id"] for c in c1 + c2]) or (0,), remaining))
        others = [dict(r) for r in cursor.fetchall()]

    sample = c1 + c2 + others

    return {
        "sample": sample,
        "total": len(sample),
        "breakdown": {
            "circulo_1": len(c1),
            "circulo_2": len(c2),
            "outros": len(others)
        }
    }


# Contact search routes - MUST come before /api/contacts/{contact_id}
@app.get("/api/contacts/search")
async def search_contacts_api(
    request: Request,
    q: str = None,
    circulo: int = None,
    tags: str = None,
    health_min: int = None,
    health_max: int = None,
    has_email: bool = None,
    has_whatsapp: bool = None,
    empresa: str = None,
    contexto: str = None,
    ordem: str = "nome",
    limit: int = 50,
    offset: int = 0
):
    """Busca avancada de contatos com multiplos filtros"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    try:
        service = get_search_service()

        # Parse tags from comma-separated string
        tags_list = [t.strip() for t in tags.split(",")] if tags else None

        return service.search_contacts(
            query=q,
            circulo=circulo,
            tags=tags_list,
            health_min=health_min,
            health_max=health_max,
            has_email=has_email,
            has_whatsapp=has_whatsapp,
            empresa=empresa,
            contexto=contexto,
            ordem=ordem,
            limit=limit,
            offset=offset
        )
    except Exception as e:
        logger.error(f"Error in contact search: {e}")
        return {"contacts": [], "total": 0, "error": str(e)}


@app.get("/api/contacts/suggestions")
async def get_contact_suggestions(
    request: Request,
    q: str = "",
    limit: int = 10
):
    """Sugestoes de autocomplete para busca"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    if not q or len(q) < 2:
        return {"suggestions": []}

    try:
        service = get_search_service()
        results = service.get_search_suggestions(q, limit)
        return {"suggestions": results}
    except Exception as e:
        logger.error(f"Error in contact suggestions: {e}")
        return {"suggestions": [], "error": str(e)}


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

    # Recalcular health_score em tempo real baseado no ultimo_contato atual
    circulo = contact.get("circulo_profissional") or contact.get("circulo_pessoal") or contact.get("circulo") or 5
    contact["health_score"] = calcular_health_score(contact, circulo)

    # Buscar memórias/notas
    cursor.execute('''
        SELECT *, 'memory' as item_type FROM contact_memories
        WHERE contact_id = %s
        ORDER BY data_ocorrencia DESC
        LIMIT 50
    ''', (contact_id,))
    memories = [row_to_dict(r) for r in cursor.fetchall()]

    # Buscar interações manuais
    cursor.execute('''
        SELECT id, tipo, titulo, descricao, data_interacao as data_ocorrencia,
               tags, sentimento, criado_em, 'interaction' as item_type
        FROM contact_interactions
        WHERE contact_id = %s
        ORDER BY data_interacao DESC
        LIMIT 50
    ''', (contact_id,))
    interactions = [row_to_dict(r) for r in cursor.fetchall()]

    # Merge memories and interactions, sort by date
    memories = memories + interactions
    def get_sort_date(x):
        dt = x.get('data_ocorrencia') or x.get('criado_em')
        if dt is None:
            return ''
        return str(dt) if not isinstance(dt, str) else dt
    memories.sort(key=get_sort_date, reverse=True)

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
                      'linkedin', 'contexto', 'categorias', 'tags', 'aniversario',
                      'circulo', 'circulo_manual', 'foto_url', 'enderecos',
                      'relacionamentos', 'datas_importantes', 'manual_notes',
                      'google_drive_folder_id']

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
                             datas_importantes, enderecos, relacionamentos,
                             google_contact_id, origem)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        json.dumps(contact.datas_importantes),
        json.dumps(contact.enderecos),
        json.dumps(contact.relacionamentos),
        contact.google_contact_id,
        contact.origem
    ))

    contact_id = cursor.fetchone()['id']
    conn.commit()
    conn.close()

    return {"id": contact_id, "status": "created"}


# Mapeamento de relacionamentos inversos
RELACIONAMENTOS_INVERSOS = {
    'conjuge': 'conjuge',
    'pai': 'filho',
    'mae': 'filho',
    'filho': 'pai',  # ou mae - determinado pelo contexto se disponivel
    'irmao': 'irmao',
    'avo': 'neto',
    'neto': 'avo',
    'tio': 'sobrinho',
    'sobrinho': 'tio',
    'primo': 'primo',
    'cunhado': 'cunhado',
    'sogro': 'genro',
    'sogra': 'genro',
    'genro': 'sogro',
    'nora': 'sogro',
    'chefe': 'subordinado',
    'subordinado': 'chefe',
    'assistente': 'chefe',
    'socio': 'socio',
    'amigo': 'amigo',
}

# Relacionamentos profissionais (nao sao familia)
RELACIONAMENTOS_PROFISSIONAIS = {'chefe', 'subordinado', 'assistente', 'socio', 'amigo', 'colega'}


class RelationshipUpdate(BaseModel):
    tipo: str
    nome: str
    contact_id: Optional[int] = None


@app.post("/api/contacts/{contact_id}/relationships")
async def add_relationship(contact_id: int, relationship: RelationshipUpdate):
    """
    Adiciona um relacionamento bidirecional.
    Se o contato relacionado existir (contact_id), adiciona a relacao inversa nele.
    """
    conn = get_db()
    cursor = conn.cursor()

    # Verificar se contato existe
    cursor.execute("SELECT id, nome, relacionamentos FROM contacts WHERE id = %s", (contact_id,))
    contact = cursor.fetchone()
    if not contact:
        conn.close()
        raise HTTPException(status_code=404, detail="Contato nao encontrado")

    contact = row_to_dict(contact)
    relacionamentos = contact.get('relacionamentos') or []
    if isinstance(relacionamentos, str):
        relacionamentos = json.loads(relacionamentos)

    # Adicionar nova relacao (ou atualizar existente)
    existing_idx = None
    for i, rel in enumerate(relacionamentos):
        if rel.get('contact_id') == relationship.contact_id and relationship.contact_id:
            existing_idx = i
            break
        if rel.get('nome', '').lower() == relationship.nome.lower() and not relationship.contact_id:
            existing_idx = i
            break

    new_rel = {
        'tipo': relationship.tipo,
        'nome': relationship.nome,
        'contact_id': relationship.contact_id
    }

    if existing_idx is not None:
        relacionamentos[existing_idx] = new_rel
    else:
        relacionamentos.append(new_rel)

    # Atualizar contato principal
    cursor.execute(
        "UPDATE contacts SET relacionamentos = %s, atualizado_em = NOW() WHERE id = %s",
        (json.dumps(relacionamentos), contact_id)
    )

    # Se ha um contato vinculado, adicionar relacao inversa
    inverse_added = False
    if relationship.contact_id:
        cursor.execute(
            "SELECT id, nome, relacionamentos FROM contacts WHERE id = %s",
            (relationship.contact_id,)
        )
        related_contact = cursor.fetchone()

        if related_contact:
            related_contact = row_to_dict(related_contact)
            related_rels = related_contact.get('relacionamentos') or []
            if isinstance(related_rels, str):
                related_rels = json.loads(related_rels)

            # Determinar tipo inverso
            tipo_inverso = RELACIONAMENTOS_INVERSOS.get(relationship.tipo, 'parente')

            # Verificar se ja existe relacao inversa
            inverse_exists = False
            for i, rel in enumerate(related_rels):
                if rel.get('contact_id') == contact_id:
                    # Atualizar relacao existente
                    related_rels[i] = {
                        'tipo': tipo_inverso,
                        'nome': contact['nome'],
                        'contact_id': contact_id
                    }
                    inverse_exists = True
                    break

            if not inverse_exists:
                related_rels.append({
                    'tipo': tipo_inverso,
                    'nome': contact['nome'],
                    'contact_id': contact_id
                })

            cursor.execute(
                "UPDATE contacts SET relacionamentos = %s, atualizado_em = NOW() WHERE id = %s",
                (json.dumps(related_rels), relationship.contact_id)
            )
            inverse_added = True

    conn.commit()
    conn.close()

    return {
        "status": "ok",
        "relationship": new_rel,
        "inverse_added": inverse_added,
        "inverse_type": RELACIONAMENTOS_INVERSOS.get(relationship.tipo) if inverse_added else None
    }


@app.delete("/api/contacts/{contact_id}/relationships/{related_contact_id}")
async def remove_relationship(contact_id: int, related_contact_id: int):
    """
    Remove um relacionamento de ambos os contatos (bidirecional).
    """
    conn = get_db()
    cursor = conn.cursor()

    # Atualizar contato principal
    cursor.execute("SELECT relacionamentos FROM contacts WHERE id = %s", (contact_id,))
    result = cursor.fetchone()
    if result:
        rels = result['relacionamentos'] or []
        if isinstance(rels, str):
            rels = json.loads(rels)
        rels = [r for r in rels if r.get('contact_id') != related_contact_id]
        cursor.execute(
            "UPDATE contacts SET relacionamentos = %s, atualizado_em = NOW() WHERE id = %s",
            (json.dumps(rels), contact_id)
        )

    # Atualizar contato relacionado
    cursor.execute("SELECT relacionamentos FROM contacts WHERE id = %s", (related_contact_id,))
    result = cursor.fetchone()
    if result:
        rels = result['relacionamentos'] or []
        if isinstance(rels, str):
            rels = json.loads(rels)
        rels = [r for r in rels if r.get('contact_id') != contact_id]
        cursor.execute(
            "UPDATE contacts SET relacionamentos = %s, atualizado_em = NOW() WHERE id = %s",
            (json.dumps(rels), related_contact_id)
        )

    conn.commit()
    conn.close()

    return {"status": "ok", "message": "Relacionamento removido de ambos os contatos"}


@app.get("/api/contacts/{contact_id}/relationships/inferred")
async def get_inferred_relationships(contact_id: int):
    """
    Retorna relacionamentos inferidos com base nas relacoes familiares.
    Ex: Se A e pai de B, e B e pai de C, entao A e avo de C.
    """
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, nome, relacionamentos FROM contacts WHERE id = %s", (contact_id,))
    contact = cursor.fetchone()
    if not contact:
        conn.close()
        return {"inferred": []}

    contact = row_to_dict(contact)
    relacionamentos = contact.get('relacionamentos') or []
    if isinstance(relacionamentos, str):
        relacionamentos = json.loads(relacionamentos)

    inferred = []

    # Para cada pessoa relacionada diretamente
    for rel in relacionamentos:
        if not rel.get('contact_id'):
            continue

        rel_tipo = rel['tipo']
        rel_id = rel['contact_id']

        # Buscar relacionamentos dessa pessoa
        cursor.execute("SELECT id, nome, relacionamentos FROM contacts WHERE id = %s", (rel_id,))
        rel_contact = cursor.fetchone()
        if not rel_contact:
            continue

        rel_contact = row_to_dict(rel_contact)
        rel_rels = rel_contact.get('relacionamentos') or []
        if isinstance(rel_rels, str):
            rel_rels = json.loads(rel_rels)

        # Inferir relacoes de segundo grau
        for rel2 in rel_rels:
            if rel2.get('contact_id') == contact_id:
                continue  # Pular o proprio contato

            rel2_tipo = rel2['tipo']
            inferred_tipo = infer_relationship(rel_tipo, rel2_tipo)

            if inferred_tipo and inferred_tipo != 'desconhecido':
                inferred.append({
                    'tipo': inferred_tipo,
                    'nome': rel2.get('nome'),
                    'contact_id': rel2.get('contact_id'),
                    'via': {
                        'nome': rel['nome'],
                        'tipo': rel_tipo,
                        'contact_id': rel_id
                    }
                })

    conn.close()

    return {"inferred": inferred}


def infer_relationship(rel1: str, rel2: str) -> str:
    """
    Infere o tipo de relacionamento baseado em dois relacionamentos.
    Ex: pai + irmao = tio, pai + pai = avo
    """
    # Matriz de inferencia familiar
    inference_matrix = {
        ('pai', 'pai'): 'avo',
        ('pai', 'mae'): 'avo',
        ('mae', 'pai'): 'avo',
        ('mae', 'mae'): 'avo',
        ('pai', 'irmao'): 'tio',
        ('pai', 'irmao'): 'tio',
        ('mae', 'irmao'): 'tio',
        ('mae', 'irmao'): 'tio',
        ('tio', 'filho'): 'primo',
        ('tio', 'filho'): 'primo',
        ('irmao', 'filho'): 'sobrinho',
        ('irmao', 'filho'): 'sobrinho',
        ('filho', 'filho'): 'neto',
        ('filho', 'filho'): 'neto',
        ('conjuge', 'pai'): 'sogro',
        ('conjuge', 'mae'): 'sogra',
        ('conjuge', 'irmao'): 'cunhado',
        ('conjuge', 'irmao'): 'cunhado',
        ('pai', 'conjuge'): 'madrasta',  # ou padrasto
        ('mae', 'conjuge'): 'padrasto',  # ou madrasta
        ('irmao', 'conjuge'): 'cunhado',
    }

    return inference_matrix.get((rel1, rel2), 'desconhecido')


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
                "oportunidades": result.get("enrichment", {}).get("oportunidades", []),
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


@app.put("/api/contacts/{contact_id}/enrichment-data")
async def update_contact_enrichment_data(contact_id: int, request: Request):
    """
    Atualiza dados manuais de enriquecimento do contato.

    Body JSON:
    {
        "relationship_context": "Como conheci esta pessoa...",
        "linkedin_url": "https://linkedin.com/in/...",
        "company_website": "https://empresa.com.br",
        "empresa": "Nome da Empresa",
        "cargo": "Cargo da Pessoa",
        "manual_notes": "Notas adicionais..."
    }
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="JSON invalido")

    conn = get_db()

    try:
        from services.contact_enrichment import update_manual_enrichment

        result = await update_manual_enrichment(
            contact_id=contact_id,
            db_connection=conn,
            nome=data.get('nome'),
            contexto=data.get('contexto'),
            relationship_context=data.get('relationship_context'),
            linkedin_url=data.get('linkedin_url'),
            company_website=data.get('company_website'),
            empresa=data.get('empresa'),
            cargo=data.get('cargo'),
            manual_notes=data.get('manual_notes')
        )

        return result

    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


# ============== CONTACT INTELLIGENCE ==============

@app.get("/api/contacts/{contact_id}/article-suggestions")
async def get_article_suggestions(contact_id: int, limit: int = 3):
    """Sugere artigos para reconexao com o contato baseado no perfil"""
    from services.content_matcher import get_content_matcher

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, empresa, cargo, tags, contexto, circulo, health_score, ultimo_contato
            FROM contacts WHERE id = %s
        """, (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")
        contact = dict(contact)

    tags = contact.get('tags', [])
    if isinstance(tags, str):
        import json as _json
        tags = _json.loads(tags) if tags else []

    matcher = get_content_matcher()
    suggestions = []
    for i in range(limit):
        result = matcher.get_content_for_contact(
            contact_id=contact_id,
            contact_tags=tags,
            contact_setor=contact.get('empresa', ''),
            limit=1
        )
        if result and result not in suggestions:
            suggestions.append(result)

    # Calcular contexto de reconexao
    dias_sem_contato = None
    if contact.get('ultimo_contato'):
        from datetime import datetime as dt_cls
        ultimo = contact['ultimo_contato']
        dias_sem_contato = (dt_cls.now() - ultimo).days if not ultimo.tzinfo else (dt_cls.now(ultimo.tzinfo) - ultimo).days

    return {
        "contact": {"nome": contact['nome'], "empresa": contact.get('empresa'), "circulo": contact.get('circulo')},
        "dias_sem_contato": dias_sem_contato,
        "suggestions": suggestions
    }


@app.post("/api/contacts/{contact_id}/analyze-conversations")
async def analyze_contact_conversations(contact_id: int):
    """Analisa conversas recentes com o contato e identifica tom, intencoes, pendencias"""
    import httpx as _hx

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="API key nao configurada")

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT id, nome, empresa, cargo, circulo, contexto FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")
        contact = dict(contact)

        # Mensagens recentes (WA + email)
        cursor.execute("""
            SELECT m.conteudo, m.direcao, m.enviado_em, cv.canal
            FROM messages m
            JOIN conversations cv ON cv.id = m.conversation_id
            WHERE cv.contact_id = %s
              AND m.conteudo IS NOT NULL AND LENGTH(m.conteudo) > 5
            ORDER BY m.enviado_em DESC LIMIT 30
        """, (contact_id,))
        messages = [dict(r) for r in cursor.fetchall()]

    if not messages:
        return {"analysis": "Nenhuma mensagem recente encontrada para analisar.", "has_data": False}

    msgs_text = "\n".join([
        f"[{str(m.get('enviado_em','?'))[:16]}] {'RENATO' if m['direcao']=='outgoing' else contact['nome']}: {(m.get('conteudo') or '')[:400]}"
        for m in reversed(messages)
    ])

    prompt = f"""Analise as conversas recentes entre Renato e {contact['nome']}.

CONTATO: {contact['nome']} | {contact.get('cargo', '')} {('@ ' + contact['empresa']) if contact.get('empresa') else ''} | Circulo {contact.get('circulo', '?')}

CONVERSAS RECENTES:
{msgs_text}

Gere uma analise CURTA e ACIONAVEL (max 200 palavras):

1. **Tom da relacao**: como esta a dinamica entre eles?
2. **Assuntos em aberto**: o que ficou pendente?
3. **Oportunidades**: como Renato pode fortalecer esta relacao?
4. **Proxima acao sugerida**: 1 acao concreta com prazo

Seja direto. Portugues. Sem juridiques."""

    try:
        async with _hx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}
            )
        if resp.status_code == 200:
            analysis = resp.json()["content"][0]["text"]
            return {"analysis": analysis, "has_data": True, "messages_analyzed": len(messages)}
        raise HTTPException(status_code=resp.status_code, detail="Erro na API")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/contacts/{contact_id}/intelligence/chat")
async def contact_intelligence_chat(contact_id: int, request: Request):
    """
    Chat with AI about a contact. Ask questions and get intelligent answers.

    Body JSON:
    {
        "question": "O que ele faz profissionalmente?"
    }
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = {}
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="JSON invalido")

    question = data.get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Pergunta nao informada")

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT id, nome FROM contacts WHERE id = %s', (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")

        from services.contact_intelligence import chat_about_contact
        result = await chat_about_contact(contact_id, question, conn)

        return result

    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


@app.post("/api/contacts/{contact_id}/intelligence/suggest-response")
async def contact_suggest_response(contact_id: int, request: Request):
    """
    Suggest a response message for the contact.

    Body JSON:
    {
        "context_type": "reply" | "reconnect"
    }
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = {}
    try:
        data = await request.json()
    except:
        pass

    context_type = data.get("context_type", "reply")

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT id, nome FROM contacts WHERE id = %s', (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")

        from services.contact_intelligence import suggest_response
        result = await suggest_response(contact_id, conn, context_type)

        return result

    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


@app.post("/api/contacts/{contact_id}/enrich-with-context")
async def enrich_contact_with_context(contact_id: int, request: Request):
    """
    Enriquece contato usando o contexto do relacionamento informado pelo usuario.

    Body JSON:
    {
        "relationship_context": "Participa comigo do Conselho Consultivo..."
    }
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = {}
    try:
        data = await request.json()
    except:
        pass

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('SELECT id, nome FROM contacts WHERE id = %s', (contact_id,))
    contact = cursor.fetchone()
    if not contact:
        conn.close()
        raise HTTPException(status_code=404, detail="Contato nao encontrado")

    cursor.execute('''
        UPDATE contacts SET enriquecimento_status = 'pending'
        WHERE id = %s
    ''', (contact_id,))
    conn.commit()

    try:
        from services.contact_enrichment import enrich_with_context

        result = await enrich_with_context(
            contact_id=contact_id,
            db_connection=conn,
            relationship_context=data.get('relationship_context')
        )

        if result.get("status") == "success":
            return {
                "status": "success",
                "resumo": result.get("enrichment", {}).get("resumo", ""),
                "fatos": result.get("enrichment", {}).get("fatos", []),
                "insights": result.get("enrichment", {}).get("insights", {}),
                "sugestoes": result.get("enrichment", {}).get("sugestoes", [])
            }
        else:
            cursor.execute('''
                UPDATE contacts SET enriquecimento_status = 'failed'
                WHERE id = %s
            ''', (contact_id,))
            conn.commit()
            return result

    except Exception as e:
        cursor.execute('''
            UPDATE contacts SET enriquecimento_status = 'failed'
            WHERE id = %s
        ''', (contact_id,))
        conn.commit()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


@app.post("/api/contacts/{contact_id}/web-search-company")
async def web_search_company_info(contact_id: int, request: Request):
    """
    Busca informacoes da empresa na web usando o email corporativo ou website cadastrado.
    Usa AI para extrair informacoes estruturadas da pagina da empresa.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('SELECT id, nome, empresa FROM contacts WHERE id = %s', (contact_id,))
    contact = cursor.fetchone()
    if not contact:
        conn.close()
        raise HTTPException(status_code=404, detail="Contato nao encontrado")

    try:
        from services.contact_enrichment import enrich_contact_with_web_search

        result = await enrich_contact_with_web_search(
            contact_id=contact_id,
            db_connection=conn
        )

        return result

    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


@app.post("/api/contacts/auto-enrich-priority")
async def auto_enrich_priority_contacts_endpoint(
    request: Request,
    limit: int = 10,
    circulo_max: int = 2
):
    """
    Enriquece automaticamente contatos prioritarios (circulos 1 e 2).
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    conn = get_db()

    try:
        from services.contact_enrichment import auto_enrich_priority_contacts

        result = await auto_enrich_priority_contacts(
            db_connection=conn,
            circulo_max=circulo_max,
            limit=limit
        )
        return result

    except Exception as e:
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


# ============== Google Drive Integration ==============

from integrations.google_drive import (
    get_valid_token,
    list_folders,
    get_folder_contents,
    get_file_metadata,
    create_folder,
    upload_file,
    search_files,
    get_folder_path,
    index_document_to_db,
    link_document_to_entity,
    get_documents_for_entity,
    index_folder_documents,
    fix_document_column_shift,
    watch_folder,
    stop_watch_channel
)


@app.get("/api/drive/folders")
async def api_list_drive_folders(parent_id: str = None, account_type: str = 'professional'):
    """Lista pastas do Google Drive

    Args:
        parent_id: ID da pasta pai (opcional)
        account_type: 'professional' ou 'personal'
    """
    conn = get_db()
    try:
        access_token = await get_valid_token(conn, account_type)
        if not access_token:
            raise HTTPException(status_code=401, detail=f"Google Drive ({account_type}) não conectado")

        folders = await list_folders(access_token, parent_id)
        return {"folders": folders, "account_type": account_type}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.get("/api/drive/accounts")
async def api_list_drive_accounts():
    """Lista contas do Google Drive disponíveis"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT tipo, email, conectado
        FROM google_accounts
        WHERE conectado = TRUE
        ORDER BY tipo
    """)
    rows = cursor.fetchall()
    conn.close()

    accounts = []
    for row in rows:
        accounts.append({
            "type": row['tipo'],
            "email": row['email'],
            "label": "Profissional" if row['tipo'] == 'professional' else "Pessoal"
        })

    return {"accounts": accounts}


@app.get("/api/drive/folders/{folder_id}/contents")
async def api_get_folder_contents(folder_id: str, account_type: str = 'professional'):
    """Lista conteúdo de uma pasta"""
    conn = get_db()

    access_token = await get_valid_token(conn, account_type)
    if not access_token:
        conn.close()
        raise HTTPException(status_code=401, detail=f"Google Drive ({account_type}) não conectado")

    try:
        contents = await get_folder_contents(access_token, folder_id)
        path = await get_folder_path(access_token, folder_id)
        conn.close()
        return {"contents": contents, "path": path}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/drive/folders/{folder_id}/link")
async def api_link_folder_to_entity(folder_id: str, request: Request):
    """Vincula uma pasta do Drive a um projeto ou contato"""
    body = await request.json()
    entidade_tipo = body.get("entidade_tipo")  # 'projeto' ou 'contato'
    entidade_id = body.get("entidade_id")

    if not entidade_tipo or not entidade_id:
        raise HTTPException(status_code=400, detail="entidade_tipo e entidade_id são obrigatórios")

    conn = get_db()
    cursor = conn.cursor()

    try:
        if entidade_tipo == "projeto":
            cursor.execute(
                "UPDATE projects SET google_drive_folder_id = %s WHERE id = %s",
                (folder_id, entidade_id)
            )
        elif entidade_tipo == "contato":
            cursor.execute(
                "UPDATE contacts SET google_drive_folder_id = %s WHERE id = %s",
                (folder_id, entidade_id)
            )
        else:
            conn.close()
            raise HTTPException(status_code=400, detail="entidade_tipo deve ser 'projeto' ou 'contato'")

        conn.commit()
        conn.close()
        return {"status": "ok", "message": f"Pasta vinculada ao {entidade_tipo} {entidade_id}"}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/drive/folders/{folder_id}/index")
async def api_index_folder(folder_id: str, request: Request):
    """Indexa todos os documentos de uma pasta"""
    body = await request.json()
    entidade_tipo = body.get("entidade_tipo")
    entidade_id = body.get("entidade_id")
    account_type = body.get("account_type", "professional")

    conn = get_db()

    access_token = await get_valid_token(conn, account_type)
    if not access_token:
        conn.close()
        raise HTTPException(status_code=401, detail=f"Google Drive ({account_type}) não conectado")

    try:
        count = await index_folder_documents(
            conn,
            access_token,
            folder_id,
            entidade_tipo,
            entidade_id
        )
        conn.close()
        return {"status": "ok", "indexed": count}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/drive/upload")
async def api_upload_file(
    file: UploadFile = File(...),
    folder_id: str = Query(None),
    projeto_id: int = Query(None),
    contato_id: int = Query(None),
    account_type: str = Query('professional')
):
    """
    Upload de arquivo para o Google Drive com vinculação a projeto/contato
    """
    conn = get_db()

    access_token = await get_valid_token(conn, account_type)
    if not access_token:
        conn.close()
        raise HTTPException(status_code=401, detail=f"Google Drive ({account_type}) não conectado")

    try:
        # Se não especificou folder_id, mas especificou projeto, usar pasta do projeto
        if not folder_id and projeto_id:
            cursor = conn.cursor()
            cursor.execute("SELECT google_drive_folder_id FROM projects WHERE id = %s", (projeto_id,))
            row = cursor.fetchone()
            if row and row['google_drive_folder_id']:
                folder_id = row['google_drive_folder_id']

        # Se não especificou folder_id, mas especificou contato, usar pasta do contato
        if not folder_id and contato_id:
            cursor = conn.cursor()
            cursor.execute("SELECT google_drive_folder_id FROM contacts WHERE id = %s", (contato_id,))
            row = cursor.fetchone()
            if row and row['google_drive_folder_id']:
                folder_id = row['google_drive_folder_id']

        # Upload para o Drive
        file_content = await file.read()
        result = await upload_file(
            access_token,
            file_content,
            file.filename,
            file.content_type or "application/octet-stream",
            folder_id
        )

        # Indexar no banco
        doc_id = index_document_to_db(
            conn,
            google_drive_id=result['id'],
            nome=file.filename,
            mime_type=file.content_type,
            web_view_link=f"https://drive.google.com/file/d/{result['id']}/view",
            tamanho_bytes=len(file_content),
            pasta_id=folder_id
        )

        # Vincular a projeto/contato
        if projeto_id:
            link_document_to_entity(conn, doc_id, 'projeto', projeto_id)
        if contato_id:
            link_document_to_entity(conn, doc_id, 'contato', contato_id)

        conn.close()
        return {
            "status": "ok",
            "documento_id": doc_id,
            "google_drive_id": result['id'],
            "nome": file.filename
        }
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/documentos/{entidade_tipo}/{entidade_id}")
async def api_get_entity_documents(entidade_tipo: str, entidade_id: int):
    """Retorna todos os documentos vinculados a uma entidade"""
    conn = get_db()

    try:
        docs = get_documents_for_entity(conn, entidade_tipo, entidade_id)
        conn.close()
        return {"documentos": docs}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/documentos/{documento_id}/link")
async def api_link_document(documento_id: int, request: Request):
    """Vincula um documento existente a uma entidade adicional"""
    body = await request.json()
    entidade_tipo = body.get("entidade_tipo")
    entidade_id = body.get("entidade_id")

    if not entidade_tipo or not entidade_id:
        raise HTTPException(status_code=400, detail="entidade_tipo e entidade_id são obrigatórios")

    conn = get_db()

    try:
        link_document_to_entity(conn, documento_id, entidade_tipo, entidade_id)
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/drive/search")
async def api_search_drive(q: str, folder_id: str = None):
    """Busca arquivos no Google Drive"""
    conn = get_db()

    access_token = await get_valid_token(conn)
    if not access_token:
        conn.close()
        raise HTTPException(status_code=401, detail="Google Drive não conectado")

    try:
        results = await search_files(access_token, q, folder_id)
        conn.close()
        return {"results": results}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


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


# ============== CRON JOBS ==============
# Vercel Cron Jobs - executados automaticamente
# Configurados em vercel.json

def verify_cron_auth(request: Request) -> bool:
    """Verifica autorizacao de cron job"""
    auth_header = request.headers.get("authorization", "")
    cron_secret = os.getenv("CRON_SECRET", "")
    is_vercel_cron = request.headers.get("x-vercel-cron") == "true"

    return (
        is_vercel_cron or
        auth_header == f"Bearer {cron_secret}" or
        os.getenv("VERCEL_ENV") != "production"
    )


@app.get("/api/cron/daily-sync")
async def cron_daily_sync(request: Request):
    """
    Cron: Sincronizacao diaria completa (5h da manha).

    Executa sequencialmente:
    1. Health recalc
    2. Sync Contacts
    3. Sync Calendar
    4. Sync Tasks
    5. Sync Gmail
    6. Sync WhatsApp
    7. AI suggestions generation
    8. Campaign steps processing
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    import asyncio as _aio

    results = {
        "job": "daily-sync",
        "started_at": datetime.now().isoformat(),
        "steps": {}
    }

    # Helper para executar step com error handling
    async def run_step(name, func):
        try:
            result = await func()
            results["steps"][name] = {"status": "success", **(result if isinstance(result, dict) else {"result": result})}
        except Exception as e:
            results["steps"][name] = {"status": "error", "error": str(e)}

    # ==================== FASE 1: Sync dados (paralelo) ====================

    async def step_health():
        from services.circulos import calcular_health_score
        updated = 0
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, circulo, ultimo_contato, total_interacoes FROM contacts WHERE COALESCE(circulo, 5) <= 4")
            for contact in cursor.fetchall():
                try:
                    health = calcular_health_score(dict(contact), contact["circulo"])
                    cursor.execute("UPDATE contacts SET health_score = %s WHERE id = %s", (health, contact["id"]))
                    updated += 1
                except Exception:
                    pass
            conn.commit()
        return {"updated": updated}

    async def step_contacts():
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE")
            accounts = [dict(row) for row in cursor.fetchall()]
        synced = 0
        for account in accounts:
            try:
                stats = await sync_contacts_incremental(
                    access_token=account["access_token"], refresh_token=account["refresh_token"],
                    account_email=account["email"], sync_token=account.get("sync_token"), db_connection=conn)
                synced += stats.get("total_changes", 0)
            except Exception:
                pass
        return {"changes": synced}

    async def step_calendar():
        from services.calendar_sync import get_calendar_sync
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT email FROM google_accounts WHERE conectado = TRUE")
            accounts = cursor.fetchall()
        count = 0
        for account in accounts:
            try:
                await get_calendar_sync().incremental_sync(account["email"])
                count += 1
            except Exception:
                pass
        return {"accounts": count}

    async def step_tasks():
        from services.tasks_sync import get_tasks_sync_service
        return await get_tasks_sync_service().full_sync()

    await _aio.gather(
        run_step("health_recalc", step_health),
        run_step("sync_contacts", step_contacts),
        run_step("sync_calendar", step_calendar),
        run_step("sync_tasks", step_tasks),
    )

    # ==================== FASE 2: Comunicacao (paralelo) ====================

    async def step_gmail():
        from services.gmail_sync import get_gmail_sync_service
        return await get_gmail_sync_service().sync_all_contacts(months_back=1)

    async def step_whatsapp():
        from services.whatsapp_sync import get_whatsapp_sync_service
        return await get_whatsapp_sync_service().sync_all_chats(include_groups=False)

    async def step_payment():
        from services.payment_cycle import check_payment_replies
        with get_db() as conn:
            token = await get_valid_token(conn, 'professional')
        if token:
            return await check_payment_replies(token)
        return {"skipped": "no token"}

    async def step_fup():
        from services.smart_fup import check_pending_fups
        with get_db() as conn:
            token = await get_valid_token(conn, 'professional')
        if token:
            return await check_pending_fups(token)
        return {"skipped": "no token"}

    await _aio.gather(
        run_step("sync_gmail", step_gmail),
        run_step("sync_whatsapp", step_whatsapp),
        run_step("payment_cycle", step_payment),
        run_step("smart_fup", step_fup),
    )

    # ==================== FASE 3: IA e processamento (paralelo) ====================

    async def step_ai():
        from services.ai_agent import get_ai_agent
        return await get_ai_agent().run_daily_generation()

    async def step_campaigns():
        from services.campaign_executor import CampaignExecutor
        return CampaignExecutor().process_pending_steps(limit=200)

    async def step_enrich():
        from services.contact_enrichment import auto_enrich_priority_contacts
        with get_db() as conn:
            r = await auto_enrich_priority_contacts(conn, circulo_max=2, limit=5)
        return {"enriched": len(r)}

    async def step_avatars():
        from services.avatar_fetcher import get_avatar_fetcher
        return await get_avatar_fetcher().fetch_photos_batch(limit=10, delay_between=1.0)

    async def step_group_docs():
        from services.project_smart_update import download_group_documents
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT project_id FROM project_whatsapp_groups WHERE ativo = TRUE")
            pids = [r['project_id'] for r in cursor.fetchall()]
        r = {}
        for pid in pids:
            r[pid] = await download_group_documents(pid)
        return {"projects": len(pids)}

    async def step_clipping():
        from services.news_hub import generate_daily_clipping
        r = await generate_daily_clipping(limit=10)
        return {"items": len(r.get('clipping', []))}

    async def step_social_groups():
        from services.social_groups import sync_all_groups_cache
        return await sync_all_groups_cache()

    async def step_group_messages():
        from services.group_message_sync import sync_group_messages
        return await sync_group_messages(limit_per_group=50)

    async def step_linkedin_enrichment():
        from services.linkedin_enrichment import get_linkedin_enrichment
        from services.action_proposals import get_action_proposals
        enricher = get_linkedin_enrichment()
        result = await enricher.enrich_batch(limit=10, circulo_max=3)

        # Create action proposals for job changes
        proposals_created = 0
        for r in result.get("results", []):
            jc = r.get("job_change")
            if jc:
                ap = get_action_proposals()
                change_type = "mudou de empresa" if jc["type"] == "job_change" else "foi promovido(a)"
                ap.create_proposal({
                    "action_type": "linkedin_job_change",
                    "contact_id": r["contact_id"],
                    "title": f'{r["nome"]} {change_type}',
                    "description": f'De {jc.get("old_company", "?")} ({jc.get("old_title", "?")}) para {jc.get("new_company", "?")} ({jc.get("new_title", "?")})',
                    "trigger_text": f'Detectado via LinkedIn em {jc.get("detected_at", "")[:10]}',
                    "ai_reasoning": "Mudanca de emprego e oportunidade para reconexao e parabens",
                    "confidence": 0.9,
                    "urgency": "high",
                    "action_params": {"job_change": jc},
                })
                # Mark as notified in history
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE linkedin_enrichment_history SET notificado = TRUE
                        WHERE contact_id = %s AND notificado = FALSE
                        ORDER BY detectado_em DESC LIMIT 1
                    """, (r["contact_id"],))
                    conn.commit()
                proposals_created += 1

        result["proposals_created"] = proposals_created
        return result

    await _aio.gather(
        run_step("daily_ai", step_ai),
        run_step("campaigns", step_campaigns),
        run_step("auto_enrich", step_enrich),
        run_step("avatar_fetch", step_avatars),
        run_step("group_docs", step_group_docs),
        run_step("daily_clipping", step_clipping),
        run_step("social_groups_cache", step_social_groups),
        run_step("group_messages_sync", step_group_messages),
        run_step("linkedin_enrichment", step_linkedin_enrichment),
    )

    results["completed_at"] = datetime.now().isoformat()

    return results


# Individual cron endpoints (kept for manual triggering)

    return (
        is_vercel_cron or
        auth_header == f"Bearer {cron_secret}" or
        os.getenv("VERCEL_ENV") != "production"
    )


@app.get("/api/cron/sync-calendar")
async def cron_sync_calendar(request: Request):
    """
    Cron: Sincroniza eventos do Google Calendar.
    Schedule: 0 8,12,18 * * * (8h, 12h, 18h)
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.calendar_sync import get_calendar_sync

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM google_accounts WHERE conectado = TRUE")
        accounts = cursor.fetchall()

    results = []
    for account in accounts:
        try:
            sync = get_calendar_sync()
            stats = await sync.incremental_sync(account["email"])
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

    return {
        "job": "sync-calendar",
        "timestamp": datetime.now().isoformat(),
        "accounts": len(accounts),
        "results": results
    }


@app.get("/api/cron/sync-tasks")
async def cron_sync_tasks(request: Request):
    """
    Cron: Sincronizacao bidirecional de tarefas com Google Tasks.
    Schedule: 0 7,13,19 * * * (7h, 13h, 19h)
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.tasks_sync import get_tasks_sync_service

    try:
        tasks_service = get_tasks_sync_service()
        result = await tasks_service.full_sync()

        if "error" in result:
            return {"job": "sync-tasks", "status": "error", "error": result["error"]}

        return {
            "job": "sync-tasks",
            "timestamp": datetime.now().isoformat(),
            "status": "success",
            "push": result.get("push", {}),
            "pull": result.get("pull", {})
        }

    except Exception as e:
        return {"job": "sync-tasks", "status": "error", "error": str(e)}


@app.get("/api/cron/daily-ai")
async def cron_daily_ai(request: Request):
    """
    Cron: Executa geracao diaria de sugestoes AI.
    Schedule: 0 6 * * * (6h da manha)

    Inclui:
    - Sugestoes de reconexao
    - Lembretes de aniversario
    - Follow-ups pendentes
    - Alertas de health baixo
    - Auto-enriquecimento C1-C2
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.ai_agent import get_ai_agent

    agent = get_ai_agent()

    try:
        results = await agent.run_daily_generation()
        return {
            "job": "daily-ai",
            "timestamp": datetime.now().isoformat(),
            "status": "success",
            "results": results
        }
    except Exception as e:
        return {"job": "daily-ai", "status": "error", "error": str(e)}


@app.get("/api/cron/health-recalc")
async def cron_health_recalc(request: Request):
    """
    Cron: Recalcula health scores de todos os contatos.
    Schedule: 0 5 * * * (5h da manha)
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.circulos import calcular_health_score

    updated = 0
    errors = 0

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contatos com circulo definido
        cursor.execute("""
            SELECT id, nome, circulo, ultimo_contato, total_interacoes
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 4
        """)
        contacts = cursor.fetchall()

        for contact in contacts:
            try:
                contact_dict = dict(contact)
                health = calcular_health_score(contact_dict, contact["circulo"])

                cursor.execute("""
                    UPDATE contacts
                    SET health_score = %s, atualizado_em = NOW()
                    WHERE id = %s
                """, (health, contact["id"]))
                updated += 1

            except Exception as e:
                errors += 1

        conn.commit()

    return {
        "job": "health-recalc",
        "timestamp": datetime.now().isoformat(),
        "status": "success",
        "contacts_updated": updated,
        "errors": errors
    }


@app.get("/api/cron/cleanup")
async def cron_cleanup(request: Request):
    """
    Cron: Limpeza de dados expirados.
    Schedule: 0 4 * * 0 (Domingos as 4h)

    Limpa:
    - Sugestoes AI expiradas
    - Notificacoes antigas (> 30 dias)
    - Tokens expirados
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.ai_agent import get_ai_agent

    stats = {
        "suggestions_deleted": 0,
        "notifications_deleted": 0,
        "old_predictions_deleted": 0
    }

    # 1. Cleanup sugestoes expiradas
    agent = get_ai_agent()
    stats["suggestions_deleted"] = agent.cleanup_expired_suggestions()

    # 1b. Expirar action_proposals antigas (>7 dias)
    try:
        from services.action_proposals import ActionProposalsService
        stats["proposals_expired"] = ActionProposalsService().expire_old_proposals()
    except Exception as e:
        stats["proposals_expired"] = f"error: {e}"

    # 2. Cleanup notificacoes antigas
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM sse_notifications
            WHERE criado_em < NOW() - INTERVAL '30 days'
        """)
        stats["notifications_deleted"] = cursor.rowcount

        # 3. Cleanup predicoes antigas
        cursor.execute("""
            DELETE FROM health_predictions
            WHERE data_predicao < NOW() - INTERVAL '90 days'
        """)
        stats["old_predictions_deleted"] = cursor.rowcount

        conn.commit()

    return {
        "job": "cleanup",
        "timestamp": datetime.now().isoformat(),
        "status": "success",
        "stats": stats
    }


@app.get("/api/cron/sync-gmail")
async def cron_sync_gmail(request: Request):
    """
    Cron: Sincroniza emails do Gmail.
    Schedule: 0 10 * * * (10h diario)

    Sincroniza emails recentes (ultimos 7 dias) de todos os contatos.
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.gmail_sync import get_gmail_sync_service

    try:
        service = get_gmail_sync_service()
        # Sync ultimos 7 dias para cron diario
        result = await service.sync_all_contacts(months_back=1)

        return {
            "job": "sync-gmail",
            "timestamp": datetime.now().isoformat(),
            "status": "success",
            "result": result
        }
    except Exception as e:
        return {"job": "sync-gmail", "status": "error", "error": str(e)}


@app.get("/api/cron/sync-whatsapp")
async def cron_sync_whatsapp(request: Request):
    """
    Cron: Sincroniza mensagens do WhatsApp.
    Schedule: 0 11 * * * (11h diario)

    Sincroniza novos chats e mensagens do WhatsApp.
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.whatsapp_sync import get_whatsapp_sync_service

    try:
        service = get_whatsapp_sync_service()
        result = await service.sync_all_chats(include_groups=False)

        return {
            "job": "sync-whatsapp",
            "timestamp": datetime.now().isoformat(),
            "status": "success",
            "result": result
        }
    except Exception as e:
        return {"job": "sync-whatsapp", "status": "error", "error": str(e)}


@app.get("/api/cron/weekly-digest")
async def cron_weekly_digest(request: Request):
    """
    Cron: Gera digest semanal.
    Schedule: 0 8 * * 1 (Segundas as 8h)
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.digest_generator import get_digest_generator

    try:
        generator = get_digest_generator()
        digest = generator.generate_weekly_digest()

        return {
            "job": "weekly-digest",
            "timestamp": datetime.now().isoformat(),
            "status": "success",
            "digest_id": digest.get("id") if digest else None
        }
    except Exception as e:
        return {"job": "weekly-digest", "status": "error", "error": str(e)}


@app.get("/api/cron/sync-whatsapp-history")
async def cron_sync_whatsapp_history(request: Request):
    """
    Cron: Sync WhatsApp message history from Evolution API for contacts
    that have a phone number but no conversation in the DB.
    Schedule: 0 6 * * * (daily at 6am UTC)
    Processes 50 contacts per run to avoid timeout.
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    import asyncio as _asyncio

    whatsapp_client = WhatsAppIntegration()
    if not whatsapp_client.base_url or not whatsapp_client.api_key:
        return {"job": "sync-whatsapp-history", "status": "error",
                "error": "Evolution API not configured"}

    batch_limit = 50
    synced = 0
    total_messages = 0
    errors = 0

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Find contacts with phone but no WhatsApp conversation
            cursor.execute("""
                SELECT c.id, c.nome, c.telefones
                FROM contacts c
                WHERE c.telefones IS NOT NULL
                  AND c.telefones::text != '[]'
                  AND NOT EXISTS (
                      SELECT 1 FROM conversations conv
                      WHERE conv.contact_id = c.id AND conv.canal = 'whatsapp'
                  )
                ORDER BY c.ultimo_contato DESC NULLS LAST
                LIMIT %s
            """, (batch_limit,))
            contacts_to_sync = cursor.fetchall()

        if not contacts_to_sync:
            return {"job": "sync-whatsapp-history", "status": "success",
                    "message": "No contacts to sync", "synced": 0}

        for row in contacts_to_sync:
            phones = row['telefones'] if isinstance(row['telefones'], list) else []
            phone = None
            for p in phones:
                phone_num = p.get('number', '') if isinstance(p, dict) else str(p)
                digits = ''.join(filter(str.isdigit, phone_num))
                if len(digits) >= 8:
                    phone = digits
                    break
            if not phone:
                continue

            try:
                messages = await whatsapp_client.get_messages_for_chat(phone, limit=100)
                if not messages:
                    # Brief pause even on empty to respect rate limit
                    await _asyncio.sleep(0.5)
                    continue

                with get_db() as conn:
                    cursor = conn.cursor()

                    cursor.execute("""
                        INSERT INTO conversations (contact_id, canal, status, criado_em, atualizado_em)
                        VALUES (%s, 'whatsapp', 'open', NOW(), NOW())
                        RETURNING id
                    """, (row['id'],))
                    conversation_id = cursor.fetchone()["id"]

                    # Check existing message IDs
                    cursor.execute("""
                        SELECT external_id FROM messages
                        WHERE contact_id = %s AND external_id IS NOT NULL
                    """, (row['id'],))
                    existing_ids = {r['external_id'] for r in cursor.fetchall()}
                    cursor.execute("""
                        SELECT metadata->>'message_id' as msg_id FROM messages
                        WHERE contact_id = %s AND metadata->>'message_id' IS NOT NULL
                    """, (row['id'],))
                    existing_ids.update(r['msg_id'] for r in cursor.fetchall())

                    inserted = 0
                    latest_ts = None

                    for msg in messages:
                        parsed = whatsapp_client.parse_stored_message(msg)
                        if not parsed:
                            continue
                        msg_id_ext = parsed.get("message_id")
                        if msg_id_ext and msg_id_ext in existing_ids:
                            continue
                        ts = parsed.get("timestamp")
                        cursor.execute("""
                            INSERT INTO messages
                            (conversation_id, contact_id, external_id, direcao, conteudo,
                             enviado_em, metadata, criado_em)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        """, (
                            conversation_id, row['id'], msg_id_ext,
                            parsed.get("direction"), parsed.get("content"), ts,
                            json.dumps({
                                "phone": phone,
                                "push_name": parsed.get("push_name"),
                                "message_id": msg_id_ext,
                                "message_type": parsed.get("message_type"),
                                "is_group": False,
                                "source": "cron_history_sync",
                            })
                        ))
                        inserted += 1
                        if msg_id_ext:
                            existing_ids.add(msg_id_ext)
                        if ts and (latest_ts is None or ts > latest_ts):
                            latest_ts = ts

                    if inserted > 0:
                        cursor.execute("""
                            UPDATE conversations
                            SET total_mensagens = %s, ultimo_mensagem = %s, atualizado_em = NOW()
                            WHERE id = %s
                        """, (inserted, latest_ts, conversation_id))
                        cursor.execute("""
                            UPDATE contacts
                            SET ultimo_contato = GREATEST(COALESCE(ultimo_contato, '1970-01-01'), %s),
                                total_interacoes = COALESCE(total_interacoes, 0) + %s
                            WHERE id = %s
                        """, (latest_ts, inserted, row['id']))
                        synced += 1
                        total_messages += inserted
                    else:
                        cursor.execute("DELETE FROM conversations WHERE id = %s",
                                       (conversation_id,))

                    conn.commit()

            except Exception as e:
                print(f"[sync-whatsapp-history] Error for contact {row['id']}: {e}")
                errors += 1

            # Rate limit: 1 req/sec
            await _asyncio.sleep(1.0)

    except Exception as e:
        print(f"[sync-whatsapp-history] Fatal error: {e}")
        return {"job": "sync-whatsapp-history", "status": "error", "error": str(e)}

    return {
        "job": "sync-whatsapp-history",
        "timestamp": datetime.now().isoformat(),
        "status": "success",
        "contacts_checked": len(contacts_to_sync),
        "conversations_synced": synced,
        "messages_imported": total_messages,
        "errors": errors,
    }


# ============== Google Drive Cron & Webhooks ==============

@app.get("/api/cron/index-drive-documents")
async def cron_index_drive_documents(request: Request):
    """
    Cron: Re-index Google Drive folders for all projects that have a linked folder.
    Schedule: 0 7 * * * (daily at 7am UTC)
    Rate-limited to 1 folder per 2 seconds to avoid Google API throttling.
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    total_indexed = 0
    folders_processed = 0
    errors = []

    try:
        conn = get_db()
        cursor = conn.cursor()

        # Get all projects with a Google Drive folder
        cursor.execute("""
            SELECT id, nome, google_drive_folder_id
            FROM projects
            WHERE google_drive_folder_id IS NOT NULL
              AND google_drive_folder_id != ''
        """)
        projects = cursor.fetchall()

        access_token = await get_valid_token(conn, 'professional')
        if not access_token:
            conn.close()
            return {
                "job": "index-drive-documents",
                "status": "error",
                "error": "Google Drive not connected"
            }

        for project in projects:
            try:
                count = await index_folder_documents(
                    conn,
                    access_token,
                    project['google_drive_folder_id'],
                    entidade_tipo='projeto',
                    entidade_id=project['id']
                )
                total_indexed += count
                folders_processed += 1
            except Exception as e:
                errors.append({
                    "project_id": project['id'],
                    "project_name": project['nome'],
                    "error": str(e)
                })

            # Rate limit: 1 folder per 2 seconds
            await asyncio.sleep(2)

        conn.close()
    except Exception as e:
        return {
            "job": "index-drive-documents",
            "status": "error",
            "error": str(e)
        }

    return {
        "job": "index-drive-documents",
        "timestamp": datetime.now().isoformat(),
        "status": "success",
        "folders_processed": folders_processed,
        "documents_indexed": total_indexed,
        "errors": errors
    }


@app.post("/api/webhooks/google-drive")
async def webhook_google_drive(request: Request):
    """
    Webhook: Receives Google Drive push notifications when files change.
    Google sends POST requests here when watched folders are modified.
    No user auth required - verified via channel token.
    """
    # Google Drive sends these headers
    channel_id = request.headers.get("x-goog-channel-id", "")
    resource_state = request.headers.get("x-goog-resource-state", "")
    resource_id = request.headers.get("x-goog-resource-id", "")
    channel_token = request.headers.get("x-goog-channel-token", "")

    # Verify channel token
    expected_token = os.getenv("DRIVE_WEBHOOK_TOKEN", "")
    if not expected_token or channel_token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid channel token")

    # Google sends a 'sync' message when watch is first created - acknowledge it
    if resource_state == "sync":
        return {"status": "ok", "message": "sync acknowledged"}

    # Only process 'update' and 'add' events
    if resource_state not in ("update", "add", "change"):
        return {"status": "ok", "message": f"ignored state: {resource_state}"}

    # Extract folder_id from channel_id (format: drive-watch-{folder_id}-{random})
    parts = channel_id.split("-")
    if len(parts) >= 4 and parts[0] == "drive" and parts[1] == "watch":
        folder_id = parts[2]
    else:
        return {"status": "ok", "message": "unrecognized channel format"}

    # Re-index the folder in background
    try:
        conn = get_db()

        # Find the project linked to this folder
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM projects WHERE google_drive_folder_id = %s LIMIT 1
        """, (folder_id,))
        project = cursor.fetchone()

        access_token = await get_valid_token(conn, 'professional')
        if access_token:
            entidade_tipo = 'projeto' if project else None
            entidade_id = project['id'] if project else None

            count = await index_folder_documents(
                conn,
                access_token,
                folder_id,
                entidade_tipo=entidade_tipo,
                entidade_id=entidade_id
            )
            conn.close()
            return {"status": "ok", "reindexed": count, "folder_id": folder_id}
        else:
            conn.close()
            return {"status": "error", "message": "No valid access token"}

    except Exception as e:
        print(f"[drive-webhook] Error processing notification: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/drive/watch/{project_id}")
async def api_watch_project_folder(project_id: int, request: Request):
    """
    Set up Google Drive push notifications for a project's folder.
    The watch expires after ~7 days and needs renewal.
    """
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT google_drive_folder_id, nome FROM projects WHERE id = %s", (project_id,))
    project = cursor.fetchone()
    if not project or not project['google_drive_folder_id']:
        conn.close()
        raise HTTPException(status_code=404, detail="Project not found or no Drive folder linked")

    access_token = await get_valid_token(conn, 'professional')
    if not access_token:
        conn.close()
        raise HTTPException(status_code=401, detail="Google Drive not connected")

    # Build webhook URL
    base_url = os.getenv("BASE_URL", "https://intel.almeida-prado.com")
    webhook_url = f"{base_url}/api/webhooks/google-drive"

    # Channel token for verification
    channel_token = os.getenv("DRIVE_WEBHOOK_TOKEN", "")
    if not channel_token:
        conn.close()
        raise HTTPException(status_code=500, detail="DRIVE_WEBHOOK_TOKEN not configured")

    try:
        result = await watch_folder(
            access_token,
            project['google_drive_folder_id'],
            webhook_url,
            channel_token
        )

        # Store watch info for later renewal/cleanup
        cursor.execute("""
            INSERT INTO drive_watches (project_id, folder_id, channel_id, resource_id, expiration)
            VALUES (%s, %s, %s, %s, to_timestamp(%s / 1000.0))
            ON CONFLICT (project_id) DO UPDATE SET
                channel_id = EXCLUDED.channel_id,
                resource_id = EXCLUDED.resource_id,
                expiration = EXCLUDED.expiration,
                updated_at = NOW()
        """, (
            project_id,
            project['google_drive_folder_id'],
            result.get('id'),
            result.get('resourceId'),
            result.get('expiration')
        ))
        conn.commit()
        conn.close()

        return {
            "status": "ok",
            "channel_id": result.get('id'),
            "expiration": result.get('expiration'),
            "project": project['nome']
        }
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


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


# ============== Scoring de Contacts (Google Contacts) ==============
# Endpoints para scoring de contacts sincronizados do Google
# Adicionado por INST-3

@app.post("/api/contacts/scoring/recalculate")
async def api_contacts_scoring_recalculate(
    batch_size: int = 200,
    offset: int = 0,
    user: dict = Depends(require_admin)
):
    """
    Recalcula scores de contacts (Google Contacts) em batches.
    Usa informações enriquecidas: emails, telefones, linkedin_headline, tags, etc.
    Requer permissão de admin.
    """
    try:
        stats = scorer.recalculate_contact_scores(batch_size=batch_size, offset=offset)
        return {
            "success": True,
            "message": f"Batch processado: {stats['total_processados']} contacts",
            "stats": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao recalcular scores de contacts: {str(e)}")


@app.get("/api/contacts/scoring/stats")
async def api_contacts_scoring_stats(user: dict = Depends(require_admin)):
    """
    Retorna estatísticas do scoring de contacts.
    Inclui: distribuição por tier, médias, totais.
    Requer permissão de admin.
    """
    try:
        stats = scorer.get_contact_scoring_stats()
        return {
            "success": True,
            "stats": stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao obter stats de contacts: {str(e)}")


# ============== DASHBOARD API v1 ==============
# API unificada para o Dashboard principal
# Implementado por: INTEL (2026-03-25)

@app.get("/api/v1/test")
async def test_endpoint():
    """Test endpoint"""
    return {"status": "ok", "message": "API v1 working"}

# Cache do dashboard (60s TTL)
_dashboard_cache = {"data": None, "timestamp": None}

@app.get("/api/v1/dashboard")
async def get_dashboard_unified(request: Request):
    """
    Retorna TODOS os dados do Dashboard em uma unica chamada.
    Cache de 60s para evitar queries pesadas em cada page load.
    """
    import time
    from services.dashboard import get_full_dashboard

    # Usar cache se < 60s
    global _dashboard_cache
    if _dashboard_cache["data"] and _dashboard_cache["timestamp"]:
        age = (datetime.now() - _dashboard_cache["timestamp"]).total_seconds()
        if age < 60:
            return _dashboard_cache["data"]

    t0 = time.time()
    try:
        result = get_full_dashboard()
        _dashboard_cache = {"data": result, "timestamp": datetime.now()}
    except Exception as e:
        print(f"[DASHBOARD ERROR] {e}")
        result = {
            "stats": {},
            "circulos_resumo": {},
            "alertas": [],
            "contatos_recentes": []
        }

    # Inbox count - query simples separada
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as count FROM messages WHERE lida = FALSE")
            row = cursor.fetchone()
            result["inbox_unread"] = row["count"] if row else 0
    except:
        result["inbox_unread"] = 0

    result["tasks"] = []
    result["calendar_today"] = []
    result["aniversarios"] = []  # Ja incluso em alertas

    print(f"[DASHBOARD TIMING] total={round(time.time() - t0, 2)}s")
    return result


# ============== RODAS DE RELACIONAMENTO ==============
# Sistema de sugestoes inteligentes baseadas em contexto extraido de mensagens

# Templates de mensagem por tipo de roda
RODA_MESSAGE_TEMPLATES = {
    "birthday": [
        "Feliz aniversário, {nome}! 🎂 Desejo um dia incrível!",
        "Parabéns, {nome}! 🎉 Que este novo ano seja repleto de conquistas!",
    ],
    "promessa": [
        "Oi {nome}! Lembrei que ficamos de {conteudo}. Vou providenciar!",
        "{nome}, voltando sobre {conteudo}. Me desculpe a demora!",
    ],
    "favor_recebido": [
        "Oi {nome}! Queria agradecer novamente por {conteudo}. Muito obrigado!",
        "{nome}, lembrei de você! Aquele {conteudo} fez toda diferença.",
    ],
    "proximo_passo": [
        "Oi {nome}! Sobre {conteudo} - vamos confirmar?",
        "{nome}, só passando para lembrar: {conteudo}",
    ],
    "topico": [
        "Oi {nome}! Lembrei da nossa conversa sobre {conteudo}. Novidades?",
        "{nome}, estava pensando em {conteudo}. Podemos retomar?",
    ],
    "low_health": [
        "Oi {nome}! Faz tempo que não conversamos. Como você está?",
        "{nome}, sumimos! Vamos marcar um café?",
    ],
}


def get_message_template(reason: str, contact_name: str, roda_content: str = None) -> str:
    """Retorna um template de mensagem baseado no tipo."""
    import random

    # Extrair tipo base do reason (ex: roda_promessa -> promessa)
    tipo = reason.replace("roda_", "") if reason.startswith("roda_") else reason

    templates = RODA_MESSAGE_TEMPLATES.get(tipo, RODA_MESSAGE_TEMPLATES.get("low_health"))
    if not templates:
        return None

    template = random.choice(templates)

    # Substituir placeholders
    primeiro_nome = contact_name.split()[0] if contact_name else "amigo"
    message = template.replace("{nome}", primeiro_nome)

    if roda_content and "{conteudo}" in message:
        # Truncar conteudo se muito longo
        conteudo_curto = roda_content[:50] + "..." if len(roda_content) > 50 else roda_content
        message = message.replace("{conteudo}", conteudo_curto.lower())

    return message


def check_contacted_today(contact_ids: list) -> set:
    """
    Verifica quais contatos ja receberam mensagem hoje (automatico ou manual).
    Retorna set de contact_ids que foram contactados.
    """
    if not contact_ids:
        return set()

    from database import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        # Query otimizada: usa timestamp range para aproveitar indices
        cursor.execute("""
            SELECT DISTINCT contact_id FROM (
                -- Mensagens enviadas hoje (usando range para indice)
                SELECT contact_id
                FROM messages
                WHERE contact_id = ANY(%s)
                  AND direcao = 'outgoing'
                  AND enviado_em >= CURRENT_DATE
                  AND enviado_em < CURRENT_DATE + INTERVAL '1 day'

                UNION ALL

                -- Marcacoes manuais "Ja contatei"
                SELECT contact_id
                FROM contact_today_manual
                WHERE contact_id = ANY(%s)
                  AND data = CURRENT_DATE
            ) AS contacted
        """, (contact_ids, contact_ids))

        return {row["contact_id"] for row in cursor.fetchall()}


@app.get("/api/v1/contact-suggestions")
def get_contact_suggestions_v1(limit: int = 6, hide_contacted: bool = True):
    """
    Retorna contatos com sugestoes de acao baseadas em rodas de relacionamento.

    Priorizacao:
    1. Aniversario hoje
    2. Promessa pendente > 3 dias
    3. Favor recebido nao retribuido
    4. Health baixo (< 30%)
    5. Proximo passo vencido
    6. Topicos de interesse

    Args:
        limit: Numero maximo de sugestoes
        hide_contacted: Se True, esconde contatos que ja receberam mensagem hoje

    Returns:
        Lista de contatos com roda mais relevante e sugestao de acao
    """
    from database import get_db
    from services.content_matcher import get_content_matcher
    from services.business_matcher import get_business_match

    content_matcher = get_content_matcher()
    suggestions = []

    # Usar uma unica conexao para todas as queries
    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Buscar aniversarios de hoje (query otimizada)
        aniversarios_hoje = []
        try:
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, foto_url
                FROM contacts
                WHERE aniversario IS NOT NULL
                  AND EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
                  AND EXTRACT(DAY FROM aniversario) = EXTRACT(DAY FROM CURRENT_DATE)
                LIMIT 10
            """)
            for row in cursor.fetchall():
                a = dict(row)
                aniversarios_hoje.append({
                    "contact": {
                        "id": a["id"],
                        "nome": a["nome"],
                        "empresa": a.get("empresa"),
                        "cargo": a.get("cargo"),
                        "circulo": a.get("circulo"),
                        "foto_url": a.get("foto_url"),
                    },
                    "reason": "birthday",
                    "reason_label": "Aniversário hoje!",
                    "roda": None,
                    "priority": 0,
                    "message_template": get_message_template("birthday", a["nome"])
                })
        except Exception as e:
            print(f"[SUGGESTIONS] Error fetching birthdays: {e}")

        # 2. Buscar rodas pendentes (usa propria conexao interna)
        rodas_service = get_rodas_service()
        rodas_dashboard = []
        try:
            rodas_dashboard = rodas_service.get_rodas_para_dashboard(limit=limit * 2)
        except Exception as e:
            print(f"[SUGGESTIONS] Error fetching rodas: {e}")

        # 3. Buscar contatos com health baixo (query otimizada)
        contatos_health_baixo = []
        try:
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, foto_url, health_score, tags
                FROM contacts
                WHERE health_score IS NOT NULL
                  AND health_score < 30
                  AND circulo IS NOT NULL
                  AND circulo <= 3
                  AND NOT (tags && ARRAY['familia']::text[])
                ORDER BY health_score ASC, circulo ASC
                LIMIT %s
            """, (limit * 2,))
            contatos_health_baixo = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"[SUGGESTIONS] Error fetching low health contacts: {e}")

        # 4. Verificar quais contatos ja foram contactados hoje
        contacted_today = set()
        if hide_contacted:
            try:
                all_candidate_ids = []
                all_candidate_ids.extend([a["contact"]["id"] for a in aniversarios_hoje])
                all_candidate_ids.extend([r["contact"]["id"] for r in rodas_dashboard])
                all_candidate_ids.extend([c["id"] for c in contatos_health_baixo])

                if all_candidate_ids:
                    cursor.execute("""
                        SELECT DISTINCT contact_id FROM (
                            SELECT contact_id FROM messages
                            WHERE contact_id = ANY(%s) AND direcao = 'outgoing'
                              AND enviado_em >= CURRENT_DATE AND enviado_em < CURRENT_DATE + INTERVAL '1 day'
                            UNION ALL
                            SELECT contact_id FROM contact_today_manual
                            WHERE contact_id = ANY(%s) AND data = CURRENT_DATE
                        ) AS contacted
                    """, (all_candidate_ids, all_candidate_ids))
                    contacted_today = {row["contact_id"] for row in cursor.fetchall()}
                    if contacted_today:
                        print(f"[SUGGESTIONS] {len(contacted_today)} contatos ja contactados hoje, pulando...")
            except Exception as e:
                print(f"[SUGGESTIONS] Error checking contacted today: {e}")

    # Montar lista unificada com prioridade
    contact_ids_used = set()

    # Adicionar aniversarios (prioridade 0)
    for item in aniversarios_hoje:
        if len(suggestions) >= limit:
            break
        contact_id = item["contact"]["id"]
        if contact_id in contacted_today:
            continue  # Ja contactou hoje, pular
        if contact_id not in contact_ids_used:
            # Aniversario nao precisa de content_suggestion
            item["content_suggestion"] = None
            item["business_match"] = None  # Aniversario nao precisa de business match
            suggestions.append(item)
            contact_ids_used.add(contact_id)

    # Adicionar rodas com prioridade calculada
    for item in rodas_dashboard:
        if len(suggestions) >= limit:
            break

        contact_id = item["contact"]["id"]
        if contact_id in contacted_today:
            continue  # Ja contactou hoje, pular
        if contact_id in contact_ids_used:
            continue

        roda = item["roda"]
        tipo = roda["tipo"]
        dias_pendente = roda.get("dias_pendente", 0)

        # Calcular prioridade
        if tipo == "promessa" and dias_pendente > 3:
            priority = 1
            reason_label = f"Promessa pendente ({dias_pendente} dias)"
        elif tipo == "promessa":
            priority = 2
            reason_label = f"Promessa pendente"
        elif tipo == "favor_recebido":
            priority = 3
            reason_label = "Favor recebido - retribuir"
        elif tipo == "proximo_passo":
            priority = 4
            reason_label = "Próximo passo combinado"
        else:  # topico
            priority = 5
            reason_label = "Tópico para retomar"

        # Buscar sugestao de conteudo para este contato/roda
        content_suggestion = None
        try:
            content_suggestion = content_matcher.get_content_for_contact(
                contact_id=contact_id,
                roda=roda,
                contact_tags=item["contact"].get("tags"),
                contact_setor=item["contact"].get("setor") or item["contact"].get("empresa_setor")
            )
        except Exception as e:
            print(f"[SUGGESTIONS] Error getting content for contact {contact_id}: {e}")

        # Buscar business match para este contato
        business_match = None
        try:
            business_match = get_business_match(
                contact_tags=item["contact"].get("tags"),
                contact_cargo=item["contact"].get("cargo"),
                contact_empresa=item["contact"].get("empresa"),
                roda_tags=roda.get("tags") if roda else None,
                roda_conteudo=roda.get("conteudo") if roda else None
            )
        except Exception as e:
            print(f"[SUGGESTIONS] Error getting business match for contact {contact_id}: {e}")

        suggestions.append({
            "contact": item["contact"],
            "reason": f"roda_{tipo}",
            "reason_label": reason_label,
            "roda": roda,
            "priority": priority,
            "message_template": get_message_template(tipo, item["contact"]["nome"], roda.get("conteudo")),
            "content_suggestion": content_suggestion,
            "business_match": business_match
        })
        contact_ids_used.add(contact_id)

    # Adicionar contatos com health baixo (sem roda)
    for contato in contatos_health_baixo:
        if len(suggestions) >= limit:
            break

        contact_id = contato["id"]
        if contact_id in contacted_today:
            continue  # Ja contactou hoje, pular
        if contact_id in contact_ids_used:
            continue

        health = contato.get("health_score", 50)
        if health < 30:
            # Buscar sugestao de conteudo para contato sem roda
            content_suggestion = None
            try:
                content_suggestion = content_matcher.get_content_for_contact(
                    contact_id=contact_id,
                    roda=None,
                    contact_tags=contato.get("tags"),
                    contact_setor=contato.get("setor") or contato.get("empresa_setor")
                )
            except Exception as e:
                print(f"[SUGGESTIONS] Error getting content for low-health contact {contact_id}: {e}")

            # Buscar business match para contato com health baixo
            business_match = None
            try:
                business_match = get_business_match(
                    contact_tags=contato.get("tags"),
                    contact_cargo=contato.get("cargo"),
                    contact_empresa=contato.get("empresa")
                )
            except Exception as e:
                print(f"[SUGGESTIONS] Error getting business match for low-health contact {contact_id}: {e}")

            suggestions.append({
                "contact": {
                    "id": contato["id"],
                    "nome": contato["nome"],
                    "empresa": contato.get("empresa"),
                    "cargo": contato.get("cargo"),
                    "circulo": contato.get("circulo"),
                    "foto_url": contato.get("foto_url"),
                },
                "reason": "low_health",
                "reason_label": f"Relacionamento esfriando ({health}%)",
                "roda": None,
                "priority": 6,
                "message_template": get_message_template("low_health", contato["nome"]),
                "content_suggestion": content_suggestion,
                "business_match": business_match
            })
            contact_ids_used.add(contact_id)

    # Ordenar por prioridade
    suggestions.sort(key=lambda x: x["priority"])

    return {
        "suggestions": suggestions[:limit],
        "total_rodas_pendentes": len(rodas_dashboard),
        "contacted_today_count": len(contacted_today),
        "generated_at": datetime.now().isoformat()
    }


@app.post("/api/v1/rodas/{roda_id}/complete")
async def complete_roda(roda_id: int):
    """Marca uma roda como cumprida."""
    rodas_service = get_rodas_service()
    success = rodas_service.marcar_cumprida(roda_id)

    if not success:
        raise HTTPException(status_code=404, detail="Roda não encontrada")

    return {"status": "ok", "message": "Roda marcada como cumprida"}


@app.post("/api/v1/rodas/{roda_id}/expire")
async def expire_roda(roda_id: int):
    """Marca uma roda como expirada (não é mais relevante)."""
    rodas_service = get_rodas_service()
    success = rodas_service.marcar_expirada(roda_id)

    if not success:
        raise HTTPException(status_code=404, detail="Roda não encontrada")

    return {"status": "ok", "message": "Roda marcada como expirada"}


@app.post("/api/v1/contacts/{contact_id}/mark-contacted")
async def mark_contact_as_contacted(contact_id: int):
    """
    Marca um contato como 'ja contatei hoje' manualmente.
    Usado quando a mensagem nao foi sincronizada ou contato foi por outro meio.
    """
    from database import get_db

    with get_db() as conn:
        cursor = conn.cursor()

        # Verificar se contato existe
        cursor.execute("SELECT id, nome FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato não encontrado")

        # Inserir ou ignorar se ja existe
        cursor.execute("""
            INSERT INTO contact_today_manual (contact_id, data)
            VALUES (%s, CURRENT_DATE)
            ON CONFLICT (contact_id, data) DO NOTHING
        """, (contact_id,))
        conn.commit()

    return {
        "status": "ok",
        "message": f"Contato {contact['nome']} marcado como contatado hoje"
    }


@app.delete("/api/v1/contacts/{contact_id}/mark-contacted")
async def unmark_contact_as_contacted(contact_id: int):
    """Remove a marcacao manual de 'ja contatei hoje'."""
    from database import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM contact_today_manual
            WHERE contact_id = %s AND data = CURRENT_DATE
        """, (contact_id,))
        conn.commit()

    return {"status": "ok", "message": "Marcacao removida"}


@app.get("/api/v1/contacts/{contact_id}/rodas")
async def get_contact_rodas(contact_id: int, include_all: bool = False):
    """
    Retorna rodas de um contato específico.

    Args:
        contact_id: ID do contato
        include_all: Se True, inclui rodas cumpridas e expiradas
    """
    rodas_service = get_rodas_service()
    rodas = rodas_service.get_historico_contato(contact_id, include_all=include_all)

    return {
        "contact_id": contact_id,
        "rodas": rodas,
        "total": len(rodas)
    }


@app.post("/api/v1/rodas/expire-old")
async def expire_old_rodas(dias_promessa: int = 30, dias_outros: int = 90):
    """
    Expira automaticamente rodas antigas (para ser chamado por cron).

    Args:
        dias_promessa: Dias para expirar promessas (default 30)
        dias_outros: Dias para expirar outros tipos (default 90)

    Returns:
        Numero de rodas expiradas
    """
    rodas_service = get_rodas_service()
    count = rodas_service.expirar_rodas_antigas(dias_promessa, dias_outros)

    return {
        "status": "ok",
        "expired_count": count,
        "config": {
            "dias_promessa": dias_promessa,
            "dias_outros": dias_outros
        }
    }


@app.get("/api/v1/rodas/stats")
async def get_rodas_stats():
    """Retorna estatisticas das rodas."""
    from database import get_db

    with get_db() as conn:
        cursor = conn.cursor()

        # Total por status
        cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM contact_rodas
            GROUP BY status
        """)
        by_status = {row["status"]: row["count"] for row in cursor.fetchall()}

        # Total por tipo (apenas pendentes)
        cursor.execute("""
            SELECT tipo, COUNT(*) as count
            FROM contact_rodas
            WHERE status = 'pendente'
            GROUP BY tipo
        """)
        by_tipo = {row["tipo"]: row["count"] for row in cursor.fetchall()}

        # Rodas antigas (promessas > 7 dias, outros > 30 dias)
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM contact_rodas
            WHERE status = 'pendente'
            AND (
                (tipo = 'promessa' AND criado_em < NOW() - INTERVAL '7 days')
                OR (tipo != 'promessa' AND criado_em < NOW() - INTERVAL '30 days')
            )
        """)
        antigas = cursor.fetchone()["count"]

    return {
        "by_status": by_status,
        "by_tipo": by_tipo,
        "pendentes_antigas": antigas,
        "total_pendentes": sum(by_tipo.values()) if by_tipo else 0
    }


# ============== CIRCULOS ENDPOINTS ==============
# Sistema de classificacao de contatos em niveis de proximidade
# Implementado por: FLOW (2026-03-25)

@app.get("/api/circulos")
async def get_circulos(contexto: str = None):
    """Retorna configuracao e estatisticas dos circulos.

    Args:
        contexto: Filtro de contexto ('professional', 'personal', ou None para todos)
    """
    return get_dashboard_circulos(contexto=contexto)


@app.get("/api/circulos/{circulo}/contacts")
async def get_contacts_by_circulo(
    circulo: int,
    sort_by: str = "health",
    limit: int = 50,
    offset: int = 0,
    contexto: str = None
):
    """Lista contatos de um circulo especifico"""
    if circulo < 1 or circulo > 5:
        raise HTTPException(status_code=400, detail="Circulo deve ser entre 1 e 5")

    result = get_contatos_por_circulo(circulo, sort_by=sort_by, limit=limit, offset=offset, contexto=contexto)
    return {
        "circulo": circulo,
        "contexto": contexto,
        "config": result.get("config", CIRCULO_CONFIG.get(circulo)),
        "total": result.get("total", 0),
        "contacts": result.get("contacts", [])
    }


@app.get("/api/circulos/health")
async def get_circulos_health():
    """Dashboard de saude - contatos precisando atencao"""
    return {
        "precisam_atencao": get_contatos_precisando_atencao(20),
        "aniversarios": get_aniversarios_proximos(30)
    }


@app.get("/api/contacts/{contact_id}/circulo")
async def get_contact_circulo(contact_id: int):
    """Detalhes do circulo de um contato especifico"""
    with get_pg_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, tags, total_interacoes, ultimo_contato,
                   aniversario, linkedin, empresa, contexto,
                   circulo, circulo_manual, frequencia_ideal_dias, health_score
            FROM contacts WHERE id = %s
        """, (contact_id,))

        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")

        contact = dict(contact)

        # Calcular score atual (para mostrar breakdown)
        circulo_calc, score, reasons = calcular_score_circulo(contact)
        health = calcular_health_score(contact, contact.get("circulo") or circulo_calc)

        return {
            "contact_id": contact_id,
            "nome": contact["nome"],
            "circulo_atual": contact.get("circulo") or 5,
            "circulo_calculado": circulo_calc,
            "circulo_manual": contact.get("circulo_manual", False),
            "score": score,
            "health_score": health,
            "frequencia_ideal_dias": contact.get("frequencia_ideal_dias") or CIRCULO_CONFIG[contact.get("circulo") or 5]["frequencia_dias"],
            "ultimo_contato": contact.get("ultimo_contato"),
            "reasons": reasons,
            "config": CIRCULO_CONFIG.get(contact.get("circulo") or 5)
        }


@app.post("/api/contacts/{contact_id}/circulo")
async def update_contact_circulo_legacy(contact_id: int, data: dict):
    """Atualiza circulo de um contato manualmente (legacy endpoint)"""
    circulo = data.get("circulo")
    frequencia = data.get("frequencia_ideal_dias")

    if circulo and (circulo < 1 or circulo > 5):
        raise HTTPException(status_code=400, detail="Circulo deve ser entre 1 e 5")

    result = definir_circulo_manual(
        contact_id=contact_id,
        circulo=circulo,
        frequencia_dias=frequencia
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@app.post("/api/circulos/recalculate")
async def recalculate_circulos(force: bool = False, limit: int = None):
    """Recalcula circulos de todos os contatos"""
    result = recalcular_todos_circulos(force=force, limit=limit)
    return result


@app.post("/api/contacts/{contact_id}/circulo/recalculate")
async def recalculate_contact_circulo(contact_id: int, force: bool = False):
    """Recalcula circulos (pessoal e profissional) de um contato"""
    result = recalcular_circulos_dual(contact_id, force=force)
    return result


@app.put("/api/contatos/{contact_id}/circulo")
async def update_contact_circulo_dual(contact_id: int, data: dict):
    """Atualiza circulo e/ou contexto de um contato manualmente (dual circles)."""
    from database import get_db

    contexto = data.get("contexto")  # 'pessoal' ou 'profissional'
    circulo = data.get("circulo")    # 1-5

    if not contexto or not circulo:
        raise HTTPException(status_code=400, detail="contexto e circulo são obrigatórios")

    if circulo not in [1, 2, 3, 4, 5]:
        raise HTTPException(status_code=400, detail="circulo deve ser 1-5")

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Map contexto to database field
            # Also update the effective 'circulo' field and set manual flag
            if contexto == "pessoal":
                cursor.execute("""
                    UPDATE contacts
                    SET circulo_pessoal = %s,
                        circulo_pessoal_manual = TRUE,
                        contexto = 'personal',
                        circulo = LEAST(%s, COALESCE(circulo_profissional, 5)),
                        circulo_manual = TRUE,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id, nome, circulo_pessoal, circulo_profissional, circulo, contexto
                """, (circulo, circulo, contact_id))
            else:  # profissional
                cursor.execute("""
                    UPDATE contacts
                    SET circulo_profissional = %s,
                        circulo_profissional_manual = TRUE,
                        contexto = 'professional',
                        circulo = LEAST(COALESCE(circulo_pessoal, 5), %s),
                        circulo_manual = TRUE,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id, nome, circulo_pessoal, circulo_profissional, circulo, contexto
                """, (circulo, circulo, contact_id))

            result = cursor.fetchone()
            conn.commit()

            if not result:
                raise HTTPException(status_code=404, detail="Contato não encontrado")

            return {
                "success": True,
                "contact": {
                    "id": result["id"],
                    "nome": result["nome"],
                    "circulo_pessoal": result["circulo_pessoal"],
                    "circulo_profissional": result["circulo_profissional"],
                    "circulo": result["circulo"],
                    "contexto": result["contexto"]
                }
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/contatos/{contact_id}/circulo-clear")
async def clear_contact_circulo(contact_id: int, data: dict):
    """Remove o circulo de um contexto específico (define como NULL)."""
    from database import get_db

    contexto = data.get("contexto")  # 'pessoal' ou 'profissional'

    if contexto not in ["pessoal", "profissional"]:
        raise HTTPException(status_code=400, detail="contexto deve ser 'pessoal' ou 'profissional'")

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            if contexto == "pessoal":
                cursor.execute("""
                    UPDATE contacts
                    SET circulo_pessoal = NULL,
                        circulo_pessoal_manual = FALSE,
                        circulo = COALESCE(circulo_profissional, 5),
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id, nome, circulo_pessoal, circulo_profissional, circulo
                """, (contact_id,))
            else:
                cursor.execute("""
                    UPDATE contacts
                    SET circulo_profissional = NULL,
                        circulo_profissional_manual = FALSE,
                        circulo = COALESCE(circulo_pessoal, 5),
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id, nome, circulo_pessoal, circulo_profissional, circulo
                """, (contact_id,))

            result = cursor.fetchone()
            conn.commit()

            if not result:
                raise HTTPException(status_code=404, detail="Contato não encontrado")

            return {
                "success": True,
                "contact": dict(result)
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/circulos/sync-effective")
async def sync_effective_circles():
    """
    Sincroniza o campo 'circulo' (efetivo) baseado em circulo_pessoal e circulo_profissional.
    Corrige contatos que foram reorganizados mas não tiveram o círculo efetivo atualizado.
    """
    from database import get_db

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Update effective circle = MIN(pessoal, profissional)
            # Only update where at least one dual circle is set
            cursor.execute("""
                UPDATE contacts
                SET circulo = LEAST(
                    COALESCE(circulo_pessoal, 5),
                    COALESCE(circulo_profissional, 5)
                ),
                atualizado_em = CURRENT_TIMESTAMP
                WHERE circulo_pessoal IS NOT NULL OR circulo_profissional IS NOT NULL
                RETURNING id
            """)

            updated = cursor.rowcount
            conn.commit()

            return {
                "success": True,
                "updated": updated,
                "message": f"Sincronizado {updated} contatos"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== CIRCULOS PAGE ROUTE ==============

@app.get("/rap/circulos")
async def rap_circulos_redirect():
    return RedirectResponse(url="/circulos", status_code=301)


# ============== BRIEFINGS ENDPOINTS ==============
# Sistema de geracao de briefings inteligentes para contatos
# Implementado por: FLOW (2026-03-25)

@app.get("/api/briefings/pending")
async def get_pending_briefings(limit: int = 10):
    """Lista contatos que precisam de briefing"""
    return get_contacts_needing_briefing(limit=limit)


@app.post("/api/contacts/{contact_id}/briefing")
async def create_contact_briefing(contact_id: int, data: dict = None):
    """Gera briefing inteligente para um contato usando AI"""
    contexto = data.get("contexto") if data else None
    result = await generate_briefing(
        contact_id=contact_id,
        contexto_reuniao=contexto,
        incluir_sugestoes=True
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/contacts/{contact_id}/briefing/current")
async def get_contact_current_briefing(contact_id: int):
    """
    Retorna o briefing atual (mais recente) de um contato.

    Returns:
        O briefing atual ou null se nao existir
    """
    briefing = get_current_briefing(contact_id)
    if not briefing:
        return {"briefing": None, "exists": False}

    return {
        "exists": True,
        "briefing": briefing
    }


@app.get("/api/contacts/{contact_id}/briefings")
async def get_contact_briefings_history(contact_id: int, limit: int = 5):
    """
    Retorna historico de briefings de um contato.
    """
    from services.briefings import get_briefing_history
    history = get_briefing_history(contact_id, limit=limit)
    return {
        "contact_id": contact_id,
        "total": len(history),
        "briefings": history
    }


@app.post("/api/briefings/{briefing_id}/feedback")
async def add_feedback_to_briefing(briefing_id: int, data: dict):
    """
    Adiciona feedback a um briefing (util para melhorar AI).

    Body: {"feedback": "O briefing foi util, a sugestao de cafe funcionou"}
    """
    from services.briefings import add_briefing_feedback
    feedback = data.get("feedback", "")
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback e obrigatorio")

    success = add_briefing_feedback(briefing_id, feedback)
    return {"status": "success" if success else "error", "briefing_id": briefing_id}


# ============== BRIEFINGS PAGE ROUTE ==============

@app.get("/rap/briefings")
async def rap_briefings_redirect():
    return RedirectResponse(url="/briefings", status_code=301)


# ============== DUPLICADOS ENDPOINTS ==============
# Sistema de deteccao e merge de contatos duplicados
# Implementado por: INTEL (2026-03-25)

@app.get("/api/contacts/duplicates")
async def get_duplicates(
    threshold: float = 0.5,
    limit: int = 50,
    offset: int = 0
):
    """
    Encontra possiveis contatos duplicados.

    Args:
        threshold: Score minimo (0.0-1.0) para considerar duplicado
        limit: Numero maximo de pares a retornar
        offset: Offset para paginacao

    Score considera:
    - Email igual: +0.5
    - Telefone igual: +0.4
    - Nome similar: +0.1 a +0.3
    """
    return encontrar_duplicados(
        threshold=threshold,
        limit=limit,
        offset=offset
    )


@app.post("/api/contacts/merge")
async def merge_duplicate_contacts(data: dict):
    """
    Merge dois contatos duplicados.

    Body: {
        "keep_id": 123,
        "merge_id": 456,
        "field_choices": {                    # Opcional
            "nome": 123,                      # Usar valor do contato 123
            "empresa": 456,                   # Usar valor do contato 456
            "emails": "combine",              # Combinar todos
            "telefones": "combine"
        }
    }

    O contato merge_id sera excluido apos transferir:
    - Dados conforme field_choices (ou automatico se nao especificado)
    - Mensagens
    - Conversas
    - Tasks
    """
    keep_id = data.get("keep_id")
    merge_id = data.get("merge_id")
    field_choices = data.get("field_choices")

    if not keep_id or not merge_id:
        raise HTTPException(status_code=400, detail="keep_id e merge_id sao obrigatorios")

    result = merge_contatos(keep_id, merge_id, field_choices)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/contacts/duplicates/stats")
async def get_duplicates_stats():
    """Retorna estatisticas sobre duplicados no sistema."""
    return get_duplicate_statistics()


# ============== BRIEFING CONTEXT ENDPOINTS ==============
# Sistema de contexto enriquecido para briefings
# Implementado por: INTEL (2026-03-25)

@app.get("/api/contacts/{contact_id}/briefing-context")
async def get_contact_briefing_context(contact_id: int):
    """
    Retorna contexto enriquecido completo para briefing.

    Inclui:
    - Tom das conversas (positivo/negativo/neutro)
    - Topicos recorrentes
    - Assuntos sugeridos para retomar
    - Promessas/compromissos pendentes
    - Alertas importantes
    """
    return get_contexto_enriquecido(contact_id)


@app.get("/api/contacts/{contact_id}/conversation-tone")
async def get_conversation_tone(contact_id: int, dias: int = 30):
    """Analisa o tom das ultimas conversas."""
    return analisar_tom_conversas(contact_id, dias=dias)


@app.get("/api/contacts/{contact_id}/topics")
async def get_contact_topics(contact_id: int, dias: int = 90):
    """Identifica topicos recorrentes nas conversas."""
    return identificar_topicos_recorrentes(contact_id, dias=dias)


@app.get("/api/contacts/{contact_id}/suggested-topics")
async def get_suggested_topics(contact_id: int):
    """Sugere assuntos para retomar com o contato."""
    return sugerir_assuntos_retomar(contact_id)


@app.get("/api/contacts/{contact_id}/pending-promises")
async def get_pending_promises(contact_id: int, dias: int = 60):
    """Detecta promessas/compromissos pendentes."""
    return detectar_promessas_pendentes(contact_id, dias=dias)


# ============== AUTO TAGS ENDPOINTS ==============
# Sistema de sugestao automatica de tags para contatos
# Implementado por: INTEL (2026-03-25)

@app.get("/api/contacts/{contact_id}/suggested-tags")
async def get_suggested_tags(contact_id: int):
    """
    Analisa um contato e sugere tags automaticas.

    Analisa:
    - Empresa (setor: financeiro, tecnologia, etc)
    - Cargo (nivel: c-level, diretor, gerente)
    - Email domain (governo, educacao)
    - Historico de mensagens (keywords)
    """
    result = analisar_contato_para_tags(contact_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/contacts/{contact_id}/apply-tags")
async def apply_tags_to_contact(contact_id: int, data: dict = None):
    """
    Aplica tags sugeridas a um contato.

    Body (opcional): {"tags": ["tag1", "tag2"]}
    Se nao informado, aplica todas as tags sugeridas.
    """
    if data and data.get("tags"):
        tags = data["tags"]
    else:
        # Analisar e pegar tags novas
        analise = analisar_contato_para_tags(contact_id)
        if "error" in analise:
            raise HTTPException(status_code=404, detail=analise["error"])
        tags = analise.get("tags_novas", [])

    result = aplicar_tags_contato(contact_id, tags)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/contacts/apply-auto-tags")
async def apply_auto_tags_batch(
    batch_size: int = 100,
    offset: int = 0,
    auto_apply: bool = False
):
    """
    Analisa e opcionalmente aplica tags em lote.

    Args:
        batch_size: Contatos por lote (default 100)
        offset: Offset para paginacao
        auto_apply: Se True, aplica tags automaticamente

    Retorna progresso para chamadas subsequentes.
    """
    return aplicar_tags_em_lote(
        batch_size=batch_size,
        offset=offset,
        auto_apply=auto_apply
    )


@app.get("/api/tags/statistics")
async def get_tags_stats():
    """Retorna estatisticas de uso das tags no sistema."""
    return get_tag_statistics()


# ============== Gmail Sync Service Endpoints ==============

from services.gmail_sync import get_gmail_sync_service


@app.post("/api/gmail/sync-all")
async def gmail_sync_all_contacts(
    request: Request,
    months_back: int = 12,
    background: bool = True
):
    """
    Inicia sincronizacao de emails de todos os contatos.

    Args:
        months_back: Meses para buscar (default 12)
        background: Se True, executa em background

    Returns:
        Status do sync ou confirmacao de inicio
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode sincronizar")

    service = get_gmail_sync_service()

    if background:
        import asyncio
        asyncio.create_task(service.sync_all_contacts(months_back=months_back))
        return {"status": "started", "message": "Sync iniciado em background"}
    else:
        result = await service.sync_all_contacts(months_back=months_back)
        return result


@app.get("/api/gmail/sync-status")
async def gmail_sync_status():
    """
    Retorna status atual da sincronizacao Gmail.
    """
    service = get_gmail_sync_service()
    return service.get_sync_status()


@app.post("/api/gmail/sync-contact/{contact_id}")
async def gmail_sync_single_contact(
    request: Request,
    contact_id: int,
    months_back: int = 12
):
    """
    Sincroniza emails de um contato especifico.
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode sincronizar")

    # Get contact email
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT emails FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato não encontrado")

        # Get first Gmail account
        cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE LIMIT 1")
        account = cursor.fetchone()
        if not account:
            raise HTTPException(status_code=400, detail="Nenhuma conta Gmail conectada")

    # Parse email
    emails_data = contact["emails"]
    if isinstance(emails_data, str):
        import json
        emails_data = json.loads(emails_data)

    if not emails_data:
        raise HTTPException(status_code=400, detail="Contato não tem email")

    email = emails_data[0].get("email", "") if isinstance(emails_data[0], dict) else str(emails_data[0])

    service = get_gmail_sync_service()
    access_token = await service.get_valid_token(dict(account))

    if not access_token:
        raise HTTPException(status_code=401, detail="Token Gmail inválido")

    result = await service.sync_contact_emails(
        contact_id=contact_id,
        email=email,
        access_token=access_token,
        months_back=months_back
    )

    return result


@app.post("/api/gmail/recalculate-circles")
async def gmail_recalculate_after_sync(request: Request):
    """
    Recalcula circulos de todos os contatos apos sync Gmail.
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode recalcular")

    service = get_gmail_sync_service()
    result = await service.recalculate_circles_after_sync()
    return result


# ============== WhatsApp Sync Service Endpoints ==============

from services.whatsapp_sync import get_whatsapp_sync_service


@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    """
    Webhook do Evolution API para mensagens WhatsApp em tempo real.
    Processa mensagens recebidas e atualiza interacoes dos contatos.
    """
    try:
        payload = await request.json()
        service = get_whatsapp_sync_service()
        result = await service.process_webhook(payload)
        return {"status": "ok", **result}
    except Exception as e:
        logger.error(f"Erro no webhook WhatsApp: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/whatsapp/sync-all")
async def whatsapp_sync_all_chats(
    request: Request,
    include_groups: bool = False,
    background: bool = True
):
    """
    Sincroniza todos os chats do WhatsApp com contatos.

    Args:
        include_groups: Se True, inclui grupos
        background: Se True, executa em background

    Returns:
        Status do sync
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode sincronizar")

    service = get_whatsapp_sync_service()

    if background:
        import asyncio
        asyncio.create_task(service.sync_all_chats(include_groups=include_groups))
        return {"status": "started", "message": "Sync iniciado em background"}
    else:
        result = await service.sync_all_chats(include_groups=include_groups)
        return result


@app.post("/api/sync/global")
async def global_sync(request: Request, background: bool = True):
    """
    Sincronizacao global manual - WhatsApp + Gmail.
    Botao de refresh no sidebar.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    import asyncio

    results = {"status": "started", "steps": {}}

    async def do_sync():
        # WhatsApp sync
        try:
            wa_service = get_whatsapp_sync_service()
            wa_result = await wa_service.sync_all_chats(include_groups=False)
            results["steps"]["whatsapp"] = {
                "status": "success",
                "linked": wa_result.get("linked", 0),
                "messages": wa_result.get("messages_saved", 0)
            }
        except Exception as e:
            results["steps"]["whatsapp"] = {"status": "error", "error": str(e)}

        # Gmail sync
        try:
            from services.gmail_sync import get_gmail_sync_service
            gmail_service = get_gmail_sync_service()
            gmail_result = await gmail_service.sync_all_contacts(months_back=1)
            results["steps"]["gmail"] = {
                "status": "success",
                "synced": gmail_result.get("total_synced", 0)
            }
        except Exception as e:
            results["steps"]["gmail"] = {"status": "error", "error": str(e)}

        results["status"] = "completed"

    if background:
        asyncio.create_task(do_sync())
        return {"status": "started", "message": "Sincronizacao iniciada em background"}
    else:
        await do_sync()
        return results


@app.get("/api/whatsapp/sync-status")
async def whatsapp_sync_status():
    """
    Retorna status atual da sincronizacao WhatsApp.
    """
    service = get_whatsapp_sync_service()
    return service.get_sync_status()


@app.post("/api/whatsapp/sync-chat/{phone}")
async def whatsapp_sync_single_chat(request: Request, phone: str):
    """
    Sincroniza chat de um numero especifico.
    """
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode sincronizar")

    service = get_whatsapp_sync_service()
    result = await service.sync_single_chat(phone)
    return result


@app.post("/api/contacts/{contact_id}/sync-whatsapp")
async def sync_contact_whatsapp(request: Request, contact_id: int):
    """
    Sincroniza mensagens WhatsApp para um contato específico.
    Busca o telefone do contato e faz sync via Evolution API.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    # Buscar telefone do contato
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome, telefones FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()

        if not contact:
            raise HTTPException(status_code=404, detail="Contato não encontrado")

        telefones = contact["telefones"]
        if not telefones:
            return {"success": False, "error": "Contato não tem telefone cadastrado"}

        # Pegar primeiro telefone WhatsApp
        phone = None
        for tel in telefones:
            if isinstance(tel, dict):
                if tel.get("whatsapp"):
                    phone = tel.get("number", "") or tel.get("phone", "")
                    break
            else:
                phone = str(tel)
                break

        if not phone:
            return {"success": False, "error": "Contato não tem telefone WhatsApp"}

        # Normalizar telefone (remover formatação, adicionar 55 se necessário)
        import re
        phone_digits = re.sub(r'\D', '', phone)
        if len(phone_digits) == 11:  # Ex: 11999232162
            phone_digits = "55" + phone_digits
        elif len(phone_digits) == 10:  # Ex: 1199923216
            phone_digits = "55" + phone_digits

    # Fazer sync
    service = get_whatsapp_sync_service()
    result = await service.sync_single_chat(phone_digits)
    result["contact_name"] = contact["nome"]
    return result


# ============== Meeting Suggestion Endpoints ==============

from services.meeting_suggestion import generate_event_suggestion, find_company_address


@app.get("/api/contacts/{contact_id}/meeting-suggestion")
async def api_meeting_suggestion(request: Request, contact_id: int, limit: int = 10):
    """
    Analisa mensagens recentes do contato e sugere criacao de evento.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    suggestion = await generate_event_suggestion(contact_id, limit=limit)

    if not suggestion:
        return {"detected": False, "message": "Nenhuma reuniao detectada nas mensagens recentes"}

    return {"detected": True, "suggestion": suggestion}


@app.post("/api/contacts/{contact_id}/create-meeting")
async def api_create_meeting_from_suggestion(request: Request, contact_id: int):
    """
    Cria evento no Google Calendar a partir de sugestao.
    Espera JSON com: title, date, time, duration_minutes, location, description, attendees
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()

    # Obter token do Gmail (mesmo OAuth)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE LIMIT 1")
        account = cursor.fetchone()

    if not account:
        raise HTTPException(status_code=401, detail="Gmail nao conectado - necessario para criar eventos")

    # Refresh token
    tokens = await gmail.refresh_access_token(account["refresh_token"])
    if "error" in tokens:
        raise HTTPException(status_code=401, detail=f"Erro ao renovar token: {tokens.get('error')}")

    access_token = tokens.get("access_token")

    # Preparar dados do evento
    from zoneinfo import ZoneInfo

    sp_tz = ZoneInfo("America/Sao_Paulo")

    date_str = data.get("date")  # YYYY-MM-DD
    time_str = data.get("time", "10:00")  # HH:MM
    duration = int(data.get("duration_minutes", 60))

    try:
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        start_dt = start_dt.replace(tzinfo=sp_tz)
        end_dt = start_dt + timedelta(minutes=duration)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Data/hora invalida: {e}")

    # Criar evento
    event_result = await calendar.create_event(
        access_token=access_token,
        summary=data.get("title", "Reuniao"),
        start_datetime=start_dt,
        end_datetime=end_dt,
        description=data.get("description"),
        location=data.get("location"),
        attendees=data.get("attendees", []),
        create_meet=data.get("create_meet", False)
    )

    if "error" in event_result:
        raise HTTPException(status_code=500, detail=event_result["error"])

    return {
        "success": True,
        "event": event_result,
        "calendar_link": event_result.get("htmlLink")
    }


@app.get("/api/companies/{company_name}/address")
async def api_company_address(company_name: str, contact_id: int = None, website: str = None):
    """
    Busca endereco de uma empresa.
    """
    address_info = await find_company_address(
        company_name=company_name,
        contact_id=contact_id,
        company_website=website
    )

    if not address_info:
        return {"found": False}

    return {"found": True, "address_info": address_info}


# ============== WhatsApp Import Endpoints ==============

from services.whatsapp_import import get_whatsapp_import_service


@app.post("/api/whatsapp/import/parse")
async def parse_whatsapp_file(request: Request, file: UploadFile = File(...)):
    """
    Parse WhatsApp export file and return preview.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    try:
        content = await file.read()
        content_str = content.decode('utf-8', errors='ignore')

        service = get_whatsapp_import_service()
        result = service.parse_file(content_str, file.filename)

        return result

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/whatsapp/import/confirm")
async def confirm_whatsapp_import(request: Request):
    """
    Confirm import of parsed messages to a contact.

    Body:
    {
        "messages": [...],
        "contact_id": 123,
        "my_name": "Renato"
    }
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    try:
        data = await request.json()
        messages = data.get("messages", [])
        contact_id = data.get("contact_id")
        my_name = data.get("my_name", "Renato")

        if not contact_id:
            raise HTTPException(status_code=400, detail="contact_id obrigatorio")

        # Convert timestamp strings back to datetime
        for msg in messages:
            if isinstance(msg.get("timestamp"), str):
                msg["timestamp"] = datetime.fromisoformat(msg["timestamp"])

        service = get_whatsapp_import_service()
        result = service.import_to_contact(messages, contact_id, my_name)

        return result

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/whatsapp/import/status")
async def get_import_status(request: Request):
    """Get current import status."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_whatsapp_import_service()
    return service.get_import_status()


# ============== WhatsApp Batch Import ==============

from services.whatsapp_batch_import import get_batch_importer

@app.post("/api/whatsapp/batch-import")
async def batch_import_whatsapp(request: Request, files: List[UploadFile] = File(...)):
    """
    Importa múltiplos arquivos .txt do WhatsApp em lote.
    Detecta contatos automaticamente e importa para o Inbox.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    importer = get_batch_importer()
    file_data = []

    for file in files:
        try:
            content = await file.read()
            content_str = content.decode('utf-8', errors='ignore')
            file_data.append((file.filename, content_str, None))
        except Exception as e:
            file_data.append((file.filename, "", None))

    result = importer.process_batch(file_data)
    return result


@app.post("/api/whatsapp/batch-import/preview")
async def batch_import_preview(request: Request, files: List[UploadFile] = File(...)):
    """
    Analisa arquivos e detecta contatos automaticamente a partir do conteúdo.
    Retorna nível de confiança para cada arquivo.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    importer = get_batch_importer()
    previews = []

    for file in files:
        try:
            content = await file.read()
            content_str = content.decode('utf-8', errors='ignore')
            parsed = importer.parse_file_content(content_str)

            # Auto-detectar contato a partir do conteúdo do chat
            detection = importer.auto_detect_contact(parsed)

            previews.append({
                'filename': file.filename,
                'messages_count': parsed['total_messages'],
                'participants': parsed['participants'],
                'contact_name': parsed.get('contact_name'),
                'contact_phone': parsed.get('contact_phone'),
                'all_phones': parsed.get('all_phones', []),
                # Resultado da detecção automática
                'contact': detection['contact'],
                'contact_found': detection['contact'] is not None,
                'confidence': detection['confidence'],
                'match_reason': detection['match_reason'],
                'alternatives': detection['alternatives'],
                # Para importação automática
                'auto_import': detection['confidence'] in ['high', 'medium'],
                'needs_review': detection['confidence'] in ['low', 'none'],
                'date_range': {
                    'start': parsed['date_range']['start'].isoformat() if parsed['date_range']['start'] else None,
                    'end': parsed['date_range']['end'].isoformat() if parsed['date_range']['end'] else None
                }
            })
        except Exception as e:
            previews.append({
                'filename': file.filename,
                'error': str(e),
                'needs_review': True
            })

    return {'files': previews}


@app.post("/api/whatsapp/batch-import/confirm")
async def batch_import_confirm(request: Request):
    """
    Confirma importação com mapeamento de contatos.

    Body:
    {
        "files": [
            {"filename": "chat.txt", "content": "...", "contact_id": 123},
            ...
        ]
    }
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()
    files = data.get('files', [])

    importer = get_batch_importer()
    file_data = [(f['filename'], f['content'], f.get('contact_id')) for f in files]

    result = importer.process_batch(file_data)
    return result


@app.get("/api/whatsapp/messages/{contact_id}")
async def get_whatsapp_messages(request: Request, contact_id: int, limit: int = 100):
    """Get WhatsApp messages for a contact."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, direction, content, message_type, message_date, imported_from
            FROM whatsapp_messages
            WHERE contact_id = %s
            ORDER BY message_date DESC
            LIMIT %s
        """, (contact_id, limit))

        messages = cursor.fetchall()
        return {"messages": [dict(m) for m in messages]}


# ============== Evolution API Integration ==============

from integrations.evolution_api import get_evolution_client, handle_evolution_webhook

@app.post("/api/webhooks/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Webhook para receber eventos da Evolution API.
    Configura este URL na Evolution API: POST /webhook/set/{instance}
    """
    try:
        payload = await request.json()
        result = await handle_evolution_webhook(payload)
        return result
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"error": str(e)}


@app.get("/api/evolution/status")
async def evolution_status(request: Request):
    """Status da conexão com Evolution API"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    client = get_evolution_client()

    if not client.is_configured:
        return {
            "configured": False,
            "message": "Evolution API não configurada. Configure EVOLUTION_API_URL e EVOLUTION_API_KEY"
        }

    status = await client.check_connection()
    return {
        "configured": True,
        "instance": client.instance_name,
        **status
    }


@app.get("/api/evolution/qrcode")
async def evolution_qrcode(request: Request):
    """Obtém QR Code para conexão"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    client = get_evolution_client()

    if not client.is_configured:
        raise HTTPException(status_code=400, detail="Evolution API não configurada")

    result = await client.get_qr_code()
    return result


@app.post("/api/evolution/create-instance")
async def evolution_create_instance(request: Request):
    """Cria instância do WhatsApp na Evolution API"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    client = get_evolution_client()

    if not client.is_configured:
        raise HTTPException(status_code=400, detail="Evolution API não configurada")

    result = await client.create_instance()
    return result


@app.post("/api/evolution/disconnect")
async def evolution_disconnect(request: Request):
    """Desconecta a instância do WhatsApp"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    client = get_evolution_client()

    if not client.is_configured:
        raise HTTPException(status_code=400, detail="Evolution API não configurada")

    result = await client.logout_instance()
    return result


@app.post("/api/evolution/setup-webhook")
async def evolution_setup_webhook(request: Request):
    """Configura webhook na Evolution API"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    client = get_evolution_client()

    if not client.is_configured:
        raise HTTPException(status_code=400, detail="Evolution API não configurada")

    # URL do webhook deste sistema
    base_url = os.getenv("BASE_URL", "https://intel.almeida-prado.com")
    webhook_url = f"{base_url}/api/webhooks/whatsapp"

    result = await client.set_webhook(webhook_url)
    return {"webhook_url": webhook_url, "result": result}


@app.get("/api/evolution/webhook")
async def evolution_get_webhook(request: Request):
    """Verifica configuração atual do webhook"""
    try:
        user = get_current_user(request)
        if not user:
            return {"configured": False, "error": "Nao autenticado"}

        client = get_evolution_client()

        if not client.is_configured:
            return {"configured": False, "error": "Evolution API não configurada"}

        result = await client.get_webhook()

        # Expected URL
        base_url = os.getenv("BASE_URL", "https://intel.almeida-prado.com")
        expected_url = f"{base_url}/api/webhooks/whatsapp"

        # Handle multiple possible response formats
        current_url = None
        enabled = False
        events = []

        if result and isinstance(result, dict):
            if "error" in result:
                return {"configured": False, "error": result.get("error"), "expected_url": expected_url}

            current_url = result.get("url")
            enabled = result.get("enabled", False)
            events = result.get("events", [])

            # Nested webhook object
            if not current_url and isinstance(result.get("webhook"), dict):
                webhook = result.get("webhook", {})
                current_url = webhook.get("url")
                enabled = webhook.get("enabled", False)
                events = webhook.get("events", [])

        is_correct = current_url == expected_url if current_url else False

        return {
            "configured": bool(current_url),
            "url": current_url,
            "expected_url": expected_url,
            "is_correct": is_correct,
            "enabled": enabled,
            "events": events
        }
    except Exception as e:
        logger.error(f"Webhook check error: {e}")
        return {"configured": False, "error": str(e)}


@app.post("/api/evolution/send")
async def evolution_send_message(request: Request):
    """Envia mensagem via Evolution API"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    client = get_evolution_client()

    if not client.is_configured:
        raise HTTPException(status_code=400, detail="Evolution API não configurada")

    data = await request.json()
    phone = data.get("phone")
    message = data.get("message")

    if not phone or not message:
        raise HTTPException(status_code=400, detail="phone e message são obrigatórios")

    result = await client.send_text(phone, message)
    return result


# ============== ConselhoOS Sync Endpoints ==============

from services.conselhoos_sync import get_conselhoos_sync_service


@app.get("/api/conselhoos/empresas")
async def get_conselhoos_empresas(request: Request):
    """Get empresas from ConselhoOS."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    empresas = service.get_empresas()
    return {"empresas": empresas}


@app.get("/api/conselhoos/reunioes")
async def get_conselhoos_reunioes(request: Request, limit: int = 10):
    """Get upcoming meetings from ConselhoOS."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    reunioes = service.get_proximas_reunioes(limit=limit)
    return {"reunioes": reunioes}


@app.get("/api/conselhoos/raci")
async def get_conselhoos_raci(request: Request, limit: int = 20):
    """Get pending RACI items from ConselhoOS."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    raci = service.get_raci_pendentes(limit=limit)
    return {"raci": raci}


@app.get("/api/conselhoos/dashboard")
async def get_conselhoos_dashboard(request: Request):
    """Get ConselhoOS summary for INTEL dashboard."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    summary = service.get_dashboard_summary()
    return summary


@app.post("/api/contacts/{contact_id}/conselhoos/link")
async def link_contact_to_conselhoos(request: Request, contact_id: int):
    """Link a contact to a ConselhoOS empresa."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()
    empresa_id = data.get("empresa_id")
    empresa_nome = data.get("empresa_nome")
    role = data.get("role", "stakeholder")

    if not empresa_id or not empresa_nome:
        raise HTTPException(status_code=400, detail="empresa_id e empresa_nome obrigatorios")

    service = get_conselhoos_sync_service()
    result = service.link_contact_to_empresa(contact_id, empresa_id, empresa_nome, role)
    return result


@app.get("/api/contacts/{contact_id}/conselhoos")
async def get_contact_conselhoos(request: Request, contact_id: int):
    """Get ConselhoOS empresas linked to a contact."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    empresas = service.get_contact_empresas(contact_id)
    return {"empresas": empresas}


@app.get("/api/contacts/{contact_id}/conselhoos/reunioes")
async def get_contact_conselhoos_reunioes(request: Request, contact_id: int, limit: int = 20):
    """Get ConselhoOS reuniões for empresas linked to a contact."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_conselhoos_sync_service()
    reunioes = service.get_reunioes_by_contact(contact_id, limit=limit)
    return {"reunioes": reunioes}


# ---------- ConselhoOS RACI <-> Tasks Sync ----------

from services.conselhoos_raci_sync import get_raci_sync_service


@app.post("/api/conselhoos/raci/sync")
async def manual_sync_raci(request: Request):
    """
    Manual trigger: bidirectional sync between ConselhoOS RACI and INTEL tasks.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = {}
    try:
        data = await request.json()
    except Exception:
        pass

    empresa_nome = data.get("empresa_nome")

    service = get_raci_sync_service()

    if empresa_nome:
        raci_results = service.sync_raci_to_tasks(empresa_nome=empresa_nome)
    else:
        raci_results = service.sync_raci_to_tasks()

    task_results = service.sync_task_status_to_raci()

    return {
        "raci_to_tasks": raci_results,
        "tasks_to_raci": task_results,
        "synced_at": datetime.now().isoformat()
    }


@app.get("/api/cron/sync-conselhoos-raci")
async def cron_sync_conselhoos_raci(request: Request):
    """
    Cron: Daily bidirectional sync ConselhoOS RACI <-> INTEL Tasks.

    1. Creates INTEL tasks for new ConselhoOS RACI items
    2. Marks ConselhoOS RACIs as done when INTEL task is completed
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    service = get_raci_sync_service()
    results = service.full_sync()
    return results


# ============== Google Calendar Endpoints ==============

from integrations.google_calendar import get_calendar_integration


@app.get("/api/calendar/today")
async def calendar_today(request: Request, debug: bool = False):
    """
    Retorna eventos de hoje.
    Formato compativel com 3FLOW: start_datetime, end_datetime, contact_name
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Buscar token da primeira conta Google conectada
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE LIMIT 1")
        account = cursor.fetchone()

    if not account:
        if debug:
            return {"error": "no_account", "events": []}
        return []

    # Refresh token
    from integrations.gmail import GmailIntegration
    gmail = GmailIntegration()
    tokens = await gmail.refresh_access_token(account["refresh_token"])

    if "error" in tokens:
        if debug:
            return {"error": "token_refresh_failed", "details": tokens, "events": []}
        return []

    access_token = tokens.get("access_token")
    calendar = get_calendar_integration()

    if debug:
        # Debug mode - return raw API response
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        sp_tz = ZoneInfo("America/Sao_Paulo")
        now_sp = datetime.now(sp_tz)
        start_of_day_sp = now_sp.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day_sp = start_of_day_sp + timedelta(days=1)
        start_utc = start_of_day_sp.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        end_utc = end_of_day_sp.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

        raw_result = await calendar.list_events(
            access_token=access_token,
            time_min=start_utc,
            time_max=end_utc,
            max_results=20
        )

        return {
            "debug": True,
            "now_sp": now_sp.isoformat(),
            "start_of_day_sp": start_of_day_sp.isoformat(),
            "end_of_day_sp": end_of_day_sp.isoformat(),
            "start_utc": start_utc.isoformat(),
            "end_utc": end_utc.isoformat(),
            "raw_api_response": raw_result,
            "formatted_events": await calendar.get_today_events(access_token)
        }

    events = await calendar.get_today_events(access_token)

    # Transform to 3FLOW format: start_datetime, end_datetime, contact_name
    formatted_events = []
    for event in events:
        formatted = {
            "id": event.get("id"),
            "summary": event.get("summary", "Sem titulo"),
            "description": event.get("description"),
            "location": event.get("location"),
            "start_datetime": event.get("start"),  # Rename start -> start_datetime
            "end_datetime": event.get("end"),      # Rename end -> end_datetime
            "is_all_day": event.get("is_all_day", False),
            "html_link": event.get("html_link"),
            "conference": event.get("conference"),
            "contact_name": None  # Will be filled if we find matching contact
        }

        # Try to match attendees to contacts
        attendees = event.get("attendees", [])
        if attendees:
            attendee_emails = [a.get("email") for a in attendees if a.get("email")]
            if attendee_emails:
                with get_db() as conn:
                    cursor = conn.cursor()
                    # Search for contacts matching attendee emails
                    placeholders = ", ".join(["%s"] * len(attendee_emails))
                    cursor.execute(f"""
                        SELECT nome, emails FROM contacts
                        WHERE EXISTS (
                            SELECT 1 FROM jsonb_array_elements(emails) AS e
                            WHERE LOWER(e->>'email') = ANY(ARRAY[{placeholders}])
                        )
                        LIMIT 1
                    """, [e.lower() for e in attendee_emails])
                    contact = cursor.fetchone()
                    if contact:
                        formatted["contact_name"] = contact["nome"]

        formatted_events.append(formatted)

    return formatted_events


# NOTE: /api/calendar/events endpoint is defined later in the file (CalendarEventsService)
# It returns events from local DB with contact_name field

# ============== Google Tasks Endpoints (Bidirectional Sync) ==============

from integrations.google_tasks import get_tasks_integration
from services.tasks_sync import get_tasks_sync_service


@app.get("/api/tasks")
async def list_tasks(
    request: Request,
    show_completed: bool = False,
    limit: int = 50,
    status: str = None,  # 'pending', 'completed', or None for all
    contact_id: int = None,
    project_id: int = None,
    source: str = "local"  # 'local', 'google', or 'both'
):
    """
    Lista tarefas. Suporta fonte local, Google Tasks, ou ambas.

    Params:
        show_completed: Include completed tasks
        limit: Max number of tasks to return
        status: Filter by status ('pending' or 'completed')
        contact_id: Filter by contact
        project_id: Filter by project
        source: 'local' (DB), 'google' (API), or 'both'
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    tasks_service = get_tasks_sync_service()

    # Local tasks from database
    local_tasks = []
    if source in ["local", "both"]:
        # Get all tasks first, then filter
        # This handles NULL status and other edge cases
        local_status = None
        if status == "completed":
            local_status = "completed"

        local_tasks = tasks_service.get_tasks(
            status=local_status,
            contact_id=contact_id,
            project_id=project_id,
            limit=limit * 2  # Get more to account for filtering
        )

        # Filter based on requested status
        if status == "pending":
            # Show non-completed tasks (pending, NULL, or any other status)
            local_tasks = [t for t in local_tasks if t.get("status") != "completed"]
        elif not show_completed and status != "completed":
            local_tasks = [t for t in local_tasks if t.get("status") != "completed"]

        # Normalize for frontend
        for task in local_tasks:
            task["due_date"] = task.get("data_vencimento")
            task["description"] = task.get("descricao")
            task["title"] = task.get("titulo")
            task["source"] = "local"

        if source == "local":
            return {"tasks": local_tasks[:limit]}

    # Google Tasks from API
    google_tasks = []
    if source in ["google", "both"]:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE LIMIT 1")
            account = cursor.fetchone()

        if account:
            from integrations.gmail import GmailIntegration
            gmail = GmailIntegration()
            tokens = await gmail.refresh_access_token(account["refresh_token"])

            if "error" not in tokens:
                access_token = tokens.get("access_token")
                tasks_api = get_tasks_integration()

                include_completed = show_completed or (status == "completed")
                task_lists = await tasks_api.list_task_lists(access_token)

                if not task_lists:
                    task_lists = [{"id": "@default", "title": "Minhas tarefas"}]

                for tl in task_lists:
                    tasks = await tasks_api.list_tasks(
                        access_token,
                        tasklist_id=tl.get("id", "@default"),
                        show_completed=include_completed
                    )
                    for task in tasks:
                        task["tasklist_title"] = tl.get("title", "Tarefas")
                        task["due_date"] = task.get("due")
                        task["description"] = task.get("notes")
                        task["source"] = "google"
                        google_tasks.append(task)

        if source == "google":
            # Filter by status
            if status == "pending":
                google_tasks = [t for t in google_tasks if t.get("status") != "completed"]
            elif status == "completed":
                google_tasks = [t for t in google_tasks if t.get("status") == "completed"]

            return {"tasks": google_tasks[:limit]}

    # Merge local and Google tasks (deduplicate by google_task_id)
    all_tasks = local_tasks.copy()
    local_google_ids = {t.get("google_task_id") for t in local_tasks if t.get("google_task_id")}

    for gtask in google_tasks:
        if gtask.get("id") not in local_google_ids:
            all_tasks.append(gtask)

    # Filter by status
    if status == "pending":
        all_tasks = [t for t in all_tasks if t.get("status") != "completed"]
    elif status == "completed":
        all_tasks = [t for t in all_tasks if t.get("status") == "completed"]

    # Sort by due date
    def sort_key(task):
        due = task.get("due_date") or task.get("due") or task.get("data_vencimento")
        if due:
            return (0, str(due))
        return (1, "9999-12-31")

    all_tasks.sort(key=sort_key)

    return {"tasks": all_tasks[:limit]}


@app.post("/api/tasks")
async def create_task(request: Request):
    """
    Cria nova tarefa localmente e sincroniza com Google Tasks.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()
    title = data.get("title") or data.get("titulo")
    notes = data.get("notes") or data.get("descricao")
    due = data.get("due") or data.get("data_vencimento")
    contact_id = data.get("contact_id")
    project_id = data.get("project_id")
    prioridade = data.get("prioridade", 5)
    sync_to_google = data.get("sync_to_google", True)

    if not title:
        raise HTTPException(status_code=400, detail="Titulo obrigatorio")

    due_datetime = None
    if due:
        try:
            if isinstance(due, str):
                due_datetime = datetime.fromisoformat(due.replace("Z", "+00:00"))
            else:
                due_datetime = due
        except:
            pass

    tasks_service = get_tasks_sync_service()
    result = await tasks_service.create_task(
        titulo=title,
        descricao=notes,
        data_vencimento=due_datetime,
        prioridade=prioridade,
        contact_id=contact_id,
        project_id=project_id,
        sync_to_google=sync_to_google
    )

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return result


@app.put("/api/tasks/{task_id}")
async def update_task_endpoint(request: Request, task_id: int):
    """
    Atualiza tarefa e sincroniza com Google Tasks.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()
    title = data.get("title") or data.get("titulo")
    notes = data.get("notes") or data.get("descricao")
    due = data.get("due") or data.get("data_vencimento")
    status = data.get("status")
    prioridade = data.get("prioridade")
    sync_to_google = data.get("sync_to_google", True)

    due_datetime = None
    if due:
        try:
            if isinstance(due, str):
                due_datetime = datetime.fromisoformat(due.replace("Z", "+00:00"))
            else:
                due_datetime = due
        except:
            pass

    tasks_service = get_tasks_sync_service()
    result = await tasks_service.update_task(
        task_id=task_id,
        titulo=title,
        descricao=notes,
        data_vencimento=due_datetime,
        status=status,
        prioridade=prioridade,
        sync_to_google=sync_to_google
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@app.patch("/api/tasks/{task_id}")
async def patch_task_endpoint(request: Request, task_id: int):
    """
    Atualiza parcialmente tarefa (PATCH para compatibilidade com frontend).
    Redireciona para o endpoint PUT.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()

    # Mapear 'completed' para 'status' se presente
    if "completed" in data:
        data["status"] = "completed" if data["completed"] else "pending"

    # Usar o serviço de tasks
    tasks_service = get_tasks_sync_service()
    result = await tasks_service.update_task(
        task_id=task_id,
        titulo=data.get("title") or data.get("titulo"),
        descricao=data.get("notes") or data.get("descricao"),
        status=data.get("status"),
        prioridade=data.get("prioridade"),
        sync_to_google=data.get("sync_to_google", True)
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@app.put("/api/tasks/{task_id}/complete")
async def complete_task(request: Request, task_id: str):
    """
    Marca tarefa como concluida.
    Suporta tanto ID local (int) quanto Google Task ID (string).
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    tasks_service = get_tasks_sync_service()

    # Check if it's a local ID (numeric) or Google ID (string)
    try:
        local_id = int(task_id)
        result = await tasks_service.complete_task(local_id)
    except ValueError:
        # It's a Google Task ID - complete directly via API
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE LIMIT 1")
            account = cursor.fetchone()

        if not account:
            raise HTTPException(status_code=400, detail="Nenhuma conta Google conectada")

        from integrations.gmail import GmailIntegration
        gmail = GmailIntegration()
        tokens = await gmail.refresh_access_token(account["refresh_token"])

        if "error" in tokens:
            raise HTTPException(status_code=401, detail="Token invalido")

        tasks_api = get_tasks_integration()
        result = await tasks_api.complete_task(tokens["access_token"], task_id)

    return result


@app.delete("/api/tasks/{task_id}")
async def delete_task_endpoint(request: Request, task_id: int):
    """
    Deleta tarefa localmente e do Google Tasks.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    tasks_service = get_tasks_sync_service()
    result = await tasks_service.delete_task(task_id)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@app.get("/api/tasks/{task_id}/context")
async def get_task_context(request: Request, task_id: int):
    """
    Busca contexto completo de uma tarefa.

    Retorna:
    - Tarefa com dados do projeto
    - Contato relacionado (identificado pelo titulo ou contact_id)
    - Mensagens WhatsApp recentes com o contato
    - Emails recentes com o contato
    - Contexto do projeto (participantes, outras tarefas)
    - Sugestao de acao gerada por IA
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.task_context import get_task_context_service
    context_service = get_task_context_service()
    result = await context_service.get_task_context(task_id)

    if "error" in result and result["error"] == "Tarefa nao encontrada":
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@app.get("/api/tasks/{task_id}/suggest-followup")
async def suggest_task_followup(request: Request, task_id: int):
    """
    Sugere follow-up ao completar uma tarefa.
    Analisa contexto e retorna sugestão de prazo.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.task_context import get_task_context_service
    context_service = get_task_context_service()
    result = await context_service.suggest_followup(task_id)

    return result


@app.post("/api/tasks/{task_id}/complete-with-followup")
async def complete_task_with_followup(request: Request, task_id: int):
    """
    Completa tarefa e opcionalmente cria follow-up.

    Body:
    {
        "create_followup": true/false,
        "followup_title": "título do follow-up",
        "followup_days": 3,
        "contact_id": 123 (opcional),
        "project_id": 456 (opcional)
    }
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()

    # 1. Completar tarefa original
    tasks_service = get_tasks_sync_service()
    result = await tasks_service.complete_task(task_id)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # 2. Criar follow-up se solicitado
    followup_task = None
    if data.get("create_followup"):
        from datetime import timedelta

        followup_days = data.get("followup_days", 3)
        due_date = datetime.now() + timedelta(days=followup_days)

        followup_result = await tasks_service.create_task(
            titulo=data.get("followup_title", "Follow-up"),
            descricao=f"Follow-up da tarefa #{task_id}",
            data_vencimento=due_date,
            prioridade=5,
            contact_id=data.get("contact_id"),
            project_id=data.get("project_id"),
            sync_to_google=True
        )

        if "error" not in followup_result:
            followup_task = followup_result

    return {
        "completed": True,
        "task_id": task_id,
        "followup_created": followup_task is not None,
        "followup_task": followup_task
    }


@app.post("/api/tasks/sync")
async def sync_tasks(request: Request):
    """
    Sincronizacao bidirecional de tasks com Google Tasks.
    Push local -> Google, Pull Google -> Local.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    tasks_service = get_tasks_sync_service()
    result = await tasks_service.full_sync()

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return result


@app.get("/api/tasks/sync/test")
async def test_tasks_sync():
    """
    Endpoint para testar sync de tasks (sem autenticacao).
    Executa sync bidirecional completo.
    """
    tasks_service = get_tasks_sync_service()
    result = await tasks_service.full_sync()

    return {
        "status": "error" if "error" in result else "success",
        "result": result
    }


@app.get("/api/tasks/test-list")
async def test_list_tasks():
    """
    Endpoint para testar listagem de tasks (sem auth).
    Mostra as tarefas do banco de dados.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Primeiro, contar total
        cursor.execute("SELECT COUNT(*) as total FROM tasks")
        total = cursor.fetchone()['total']

        # Contar por status
        cursor.execute("SELECT status, COUNT(*) as count FROM tasks GROUP BY status")
        by_status = {row['status']: row['count'] for row in cursor.fetchall()}

        # Buscar todas as tarefas
        cursor.execute("""
            SELECT id, titulo, status, origem, google_task_id, data_vencimento,
                   sync_status, last_synced_at
            FROM tasks
            ORDER BY
                CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                data_vencimento ASC NULLS LAST
            LIMIT 30
        """)
        tasks = [dict(row) for row in cursor.fetchall()]

        # Format dates for JSON
        for t in tasks:
            if t.get('data_vencimento'):
                t['data_vencimento'] = t['data_vencimento'].isoformat() if hasattr(t['data_vencimento'], 'isoformat') else str(t['data_vencimento'])
            if t.get('last_synced_at'):
                t['last_synced_at'] = t['last_synced_at'].isoformat() if hasattr(t['last_synced_at'], 'isoformat') else str(t['last_synced_at'])

    return {"total": total, "by_status": by_status, "count": len(tasks), "tasks": tasks}


@app.get("/api/tasks/sync/status")
async def get_tasks_sync_status(request: Request):
    """
    Retorna status do sync de tasks.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        # Count by sync status
        cursor.execute("""
            SELECT sync_status, COUNT(*) as count
            FROM tasks
            GROUP BY sync_status
        """)
        by_status = {row["sync_status"]: row["count"] for row in cursor.fetchall()}

        # Count by origin
        cursor.execute("""
            SELECT origem, COUNT(*) as count
            FROM tasks
            GROUP BY origem
        """)
        by_origin = {row["origem"] or "unknown": row["count"] for row in cursor.fetchall()}

        # Last sync
        cursor.execute("""
            SELECT MAX(last_synced_at) as last_sync
            FROM tasks
            WHERE last_synced_at IS NOT NULL
        """)
        last_sync = cursor.fetchone()

    return {
        "by_sync_status": by_status,
        "by_origin": by_origin,
        "last_sync": last_sync["last_sync"] if last_sync else None,
        "total_local": sum(by_status.values()) if by_status else 0
    }


# ============== ANALYTICS API ==============

@app.get("/api/analytics/summary")
async def get_analytics_summary(
    request: Request,
    days: int = 30
):
    """
    Estatisticas para dashboard de analytics.

    Args:
        days: Periodo para analisar (default 30 dias)
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        # Total contatos por circulo
        cursor.execute("""
            SELECT COALESCE(circulo, 5) as circulo, COUNT(*) as total
            FROM contacts GROUP BY COALESCE(circulo, 5)
            ORDER BY circulo
        """)
        por_circulo = {row["circulo"]: row["total"] for row in cursor.fetchall()}

        # Interacoes no periodo (mensagens)
        cursor.execute("""
            SELECT COUNT(*) as total FROM messages
            WHERE enviado_em > NOW() - INTERVAL '%s days'
        """, (days,))
        total_mensagens = cursor.fetchone()["total"]

        # Mensagens por direcao
        cursor.execute("""
            SELECT
                direcao,
                COUNT(*) as total
            FROM messages
            WHERE enviado_em > NOW() - INTERVAL '%s days'
            GROUP BY direcao
        """, (days,))
        por_direcao = {row["direcao"]: row["total"] for row in cursor.fetchall()}

        # Health medio por circulo
        cursor.execute("""
            SELECT
                COALESCE(circulo, 5) as circulo,
                AVG(COALESCE(health_score, 50)) as avg_health,
                MIN(COALESCE(health_score, 50)) as min_health,
                MAX(COALESCE(health_score, 50)) as max_health
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 4
            GROUP BY COALESCE(circulo, 5)
            ORDER BY circulo
        """)
        health_por_circulo = {}
        for row in cursor.fetchall():
            health_por_circulo[row["circulo"]] = {
                "avg": round(float(row["avg_health"]), 1),
                "min": int(row["min_health"]),
                "max": int(row["max_health"])
            }

        # Health medio geral
        cursor.execute("""
            SELECT AVG(COALESCE(health_score, 50)) as avg
            FROM contacts WHERE COALESCE(circulo, 5) <= 4
        """)
        health_medio = cursor.fetchone()["avg"] or 50

        # Contatos por contexto
        cursor.execute("""
            SELECT
                COALESCE(contexto, 'professional') as contexto,
                COUNT(*) as total
            FROM contacts
            GROUP BY COALESCE(contexto, 'professional')
        """)
        por_contexto = {row["contexto"]: row["total"] for row in cursor.fetchall()}

        # Mensagens por canal
        cursor.execute("""
            SELECT
                c.canal,
                COUNT(*) as total
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.enviado_em > NOW() - INTERVAL '%s days'
            GROUP BY c.canal
        """, (days,))
        por_canal = {row["canal"]: row["total"] for row in cursor.fetchall()}

        # Contatos adicionados no periodo
        cursor.execute("""
            SELECT COUNT(*) as total FROM contacts
            WHERE criado_em > NOW() - INTERVAL '%s days'
        """, (days,))
        novos_contatos = cursor.fetchone()["total"]

        # Top tags
        cursor.execute("""
            SELECT tag, COUNT(*) as count
            FROM (
                SELECT jsonb_array_elements_text(tags) as tag
                FROM contacts
                WHERE tags IS NOT NULL AND tags != '[]'::jsonb
            ) t
            GROUP BY tag
            ORDER BY count DESC
            LIMIT 10
        """)
        top_tags = [{"tag": row["tag"], "count": row["count"]} for row in cursor.fetchall()]

        return {
            "periodo_dias": days,
            "contatos": {
                "total": sum(por_circulo.values()),
                "por_circulo": por_circulo,
                "por_contexto": por_contexto,
                "novos_periodo": novos_contatos
            },
            "mensagens": {
                "total_periodo": total_mensagens,
                "por_direcao": por_direcao,
                "por_canal": por_canal
            },
            "health": {
                "medio_geral": round(float(health_medio), 1),
                "por_circulo": health_por_circulo
            },
            "top_tags": top_tags,
            "gerado_em": datetime.now().isoformat()
        }


@app.get("/api/analytics/trends")
async def get_analytics_trends(
    request: Request,
    days: int = 30
):
    """
    Tendencias de interacoes ao longo do tempo.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        # Mensagens por dia
        cursor.execute("""
            SELECT
                DATE(enviado_em) as data,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE direcao = 'outbound') as enviadas,
                COUNT(*) FILTER (WHERE direcao = 'inbound') as recebidas
            FROM messages
            WHERE enviado_em > NOW() - INTERVAL '%s days'
            GROUP BY DATE(enviado_em)
            ORDER BY data
        """, (days,))

        mensagens_por_dia = []
        for row in cursor.fetchall():
            mensagens_por_dia.append({
                "data": row["data"].isoformat() if row["data"] else None,
                "total": row["total"],
                "enviadas": row["enviadas"],
                "recebidas": row["recebidas"]
            })

        # Contatos contatados por semana
        cursor.execute("""
            SELECT
                DATE_TRUNC('week', ultimo_contato) as semana,
                COUNT(*) as contatados
            FROM contacts
            WHERE ultimo_contato > NOW() - INTERVAL '%s days'
            GROUP BY DATE_TRUNC('week', ultimo_contato)
            ORDER BY semana
        """, (days,))

        contatos_por_semana = []
        for row in cursor.fetchall():
            contatos_por_semana.append({
                "semana": row["semana"].isoformat() if row["semana"] else None,
                "contatados": row["contatados"]
            })

        return {
            "periodo_dias": days,
            "mensagens_por_dia": mensagens_por_dia,
            "contatos_por_semana": contatos_por_semana,
            "gerado_em": datetime.now().isoformat()
        }


# ============== NOTIFICATIONS API ==============

from services.notifications import get_notification_service

@app.get("/api/notifications")
async def list_notifications(
    request: Request,
    limit: int = 20
):
    """
    Lista notificacoes priorizadas.
    Inclui aniversarios, health baixo, mensagens pendentes, tarefas.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_notification_service()
    notifications = service.get_notifications(limit)
    return {"notifications": notifications, "total": len(notifications)}


@app.get("/api/notifications/count")
async def get_notifications_count(request: Request):
    """Retorna contagem de notificacoes por tipo"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_notification_service()
    counts = service.get_notification_count()
    return counts


# ============== TIMELINE API ==============

from services.timeline import get_timeline_service

@app.get("/api/contacts/{contact_id}/timeline")
async def get_contact_timeline(
    request: Request,
    contact_id: int,
    limit: int = 50
):
    """
    Retorna timeline unificada do contato.
    Inclui mensagens, memorias e fatos.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_timeline_service()
    timeline = service.get_contact_timeline(contact_id, limit)
    return {"timeline": timeline, "contact_id": contact_id, "total": len(timeline)}


@app.get("/api/contacts/{contact_id}/timeline/summary")
async def get_contact_timeline_summary(
    request: Request,
    contact_id: int
):
    """Retorna resumo do contato para o timeline"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_timeline_service()
    summary = service.get_contact_summary(contact_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Contato nao encontrado")
    return summary


@app.get("/api/contacts/{contact_id}/timeline/messages")
async def get_timeline_message_details(
    request: Request,
    contact_id: int,
    ids: str = ""
):
    """
    Retorna detalhes de um grupo de mensagens para expandir na timeline.
    IDs sao passados como string separada por virgulas.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    if not ids:
        return []

    try:
        message_ids = [int(id.strip()) for id in ids.split(',') if id.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="IDs invalidos")

    service = get_timeline_service()
    messages = service.get_message_group_details(contact_id, message_ids)
    return messages


# ============== CONTACT INTERACTIONS ==============

class InteractionCreate(BaseModel):
    tipo: str
    titulo: Optional[str] = None
    descricao: Optional[str] = None
    data_interacao: Optional[str] = None


@app.post("/api/contacts/{contact_id}/interactions")
async def create_contact_interaction(contact_id: int, interaction: InteractionCreate):
    """Cria uma nova interação manual para o contato."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify contact exists
        cursor.execute("SELECT id, nome FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")

        # Parse date
        data_interacao = None
        if interaction.data_interacao:
            try:
                # Handle datetime-local format: "2026-03-26T14:52"
                data_interacao = datetime.fromisoformat(interaction.data_interacao.replace('Z', '+00:00'))
            except:
                data_interacao = datetime.now()
        else:
            data_interacao = datetime.now()

        # Insert interaction
        cursor.execute("""
            INSERT INTO contact_interactions (contact_id, tipo, titulo, descricao, data_interacao)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, criado_em
        """, (
            contact_id,
            interaction.tipo,
            interaction.titulo,
            interaction.descricao,
            data_interacao
        ))

        result = cursor.fetchone()

        # Update ultimo_contato on contact
        cursor.execute("""
            UPDATE contacts
            SET ultimo_contato = %s,
                total_interacoes = COALESCE(total_interacoes, 0) + 1,
                atualizado_em = NOW()
            WHERE id = %s
        """, (data_interacao, contact_id))

        conn.commit()

    # Recalculate health score after interaction (dual circles)
    health_result = recalcular_circulos_dual(contact_id)
    new_health = health_result.get('health_efetivo', 0)

    return {
        "status": "success",
        "interaction_id": result['id'],
        "contact_id": contact_id,
        "contact_name": contact['nome'],
        "health_score": new_health,
        "criado_em": result['criado_em'].isoformat()
        }


@app.get("/api/contacts/{contact_id}/interactions")
async def get_contact_interactions(contact_id: int, limit: int = 50):
    """Retorna interações manuais do contato."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, tipo, titulo, descricao, data_interacao, tags, sentimento, criado_em
            FROM contact_interactions
            WHERE contact_id = %s
            ORDER BY data_interacao DESC
            LIMIT %s
        """, (contact_id, limit))

        interactions = [dict(row) for row in cursor.fetchall()]
        return {"interactions": interactions, "total": len(interactions)}


@app.delete("/api/contacts/{contact_id}/interactions/{interaction_id}")
async def delete_contact_interaction(contact_id: int, interaction_id: int):
    """Remove uma interação do contato."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM contact_interactions
            WHERE id = %s AND contact_id = %s
        """, (interaction_id, contact_id))
        conn.commit()

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Interacao nao encontrada")

        return {"status": "success", "deleted_id": interaction_id}


# ============== INBOX API ==============

from services.inbox import get_inbox_service

@app.get("/api/inbox/conversations")
async def list_inbox_conversations(
    request: Request,
    limit: int = 50,
    filter_type: str = None
):
    """Lista conversas do inbox unificado (email + whatsapp)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_inbox_service()
    conversations = service.get_conversations(limit, filter_type)
    return {"conversations": conversations, "total": len(conversations)}


@app.get("/api/inbox/conversations/{conversation_id}")
async def get_inbox_conversation(
    request: Request,
    conversation_id: int
):
    """Detalhes de uma conversa"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_inbox_service()
    conversation = service.get_conversation_by_id(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")
    return conversation


@app.get("/api/inbox/conversations/{conversation_id}/messages")
async def get_inbox_conversation_messages(
    request: Request,
    conversation_id: int,
    limit: int = 100
):
    """Mensagens de uma conversa"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_inbox_service()
    messages = service.get_messages(conversation_id, limit)
    return {"messages": messages, "total": len(messages)}


@app.get("/api/inbox/unread")
async def get_inbox_unread_count(request: Request):
    """Total de conversas que requerem resposta"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_inbox_service()
    count = service.get_unread_count()
    return {"unread": count}


@app.post("/api/inbox/conversations/{conversation_id}/read")
async def mark_inbox_conversation_read(
    request: Request,
    conversation_id: int
):
    """Marca conversa como lida"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_inbox_service()
    service.mark_as_read(conversation_id)
    return {"success": True}


@app.post("/api/inbox/conversations/{conversation_id}/reply")
async def send_inbox_reply(
    request: Request,
    conversation_id: int
):
    """Envia resposta em uma conversa (WhatsApp ou Email)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()
    content = data.get("content", "").strip()

    if not content:
        raise HTTPException(status_code=400, detail="Conteudo da mensagem e obrigatorio")

    service = get_inbox_service()

    # Get conversation details
    conversation = service.get_conversation_by_id(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    contact_id = conversation.get("contact_id")
    channel = conversation.get("channel")

    # Get contact info for phone/email
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, telefones, emails
            FROM contacts
            WHERE id = %s
        """, (contact_id,))
        contact = cursor.fetchone()

    if not contact:
        raise HTTPException(status_code=404, detail="Contato nao encontrado")

    contact = dict(contact)
    sent_result = None

    # Send via appropriate channel
    if channel == "whatsapp":
        # Get phone number
        phones = contact.get("telefones") or []
        logger.info(f"Contact phones: {phones}")

        if not phones:
            raise HTTPException(status_code=400, detail="Contato nao tem telefone cadastrado")

        # Extract phone number - handle different formats
        phone = None
        if isinstance(phones, list) and len(phones) > 0:
            first_phone = phones[0]
            if isinstance(first_phone, dict):
                phone = first_phone.get("numero") or first_phone.get("phone") or first_phone.get("number")
            else:
                phone = str(first_phone)
        elif isinstance(phones, str):
            phone = phones

        if not phone:
            raise HTTPException(status_code=400, detail=f"Formato de telefone invalido: {phones}")

        logger.info(f"Sending WhatsApp to {phone}: {content[:50]}...")

        # Check Evolution API configuration
        if not whatsapp.base_url or not whatsapp.api_key:
            logger.error("Evolution API not configured - missing EVOLUTION_API_URL or EVOLUTION_API_KEY")
            raise HTTPException(
                status_code=503,
                detail="WhatsApp nao configurado. Verifique EVOLUTION_API_URL e EVOLUTION_API_KEY no ambiente."
            )

        # Send via WhatsApp
        try:
            sent_result = await whatsapp.send_text(phone, content)
            logger.info(f"WhatsApp send result: {sent_result}")
            if sent_result and "error" in sent_result:
                error_msg = sent_result.get('error', 'Erro desconhecido')
                logger.error(f"WhatsApp API error: {error_msg}")
                raise HTTPException(status_code=502, detail=f"Erro da API WhatsApp: {error_msg}")
        except HTTPException:
            raise
        except httpx.TimeoutException:
            logger.error("WhatsApp API timeout")
            raise HTTPException(status_code=504, detail="Timeout ao conectar com WhatsApp API")
        except httpx.ConnectError as e:
            logger.error(f"WhatsApp API connection error: {e}")
            raise HTTPException(status_code=503, detail="Nao foi possivel conectar com WhatsApp API")
        except Exception as e:
            logger.error(f"Error sending WhatsApp: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Erro ao enviar mensagem: {str(e)}")

    elif channel == "email":
        # TODO: Implement email sending via Gmail API
        raise HTTPException(status_code=501, detail="Envio de email ainda nao implementado")

    else:
        raise HTTPException(status_code=400, detail=f"Canal desconhecido: {channel}")

    # Save message to database
    with get_db() as conn:
        cursor = conn.cursor()

        # Insert outgoing message
        cursor.execute("""
            INSERT INTO messages (conversation_id, contact_id, direcao, conteudo, enviado_em)
            VALUES (%s, %s, 'outgoing', %s, NOW())
            RETURNING id
        """, (conversation_id, contact_id, content))
        message_id = cursor.fetchone()["id"]

        # Update conversation
        cursor.execute("""
            UPDATE conversations
            SET ultimo_mensagem = NOW(),
                total_mensagens = total_mensagens + 1,
                requer_resposta = FALSE,
                atualizado_em = NOW()
            WHERE id = %s
        """, (conversation_id,))

        conn.commit()

    return {
        "success": True,
        "message_id": message_id,
        "channel": channel,
        "sent_result": sent_result
    }


# =============================================================================
# EMAIL TRIAGE ENDPOINTS
# =============================================================================

@app.get("/emails", response_class=HTMLResponse)
async def emails_page(request: Request):
    """Página de triagem de emails"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_emails.html", {"request": request, "user": user})


@app.get("/api/email-triage")
async def get_email_triage_list(
    request: Request,
    status: str = "pending",
    account_type: str = None,
    classification: str = None,
    limit: int = 50,
    offset: int = 0
):
    """Lista emails para triagem"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        # Garantir que as tabelas existem
        init_db()

        from services.email_triage import get_email_triage_service
        service = get_email_triage_service()

        return service.get_triage_list(
            status=status,
            account_type=account_type,
            classification=classification,
            limit=limit,
            offset=offset
        )
    except Exception as e:
        import traceback
        print(f"Error listing emails: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Erro: {str(e)}")


@app.get("/api/email-triage/stats")
async def get_email_triage_stats(request: Request):
    """Estatísticas da triagem de emails"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        # Garantir que as tabelas existem
        init_db()

        from services.email_triage import get_email_triage_service
        service = get_email_triage_service()

        return service.get_stats()
    except Exception as e:
        import traceback
        print(f"Error getting stats: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Erro: {str(e)}")


@app.post("/api/email-triage/process")
async def process_emails_for_triage(
    request: Request,
    account_type: str = None,
    limit: int = 50
):
    """Processa novos emails para triagem"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        # Garantir que as tabelas existem
        init_db()

        from services.email_triage import get_email_triage_service
        service = get_email_triage_service()

        return service.process_new_emails(account_type=account_type, limit=limit)
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"Error processing emails: {error_detail}")
        raise HTTPException(status_code=500, detail=f"Erro: {str(e)}")


@app.post("/api/email-triage/approve")
async def approve_email_triage(request: Request, data: dict):
    """Aprova emails em lote"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    ids = data.get("ids", [])
    tags = data.get("tags")

    if not ids:
        raise HTTPException(status_code=400, detail="IDs são obrigatórios")

    from services.email_triage import get_email_triage_service
    service = get_email_triage_service()

    return service.approve_batch(ids, tags)


@app.post("/api/email-triage/dismiss")
async def dismiss_email_triage(request: Request, data: dict):
    """Descarta emails em lote"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    ids = data.get("ids", [])
    reason = data.get("reason")

    if not ids:
        raise HTTPException(status_code=400, detail="IDs são obrigatórios")

    from services.email_triage import get_email_triage_service
    service = get_email_triage_service()

    return service.dismiss_batch(ids, reason)


@app.post("/api/email-triage/{triage_id}/action")
async def mark_email_triage_action(request: Request, triage_id: int, data: dict):
    """Marca email como tendo ação tomada"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    action = data.get("action")
    if not action:
        raise HTTPException(status_code=400, detail="Ação é obrigatória")

    from services.email_triage import get_email_triage_service
    service = get_email_triage_service()

    return service.mark_actioned(triage_id, action)


@app.post("/api/email-triage/{triage_id}/archive")
async def archive_email_triage(request: Request, triage_id: int):
    """Arquiva email no Gmail e remove label !!Renato"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar triage com dados da mensagem
            cursor.execute("""
                SELECT et.*, m.external_id as gmail_id, m.metadata
                FROM email_triage et
                LEFT JOIN messages m ON m.id = et.message_id
                WHERE et.id = %s
            """, (triage_id,))
            triage = cursor.fetchone()

            if not triage:
                raise HTTPException(status_code=404, detail="Email não encontrado")

            gmail_id = triage.get('gmail_id')
            if not gmail_id:
                raise HTTPException(status_code=400, detail="Email sem ID do Gmail")

            # Buscar conta do email
            metadata = triage.get('metadata') or {}
            account_email = metadata.get('account')

            if not account_email:
                raise HTTPException(status_code=400, detail="Email sem conta associada")

            # Buscar access token da conta
            cursor.execute("""
                SELECT access_token, refresh_token
                FROM google_accounts
                WHERE email = %s
            """, (account_email,))
            account = cursor.fetchone()

            if not account:
                raise HTTPException(status_code=400, detail=f"Conta {account_email} não encontrada")

            access_token = account.get('access_token')

            # Se o token expirou, tentar refresh
            from integrations.gmail import GmailIntegration
            gmail = GmailIntegration()

            # Arquivar e remover label
            result = await gmail.archive_and_remove_label(
                access_token,
                gmail_id,
                "!!Renato"
            )

            # Marcar como arquivado no sistema
            cursor.execute("""
                UPDATE email_triage
                SET status = 'actioned',
                    action_taken = 'archived',
                    actioned_at = NOW()
                WHERE id = %s
            """, (triage_id,))
            conn.commit()

            return {
                "success": True,
                "gmail_archived": result.get('archived'),
                "label_removed": result.get('label_removed'),
                "triage_id": triage_id
            }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error archiving email: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Erro ao arquivar: {str(e)}")


@app.post("/api/email-triage/sync-labels")
async def sync_gmail_labels(request: Request, label: str = "!!Renato"):
    """Importa emails que já têm uma label específica no Gmail"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        from services.email_triage import get_email_triage_service
        service = get_email_triage_service()

        result = await service.sync_labeled_emails(label_name=label)
        return result

    except Exception as e:
        import traceback
        print(f"Error syncing labels: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Erro ao sincronizar: {str(e)}")


@app.post("/api/email-triage/fix-metadata")
async def fix_email_metadata(request: Request):
    """Corrige metadata dos emails importados que estão sem from_name"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        from integrations.gmail import GmailIntegration
        import json

        gmail = GmailIntegration()
        fixed = 0
        errors = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar emails sem from_name no metadata
            cursor.execute("""
                SELECT m.id, m.external_id, ga.access_token, ga.refresh_token, ga.email as account_email
                FROM messages m
                JOIN email_triage et ON et.message_id = m.id
                JOIN google_accounts ga ON m.metadata->>'account' = ga.email
                WHERE m.external_id IS NOT NULL
                  AND (m.metadata->>'from_name' IS NULL OR m.metadata->>'from_name' = '')
            """)
            messages = cursor.fetchall()

            for msg in messages:
                try:
                    access_token = msg['access_token']
                    gmail_id = msg['external_id']

                    # Buscar detalhes do email
                    msg_details = await gmail.get_message(access_token, gmail_id)

                    if "error" in msg_details:
                        if msg_details.get("error") == "token_expired":
                            # Tentar refresh
                            refresh_result = await gmail.refresh_access_token(msg['refresh_token'])
                            if "access_token" in refresh_result:
                                access_token = refresh_result["access_token"]
                                cursor.execute("""
                                    UPDATE google_accounts SET access_token = %s WHERE email = %s
                                """, (access_token, msg['account_email']))
                                conn.commit()
                                msg_details = await gmail.get_message(access_token, gmail_id)
                            else:
                                continue
                        else:
                            continue

                    if "error" in msg_details:
                        continue

                    headers = gmail.parse_message_headers(msg_details)
                    from_header = headers.get("from", "")
                    from_email = gmail.extract_email_address(from_header)
                    from_name = from_header.split('<')[0].strip().strip('"') if '<' in from_header else from_email

                    # Atualizar metadata
                    cursor.execute("""
                        UPDATE messages
                        SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                        WHERE id = %s
                    """, (
                        json.dumps({"from": from_email, "from_name": from_name}),
                        msg['id']
                    ))
                    fixed += 1

                except Exception as e:
                    errors.append(f"Message {msg['id']}: {str(e)}")

            conn.commit()

        return {"fixed": fixed, "errors": errors}

    except Exception as e:
        import traceback
        print(f"Error fixing metadata: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Erro: {str(e)}")


# === REGRAS DE TRIAGEM ===

@app.get("/api/email-triage/rules")
async def get_triage_rules(request: Request):
    """Lista regras de classificação"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    from services.email_triage import get_email_triage_service
    service = get_email_triage_service()

    return service.get_rules_list()


@app.post("/api/email-triage/rules")
async def create_triage_rule(request: Request, data: dict):
    """Cria nova regra de classificação"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    from services.email_triage import get_email_triage_service
    service = get_email_triage_service()

    return service.create_rule(data)


@app.put("/api/email-triage/rules/{rule_id}")
async def update_triage_rule(request: Request, rule_id: int, data: dict):
    """Atualiza regra de classificação"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    from services.email_triage import get_email_triage_service
    service = get_email_triage_service()

    return service.update_rule(rule_id, data)


@app.delete("/api/email-triage/rules/{rule_id}")
async def delete_triage_rule(request: Request, rule_id: int):
    """Deleta regra de classificação"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    from services.email_triage import get_email_triage_service
    service = get_email_triage_service()

    return service.delete_rule(rule_id)


@app.post("/api/email-triage/rules/init")
async def init_default_triage_rules(request: Request):
    """Inicializa tabelas e regras default"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    # Garantir que as tabelas existem
    init_db()

    from services.email_triage import get_email_triage_service
    service = get_email_triage_service()

    return service.init_default_rules()


# =============================================================================
# ACTION PROPOSALS ENDPOINTS (INTEL Proativo)
# =============================================================================

@app.get("/api/action-proposals")
async def get_action_proposals_list(
    request: Request,
    limit: int = 20,
    include_resolved: bool = False
):
    """Lista propostas de acao pendentes (auto-limpa stale antes)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Auto-resolver ações onde usuario já respondeu (cleanup on read)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE action_proposals ap
                SET status = 'resolved', responded_at = NOW(),
                    ai_reasoning = COALESCE(ap.ai_reasoning, '') || ' | Auto-resolvido ao carregar dashboard'
                WHERE ap.status = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM messages m
                      JOIN conversations cv ON cv.id = m.conversation_id
                      WHERE cv.contact_id = ap.contact_id
                        AND m.direcao = 'outgoing'
                        AND m.enviado_em > ap.criado_em
                  )
            """)
            conn.commit()
    except Exception:
        pass

    from services.action_proposals import get_action_proposals
    service = get_action_proposals()

    if include_resolved:
        # Para admin/debug, incluir todas
        with get_pg_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ap.*, c.nome as contact_name, c.foto_url as contact_foto
                FROM action_proposals ap
                LEFT JOIN contacts c ON c.id = ap.contact_id
                ORDER BY ap.criado_em DESC
                LIMIT %s
            """, (limit,))
            proposals = []
            for row in cursor.fetchall():
                proposal = dict(row)
                for key in ['criado_em', 'expires_at', 'responded_at', 'executed_at']:
                    if proposal.get(key) and hasattr(proposal[key], 'isoformat'):
                        proposal[key] = proposal[key].isoformat()
                proposals.append(proposal)
            return {"proposals": proposals}
    else:
        proposals = service.get_pending_proposals(limit)
        return {"proposals": proposals}


@app.get("/api/action-proposals/count")
async def get_action_proposals_count(request: Request):
    """Retorna contagem de propostas pendentes por urgencia"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.action_proposals import get_action_proposals
    service = get_action_proposals()
    return service.get_pending_count()


@app.get("/api/action-proposals/stats")
async def get_action_proposals_stats(request: Request, days: int = 30):
    """Estatisticas de propostas"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.action_proposals import get_action_proposals
    service = get_action_proposals()
    return service.get_stats(days)


@app.post("/api/action-proposals/test-notification")
@app.get("/api/action-proposals/test-notification")
async def test_proposal_notification(request: Request, contact_name: str = "Pedro Salles"):
    """Endpoint de teste - cria proposta fake e envia notificacao via intel-bot (sem auth)"""
    from services.action_proposals import get_action_proposals
    from services.whatsapp_notifications import get_whatsapp_notifications

    proposals_service = get_action_proposals()
    notifications = get_whatsapp_notifications()

    # Criar proposta de teste - usando action_type que funciona sem dados reais
    test_proposal = {
        'action_type': 'pending_response',  # Tipo que funciona sem event_id
        'contact_id': None,
        'message_id': None,
        'title': f'Responder {contact_name}',
        'description': f'{contact_name} fez uma pergunta que aguarda resposta.',
        'trigger_text': 'Oi Renato, consegue me enviar o orcamento atualizado? Preciso apresentar amanha.',
        'ai_reasoning': 'Mensagem contem pergunta que requer resposta',
        'confidence': 0.92,
        'urgency': 'high',
        'action_params': {
            'contact_name': contact_name,
            'question_type': 'orcamento'
        },
        'options': [
            {'id': 'open_conversation', 'label': 'Abrir conversa', 'action': 'open_conversation'},
            {'id': 'create_task', 'label': 'Criar tarefa', 'action': 'create_task'},
            {'id': 'acknowledge', 'label': 'Marcar como visto', 'action': 'acknowledge'},
            {'id': 'ignore', 'label': 'Ignorar', 'action': 'dismiss'}
        ]
    }

    # Salvar proposta
    proposal = proposals_service.create_proposal(test_proposal)

    if not proposal:
        return {"success": False, "error": "Falha ao criar proposta"}

    # Adicionar contact_name para a notificacao
    proposal['contact_name'] = contact_name

    # Enviar notificacao
    sent = await notifications.send_proposal_notification(proposal)

    return {
        "success": sent,
        "proposal_id": proposal['id'],
        "message": "Notificacao enviada via intel-bot! Responda na conversa para executar" if sent else "Falha ao enviar notificacao"
    }


@app.delete("/api/action-proposals/test-cleanup")
@app.get("/api/action-proposals/test-cleanup")
async def cleanup_test_proposals():
    """Remove todas as proposals de teste (sem contact_id)"""
    with get_pg_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM action_proposals
            WHERE contact_id IS NULL
            RETURNING id
        """)
        deleted = cursor.fetchall()
        conn.commit()
        return {
            "success": True,
            "deleted_count": len(deleted),
            "message": f"Removidas {len(deleted)} proposals de teste"
        }


@app.get("/api/action-proposals/{proposal_id}/quick-action")
async def quick_action_proposal(proposal_id: int, option: str, confirm: bool = False):
    """
    Endpoint para executar acao rapidamente via link (sem auth).
    Usado pelos links enviados via WhatsApp.
    Requer confirm=true para executar (evita execucao por preview de link).
    """
    from services.action_proposals import get_action_proposals
    from services.action_executor import get_action_executor

    proposals_service = get_action_proposals()
    executor = get_action_executor()

    # Buscar proposta
    proposal = proposals_service.get_proposal(proposal_id)
    if not proposal:
        return HTMLResponse(content="""
            <html><body style="font-family: sans-serif; padding: 20px; text-align: center;">
                <h2>❌ Proposta não encontrada</h2>
                <p>Esta proposta pode ter sido removida ou expirada.</p>
            </body></html>
        """, status_code=404)

    if proposal['status'] != 'pending':
        return HTMLResponse(content=f"""
            <html><body style="font-family: sans-serif; padding: 20px; text-align: center;">
                <h2>ℹ️ Proposta já processada</h2>
                <p>Status atual: {proposal['status']}</p>
            </body></html>
        """)

    # Se não confirmou, mostrar página de confirmação
    if not confirm:
        option_label = option.replace('_', ' ').title()
        return HTMLResponse(content=f"""
            <html>
            <head>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body {{ font-family: -apple-system, sans-serif; padding: 20px; text-align: center; background: #f5f5f5; }}
                    .card {{ background: white; border-radius: 12px; padding: 24px; max-width: 400px; margin: 20px auto; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
                    h2 {{ color: #333; margin-bottom: 8px; }}
                    .desc {{ color: #666; margin-bottom: 20px; }}
                    .action {{ font-weight: 600; color: #6366f1; margin: 16px 0; }}
                    .btn {{ display: inline-block; padding: 12px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; margin: 8px; }}
                    .btn-primary {{ background: #6366f1; color: white; }}
                    .btn-secondary {{ background: #e5e7eb; color: #374151; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <h2>📋 {proposal['title']}</h2>
                    <p class="desc">{proposal.get('description', '')}</p>
                    <p class="action">Ação: {option_label}</p>
                    <div>
                        <a href="?option={option}&confirm=true" class="btn btn-primary">✓ Confirmar</a>
                        <a href="/" class="btn btn-secondary">✕ Cancelar</a>
                    </div>
                </div>
            </body>
            </html>
        """)

    # Executar acao (confirmado)
    result = await executor.execute(proposal_id, option_id=option)

    if result.get('success'):
        # Enviar confirmacao via intel-bot
        try:
            from services.whatsapp_notifications import get_whatsapp_notifications
            await get_whatsapp_notifications().send_confirmation(f"✅ {result.get('message', 'Acao executada!')}")
        except Exception:
            pass

        return HTMLResponse(content=f"""
            <html><body style="font-family: sans-serif; padding: 20px; text-align: center;">
                <h2>✅ Ação executada!</h2>
                <p>{result.get('message', 'Sucesso')}</p>
                <p style="color: #666; margin-top: 20px;">Você pode fechar esta janela.</p>
            </body></html>
        """)
    else:
        return HTMLResponse(content=f"""
            <html><body style="font-family: sans-serif; padding: 20px; text-align: center;">
                <h2>❌ Erro</h2>
                <p>{result.get('message', 'Falha ao executar ação')}</p>
            </body></html>
        """, status_code=400)


@app.get("/api/action-proposals/{proposal_id}")
async def get_action_proposal(request: Request, proposal_id: int):
    """Detalhes de uma proposta"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.action_proposals import get_action_proposals
    service = get_action_proposals()

    proposal = service.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposta nao encontrada")

    return proposal


class ExecuteProposalRequest(BaseModel):
    option_id: Optional[str] = None
    custom_params: Optional[Dict] = None


@app.post("/api/action-proposals/{proposal_id}/execute")
async def execute_action_proposal(
    request: Request,
    proposal_id: int,
    body: ExecuteProposalRequest
):
    """Executa acao de uma proposta"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.action_executor import get_action_executor
    executor = get_action_executor()

    result = await executor.execute(
        proposal_id,
        option_id=body.option_id,
        custom_params=body.custom_params
    )

    return result


@app.post("/api/action-proposals/{proposal_id}/dismiss")
async def dismiss_action_proposal(request: Request, proposal_id: int):
    """Ignora uma proposta"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.action_proposals import get_action_proposals
    service = get_action_proposals()

    result = service.dismiss_proposal(proposal_id)
    if not result:
        raise HTTPException(status_code=404, detail="Proposta nao encontrada ou ja processada")

    return {"success": True, "proposal": result}


@app.post("/api/action-proposals/{proposal_id}/reject")
async def reject_action_proposal(
    request: Request,
    proposal_id: int,
    reason: Optional[str] = None
):
    """Rejeita uma proposta"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.action_proposals import get_action_proposals
    service = get_action_proposals()

    result = service.reject_proposal(proposal_id, reason)
    if not result:
        raise HTTPException(status_code=404, detail="Proposta nao encontrada ou ja processada")

    return {"success": True, "proposal": result}


@app.post("/api/action-proposals/expire-old")
async def expire_old_proposals(request: Request):
    """Marca propostas expiradas (pode ser chamado por cron)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.action_proposals import get_action_proposals
    service = get_action_proposals()

    count = service.expire_old_proposals()
    return {"expired": count}


# =============================================================================
# ANALYZER FEEDBACK & LEARNING ENDPOINTS
# =============================================================================

@app.get("/api/analyzer/feedback/stats")
async def get_analyzer_feedback_stats(request: Request, days: int = 30):
    """Get feedback statistics for intent learning."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.analyzer_feedback import get_feedback_service
    service = get_feedback_service()

    return service.get_intent_stats(days=days)


@app.get("/api/analyzer/learning-summary")
async def get_analyzer_learning_summary(request: Request):
    """Get summary of what the analyzer has learned from feedback."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.analyzer_feedback import get_feedback_service
    service = get_feedback_service()

    return service.get_learning_summary()


@app.get("/api/analyzer/settings")
async def get_analyzer_settings(request: Request):
    """Get current analyzer settings."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.analyzer_feedback import get_feedback_service
    service = get_feedback_service()

    return service.get_settings()


class AnalyzerSettingUpdate(BaseModel):
    key: str
    value: Any


@app.post("/api/analyzer/settings")
async def update_analyzer_setting(request: Request, setting: AnalyzerSettingUpdate):
    """Update an analyzer setting."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.analyzer_feedback import get_feedback_service
    service = get_feedback_service()

    valid_keys = ['min_confidence', 'enabled_intents', 'urgency_threshold']
    if setting.key not in valid_keys:
        raise HTTPException(status_code=400, detail=f"Invalid setting key. Valid: {valid_keys}")

    success = service.update_setting(setting.key, setting.value)
    return {"success": success}


# =============================================================================
# BATCH OPERATIONS ENDPOINTS
# =============================================================================

@app.post("/api/contacts/enrich-linkedin-batch")
async def enrich_linkedin_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = 20,
    circulo_max: int = 3
):
    """Inicia enriquecimento LinkedIn em batch (background)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    import sys
    sys.path.insert(0, os.path.join(BASE_DIR, '..', 'scripts'))
    from enrich_linkedin_batch import enrich_batch

    background_tasks.add_task(enrich_batch, limit, circulo_max)

    return {
        "status": "started",
        "message": f"Enriquecimento iniciado para ate {limit} contatos (circulos 1-{circulo_max})"
    }


@app.post("/api/contacts/generate-insights-batch")
async def generate_insights_batch_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = 10,
    circulo_max: int = 3
):
    """Inicia geracao de insights AI em batch (background)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    import sys
    import asyncio
    sys.path.insert(0, os.path.join(BASE_DIR, '..', 'scripts'))
    from generate_insights_batch import generate_insights_batch

    async def run_task():
        await generate_insights_batch(limit, circulo_max)

    background_tasks.add_task(asyncio.run, run_task())

    return {
        "status": "started",
        "message": f"Geracao de insights iniciada para ate {limit} contatos (circulos 1-{circulo_max})"
    }


# =============================================================================
# SEARCH API ENDPOINTS
# =============================================================================

@app.get("/api/contacts/by-company/{empresa}")
async def get_contacts_by_company(
    request: Request,
    empresa: str,
    limit: int = 50
):
    """Busca contatos por empresa"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_search_service()
    return {"contacts": service.get_contacts_by_company(empresa, limit)}


@app.get("/api/contacts/birthdays")
async def get_upcoming_birthdays(
    request: Request,
    days: int = 30
):
    """Busca contatos com aniversario nos proximos N dias"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_search_service()
    return {"contacts": service.get_nearby_birthdays(days)}


@app.get("/api/contacts/stale")
async def get_stale_contacts_api(
    request: Request,
    days: int = 90,
    circulo_max: int = 3
):
    """Busca contatos importantes sem interacao recente"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_search_service()
    return {"contacts": service.get_stale_contacts(days, circulo_max)}


# =============================================================================
# EXPORT API ENDPOINTS
# =============================================================================

from services.export import get_export_service
from fastapi.responses import StreamingResponse

@app.get("/api/export/contacts/csv")
async def export_contacts_csv(
    request: Request,
    circulo: int = None,
    tags: str = None,
    empresa: str = None
):
    """Exporta contatos para CSV"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_export_service()
    tags_list = [t.strip() for t in tags.split(",")] if tags else None

    csv_content = service.export_contacts_csv(
        circulo=circulo,
        tags=tags_list,
        empresa=empresa
    )

    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=contacts_{datetime.now().strftime('%Y%m%d')}.csv"}
    )


@app.get("/api/export/contacts/json")
async def export_contacts_json(
    request: Request,
    circulo: int = None,
    tags: str = None,
    empresa: str = None,
    include_messages: bool = False,
    include_insights: bool = True
):
    """Exporta contatos para JSON"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_export_service()
    tags_list = [t.strip() for t in tags.split(",")] if tags else None

    contacts = service.export_contacts_json(
        circulo=circulo,
        tags=tags_list,
        empresa=empresa,
        include_messages=include_messages,
        include_insights=include_insights
    )

    return {"contacts": contacts, "total": len(contacts), "exported_at": datetime.now().isoformat()}


@app.get("/api/export/statistics")
async def export_statistics(request: Request):
    """Exporta estatisticas gerais do sistema"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_export_service()
    return service.export_statistics()


# =============================================================================
# BATCH OPERATIONS API ENDPOINTS
# =============================================================================

from services.batch_operations import get_batch_service

class BatchTagsRequest(BaseModel):
    contact_ids: List[int]
    add_tags: Optional[List[str]] = None
    remove_tags: Optional[List[str]] = None

class BatchCircleRequest(BaseModel):
    contact_ids: List[int]
    circulo: int

class BatchContextRequest(BaseModel):
    contact_ids: List[int]
    contexto: str

class MergeContactsRequest(BaseModel):
    primary_id: int
    secondary_ids: List[int]

class DeleteContactsRequest(BaseModel):
    contact_ids: List[int]
    confirm: bool = False


@app.post("/api/batch/tags")
async def batch_update_tags(
    request: Request,
    data: BatchTagsRequest
):
    """Adiciona ou remove tags de multiplos contatos"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_batch_service()
    return service.update_tags_batch(data.contact_ids, data.add_tags, data.remove_tags)


@app.post("/api/batch/circle")
async def batch_update_circle(
    request: Request,
    data: BatchCircleRequest
):
    """Atualiza circulo de multiplos contatos"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_batch_service()
    return service.update_circle_batch(data.contact_ids, data.circulo)


@app.post("/api/batch/context")
async def batch_update_context(
    request: Request,
    data: BatchContextRequest
):
    """Atualiza contexto de multiplos contatos"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_batch_service()
    return service.update_context_batch(data.contact_ids, data.contexto)


@app.post("/api/batch/merge")
async def batch_merge_contacts(
    request: Request,
    data: MergeContactsRequest
):
    """Merge multiplos contatos em um principal"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_batch_service()
    return service.merge_contacts(data.primary_id, data.secondary_ids)


@app.post("/api/batch/delete")
async def batch_delete_contacts(
    request: Request,
    data: DeleteContactsRequest
):
    """Deleta multiplos contatos"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_batch_service()
    return service.delete_contacts_batch(data.contact_ids, data.confirm)


@app.post("/api/batch/recalculate-health")
async def batch_recalculate_health(
    request: Request,
    background_tasks: BackgroundTasks,
    circulo_max: int = 5
):
    """Recalcula health score para contatos"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_batch_service()
    background_tasks.add_task(service.recalculate_health_batch, None, circulo_max)

    return {"status": "started", "message": f"Recalculando health para circulos 1-{circulo_max}"}


# =============================================================================
# MAINTENANCE CRON ENDPOINTS
# =============================================================================

@app.post("/api/maintenance/daily")
async def run_daily_maintenance(
    request: Request,
    background_tasks: BackgroundTasks,
    full: bool = False
):
    """Executa manutencao diaria em background"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    import sys
    import asyncio
    sys.path.insert(0, os.path.join(BASE_DIR, '..', 'scripts'))
    from daily_maintenance import run_maintenance

    async def run_task():
        await run_maintenance(full=full)

    background_tasks.add_task(asyncio.run, run_task())

    return {
        "status": "started",
        "message": f"Manutencao {'completa' if full else 'rapida'} iniciada em background"
    }


@app.get("/api/maintenance/status")
async def get_maintenance_status(request: Request):
    """Retorna status e alertas do sistema"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_pg_db() as conn:
        cursor = conn.cursor()

        status = {"alerts": [], "stats": {}}

        # Stale contacts alert
        cursor.execute("""
            SELECT COUNT(*) as count FROM contacts
            WHERE COALESCE(circulo, 5) <= 2
            AND ultimo_contato < NOW() - INTERVAL '30 days'
        """)
        stale_count = cursor.fetchone()["count"]
        if stale_count > 0:
            status["alerts"].append({
                "type": "stale_contacts",
                "message": f"{stale_count} contatos importantes sem contato ha mais de 30 dias",
                "severity": "warning"
            })

        # Low health contacts alert
        cursor.execute("""
            SELECT COUNT(*) as count FROM contacts
            WHERE COALESCE(circulo, 5) <= 2
            AND health_score < 40
        """)
        low_health_count = cursor.fetchone()["count"]
        if low_health_count > 0:
            status["alerts"].append({
                "type": "low_health",
                "message": f"{low_health_count} contatos importantes com health baixo",
                "severity": "warning"
            })

        # Stats
        cursor.execute("SELECT COUNT(*) as total FROM contacts")
        status["stats"]["total_contacts"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE total_interacoes > 0")
        status["stats"]["with_interactions"] = cursor.fetchone()["total"]

        return status


# =============================================================================
# SSE (SERVER-SENT EVENTS) ENDPOINTS
# =============================================================================

from sse_starlette.sse import EventSourceResponse
from services.notifications import get_notification_service

async def notification_event_generator(request: Request, interval: int = 30):
    """Generator para SSE de notificacoes e action proposals"""
    import json
    from services.action_proposals import get_action_proposals

    service = get_notification_service()
    proposals_service = get_action_proposals()

    while True:
        if await request.is_disconnected():
            break

        notifications = service.get_notifications(limit=10)
        counts = service.get_notification_count()

        # Incluir action proposals pendentes
        proposals = proposals_service.list_pending(limit=10)
        proposals_count = len(proposals)

        yield {
            "event": "notifications",
            "data": json.dumps({
                "notifications": notifications,
                "counts": counts,
                "action_proposals": proposals,
                "proposals_count": proposals_count,
                "timestamp": datetime.now().isoformat()
            })
        }

        await asyncio.sleep(interval)


@app.get("/api/notifications/stream")
async def notifications_stream(
    request: Request,
    interval: int = 30
):
    """
    SSE endpoint para notificacoes em tempo real.

    Uso no frontend:
    ```javascript
    const eventSource = new EventSource('/api/notifications/stream?interval=30');
    eventSource.addEventListener('notifications', (e) => {
        const data = JSON.parse(e.data);
        console.log(data.notifications);
        console.log(data.counts);
    });
    ```
    """
    import asyncio

    # Note: SSE may not work well on Vercel serverless, better for local dev
    return EventSourceResponse(notification_event_generator(request, interval))


# =============================================================================
# PUSH NOTIFICATIONS ENDPOINTS
# =============================================================================

from pydantic import BaseModel

class PushSubscription(BaseModel):
    endpoint: str
    keys: dict


@app.get("/api/push/vapid-public-key")
async def get_vapid_public_key():
    """Get VAPID public key for push subscription."""
    from services.push_notifications import get_push_service
    service = get_push_service()
    public_key = service.get_public_key()

    if not public_key:
        return {"configured": False, "key": None}

    return {"configured": True, "key": public_key}


@app.post("/api/push/subscribe")
async def subscribe_push(request: Request, subscription: PushSubscription):
    """Subscribe to push notifications."""
    from services.push_notifications import get_push_service

    user = get_current_user(request)
    user_id = user.get('email') if user else None

    service = get_push_service()

    if not service.is_configured():
        return {"success": False, "error": "Push notifications not configured on server"}

    success = service.save_subscription(
        {
            'endpoint': subscription.endpoint,
            'keys': subscription.keys
        },
        user_id=user_id
    )

    return {"success": success}


@app.post("/api/push/unsubscribe")
async def unsubscribe_push(request: Request, subscription: PushSubscription):
    """Unsubscribe from push notifications."""
    from services.push_notifications import get_push_service

    service = get_push_service()
    success = service.remove_subscription(subscription.endpoint)

    return {"success": success}


@app.post("/api/push/test")
async def test_push_notification(request: Request):
    """Send a test push notification (for debugging)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.push_notifications import get_push_service

    service = get_push_service()

    if not service.is_configured():
        return {"success": False, "error": "Push notifications not configured"}

    result = service.send_notification(
        title="INTEL Test",
        body="Push notifications estao funcionando!",
        data={"url": "/rap"},
        tag="test-notification"
    )

    return result


# =============================================================================
# AVATAR FETCHER ENDPOINTS
# =============================================================================

@app.get("/api/avatars/stats")
async def get_avatar_stats(request: Request):
    """Retorna estatisticas de fotos dos contatos."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.avatar_fetcher import get_avatar_fetcher
    fetcher = get_avatar_fetcher()
    return fetcher.get_photo_stats()


@app.post("/api/avatars/fetch")
async def fetch_avatars(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = 50
):
    """
    Inicia busca de fotos de perfil do WhatsApp em background.
    Retorna imediatamente com status de inicio.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.avatar_fetcher import get_avatar_fetcher

    fetcher = get_avatar_fetcher()

    # Verificar quantos contatos precisam de foto
    contacts = fetcher.get_contacts_needing_photos(limit)

    if not contacts:
        return {
            "started": False,
            "message": "Nenhum contato precisa de foto",
            "contacts_to_process": 0
        }

    # Executar em background
    async def run_fetch():
        await fetcher.fetch_photos_batch(limit=limit, delay_between=1.5)

    background_tasks.add_task(asyncio.create_task, run_fetch())

    return {
        "started": True,
        "message": f"Iniciando busca de fotos para {len(contacts)} contatos",
        "contacts_to_process": len(contacts)
    }


@app.get("/api/avatars/fetch-single/{contact_id}")
async def fetch_single_avatar(request: Request, contact_id: int):
    """Busca foto de um contato especifico."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.avatar_fetcher import get_avatar_fetcher
    from database import get_db

    # Buscar contato
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, telefones FROM contacts WHERE id = %s
        """, (contact_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")

        contact = dict(row)

    # Extrair telefone
    telefones = contact.get('telefones', [])
    if isinstance(telefones, str):
        import json
        try:
            telefones = json.loads(telefones)
        except:
            telefones = []

    if not telefones:
        return {"success": False, "error": "Contato sem telefone"}

    phone = telefones[0] if isinstance(telefones[0], str) else (telefones[0].get('number') or telefones[0].get('numero', ''))

    if not phone:
        return {"success": False, "error": "Telefone invalido"}

    # Buscar foto
    fetcher = get_avatar_fetcher()
    photo_url = await fetcher.fetch_whatsapp_photo(phone)

    if photo_url:
        success = fetcher.update_contact_photo(contact_id, photo_url)
        return {
            "success": success,
            "photo_url": photo_url,
            "contact_name": contact['nome']
        }

    return {"success": False, "error": "Foto nao encontrada no WhatsApp"}


@app.get("/api/avatars/debug/{contact_id}")
async def debug_avatar_fetch(request: Request, contact_id: int):
    """Debug: mostra resposta crua da Evolution API."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from integrations.evolution_api import get_evolution_client
    from database import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, telefones FROM contacts WHERE id = %s", (contact_id,))
        row = cursor.fetchone()
        if not row:
            return {"error": "Contato nao encontrado"}
        contact = dict(row)

    telefones = contact.get('telefones', [])
    if isinstance(telefones, str):
        import json
        try:
            telefones = json.loads(telefones)
        except:
            return {"error": "Erro parsing telefones", "raw": contact.get('telefones')}

    if not telefones:
        return {"error": "Sem telefones", "contact": contact}

    if isinstance(telefones[0], str):
        phone = telefones[0]
    else:
        phone = telefones[0].get('number') or telefones[0].get('numero', '')

    # Limpar telefone (mesmo que Evolution API faz)
    phone_clean = ''.join(filter(str.isdigit, phone))
    if not phone_clean.startswith('55') and len(phone_clean) <= 11:
        phone_clean = '55' + phone_clean

    # Chamar Evolution API diretamente
    evolution = get_evolution_client()
    result = await evolution.get_profile_picture(phone)

    return {
        "contact_name": contact['nome'],
        "phone_original": phone,
        "phone_clean": phone_clean,
        "evolution_configured": evolution.is_configured,
        "evolution_instance": evolution.instance_name,
        "api_response": result
    }


@app.post("/api/avatars/fetch-google")
async def fetch_google_avatars(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = 50
):
    """
    Inicia busca de fotos do Google para contatos com google_contact_id.
    Alguns contatos podem ter fotos reais no Google que nao foram importadas.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.avatar_fetcher import get_avatar_fetcher

    fetcher = get_avatar_fetcher()
    contacts = fetcher.get_contacts_needing_google_photos(limit)

    if not contacts:
        return {
            "started": False,
            "message": "Nenhum contato com google_contact_id precisa de foto",
            "contacts_to_process": 0
        }

    async def run_fetch():
        await fetcher.fetch_google_photos_batch(limit=limit, delay_between=0.5)

    background_tasks.add_task(asyncio.create_task, run_fetch())

    return {
        "started": True,
        "message": f"Iniciando busca de fotos Google para {len(contacts)} contatos",
        "contacts_to_process": len(contacts)
    }


@app.post("/api/avatars/fetch-batch")
async def fetch_avatars_batch(request: Request, limit: int = 100):
    """
    Processa um lote de contatos (default 100).
    Chame repetidamente até processed == 0 para processar todos.
    Funciona no Vercel (não ultrapassa timeout).
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.avatar_fetcher import get_avatar_fetcher

    fetcher = get_avatar_fetcher()

    # Buscar contatos que precisam de foto
    contacts = fetcher.get_contacts_needing_photos(limit)

    if not contacts:
        # Buscar stats finais
        stats = fetcher.get_photo_stats()
        return {
            "done": True,
            "message": "Todos os contatos foram processados!",
            "processed": 0,
            "stats": stats
        }

    results = {"success": 0, "failed": 0, "skipped": 0}

    for contact in contacts:
        phone = contact.get('phone', '')

        if not phone:
            results['skipped'] += 1
            continue

        try:
            photo_url = await fetcher.fetch_whatsapp_photo(phone)

            if photo_url:
                success = fetcher.update_contact_photo(contact['id'], photo_url)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            else:
                results['failed'] += 1
        except Exception as e:
            results['failed'] += 1

    # Buscar stats atualizados
    stats = fetcher.get_photo_stats()

    return {
        "done": False,
        "processed": len(contacts),
        "results": results,
        "remaining": stats.get('potencial_whatsapp', 0),
        "message": f"Processado {len(contacts)} contatos. Restam {stats.get('potencial_whatsapp', 0)}. Chame novamente para continuar."
    }


@app.post("/api/avatars/fetch-all-auto")
async def fetch_all_avatars_auto(request: Request):
    """
    Processa TODOS os contatos automaticamente em lotes.
    Usa loop interno com limite de tempo para não estourar timeout do Vercel.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    import time
    from services.avatar_fetcher import get_avatar_fetcher

    fetcher = get_avatar_fetcher()
    start_time = time.time()
    max_time = 55  # 55 segundos (Vercel timeout é ~60s)

    total_processed = 0
    results = {"success": 0, "failed": 0, "skipped": 0}

    while True:
        # Verificar timeout
        elapsed = time.time() - start_time
        if elapsed > max_time:
            break

        # Buscar próximo lote
        contacts = fetcher.get_contacts_needing_photos(limit=20)

        if not contacts:
            # Terminou!
            stats = fetcher.get_photo_stats()
            return {
                "done": True,
                "message": "Todos os contatos foram processados!",
                "total_processed": total_processed,
                "results": results,
                "stats": stats
            }

        for contact in contacts:
            # Verificar timeout novamente
            if time.time() - start_time > max_time:
                break

            phone = contact.get('phone')
            google_id = contact.get('google_contact_id')
            photo_url = None

            # 1. Tentar WhatsApp primeiro (se tem telefone)
            if phone:
                try:
                    photo_url = await fetcher.fetch_whatsapp_photo(phone)
                except:
                    pass

            # 2. Se não achou no WhatsApp, tentar Google (se tem google_contact_id)
            if not photo_url and google_id:
                try:
                    photo_url = await fetcher.fetch_google_photo(google_id)
                except:
                    pass

            # 3. Atualizar resultado
            if photo_url:
                if fetcher.update_contact_photo(contact['id'], photo_url):
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif not phone and not google_id:
                results['skipped'] += 1
            else:
                results['failed'] += 1

            # Marcar contato como verificado (evita reprocessar)
            fetcher.mark_avatar_checked(contact['id'])
            total_processed += 1

    # Stats atualizados
    stats = fetcher.get_photo_stats()

    return {
        "done": False,
        "message": f"Processado {total_processed} contatos em {round(time.time() - start_time)}s. Chame novamente para continuar.",
        "total_processed": total_processed,
        "results": results,
        "remaining": stats.get('pendentes_total', 0),
        "stats": stats
    }


@app.get("/api/activity/recent")
async def get_recent_activity(
    request: Request,
    limit: int = 20
):
    """Retorna atividades recentes para feed"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_notification_service()
    return {"activities": service.get_recent_activity(limit)}


# =============================================================================
# AI SUGGESTIONS ENDPOINTS
# =============================================================================

class AISuggestionCreate(BaseModel):
    contact_id: int
    tipo: str
    titulo: str
    descricao: Optional[str] = None
    razao: Optional[str] = None
    dados: Optional[dict] = None
    prioridade: Optional[int] = 5
    validade: Optional[str] = None
    confianca: Optional[float] = 0.8


@app.get("/api/ai/suggestions")
async def get_ai_suggestions(
    request: Request,
    status: str = "pending",
    tipo: str = None,
    contact_id: int = None,
    limit: int = 50
):
    """Lista sugestoes da IA"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_pg_db() as conn:
        cursor = conn.cursor()

        conditions = ["1=1"]
        params = []

        if status:
            conditions.append("s.status = %s")
            params.append(status)

        if tipo:
            conditions.append("s.tipo = %s")
            params.append(tipo)

        if contact_id:
            conditions.append("s.contact_id = %s")
            params.append(contact_id)

        where_clause = " AND ".join(conditions)

        cursor.execute(f"""
            SELECT s.*, c.nome as contact_name, c.foto_url, c.circulo
            FROM ai_suggestions s
            LEFT JOIN contacts c ON c.id = s.contact_id
            WHERE {where_clause}
            AND (s.validade IS NULL OR s.validade > NOW())
            ORDER BY s.prioridade DESC, s.criado_em DESC
            LIMIT %s
        """, params + [limit])

        suggestions = []
        for row in cursor.fetchall():
            s = dict(row)
            if s.get("criado_em"):
                s["criado_em"] = s["criado_em"].isoformat()
            if s.get("validade"):
                s["validade"] = s["validade"].isoformat()
            suggestions.append(s)

        return {"suggestions": suggestions, "total": len(suggestions)}


@app.post("/api/ai/suggestions")
async def create_ai_suggestion(
    request: Request,
    data: AISuggestionCreate
):
    """Cria uma nova sugestao da IA"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_pg_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO ai_suggestions
            (contact_id, tipo, titulo, descricao, razao, dados, prioridade, validade, confianca)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data.contact_id,
            data.tipo,
            data.titulo,
            data.descricao,
            data.razao,
            json.dumps(data.dados) if data.dados else '{}',
            data.prioridade,
            data.validade,
            data.confianca
        ))

        suggestion_id = cursor.fetchone()["id"]
        conn.commit()

        return {"id": suggestion_id, "status": "created"}


@app.get("/api/ai/suggestions/{suggestion_id}")
async def get_ai_suggestion(
    request: Request,
    suggestion_id: int
):
    """Detalhes de uma sugestao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_pg_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT s.*, c.nome as contact_name, c.foto_url, c.empresa, c.circulo
            FROM ai_suggestions s
            LEFT JOIN contacts c ON c.id = s.contact_id
            WHERE s.id = %s
        """, (suggestion_id,))

        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Sugestao nao encontrada")

        s = dict(row)
        for key in ["criado_em", "aceita_em", "descartada_em", "executada_em", "validade"]:
            if s.get(key) and hasattr(s[key], "isoformat"):
                s[key] = s[key].isoformat()

        return s


@app.post("/api/ai/suggestions/{suggestion_id}/accept")
async def accept_ai_suggestion(
    request: Request,
    suggestion_id: int
):
    """Aceita uma sugestao da IA"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_pg_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE ai_suggestions
            SET status = 'accepted', aceita_em = NOW()
            WHERE id = %s AND status = 'pending'
            RETURNING id
        """, (suggestion_id,))

        result = cursor.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Sugestao nao encontrada ou ja processada")

        conn.commit()
        return {"id": suggestion_id, "status": "accepted"}


@app.post("/api/ai/suggestions/{suggestion_id}/dismiss")
async def dismiss_ai_suggestion(
    request: Request,
    suggestion_id: int,
    motivo: str = None
):
    """Descarta uma sugestao da IA"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_pg_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE ai_suggestions
            SET status = 'dismissed', descartada_em = NOW(), motivo_descarte = %s
            WHERE id = %s AND status = 'pending'
            RETURNING id
        """, (motivo, suggestion_id))

        result = cursor.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Sugestao nao encontrada ou ja processada")

        conn.commit()
        return {"id": suggestion_id, "status": "dismissed"}


@app.post("/api/ai/suggestions/{suggestion_id}/execute")
async def mark_suggestion_executed(
    request: Request,
    suggestion_id: int,
    resultado: str = None
):
    """Marca uma sugestao como executada"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_pg_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE ai_suggestions
            SET status = 'executed', executada_em = NOW(), resultado = %s
            WHERE id = %s AND status = 'accepted'
            RETURNING id
        """, (resultado, suggestion_id))

        result = cursor.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Sugestao nao encontrada ou nao aceita")

        conn.commit()
        return {"id": suggestion_id, "status": "executed"}


@app.get("/api/ai/suggestions/stats")
async def get_ai_suggestions_stats(request: Request):
    """Estatisticas de sugestoes da IA"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_pg_db() as conn:
        cursor = conn.cursor()

        stats = {}

        # By status
        cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM ai_suggestions
            GROUP BY status
        """)
        stats["by_status"] = {row["status"]: row["count"] for row in cursor.fetchall()}

        # By type
        cursor.execute("""
            SELECT tipo, COUNT(*) as count
            FROM ai_suggestions
            WHERE status = 'pending'
            GROUP BY tipo
        """)
        stats["pending_by_type"] = {row["tipo"]: row["count"] for row in cursor.fetchall()}

        # Acceptance rate
        cursor.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'accepted' OR status = 'executed') as accepted,
                COUNT(*) FILTER (WHERE status = 'dismissed') as dismissed,
                COUNT(*) as total
            FROM ai_suggestions
            WHERE status != 'pending'
        """)
        rates = cursor.fetchone()
        if rates["total"] > 0:
            stats["acceptance_rate"] = round(rates["accepted"] / rates["total"] * 100, 1)
        else:
            stats["acceptance_rate"] = 0

        return stats


# =============================================================================
# AI AGENT ENDPOINTS
# =============================================================================

from services.ai_agent import get_ai_agent

@app.post("/api/ai/generate-suggestions")
async def generate_ai_suggestions(
    request: Request,
    background_tasks: BackgroundTasks
):
    """Dispara geracao de sugestoes da IA em background"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    agent = get_ai_agent()

    async def run_generation():
        await agent.run_daily_generation()

    background_tasks.add_task(asyncio.run, run_generation())

    return {"status": "started", "message": "Geracao de sugestoes iniciada em background"}


@app.post("/api/ai/cleanup-expired")
async def cleanup_expired_suggestions(request: Request):
    """Remove sugestoes expiradas"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    agent = get_ai_agent()
    deleted = agent.cleanup_expired_suggestions()

    return {"deleted": deleted, "message": f"{deleted} sugestoes expiradas removidas"}


@app.post("/api/ai/auto-enrich")
async def auto_enrich_priority_contacts(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = 10
):
    """
    Enriquece automaticamente contatos dos circulos 1 e 2.
    Busca contatos sem resumo_ai ou com enriquecimento desatualizado (>30 dias).
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    agent = get_ai_agent()

    async def run_enrichment():
        return await agent.auto_enrich_priority_contacts(limit=limit)

    background_tasks.add_task(asyncio.run, run_enrichment())

    return {
        "status": "started",
        "message": f"Enriquecimento de ate {limit} contatos C1-C2 iniciado em background"
    }


@app.get("/api/ai/auto-enrich/status")
async def get_auto_enrich_status(request: Request):
    """
    Retorna contatos C1-C2 que precisam de enriquecimento.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        # Contatos que precisam enriquecimento
        cursor.execute("""
            SELECT COUNT(*) as total FROM contacts
            WHERE COALESCE(circulo, 5) <= 2
            AND (
                resumo_ai IS NULL
                OR ultimo_enriquecimento IS NULL
                OR ultimo_enriquecimento < NOW() - INTERVAL '30 days'
            )
        """)
        needs_enrichment = cursor.fetchone()["total"]

        # Contatos ja enriquecidos
        cursor.execute("""
            SELECT COUNT(*) as total FROM contacts
            WHERE COALESCE(circulo, 5) <= 2
            AND resumo_ai IS NOT NULL
            AND ultimo_enriquecimento IS NOT NULL
            AND ultimo_enriquecimento >= NOW() - INTERVAL '30 days'
        """)
        already_enriched = cursor.fetchone()["total"]

        # Total C1-C2
        cursor.execute("""
            SELECT COUNT(*) as total FROM contacts
            WHERE COALESCE(circulo, 5) <= 2
        """)
        total_priority = cursor.fetchone()["total"]

    return {
        "total_priority_contacts": total_priority,
        "needs_enrichment": needs_enrichment,
        "already_enriched": already_enriched,
        "enrichment_rate": round(already_enriched / total_priority * 100, 1) if total_priority > 0 else 0
    }


# =============================================================================
# SMART TRIGGERS / AUTOMATIONS ENDPOINTS
# =============================================================================

from services.smart_triggers import get_smart_triggers

class AutomationCreate(BaseModel):
    nome: str
    descricao: Optional[str] = None
    trigger_type: str
    trigger_config: dict
    action_type: str
    action_config: dict


@app.get("/api/ai/automations")
async def get_automations(
    request: Request,
    active_only: bool = True
):
    """Lista automacoes"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_smart_triggers()
    automations = service.get_automations(active_only=active_only)

    return {"automations": automations, "total": len(automations)}


@app.post("/api/ai/automations")
async def create_automation(
    request: Request,
    data: AutomationCreate
):
    """Cria nova automacao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_smart_triggers()
    automation_id = service.create_automation(
        nome=data.nome,
        descricao=data.descricao,
        trigger_type=data.trigger_type,
        trigger_config=data.trigger_config,
        action_type=data.action_type,
        action_config=data.action_config
    )

    return {"id": automation_id, "status": "created"}


@app.post("/api/ai/automations/{automation_id}/toggle")
async def toggle_automation(
    request: Request,
    automation_id: int,
    ativo: bool = True
):
    """Ativa/desativa automacao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_smart_triggers()
    success = service.toggle_automation(automation_id, ativo)

    if not success:
        raise HTTPException(status_code=404, detail="Automacao nao encontrada")

    return {"id": automation_id, "ativo": ativo}


@app.delete("/api/ai/automations/{automation_id}")
async def delete_automation(
    request: Request,
    automation_id: int
):
    """Remove automacao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_smart_triggers()
    success = service.delete_automation(automation_id)

    if not success:
        raise HTTPException(status_code=404, detail="Automacao nao encontrada")

    return {"id": automation_id, "status": "deleted"}


@app.post("/api/ai/automations/run")
async def run_automations(
    request: Request,
    background_tasks: BackgroundTasks
):
    """Executa todas as automacoes ativas"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_smart_triggers()

    def run_task():
        return service.run_automations()

    background_tasks.add_task(run_task)

    return {"status": "started", "message": "Execucao de automacoes iniciada"}


@app.post("/api/ai/automations/setup-defaults")
async def setup_default_automations(request: Request):
    """Configura automacoes padrao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_smart_triggers()
    created = service.setup_default_automations()

    return {"created": created, "message": f"{created} automacoes padrao criadas"}


# =============================================================================
# HEALTH PREDICTIONS ENDPOINTS
# =============================================================================

from services.health_predictions import get_health_predictions

@app.get("/api/ai/at-risk")
async def get_at_risk_contacts(
    request: Request,
    threshold: int = 40,
    circulo_max: int = 3,
    limit: int = 50
):
    """Retorna contatos em risco de queda de health"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_health_predictions()
    contacts = service.get_at_risk_contacts(threshold, circulo_max, limit)

    return {"contacts": contacts, "total": len(contacts)}


@app.get("/api/ai/predict-health/{contact_id}")
async def predict_contact_health(
    request: Request,
    contact_id: int,
    dias: int = 30
):
    """Preve health futuro de um contato"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_health_predictions()
    prediction = service.predict_health(contact_id, dias)

    if "error" in prediction:
        raise HTTPException(status_code=404, detail=prediction["error"])

    return prediction


@app.get("/api/ai/prediction-history/{contact_id}")
async def get_prediction_history(
    request: Request,
    contact_id: int,
    limit: int = 10
):
    """Historico de previsoes para um contato"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_health_predictions()
    history = service.get_prediction_history(contact_id, limit)

    return {"predictions": history, "total": len(history)}


@app.post("/api/ai/run-predictions")
async def run_batch_predictions(
    request: Request,
    background_tasks: BackgroundTasks,
    circulo_max: int = 3,
    dias: int = 30
):
    """Executa previsoes em batch"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_health_predictions()

    def run_task():
        return service.run_batch_predictions(circulo_max, dias, limit=100)

    background_tasks.add_task(run_task)

    return {"status": "started", "message": "Previsoes em batch iniciadas"}


@app.post("/api/ai/verify-predictions")
async def verify_past_predictions(
    request: Request,
    days_back: int = 30
):
    """Verifica acuracia de previsoes passadas"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_health_predictions()
    result = service.verify_past_predictions(days_back)

    return result


# =============================================================================
# MESSAGE TEMPLATES ENDPOINTS
# =============================================================================

from services.message_suggestions import get_message_suggestions

class TemplateCreate(BaseModel):
    nome: str
    categoria: str
    corpo: str
    canal: Optional[str] = None
    assunto: Optional[str] = None
    tags: Optional[List[str]] = None


@app.get("/api/templates")
async def get_templates(
    request: Request,
    categoria: str = None,
    canal: str = None
):
    """Lista templates de mensagens"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_message_suggestions()
    templates = service.get_templates(categoria=categoria, canal=canal)

    return {"templates": templates, "total": len(templates)}


@app.post("/api/templates")
async def create_template(
    request: Request,
    data: TemplateCreate
):
    """Cria novo template"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_message_suggestions()
    template_id = service.create_template(
        nome=data.nome,
        categoria=data.categoria,
        corpo=data.corpo,
        canal=data.canal,
        assunto=data.assunto,
        tags=data.tags
    )

    return {"id": template_id, "status": "created"}


@app.get("/api/templates/{template_id}")
async def get_template(
    request: Request,
    template_id: int
):
    """Obtem template especifico"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_message_suggestions()
    template = service.get_template(template_id)

    if not template:
        raise HTTPException(status_code=404, detail="Template nao encontrado")

    return template


@app.delete("/api/templates/{template_id}")
async def delete_template(
    request: Request,
    template_id: int
):
    """Remove template"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_message_suggestions()
    success = service.delete_template(template_id)

    if not success:
        raise HTTPException(status_code=404, detail="Template nao encontrado")

    return {"id": template_id, "status": "deleted"}


@app.post("/api/templates/{template_id}/render")
async def render_template(
    request: Request,
    template_id: int,
    contact_id: int = None,
    variables: Dict = None
):
    """Renderiza template com variaveis"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_message_suggestions()

    if contact_id:
        result = service.render_for_contact(template_id, contact_id)
    else:
        result = service.render_template(template_id, variables or {})

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@app.get("/api/templates/categories")
async def get_template_categories(request: Request):
    """Lista categorias de templates"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_message_suggestions()
    categories = service.get_categories()

    return {"categories": categories}


@app.post("/api/templates/setup-defaults")
async def setup_default_templates(request: Request):
    """Configura templates padrao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_message_suggestions()
    created = service.setup_default_templates()

    return {"created": created, "message": f"{created} templates padrao criados"}


@app.post("/api/ai/suggest-message/{contact_id}")
async def suggest_message_for_contact(
    request: Request,
    contact_id: int,
    contexto: str = None,
    canal: str = "whatsapp"
):
    """Sugere mensagem personalizada usando IA"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_message_suggestions()
    result = await service.suggest_message(contact_id, contexto, canal)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


# =========================================================================
# DIGEST GENERATOR ENDPOINTS
# =========================================================================

from services.digest_generator import get_digest_generator


@app.get("/api/digests")
def list_digests(
    request: Request,
    tipo: str = None,
    limit: int = 10
):
    """Lista digests recentes"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_digest_generator()
    return service.get_recent_digests(tipo=tipo, limit=limit)


@app.get("/api/digests/latest/{tipo}")
def get_latest_digest(
    request: Request,
    tipo: str
):
    """Obtem digest mais recente de um tipo"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_digest_generator()
    digest = service.get_latest_digest(tipo)

    if not digest:
        raise HTTPException(status_code=404, detail="Nenhum digest encontrado")

    return digest


@app.get("/api/digests/{digest_id}")
def get_digest(
    request: Request,
    digest_id: int
):
    """Obtem digest por ID"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_digest_generator()
    digest = service.get_digest(digest_id)

    if not digest:
        raise HTTPException(status_code=404, detail="Digest nao encontrado")

    return digest


@app.post("/api/digests/daily")
def generate_daily_digest(
    request: Request,
    date: str = None
):
    """Gera digest diario"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_digest_generator()

    target_date = None
    if date:
        try:
            target_date = datetime.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de data invalido")

    return service.generate_daily_digest(date=target_date)


@app.post("/api/digests/weekly")
def generate_weekly_digest(
    request: Request,
    week_start: str = None
):
    """Gera digest semanal"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_digest_generator()

    target_date = None
    if week_start:
        try:
            target_date = datetime.fromisoformat(week_start)
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de data invalido")

    return service.generate_weekly_digest(week_start=target_date)


@app.post("/api/digests/{digest_id}/send")
def mark_digest_sent(
    request: Request,
    digest_id: int
):
    """Marca digest como enviado"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_digest_generator()
    success = service.mark_as_sent(digest_id)

    if not success:
        raise HTTPException(status_code=404, detail="Digest nao encontrado")

    return {"status": "sent", "digest_id": digest_id}


@app.post("/api/digests/{digest_id}/ai-summary")
async def generate_digest_ai_summary(
    request: Request,
    digest_id: int
):
    """Gera resumo com IA para um digest"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_digest_generator()
    summary = await service.generate_ai_summary(digest_id)

    if not summary:
        raise HTTPException(status_code=400, detail="Nao foi possivel gerar resumo")

    return {"digest_id": digest_id, "summary": summary}


# =========================================================================
# CALENDAR EVENTS ENDPOINTS
# =========================================================================

from services.calendar_events import get_calendar_events
from services.calendar_sync import get_calendar_sync


@app.post("/api/calendar/events")
async def create_calendar_event_endpoint(request: Request):
    """Cria evento no calendario"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()

    if "summary" not in data or "start_datetime" not in data or "end_datetime" not in data:
        raise HTTPException(status_code=400, detail="summary, start_datetime e end_datetime sao obrigatorios")

    service = get_calendar_events()

    event = service.create_event(
        summary=data["summary"],
        start_datetime=datetime.fromisoformat(data["start_datetime"]),
        end_datetime=datetime.fromisoformat(data["end_datetime"]),
        description=data.get("description"),
        location=data.get("location"),
        contact_id=data.get("contact_id"),
        prospect_id=data.get("prospect_id"),
        attendees=data.get("attendees"),
        create_in_google=data.get("create_in_google", True)
    )
    return event


@app.get("/api/calendar/events")
async def list_calendar_events(
    request: Request,
    start: str = None,
    end: str = None,
    days: int = 7,
    limit: int = 50
):
    """Lista eventos do calendario - busca direto do Google Calendar"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Buscar token da conta Google conectada
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE LIMIT 1")
        account = cursor.fetchone()

    if not account:
        return {"events": [], "total": 0, "error": "no_google_account"}

    # Refresh token
    from integrations.gmail import GmailIntegration
    gmail = GmailIntegration()
    tokens = await gmail.refresh_access_token(account["refresh_token"])

    if "error" in tokens:
        return {"events": [], "total": 0, "error": "token_refresh_failed"}

    access_token = tokens.get("access_token")
    calendar = get_calendar_integration()

    # Calcular periodo
    from zoneinfo import ZoneInfo
    sp_tz = ZoneInfo("America/Sao_Paulo")

    if start and end:
        # Parse das datas (formato YYYY-MM-DD)
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=sp_tz)
            end_dt = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=sp_tz)
        except ValueError:
            # Tentar formato ISO
            start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
    else:
        # Proximos N dias
        now = datetime.now(sp_tz)
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=days)

    # Converter para UTC
    start_utc = start_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    # Buscar eventos do Google Calendar
    result = await calendar.list_events(
        access_token=access_token,
        time_min=start_utc,
        time_max=end_utc,
        max_results=limit
    )

    if "error" in result:
        return {"events": [], "total": 0, "error": result.get("error")}

    # Formatar eventos para o frontend
    events = []
    for item in result.get("items", []):
        start_info = item.get("start", {})
        end_info = item.get("end", {})

        # Determinar se e all-day
        is_all_day = "date" in start_info and "dateTime" not in start_info

        if is_all_day:
            event_start = start_info.get("date") + "T00:00:00"
            event_end = end_info.get("date") + "T23:59:59"
        else:
            event_start = start_info.get("dateTime", "")
            event_end = end_info.get("dateTime", "")

        # Extrair link de conferencia
        conference = None
        if item.get("conferenceData"):
            entry_points = item["conferenceData"].get("entryPoints", [])
            for ep in entry_points:
                if ep.get("entryPointType") == "video":
                    conference = ep.get("uri")
                    break

        events.append({
            "id": item.get("id"),
            "summary": item.get("summary", "Sem titulo"),
            "description": item.get("description"),
            "location": item.get("location"),
            "start_datetime": event_start,
            "end_datetime": event_end,
            "is_all_day": is_all_day,
            "html_link": item.get("htmlLink"),
            "conference": conference,
            "contact_name": None  # TODO: match with contacts
        })

    return {"events": events, "total": len(events)}


@app.get("/api/calendar/events/today")
def get_today_calendar_events(request: Request):
    """Lista eventos de hoje"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_events()
    events = service.get_today_events()
    return {"events": events, "total": len(events)}


@app.get("/api/calendar/events/{event_id}")
def get_calendar_event_endpoint(request: Request, event_id: int):
    """Busca evento por ID"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_events()
    event = service.get_event(event_id)

    if not event:
        raise HTTPException(status_code=404, detail="Evento nao encontrado")

    return event


@app.put("/api/calendar/events/{event_id}")
async def update_calendar_event_endpoint(request: Request, event_id: int):
    """Atualiza evento"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    data = await request.json()
    service = get_calendar_events()

    # Converter datetime strings se presentes
    if "start_datetime" in data:
        data["start_datetime"] = datetime.fromisoformat(data["start_datetime"])
    if "end_datetime" in data:
        data["end_datetime"] = datetime.fromisoformat(data["end_datetime"])

    event = service.update_event(event_id, data)

    if not event:
        raise HTTPException(status_code=404, detail="Evento nao encontrado")

    return event


@app.delete("/api/calendar/events/{event_id}")
def delete_calendar_event_endpoint(
    request: Request,
    event_id: int,
    delete_from_google: bool = True
):
    """Deleta evento"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_events()
    success = service.delete_event(event_id, delete_from_google=delete_from_google)

    if not success:
        raise HTTPException(status_code=404, detail="Evento nao encontrado")

    return {"deleted": True, "event_id": event_id}


@app.post("/api/calendar/events/{event_id}/link-contact/{contact_id}")
def link_event_to_contact(request: Request, event_id: int, contact_id: int):
    """Vincula evento a um contato"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_events()
    event = service.link_to_contact(event_id, contact_id)

    if not event:
        raise HTTPException(status_code=404, detail="Evento nao encontrado")

    return event


@app.post("/api/calendar/events/{event_id}/link-prospect/{prospect_id}")
def link_event_to_prospect(request: Request, event_id: int, prospect_id: int):
    """Vincula evento a um prospect"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_events()
    event = service.link_to_prospect(event_id, prospect_id)

    if not event:
        raise HTTPException(status_code=404, detail="Evento nao encontrado")

    return event


@app.get("/api/contacts/{contact_id}/calendar")
async def get_contact_calendar_events(request: Request, contact_id: int, limit: int = 20):
    """Lista eventos de um contato - busca no Google Calendar pelo nome e email"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Buscar dados do contato incluindo emails
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome, empresa, emails FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()

        if not contact:
            raise HTTPException(status_code=404, detail="Contato não encontrado")

        # Buscar token Google
        cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE LIMIT 1")
        account = cursor.fetchone()

    if not account:
        return {"events": [], "total": 0, "error": "no_google_account"}

    # Refresh token
    from integrations.gmail import GmailIntegration
    gmail = GmailIntegration()
    tokens = await gmail.refresh_access_token(account["refresh_token"])

    if "error" in tokens:
        return {"events": [], "total": 0, "error": "token_refresh_failed"}

    access_token = tokens.get("access_token")
    from integrations.google_calendar import GoogleCalendarIntegration
    calendar = GoogleCalendarIntegration()

    # Extrair emails do contato
    contact_emails = []
    if contact.get("emails"):
        emails_data = contact["emails"]
        if isinstance(emails_data, str):
            import json
            try:
                emails_data = json.loads(emails_data)
            except:
                emails_data = []
        if isinstance(emails_data, list):
            for e in emails_data:
                if isinstance(e, dict) and e.get("email"):
                    contact_emails.append(e["email"].lower())
                elif isinstance(e, str):
                    contact_emails.append(e.lower())

    # Se não tem email, não há como filtrar por participante
    if not contact_emails:
        return {"events": [], "total": 0, "message": "Contato sem email cadastrado"}

    unique_items = []
    seen_ids = set()

    # Buscar pelo nome do contato usando search_events (busca em todos os calendários)
    contact_name = contact["nome"]
    name_parts = contact_name.split()
    search_term = name_parts[0] if name_parts else contact_name

    search_result = await calendar.search_events(
        access_token=access_token,
        query=search_term,
        max_results=50
    )

    # Filtrar APENAS eventos onde o email do contato está nos participantes
    if "items" in search_result:
        for item in search_result.get("items", []):
            attendees = item.get("attendees", [])
            for attendee in attendees:
                if attendee.get("email", "").lower() in contact_emails:
                    if item.get("id") not in seen_ids:
                        unique_items.append(item)
                        seen_ids.add(item.get("id"))
                    break

    # Formatar eventos
    events = []
    for item in unique_items:
        start_info = item.get("start", {})
        end_info = item.get("end", {})

        is_all_day = "date" in start_info and "dateTime" not in start_info

        if is_all_day:
            event_start = start_info.get("date") + "T00:00:00"
            event_end = end_info.get("date") + "T23:59:59"
        else:
            event_start = start_info.get("dateTime", "")
            event_end = end_info.get("dateTime", "")

        events.append({
            "id": item.get("id"),
            "summary": item.get("summary", "Sem titulo"),
            "description": item.get("description"),
            "location": item.get("location"),
            "start_datetime": event_start,
            "end_datetime": event_end,
            "is_all_day": is_all_day,
            "status": item.get("status"),
            "html_link": item.get("htmlLink")
        })

    # Ordenar por data (mais recentes primeiro para passadas, próximas primeiro para futuras)
    events.sort(key=lambda x: x.get("start_datetime", ""), reverse=True)

    return {"events": events, "total": len(events)}


@app.get("/api/prospects/{prospect_id}/calendar")
def get_prospect_calendar_events(request: Request, prospect_id: int, limit: int = 20):
    """Lista eventos de um prospect"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_events()
    events = service.get_events_for_prospect(prospect_id, limit=limit)
    return {"events": events, "total": len(events)}


@app.post("/api/calendar/sync")
async def trigger_calendar_sync(request: Request):
    """Dispara sincronizacao manual do calendario"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Buscar conta Google
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM google_accounts WHERE conectado = TRUE LIMIT 1")
        account = cursor.fetchone()

    if not account:
        raise HTTPException(status_code=400, detail="Nenhuma conta Google configurada")

    sync = get_calendar_sync()
    stats = await sync.incremental_sync(account["email"])

    return {"status": "completed", "stats": stats}


@app.post("/api/calendar/sync/full")
async def trigger_full_calendar_sync(request: Request):
    """Dispara sincronizacao completa do calendario"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Buscar conta Google
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM google_accounts WHERE conectado = TRUE LIMIT 1")
        account = cursor.fetchone()

    if not account:
        raise HTTPException(status_code=400, detail="Nenhuma conta Google configurada")

    sync = get_calendar_sync()
    stats = await sync.full_sync(account["email"])

    return {"status": "completed", "stats": stats}


@app.get("/api/calendar/sync/status")
def get_calendar_sync_status(request: Request):
    """Retorna status da sincronizacao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    sync = get_calendar_sync()
    return sync.get_sync_status()


@app.get("/api/calendar/stats")
def get_calendar_stats(request: Request, days: int = 30):
    """Retorna estatisticas do calendario"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_events()
    return service.get_events_count(days=days)


# =========================================================================
# CALENDAR AI ENDPOINTS
# =========================================================================

from services.calendar_ai import get_calendar_ai


@app.get("/api/ai/calendar-suggestions")
def list_calendar_suggestions(request: Request, limit: int = 20):
    """Lista sugestoes de reuniao da AI"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_ai()
    suggestions = service.get_calendar_suggestions(limit=limit)
    return {"suggestions": suggestions, "total": len(suggestions)}


@app.post("/api/ai/calendar-suggestions/generate")
def generate_calendar_suggestions(request: Request, limit: int = 10):
    """Gera novas sugestoes de reuniao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_ai()
    suggestions = service.generate_calendar_suggestions(limit=limit)
    return {"generated": len(suggestions), "suggestions": suggestions}


@app.post("/api/ai/calendar-suggestions/{suggestion_id}/accept")
async def accept_calendar_suggestion(
    request: Request,
    suggestion_id: int,
    custom_datetime: str = None,
    duration_minutes: int = None
):
    """Aceita sugestao e cria evento"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_ai()

    dt = None
    if custom_datetime:
        dt = datetime.fromisoformat(custom_datetime)

    result = await service.accept_and_create_event(
        suggestion_id=suggestion_id,
        custom_datetime=dt,
        duration_minutes=duration_minutes
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@app.post("/api/ai/calendar-suggestions/{suggestion_id}/dismiss")
async def dismiss_calendar_suggestion(
    request: Request,
    suggestion_id: int,
    motivo: str = None
):
    """Descarta sugestao de reuniao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_ai()
    success = service.dismiss_suggestion(suggestion_id, motivo)

    if not success:
        raise HTTPException(status_code=404, detail="Sugestao nao encontrada ou ja processada")

    return {"dismissed": True, "suggestion_id": suggestion_id}


@app.get("/api/ai/calendar-suggestions/stats")
def get_calendar_suggestions_stats(request: Request):
    """Retorna estatisticas das sugestoes de calendario"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_ai()
    return service.get_suggestion_stats()


@app.get("/api/ai/contacts-needing-meeting")
def get_contacts_needing_meeting(request: Request, limit: int = 20):
    """Lista contatos que precisam de reuniao"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_calendar_ai()
    contacts = service.get_contacts_needing_meeting(limit=limit)
    return {"contacts": contacts, "total": len(contacts)}


# =============================================================================
# BRIEFING ACTIONS API - Quick actions from the morning briefing
# Implemented by: INTEL (2026-03-28)
# =============================================================================

class BriefingTaskCreate(BaseModel):
    """Create task from briefing"""
    contact_id: int
    title: Optional[str] = None  # Auto-generated if not provided
    notes: Optional[str] = None
    due_date: Optional[str] = None  # ISO date string
    action_type: str = "followup"  # followup, birthday, reconnect


class BriefingMeetingCreate(BaseModel):
    """Schedule meeting from briefing"""
    contact_id: int
    title: Optional[str] = None  # Auto-generated if not provided
    date: str  # ISO date string (YYYY-MM-DD)
    time: str = "10:00"  # HH:MM
    duration_minutes: int = 30
    create_meet: bool = True
    notes: Optional[str] = None


class BriefingMessageDraft(BaseModel):
    """Draft message for contact"""
    contact_id: int
    channel: str = "email"  # email, whatsapp
    context: str = "followup"  # followup, birthday, reconnect, custom
    custom_prompt: Optional[str] = None


@app.post("/api/briefing/create-task")
async def briefing_create_task(request: Request, data: BriefingTaskCreate):
    """
    Cria tarefa rapida a partir do briefing.
    Auto-gera titulo baseado no tipo de acao e dados do contato.
    Salva localmente E sincroniza com Google Tasks imediatamente.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Get contact info
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, empresa, cargo, aniversario
            FROM contacts WHERE id = %s
        """, (data.contact_id,))
        contact = cursor.fetchone()

    if not contact:
        raise HTTPException(status_code=404, detail="Contato nao encontrado")

    # Auto-generate title if not provided
    title = data.title
    if not title:
        action_titles = {
            "followup": f"Follow-up com {contact['nome']}",
            "birthday": f"Parabenizar {contact['nome']} - Aniversario",
            "reconnect": f"Reconectar com {contact['nome']}",
        }
        title = action_titles.get(data.action_type, f"Contatar {contact['nome']}")

    # Auto-generate notes if not provided
    notes = data.notes
    if not notes:
        empresa_info = f" - {contact['empresa']}" if contact.get('empresa') else ""
        cargo_info = f" ({contact['cargo']})" if contact.get('cargo') else ""
        notes = f"Contato: {contact['nome']}{cargo_info}{empresa_info}\nCriado via Briefing RAP"

    # Parse due date
    due_datetime = None
    if data.due_date:
        try:
            due_datetime = datetime.fromisoformat(data.due_date.replace("Z", "+00:00"))
        except:
            pass

    # Use sync service to create locally AND push to Google
    tasks_service = get_tasks_sync_service()
    result = await tasks_service.create_task(
        titulo=title,
        descricao=notes,
        data_vencimento=due_datetime,
        contact_id=data.contact_id,
        sync_to_google=True
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=f"Erro ao criar tarefa: {result['error']}")

    return {
        "status": "success",
        "task_id": result.get("id"),
        "contact_name": contact['nome'],
        "action_type": data.action_type,
        "synced_to_google": True
    }


@app.post("/api/briefing/schedule-meeting")
async def briefing_schedule_meeting(request: Request, data: BriefingMeetingCreate):
    """
    Agenda reuniao rapida a partir do briefing.
    Cria evento no Google Calendar com link do Meet.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Get contact info
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, empresa, cargo, emails
            FROM contacts WHERE id = %s
        """, (data.contact_id,))
        contact = cursor.fetchone()

    if not contact:
        raise HTTPException(status_code=404, detail="Contato nao encontrado")

    # Auto-generate title
    title = data.title or f"Reuniao com {contact['nome']}"

    # Parse datetime
    try:
        date_obj = datetime.strptime(data.date, "%Y-%m-%d")
        time_parts = data.time.split(":")
        start_datetime = date_obj.replace(hour=int(time_parts[0]), minute=int(time_parts[1]))
        end_datetime = start_datetime + timedelta(minutes=data.duration_minutes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Formato de data/hora invalido: {e}")

    # Get attendee email
    attendees = []
    emails = contact.get("emails") or []
    if isinstance(emails, str):
        import json
        try:
            emails = json.loads(emails)
        except:
            emails = []
    if emails:
        primary_email = next((e.get("email") for e in emails if e.get("primary")), None)
        if not primary_email and emails:
            primary_email = emails[0].get("email") if isinstance(emails[0], dict) else emails[0]
        if primary_email:
            attendees.append(primary_email)

    # Build description
    description = data.notes or ""
    if contact.get("empresa"):
        description = f"Contato: {contact['nome']} - {contact['empresa']}\n\n{description}"
    description += "\n\nAgendado via Briefing RAP"

    # Create calendar event
    service = get_calendar_events()
    event = service.create_event(
        summary=title,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        description=description,
        contact_id=data.contact_id,
        attendees=attendees if attendees else None,
        create_in_google=True
    )

    return {
        "status": "success",
        "event": event,
        "contact_name": contact['nome'],
        "start": start_datetime.isoformat(),
        "end": end_datetime.isoformat()
    }


@app.post("/api/briefing/draft-message")
async def briefing_draft_message(request: Request, data: BriefingMessageDraft):
    """
    Gera rascunho de mensagem (email ou WhatsApp) para o contato.
    Usa IA para personalizar baseado no contexto e historico.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    # Get contact info with context
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.id, c.nome, c.empresa, c.cargo, c.emails, c.telefones,
                   c.aniversario, c.circulo, c.ultimo_contato, c.health_score,
                   c.resumo_ai
            FROM contacts c WHERE c.id = %s
        """, (data.contact_id,))
        contact = cursor.fetchone()

        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")

        # Get recent interactions
        cursor.execute("""
            SELECT canal, assunto, ultimo_mensagem
            FROM conversations
            WHERE contact_id = %s
            ORDER BY ultimo_mensagem DESC
            LIMIT 3
        """, (data.contact_id,))
        recent_convs = cursor.fetchall()

    # Build context for AI
    nome = contact["nome"].split()[0]  # First name
    empresa = contact.get("empresa") or ""
    cargo = contact.get("cargo") or ""
    circulo = contact.get("circulo") or 5
    dias_sem_contato = 0
    if contact.get("ultimo_contato"):
        dias_sem_contato = (datetime.now() - contact["ultimo_contato"]).days

    # Buscar briefing atual do contato para enriquecer contexto
    current_briefing = get_current_briefing(data.contact_id)
    briefing_context = ""
    briefing_id = None
    hook_suggestion = ""
    opportunities_text = ""

    if current_briefing:
        briefing_id = current_briefing.get("id")
        parts = []

        # Usar o conteudo completo do briefing
        briefing_content = current_briefing.get("content", "")

        # Extrair secao de oportunidades do briefing completo
        if briefing_content:
            import re
            # Procurar secao de oportunidades no texto completo
            oport_match = re.search(
                r'(?:##\s*\d*\.?\s*)?OPORTUNIDADES?\s*\n(.*?)(?=\n##|\n\*\*[A-Z]|\Z)',
                briefing_content, re.DOTALL | re.IGNORECASE
            )
            if oport_match:
                opportunities_text = oport_match.group(1).strip()[:800]

            # Procurar sugestoes de pauta
            pauta_match = re.search(
                r'(?:##\s*\d*\.?\s*)?SUGEST[ÕO]ES?\s*(?:DE PAUTA)?\s*\n(.*?)(?=\n##|\n\*\*[A-Z]|\Z)',
                briefing_content, re.DOTALL | re.IGNORECASE
            )
            if pauta_match:
                pauta_text = pauta_match.group(1).strip()[:500]
                if pauta_text:
                    parts.append(f"Sugestoes de pauta:\n{pauta_text}")

            # Extrair primeira sugestao concreta como hook
            hook_match = re.search(
                r'(?:Propor|Convidar|Agendar|Levar|Criar)[^\n.!?]*',
                briefing_content, re.IGNORECASE
            )
            if hook_match:
                hook_suggestion = hook_match.group(0).strip()[:150]

        # Adicionar resumo do briefing
        if current_briefing.get("summary"):
            parts.append(f"Contexto: {current_briefing['summary']}")

        # Usar dados estruturados se disponiveis
        opportunities = current_briefing.get("opportunities") or []
        if opportunities:
            parts.append(f"Oportunidades: {'; '.join(opportunities[:3])}")
            if not hook_suggestion:
                hook_suggestion = opportunities[0]

        talking_points = current_briefing.get("talking_points") or []
        if talking_points and not hook_suggestion:
            hook_suggestion = talking_points[0]

        if parts:
            briefing_context = "\n- ".join([""] + parts)

    # Context templates com gancho especifico
    context_prompts = {
        "followup": f"Escreva uma mensagem de follow-up para {nome} com um MOTIVO ESPECIFICO para o contato.",
        "birthday": f"Escreva uma mensagem de aniversario sincera e calorosa para {nome}. Seja genuino e evite cliches.",
        "reconnect": f"Escreva uma mensagem para reconectar com {nome} apos {dias_sem_contato} dias sem contato. Inclua um MOTIVO CONCRETO para retomar o contato.",
        "custom": data.custom_prompt or f"Escreva uma mensagem para {nome}."
    }

    context_text = context_prompts.get(data.context, context_prompts["followup"])

    # Add contact details
    prompt = f"""
{context_text}

INFORMACOES DO CONTATO:
- Nome completo: {contact['nome']}
- Empresa: {empresa}
- Cargo: {cargo}
- Proximidade: Circulo {circulo} (1=muito proximo, 5=distante)
- Dias desde ultimo contato: {dias_sem_contato}
{f"- Relacionamento: {contact['resumo_ai'][:300]}" if contact.get('resumo_ai') else ""}{briefing_context}

{f'''OPORTUNIDADES IDENTIFICADAS NO BRIEFING:
{opportunities_text}
''' if opportunities_text else ""}

{f"USE ESTE GANCHO NA MENSAGEM: {hook_suggestion}" if hook_suggestion else ""}

CANAL: {data.channel.upper()}
{'''REGRAS PARA WHATSAPP:
- Maximo 2-3 frases curtas e diretas
- OBRIGATORIO: Mencione uma oportunidade especifica do briefing acima (ex: "levar metodologia ImensIAH", "parceria estrategica", "mentoria cruzada")
- Proponha acao clara: cafe, call, ou encontro no proximo conselho
- Maximo 1 emoji
- Tom casual mas profissional
- NAO use frases genericas como "trocar ideias" sem especificar o que''' if data.channel == "whatsapp" else "(Email pode ser mais elaborado)"}

IMPORTANTE: A mensagem DEVE mencionar uma OPORTUNIDADE ESPECIFICA do briefing. Seja direto sobre o que voce quer propor.

Responda APENAS com a mensagem, sem explicacoes.
"""

    # Call Claude API
    agent = get_ai_agent()
    message_draft = await agent.call_claude(prompt, max_tokens=500)

    if not message_draft:
        # Fallback templates
        fallbacks = {
            "followup": f"Ola {nome}, espero que esteja bem! Gostaria de retomar nosso contato. Podemos marcar uma conversa?",
            "birthday": f"Feliz aniversario, {nome}! Desejo um dia especial e um ano cheio de realizacoes.",
            "reconnect": f"Ola {nome}, faz tempo que nao conversamos! Como voce esta? Gostaria de saber das novidades.",
        }
        message_draft = fallbacks.get(data.context, f"Ola {nome}, tudo bem?")

    # Get contact info for sending
    contact_info = {}
    emails = contact.get("emails") or []
    if isinstance(emails, str):
        import json
        try:
            emails = json.loads(emails)
        except:
            emails = []
    if emails:
        contact_info["email"] = next((e.get("email") for e in emails if e.get("primary")), emails[0].get("email") if isinstance(emails[0], dict) else emails[0])

    telefones = contact.get("telefones") or []
    if isinstance(telefones, str):
        import json
        try:
            telefones = json.loads(telefones)
        except:
            telefones = []
    if telefones:
        contact_info["phone"] = next((t.get("number") for t in telefones if t.get("whatsapp")), telefones[0].get("number") if isinstance(telefones[0], dict) else telefones[0])

    # Registrar acao no briefing se existir
    if briefing_id:
        record_briefing_action(briefing_id, {
            "type": f"draft_{data.channel}",
            "context": data.context,
            "timestamp": datetime.now().isoformat()
        })

    return {
        "status": "success",
        "channel": data.channel,
        "context": data.context,
        "contact_name": contact["nome"],
        "contact_info": contact_info,
        "draft": message_draft.strip(),
        "briefing_used": briefing_id is not None,
        "briefing_id": briefing_id
    }


@app.get("/api/briefing/quick-actions/{contact_id}")
async def get_briefing_quick_actions(request: Request, contact_id: int):
    """
    Retorna acoes rapidas disponiveis para um contato no briefing.
    Inclui sugestoes contextuais baseadas no estado do relacionamento.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, empresa, cargo, emails, telefones,
                   aniversario, circulo, ultimo_contato, health_score
            FROM contacts WHERE id = %s
        """, (contact_id,))
        contact = cursor.fetchone()

    if not contact:
        raise HTTPException(status_code=404, detail="Contato nao encontrado")

    actions = []

    # Check if birthday is today or soon
    if contact.get("aniversario"):
        aniv = contact["aniversario"]
        today = datetime.now().date()
        this_year_birthday = aniv.replace(year=today.year)
        days_until = (this_year_birthday - today).days
        if days_until < 0:
            this_year_birthday = aniv.replace(year=today.year + 1)
            days_until = (this_year_birthday - today).days

        if days_until == 0:
            actions.append({
                "type": "birthday",
                "label": "Enviar parabens",
                "icon": "bi-cake2",
                "priority": 10,
                "reason": "Aniversario HOJE!"
            })
        elif days_until <= 7:
            actions.append({
                "type": "birthday",
                "label": f"Preparar parabens ({days_until} dias)",
                "icon": "bi-cake2",
                "priority": 7,
                "reason": f"Aniversario em {days_until} dias"
            })

    # Check days since last contact
    dias_sem_contato = 0
    if contact.get("ultimo_contato"):
        dias_sem_contato = (datetime.now() - contact["ultimo_contato"]).days

    circulo = contact.get("circulo") or 5
    needs_reconnect = (circulo <= 2 and dias_sem_contato > 30) or (circulo == 3 and dias_sem_contato > 60)

    if needs_reconnect:
        actions.append({
            "type": "reconnect",
            "label": "Reconectar",
            "icon": "bi-arrow-repeat",
            "priority": 8,
            "reason": f"{dias_sem_contato} dias sem contato"
        })

    # Check health score
    health = contact.get("health_score") or 50
    if health < 40:
        actions.append({
            "type": "followup",
            "label": "Follow-up urgente",
            "icon": "bi-exclamation-triangle",
            "priority": 9,
            "reason": f"Health {health}% - relacionamento em risco"
        })

    # Standard actions always available
    actions.append({
        "type": "task",
        "label": "Criar tarefa",
        "icon": "bi-check2-square",
        "priority": 3
    })

    actions.append({
        "type": "meeting",
        "label": "Agendar reuniao",
        "icon": "bi-calendar-plus",
        "priority": 4
    })

    # Check available channels
    emails = contact.get("emails") or []
    telefones = contact.get("telefones") or []
    if isinstance(emails, str):
        import json
        try:
            emails = json.loads(emails)
        except:
            emails = []
    if isinstance(telefones, str):
        import json
        try:
            telefones = json.loads(telefones)
        except:
            telefones = []

    if emails:
        actions.append({
            "type": "email",
            "label": "Enviar email",
            "icon": "bi-envelope",
            "priority": 5
        })

    if telefones:
        has_whatsapp = any(t.get("whatsapp") for t in telefones if isinstance(t, dict))
        actions.append({
            "type": "whatsapp",
            "label": "Enviar WhatsApp",
            "icon": "bi-whatsapp",
            "priority": 5 if has_whatsapp else 6
        })

    # Sort by priority (higher first)
    actions.sort(key=lambda x: x.get("priority", 0), reverse=True)

    return {
        "contact_id": contact_id,
        "contact_name": contact["nome"],
        "actions": actions,
        "context": {
            "circulo": circulo,
            "dias_sem_contato": dias_sem_contato,
            "health_score": health
        }
    }


# =========================================================================
# PROJECTS ENDPOINTS - Sistema de Projetos
# =========================================================================

@app.get("/api/projects")
async def api_list_projects(
    tipo: str = None,
    status: str = None,
    limit: int = 50,
    offset: int = 0,
    include_completed: bool = False
):
    """Lista projetos com filtros opcionais e dados de urgencia."""
    return {
        "projects": list_projects(tipo=tipo, status=status, limit=limit, offset=offset, include_completed=include_completed),
        "types": PROJECT_TYPES,
        "statuses": PROJECT_STATUS
    }


@app.get("/api/projects/stats")
async def api_projects_stats():
    """Retorna estatisticas dos projetos."""
    return get_projects_stats()


@app.get("/api/projects/active")
async def api_active_projects(limit: int = 5):
    """Retorna resumo dos projetos ativos para dashboard."""
    return {
        "projects": get_active_projects_summary(limit=limit)
    }


@app.get("/api/projects/active-summary")
async def api_active_projects_summary(limit: int = 5):
    """Retorna array de projetos ativos para widget do dashboard."""
    return get_active_projects_summary(limit=limit)


@app.get("/api/tasks/standalone-pending")
async def api_standalone_pending_tasks():
    """Conta tarefas pendentes avulsas (exclui campanhas e Google Tasks)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) as count FROM tasks
            WHERE status = 'pending'
              AND project_id IS NULL
              AND COALESCE(origem, '') != 'campaign'
              AND google_task_id IS NOT NULL
        """)
        return {"count": cursor.fetchone()['count']}


@app.get("/tarefas-pendentes", response_class=HTMLResponse)
async def tarefas_pendentes_page(request: Request):
    """Página de tarefas avulsas (sem projeto)"""
    return templates.TemplateResponse("rap_tarefas_avulsas.html", {"request": request})


@app.get("/api/projects/all-tasks")
async def api_all_project_tasks(status: str = "pending", limit: int = 10):
    """Lista tarefas de todos os projetos"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.id, t.titulo, t.descricao, t.status, t.data_vencimento, t.prioridade,
                   p.nome as project_name, c.nome as responsavel
            FROM tasks t
            LEFT JOIN projects p ON p.id = t.project_id
            LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.status = %s
            ORDER BY t.data_vencimento ASC NULLS LAST
            LIMIT %s
        """, (status, limit))
        tasks = [dict(r) for r in cursor.fetchall()]
    return {"tasks": tasks}


@app.get("/api/projects/overdue-count")
async def api_projects_overdue_count():
    """Conta projetos com tarefas vencidas (consistente com /projetos)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(DISTINCT t.project_id) as count
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE t.status = 'pending'
              AND t.data_vencimento::date < CURRENT_DATE
              AND p.status = 'ativo'
        """)
        return {"count": cursor.fetchone()['count']}


@app.get("/api/projects/{project_id}")
async def api_get_project(project_id: int):
    """Retorna projeto com todos os detalhes."""
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Projeto nao encontrado")
    return project


@app.get("/api/projects/{project_id}/briefing")
async def api_project_briefing(request: Request, project_id: int):
    """AI-generated briefing: why this project needs attention, recent activity, next actions."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from services.projects import get_project_briefing_context
    import httpx as _httpx

    ctx = get_project_briefing_context(project_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Projeto nao encontrado")

    today = datetime.now().strftime("%Y-%m-%d (%A)")

    # Build context for Claude
    tasks_text = ""
    overdue = []
    upcoming = []
    for t in ctx['tasks']:
        status_emoji = "pending" if t['status'] == 'pending' else t['status']
        prazo = str(t['data_vencimento'].date()) if t.get('data_vencimento') else "sem prazo"
        resp = t.get('responsavel') or 'sem responsavel'
        line = f"- [{status_emoji}] {t['titulo']} | prazo: {prazo} | resp: {resp}"
        tasks_text += line + "\n"
        if t.get('data_vencimento') and t['status'] == 'pending':
            if t['data_vencimento'].date() < datetime.now().date():
                overdue.append(t)
            elif (t['data_vencimento'].date() - datetime.now().date()).days <= 7:
                upcoming.append(t)

    members_text = "\n".join(
        f"- {m['nome']} (papel: {m.get('papel') or '-'})"
        for m in ctx['members']
    ) or "(nenhum membro)"

    msgs_text = ""
    for m in ctx['recent_messages'][:15]:
        dt = (m.get('enviado_em') or m.get('recebido_em'))
        dt_str = dt.strftime("%d/%m %H:%M") if dt else "?"
        direction = "enviada" if m['direcao'] == 'outgoing' else "recebida"
        content = (m.get('conteudo') or '')[:500].replace('\n', ' ')
        if m['direcao'] == 'outgoing':
            who = f"RENATO escreveu para {m['contact_nome']}"
        else:
            who = f"{m['contact_nome']} escreveu para RENATO"
        msgs_text += f"- [{m['canal']}] {dt_str} {who}: {content}\n"
    msgs_text = msgs_text or "(nenhuma mensagem recente dos membros)"

    notes_text = ""
    for n in ctx['notes'][:3]:
        dt_str = n['criado_em'].strftime("%d/%m") if n.get('criado_em') else "?"
        notes_text += f"- [{dt_str}] {n.get('titulo') or 'Nota'}: {(n.get('conteudo') or '')[:100]}\n"

    events_text = ""
    for e in ctx['events']:
        dt_str = e['start_datetime'].strftime("%d/%m %H:%M") if e.get('start_datetime') else "?"
        events_text += f"- {dt_str}: {e.get('summary', '?')}\n"

    milestones_text = ""
    for ms in ctx['milestones']:
        st = "concluido" if ms.get('data_conclusao') else ms.get('status', 'pendente')
        dt_str = str(ms['data_prevista']) if ms.get('data_prevista') else "sem data"
        milestones_text += f"- [{st}] {ms['titulo']} | previsto: {dt_str}\n"

    prompt = f"""Voce e o assistente inteligente do CRM pessoal do Renato. Ele acabou de abrir o projeto abaixo.
Gere um BRIEFING conciso e acionavel.

HOJE: {today}

PROJETO: {ctx['project']['nome']}
Tipo: {ctx['project']['tipo']} | Status: {ctx['project']['status']}
Descricao: {ctx['project'].get('descricao') or '(sem descricao)'}
Previsao: {ctx['project'].get('data_previsao') or 'sem previsao'}

MEMBROS:
{members_text}

TAREFAS:
{tasks_text or '(nenhuma tarefa)'}

MARCOS:
{milestones_text or '(nenhum marco)'}

MENSAGENS RECENTES (WhatsApp/Email dos membros, ultimos 30 dias):
{msgs_text}

NOTAS/TIMELINE:
{notes_text or '(nenhuma nota)'}

EVENTOS/CALENDARIO:
{events_text or '(nenhum evento)'}

## INSTRUCOES

- "enviada" = mensagem de Renato (outgoing). "recebida" = mensagem do contato (incoming).
- NAO diga que uma mensagem "parece incompleta" a menos que ela termine abruptamente no meio de uma frase.
- Se Renato respondeu uma mensagem recebida, a conversa esta em andamento, nao pendente.
- Converta datas relativas mencionadas para datas absolutas (ex: "primeira semana de maio" = 2026-05-05).

Retorne um JSON valido com este formato (SEM markdown, SEM ```):

{{
  "status_geral": "no_caminho" | "atencao" | "critico" | "parado",
  "resumo": "1-2 frases dizendo a situacao atual do projeto",
  "alertas": [
    "frase curta de cada problema que precisa atencao (tarefas vencidas, mensagens sem resposta, etc)"
  ],
  "atividade_recente": [
    "frase curta descrevendo cada atividade relevante recente (msg recebida, tarefa concluida, etc)"
  ],
  "proximas_acoes": [
    "acao concreta que Renato deve tomar AGORA, em ordem de prioridade"
  ],
  "destaque_comunicacao": "resumo de 1 frase sobre as comunicacoes recentes com membros (ou null se nenhuma)"
}}

REGRAS:
- Maximo 3 alertas, 4 atividades, 3 proximas_acoes
- Seja DIRETO e ACIONAVEL. Nao seja generico.
- Se uma tarefa esta vencida, diga qual e de quem.
- Se ha mensagem recebida sem resposta, diga de quem e sobre o que.
- proximas_acoes deve ter verbos de acao: "Cobrar Kesley sobre X", "Responder Ana sobre Y"
- Use portugues brasileiro
"""

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return {"error": "ANTHROPIC_API_KEY nao configurada"}

    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                return {"error": f"Claude API error: {resp.status_code}"}

            import json as _json
            text = resp.json()["content"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("```", 2)[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            return _json.loads(text)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/projects")
async def api_create_project(request: Request):
    """Cria novo projeto."""
    data = await request.json()
    if not data.get('nome'):
        raise HTTPException(status_code=400, detail="Nome e obrigatorio")
    project = create_project(data)
    return {"status": "success", "project": project}


@app.post("/api/projects/enrich")
async def api_enrich_project(request: Request):
    """
    Enriquece descrição de projeto usando IA.
    Busca emails, WhatsApp e informações públicas para sugerir campos.
    """
    from services.project_enrichment import enrich_project_from_description

    data = await request.json()
    descricao = data.get('descricao', '')

    if not descricao or len(descricao.strip()) < 10:
        raise HTTPException(
            status_code=400,
            detail="Forneça uma descrição com pelo menos 10 caracteres"
        )

    result = await enrich_project_from_description(descricao)

    if result.get('status') == 'error':
        raise HTTPException(status_code=500, detail=result.get('error', 'Erro ao enriquecer'))

    return result


@app.put("/api/projects/{project_id}")
async def api_update_project(project_id: int, request: Request):
    """Atualiza projeto existente."""
    data = await request.json()
    project = update_project(project_id, data)
    if not project:
        raise HTTPException(status_code=404, detail="Projeto nao encontrado")
    return {"status": "success", "project": project}


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: int):
    """Deleta projeto."""
    if delete_project(project_id):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Projeto nao encontrado")


# ============== PROJECT MEMBERS ==============

@app.post("/api/projects/{project_id}/members")
async def api_add_project_member(project_id: int, request: Request):
    """Adiciona membro ao projeto."""
    data = await request.json()
    contact_id = data.get('contact_id')
    if not contact_id:
        raise HTTPException(status_code=400, detail="contact_id e obrigatorio")

    member = add_project_member(project_id, contact_id, data.get('papel'))
    if not member:
        raise HTTPException(status_code=400, detail="Erro ao adicionar membro")
    return {"status": "success", "member": member}


@app.delete("/api/projects/{project_id}/members/{contact_id}")
async def api_remove_project_member(project_id: int, contact_id: int):
    """Remove membro do projeto."""
    if remove_project_member(project_id, contact_id):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Membro nao encontrado")


@app.get("/api/contacts/{contact_id}/projects")
async def api_contact_projects(contact_id: int):
    """Retorna projetos que o contato participa."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.nome, p.tipo, p.status, p.descricao, pm.papel,
                   (SELECT COUNT(*) FROM project_members WHERE project_id = p.id) as total_membros,
                   (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'pending') as tasks_pendentes
            FROM projects p
            JOIN project_members pm ON pm.project_id = p.id
            WHERE pm.contact_id = %s
            ORDER BY p.status = 'ativo' DESC, p.nome
        """, (contact_id,))
        projects = [dict(row) for row in cursor.fetchall()]
        return {"projects": projects}


@app.get("/api/contacts/{contact_id}/tasks")
async def api_contact_tasks(contact_id: int):
    """Retorna tarefas vinculadas ao contato."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.*, p.nome as project_nome
            FROM tasks t
            LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.contact_id = %s
            ORDER BY t.status = 'pending' DESC, t.prioridade ASC, t.data_vencimento ASC NULLS LAST
            LIMIT 20
        """, (contact_id,))
        tasks = [dict(row) for row in cursor.fetchall()]
        return {"tasks": tasks}


@app.get("/api/contacts/{contact_id}/messages")
async def api_contact_messages(contact_id: int, limit: int = 5):
    """Retorna mensagens recentes do contato (WhatsApp + Email)."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Quick query with index-friendly conditions
            cursor.execute("""
                SELECT m.id, m.conteudo, m.direcao, m.enviado_em, c.canal
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE m.contact_id = %s
                ORDER BY m.id DESC
                LIMIT %s
            """, (contact_id, limit))
            messages = [dict(row) for row in cursor.fetchall()]

            return {"messages": messages}
    except Exception as e:
        # Table might not exist or other error - return empty
        return {"messages": []}


@app.get("/api/projects/available")
async def api_available_projects():
    """Retorna lista de projetos ativos para selecao."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, tipo, status
            FROM projects
            WHERE status = 'ativo'
            ORDER BY nome
        """)
        return {"projects": [dict(row) for row in cursor.fetchall()]}


# ============== PROJECT MILESTONES ==============

@app.post("/api/projects/{project_id}/milestones")
async def api_add_milestone(project_id: int, request: Request):
    """Adiciona marco ao projeto."""
    data = await request.json()
    if not data.get('titulo'):
        raise HTTPException(status_code=400, detail="titulo e obrigatorio")

    milestone = add_milestone(project_id, data)
    return {"status": "success", "milestone": milestone}


@app.put("/api/milestones/{milestone_id}")
async def api_update_milestone(milestone_id: int, request: Request):
    """Atualiza marco."""
    data = await request.json()
    milestone = update_milestone(milestone_id, data)
    if not milestone:
        raise HTTPException(status_code=404, detail="Marco nao encontrado")
    return {"status": "success", "milestone": milestone}


@app.delete("/api/milestones/{milestone_id}")
async def api_delete_milestone(milestone_id: int):
    """Deleta marco."""
    if delete_milestone(milestone_id):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Marco nao encontrado")


# ============== SMART PROJECT UPDATE ==============

@app.post("/api/projects/{project_id}/smart-update")
async def api_smart_update(project_id: int):
    """Analisa emails/WhatsApp dos membros e sugere atualizacoes de tarefas"""
    from services.project_smart_update import analyze_project_updates
    result = await analyze_project_updates(project_id)
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result['error'])
    return result


@app.post("/api/projects/{project_id}/smart-update/apply")
async def api_smart_update_apply(project_id: int, request: Request):
    """Aplica sugestoes: marca tarefas como concluidas"""
    from services.project_smart_update import apply_smart_updates
    data = await request.json()
    task_ids = data.get('task_ids', [])
    new_tasks = data.get('new_tasks', [])
    if not task_ids and not new_tasks:
        raise HTTPException(status_code=400, detail="Nenhuma acao selecionada")
    result = await apply_smart_updates(project_id, task_ids, new_tasks)
    return result


# ============== PROJECT TASK RESEARCH ==============

@app.post("/api/projects/{project_id}/research-task")
async def api_research_task(project_id: int, request: Request):
    """Pesquisa com IA sobre o tema de uma tarefa e salva como nota do projeto."""
    import httpx
    from services.projects import get_project, add_project_note

    data = await request.json()
    task_id = data.get('task_id')
    query = data.get('query', '')

    if not query:
        raise HTTPException(status_code=400, detail="query obrigatoria")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY nao configurada")

    # Get project context
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Projeto nao encontrado")

    # Build research prompt
    prompt = f"""Voce e um assistente de pesquisa. Faca uma pesquisa detalhada sobre o seguinte tema,
no contexto do projeto "{project['nome']}":

Tarefa de pesquisa: {query}

Descricao do projeto: {project.get('descricao', 'N/A')}

Instrucoes:
- Faca uma pesquisa abrangente sobre o tema
- Inclua: o que e, principais programas/atividades, atuacao no Brasil (se aplicavel), oportunidades de parceria
- Formate em Markdown com secoes claras
- Seja objetivo e pratico, focando em informacoes acionaveis
- Inclua links ou referencias quando possivel
- Maximo 800 palavras"""

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )

        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Erro API Claude: {response.status_code}")

        result = response.json()
        research_text = result.get("content", [{}])[0].get("text", "")

        if not research_text:
            raise HTTPException(status_code=500, detail="Pesquisa retornou vazio")

        # Save as project note
        note = add_project_note(project_id, {
            'tipo': 'pesquisa',
            'titulo': f'Pesquisa IA: {query}',
            'conteudo': research_text,
            'autor': 'IA'
        })

        # Mark task as completed if task_id provided
        if task_id:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE tasks SET status = 'completed', descricao = COALESCE(descricao, '') || %s
                    WHERE id = %s AND project_id = %s
                """, (f'\n\n--- Pesquisa realizada por IA em {datetime.now().strftime("%d/%m/%Y")} ---', task_id, project_id))
                conn.commit()

        return {"status": "success", "note": note, "research": research_text}

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout na pesquisa")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== PROJECT AI ANALYSIS ==============

@app.post("/api/projects/{project_id}/ai-analysis")
async def api_project_ai_analysis(project_id: int, request: Request):
    """Gera parecer IA sobre o projeto cruzando mensagens, grupos, docs e tarefas"""
    from services.project_smart_update import generate_project_analysis
    try:
        data = await request.json()
    except Exception:
        data = {}
    custom_prompt = data.get('prompt')
    result = await generate_project_analysis(project_id, custom_prompt)
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result['error'])
    return result


# ============== CONDENSE PARECERES ==============

@app.post("/api/projects/{project_id}/condense-analyses")
async def api_condense_analyses(project_id: int):
    """Condensa multiplos pareceres IA em um resumo executivo acumulativo"""
    import httpx as _hx
    from services.projects import get_project, add_project_note

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY nao configurada")

    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Projeto nao encontrado")

    # Get all pareceres
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, titulo, conteudo, criado_em FROM project_notes
            WHERE project_id = %s AND tipo = 'parecer'
            ORDER BY criado_em ASC
        """, (project_id,))
        pareceres = [dict(r) for r in cursor.fetchall()]

    if len(pareceres) < 3:
        raise HTTPException(status_code=400, detail="Minimo 3 pareceres para condensar")

    # Build prompt with all pareceres
    pareceres_text = "\n\n---\n\n".join([
        f"### {p['titulo']} ({str(p['criado_em'])[:10]})\n{p['conteudo']}"
        for p in pareceres
    ])

    prompt = f"""Voce e um consultor estrategico. Abaixo estao {len(pareceres)} pareceres de IA gerados ao longo do tempo sobre o projeto "{project['nome']}".

Sua tarefa: CONDENSAR todos em um unico RESUMO EXECUTIVO ACUMULATIVO.

PARECERES:
{pareceres_text}

INSTRUCOES:
1. **Evolucao do projeto**: como o projeto evoluiu ao longo dos pareceres
2. **Decisoes tomadas**: o que foi decidido e executado
3. **Situacao atual**: status consolidado
4. **Riscos e pendencias**: o que ainda precisa de atencao
5. **Recomendacoes**: proximos passos priorizados

- Maximo 600 palavras
- Portugues, objetivo, acionavel
- Formate em Markdown com secoes claras
- NAO repita informacoes redundantes entre pareceres
- Destaque MUDANCAS e TENDENCIAS ao longo do tempo"""

    try:
        async with _hx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]}
            )
        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Erro API: {resp.status_code}")

        summary = resp.json()["content"][0]["text"]

        # Save as resumo_executivo
        note = add_project_note(project_id, {
            'tipo': 'resumo_executivo',
            'titulo': f'Resumo Executivo: {project["nome"]} ({len(pareceres)} pareceres condensados)',
            'conteudo': summary,
            'autor': 'INTEL IA'
        })

        # Archive old pareceres (keep last 2, remove older ones)
        ids_to_keep = [p['id'] for p in pareceres[-2:]]
        ids_to_archive = [p['id'] for p in pareceres if p['id'] not in ids_to_keep]

        if ids_to_archive:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE project_notes SET tipo = 'parecer_arquivado'
                    WHERE id = ANY(%s)
                """, (ids_to_archive,))
                conn.commit()

        return {
            "status": "success",
            "note_id": note.get('id'),
            "pareceres_condensed": len(pareceres),
            "pareceres_archived": len(ids_to_archive),
            "summary_preview": summary[:300]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== DOWNLOAD GROUP DOCUMENTS ==============

@app.post("/api/projects/{project_id}/download-group-docs")
async def api_download_group_docs(project_id: int):
    """Baixa documentos dos grupos WhatsApp vinculados e salva no Google Drive"""
    from services.project_smart_update import download_group_documents
    result = await download_group_documents(project_id)
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result['error'])
    return result


# ============== SHARE PROJECT ANALYSIS ==============

@app.post("/api/projects/{project_id}/adapt-analysis")
async def api_adapt_analysis(project_id: int, request: Request):
    """Adapta o parecer para um destinatario especifico e canal"""
    import httpx as _hx

    data = await request.json()
    analysis = data.get('analysis', '')
    contact_name = data.get('contact_name', '')
    channel = data.get('channel', 'whatsapp')
    project_name = data.get('project_name', '')

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="API key nao configurada")

    if channel == 'whatsapp':
        instruction = f"""Reescreva este parecer como uma MENSAGEM DE WHATSAPP para {contact_name}.
Tom: informal, carinhoso, direto. Como um parente/amigo explicando.
SEM juridiquês. Máximo 15 linhas. 1-2 emojis no máximo.
NÃO peça para ler documentos jurídicos.
Foque em: o que aconteceu (2 frases), o que ELA precisa fazer (3 ações concretas), o que VOCÊ vai fazer.
Assine como Renato."""
    else:
        instruction = f"""Reescreva este parecer como um EMAIL para {contact_name}.
Tom: profissional mas acessível. Sem juridiquês excessivo.
Estruture em: Resumo da situação, Ações necessárias, Próximos passos, Prazos.
Assine como Renato de Faria e Almeida Prado."""

    prompt = f"""{instruction}

PARECER ORIGINAL (projeto: {project_name}):
{analysis[:3000]}"""

    try:
        async with _hx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 800,
                      "messages": [{"role": "user", "content": prompt}]}
            )
        if resp.status_code == 200:
            message = resp.json()["content"][0]["text"]
            return {"message": message, "channel": channel, "recipient": contact_name}
        raise HTTPException(status_code=resp.status_code, detail="Erro na API")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/projects/{project_id}/send-analysis")
async def api_send_analysis(project_id: int, request: Request):
    """Envia mensagem adaptada do parecer via WhatsApp ou Email"""
    data = await request.json()
    contact_id = data.get('contact_id')
    channel = data.get('channel', 'whatsapp')
    message = data.get('message', '')

    if not contact_id or not message:
        raise HTTPException(status_code=400, detail="contact_id e message obrigatorios")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome, telefones, emails FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")
        contact = dict(contact)

    if channel == 'whatsapp':
        # Enviar via WhatsApp
        telefones = contact.get('telefones', [])
        if isinstance(telefones, str):
            import json as _json
            telefones = _json.loads(telefones)
        phone = None
        for t in telefones:
            if t.get('whatsapp') or t.get('number'):
                phone = t.get('number', '')
                break
        if not phone and telefones:
            phone = telefones[0].get('number', '')

        if not phone:
            raise HTTPException(status_code=400, detail=f"Contato {contact['nome']} nao tem telefone")

        from integrations.whatsapp import WhatsAppIntegration
        wa = WhatsAppIntegration()
        result = await wa.send_text_message(phone, message)
        return {"status": "sent", "channel": "whatsapp", "to": phone, "result": result}

    elif channel == 'email':
        emails = contact.get('emails', [])
        if isinstance(emails, str):
            import json as _json
            emails = _json.loads(emails)
        email = None
        for e in emails:
            if e.get('email'):
                email = e['email']
                break
        if not email:
            raise HTTPException(status_code=400, detail=f"Contato {contact['nome']} nao tem email")

        from integrations.gmail import GmailIntegration
        from integrations.google_drive import get_valid_token as _get_token
        with get_db() as conn:
            token = await _get_token(conn, 'professional')
        if not token:
            raise HTTPException(status_code=401, detail="Token Gmail indisponivel")

        gmail = GmailIntegration()
        # Buscar nome do projeto para subject
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT nome FROM projects WHERE id = %s", (project_id,))
            proj = cursor.fetchone()
        subject = f"Atualização: {proj['nome']}" if proj else "Atualização de Projeto"

        result = await gmail.send_message(token, email, subject, message)
        return {"status": "sent", "channel": "email", "to": email, "result": result}

    raise HTTPException(status_code=400, detail="Canal invalido")


# ============== SOCIAL GROUPS ==============

@app.get("/grupos-sociais", response_class=HTMLResponse)
async def social_groups_page(request: Request):
    """Pagina de Grupos Sociais"""
    return templates.TemplateResponse("rap_social_groups.html", {"request": request})


@app.get("/api/social-groups")
async def api_list_social_groups():
    """Lista grupos sociais (do cache se disponivel)"""
    from services.social_groups import list_all_social_groups
    return await list_all_social_groups()


@app.post("/api/social-groups/sync")
async def api_sync_social_groups():
    """Forca sync do cache de grupos sociais"""
    from services.social_groups import sync_all_groups_cache
    result = await sync_all_groups_cache()
    return result


@app.get("/api/social-groups/crossover")
async def api_social_groups_crossover():
    """Contatos que participam de 2+ grupos em comum"""
    from services.social_groups import get_contact_crossover
    return {"contacts": get_contact_crossover()}


@app.get("/api/social-groups/introductions")
async def api_social_groups_introductions(limit: int = 10):
    """Sugestoes de introducoes entre contatos de grupos em comum"""
    from services.social_groups import suggest_introductions
    return {"introductions": suggest_introductions(limit=limit)}


@app.post("/api/social-groups/sync-messages")
async def api_sync_group_messages():
    """Sincroniza mensagens dos grupos marcados"""
    from services.group_message_sync import sync_group_messages
    return await sync_group_messages(limit_per_group=50)


@app.post("/api/social-groups/messages")
async def api_get_group_messages(request: Request):
    """Retorna mensagens de um grupo"""
    from services.group_message_sync import get_group_messages
    data = await request.json()
    group_jid = data.get('group_jid', '')
    limit = data.get('limit', 50)
    return {"messages": get_group_messages(group_jid, limit=limit)}


@app.post("/api/social-groups/toggle-sync")
async def api_toggle_group_sync(request: Request):
    """Ativa/desativa sincronizacao de um grupo"""
    from services.social_groups import toggle_group_sync
    data = await request.json()
    group_jid = data.get('group_jid')
    enabled = data.get('enabled', False)
    if not group_jid:
        raise HTTPException(status_code=400, detail="group_jid obrigatorio")
    success = toggle_group_sync(group_jid, enabled)
    if not success:
        raise HTTPException(status_code=404, detail="Grupo nao encontrado")
    return {"status": "success", "sync_enabled": enabled}


@app.get("/api/social-groups/{group_jid:path}")
async def api_get_social_group(group_jid: str):
    """Detalhes de um grupo com membros (do cache)"""
    from services.social_groups import get_group_with_members
    result = await get_group_with_members(group_jid)
    if not result:
        raise HTTPException(status_code=404, detail="Grupo nao encontrado")
    return result


# ============== PROJECT WHATSAPP GROUPS ==============

@app.get("/api/projects/{project_id}/whatsapp-groups")
async def api_list_project_groups(project_id: int):
    """Lista grupos WhatsApp vinculados ao projeto"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, group_jid, group_name, ativo, last_synced_at
            FROM project_whatsapp_groups WHERE project_id = %s ORDER BY group_name
        """, (project_id,))
        return [dict(r) for r in cursor.fetchall()]


@app.post("/api/projects/{project_id}/whatsapp-groups")
async def api_add_project_group(project_id: int, request: Request):
    """Vincula um grupo WhatsApp ao projeto"""
    data = await request.json()
    group_jid = data.get('group_jid')
    group_name = data.get('group_name', '')
    if not group_jid:
        raise HTTPException(status_code=400, detail="group_jid obrigatorio")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO project_whatsapp_groups (project_id, group_jid, group_name)
            VALUES (%s, %s, %s) ON CONFLICT (project_id, group_jid) DO UPDATE SET group_name = %s, ativo = TRUE
            RETURNING id
        """, (project_id, group_jid, group_name, group_name))
        conn.commit()
        return {"status": "success", "id": cursor.fetchone()['id']}


@app.delete("/api/projects/{project_id}/whatsapp-groups/{group_id}")
async def api_remove_project_group(project_id: int, group_id: int):
    """Remove vinculo de grupo WhatsApp do projeto"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM project_whatsapp_groups WHERE id = %s AND project_id = %s", (group_id, project_id))
        conn.commit()
    return {"status": "success"}


# ============== PAYMENT CYCLE ==============

@app.post("/api/projects/{project_id}/payment-cycle/preview")
async def api_payment_cycle_preview(project_id: int, request: Request):
    """Preview do email de cobranca"""
    from services.payment_cycle import generate_payment_email
    data = await request.json()
    result = generate_payment_email(
        project_id,
        month=data.get('month', datetime.now().month),
        year=data.get('year', datetime.now().year),
        expenses_override=data.get('expenses')
    )
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result['error'])
    return result


@app.post("/api/projects/{project_id}/payment-cycle/send")
async def api_payment_cycle_send(project_id: int, request: Request):
    """Envia email de cobranca e cria milestone"""
    from services.payment_cycle import send_payment_email
    data = await request.json()
    result = await send_payment_email(
        project_id,
        month=data.get('month', datetime.now().month),
        year=data.get('year', datetime.now().year),
        expenses=data.get('expenses')
    )
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result['error'])
    return result


# ============== PROJECT NOTES ==============

@app.post("/api/projects/{project_id}/notes")
async def api_add_project_note(project_id: int, request: Request):
    """Adiciona nota ao projeto."""
    data = await request.json()
    if not data.get('conteudo'):
        raise HTTPException(status_code=400, detail="conteudo e obrigatorio")

    note = add_project_note(project_id, data)
    return {"status": "success", "note": note}


@app.get("/api/projects/{project_id}/timeline")
async def api_project_timeline(project_id: int, limit: int = 50):
    """Retorna timeline do projeto."""
    return {"timeline": get_project_timeline(project_id, limit=limit)}


# ============== PROJECT TASKS ==============

@app.post("/api/projects/{project_id}/tasks")
async def api_add_project_task(project_id: int, request: Request):
    """
    Cria tarefa vinculada ao projeto.
    Salva no banco local com project_id E sincroniza com Google Tasks.
    """
    data = await request.json()
    titulo = data.get('titulo')
    if not titulo:
        raise HTTPException(status_code=400, detail="titulo e obrigatorio")

    with get_db() as conn:
        cursor = conn.cursor()

        # Verify project exists and get project name for context
        cursor.execute("SELECT id, nome FROM projects WHERE id = %s", (project_id,))
        project = cursor.fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Projeto nao encontrado")

    # Parse due date
    due_datetime = None
    if data.get('data_vencimento'):
        try:
            due_datetime = datetime.fromisoformat(str(data['data_vencimento']).replace("Z", "+00:00"))
        except:
            pass

    # Add project context to description
    descricao = data.get('descricao') or ''
    if project['nome']:
        descricao = f"[Projeto: {project['nome']}]\n{descricao}".strip()

    # Use sync service to create locally AND push to Google
    tasks_service = get_tasks_sync_service()
    result = await tasks_service.create_task(
        titulo=titulo,
        descricao=descricao,
        data_vencimento=due_datetime,
        prioridade=data.get('prioridade', 5),
        contact_id=data.get('contact_id'),
        project_id=project_id,
        sync_to_google=True
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=f"Erro ao criar tarefa: {result['error']}")

    # Get the created task for response
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = %s", (result.get('id'),))
        row = cursor.fetchone()
        task = dict(row) if row else result

    return {"status": "success", "task": task, "synced_to_google": True}


    # all-tasks moved before {project_id} routes


@app.put("/api/projects/tasks/{task_id}")
async def api_update_project_task(task_id: int, request: Request):
    """Atualiza tarefa do projeto e sincroniza com Google Tasks."""
    data = await request.json()

    # Parse due date if present
    due_datetime = None
    if data.get('data_vencimento'):
        try:
            due_datetime = datetime.fromisoformat(str(data['data_vencimento']).replace("Z", "+00:00"))
        except:
            due_datetime = data.get('data_vencimento')

    # Use sync service to update locally AND push to Google
    tasks_service = get_tasks_sync_service()
    result = await tasks_service.update_task(
        task_id=task_id,
        titulo=data.get('titulo'),
        descricao=data.get('descricao'),
        status=data.get('status'),
        prioridade=data.get('prioridade'),
        data_vencimento=due_datetime,
        sync_to_google=True
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return {"status": "success", "task": result.get("task", {}), "synced_to_google": True}


@app.delete("/api/projects/tasks/{task_id}")
async def api_delete_project_task(task_id: int):
    """Deleta tarefa do projeto e do Google Tasks."""
    tasks_service = get_tasks_sync_service()
    result = await tasks_service.delete_task(task_id, delete_from_google=True)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return {"status": "success", "deleted_from_google": True}


# ============== PROJECTS PAGE ==============

@app.get("/rap/projetos")
async def rap_projetos_redirect():
    return RedirectResponse(url="/projetos", status_code=301)


@app.get("/rap/projetos/{project_id}")
async def rap_projeto_detail_redirect(project_id: int):
    return RedirectResponse(url=f"/projetos/{project_id}", status_code=301)


# ============== EDITORIAL CALENDAR API ==============

from services.editorial_calendar import (
    get_editorial_posts, get_editorial_post, create_editorial_post,
    update_editorial_post, delete_editorial_post, schedule_post,
    mark_as_published, import_articles_from_site, get_calendar_view,
    get_stats as get_editorial_stats, get_pending_tasks as get_editorial_pending_tasks,
    EDITORIAL_STATUS, EDITORIAL_CANAIS, EDITORIAL_TIPOS
)


@app.get("/api/editorial")
async def api_editorial_list(
    status: str = None,
    canal: str = None,
    project_id: int = None,
    from_date: str = None,
    to_date: str = None,
    limit: int = 100
):
    """Lista posts do calendario editorial"""
    from_dt = datetime.fromisoformat(from_date) if from_date else None
    to_dt = datetime.fromisoformat(to_date) if to_date else None

    posts = get_editorial_posts(
        status=status, canal=canal, project_id=project_id,
        from_date=from_dt, to_date=to_dt, limit=limit
    )
    return {"posts": posts, "total": len(posts)}


@app.get("/api/editorial/stats")
async def api_editorial_stats():
    """Estatisticas do calendario editorial"""
    return get_editorial_stats()


@app.get("/api/editorial/pending-tasks")
async def api_editorial_pending_tasks():
    """Tarefas pendentes do editorial - o que precisa ser feito"""
    return get_editorial_pending_tasks()


@app.get("/api/editorial/calendar/{year}/{month}")
async def api_editorial_calendar_view(year: int, month: int):
    """Visualizacao de calendario mensal"""
    return get_calendar_view(year, month)


@app.get("/api/editorial/meta")
async def api_editorial_meta():
    """Retorna constantes do calendario editorial"""
    return {
        "status": EDITORIAL_STATUS,
        "canais": EDITORIAL_CANAIS,
        "tipos": EDITORIAL_TIPOS
    }


# =============================================================================
# EDITORIAL AI ANALYSIS
# =============================================================================

@app.get("/api/editorial/ai/stats")
async def api_editorial_ai_stats():
    """Estatísticas da análise IA dos artigos"""
    from services.editorial_calendar import get_analysis_stats
    return get_analysis_stats()


@app.get("/api/editorial/ai-top-suggestions")
async def api_editorial_ai_top_suggestions():
    """Retorna os artigos com melhor score de IA para sugestão de repost"""
    from database import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, article_title, article_url, ai_categoria,
                   ai_score_relevancia, ai_gancho_linkedin, ai_evergreen
            FROM editorial_posts
            WHERE status = 'draft'
              AND ai_score_relevancia >= 7
              AND ai_gancho_linkedin IS NOT NULL
            ORDER BY ai_score_relevancia DESC, ai_evergreen DESC
            LIMIT 10
        """)

        suggestions = []
        for row in cursor.fetchall():
            post = dict(row)
            suggestions.append({
                'id': post['id'],
                'titulo': post['article_title'],
                'url': post.get('article_url'),
                'categoria': post.get('ai_categoria'),
                'score': post.get('ai_score_relevancia'),
                'gancho': post.get('ai_gancho_linkedin'),
                'evergreen': post.get('ai_evergreen', False)
            })

    return {"suggestions": suggestions}


@app.get("/api/editorial/dashboard-tasks")
async def api_editorial_dashboard_tasks():
    """Retorna tarefas do LinkedIn para o dashboard - hoje e próximos"""
    from database import get_db
    from datetime import datetime, timedelta

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    next_week = today + timedelta(days=7)

    result = {
        'hoje': [],
        'proximos': []
    }

    with get_db() as conn:
        cursor = conn.cursor()

        # Posts para HOJE
        cursor.execute("""
            SELECT ep.id, ep.article_title, ep.tipo, ep.status,
                   ep.data_publicacao, ep.conteudo_adaptado, ep.ai_gancho_linkedin
            FROM editorial_posts ep
            WHERE ep.status IN ('scheduled', 'published')
              AND DATE(ep.data_publicacao) = DATE(%s)
            ORDER BY ep.data_publicacao ASC
        """, (today,))

        for row in cursor.fetchall():
            post = dict(row)
            result['hoje'].append({
                'id': post['id'],
                'titulo': post['article_title'] or 'Sem título',
                'tipo': post['tipo'] or 'artigo',
                'status': post['status'],
                'horario': post['data_publicacao'].isoformat() if post['data_publicacao'] else None
            })

        # Próximos posts (exceto hoje)
        cursor.execute("""
            SELECT ep.id, ep.article_title, ep.tipo, ep.data_publicacao
            FROM editorial_posts ep
            WHERE ep.status = 'scheduled'
              AND DATE(ep.data_publicacao) > DATE(%s)
              AND DATE(ep.data_publicacao) <= DATE(%s)
            ORDER BY ep.data_publicacao ASC
            LIMIT 5
        """, (today, next_week))

        for row in cursor.fetchall():
            post = dict(row)
            result['proximos'].append({
                'id': post['id'],
                'titulo': post['article_title'] or 'Sem título',
                'tipo': post['tipo'] or 'artigo',
                'data_publicacao': post['data_publicacao'].isoformat() if post['data_publicacao'] else None
            })

        # Se nada agendado, sugerir um draft
        if not result['hoje']:
            cursor.execute("""
                SELECT id, article_title, tipo, conteudo_adaptado
                FROM editorial_posts
                WHERE status = 'draft' AND article_title IS NOT NULL AND article_title != ''
                ORDER BY RANDOM() LIMIT 1
            """)
            draft = cursor.fetchone()
            if draft:
                result['draft_sugerido'] = {
                    'id': draft['id'],
                    'titulo': draft['article_title'],
                    'tipo': draft['tipo']
                }

            cursor.execute("SELECT COUNT(*) FROM editorial_posts WHERE status = 'draft'")
            result['total_drafts'] = cursor.fetchone()['count']

        # Count posts needing metrics (published, 48h+ ago, no metrics)
        cursor.execute("""
            SELECT COUNT(*) FROM editorial_posts
            WHERE status = 'published'
              AND data_publicado IS NOT NULL
              AND data_publicado < NOW() - INTERVAL '48 hours'
              AND (linkedin_impressoes IS NULL OR linkedin_impressoes = 0)
        """)
        result['needs_metrics_count'] = cursor.fetchone()['count']

    return result


@app.post("/api/editorial/ai/analyze")
async def api_editorial_ai_analyze(request: Request):
    """
    Analisa artigos com IA.

    Body:
    {
        "post_ids": [1, 2, 3],  // opcional - se vazio, analisa todos pendentes
        "limit": 10,            // opcional - máximo de artigos
        "force": false          // opcional - reanalisar já analisados
    }
    """
    from services.editorial_calendar import analyze_all_articles, analyze_article_with_ai

    data = await request.json()
    post_ids = data.get('post_ids', [])
    limit = data.get('limit')
    force = data.get('force', False)

    if post_ids:
        # Analyze specific posts
        results = {
            'total': len(post_ids),
            'analyzed': 0,
            'errors': 0,
            'details': []
        }
        for post_id in post_ids:
            try:
                analysis = analyze_article_with_ai(post_id)
                if analysis:
                    results['analyzed'] += 1
                    results['details'].append({
                        'id': post_id,
                        'categoria': analysis.get('categoria'),
                        'score': analysis.get('score_relevancia'),
                        'status': 'success'
                    })
                else:
                    results['errors'] += 1
                    results['details'].append({'id': post_id, 'status': 'error'})
            except Exception as e:
                results['errors'] += 1
                results['details'].append({'id': post_id, 'status': 'error', 'error': str(e)})
        return {"status": "success", **results}
    else:
        # Analyze all pending
        results = analyze_all_articles(limit=limit, force=force)
        return {"status": "success", **results}


@app.get("/api/editorial/{post_id}/analysis")
async def api_editorial_get_analysis(post_id: int):
    """Retorna análise IA de um post específico"""
    post = get_editorial_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post não encontrado")

    return {
        "post_id": post_id,
        "title": post.get('article_title'),
        "analyzed": post.get('ai_analisado_em') is not None,
        "analysis": {
            "categoria": post.get('ai_categoria'),
            "subcategoria": post.get('ai_subcategoria'),
            "publico_alvo": post.get('ai_publico_alvo'),
            "tipo_conteudo": post.get('ai_tipo_conteudo'),
            "complexidade": post.get('ai_complexidade'),
            "evergreen": post.get('ai_evergreen'),
            "keywords": post.get('ai_keywords'),
            "gancho_linkedin": post.get('ai_gancho_linkedin'),
            "tempo_leitura": post.get('ai_tempo_leitura'),
            "score_relevancia": post.get('ai_score_relevancia'),
            "analise_completa": post.get('ai_analise_completa'),
            "analisado_em": post.get('ai_analisado_em').isoformat() if post.get('ai_analisado_em') else None
        }
    }


@app.get("/api/editorial/funnel")
async def api_editorial_funnel():
    """Editorial funnel: Posts -> Impressoes -> Engajamento -> Mensagens -> Reunioes"""
    from services.editorial_pdca import get_editorial_funnel
    return get_editorial_funnel()


@app.get("/api/editorial/{post_id}")
async def api_editorial_get(post_id: int):
    """Retorna um post especifico"""
    post = get_editorial_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post nao encontrado")
    return post


@app.post("/api/editorial")
async def api_editorial_create(request: Request):
    """Cria novo post editorial"""
    data = await request.json()
    if not data.get('article_title'):
        raise HTTPException(status_code=400, detail="article_title e obrigatorio")

    post = create_editorial_post(data)
    return {"status": "success", "post": post}


@app.put("/api/editorial/{post_id}")
async def api_editorial_update(post_id: int, request: Request):
    """Atualiza post editorial"""
    data = await request.json()
    post = update_editorial_post(post_id, data)
    if not post:
        raise HTTPException(status_code=404, detail="Post nao encontrado")
    return {"status": "success", "post": post}


@app.delete("/api/editorial/{post_id}")
async def api_editorial_delete(post_id: int):
    """Remove post editorial"""
    if delete_editorial_post(post_id):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Post nao encontrado")


@app.post("/api/editorial/{post_id}/schedule")
async def api_editorial_schedule(post_id: int, request: Request):
    """Agenda post para publicacao"""
    data = await request.json()
    data_publicacao = data.get('data_publicacao')
    if not data_publicacao:
        raise HTTPException(status_code=400, detail="data_publicacao e obrigatoria")

    try:
        dt = datetime.fromisoformat(data_publicacao.replace('Z', '+00:00'))
    except:
        raise HTTPException(status_code=400, detail="Formato de data invalido")

    post = schedule_post(
        post_id, dt,
        create_task=data.get('create_task', True),
        create_event=data.get('create_event', True)
    )
    return {"status": "success", "post": post}


@app.post("/api/editorial/{post_id}/publish")
async def api_editorial_publish(post_id: int, request: Request):
    """Marca post como publicado"""
    data = await request.json()
    post = mark_as_published(
        post_id,
        url_publicado=data.get('url_publicado'),
        metricas=data.get('metricas')
    )
    if not post:
        raise HTTPException(status_code=404, detail="Post nao encontrado")
    return {"status": "success", "post": post}


@app.post("/api/editorial/bulk-schedule")
async def api_editorial_bulk_schedule(request: Request):
    """
    Agenda multiplos posts em lote com horarios ideais.

    Body:
    {
        "post_ids": [1, 2, 3],
        "start_date": "2026-04-07",
        "frequency_per_week": 3,
        "preferred_days": [1, 2, 3],  // 0=Mon, 1=Tue, etc
        "preferred_hours": [9, 12],
        "create_tasks": true,
        "create_events": true
    }
    """
    from services.editorial_calendar import bulk_schedule_posts

    data = await request.json()
    post_ids = data.get('post_ids', [])

    if not post_ids:
        raise HTTPException(status_code=400, detail="post_ids e obrigatorio")

    start_date_str = data.get('start_date')
    if not start_date_str:
        start_date = datetime.now()
    else:
        try:
            start_date = datetime.fromisoformat(start_date_str)
        except:
            raise HTTPException(status_code=400, detail="Formato de data invalido")

    result = bulk_schedule_posts(
        post_ids=post_ids,
        start_date=start_date,
        frequency_per_week=data.get('frequency_per_week', 3),
        preferred_days=data.get('preferred_days'),
        preferred_hours=data.get('preferred_hours'),
        create_tasks=data.get('create_tasks', True),
        create_events=data.get('create_events', True)
    )

    return {"status": "success", **result}


@app.post("/api/editorial/import")
async def api_editorial_import(request: Request):
    """Importa artigos do site para o calendario editorial"""
    data = await request.json()
    articles = data.get('articles', [])
    project_id = data.get('project_id')

    if not articles:
        raise HTTPException(status_code=400, detail="Lista de artigos vazia")

    result = import_articles_from_site(articles, project_id)
    return {"status": "success", **result}


@app.get("/api/projects/{project_id}/editorial")
async def api_project_editorial(project_id: int):
    """Lista posts editoriais de um projeto"""
    posts = get_editorial_posts(project_id=project_id)
    return {"posts": posts, "total": len(posts)}


# ============== EDITORIAL PDCA ==============

@app.get("/api/cron/editorial-weekly-briefing")
async def cron_editorial_weekly_briefing(request: Request):
    """
    Cron: Gera briefing editorial semanal.
    Schedule: 0 21 * * 0 (Domingos as 21h UTC / 18h Brasilia)
    """
    if not verify_cron_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized cron request")

    from services.editorial_pdca import generate_weekly_briefing

    try:
        result = await generate_weekly_briefing()
        return {
            "job": "editorial-weekly-briefing",
            "timestamp": datetime.now().isoformat(),
            "status": result.get("status", "error"),
            "tasks_created": len(result.get("tasks_created", [])),
            "note_id": result.get("note_id"),
            "error": result.get("error"),
        }
    except Exception as e:
        return {
            "job": "editorial-weekly-briefing",
            "status": "error",
            "error": str(e),
        }


# ============== EDITORIAL METRICS API ==============

@app.post("/api/editorial/{post_id}/metrics")
async def api_editorial_post_metrics(post_id: int, request: Request):
    """Save metrics for an editorial post (manual or from xlsx upload)"""
    data = await request.json()

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT id, data_publicado, status FROM editorial_posts WHERE id = %s", (post_id,))
        post = cursor.fetchone()
        if not post:
            raise HTTPException(status_code=404, detail="Post nao encontrado")

        impressoes = data.get('impressoes', 0) or 0
        reacoes = data.get('reacoes', 0) or 0
        comentarios = data.get('comentarios', 0) or 0
        compartilhamentos = data.get('compartilhamentos', 0) or 0
        cliques = data.get('cliques', 0) or 0
        visitas_perfil = data.get('visitas_perfil', 0) or 0
        seguidores = data.get('seguidores', 0) or 0
        salvamentos = data.get('salvamentos', 0) or 0
        fonte = data.get('fonte', 'manual')

        dias_apos = None
        if post.get('data_publicado'):
            delta = datetime.now() - post['data_publicado']
            dias_apos = delta.days

        cursor.execute("""
            UPDATE editorial_posts
            SET linkedin_impressoes = %s,
                linkedin_reacoes = %s,
                linkedin_comentarios = %s,
                linkedin_compartilhamentos = %s,
                linkedin_cliques = %s,
                linkedin_metricas_em = CURRENT_TIMESTAMP,
                atualizado_em = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (impressoes, reacoes, comentarios, compartilhamentos, cliques, post_id))

        cursor.execute("""
            INSERT INTO editorial_metrics_history (
                post_id, impressoes, reacoes, comentarios, compartilhamentos,
                visitas_perfil, seguidores, salvamentos, dias_apos_publicacao, fonte
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (post_id, impressoes, reacoes, comentarios, compartilhamentos,
              visitas_perfil, seguidores, salvamentos, dias_apos, fonte))
        history_id = cursor.fetchone()['id']

        cursor.execute("""
            UPDATE tasks
            SET status = 'completed', data_conclusao = CURRENT_TIMESTAMP
            WHERE origem = 'editorial_metrics'
              AND status = 'pending'
              AND descricao LIKE %s
        """, (f'%post_id={post_id}%',))

        conn.commit()

    return {
        "status": "success",
        "post_id": post_id,
        "history_id": history_id,
        "dias_apos_publicacao": dias_apos
    }


@app.post("/api/editorial/metrics/upload")
async def api_editorial_metrics_upload(request: Request):
    """Accept parsed xlsx data and auto-match post by URL"""
    data = await request.json()
    post_url = data.get('post_url', '').strip()
    metrics = data.get('metrics', {})

    if not post_url and not data.get('post_id'):
        raise HTTPException(status_code=400, detail="post_url ou post_id obrigatorio")

    with get_db() as conn:
        cursor = conn.cursor()

        post_id = data.get('post_id')
        if not post_id and post_url:
            cursor.execute("""
                SELECT id FROM editorial_posts
                WHERE linkedin_post_url = %s OR url_publicado = %s
                ORDER BY id DESC LIMIT 1
            """, (post_url, post_url))
            row = cursor.fetchone()
            if row:
                post_id = row['id']

        if not post_id and post_url and 'linkedin.com' in post_url:
            url_part = post_url.split('/')[-1].split('?')[0] if post_url else ''
            if url_part:
                cursor.execute("""
                    SELECT id FROM editorial_posts
                    WHERE linkedin_post_url LIKE %s OR url_publicado LIKE %s
                    ORDER BY id DESC LIMIT 1
                """, (f'%{url_part}%', f'%{url_part}%'))
                row = cursor.fetchone()
                if row:
                    post_id = row['id']

        if not post_id:
            return {
                "status": "not_found",
                "message": "Nenhum post encontrado com essa URL. Selecione o post manualmente.",
                "post_url": post_url
            }

        cursor.execute("SELECT id, data_publicado, status FROM editorial_posts WHERE id = %s", (post_id,))
        post = cursor.fetchone()

        dias_apos = None
        if post.get('data_publicado'):
            delta = datetime.now() - post['data_publicado']
            dias_apos = delta.days

        impressoes = metrics.get('impressoes', 0) or 0
        reacoes = metrics.get('reacoes', 0) or 0
        comentarios = metrics.get('comentarios', 0) or 0
        compartilhamentos = metrics.get('compartilhamentos', 0) or 0
        cliques = metrics.get('cliques', 0) or 0
        visitas_perfil = metrics.get('visitas_perfil', 0) or 0
        seguidores = metrics.get('seguidores', 0) or 0
        salvamentos = metrics.get('salvamentos', 0) or 0

        cursor.execute("""
            UPDATE editorial_posts
            SET linkedin_impressoes = %s,
                linkedin_reacoes = %s,
                linkedin_comentarios = %s,
                linkedin_compartilhamentos = %s,
                linkedin_cliques = %s,
                linkedin_metricas_em = CURRENT_TIMESTAMP,
                atualizado_em = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (impressoes, reacoes, comentarios, compartilhamentos, cliques, post_id))

        cursor.execute("""
            INSERT INTO editorial_metrics_history (
                post_id, impressoes, reacoes, comentarios, compartilhamentos,
                visitas_perfil, seguidores, salvamentos, dias_apos_publicacao, fonte
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (post_id, impressoes, reacoes, comentarios, compartilhamentos,
              visitas_perfil, seguidores, salvamentos, dias_apos, 'xlsx_upload'))
        history_id = cursor.fetchone()['id']

        cursor.execute("""
            UPDATE tasks
            SET status = 'completed', data_conclusao = CURRENT_TIMESTAMP
            WHERE origem = 'editorial_metrics'
              AND status = 'pending'
              AND descricao LIKE %s
        """, (f'%post_id={post_id}%',))

        conn.commit()

    return {
        "status": "success",
        "post_id": post_id,
        "history_id": history_id,
        "dias_apos_publicacao": dias_apos
    }


@app.get("/api/editorial/metrics/comparison")
async def api_editorial_metrics_comparison():
    """Get normalized comparison of all published posts at their 48h mark"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DISTINCT ON (emh.post_id)
                emh.post_id,
                ep.article_title,
                ep.data_publicado,
                ep.ai_categoria,
                emh.impressoes,
                emh.reacoes,
                emh.comentarios,
                emh.compartilhamentos,
                emh.salvamentos,
                emh.dias_apos_publicacao,
                emh.coletado_em,
                CASE WHEN emh.impressoes > 0
                    THEN ROUND(((emh.reacoes + emh.comentarios)::numeric / emh.impressoes) * 100, 2)
                    ELSE 0
                END as taxa_engajamento
            FROM editorial_metrics_history emh
            JOIN editorial_posts ep ON ep.id = emh.post_id
            WHERE ep.status = 'published'
            ORDER BY emh.post_id, ABS(COALESCE(emh.dias_apos_publicacao, 999) - 2) ASC
        """)
        rows = [dict(r) for r in cursor.fetchall()]

        # Include legacy posts with metrics but no history entries
        cursor.execute("""
            SELECT ep.id as post_id, ep.article_title, ep.data_publicado, ep.ai_categoria,
                   ep.linkedin_impressoes as impressoes, ep.linkedin_reacoes as reacoes,
                   ep.linkedin_comentarios as comentarios, ep.linkedin_compartilhamentos as compartilhamentos,
                   0 as salvamentos,
                   CASE WHEN ep.data_publicado IS NOT NULL
                       THEN EXTRACT(DAY FROM (COALESCE(ep.linkedin_metricas_em, CURRENT_TIMESTAMP) - ep.data_publicado))::integer
                       ELSE NULL
                   END as dias_apos_publicacao,
                   ep.linkedin_metricas_em as coletado_em,
                   CASE WHEN ep.linkedin_impressoes > 0
                       THEN ROUND(((COALESCE(ep.linkedin_reacoes,0) + COALESCE(ep.linkedin_comentarios,0))::numeric / ep.linkedin_impressoes) * 100, 2)
                       ELSE 0
                   END as taxa_engajamento
            FROM editorial_posts ep
            WHERE ep.status = 'published'
              AND ep.linkedin_impressoes > 0
              AND ep.id NOT IN (SELECT DISTINCT post_id FROM editorial_metrics_history)
            ORDER BY ep.data_publicado DESC
        """)
        legacy_rows = [dict(r) for r in cursor.fetchall()]

        all_posts = rows + legacy_rows
        all_posts.sort(key=lambda x: x.get('impressoes') or 0, reverse=True)

        return {"posts": all_posts, "total": len(all_posts)}


# ============== HOT TAKES API ==============

@app.get("/api/hot-takes/version")
async def api_hot_takes_version():
    """Versao do servico hot takes"""
    return {"version": "2.2", "timestamp": "2026-04-03T17:35:00"}


@app.get("/api/hot-takes")
async def api_hot_takes_list(status: str = None, limit: int = 20):
    """Lista hot takes salvos"""
    from services.hot_takes import get_hot_takes
    return {"hot_takes": get_hot_takes(status=status, limit=limit)}


@app.get("/api/hot-takes/stats")
async def api_hot_takes_stats():
    """Estatisticas de hot takes"""
    from services.hot_takes import get_weekly_digest_stats
    return get_weekly_digest_stats()


@app.post("/api/hot-takes/digest")
async def api_generate_digest(request: Request):
    """Gera digest semanal de hot takes"""
    from services.hot_takes import generate_weekly_digest
    data = await request.json()
    limit = data.get("limit", 5)
    result = await generate_weekly_digest(limit=limit)
    return result


@app.post("/api/hot-takes/from-url")
async def api_hot_take_from_url(request: Request):
    """Gera hot take a partir de URL manual"""
    from services.hot_takes import generate_hot_take_from_url
    data = await request.json()
    url = data.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="URL e obrigatoria")
    result = await generate_hot_take_from_url(url)
    return result


@app.get("/api/hot-takes/{hot_take_id}")
async def api_hot_take_get(hot_take_id: int):
    """Retorna um hot take especifico"""
    from services.hot_takes import get_hot_takes
    hot_takes = get_hot_takes()
    for ht in hot_takes:
        if ht.get("id") == hot_take_id:
            return ht
    raise HTTPException(status_code=404, detail="Hot take nao encontrado")


@app.post("/api/hot-takes/{hot_take_id}/schedule")
async def api_hot_take_schedule(hot_take_id: int, request: Request):
    """Agenda hot take para publicacao e cria entrada no calendario editorial"""
    from services.hot_takes import schedule_hot_take
    data = await request.json()
    scheduled_for = data.get("scheduled_for")
    create_editorial = data.get("create_editorial", True)

    result = schedule_hot_take(hot_take_id, scheduled_for, create_editorial)
    return result


@app.post("/api/hot-takes/{hot_take_id}/publish")
async def api_hot_take_publish(hot_take_id: int, request: Request):
    """Marca hot take como publicado"""
    from services.hot_takes import mark_hot_take_published
    data = await request.json()
    linkedin_url = data.get("linkedin_url")

    result = mark_hot_take_published(hot_take_id, linkedin_url)
    return result


@app.post("/api/hot-takes/{hot_take_id}/metrics")
async def api_hot_take_metrics(hot_take_id: int, request: Request):
    """Atualiza metricas de engajamento do hot take"""
    from services.hot_takes import update_hot_take_metrics
    data = await request.json()
    metrics = {
        "likes": data.get("likes", 0),
        "comments": data.get("comments", 0),
        "shares": data.get("shares", 0),
        "impressions": data.get("impressions", 0)
    }

    result = update_hot_take_metrics(hot_take_id, metrics)
    return result


@app.get("/hot-takes", response_class=HTMLResponse)
async def hot_takes_page(request: Request):
    """Pagina de Hot Takes"""
    import traceback
    try:
        from services.hot_takes import get_hot_takes, get_weekly_digest_stats
        hot_takes = get_hot_takes(limit=50)
        stats = get_weekly_digest_stats()

        return templates.TemplateResponse("hot_takes.html", {
            "request": request,
            "hot_takes": hot_takes,
            "stats": stats
        })
    except Exception as e:
        error_detail = traceback.format_exc()
        return HTMLResponse(f"<pre>Error: {e}\n\n{error_detail}</pre>", status_code=500)


# ============== NEWS HUB ==============

@app.get("/news", response_class=HTMLResponse)
async def news_hub_page(request: Request):
    """News Hub - Central de Notícias Inteligente"""
    return templates.TemplateResponse("rap_news.html", {"request": request})


@app.get("/api/news/feed")
async def api_news_feed(request: Request):
    """Retorna feed de notícias personalizado"""
    from services.news_hub import get_news_feed, detect_trending

    detect_trending(hours=48)
    feed = get_news_feed(1, limit=15)
    return feed


_clipping_cache = {"data": None, "date": None}

@app.get("/api/news/clipping")
async def api_daily_clipping():
    """Gera ou retorna clipping diario (cache in-memory)"""
    from services.news_hub import generate_daily_clipping

    global _clipping_cache
    today = datetime.now().date()
    if _clipping_cache["data"] and _clipping_cache["date"] == today:
        return _clipping_cache["data"]

    # Verificar se ja tem clipping de hoje
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, conteudo, resumo_dia, total_noticias, gerado_em
            FROM news_clippings
            WHERE DATE(gerado_em) = CURRENT_DATE
            ORDER BY gerado_em DESC LIMIT 1
        """)
        existing = cursor.fetchone()

    if existing:
        existing = dict(existing)
        import json as _json
        clipping = existing['conteudo']
        if isinstance(clipping, str):
            clipping = _json.loads(clipping)
        result = {
            "clipping": clipping,
            "resumo_dia": existing['resumo_dia'],
            "total_collected": existing['total_noticias'],
            "clipping_id": existing['id'],
            "cached": True
        }
        _clipping_cache = {"data": result, "date": today}
        return result

    # Gerar novo
    result = await generate_daily_clipping(limit=10)
    _clipping_cache = {"data": result, "date": today}
    return result


@app.post("/api/news/clipping/refresh")
async def api_refresh_clipping():
    """Forca geracao de novo clipping (mesmo se ja existe hoje)"""
    from services.news_hub import generate_daily_clipping
    return await generate_daily_clipping(limit=10)


@app.post("/api/news/to-post")
async def api_news_to_post(request: Request):
    """Cria post editorial a partir de noticia do clipping, gerando texto e cruzando com artigos"""
    import httpx as _hx

    data = await request.json()
    titulo = data.get('titulo', '')
    sugestao = data.get('sugestao', '')
    news_id = data.get('news_id')

    # Buscar artigo relacionado dos drafts
    article_url = None
    article_match = None
    with get_db() as conn:
        cursor = conn.cursor()
        # Buscar draft que melhor combina por keywords no titulo
        words = [w.lower() for w in titulo.split() if len(w) > 4]
        if words:
            like_clause = " OR ".join(["article_title ILIKE %s" for _ in words[:5]])
            params = [f"%{w}%" for w in words[:5]]
            cursor.execute(f"""
                SELECT id, article_title, article_url FROM editorial_posts
                WHERE status = 'draft' AND article_url IS NOT NULL AND article_url != ''
                  AND ({like_clause})
                LIMIT 1
            """, params)
            match = cursor.fetchone()
            if match:
                article_match = dict(match)
                article_url = match['article_url']

    # Gerar texto para LinkedIn com IA
    api_key = os.getenv("ANTHROPIC_API_KEY")
    linkedin_text = ""
    if api_key:
        try:
            prompt = f"""Gere um post COMPLETO para LinkedIn do Renato Prado sobre esta noticia.

NOTICIA: {titulo}
SUGESTAO DE ANGULO: {sugestao or 'livre'}
{f'ARTIGO RELACIONADO DO RENATO: {article_match["article_title"]} ({article_url})' if article_match else ''}

FORMATO OBRIGATORIO (minimo 150 palavras, maximo 250):

1. GANCHO (1 frase impactante que prende atencao)
2. CONTEXTO (2-3 frases sobre a noticia)
3. OPINIAO DO RENATO (3-4 frases com visao forte, conectando com governanca/IA/empreendedorismo)
{f'4. LINK DO ARTIGO: "Escrevi sobre isso em: {article_url}"' if article_url else '4. REFLEXAO FINAL (1-2 frases)'}
5. CALL TO ACTION (pergunta para gerar engajamento)
6. HASHTAGS (3-4 hashtags relevantes)

Tom: executivo mas acessivel, com opiniao forte. Em portugues.
NAO use markdown (sem ** ou ## ou *). Texto puro para colar no LinkedIn."""

            async with _hx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
                          "messages": [{"role": "user", "content": prompt}]}
                )
                if resp.status_code == 200:
                    linkedin_text = resp.json()["content"][0]["text"]
        except Exception as e:
            linkedin_text = sugestao or titulo

    # Criar post editorial
    from services.editorial_calendar import create_editorial_post
    post = create_editorial_post({
        'article_title': titulo,
        'article_url': article_url or '',
        'tipo': 'hot_take',
        'canal': 'linkedin',
        'status': 'draft',
        'conteudo_adaptado': linkedin_text,
        'notas': f"Criado do clipping diario. {('Sugestao IA: ' + sugestao) if sugestao else ''}",
    })

    return {"status": "success", "post": post}


@app.post("/api/share/generate-message")
async def api_generate_share_message(request: Request):
    """Gera mensagem personalizada para compartilhar conteudo com um contato"""
    import httpx as _hx
    import json as _json

    data = await request.json()
    contact_id = data.get('contact_id')
    content_title = data.get('title', '')
    content_url = data.get('url', '')
    content_type = data.get('type', 'post')  # post, article, news

    if not contact_id:
        raise HTTPException(status_code=400, detail="contact_id obrigatorio")

    # Buscar contexto do contato
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, nome, apelido, empresa, cargo, circulo, contexto, tags, resumo_ai
            FROM contacts WHERE id = %s
        """, (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")
        contact = dict(contact)

        # Fatos do contato
        cursor.execute("SELECT categoria, fato FROM contact_facts WHERE contact_id = %s LIMIT 5", (contact_id,))
        fatos = [dict(r) for r in cursor.fetchall()]

        # Mensagens recentes (para captar tom)
        cursor.execute("""
            SELECT LEFT(m.conteudo, 150) as msg, m.direcao
            FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
            WHERE cv.contact_id = %s ORDER BY m.enviado_em DESC LIMIT 3
        """, (contact_id,))
        msgs = [dict(r) for r in cursor.fetchall()]

    # Gerar mensagem com IA
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # Fallback sem IA
        nome = contact.get('apelido') or contact['nome'].split()[0]
        return {"message": f"Oi {nome}! Publiquei este conteudo e achei que pode te interessar:\n\n{content_url}\n\nAbraco!"}

    nome = contact.get('apelido') or contact['nome'].split()[0]
    tags = contact.get('tags', [])
    if isinstance(tags, str):
        tags = _json.loads(tags) if tags else []

    fatos_text = "\n".join([f"- {f['fato']}" for f in fatos]) if fatos else "Sem fatos registrados"
    msgs_text = "\n".join([
        f"{'Renato' if m['direcao']=='outgoing' else nome}: {m['msg']}"
        for m in msgs
    ]) if msgs else "Sem historico recente"

    prompt = f"""Gere uma mensagem de WhatsApp do Renato para {contact['nome']} compartilhando um conteudo que publicou.

CONTEXTO DO RELACIONAMENTO:
- Nome: {contact['nome']} | Apelido: {nome}
- Profissao: {contact.get('cargo', '?')} @ {contact.get('empresa', '?')}
- Proximidade: Circulo {contact['circulo']} ({contact.get('contexto', '')})
- Tags: {', '.join(tags)}
- Fatos sobre esta pessoa:
{fatos_text}
- Conversa recente:
{msgs_text}

CONTEUDO QUE RENATO PUBLICOU NO LINKEDIN:
Titulo: {content_title}
Link: {content_url}

INSTRUCOES - Gere APENAS a mensagem, nada mais:
1. Use o apelido "{nome}" (nao nome completo)
2. CONECTE o artigo com algo ESPECIFICO da relacao (ex: "lembrei da nossa conversa sobre conselhos")
3. Explique em 1 frase por que ESTA PESSOA especificamente se interessaria
4. Inclua o link no meio da mensagem (nao no final isolado)
5. Tom: como amigo que manda algo relevante, nao como vendedor
6. Max 4-5 linhas
7. Portugues, max 1 emoji"""

    try:
        async with _hx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                      "messages": [{"role": "user", "content": prompt}]}
            )
        if resp.status_code == 200:
            message = resp.json()["content"][0]["text"]
            return {"message": message, "contact_name": contact['nome']}
    except Exception:
        pass

    return {"message": f"Oi {nome}! Publiquei este conteudo e achei que pode te interessar:\n\n{content_url}\n\nAbraco!"}


@app.get("/api/news/suggest-contacts")
async def api_suggest_contacts_for_news(titulo: str = "", categoria: str = ""):
    """Sugere contatos que podem se interessar por uma noticia"""
    import json as _json
    with get_db() as conn:
        cursor = conn.cursor()

        cat_to_tags = {
            'governanca': ['conselheiro', 'board-academy', 'assespro', 'diretoria'],
            'ia': ['imensiah', 'tecnologia', 'c-level'],
            'empreendedorismo': ['ex-socio', 'carambola', 'parenet', 'c-level'],
            'esg': ['esg', 'sustentabilidade', 'board-academy'],
            'esporte': ['cap', 'judo', 'tenis', 'charuto'],
            'agro': ['fazenda', 'agro'],
            'juridico': ['juridico', 'advogado'],
            'estrategia': ['conselheiro', 'board-academy', 'mentor', 'c-level'],
        }
        relevant_tags = cat_to_tags.get(categoria, ['c-level', 'conselheiro'])
        tag_clause = " OR ".join(["tags::text ILIKE %s" for _ in relevant_tags[:5]])
        params = [f"%{t}%" for t in relevant_tags[:5]]

        cursor.execute(f"""
            SELECT id, nome, empresa, cargo, circulo, health_score, foto_url, tags, telefones, emails
            FROM contacts WHERE circulo <= 3 AND ({tag_clause})
            ORDER BY circulo ASC, health_score ASC LIMIT 10
        """, params)

        contacts = []
        for row in cursor.fetchall():
            c = dict(row)
            telefones = c.get('telefones', [])
            if isinstance(telefones, str):
                telefones = _json.loads(telefones) if telefones else []
            phone = ''
            for t in telefones:
                if t.get('number'):
                    phone = t['number']
                    break

            emails = c.get('emails', [])
            if isinstance(emails, str):
                emails = _json.loads(emails) if emails else []
            email = emails[0].get('email', '') if emails else ''

            tags = c.get('tags', [])
            if isinstance(tags, str):
                tags = _json.loads(tags) if tags else []
            matching = [t for t in tags if t in relevant_tags]
            reason = ', '.join(matching[:3]) if matching else f'C{c["circulo"]}'

            contacts.append({
                'id': c['id'], 'nome': c['nome'], 'empresa': c.get('empresa', ''),
                'cargo': c.get('cargo', ''), 'circulo': c['circulo'],
                'foto_url': c.get('foto_url', ''), 'phone': phone, 'email': email, 'reason': reason
            })

    return {"contacts": contacts}


@app.post("/api/news/{news_id}/feedback")
async def api_news_feedback(news_id: int, request: Request):
    """Registra feedback sobre noticia (liked/disliked/shared/hot_take)"""
    from services.news_hub import record_clipping_feedback
    data = await request.json()
    feedback = data.get('feedback', 'liked')
    result = record_clipping_feedback(news_id, feedback)
    return result


@app.post("/api/news/collect")
async def api_news_collect():
    """Coleta novas notícias das fontes RSS"""
    from services.news_hub import collect_news
    result = await collect_news()
    return result


@app.post("/api/news/interaction")
async def api_news_interaction(request: Request):
    """Registra interação do usuário com uma notícia"""
    from services.news_hub import record_interaction

    data = await request.json()
    news_id = data.get("news_id")
    action = data.get("action")
    time_spent = data.get("time_spent", 0)

    if not news_id or not action:
        raise HTTPException(status_code=400, detail="news_id and action required")

    # TODO: Pegar user_id da sessão
    user_id = 1

    record_interaction(user_id, news_id, action, time_spent)
    return {"status": "ok"}


@app.get("/api/news/{news_id}/contacts")
async def api_news_contacts(news_id: int):
    """Retorna contatos sugeridos para uma notícia"""
    from services.news_hub import match_contacts_for_news
    contacts = match_contacts_for_news(news_id)
    return contacts


@app.get("/api/news/{news_id}/details")
async def api_news_details(news_id: int):
    """Retorna detalhes da notícia com insights conectando aos artigos (usa IA)"""
    from services.news_hub import get_news_with_insights
    result = await get_news_with_insights(news_id)
    if not result:
        raise HTTPException(status_code=404, detail="Notícia não encontrada")
    return result


@app.post("/api/news/{news_id}/summary")
async def api_news_summary(news_id: int):
    """Gera resumo inteligente da notícia usando Haiku"""
    from services.news_hub import generate_smart_summary
    summary = await generate_smart_summary(news_id)
    if not summary:
        raise HTTPException(status_code=500, detail="Não foi possível gerar resumo")
    return {"summary": summary}


@app.post("/api/news/{news_id}/connect-article/{article_id}")
async def api_news_connect_article(news_id: int, article_id: int):
    """Gera conexão inteligente entre notícia e artigo do blog"""
    from services.news_hub import generate_article_connection
    result = await generate_article_connection(news_id, article_id)
    if not result:
        raise HTTPException(status_code=500, detail="Não foi possível gerar conexão")
    return result


@app.post("/api/hot-takes/from-news")
async def api_hot_take_from_news(request: Request):
    """Cria hot take a partir de uma notícia"""
    from services.hot_takes import save_hot_take

    data = await request.json()

    hot_take = {
        "news_title": data.get("news_title", ""),
        "news_link": data.get("news_link", ""),
        "hook": "",
        "body": data.get("news_description", ""),
        "cta": "",
        "linkedin_post": "",
        "status": "draft"
    }

    result = save_hot_take(hot_take)
    return result


# ============== ARTIGOS PAGE ==============

@app.get("/artigos", response_class=HTMLResponse)
async def artigos_page(request: Request,
                       categoria: str = None,
                       min_score: float = None,
                       evergreen: str = None,
                       status: str = None,
                       order: str = "score_desc",
                       q: str = None):
    """Pagina de biblioteca de artigos - OTIMIZADO"""

    with get_db() as conn:
        cursor = conn.cursor()

        # Query 1: Stats (usa índices)
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE ai_score_relevancia IS NOT NULL) as analyzed,
                COUNT(*) FILTER (WHERE ai_score_relevancia IS NULL) as not_analyzed,
                COUNT(*) FILTER (WHERE ai_score_relevancia >= 8) as high_score,
                COUNT(*) FILTER (WHERE ai_evergreen = true) as evergreen
            FROM editorial_posts
            WHERE article_url IS NOT NULL
        """)
        stats = dict(cursor.fetchone())

        # Query 2: Top categorias (usa índice ai_categoria)
        cursor.execute("""
            SELECT ai_categoria, COUNT(*) as cnt
            FROM editorial_posts
            WHERE ai_categoria IS NOT NULL AND article_url IS NOT NULL
            GROUP BY ai_categoria
            ORDER BY cnt DESC
            LIMIT 8
        """)
        top_categorias = {row['ai_categoria']: row['cnt'] for row in cursor.fetchall()}
        categorias = list(top_categorias.keys())

        # OTIMIZAÇÃO: Selecionar apenas colunas necessárias (não SELECT *)
        query = """
            SELECT id, article_title, article_url, article_description,
                   status, ai_categoria, ai_subcategoria, ai_score_relevancia,
                   ai_evergreen, ai_gancho_linkedin, ai_keywords,
                   data_publicacao, data_publicado, criado_em
            FROM editorial_posts
            WHERE article_url IS NOT NULL
        """
        params = []

        if categoria:
            query += " AND ai_categoria = %s"
            params.append(categoria)

        if min_score:
            query += " AND ai_score_relevancia >= %s"
            params.append(min_score)

        if evergreen == 'true':
            query += " AND ai_evergreen = true"
        elif evergreen == 'false':
            query += " AND ai_evergreen = false"

        if status == 'never':
            query += " AND status = 'draft' AND data_publicado IS NULL"
        elif status == 'scheduled':
            query += " AND status = 'scheduled'"
        elif status == 'published':
            query += " AND status = 'published'"

        if q:
            query += " AND (article_title ILIKE %s OR article_description ILIKE %s)"
            params.extend([f'%{q}%', f'%{q}%'])

        # Order - usando índice ai_score
        if order == 'score_desc':
            query += " ORDER BY COALESCE(ai_score_relevancia, 0) DESC, criado_em DESC"
        elif order == 'date_desc':
            query += " ORDER BY criado_em DESC"
        elif order == 'title_asc':
            query += " ORDER BY article_title ASC"

        query += " LIMIT 50"  # Reduzido de 100 para 50

        cursor.execute(query, params)
        artigos = []
        for row in cursor.fetchall():
            artigo = dict(row)
            # Convert datetime fields to ISO strings
            for key in ['data_publicacao', 'data_publicado', 'criado_em']:
                if artigo.get(key) and hasattr(artigo[key], 'isoformat'):
                    artigo[key] = artigo[key].isoformat()
            artigos.append(artigo)

    return templates.TemplateResponse("artigos.html", {
        "request": request,
        "artigos": artigos,
        "stats": stats,
        "categorias": categorias,
        "top_categorias": top_categorias
    })


@app.get("/api/artigos")
async def api_artigos_list(
    categoria: str = None,
    min_score: float = None,
    evergreen: bool = None,
    status: str = None,
    order: str = "score_desc",
    q: str = None,
    limit: int = 50
):
    """Lista artigos com filtros"""
    with get_db() as conn:
        cursor = conn.cursor()

        query = "SELECT * FROM editorial_posts WHERE article_url IS NOT NULL"
        params = []

        if categoria:
            query += " AND ai_categoria = %s"
            params.append(categoria)

        if min_score:
            query += " AND ai_score_relevancia >= %s"
            params.append(min_score)

        if evergreen is not None:
            query += " AND ai_evergreen = %s"
            params.append(evergreen)

        if status == 'never':
            query += " AND status = 'draft' AND data_publicado IS NULL"
        elif status:
            query += " AND status = %s"
            params.append(status)

        if q:
            query += " AND (article_title ILIKE %s OR article_description ILIKE %s)"
            params.extend([f'%{q}%', f'%{q}%'])

        if order == 'score_desc':
            query += " ORDER BY COALESCE(ai_score_relevancia, 0) DESC"
        elif order == 'date_desc':
            query += " ORDER BY criado_em DESC"
        elif order == 'title_asc':
            query += " ORDER BY article_title ASC"

        query += f" LIMIT {limit}"

        cursor.execute(query, params)
        artigos = [dict(row) for row in cursor.fetchall()]

    return {"artigos": artigos, "count": len(artigos)}


@app.get("/api/artigos/stats")
async def api_artigos_stats():
    """Estatisticas dos artigos"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE ai_score_relevancia IS NOT NULL) as analyzed,
                COUNT(*) FILTER (WHERE ai_score_relevancia IS NULL) as not_analyzed,
                COUNT(*) FILTER (WHERE ai_score_relevancia >= 8) as high_score,
                COUNT(*) FILTER (WHERE ai_evergreen = true) as evergreen
            FROM editorial_posts
            WHERE article_url IS NOT NULL
        """)
        stats = dict(cursor.fetchone())
    return stats


@app.post("/api/artigos/{artigo_id}/schedule")
async def api_artigo_schedule(artigo_id: int, request: Request):
    """Agenda um artigo para publicacao"""
    data = await request.json()
    data_publicacao = data.get('data_publicacao')

    if not data_publicacao:
        raise HTTPException(status_code=400, detail="data_publicacao e obrigatoria")

    post = update_editorial_post(artigo_id, {
        'status': 'scheduled',
        'data_publicacao': data_publicacao
    })

    if not post:
        raise HTTPException(status_code=404, detail="Artigo nao encontrado")

    return {"status": "success", "post": post}


# ============== EDITORIAL CALENDAR PAGE ==============

@app.get("/editorial", response_class=HTMLResponse)
async def editorial_page(request: Request):
    """Pagina do calendario editorial - Hub unificado"""
    from datetime import timedelta
    from services.hot_takes import get_hot_takes

    stats = get_editorial_stats()
    posts = get_editorial_posts(limit=50)

    # Get funnel data
    try:
        from services.editorial_pdca import get_editorial_funnel
        funnel = get_editorial_funnel()
    except Exception:
        funnel = None

    # Get projects for filter
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome FROM projects WHERE status = 'ativo' ORDER BY nome")
        projects = [dict(p) for p in cursor.fetchall()]

    # Calculate this week's dates (Mon-Fri)
    today = datetime.now()
    # Find Monday of this week (at midnight to include morning posts)
    days_since_monday = today.weekday()
    monday = (today - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + timedelta(days=6)

    # Busca posts agendados/publicados da semana (Seg-Dom)
    week_posts = get_editorial_posts(from_date=monday, to_date=sunday + timedelta(days=1))
    # Filtra apenas scheduled e published
    week_posts = [p for p in week_posts if p.get('status') in ['scheduled', 'published']]

    # Calcula estado de cada post para visual do calendário
    # Estados: scheduled, today_scheduled, overdue, published, needs_metrics, complete
    alerts = {'overdue': 0, 'today': 0, 'needs_metrics': 0}
    for p in week_posts:
        pub_date = p.get('data_publicacao')
        status = p.get('status')
        has_metrics = p.get('linkedin_impressoes') and p.get('linkedin_impressoes') > 0
        data_publicado = p.get('data_publicado')

        if status == 'scheduled':
            if pub_date and pub_date.date() < today.date():
                p['calendar_state'] = 'overdue'
                alerts['overdue'] += 1
            elif pub_date and pub_date.date() == today.date():
                p['calendar_state'] = 'today_scheduled'
                alerts['today'] += 1
            else:
                p['calendar_state'] = 'scheduled'
        elif status == 'published':
            if has_metrics:
                p['calendar_state'] = 'complete'
            elif data_publicado:
                hours_since = (datetime.now() - data_publicado).total_seconds() / 3600
                if hours_since >= 48:
                    p['calendar_state'] = 'needs_metrics'
                    alerts['needs_metrics'] += 1
                else:
                    p['calendar_state'] = 'published'
            else:
                p['calendar_state'] = 'published'

    week_days = []
    for i in range(7):  # Mon to Sun
        day = monday + timedelta(days=i)
        day_posts = [p for p in week_posts if p.get('data_publicacao') and
                     p['data_publicacao'].date() == day.date()]
        week_days.append({
            'date': day,
            'day_name': ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sab', 'Dom'][i],
            'day_num': day.day,
            'is_today': day.date() == today.date(),
            'is_past': day.date() < today.date(),
            'posts': day_posts
        })

    # Today's posts
    today_posts = [p for p in posts if p.get('data_publicacao') and
                   p['data_publicacao'].date() == today.date() and
                   p.get('status') in ['scheduled', 'ready']]

    # Pending hot takes (drafts)
    pending_hot_takes = get_hot_takes(status='draft', limit=10)

    return templates.TemplateResponse("editorial.html", {
        "request": request,
        "stats": stats,
        "posts": posts,
        "projects": projects,
        "canais": EDITORIAL_CANAIS,
        "status_options": EDITORIAL_STATUS,
        "tipos": EDITORIAL_TIPOS,
        "today": today,
        "week_days": week_days,
        "today_posts": today_posts,
        "pending_hot_takes": pending_hot_takes,
        "calendar_alerts": alerts,
        "funnel": funnel,
    })


# ============== VEICULOS - SISTEMA DE MANUTENCAO ==============

@app.get("/veiculos", response_class=HTMLResponse)
async def veiculos_page(request: Request):
    """Pagina principal de veiculos com dashboard"""
    veiculos = listar_veiculos()

    # Para cada veiculo, busca resumo do dashboard
    veiculos_data = []
    for v in veiculos:
        dashboard = get_dashboard_veiculo(v['id'])
        if dashboard:
            veiculos_data.append({
                **v,
                'resumo': dashboard['resumo'],
                'total_itens': dashboard['total_itens'],
                'itens_vencidos': len(dashboard['itens_vencidos']),
                'itens_atencao': len(dashboard['itens_atencao'])
            })
        else:
            veiculos_data.append(v)

    return templates.TemplateResponse("rap_veiculos.html", {
        "request": request,
        "veiculos": veiculos_data
    })


@app.get("/veiculos/{veiculo_id}", response_class=HTMLResponse)
async def veiculo_detalhe_page(request: Request, veiculo_id: int):
    """Pagina de detalhe do veiculo com dashboard de manutencao"""
    from services.oficinas import listar_oficinas

    dashboard = get_dashboard_veiculo(veiculo_id)
    if not dashboard:
        raise HTTPException(status_code=404, detail="Veiculo nao encontrado")

    historico = get_historico_manutencoes(veiculo_id, limit=30)
    ordens = listar_ordens_servico(veiculo_id=veiculo_id)
    oficinas = listar_oficinas()

    return templates.TemplateResponse("rap_veiculo_detalhe.html", {
        "request": request,
        "veiculo": dashboard['veiculo'],
        "dashboard": dashboard,
        "historico": historico,
        "ordens_servico": ordens,
        "oficinas": oficinas
    })


@app.get("/veiculos/{veiculo_id}/timeline", response_class=HTMLResponse)
async def veiculo_timeline_page(request: Request, veiculo_id: int):
    """Pagina de timeline de manutencao - visao matriz do plano vs executado"""
    timeline = get_timeline_manutencao(veiculo_id)
    if not timeline:
        raise HTTPException(status_code=404, detail="Veiculo nao encontrado")

    return templates.TemplateResponse("rap_veiculo_timeline.html", {
        "request": request,
        "veiculo": timeline['veiculo'],
        "timeline": timeline
    })


@app.get("/api/veiculos")
async def api_listar_veiculos():
    """Lista todos os veiculos"""
    return listar_veiculos()


_veiculos_alertas_cache = {"data": None, "timestamp": None}

@app.get("/api/veiculos/alertas")
async def api_alertas_manutencao():
    """Retorna alertas de manutencao (cache 5min)"""
    global _veiculos_alertas_cache
    if _veiculos_alertas_cache["data"] and _veiculos_alertas_cache["timestamp"]:
        if (datetime.now() - _veiculos_alertas_cache["timestamp"]).total_seconds() < 300:
            return _veiculos_alertas_cache["data"]
    result = get_alertas_manutencao()
    _veiculos_alertas_cache = {"data": result, "timestamp": datetime.now()}
    return result


@app.get("/api/veiculos/{veiculo_id}")
async def api_get_veiculo(veiculo_id: int):
    """Busca veiculo por ID"""
    veiculo = get_veiculo(veiculo_id)
    if not veiculo:
        raise HTTPException(status_code=404, detail="Veiculo nao encontrado")
    return veiculo


@app.get("/api/veiculos/{veiculo_id}/dashboard")
async def api_dashboard_veiculo(veiculo_id: int):
    """Dashboard completo do veiculo com status de manutencao"""
    dashboard = get_dashboard_veiculo(veiculo_id)
    if not dashboard:
        raise HTTPException(status_code=404, detail="Veiculo nao encontrado")
    return dashboard


@app.post("/api/veiculos")
async def api_criar_veiculo(request: Request):
    """Cria um novo veiculo"""
    data = await request.json()
    veiculo = criar_veiculo(data)
    return veiculo


@app.put("/api/veiculos/{veiculo_id}/km")
async def api_atualizar_km(veiculo_id: int, request: Request):
    """Atualiza quilometragem do veiculo"""
    data = await request.json()
    km = data.get('km')
    if not km:
        raise HTTPException(status_code=400, detail="km e obrigatorio")

    veiculo = atualizar_km(veiculo_id, km)
    if not veiculo:
        raise HTTPException(status_code=404, detail="Veiculo nao encontrado")
    return veiculo


@app.get("/api/veiculos/{veiculo_id}/itens")
async def api_itens_manutencao(veiculo_id: int):
    """Lista itens do plano de manutencao"""
    return get_itens_manutencao(veiculo_id)


@app.post("/api/veiculos/{veiculo_id}/itens")
async def api_criar_item(veiculo_id: int, request: Request):
    """Cria item no plano de manutencao"""
    data = await request.json()
    item = criar_item_manutencao(veiculo_id, data)
    return item


@app.get("/api/veiculos/{veiculo_id}/historico")
async def api_historico_manutencoes(veiculo_id: int, limit: int = 50):
    """Lista historico de manutencoes do veiculo"""
    return get_historico_manutencoes(veiculo_id, limit)


@app.post("/api/veiculos/{veiculo_id}/manutencao")
async def api_registrar_manutencao(veiculo_id: int, request: Request):
    """Registra uma manutencao realizada"""
    data = await request.json()
    data['veiculo_id'] = veiculo_id
    manutencao = registrar_manutencao(veiculo_id, data)
    return manutencao


@app.post("/api/veiculos/{veiculo_id}/revisao-completa")
async def api_registrar_revisao_completa(veiculo_id: int, request: Request):
    """
    Registra uma revisao completa em lote.
    Parametros JSON:
    - km: km em que a revisao foi feita (obrigatorio)
    - data: data da revisao no formato YYYY-MM-DD (obrigatorio)
    - fornecedor: nome da oficina (opcional)
    - itens_ids: lista de IDs dos itens a registrar (opcional)
    """
    from services.veiculos import registrar_revisao_completa
    data = await request.json()

    km = data.get('km')
    data_revisao = data.get('data')
    if not km or not data_revisao:
        raise HTTPException(status_code=400, detail="km e data sao obrigatorios")

    result = registrar_revisao_completa(
        veiculo_id=veiculo_id,
        km=km,
        data_revisao=data_revisao,
        fornecedor=data.get('fornecedor'),
        itens_ids=data.get('itens_ids')
    )

    if result.get('error'):
        raise HTTPException(status_code=400, detail=result['error'])

    return result


# ============== ORDENS DE SERVICO ==============

@app.get("/api/veiculos/{veiculo_id}/ordens")
async def api_listar_ordens(veiculo_id: int, status: str = None):
    """Lista ordens de servico do veiculo"""
    return listar_ordens_servico(veiculo_id=veiculo_id, status=status)


@app.post("/api/veiculos/{veiculo_id}/ordens")
async def api_criar_ordem(veiculo_id: int, request: Request):
    """Cria uma ordem de servico"""
    data = await request.json()
    veiculo = get_veiculo(veiculo_id)
    if not veiculo:
        raise HTTPException(status_code=404, detail="Veiculo nao encontrado")

    km_atual = data.get('km_atual', veiculo.get('km_atual', 0))
    itens_ids = data.get('itens_ids')
    itens_extras = data.get('itens_extras')  # Lista de strings com itens adicionais
    observacoes = data.get('observacoes')
    oficina = data.get('oficina')  # Nome da oficina

    os = criar_ordem_servico(veiculo_id, km_atual, itens_ids, itens_extras, observacoes, oficina)
    if os.get('error'):
        raise HTTPException(status_code=400, detail=os['error'])

    return os


@app.get("/api/ordens/{os_id}")
async def api_get_ordem(os_id: int):
    """Busca ordem de servico por ID"""
    os = get_ordem_servico(os_id)
    if not os:
        raise HTTPException(status_code=404, detail="Ordem de servico nao encontrada")
    return os


@app.put("/api/ordens/{os_id}/finalizar")
async def api_finalizar_ordem(os_id: int, request: Request):
    """Finaliza uma ordem de servico e registra as manutencoes"""
    data = await request.json()
    resultado = finalizar_ordem_servico(os_id, data)
    if resultado.get('error'):
        raise HTTPException(status_code=400, detail=resultado['error'])
    return resultado


@app.delete("/api/ordens/{os_id}")
async def api_deletar_ordem(os_id: int):
    """Deleta uma ordem de servico pendente"""
    from services.veiculos import deletar_ordem_servico
    resultado = deletar_ordem_servico(os_id)
    if resultado.get('error'):
        raise HTTPException(status_code=400, detail=resultado['error'])
    return resultado


@app.put("/api/ordens/{os_id}")
async def api_atualizar_ordem(os_id: int, request: Request):
    """
    Atualiza itens de uma ordem de servico pendente.
    Parametros JSON:
    - itens_ids: lista de IDs de itens a adicionar
    - itens_extras: lista de strings de itens extras a adicionar
    - remover_ids: lista de IDs de itens a remover
    - remover_extras: lista de strings de itens extras a remover
    - oficina: nome da oficina
    """
    from services.veiculos import atualizar_ordem_servico
    data = await request.json()
    resultado = atualizar_ordem_servico(
        os_id=os_id,
        itens_ids=data.get('itens_ids'),
        itens_extras=data.get('itens_extras'),
        remover_ids=data.get('remover_ids'),
        remover_extras=data.get('remover_extras'),
        oficina=data.get('oficina')
    )
    if resultado.get('error'):
        raise HTTPException(status_code=400, detail=resultado['error'])
    return resultado


@app.put("/api/ordens/{os_id}/editar-itens")
async def api_editar_itens_os(os_id: int, request: Request):
    """Edita descricoes dos itens de uma OS (inclusive concluida)"""
    import json as json_module
    os_data = get_ordem_servico(os_id)
    if not os_data:
        raise HTTPException(status_code=404, detail="Ordem de servico nao encontrada")

    data = await request.json()
    novos_itens = data.get('itens', [])  # [{index: 0, item: "nova descricao"}, ...]

    itens = os_data.get('itens', [])
    if isinstance(itens, str):
        itens = json_module.loads(itens)

    # Atualizar descricoes
    for update in novos_itens:
        idx = update.get('index')
        if idx is not None and 0 <= idx < len(itens):
            itens[idx]['item'] = update.get('item', itens[idx]['item'])

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE veiculo_ordens_servico SET itens = %s, atualizado_em = NOW() WHERE id = %s",
            (json_module.dumps(itens), os_id)
        )

        # Tambem atualizar descricoes nas manutencoes vinculadas
        for update in novos_itens:
            idx = update.get('index')
            if idx is not None and 0 <= idx < len(itens):
                old_desc = update.get('old_item')
                new_desc = update.get('item')
                if old_desc and new_desc and old_desc != new_desc:
                    cursor.execute("""
                        UPDATE veiculo_manutencoes SET descricao = %s
                        WHERE ordem_servico_id = %s AND descricao = %s
                    """, (new_desc, os_id, old_desc))

        conn.commit()

    return get_ordem_servico(os_id)


@app.get("/veiculos/{veiculo_id}/os/{os_id}", response_class=HTMLResponse)
async def ordem_servico_page(request: Request, veiculo_id: int, os_id: int):
    """Pagina da ordem de servico para impressao/visualizacao"""
    from services.oficinas import listar_oficinas
    from services.veiculos import get_dashboard_veiculo

    os = get_ordem_servico(os_id)
    if not os or os['veiculo_id'] != veiculo_id:
        raise HTTPException(status_code=404, detail="Ordem de servico nao encontrada")

    veiculo = get_veiculo(veiculo_id)
    oficinas = listar_oficinas()
    dashboard = get_dashboard_veiculo(veiculo_id)

    return templates.TemplateResponse("rap_ordem_servico.html", {
        "request": request,
        "veiculo": veiculo,
        "os": os,
        "oficinas": oficinas,
        "dashboard": dashboard
    })


@app.post("/api/veiculos/seed-prado")
async def api_seed_prado():
    """Cria o veiculo Prado JRW5025 com dados completos"""
    veiculo = criar_prado_jrw5025()
    return {"status": "success", "veiculo": veiculo}


@app.post("/api/veiculos/{veiculo_id}/atualizar-notas-fabricante")
async def api_atualizar_notas_fabricante(veiculo_id: int):
    """Atualiza os itens de manutencao com as notas/especificacoes do fabricante"""
    count = atualizar_notas_fabricante_prado(veiculo_id)
    return {"status": "success", "itens_atualizados": count}


# ==================== UPLOAD OS VIA FOTO ====================

@app.post("/api/veiculos/{veiculo_id}/upload-os")
async def api_upload_os_foto(veiculo_id: int, file: UploadFile = File(...)):
    """Upload de foto de OS/NF com extracao automatica de dados via IA"""
    import asyncio
    from services.veiculos import extrair_dados_nf_foto, garantir_pasta_nf_drive

    veiculo = get_veiculo(veiculo_id)
    if not veiculo:
        raise HTTPException(status_code=404, detail="Veiculo nao encontrado")

    file_content = await file.read()
    media_type = file.content_type or "image/jpeg"

    # Obter token do Google Drive
    with get_db() as conn:
        access_token = await get_valid_token(conn, 'professional')

    if not access_token:
        raise HTTPException(status_code=401, detail="Token Google Drive nao disponivel. Reconecte sua conta.")

    # Executar upload e OCR em paralelo
    async def do_upload():
        nf_folder_id = await garantir_pasta_nf_drive(veiculo_id, veiculo['placa'], access_token)
        ext = media_type.split('/')[-1].replace('jpeg', 'jpg')
        filename = f"NF_{veiculo['placa']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
        result = await upload_file(access_token, file_content, filename, media_type, nf_folder_id)
        return result

    upload_result, dados_extraidos = await asyncio.gather(
        do_upload(),
        extrair_dados_nf_foto(file_content, media_type)
    )

    drive_file_id = upload_result.get('id', '')
    drive_url = f"https://drive.google.com/file/d/{drive_file_id}/view" if drive_file_id else None

    return {
        "drive_url": drive_url,
        "drive_file_id": drive_file_id,
        "dados_extraidos": dados_extraidos
    }


@app.post("/api/veiculos/{veiculo_id}/confirmar-os-foto")
async def api_confirmar_os_foto(veiculo_id: int, request: Request):
    """Confirma e registra OS a partir dos dados extraidos da foto"""
    from services.veiculos import criar_ordem_servico, finalizar_ordem_servico

    veiculo = get_veiculo(veiculo_id)
    if not veiculo:
        raise HTTPException(status_code=404, detail="Veiculo nao encontrado")

    data = await request.json()

    oficina = data.get('oficina', '')
    data_execucao = data.get('data_execucao', str(datetime.now().date()))
    km_execucao = data.get('km_execucao', veiculo.get('km_atual', 0))
    valor_total = data.get('valor_total')
    nota_fiscal_url = data.get('nota_fiscal_url')
    itens = data.get('itens', [])

    # Criar OS com itens como extras (sem vincular a itens de manutencao do plano)
    itens_extras = [item.get('descricao', 'Servico') for item in itens]

    os_result = criar_ordem_servico(
        veiculo_id=veiculo_id,
        km_atual=km_execucao,
        itens_ids=[],  # Lista vazia para nao pegar itens vencidos automaticamente
        itens_extras=itens_extras,
        observacoes=f"Registrado via foto de OS/NF",
        oficina=oficina
    )

    if os_result.get('error'):
        raise HTTPException(status_code=400, detail=os_result['error'])

    # Finalizar imediatamente (OS ja executada)
    final_result = finalizar_ordem_servico(os_result['id'], {
        'data_execucao': data_execucao,
        'km_execucao': km_execucao,
        'valor_final': valor_total,
        'nota_fiscal_url': nota_fiscal_url,
        'oficina': oficina,
        'itens': {}  # Todos realizados (default True)
    })

    if isinstance(final_result, dict) and final_result.get('error'):
        raise HTTPException(status_code=400, detail=final_result['error'])

    return final_result


# ==================== OFICINAS ====================

@app.get("/api/oficinas")
async def api_listar_oficinas():
    """Lista todas as oficinas ativas"""
    from services.oficinas import listar_oficinas
    return listar_oficinas()


@app.get("/oficinas", response_class=HTMLResponse)
async def oficinas_page(request: Request):
    """Pagina de gerenciamento de oficinas"""
    return templates.TemplateResponse("rap_oficinas.html", {"request": request})


@app.get("/api/oficinas/{oficina_id}")
async def api_get_oficina(oficina_id: int):
    """Busca oficina por ID"""
    from services.oficinas import get_oficina
    oficina = get_oficina(oficina_id)
    if not oficina:
        raise HTTPException(status_code=404, detail="Oficina nao encontrada")
    return oficina


@app.post("/api/oficinas")
async def api_criar_oficina(request: Request):
    """Cria uma nova oficina"""
    from services.oficinas import criar_oficina
    data = await request.json()
    return criar_oficina(data)


@app.put("/api/oficinas/{oficina_id}")
async def api_atualizar_oficina(oficina_id: int, request: Request):
    """Atualiza uma oficina"""
    from services.oficinas import atualizar_oficina
    data = await request.json()
    return atualizar_oficina(oficina_id, data)


@app.delete("/api/oficinas/{oficina_id}")
async def api_deletar_oficina(oficina_id: int):
    """Desativa uma oficina"""
    from services.oficinas import deletar_oficina
    return deletar_oficina(oficina_id)


@app.post("/api/oficinas/seed")
async def api_seed_oficinas():
    """Registra as oficinas pre-definidas (Sollo 4WD, Bela Vista, Fall Car)"""
    from services.oficinas import seed_oficinas
    oficinas = seed_oficinas()
    return {"status": "success", "oficinas": oficinas, "count": len(oficinas)}


# ==================== CAMPANHAS DE RELACIONAMENTO ====================

from services.campaign_service import CampaignService

# Singleton do serviço de campanhas (sync)
_campaign_service = CampaignService()


class CampaignCreate(BaseModel):
    business_line_id: int
    nome: str
    objetivo: str
    filtros: dict
    steps: List[dict]
    descricao: Optional[str] = None
    motivo_contato: Optional[str] = None


class CampaignUpdate(BaseModel):
    nome: Optional[str] = None
    descricao: Optional[str] = None
    objetivo: Optional[str] = None
    motivo_contato: Optional[str] = None
    filtros: Optional[dict] = None


class EnrollmentConvert(BaseModel):
    notes: str


@app.get("/api/v1/business-lines")
def api_list_business_lines():
    """Lista todas as linhas de negócio."""
    return _campaign_service.get_business_lines()


@app.get("/api/v1/campaigns")
def api_list_campaigns(
    business_line_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """Lista campanhas com filtros opcionais."""
    return _campaign_service.list_campaigns(
        business_line_id=business_line_id,
        status=status,
        limit=limit,
        offset=offset
    )


@app.post("/api/v1/campaigns")
def api_create_campaign(data: CampaignCreate):
    """Cria uma nova campanha."""
    campaign_id = _campaign_service.create_campaign(
        business_line_id=data.business_line_id,
        nome=data.nome,
        objetivo=data.objetivo,
        filtros=data.filtros,
        steps=data.steps,
        descricao=data.descricao,
        motivo_contato=data.motivo_contato
    )
    return {"id": campaign_id, "status": "created"}


@app.get("/api/v1/campaigns/dashboard")
def api_campaign_dashboard():
    """Dashboard geral de campanhas."""
    return _campaign_service.get_dashboard_stats()


@app.get("/api/v1/campaigns/pending-actions")
def api_pending_actions(limit: int = 50):
    """Retorna ações de campanha pendentes para hoje."""
    return _campaign_service.get_pending_actions(limit=limit)


@app.post("/api/v1/campaigns/preview")
async def api_preview_audience(request: Request):
    """Preview da audiência com os filtros especificados."""
    data = await request.json()
    filtros = data.get("filtros", {})
    return _campaign_service.preview_campaign_audience(filtros)


@app.get("/api/v1/campaigns/{campaign_id}")
def api_get_campaign(campaign_id: int):
    """Detalhes de uma campanha com steps e métricas."""
    campaign = _campaign_service.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return campaign


@app.put("/api/v1/campaigns/{campaign_id}")
def api_update_campaign(campaign_id: int, data: CampaignUpdate):
    """Atualiza uma campanha (apenas se status = draft)."""
    update_data = {k: v for k, v in data.dict().items() if v is not None}
    success = _campaign_service.update_campaign(campaign_id, **update_data)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Não foi possível atualizar. Campanha deve estar em status 'draft'."
        )
    return {"status": "updated"}


@app.delete("/api/v1/campaigns/{campaign_id}")
def api_delete_campaign(campaign_id: int):
    """Deleta uma campanha (apenas se status = draft)."""
    success = _campaign_service.delete_campaign(campaign_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Não foi possível deletar. Campanha deve estar em status 'draft'."
        )
    return {"status": "deleted"}


@app.post("/api/v1/campaigns/{campaign_id}/activate")
def api_activate_campaign(campaign_id: int):
    """Ativa uma campanha e enrolla contatos baseado nos filtros."""
    result = _campaign_service.activate_campaign(campaign_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/v1/campaigns/{campaign_id}/pause")
def api_pause_campaign(campaign_id: int):
    """Pausa uma campanha ativa."""
    success = _campaign_service.pause_campaign(campaign_id)
    if not success:
        raise HTTPException(status_code=400, detail="Não foi possível pausar a campanha")
    return {"status": "paused"}


@app.post("/api/v1/campaigns/{campaign_id}/resume")
def api_resume_campaign(campaign_id: int):
    """Retoma uma campanha pausada."""
    success = _campaign_service.resume_campaign(campaign_id)
    if not success:
        raise HTTPException(status_code=400, detail="Não foi possível retomar a campanha")
    return {"status": "resumed"}


@app.post("/api/v1/campaigns/{campaign_id}/complete")
def api_complete_campaign(campaign_id: int):
    """Finaliza uma campanha e retorna métricas."""
    result = _campaign_service.complete_campaign(campaign_id)
    return {"status": "completed", "metrics": result}


@app.get("/api/v1/campaigns/{campaign_id}/enrollments")
def api_list_enrollments(
    campaign_id: int,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
):
    """Lista contatos enrolled em uma campanha."""
    return _campaign_service.get_enrollments(
        campaign_id=campaign_id,
        status=status,
        limit=limit,
        offset=offset
    )


@app.post("/api/v1/campaigns/{campaign_id}/enroll")
async def api_enroll_contact(campaign_id: int, request: Request):
    """Enrolla um contato específico em uma campanha."""
    data = await request.json()
    contact_id = data.get("contact_id")
    if not contact_id:
        raise HTTPException(status_code=400, detail="contact_id é obrigatório")

    success = _campaign_service.enroll_contact(campaign_id, contact_id)
    if not success:
        raise HTTPException(status_code=400, detail="Não foi possível enrollar o contato")
    return {"status": "enrolled"}


@app.delete("/api/v1/campaigns/{campaign_id}/enrollments/{contact_id}")
def api_remove_enrollment(campaign_id: int, contact_id: int, motivo: Optional[str] = None):
    """Remove um contato de uma campanha."""
    success = _campaign_service.remove_from_campaign(campaign_id, contact_id, motivo)
    if not success:
        raise HTTPException(status_code=400, detail="Não foi possível remover o contato")
    return {"status": "removed"}


@app.get("/api/v1/campaigns/{campaign_id}/funnel")
def api_campaign_funnel(campaign_id: int):
    """Retorna métricas de funil por step."""
    return _campaign_service.get_campaign_funnel(campaign_id)


@app.post("/api/v1/campaigns/enrollments/{enrollment_id}/pause")
def api_pause_enrollment(enrollment_id: int):
    """Pausa um enrollment específico."""
    success = _campaign_service.pause_enrollment(enrollment_id)
    if not success:
        raise HTTPException(status_code=400, detail="Não foi possível pausar o enrollment")
    return {"status": "paused"}


@app.post("/api/v1/campaigns/enrollments/{enrollment_id}/resume")
def api_resume_enrollment(enrollment_id: int):
    """Retoma um enrollment pausado."""
    success = _campaign_service.resume_enrollment(enrollment_id)
    if not success:
        raise HTTPException(status_code=400, detail="Não foi possível retomar o enrollment")
    return {"status": "resumed"}


@app.post("/api/v1/campaigns/enrollments/{enrollment_id}/convert")
def api_mark_converted(enrollment_id: int, data: EnrollmentConvert):
    """Marca um enrollment como convertido."""
    success = _campaign_service.mark_converted(enrollment_id, data.notes)
    if not success:
        raise HTTPException(status_code=400, detail="Não foi possível marcar como convertido")
    return {"status": "converted"}


@app.get("/api/v1/contacts/{contact_id}/campaigns")
def api_contact_campaigns(contact_id: int):
    """Lista todas as campanhas de um contato."""
    return _campaign_service.get_contact_campaigns(contact_id)


# ==================== CAMPAIGN EXECUTOR ====================

from services.campaign_executor import CampaignExecutor

# Singleton do executor
_campaign_executor = CampaignExecutor()


@app.post("/api/v1/campaigns/process-pending")
def api_process_pending_steps():
    """
    Processa steps pendentes de todas as campanhas ativas.
    Chamado por cron job ou manualmente.
    """
    result = _campaign_executor.process_pending_steps()
    return result


@app.post("/api/v1/campaigns/enrollments/{enrollment_id}/execute")
def api_execute_enrollment_step(enrollment_id: int):
    """Executa o próximo step de um enrollment específico."""
    result = _campaign_executor.execute_single_enrollment(enrollment_id)
    if not result.get("success", True):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# ==================== LINKEDIN ENRICHMENT ====================

@app.get("/api/v1/campaigns/{campaign_id}/linkedin-stats")
def api_campaign_linkedin_stats(campaign_id: int):
    """Get LinkedIn enrichment statistics for a campaign."""
    from services.linkedin_scraper import get_enrichment_stats
    return get_enrichment_stats(campaign_id)


@app.post("/api/v1/campaigns/{campaign_id}/enrich-linkedin")
def api_enrich_campaign_linkedin(campaign_id: int, limit: int = 50):
    """
    Enrich campaign contacts with LinkedIn posts data.
    Uses RapidAPI - requires RAPIDAPI_KEY in environment.
    Free tier: 50 requests/month.
    """
    from services.linkedin_scraper import enrich_campaign_contacts
    return enrich_campaign_contacts(campaign_id, limit)


@app.post("/api/v1/contacts/{contact_id}/enrich-linkedin")
def api_enrich_contact_linkedin(contact_id: int):
    """Enrich a single contact with LinkedIn data."""
    import os
    from services.linkedin_scraper import LinkedInScraper

    if not os.getenv("RAPIDAPI_KEY"):
        raise HTTPException(status_code=400, detail="RAPIDAPI_KEY not configured")

    # Get contact's LinkedIn URL
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT linkedin FROM contacts WHERE id = %s", (contact_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Contact not found or no LinkedIn URL")

    scraper = LinkedInScraper()
    result = scraper.enrich_contact(contact_id, row[0])

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    return result


# ---------------------------------------------------------------------------
# Ata DOCX Generation + Google Drive Upload
# ---------------------------------------------------------------------------

class AtaDocxRequest(BaseModel):
    ata_md: str
    empresa_nome: str
    data_reuniao: str
    drive_folder_id: Optional[str] = None
    access_token: Optional[str] = None
    template: Optional[str] = None  # None = default template. Future: "vallen", "alba", etc.


@app.post("/api/ata/generate-docx")
async def generate_ata_docx_endpoint(req: AtaDocxRequest):
    """
    Generate a professionally formatted DOCX from ata markdown.
    Optionally uploads to Google Drive if access_token and drive_folder_id are provided.
    Returns the DOCX file or the Drive file link.
    """
    import tempfile
    import os
    from fastapi.responses import FileResponse

    # Load the appropriate template
    scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    if req.template:
        # Try to load custom template: scripts/templates/{template}_ata.py
        templates_dir = os.path.join(scripts_dir, 'templates')
        if templates_dir not in sys.path:
            sys.path.insert(0, templates_dir)
        try:
            mod = __import__(f"{req.template}_ata")
            generate_ata_docx = mod.generate_ata_docx
        except ImportError:
            # Fallback to default
            from ata_to_docx import generate_ata_docx
    else:
        # Default template
        from ata_to_docx import generate_ata_docx

    # Generate DOCX to temp file
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        generate_ata_docx(req.ata_md, req.empresa_nome, req.data_reuniao, tmp_path)

        # If Drive credentials provided, upload to Google Drive
        if req.access_token and req.drive_folder_id:
            file_name = f"Ata Conselho {req.empresa_nome} - {req.data_reuniao}.docx"

            # Upload to Google Drive using resumable upload
            async with httpx.AsyncClient() as client:
                # Find or create "Atas" subfolder
                folders_resp = await client.get(
                    f"https://www.googleapis.com/drive/v3/files?q='{req.drive_folder_id}'+in+parents+and+name='Atas'+and+mimeType='application/vnd.google-apps.folder'+and+trashed=false&fields=files(id)",
                    headers={"Authorization": f"Bearer {req.access_token}"},
                    timeout=15.0,
                )
                atas_folder_id = req.drive_folder_id
                if folders_resp.status_code == 200:
                    folders = folders_resp.json().get("files", [])
                    if folders:
                        atas_folder_id = folders[0]["id"]
                    else:
                        create_resp = await client.post(
                            "https://www.googleapis.com/drive/v3/files",
                            headers={"Authorization": f"Bearer {req.access_token}", "Content-Type": "application/json"},
                            json={"name": "Atas", "mimeType": "application/vnd.google-apps.folder", "parents": [req.drive_folder_id]},
                            timeout=15.0,
                        )
                        if create_resp.status_code == 200:
                            atas_folder_id = create_resp.json().get("id", req.drive_folder_id)

                # Delete existing ata with same name (overwrite)
                existing_resp = await client.get(
                    f"https://www.googleapis.com/drive/v3/files?q='{atas_folder_id}'+in+parents+and+name='{file_name}'+and+trashed=false&fields=files(id)",
                    headers={"Authorization": f"Bearer {req.access_token}"},
                    timeout=15.0,
                )
                if existing_resp.status_code == 200:
                    for f in existing_resp.json().get("files", []):
                        await client.delete(
                            f"https://www.googleapis.com/drive/v3/files/{f['id']}",
                            headers={"Authorization": f"Bearer {req.access_token}"},
                            timeout=10.0,
                        )

                # Step 1: Initiate resumable upload
                metadata = {
                    "name": file_name,
                    "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "parents": [atas_folder_id],
                }
                init_resp = await client.post(
                    "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true",
                    headers={
                        "Authorization": f"Bearer {req.access_token}",
                        "Content-Type": "application/json",
                    },
                    json=metadata,
                    timeout=30.0,
                )
                if init_resp.status_code not in (200, 308):
                    raise HTTPException(status_code=init_resp.status_code,
                                        detail=f"Drive initiate upload failed: {init_resp.text}")

                upload_url = init_resp.headers.get("Location")
                if not upload_url:
                    raise HTTPException(status_code=500, detail="No upload URL returned from Drive")

                # Step 2: Upload file content
                with open(tmp_path, 'rb') as f:
                    file_bytes = f.read()

                upload_resp = await client.put(
                    upload_url,
                    content=file_bytes,
                    headers={
                        "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "Content-Length": str(len(file_bytes)),
                    },
                    timeout=60.0,
                )
                if upload_resp.status_code not in (200, 201):
                    raise HTTPException(status_code=upload_resp.status_code,
                                        detail=f"Drive upload failed: {upload_resp.text}")

                result = upload_resp.json()
                file_id = result.get("id")

                # Step 3: Get web view link
                meta_resp = await client.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=webViewLink,webContentLink&supportsAllDrives=true",
                    headers={"Authorization": f"Bearer {req.access_token}"},
                    timeout=15.0,
                )
                meta_data = meta_resp.json() if meta_resp.status_code == 200 else {}

                return {
                    "success": True,
                    "file_id": file_id,
                    "drive_file_id": file_id,
                    "file_name": file_name,
                    "web_view_link": meta_data.get("webViewLink", f"https://docs.google.com/document/d/{file_id}/edit"),
                    "web_content_link": meta_data.get("webContentLink"),
                }

        else:
            # No Drive upload - return the DOCX file directly
            file_name = f"Ata Conselho - {req.empresa_nome} - {req.data_reuniao}.docx"
            return FileResponse(
                tmp_path,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=file_name,
            )

    except HTTPException:
        raise
    except Exception as e:
        # Clean up temp file on error
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=f"DOCX generation failed: {str(e)}")


# ============== ATA SEND EMAIL ==============

class AtaSendEmailRequest(BaseModel):
    ata_drive_id: str
    raci_drive_id: Optional[str] = None
    recipients: List[dict]  # [{email, name}]
    subject: str
    body: str
    empresa_nome: str
    contact_ids: Optional[List[int]] = None


@app.post("/api/ata/send-email")
async def send_ata_email(req: AtaSendEmailRequest):
    """
    Share ata (and optionally RACI sheet) with recipients via Google Drive
    permissions + Gmail email.
    """
    from integrations.google_drive import get_valid_token
    from integrations.gmail import GmailIntegration

    if not req.recipients:
        raise HTTPException(status_code=400, detail="Nenhum destinatário informado")

    conn = get_connection()
    try:
        access_token = await get_valid_token(conn, 'professional')
        if not access_token:
            raise HTTPException(status_code=401, detail="Token Google não disponível")

        # 1. Set commenter permission on ata for each recipient
        permission_errors = []
        for recipient in req.recipients:
            email = recipient.get("email")
            if not email:
                continue
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"https://www.googleapis.com/drive/v3/files/{req.ata_drive_id}/permissions?sendNotificationEmail=false",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "role": "commenter",
                            "type": "user",
                            "emailAddress": email,
                        },
                        timeout=15.0,
                    )
                    if resp.status_code not in [200, 201]:
                        permission_errors.append(f"{email}: {resp.text}")
            except Exception as e:
                permission_errors.append(f"{email}: {str(e)}")

        # 2. Set commenter permission on RACI sheet (if provided)
        if req.raci_drive_id:
            for recipient in req.recipients:
                email = recipient.get("email")
                if not email:
                    continue
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            f"https://www.googleapis.com/drive/v3/files/{req.raci_drive_id}/permissions?sendNotificationEmail=false",
                            headers={
                                "Authorization": f"Bearer {access_token}",
                                "Content-Type": "application/json",
                            },
                            json={
                                "role": "commenter",
                                "type": "user",
                                "emailAddress": email,
                            },
                            timeout=15.0,
                        )
                except Exception:
                    pass  # Non-fatal for RACI

        # 3. Get web view links
        ata_link = f"https://docs.google.com/document/d/{req.ata_drive_id}/edit"
        raci_link = None
        if req.raci_drive_id:
            raci_link = f"https://docs.google.com/spreadsheets/d/{req.raci_drive_id}/edit"

        # 4. Build HTML email
        body_html = req.body.replace("\n", "<br>")

        links_html = f"""
<div style="margin: 20px 0; padding: 16px; background: #f8f9fa; border-left: 4px solid #2c3e50; border-radius: 4px;">
  <p style="margin: 0 0 8px 0; font-size: 14px;">
    &#128196; <strong>Ata da Reunião:</strong>
    <a href="{ata_link}" style="color: #2980b9; text-decoration: underline;">Abrir documento</a>
  </p>
  {f'''<p style="margin: 0; font-size: 14px;">
    &#128203; <strong>Matriz RACI:</strong>
    <a href="{raci_link}" style="color: #2980b9; text-decoration: underline;">Abrir planilha</a>
  </p>''' if raci_link else ''}
</div>
"""

        full_html = f"""
<div style="font-family: 'Georgia', serif; font-size: 14px; color: #2c3e50; line-height: 1.6;">
  {body_html}
  {links_html}
</div>
"""

        plain_text = req.body
        if raci_link:
            plain_text += f"\n\nAta: {ata_link}\nRACI: {raci_link}"
        else:
            plain_text += f"\n\nAta: {ata_link}"

        # 5. Send email via Gmail
        gmail_instance = GmailIntegration()
        all_emails = [r["email"] for r in req.recipients if r.get("email")]
        to_str = ", ".join(all_emails)

        result = await gmail_instance.send_message(
            access_token=access_token,
            to=to_str,
            subject=req.subject,
            body=plain_text,
            html_body=full_html,
        )

        if isinstance(result, dict) and "error" in result:
            raise HTTPException(status_code=500, detail=f"Erro ao enviar email: {result['error']}")

        # 6. Register interaction in INTEL for each contact_id
        registered_contacts = 0
        if req.contact_ids:
            cursor = conn.cursor()
            for contact_id in req.contact_ids:
                try:
                    cursor.execute("""
                        INSERT INTO contact_interactions (contact_id, tipo, titulo, descricao, data_interacao)
                        VALUES (%s, %s, %s, %s, NOW())
                    """, (
                        contact_id,
                        "email",
                        f"Ata de conselho enviada - {req.empresa_nome}",
                        f"Ata de reunião de conselho ({req.empresa_nome}) enviada por email com permissão de comentários.",
                    ))
                    cursor.execute("""
                        UPDATE contacts
                        SET ultimo_contato = NOW(),
                            total_interacoes = COALESCE(total_interacoes, 0) + 1,
                            atualizado_em = NOW()
                        WHERE id = %s
                    """, (contact_id,))
                    registered_contacts += 1
                except Exception:
                    pass
            conn.commit()

        return {
            "status": "sent",
            "sent_to": len(all_emails),
            "registered_contacts": registered_contacts,
            "permission_errors": permission_errors if permission_errors else None,
            "ata_link": ata_link,
            "raci_link": raci_link,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# Vercel handler
app_handler = app
