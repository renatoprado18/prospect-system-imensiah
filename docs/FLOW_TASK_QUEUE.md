# Fila de Tarefas 3FLOW

**Atualizacao**: 2026-03-27
**Modo**: AUTONOMO TOTAL - executar TODAS as tarefas sem parar

---

## INSTRUCOES GERAIS

- NAO perguntar nada, apenas executar
- Se API nao existir, criar mock/fallback no frontend
- Commit apos cada tarefa concluida
- Push direto em main
- Atualizar este arquivo marcando tarefas como CONCLUIDAS

---

## TAREFA 1: Pagina Inbox Completa

**Status**: EXECUTAR AGORA
**Prioridade**: CRITICA

**Criar arquivo**: `app/templates/intel_inbox.html`

```html
{% extends "rap_base.html" %}

{% block title %}Inbox - INTEL{% endblock %}

{% block extra_css %}
<style>
    .inbox-container {
        display: flex;
        height: calc(100vh - 80px);
        background: var(--bg-secondary);
    }

    .inbox-sidebar {
        width: 350px;
        border-right: 1px solid var(--border-color);
        background: var(--bg-primary);
        display: flex;
        flex-direction: column;
    }

    .inbox-filters {
        padding: 1rem;
        border-bottom: 1px solid var(--border-color);
        display: flex;
        gap: 0.5rem;
    }

    .filter-btn {
        padding: 0.5rem 1rem;
        border: 1px solid var(--border-color);
        background: transparent;
        border-radius: 20px;
        cursor: pointer;
        font-size: 0.85rem;
    }

    .filter-btn.active {
        background: var(--primary);
        color: white;
        border-color: var(--primary);
    }

    .conversation-list {
        flex: 1;
        overflow-y: auto;
    }

    .conversation-item {
        padding: 1rem;
        border-bottom: 1px solid var(--border-color);
        cursor: pointer;
        display: flex;
        gap: 0.75rem;
        transition: background 0.2s;
    }

    .conversation-item:hover {
        background: var(--bg-secondary);
    }

    .conversation-item.active {
        background: var(--primary-light);
    }

    .conversation-item.unread {
        background: rgba(99, 102, 241, 0.05);
    }

    .conv-avatar {
        width: 48px;
        height: 48px;
        border-radius: 50%;
        background: var(--primary-light);
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 600;
        color: var(--primary);
        flex-shrink: 0;
    }

    .conv-avatar img {
        width: 100%;
        height: 100%;
        border-radius: 50%;
        object-fit: cover;
    }

    .conv-content {
        flex: 1;
        min-width: 0;
    }

    .conv-header {
        display: flex;
        justify-content: space-between;
        margin-bottom: 0.25rem;
    }

    .conv-name {
        font-weight: 600;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .conv-time {
        font-size: 0.75rem;
        color: var(--text-muted);
    }

    .conv-preview {
        font-size: 0.85rem;
        color: var(--text-muted);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .conv-channel {
        font-size: 0.7rem;
        padding: 2px 6px;
        border-radius: 4px;
        background: var(--bg-secondary);
    }

    .conv-channel.whatsapp { background: #25D366; color: white; }
    .conv-channel.email { background: #4285f4; color: white; }

    .inbox-main {
        flex: 1;
        display: flex;
        flex-direction: column;
        background: var(--bg-primary);
    }

    .conversation-header {
        padding: 1rem 1.5rem;
        border-bottom: 1px solid var(--border-color);
        display: flex;
        align-items: center;
        gap: 1rem;
    }

    .messages-container {
        flex: 1;
        overflow-y: auto;
        padding: 1.5rem;
        display: flex;
        flex-direction: column;
        gap: 1rem;
    }

    .message {
        max-width: 70%;
        padding: 0.75rem 1rem;
        border-radius: 12px;
    }

    .message.inbound {
        background: var(--bg-secondary);
        align-self: flex-start;
    }

    .message.outbound {
        background: var(--primary);
        color: white;
        align-self: flex-end;
    }

    .message-time {
        font-size: 0.7rem;
        opacity: 0.7;
        margin-top: 0.25rem;
    }

    .reply-box {
        padding: 1rem 1.5rem;
        border-top: 1px solid var(--border-color);
        display: flex;
        gap: 1rem;
    }

    .reply-box textarea {
        flex: 1;
        padding: 0.75rem;
        border: 1px solid var(--border-color);
        border-radius: 8px;
        resize: none;
        height: 60px;
    }

    .empty-inbox {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100%;
        color: var(--text-muted);
    }

    @media (max-width: 768px) {
        .inbox-sidebar { width: 100%; }
        .inbox-main { display: none; }
        .inbox-main.active { display: flex; width: 100%; }
        .inbox-sidebar.hidden { display: none; }
    }
</style>
{% endblock %}

{% block content %}
<div class="inbox-container">
    <div class="inbox-sidebar" id="inboxSidebar">
        <div class="inbox-filters">
            <button class="filter-btn active" data-filter="all" onclick="filterConversations('all')">Todas</button>
            <button class="filter-btn" data-filter="email" onclick="filterConversations('email')">Email</button>
            <button class="filter-btn" data-filter="whatsapp" onclick="filterConversations('whatsapp')">WhatsApp</button>
            <button class="filter-btn" data-filter="unread" onclick="filterConversations('unread')">Nao lidas</button>
        </div>
        <div class="conversation-list" id="conversationList">
            <div class="empty-inbox">Carregando...</div>
        </div>
    </div>

    <div class="inbox-main" id="inboxMain">
        <div class="conversation-header" id="convHeader">
            <div class="empty-inbox">Selecione uma conversa</div>
        </div>
        <div class="messages-container" id="messagesContainer">
        </div>
        <div class="reply-box" id="replyBox" style="display: none;">
            <textarea placeholder="Digite sua resposta..." id="replyText"></textarea>
            <button class="btn btn-primary" onclick="sendReply()">Enviar</button>
        </div>
    </div>
</div>

<script>
let currentFilter = 'all';
let currentConversation = null;

async function loadConversations(filter = 'all') {
    currentFilter = filter;
    const container = document.getElementById('conversationList');

    try {
        const response = await fetch(`/api/inbox/conversations?filter_type=${filter}`);
        const data = await response.json();
        const conversations = data.conversations || [];

        if (conversations.length === 0) {
            container.innerHTML = '<div class="empty-inbox">Nenhuma conversa</div>';
            return;
        }

        let html = '';
        conversations.forEach(conv => {
            const initials = (conv.contact_name || '?').split(' ').map(n => n[0]).join('').substring(0,2).toUpperCase();
            const time = conv.updated_at ? new Date(conv.updated_at).toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'}) : '';
            const unreadClass = conv.unread_count > 0 ? 'unread' : '';

            html += `
                <div class="conversation-item ${unreadClass}" onclick="openConversation(${conv.id}, '${conv.contact_name}', '${conv.foto_url || ''}')">
                    <div class="conv-avatar">
                        ${conv.foto_url ? `<img src="${conv.foto_url}" alt="">` : initials}
                    </div>
                    <div class="conv-content">
                        <div class="conv-header">
                            <span class="conv-name">${conv.contact_name || 'Desconhecido'}</span>
                            <span class="conv-time">${time}</span>
                        </div>
                        <div class="conv-preview">${conv.last_message_preview || ''}</div>
                        <span class="conv-channel ${conv.channel}">${conv.channel}</span>
                    </div>
                </div>
            `;
        });
        container.innerHTML = html;
    } catch (error) {
        container.innerHTML = '<div class="empty-inbox">Erro ao carregar</div>';
    }
}

function filterConversations(filter) {
    document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelector(`[data-filter="${filter}"]`).classList.add('active');
    loadConversations(filter);
}

async function openConversation(id, name, foto) {
    currentConversation = id;

    // Update header
    const header = document.getElementById('convHeader');
    const initials = name.split(' ').map(n => n[0]).join('').substring(0,2).toUpperCase();
    header.innerHTML = `
        <div class="conv-avatar">${foto ? `<img src="${foto}" alt="">` : initials}</div>
        <div><strong>${name}</strong></div>
    `;

    // Load messages
    const container = document.getElementById('messagesContainer');
    container.innerHTML = '<div class="empty-inbox">Carregando...</div>';

    try {
        const response = await fetch(`/api/inbox/conversations/${id}/messages`);
        const data = await response.json();
        const messages = data.messages || [];

        if (messages.length === 0) {
            container.innerHTML = '<div class="empty-inbox">Nenhuma mensagem</div>';
        } else {
            let html = '';
            messages.reverse().forEach(msg => {
                const time = msg.enviado_em ? new Date(msg.enviado_em).toLocaleString('pt-BR') : '';
                html += `
                    <div class="message ${msg.direction}">
                        <div class="message-content">${msg.content || ''}</div>
                        <div class="message-time">${time}</div>
                    </div>
                `;
            });
            container.innerHTML = html;
            container.scrollTop = container.scrollHeight;
        }

        // Mark as read
        await fetch(`/api/inbox/conversations/${id}/read`, {method: 'POST'});

        // Show reply box
        document.getElementById('replyBox').style.display = 'flex';

        // Mobile: show main view
        document.getElementById('inboxSidebar').classList.add('hidden');
        document.getElementById('inboxMain').classList.add('active');

    } catch (error) {
        container.innerHTML = '<div class="empty-inbox">Erro ao carregar</div>';
    }
}

function sendReply() {
    const text = document.getElementById('replyText').value;
    if (!text.trim()) return;
    alert('Funcionalidade de envio em desenvolvimento');
    document.getElementById('replyText').value = '';
}

// Init
loadConversations();
</script>
{% endblock %}
```

