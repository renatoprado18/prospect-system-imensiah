# Fila de Tarefas 3FLOW - Calendar UI

**Atualizacao**: 2026-03-27
**Modo**: AUTONOMO TOTAL - executar TODAS as tarefas sem parar
**Prioridade**: Executar APÓS 2INTEL concluir backend do Calendar

---

## INSTRUCOES GERAIS

- NAO perguntar nada, apenas executar
- Se encontrar erro, resolver e continuar
- Commit apos cada tarefa concluida
- Push direto em main
- Atualizar este arquivo marcando tarefas como CONCLUIDAS

---

## TAREFA 1: Pagina de Calendario

**Status**: PENDENTE
**Prioridade**: CRITICA

### Criar `app/templates/rap_calendario.html`

```html
{% extends "rap_base.html" %}

{% block title %}Calendario - INTEL{% endblock %}

{% block extra_head %}
<style>
    .calendar-container {
        display: grid;
        grid-template-columns: 1fr 300px;
        gap: 24px;
    }

    .calendar-main {
        background: var(--card-bg);
        border-radius: 12px;
        padding: 24px;
    }

    .calendar-sidebar {
        display: flex;
        flex-direction: column;
        gap: 16px;
    }

    /* Header do calendario */
    .calendar-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 24px;
    }

    .calendar-nav {
        display: flex;
        gap: 8px;
        align-items: center;
    }

    .calendar-nav button {
        background: var(--bg-secondary);
        border: none;
        padding: 8px 12px;
        border-radius: 8px;
        cursor: pointer;
        color: var(--text-primary);
    }

    .calendar-nav button:hover {
        background: var(--bg-tertiary);
    }

    .calendar-title {
        font-size: 1.5rem;
        font-weight: 600;
    }

    .view-toggle {
        display: flex;
        gap: 4px;
        background: var(--bg-secondary);
        padding: 4px;
        border-radius: 8px;
    }

    .view-toggle button {
        background: none;
        border: none;
        padding: 8px 16px;
        border-radius: 6px;
        cursor: pointer;
        color: var(--text-secondary);
        font-size: 0.875rem;
    }

    .view-toggle button.active {
        background: var(--accent-blue);
        color: white;
    }

    /* Grid do calendario */
    .calendar-grid {
        display: grid;
        grid-template-columns: repeat(7, 1fr);
        gap: 1px;
        background: var(--border-color);
        border-radius: 8px;
        overflow: hidden;
    }

    .calendar-day-header {
        background: var(--bg-secondary);
        padding: 12px;
        text-align: center;
        font-weight: 500;
        font-size: 0.75rem;
        color: var(--text-secondary);
        text-transform: uppercase;
    }

    .calendar-day {
        background: var(--card-bg);
        min-height: 100px;
        padding: 8px;
        cursor: pointer;
        transition: background 0.2s;
    }

    .calendar-day:hover {
        background: var(--bg-secondary);
    }

    .calendar-day.other-month {
        opacity: 0.4;
    }

    .calendar-day.today {
        background: rgba(59, 130, 246, 0.1);
    }

    .day-number {
        font-weight: 500;
        margin-bottom: 4px;
    }

    .today .day-number {
        background: var(--accent-blue);
        color: white;
        width: 28px;
        height: 28px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    /* Eventos no calendario */
    .day-events {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    .day-event {
        font-size: 0.75rem;
        padding: 2px 6px;
        border-radius: 4px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        cursor: pointer;
    }

    .day-event.meeting {
        background: rgba(59, 130, 246, 0.2);
        color: var(--accent-blue);
    }

    .day-event.reminder {
        background: rgba(245, 158, 11, 0.2);
        color: #f59e0b;
    }

    .day-event.ai-suggested {
        background: rgba(168, 85, 247, 0.2);
        color: #a855f7;
    }

    /* Sidebar */
    .today-events {
        background: var(--card-bg);
        border-radius: 12px;
        padding: 16px;
    }

    .today-events h3 {
        font-size: 1rem;
        margin-bottom: 16px;
        display: flex;
        align-items: center;
        gap: 8px;
    }

    .event-item {
        display: flex;
        gap: 12px;
        padding: 12px;
        border-radius: 8px;
        background: var(--bg-secondary);
        margin-bottom: 8px;
        cursor: pointer;
        transition: transform 0.2s;
    }

    .event-item:hover {
        transform: translateX(4px);
    }

    .event-time {
        font-size: 0.875rem;
        color: var(--text-secondary);
        min-width: 50px;
    }

    .event-details {
        flex: 1;
    }

    .event-title {
        font-weight: 500;
        margin-bottom: 4px;
    }

    .event-contact {
        font-size: 0.875rem;
        color: var(--text-secondary);
    }

    /* AI Suggestions Card */
    .ai-suggestions-card {
        background: linear-gradient(135deg, rgba(168, 85, 247, 0.1), rgba(59, 130, 246, 0.1));
        border: 1px solid rgba(168, 85, 247, 0.2);
        border-radius: 12px;
        padding: 16px;
    }

    .ai-suggestions-card h3 {
        font-size: 1rem;
        margin-bottom: 16px;
        display: flex;
        align-items: center;
        gap: 8px;
        color: #a855f7;
    }

    .ai-suggestion-item {
        background: var(--card-bg);
        border-radius: 8px;
        padding: 12px;
        margin-bottom: 8px;
    }

    .ai-suggestion-item .contact-name {
        font-weight: 500;
        margin-bottom: 4px;
    }

    .ai-suggestion-item .reason {
        font-size: 0.875rem;
        color: var(--text-secondary);
        margin-bottom: 8px;
    }

    .ai-suggestion-item .actions {
        display: flex;
        gap: 8px;
    }

    .btn-accept {
        background: #a855f7;
        color: white;
        border: none;
        padding: 6px 12px;
        border-radius: 6px;
        font-size: 0.875rem;
        cursor: pointer;
    }

    .btn-dismiss {
        background: var(--bg-secondary);
        color: var(--text-secondary);
        border: none;
        padding: 6px 12px;
        border-radius: 6px;
        font-size: 0.875rem;
        cursor: pointer;
    }

    /* Modal de evento */
    .event-modal {
        display: none;
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0,0,0,0.5);
        z-index: 1000;
        align-items: center;
        justify-content: center;
    }

    .event-modal.active {
        display: flex;
    }

    .event-modal-content {
        background: var(--card-bg);
        border-radius: 16px;
        width: 90%;
        max-width: 500px;
        max-height: 80vh;
        overflow-y: auto;
    }

    .event-modal-header {
        padding: 20px;
        border-bottom: 1px solid var(--border-color);
        display: flex;
        justify-content: space-between;
        align-items: center;
    }

    .event-modal-body {
        padding: 20px;
    }

    .form-group {
        margin-bottom: 16px;
    }

    .form-group label {
        display: block;
        font-size: 0.875rem;
        color: var(--text-secondary);
        margin-bottom: 6px;
    }

    .form-group input,
    .form-group select,
    .form-group textarea {
        width: 100%;
        padding: 10px 12px;
        border: 1px solid var(--border-color);
        border-radius: 8px;
        background: var(--bg-secondary);
        color: var(--text-primary);
        font-size: 0.875rem;
    }

    .form-row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
    }

    .event-modal-footer {
        padding: 16px 20px;
        border-top: 1px solid var(--border-color);
        display: flex;
        justify-content: flex-end;
        gap: 8px;
    }

    /* Responsivo */
    @media (max-width: 1024px) {
        .calendar-container {
            grid-template-columns: 1fr;
        }

        .calendar-sidebar {
            flex-direction: row;
            overflow-x: auto;
        }

        .today-events,
        .ai-suggestions-card {
            min-width: 280px;
        }
    }

    @media (max-width: 768px) {
        .calendar-day {
            min-height: 60px;
            padding: 4px;
        }

        .day-events {
            display: none;
        }

        .calendar-header {
            flex-direction: column;
            gap: 12px;
        }
    }
</style>
{% endblock %}

{% block content %}
<div class="page-header">
    <div>
        <h1>Calendario</h1>
        <p class="subtitle">Seus eventos e reunioes</p>
    </div>
    <button class="btn btn-primary" onclick="openNewEventModal()">
        <i data-lucide="plus"></i>
        Novo Evento
    </button>
</div>

<div class="calendar-container">
    <div class="calendar-main">
        <div class="calendar-header">
            <div class="calendar-nav">
                <button onclick="previousMonth()"><i data-lucide="chevron-left"></i></button>
                <span class="calendar-title" id="calendarTitle">Marco 2026</span>
                <button onclick="nextMonth()"><i data-lucide="chevron-right"></i></button>
                <button onclick="goToToday()" style="margin-left: 8px;">Hoje</button>
            </div>
            <div class="view-toggle">
                <button class="active" data-view="month">Mes</button>
                <button data-view="week">Semana</button>
                <button data-view="day">Dia</button>
            </div>
        </div>

        <div class="calendar-grid" id="calendarGrid">
            <!-- Gerado via JS -->
        </div>
    </div>

    <div class="calendar-sidebar">
        <div class="today-events">
            <h3><i data-lucide="calendar-days"></i> Hoje</h3>
            <div id="todayEvents">
                <p class="text-secondary">Nenhum evento hoje</p>
            </div>
        </div>

        <div class="ai-suggestions-card">
            <h3><i data-lucide="sparkles"></i> Sugestoes AI</h3>
            <div id="aiSuggestions">
                <p class="text-secondary">Carregando...</p>
            </div>
        </div>
    </div>
</div>

<!-- Modal de Evento -->
<div class="event-modal" id="eventModal">
    <div class="event-modal-content">
        <div class="event-modal-header">
            <h3 id="eventModalTitle">Novo Evento</h3>
            <button onclick="closeEventModal()" style="background:none;border:none;cursor:pointer;color:var(--text-secondary);">
                <i data-lucide="x"></i>
            </button>
        </div>
        <div class="event-modal-body">
            <form id="eventForm">
                <input type="hidden" id="eventId">

                <div class="form-group">
                    <label>Titulo</label>
                    <input type="text" id="eventTitle" required placeholder="Reuniao com...">
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Inicio</label>
                        <input type="datetime-local" id="eventStart" required>
                    </div>
                    <div class="form-group">
                        <label>Fim</label>
                        <input type="datetime-local" id="eventEnd" required>
                    </div>
                </div>

                <div class="form-group">
                    <label>Contato</label>
                    <select id="eventContact">
                        <option value="">Selecione um contato (opcional)</option>
                    </select>
                </div>

                <div class="form-group">
                    <label>Descricao</label>
                    <textarea id="eventDescription" rows="3" placeholder="Detalhes do evento..."></textarea>
                </div>

                <div class="form-group">
                    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
                        <input type="checkbox" id="createMeet" checked>
                        Criar link do Google Meet
                    </label>
                </div>
            </form>
        </div>
        <div class="event-modal-footer">
            <button class="btn btn-secondary" onclick="closeEventModal()">Cancelar</button>
            <button class="btn btn-primary" onclick="saveEvent()">Salvar</button>
        </div>
    </div>
</div>

<script>
let currentDate = new Date();
let currentView = 'month';
let events = [];
let contacts = [];

const MONTHS = ['Janeiro', 'Fevereiro', 'Marco', 'Abril', 'Maio', 'Junho',
                'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro'];
const DAYS = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sab'];

document.addEventListener('DOMContentLoaded', function() {
    lucide.createIcons();
    loadEvents();
    loadContacts();
    loadAISuggestions();
    renderCalendar();

    // View toggle
    document.querySelectorAll('.view-toggle button').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.view-toggle button').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            currentView = this.dataset.view;
            renderCalendar();
        });
    });
});

async function loadEvents() {
    try {
        const start = new Date(currentDate.getFullYear(), currentDate.getMonth(), 1);
        const end = new Date(currentDate.getFullYear(), currentDate.getMonth() + 1, 0);

        const response = await fetch(`/api/calendar/events?start=${start.toISOString()}&end=${end.toISOString()}`);
        if (response.ok) {
            const data = await response.json();
            events = data.events || [];
        }
    } catch (e) {
        console.error('Erro ao carregar eventos:', e);
        // Mock data
        events = [
            { id: 1, summary: 'Reuniao com Joao', start_datetime: '2026-03-27T10:00:00', contact_name: 'Joao Silva' },
            { id: 2, summary: 'Call com Cliente', start_datetime: '2026-03-28T14:00:00', contact_name: 'Maria Santos' }
        ];
    }
    renderCalendar();
    renderTodayEvents();
}

async function loadContacts() {
    try {
        const response = await fetch('/api/contacts?limit=100');
        if (response.ok) {
            const data = await response.json();
            contacts = data.contacts || [];
            populateContactSelect();
        }
    } catch (e) {
        console.error('Erro ao carregar contatos:', e);
    }
}

function populateContactSelect() {
    const select = document.getElementById('eventContact');
    contacts.forEach(c => {
        const option = document.createElement('option');
        option.value = c.id;
        option.textContent = c.nome + (c.empresa ? ` (${c.empresa})` : '');
        select.appendChild(option);
    });
}

async function loadAISuggestions() {
    const container = document.getElementById('aiSuggestions');

    try {
        const response = await fetch('/api/ai/calendar-suggestions?limit=5');
        if (response.ok) {
            const data = await response.json();
            renderAISuggestions(data.suggestions || []);
        } else {
            throw new Error('API error');
        }
    } catch (e) {
        // Mock data
        renderAISuggestions([
            { id: 1, contact_name: 'Pedro Costa', titulo: 'Agendar reuniao', descricao: 'Health 25% - 45 dias sem contato' },
            { id: 2, contact_name: 'Ana Souza', titulo: 'Agendar reuniao', descricao: 'Health 30% - 30 dias sem contato' }
        ]);
    }
}

function renderAISuggestions(suggestions) {
    const container = document.getElementById('aiSuggestions');

    if (!suggestions.length) {
        container.innerHTML = '<p class="text-secondary">Nenhuma sugestao no momento</p>';
        return;
    }

    container.innerHTML = suggestions.map(s => `
        <div class="ai-suggestion-item">
            <div class="contact-name">${s.contact_name || s.titulo}</div>
            <div class="reason">${s.descricao}</div>
            <div class="actions">
                <button class="btn-accept" onclick="acceptSuggestion(${s.id})">
                    <i data-lucide="check" style="width:14px;height:14px;"></i> Agendar
                </button>
                <button class="btn-dismiss" onclick="dismissSuggestion(${s.id})">
                    <i data-lucide="x" style="width:14px;height:14px;"></i>
                </button>
            </div>
        </div>
    `).join('');

    lucide.createIcons();
}

async function acceptSuggestion(suggestionId) {
    try {
        const response = await fetch(`/api/ai/calendar-suggestions/${suggestionId}/create-event`, {
            method: 'POST'
        });
        if (response.ok) {
            showToast('Evento criado com sucesso!');
            loadEvents();
            loadAISuggestions();
        }
    } catch (e) {
        showToast('Erro ao criar evento', 'error');
    }
}

async function dismissSuggestion(suggestionId) {
    try {
        await fetch(`/api/ai/suggestions/${suggestionId}/dismiss`, { method: 'POST' });
        loadAISuggestions();
    } catch (e) {}
}

function renderCalendar() {
    const grid = document.getElementById('calendarGrid');
    const title = document.getElementById('calendarTitle');

    title.textContent = `${MONTHS[currentDate.getMonth()]} ${currentDate.getFullYear()}`;

    const year = currentDate.getFullYear();
    const month = currentDate.getMonth();

    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);
    const startDayOfWeek = firstDay.getDay();

    let html = DAYS.map(d => `<div class="calendar-day-header">${d}</div>`).join('');

    // Dias do mes anterior
    const prevMonth = new Date(year, month, 0);
    for (let i = startDayOfWeek - 1; i >= 0; i--) {
        const day = prevMonth.getDate() - i;
        html += `<div class="calendar-day other-month"><div class="day-number">${day}</div></div>`;
    }

    // Dias do mes atual
    const today = new Date();
    for (let day = 1; day <= lastDay.getDate(); day++) {
        const isToday = day === today.getDate() && month === today.getMonth() && year === today.getFullYear();
        const dateStr = `${year}-${String(month+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
        const dayEvents = events.filter(e => e.start_datetime && e.start_datetime.startsWith(dateStr));

        html += `
            <div class="calendar-day ${isToday ? 'today' : ''}" onclick="selectDate('${dateStr}')">
                <div class="day-number">${day}</div>
                <div class="day-events">
                    ${dayEvents.slice(0, 2).map(e => `
                        <div class="day-event meeting" onclick="event.stopPropagation(); openEventDetail(${e.id})">
                            ${e.summary}
                        </div>
                    `).join('')}
                    ${dayEvents.length > 2 ? `<div class="day-event" style="background:var(--bg-tertiary);">+${dayEvents.length - 2} mais</div>` : ''}
                </div>
            </div>
        `;
    }

    // Dias do proximo mes
    const remainingDays = 42 - (startDayOfWeek + lastDay.getDate());
    for (let day = 1; day <= remainingDays; day++) {
        html += `<div class="calendar-day other-month"><div class="day-number">${day}</div></div>`;
    }

    grid.innerHTML = html;
}

