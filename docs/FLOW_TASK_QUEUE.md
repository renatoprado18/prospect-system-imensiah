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

## TAREFA 1: Pagina de Inbox Unificado

**Status**: EXECUTAR AGORA
**Prioridade**: CRITICA

**Criar arquivo**: `app/templates/rap_inbox.html`

```html
{% extends "rap_base.html" %}
{% block title %}Inbox - INTEL{% endblock %}

{% block content %}
<div class="container-fluid py-4">
    <div class="row">
        <!-- Lista de Conversas -->
        <div class="col-md-4 col-lg-3">
            <div class="card">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <h5 class="mb-0">Conversas</h5>
                    <span class="badge bg-primary" id="unread-badge">0</span>
                </div>
                <div class="card-body p-0">
                    <!-- Filtros -->
                    <div class="btn-group w-100 p-2" role="group">
                        <button type="button" class="btn btn-sm btn-outline-secondary active" data-filter="all">Todos</button>
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-filter="unread">Pendentes</button>
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-filter="email">Email</button>
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-filter="whatsapp">WhatsApp</button>
                    </div>
                    <!-- Lista -->
                    <div class="list-group list-group-flush" id="conversation-list" style="max-height: 70vh; overflow-y: auto;">
                        <div class="text-center py-4 text-muted">Carregando...</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Detalhe da Conversa -->
        <div class="col-md-8 col-lg-9">
            <div class="card" id="conversation-detail">
                <div class="card-header" id="detail-header" style="display: none;">
                    <div class="d-flex justify-content-between align-items-center">
                        <div class="d-flex align-items-center">
                            <img src="" id="contact-photo" class="rounded-circle me-2" width="40" height="40" style="display: none;">
                            <div>
                                <h6 class="mb-0" id="contact-name">-</h6>
                                <small class="text-muted" id="contact-channel">-</small>
                            </div>
                        </div>
                        <a href="#" id="contact-link" class="btn btn-sm btn-outline-primary">Ver Contato</a>
                    </div>
                </div>
                <div class="card-body" id="messages-container" style="max-height: 60vh; overflow-y: auto;">
                    <div class="text-center text-muted py-5">
                        <i class="bi bi-chat-dots" style="font-size: 3rem;"></i>
                        <p class="mt-3">Selecione uma conversa para ver as mensagens</p>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
let currentFilter = 'all';
let currentConversationId = null;

async function loadConversations(filter = 'all') {
    currentFilter = filter;
    const list = document.getElementById('conversation-list');
    list.innerHTML = '<div class="text-center py-4"><div class="spinner-border spinner-border-sm"></div></div>';

    try {
        const response = await fetch(`/api/inbox/conversations?filter_type=${filter}&limit=50`);
        const data = await response.json();

        if (!data.conversations || data.conversations.length === 0) {
            list.innerHTML = '<div class="text-center py-4 text-muted">Nenhuma conversa</div>';
            return;
        }

        list.innerHTML = data.conversations.map(conv => `
            <a href="#" class="list-group-item list-group-item-action ${conv.requer_resposta ? 'border-start border-3 border-primary' : ''}"
               onclick="loadMessages(${conv.id}); return false;" data-id="${conv.id}">
                <div class="d-flex w-100 justify-content-between align-items-center">
                    <div class="d-flex align-items-center">
                        ${conv.foto_url ? `<img src="${conv.foto_url}" class="rounded-circle me-2" width="36" height="36">` : '<div class="rounded-circle bg-secondary me-2 d-flex align-items-center justify-content-center" style="width:36px;height:36px;"><i class="bi bi-person text-white"></i></div>'}
                        <div>
                            <h6 class="mb-0 ${conv.requer_resposta ? 'fw-bold' : ''}">${conv.contact_name || 'Desconhecido'}</h6>
                            <small class="text-muted text-truncate d-block" style="max-width: 200px;">${conv.subject || ''}</small>
                        </div>
                    </div>
                    <div class="text-end">
                        <small class="text-muted">${formatDate(conv.last_message_at)}</small>
                        <div><span class="badge ${conv.channel === 'email' ? 'bg-info' : 'bg-success'}">${conv.channel}</span></div>
                    </div>
                </div>
            </a>
        `).join('');

    } catch (error) {
        list.innerHTML = '<div class="text-center py-4 text-danger">Erro ao carregar</div>';
    }
}

async function loadMessages(conversationId) {
    currentConversationId = conversationId;

    document.querySelectorAll('#conversation-list a').forEach(a => a.classList.remove('active'));
    document.querySelector(`#conversation-list a[data-id="${conversationId}"]`)?.classList.add('active');

    const container = document.getElementById('messages-container');
    container.innerHTML = '<div class="text-center py-4"><div class="spinner-border"></div></div>';

    try {
        const [convResponse, msgResponse] = await Promise.all([
            fetch(`/api/inbox/conversations/${conversationId}`),
            fetch(`/api/inbox/conversations/${conversationId}/messages?limit=100`)
        ]);

        const conversation = await convResponse.json();
        const messagesData = await msgResponse.json();

        document.getElementById('detail-header').style.display = 'block';
        document.getElementById('contact-name').textContent = conversation.contact_name || 'Desconhecido';
        document.getElementById('contact-channel').textContent = `Via ${conversation.channel}`;
        document.getElementById('contact-link').href = `/contato/${conversation.contact_id}`;

        if (conversation.foto_url) {
            document.getElementById('contact-photo').src = conversation.foto_url;
            document.getElementById('contact-photo').style.display = 'block';
        } else {
            document.getElementById('contact-photo').style.display = 'none';
        }

        if (!messagesData.messages || messagesData.messages.length === 0) {
            container.innerHTML = '<div class="text-center text-muted py-5">Nenhuma mensagem</div>';
            return;
        }

        container.innerHTML = messagesData.messages.reverse().map(msg => `
            <div class="mb-3 ${msg.direction === 'outbound' ? 'text-end' : ''}">
                <div class="d-inline-block p-3 rounded ${msg.direction === 'outbound' ? 'bg-primary text-white' : 'bg-light'}" style="max-width: 70%;">
                    <div style="white-space: pre-wrap;">${escapeHtml(msg.content || '')}</div>
                    <small class="${msg.direction === 'outbound' ? 'text-white-50' : 'text-muted'}">${formatDateTime(msg.sent_at)}</small>
                </div>
            </div>
        `).join('');

        container.scrollTop = container.scrollHeight;
        await fetch(`/api/inbox/conversations/${conversationId}/read`, { method: 'POST' });
        loadUnreadCount();

    } catch (error) {
        container.innerHTML = '<div class="text-center text-danger py-5">Erro ao carregar mensagens</div>';
    }
}