**Adicionar rota em main.py**:

```python
@app.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("intel_inbox.html", {"request": request, "user": user})
```

**Commit**: `git commit -m "Add Inbox page with master-detail layout"`

---

## TAREFA 2: Melhorar Pagina de Contato

**Status**: PENDENTE
**Prioridade**: ALTA

**Editar arquivo**: `app/templates/rap_contact_detail.html`

**Adicionar apos a secao de dados basicos** (dentro do card principal):

```html
<!-- Quick Actions -->
<div class="quick-actions mt-3 d-flex gap-2">
    <a href="https://wa.me/{{ contact.telefones[0] if contact.telefones else '' }}" target="_blank" class="btn btn-outline-success btn-sm">
        <i class="bi bi-whatsapp"></i> WhatsApp
    </a>
    <a href="mailto:{{ contact.emails[0] if contact.emails else '' }}" class="btn btn-outline-primary btn-sm">
        <i class="bi bi-envelope"></i> Email
    </a>
    <button class="btn btn-outline-secondary btn-sm" onclick="addNote()">
        <i class="bi bi-sticky"></i> Nota
    </button>
</div>

<!-- Tags -->
<div class="tags-section mt-3">
    <strong>Tags:</strong>
    <div class="d-flex flex-wrap gap-1 mt-1" id="contactTags">
        {% for tag in contact.tags or [] %}
        <span class="badge bg-secondary">{{ tag }}</span>
        {% endfor %}
        <button class="btn btn-sm btn-outline-secondary" onclick="addTag()">+ Tag</button>
    </div>
</div>
```