function renderTodayEvents() {
    const container = document.getElementById('todayEvents');
    const today = new Date().toISOString().split('T')[0];
    const todayEvents = events.filter(e => e.start_datetime && e.start_datetime.startsWith(today));

    if (!todayEvents.length) {
        container.innerHTML = '<p class="text-secondary">Nenhum evento hoje</p>';
        return;
    }

    container.innerHTML = todayEvents.map(e => {
        const time = new Date(e.start_datetime).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        return `
            <div class="event-item" onclick="openEventDetail(${e.id})">
                <div class="event-time">${time}</div>
                <div class="event-details">
                    <div class="event-title">${e.summary}</div>
                    <div class="event-contact">${e.contact_name || ''}</div>
                </div>
            </div>
        `;
    }).join('');
}

function previousMonth() {
    currentDate.setMonth(currentDate.getMonth() - 1);
    loadEvents();
}

function nextMonth() {
    currentDate.setMonth(currentDate.getMonth() + 1);
    loadEvents();
}

function goToToday() {
    currentDate = new Date();
    loadEvents();
}

function selectDate(dateStr) {
    document.getElementById('eventStart').value = dateStr + 'T10:00';
    document.getElementById('eventEnd').value = dateStr + 'T10:30';
    openNewEventModal();
}

function openNewEventModal() {
    document.getElementById('eventModalTitle').textContent = 'Novo Evento';
    document.getElementById('eventForm').reset();
    document.getElementById('eventId').value = '';

    // Default times
    const now = new Date();
    now.setMinutes(0);
    now.setSeconds(0);
    const start = now.toISOString().slice(0, 16);
    now.setMinutes(30);
    const end = now.toISOString().slice(0, 16);

    if (!document.getElementById('eventStart').value) {
        document.getElementById('eventStart').value = start;
        document.getElementById('eventEnd').value = end;
    }

    document.getElementById('eventModal').classList.add('active');
}