async function loadUnreadCount() {
    try {
        const response = await fetch('/api/inbox/unread');
        const data = await response.json();
        document.getElementById('unread-badge').textContent = data.unread || 0;
    } catch (error) {}
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    const date = new Date(dateStr);
    const today = new Date();
    if (date.toDateString() === today.toDateString()) {
        return date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
    }
    return date.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
}

function formatDateTime(dateStr) {
    if (!dateStr) return '';
    return new Date(dateStr).toLocaleString('pt-BR', {
        day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

document.querySelectorAll('[data-filter]').forEach(btn => {
    btn.addEventListener('click', function() {
        document.querySelectorAll('[data-filter]').forEach(b => b.classList.remove('active'));
        this.classList.add('active');
        loadConversations(this.dataset.filter);
    });
});

loadConversations();
loadUnreadCount();
</script>
{% endblock %}
```

**Adicionar rota em main.py** (se nao existir):

```python
@app.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_inbox.html", {"request": request, "user": user})
```

**Commit**: `git commit -m "Add Inbox page"`

---

## TAREFA 2: Busca Global com Autocomplete

**Status**: PENDENTE
**Prioridade**: ALTA

**Editar**: `app/templates/rap_base.html`

**Adicionar no navbar** (apos o titulo):

```html
<!-- Busca Global -->
<div class="position-relative mx-3" style="width: 280px;">
    <input type="text" class="form-control form-control-sm" id="global-search"
           placeholder="Buscar contatos..." autocomplete="off">
    <div class="position-absolute w-100 bg-white shadow rounded" id="search-results"
         style="display: none; z-index: 1050; max-height: 300px; overflow-y: auto; top: 100%;">
    </div>
</div>

<script>
let searchTimeout = null;

document.getElementById('global-search').addEventListener('input', function() {
    const query = this.value.trim();
    const results = document.getElementById('search-results');

    if (query.length < 2) {
        results.style.display = 'none';
        return;
    }

    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(async () => {
        try {
            const response = await fetch(`/api/search/suggestions?q=${encodeURIComponent(query)}&limit=8`);
            const data = await response.json();

            if (!data.suggestions || data.suggestions.length === 0) {
                results.innerHTML = '<div class="p-3 text-muted">Nenhum resultado</div>';
            } else {
                results.innerHTML = data.suggestions.map(s => `
                    <a href="/contato/${s.id}" class="d-flex align-items-center p-2 text-decoration-none text-dark border-bottom">
                        ${s.foto_url ? `<img src="${s.foto_url}" class="rounded-circle me-2" width="32" height="32">` : '<div class="rounded-circle bg-secondary me-2 d-flex align-items-center justify-content-center" style="width:32px;height:32px;"><i class="bi bi-person text-white small"></i></div>'}
                        <div class="flex-grow-1">
                            <div class="fw-semibold small">${s.nome}</div>
                            <small class="text-muted">${s.empresa || ''}</small>
                        </div>
                        <span class="badge bg-secondary">C${s.circulo || 5}</span>
                    </a>
                `).join('');
            }

            results.style.display = 'block';
        } catch (error) {
            results.style.display = 'none';
        }
    }, 300);
});

document.addEventListener('click', function(e) {
    if (!e.target.closest('#global-search') && !e.target.closest('#search-results')) {
        document.getElementById('search-results').style.display = 'none';
    }
});

document.getElementById('global-search').addEventListener('keypress', function(e) {
    if (e.key === 'Enter') {
        const query = this.value.trim();
        if (query.length >= 2) {
            window.location.href = `/contatos?q=${encodeURIComponent(query)}`;
        }
    }
});
</script>
```

**Commit**: `git commit -m "Add global search with autocomplete"`

---

## TAREFA 3: Melhorar Pagina de Contato com Timeline

**Status**: PENDENTE
**Prioridade**: ALTA

**Editar**: `app/templates/rap_contact_detail.html`

**Adicionar nova secao apos dados basicos**:

```html
<!-- Timeline do Contato -->
<div class="card mt-4">
    <div class="card-header">
        <h5 class="mb-0"><i class="bi bi-clock-history"></i> Historico de Interacoes</h5>
    </div>
    <div class="card-body" id="contact-timeline">
        <div class="text-center py-3">
            <div class="spinner-border spinner-border-sm"></div>
        </div>
    </div>
</div>

<script>
async function loadContactTimeline() {
    const container = document.getElementById('contact-timeline');
    const contactId = {{ contact.id }};

    try {
        const response = await fetch(`/api/contacts/${contactId}/timeline?limit=20`);
        const data = await response.json();

        if (!data.timeline || data.timeline.length === 0) {
            container.innerHTML = '<div class="text-muted">Nenhuma interacao registrada</div>';
            return;
        }

        container.innerHTML = data.timeline.map(item => {
            const date = item.timestamp ? new Date(item.timestamp).toLocaleDateString('pt-BR') : '';
            const iconClass = item.type === 'message' ? 'bi-chat-dots text-primary' :
                              item.type === 'note' ? 'bi-sticky text-success' : 'bi-lightbulb text-warning';

            return `
                <div class="d-flex mb-3 pb-3 border-bottom">
                    <div class="me-3">
                        <i class="bi ${iconClass}" style="font-size: 1.25rem;"></i>
                    </div>
                    <div class="flex-grow-1">
                        <div class="d-flex justify-content-between">
                            <strong>${item.title || ''}</strong>
                            <small class="text-muted">${date}</small>
                        </div>
                        <div class="text-muted small">${item.content || ''}</div>
                        ${item.channel ? `<span class="badge bg-secondary mt-1">${item.channel}</span>` : ''}
                    </div>
                </div>
            `;
        }).join('');

    } catch (error) {
        container.innerHTML = '<div class="text-danger">Erro ao carregar historico</div>';
    }
}

document.addEventListener('DOMContentLoaded', loadContactTimeline);
</script>
```

**Commit**: `git commit -m "Add timeline to contact detail page"`

---

## TAREFA 4: Pagina de Analytics

**Status**: PENDENTE
**Prioridade**: MEDIA

**Criar arquivo**: `app/templates/rap_analytics.html`

```html
{% extends "rap_base.html" %}
{% block title %}Analytics - INTEL{% endblock %}

{% block content %}
<div class="container-fluid py-4">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h4 class="mb-0"><i class="bi bi-graph-up"></i> Analytics</h4>
        <div class="btn-group">
            <button class="btn btn-outline-secondary btn-sm active" onclick="loadAnalytics(7)">7 dias</button>
            <button class="btn btn-outline-secondary btn-sm" onclick="loadAnalytics(30)">30 dias</button>
            <button class="btn btn-outline-secondary btn-sm" onclick="loadAnalytics(90)">90 dias</button>
        </div>
    </div>

    <!-- Cards de Resumo -->
    <div class="row g-3 mb-4">
        <div class="col-md-3">
            <div class="card text-center">
                <div class="card-body">
                    <h3 id="stat-contacts">-</h3>
                    <small class="text-muted">Contatos Ativos</small>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card text-center">
                <div class="card-body">
                    <h3 id="stat-messages">-</h3>
                    <small class="text-muted">Mensagens</small>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card text-center">
                <div class="card-body">
                    <h3 id="stat-health">-</h3>
                    <small class="text-muted">Health Medio</small>
                </div>
            </div>
        </div>
        <div class="col-md-3">
            <div class="card text-center">
                <div class="card-body">
                    <h3 id="stat-new">-</h3>
                    <small class="text-muted">Novos Contatos</small>
                </div>
            </div>
        </div>
    </div>

    <!-- Graficos -->
    <div class="row g-4">
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">Contatos por Circulo</div>
                <div class="card-body">
                    <canvas id="chart-circulos" height="250"></canvas>
                </div>
            </div>
        </div>
        <div class="col-md-6">
            <div class="card">
                <div class="card-header">Health por Circulo</div>
                <div class="card-body" id="health-bars">
                    <div class="text-center py-3"><div class="spinner-border spinner-border-sm"></div></div>
                </div>
            </div>
        </div>
    </div>

    <!-- Exportar -->
    <div class="mt-4">
        <a href="/api/export/contacts?format=csv" class="btn btn-outline-primary btn-sm">
            <i class="bi bi-download"></i> Exportar Contatos (CSV)
        </a>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
let chartCirculos = null;

async function loadAnalytics(days = 30) {
    document.querySelectorAll('.btn-group .btn').forEach(b => b.classList.remove('active'));
    event?.target?.classList.add('active');

    try {
        const response = await fetch(`/api/analytics/summary?days=${days}`);
        const data = await response.json();

        // Stats
        const porCirculo = data.contatos?.por_circulo || {};
        let totalAtivos = 0;
        for (let i = 1; i <= 4; i++) totalAtivos += porCirculo[i] || 0;

        document.getElementById('stat-contacts').textContent = totalAtivos.toLocaleString();
        document.getElementById('stat-messages').textContent = (data.mensagens?.total_periodo || 0).toLocaleString();
        document.getElementById('stat-health').textContent = (data.health?.medio_geral || 0) + '%';
        document.getElementById('stat-new').textContent = (data.contatos?.novos_periodo || 0).toLocaleString();

        // Chart
        const ctx = document.getElementById('chart-circulos').getContext('2d');
        if (chartCirculos) chartCirculos.destroy();

        chartCirculos = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: ['C1 Intimo', 'C2 Proximo', 'C3 Ativo', 'C4 Conhecidos', 'C5 Arquivo'],
                datasets: [{
                    data: [porCirculo[1]||0, porCirculo[2]||0, porCirculo[3]||0, porCirculo[4]||0, porCirculo[5]||0],
                    backgroundColor: ['#ef4444', '#f97316', '#eab308', '#22c55e', '#6b7280']
                }]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });

        // Health bars
        const healthData = data.health?.por_circulo || {};
        let healthHtml = '';
        for (let i = 1; i <= 4; i++) {
            const h = healthData[i] || { avg: 50 };
            const color = h.avg >= 70 ? 'bg-success' : h.avg >= 40 ? 'bg-warning' : 'bg-danger';
            healthHtml += `
                <div class="d-flex align-items-center mb-2">
                    <span class="me-2" style="width: 80px;">Circulo ${i}</span>
                    <div class="progress flex-grow-1 me-2" style="height: 20px;">
                        <div class="progress-bar ${color}" style="width: ${h.avg}%"></div>
                    </div>
                    <span class="fw-bold">${h.avg}%</span>
                </div>
            `;
        }
        document.getElementById('health-bars').innerHTML = healthHtml;

    } catch (error) {
        console.error('Error:', error);
    }
}

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
    return templates.TemplateResponse("rap_analytics.html", {"request": request, "user": user})
```

**Commit**: `git commit -m "Add Analytics page with charts"`

---

## TAREFA 5: Mobile Responsive

**Status**: PENDENTE
**Prioridade**: MEDIA

**Editar**: `app/templates/rap_base.html`

**Adicionar CSS no head**:

```html
<style>
/* Mobile Responsive */
@media (max-width: 768px) {
    .sidebar {
        position: fixed;
        left: -260px;
        top: 0;
        height: 100vh;
        z-index: 1040;
        transition: left 0.3s;
    }
    .sidebar.show { left: 0; }

    .main-content {
        margin-left: 0 !important;
        padding: 1rem;
    }

    .navbar .nav-item span { display: none; }

    .card-body { padding: 1rem; }

    h4, h5 { font-size: 1.1rem; }

    .table-responsive { font-size: 0.85rem; }

    .btn-group { flex-wrap: wrap; }
    .btn-group .btn { flex: 1 1 auto; margin-bottom: 0.25rem; }

    #global-search { width: 150px !important; }
}

.sidebar-toggle {
    display: none;
    position: fixed;
    top: 10px;
    left: 10px;
    z-index: 1050;
    background: var(--bs-primary);
    color: white;
    border: none;
    padding: 0.5rem 0.75rem;
    border-radius: 8px;
}

@media (max-width: 768px) {
    .sidebar-toggle { display: block; }
}

.sidebar-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.5);
    z-index: 1030;
}
.sidebar-overlay.show { display: block; }
</style>
```

**Adicionar antes do sidebar**:

```html
<button class="sidebar-toggle" onclick="toggleMobileSidebar()">
    <i class="bi bi-list"></i>
</button>
<div class="sidebar-overlay" onclick="toggleMobileSidebar()"></div>

<script>
function toggleMobileSidebar() {
    document.querySelector('.sidebar')?.classList.toggle('show');
    document.querySelector('.sidebar-overlay')?.classList.toggle('show');
}
</script>
```

**Commit**: `git commit -m "Add mobile responsive styles"`

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
| 2026-03-27 | Pagina Inbox | _aguardando_ |
| 2026-03-27 | Busca Global | _aguardando_ |
| 2026-03-27 | Timeline Contato | _aguardando_ |
| 2026-03-27 | Pagina Analytics | _aguardando_ |
| 2026-03-27 | Mobile Responsive | _aguardando_ |