**Adicionar nova secao Timeline** (apos o card principal):

```html
<!-- Timeline -->
<div class="card mt-4">
    <div class="card-header d-flex justify-content-between align-items-center">
        <h5 class="mb-0"><i class="bi bi-clock-history"></i> Historico</h5>
    </div>
    <div class="card-body">
        <div id="contactTimeline">
            <div class="text-center text-muted">Carregando...</div>
        </div>
    </div>
</div>

<script>
async function loadTimeline() {
    const container = document.getElementById('contactTimeline');
    try {
        const response = await fetch('/api/contacts/{{ contact.id }}/timeline?limit=20');
        const data = await response.json();
        const timeline = data.timeline || [];

        if (timeline.length === 0) {
            container.innerHTML = '<div class="text-muted">Nenhuma interacao registrada</div>';
            return;
        }

        let html = '<div class="timeline-list">';
        timeline.forEach(item => {
            const date = item.timestamp ? new Date(item.timestamp).toLocaleString('pt-BR') : '';
            const icon = item.type === 'message' ? 'bi-chat-dots' : 'bi-sticky';
            html += `
                <div class="timeline-item d-flex gap-3 mb-3 pb-3 border-bottom">
                    <div class="timeline-icon">
                        <i class="bi ${icon}"></i>
                    </div>
                    <div class="timeline-content flex-grow-1">
                        <div class="d-flex justify-content-between">
                            <strong>${item.title}</strong>
                            <small class="text-muted">${date}</small>
                        </div>
                        <div class="text-muted">${item.content || ''}</div>
                    </div>
                </div>
            `;
        });
        html += '</div>';
        container.innerHTML = html;
    } catch (error) {
        container.innerHTML = '<div class="text-muted">Erro ao carregar historico</div>';
    }
}

function addNote() {
    const note = prompt('Digite a nota:');
    if (note) {
        alert('Nota salva (em desenvolvimento)');
    }
}

function addTag() {
    const tag = prompt('Nova tag:');
    if (tag) {
        alert('Tag adicionada (em desenvolvimento)');
    }
}

// Load timeline on page load
document.addEventListener('DOMContentLoaded', loadTimeline);
</script>
```