function closeEventModal() {
    document.getElementById('eventModal').classList.remove('active');
}

async function saveEvent() {
    const eventId = document.getElementById('eventId').value;
    const data = {
        summary: document.getElementById('eventTitle').value,
        start_datetime: document.getElementById('eventStart').value,
        end_datetime: document.getElementById('eventEnd').value,
        description: document.getElementById('eventDescription').value,
        contact_id: document.getElementById('eventContact').value || null,
        create_in_google: document.getElementById('createMeet').checked
    };

    try {
        const url = eventId ? `/api/calendar/events/${eventId}` : '/api/calendar/events';
        const method = eventId ? 'PUT' : 'POST';

        const response = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            showToast('Evento salvo com sucesso!');
            closeEventModal();
            loadEvents();
        } else {
            throw new Error('Erro ao salvar');
        }
    } catch (e) {
        showToast('Erro ao salvar evento', 'error');
    }
}

function openEventDetail(eventId) {
    const event = events.find(e => e.id === eventId);
    if (!event) return;

    document.getElementById('eventModalTitle').textContent = 'Editar Evento';
    document.getElementById('eventId').value = event.id;
    document.getElementById('eventTitle').value = event.summary;
    document.getElementById('eventStart').value = event.start_datetime.slice(0, 16);
    document.getElementById('eventEnd').value = event.end_datetime ? event.end_datetime.slice(0, 16) : '';
    document.getElementById('eventDescription').value = event.description || '';
    document.getElementById('eventContact').value = event.contact_id || '';

    document.getElementById('eventModal').classList.add('active');
}