**Commit**: `git commit -m "Improve contact detail page with timeline and quick actions"`

---

## TAREFA 3: Mobile Responsivo

**Status**: PENDENTE
**Prioridade**: ALTA

**Editar arquivo**: `app/static/css/style.css` (ou criar se nao existir)

**Adicionar ao final**:

```css
/* Mobile Responsive */
@media (max-width: 768px) {
    /* Sidebar */
    .sidebar {
        position: fixed;
        left: -260px;
        top: 0;
        height: 100vh;
        z-index: 1000;
        transition: left 0.3s ease;
    }

    .sidebar.open {
        left: 0;
    }

    .sidebar-overlay {
        display: none;
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0,0,0,0.5);
        z-index: 999;
    }

    .sidebar-overlay.active {
        display: block;
    }

    /* Main content */
    .main-content {
        margin-left: 0 !important;
        padding: 1rem;
    }

    /* Dashboard stats */
    .stats-grid {
        grid-template-columns: 1fr 1fr !important;
        gap: 0.75rem;
    }

    .stat-card {
        padding: 1rem;
    }

    /* Cards */
    .dashboard-grid {
        grid-template-columns: 1fr !important;
    }

    /* Tables to cards */
    .table-responsive table {
        display: block;
    }

    .table-responsive thead {
        display: none;
    }

    .table-responsive tbody tr {
        display: block;
        margin-bottom: 1rem;
        padding: 1rem;
        background: var(--bg-secondary);
        border-radius: 8px;
    }

    .table-responsive tbody td {
        display: flex;
        justify-content: space-between;
        padding: 0.5rem 0;
        border: none;
    }

    .table-responsive tbody td::before {
        content: attr(data-label);
        font-weight: 600;
    }

    /* Hamburger menu */
    .hamburger-btn {
        display: block !important;
        position: fixed;
        top: 1rem;
        left: 1rem;
        z-index: 1001;
        background: var(--primary);
        color: white;
        border: none;
        padding: 0.75rem;
        border-radius: 8px;
        cursor: pointer;
    }

    /* Search */
    .search-modal {
        width: 95% !important;
        max-width: none;
    }
}

/* Hamburger button (hidden on desktop) */
.hamburger-btn {
    display: none;
}
```

**Adicionar em rap_base.html** (antes do sidebar):

```html
<button class="hamburger-btn" onclick="toggleSidebar()">
    <i class="bi bi-list"></i>
</button>
<div class="sidebar-overlay" onclick="toggleSidebar()"></div>

<script>
function toggleSidebar() {
    document.querySelector('.sidebar').classList.toggle('open');
    document.querySelector('.sidebar-overlay').classList.toggle('active');
}
</script>
```

**Commit**: `git commit -m "Add mobile responsive styles and hamburger menu"`

---

## TAREFA 4: PWA Basico

**Status**: PENDENTE
**Prioridade**: MEDIA

**Criar arquivo**: `app/static/manifest.json`

```json
{
    "name": "INTEL - Assistente Pessoal",
    "short_name": "INTEL",
    "description": "Sistema de gerenciamento de contatos e relacionamentos",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#1e293b",
    "theme_color": "#6366f1",
    "orientation": "portrait-primary",
    "icons": [
        {
            "src": "/static/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable"
        },
        {
            "src": "/static/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable"
        }
    ]
}
```

**Criar arquivo**: `app/static/sw.js`

```javascript
const CACHE_NAME = 'intel-v1';
const urlsToCache = [
    '/',
    '/static/css/style.css',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css',
    'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css'
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(urlsToCache))
    );
});

self.addEventListener('fetch', event => {
    event.respondWith(
        caches.match(event.request)
            .then(response => response || fetch(event.request))
    );
});
```

**Adicionar no head de rap_base.html**:

```html
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#6366f1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/static/icon-192.png">

<script>
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js');
}
</script>
```

**Criar icones** (usar qualquer ferramenta ou placeholder):
- `app/static/icon-192.png` (192x192)
- `app/static/icon-512.png` (512x512)

Se nao conseguir criar, usar placeholder SVG inline.

**Commit**: `git commit -m "Add PWA manifest and service worker"`

---

## TAREFA 5: Pagina Analytics

**Status**: PENDENTE
**Prioridade**: BAIXA

**Criar arquivo**: `app/templates/intel_analytics.html`

```html
{% extends "rap_base.html" %}

{% block title %}Analytics - INTEL{% endblock %}

{% block extra_css %}
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
    .analytics-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
        gap: 1.5rem;
    }
    .chart-container {
        position: relative;
        height: 300px;
    }
    .period-selector {
        display: flex;
        gap: 0.5rem;
        margin-bottom: 1.5rem;
    }
    .period-btn {
        padding: 0.5rem 1rem;
        border: 1px solid var(--border-color);
        background: transparent;
        border-radius: 8px;
        cursor: pointer;
    }
    .period-btn.active {
        background: var(--primary);
        color: white;
        border-color: var(--primary);
    }
</style>
{% endblock %}

{% block content %}
<div class="page-header mb-4">
    <h1><i class="bi bi-graph-up"></i> Analytics</h1>
</div>

<div class="period-selector">
    <button class="period-btn" onclick="loadAnalytics(7)">7 dias</button>
    <button class="period-btn active" onclick="loadAnalytics(30)">30 dias</button>
    <button class="period-btn" onclick="loadAnalytics(90)">90 dias</button>
</div>

<div class="analytics-grid">
    <div class="card">
        <div class="card-header">
            <h5>Distribuicao por Circulo</h5>
        </div>
        <div class="card-body">
            <div class="chart-container">
                <canvas id="circuloChart"></canvas>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="card-header">
            <h5>Resumo do Periodo</h5>
        </div>
        <div class="card-body">
            <div class="row text-center">
                <div class="col-4">
                    <h2 id="totalInteracoes">-</h2>
                    <small class="text-muted">Interacoes</small>
                </div>
                <div class="col-4">
                    <h2 id="healthMedio">-</h2>
                    <small class="text-muted">Health Medio</small>
                </div>
                <div class="col-4">
                    <h2 id="totalContatos">-</h2>
                    <small class="text-muted">Contatos Ativos</small>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
let circuloChart = null;

async function loadAnalytics(days = 30) {
    // Update buttons
    document.querySelectorAll('.period-btn').forEach(btn => btn.classList.remove('active'));
    event.target.classList.add('active');

    try {
        const response = await fetch(`/api/analytics/summary?days=${days}`);
        const data = await response.json();

        // Update stats
        document.getElementById('totalInteracoes').textContent = data.total_interacoes || 0;
        document.getElementById('healthMedio').textContent = (data.health_medio || 0) + '%';

        // Calculate total active contacts
        const porCirculo = data.por_circulo || {};
        let totalAtivos = 0;
        for (let i = 1; i <= 4; i++) {
            totalAtivos += porCirculo[i] || 0;
        }
        document.getElementById('totalContatos').textContent = totalAtivos;

        // Update chart
        updateCirculoChart(porCirculo);
    } catch (error) {
        console.error('Error loading analytics:', error);
    }
}

function updateCirculoChart(porCirculo) {
    const ctx = document.getElementById('circuloChart').getContext('2d');

    if (circuloChart) {
        circuloChart.destroy();
    }

    circuloChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: ['C1 Intimo', 'C2 Proximo', 'C3 Ativo', 'C4 Conhecidos', 'C5 Arquivo'],
            datasets: [{
                data: [
                    porCirculo[1] || 0,
                    porCirculo[2] || 0,
                    porCirculo[3] || 0,
                    porCirculo[4] || 0,
                    porCirculo[5] || 0
                ],
                backgroundColor: ['#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', '#DDA0DD']
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false
        }
    });
}

// Init
loadAnalytics(30);
</script>
{% endblock %}
```

**Adicionar rota em main.py**:

```python
@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("intel_analytics.html", {"request": request, "user": user})
```

**Adicionar link no sidebar** (em rap_base.html):

```html
<a href="/analytics" class="nav-item">
    <i class="bi bi-graph-up"></i>
    <span>Analytics</span>
</a>
```

**Commit**: `git commit -m "Add Analytics page with charts"`

---

## APOS COMPLETAR TODAS

```bash
git push origin main
```

Atualizar este arquivo marcando todas como **CONCLUIDAS**.

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-26 | UI Agenda/Tarefas/Badge/Busca | CONCLUIDO |
| 2026-03-27 | Pagina Inbox | _aguardando_ |
| 2026-03-27 | Pagina Contato melhorada | _aguardando_ |
| 2026-03-27 | Mobile Responsivo | _aguardando_ |
| 2026-03-27 | PWA | _aguardando_ |
| 2026-03-27 | Analytics | _aguardando_ |