function showToast(message, type = 'success') {
    // Usar toast global se existir
    if (window.showToast) {
        window.showToast(message, type);
    } else {
        alert(message);
    }
}
</script>
{% endblock %}
```

### Adicionar rota em main.py

```python
@app.get("/calendario", response_class=HTMLResponse)
async def calendario_page(request: Request):
    """Pagina de calendario"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("rap_calendario.html", {"request": request, "user": user})
```

### Adicionar link no sidebar (rap_base.html)

Adicionar na lista de navegacao:

```html
<a href="/calendario" class="nav-item {% if request.url.path == '/calendario' %}active{% endif %}">
    <i data-lucide="calendar"></i>
    <span>Calendario</span>
</a>
```

**Commit**: `git commit -m "feat(calendar): Add Calendar page with month view"`

---

## TAREFA 2: Widget de Calendario no Dashboard

**Status**: PENDENTE
**Prioridade**: ALTA

### Modificar `app/templates/rap_dashboard.html`

Adicionar widget de eventos de hoje e sugestoes de reuniao.

Na secao de widgets do dashboard, adicionar:

```html
<!-- Widget Calendario -->
<div class="card">
    <div class="card-header">
        <h3><i data-lucide="calendar-days"></i> Agenda de Hoje</h3>
        <a href="/calendario" class="btn btn-sm">Ver tudo</a>
    </div>
    <div class="card-body" id="todayCalendarWidget">
        <p class="text-secondary">Carregando...</p>
    </div>
</div>

<!-- Widget Sugestoes de Reuniao -->
<div class="card ai-card">
    <div class="card-header">
        <h3><i data-lucide="sparkles"></i> Agendar Reuniao</h3>
    </div>
    <div class="card-body" id="meetingSuggestionsWidget">
        <p class="text-secondary">Carregando...</p>
    </div>
</div>
```

Adicionar no script:

```javascript
// Carregar eventos de hoje
async function loadTodayCalendar() {
    const container = document.getElementById('todayCalendarWidget');
    if (!container) return;

    try {
        const today = new Date().toISOString().split('T')[0];
        const response = await fetch(`/api/calendar/events?date=${today}`);
        if (response.ok) {
            const data = await response.json();
            renderTodayCalendar(data.events || []);
        }
    } catch (e) {
        container.innerHTML = '<p class="text-secondary">Sem eventos hoje</p>';
    }
}

function renderTodayCalendar(events) {
    const container = document.getElementById('todayCalendarWidget');

    if (!events.length) {
        container.innerHTML = '<p class="text-secondary">Sem eventos hoje</p>';
        return;
    }

    container.innerHTML = events.slice(0, 3).map(e => {
        const time = new Date(e.start_datetime).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        return `
            <div class="event-item" style="display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--border-color);">
                <div style="font-weight:500;min-width:50px;">${time}</div>
                <div>
                    <div style="font-weight:500;">${e.summary}</div>
                    ${e.contact_name ? `<div style="font-size:0.875rem;color:var(--text-secondary);">${e.contact_name}</div>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

// Carregar sugestoes de reuniao
async function loadMeetingSuggestions() {
    const container = document.getElementById('meetingSuggestionsWidget');
    if (!container) return;

    try {
        const response = await fetch('/api/ai/calendar-suggestions?limit=3');
        if (response.ok) {
            const data = await response.json();
            renderMeetingSuggestions(data.suggestions || []);
        }
    } catch (e) {
        container.innerHTML = '<p class="text-secondary">Nenhuma sugestao</p>';
    }
}

function renderMeetingSuggestions(suggestions) {
    const container = document.getElementById('meetingSuggestionsWidget');

    if (!suggestions.length) {
        container.innerHTML = '<p class="text-secondary">Todos os contatos estao em dia!</p>';
        return;
    }

    container.innerHTML = suggestions.map(s => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border-color);">
            <div>
                <div style="font-weight:500;">${s.contact_name || s.titulo}</div>
                <div style="font-size:0.75rem;color:var(--text-secondary);">${s.descricao}</div>
            </div>
            <button onclick="acceptCalendarSuggestion(${s.id})" class="btn btn-sm btn-primary">
                <i data-lucide="calendar-plus" style="width:14px;height:14px;"></i>
            </button>
        </div>
    `).join('');

    lucide.createIcons();
}

async function acceptCalendarSuggestion(id) {
    try {
        const response = await fetch(`/api/ai/calendar-suggestions/${id}/create-event`, { method: 'POST' });
        if (response.ok) {
            showToast('Reuniao agendada!');
            loadMeetingSuggestions();
            loadTodayCalendar();
        }
    } catch (e) {
        showToast('Erro ao agendar', 'error');
    }
}

// Chamar no DOMContentLoaded
loadTodayCalendar();
loadMeetingSuggestions();
```

**Commit**: `git commit -m "feat(dashboard): Add calendar and meeting suggestions widgets"`

---

## TAREFA 3: Secao de Calendario na Pagina de Contato

**Status**: PENDENTE
**Prioridade**: MEDIA

### Modificar `app/templates/rap_contact_detail.html`

Adicionar secao de eventos/reunioes do contato:

```html
<!-- Secao de Calendario/Eventos -->
<div class="section-card">
    <div class="section-header">
        <h3><i data-lucide="calendar"></i> Reunioes</h3>
        <button class="btn btn-sm btn-primary" onclick="openNewMeetingModal()">
            <i data-lucide="plus"></i> Agendar
        </button>
    </div>
    <div class="section-body" id="contactMeetings">
        <p class="text-secondary">Carregando...</p>
    </div>
</div>
```

Adicionar no script:

```javascript
async function loadContactMeetings() {
    const container = document.getElementById('contactMeetings');
    if (!container) return;

    try {
        const response = await fetch(`/api/contacts/${contactId}/calendar`);
        if (response.ok) {
            const data = await response.json();
            renderContactMeetings(data.events || []);
        }
    } catch (e) {
        container.innerHTML = '<p class="text-secondary">Nenhuma reuniao agendada</p>';
    }
}

function renderContactMeetings(events) {
    const container = document.getElementById('contactMeetings');

    if (!events.length) {
        container.innerHTML = '<p class="text-secondary">Nenhuma reuniao agendada</p>';
        return;
    }

    // Separar futuros e passados
    const now = new Date();
    const future = events.filter(e => new Date(e.start_datetime) > now);
    const past = events.filter(e => new Date(e.start_datetime) <= now);

    let html = '';

    if (future.length) {
        html += '<h4 style="font-size:0.875rem;color:var(--text-secondary);margin-bottom:8px;">Proximas</h4>';
        html += future.slice(0, 3).map(e => renderMeetingItem(e, 'upcoming')).join('');
    }

    if (past.length) {
        html += '<h4 style="font-size:0.875rem;color:var(--text-secondary);margin:16px 0 8px;">Anteriores</h4>';
        html += past.slice(0, 5).map(e => renderMeetingItem(e, 'past')).join('');
    }

    container.innerHTML = html;
}

function renderMeetingItem(event, type) {
    const date = new Date(event.start_datetime);
    const dateStr = date.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' });
    const timeStr = date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });

    return `
        <div class="meeting-item" style="display:flex;gap:12px;padding:12px;background:var(--bg-secondary);border-radius:8px;margin-bottom:8px;${type === 'past' ? 'opacity:0.7;' : ''}">
            <div style="text-align:center;min-width:50px;">
                <div style="font-size:0.75rem;color:var(--text-secondary);">${dateStr}</div>
                <div style="font-weight:600;">${timeStr}</div>
            </div>
            <div style="flex:1;">
                <div style="font-weight:500;">${event.summary}</div>
                ${event.conference_url ? `<a href="${event.conference_url}" target="_blank" style="font-size:0.875rem;color:var(--accent-blue);">Entrar na reuniao</a>` : ''}
            </div>
        </div>
    `;
}

function openNewMeetingModal() {
    // Abrir modal de novo evento com contato pre-selecionado
    const modal = document.getElementById('newMeetingModal');
    if (modal) {
        document.getElementById('meetingContactId').value = contactId;
        modal.classList.add('active');
    } else {
        // Fallback: redirecionar para calendario
        window.location.href = `/calendario?contact=${contactId}`;
    }
}

// Chamar no load
loadContactMeetings();
```

**Commit**: `git commit -m "feat(contact): Add meetings section to contact detail page"`

---

## TAREFA 4: Sincronizar Estilos e Componentes

**Status**: PENDENTE
**Prioridade**: BAIXA

### Garantir consistencia visual

1. Verificar se todos os icones do Lucide estao funcionando
2. Verificar responsividade em mobile
3. Testar dark mode

### Adicionar CSS global para calendario (rap_base.html ou styles.css)

```css
/* Calendar Global Styles */
.event-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 500;
}

.event-badge.meeting {
    background: rgba(59, 130, 246, 0.1);
    color: var(--accent-blue);
}

.event-badge.reminder {
    background: rgba(245, 158, 11, 0.1);
    color: #f59e0b;
}

.event-badge.ai {
    background: rgba(168, 85, 247, 0.1);
    color: #a855f7;
}
```

**Commit**: `git commit -m "style: Add calendar global styles and consistency fixes"`

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
| - | Pagina de Calendario | PENDENTE |
| - | Widget Calendario Dashboard | PENDENTE |
| - | Secao Calendario Contato | PENDENTE |
| - | Estilos Globais | PENDENTE |
