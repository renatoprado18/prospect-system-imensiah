# Fila de Tarefas 3FLOW - AI Avançado Frontend

**Atualizacao**: 2026-03-27
**Modo**: AUTONOMO TOTAL - executar TODAS as tarefas sem parar

---

## INSTRUCOES GERAIS

- NAO perguntar nada, apenas executar
- Se encontrar erro, resolver e continuar
- Commit apos cada tarefa concluida
- Push direto em main
- Atualizar este arquivo marcando tarefas como CONCLUIDAS

---

## TAREFA 1: Painel de Sugestoes AI

**Status**: EXECUTAR AGORA
**Prioridade**: CRITICA

### Criar componente de sugestoes em rap_base.html ou nova pagina

Adicionar sidebar ou secao no dashboard mostrando sugestoes AI.

### 1.1 Adicionar CSS em rap_base.html (ou arquivo CSS)

```css
/* AI Suggestions Panel */
.ai-suggestions-panel {
    position: fixed;
    right: 0;
    top: 60px;
    width: 320px;
    height: calc(100vh - 60px);
    background: white;
    border-left: 1px solid #e5e7eb;
    overflow-y: auto;
    transform: translateX(100%);
    transition: transform 0.3s ease;
    z-index: 40;
}

.ai-suggestions-panel.open {
    transform: translateX(0);
}

.ai-panel-header {
    padding: 16px;
    border-bottom: 1px solid #e5e7eb;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
}

.ai-panel-title {
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
}

.ai-suggestion-card {
    padding: 12px 16px;
    border-bottom: 1px solid #f3f4f6;
    cursor: pointer;
    transition: background 0.2s;
}

.ai-suggestion-card:hover {
    background: #f9fafb;
}

.ai-suggestion-card.type-reconnect {
    border-left: 3px solid #f59e0b;
}

.ai-suggestion-card.type-birthday {
    border-left: 3px solid #ec4899;
}

.ai-suggestion-card.type-followup {
    border-left: 3px solid #3b82f6;
}

.suggestion-title {
    font-weight: 500;
    color: #111827;
    margin-bottom: 4px;
}

.suggestion-desc {
    font-size: 13px;
    color: #6b7280;
    margin-bottom: 8px;
}

.suggestion-actions {
    display: flex;
    gap: 8px;
}

.suggestion-btn {
    padding: 4px 12px;
    border-radius: 4px;
    font-size: 12px;
    cursor: pointer;
    border: none;
}

.suggestion-btn.accept {
    background: #10b981;
    color: white;
}

.suggestion-btn.dismiss {
    background: #e5e7eb;
    color: #4b5563;
}

.ai-toggle-btn {
    position: fixed;
    right: 20px;
    bottom: 20px;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    border: none;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
}

.ai-toggle-btn .badge {
    position: absolute;
    top: -4px;
    right: -4px;
    background: #ef4444;
    color: white;
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 10px;
    font-weight: 600;
}

@media (max-width: 768px) {
    .ai-suggestions-panel {
        width: 100%;
    }
}
```

### 1.2 Adicionar HTML

```html
<!-- AI Toggle Button -->
<button class="ai-toggle-btn" onclick="toggleAIPanel()">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 0 1 7 7h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-1H2a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1a7 7 0 0 1 7-7h1V5.73c-.6-.34-1-.99-1-1.73a2 2 0 0 1 2-2z"/>
        <circle cx="8" cy="14" r="1"/><circle cx="16" cy="14" r="1"/>
    </svg>
    <span class="badge" id="ai-badge" style="display: none;">0</span>
</button>

<!-- AI Suggestions Panel -->
<div class="ai-suggestions-panel" id="ai-panel">
    <div class="ai-panel-header">
        <div class="ai-panel-title">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 0 1 7 7h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-1H2a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1a7 7 0 0 1 7-7h1V5.73c-.6-.34-1-.99-1-1.73a2 2 0 0 1 2-2z"/>
            </svg>
            Sugestoes AI
        </div>
        <button onclick="toggleAIPanel()" style="background:none;border:none;color:white;cursor:pointer;">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M18 6L6 18M6 6l12 12"/>
            </svg>
        </button>
    </div>
    <div id="ai-suggestions-list">
        <!-- Suggestions loaded via JS -->
        <div style="padding: 20px; text-align: center; color: #9ca3af;">
            Carregando sugestoes...
        </div>
    </div>
</div>
```

### 1.3 Adicionar JavaScript

```javascript
let aiPanelOpen = false;

function toggleAIPanel() {
    const panel = document.getElementById('ai-panel');
    aiPanelOpen = !aiPanelOpen;
    panel.classList.toggle('open', aiPanelOpen);
    if (aiPanelOpen) {
        loadAISuggestions();
    }
}

async function loadAISuggestions() {
    try {
        const response = await fetch('/api/ai/suggestions?limit=20');
        const data = await response.json();
        renderAISuggestions(data.suggestions || []);
        updateAIBadge(data.suggestions?.length || 0);
    } catch (error) {
        console.error('Erro ao carregar sugestoes:', error);
    }
}

function renderAISuggestions(suggestions) {
    const container = document.getElementById('ai-suggestions-list');

    if (!suggestions.length) {
        container.innerHTML = `
            <div style="padding: 40px 20px; text-align: center; color: #9ca3af;">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" style="margin: 0 auto 12px;">
                    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                    <polyline points="22 4 12 14.01 9 11.01"/>
                </svg>
                <p>Nenhuma sugestao no momento</p>
                <p style="font-size: 12px;">A AI esta analisando seus contatos</p>
            </div>
        `;
        return;
    }

    container.innerHTML = suggestions.map(s => `
        <div class="ai-suggestion-card type-${s.tipo}" data-id="${s.id}">
            <div class="suggestion-title">${escapeHtml(s.titulo)}</div>
            <div class="suggestion-desc">${escapeHtml(s.descricao || '')}</div>
            ${s.contact_name ? `<div style="font-size: 12px; color: #9ca3af; margin-bottom: 8px;">
                ${s.contact_foto ? `<img src="${s.contact_foto}" style="width:20px;height:20px;border-radius:50%;vertical-align:middle;margin-right:4px;">` : ''}
                ${escapeHtml(s.contact_name)}
            </div>` : ''}
            <div class="suggestion-actions">
                <button class="suggestion-btn accept" onclick="acceptSuggestion(${s.id}, ${s.contact_id})">
                    Aceitar
                </button>
                <button class="suggestion-btn dismiss" onclick="dismissSuggestion(${s.id})">
                    Ignorar
                </button>
            </div>
        </div>
    `).join('');
}

function updateAIBadge(count) {
    const badge = document.getElementById('ai-badge');
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : count;
        badge.style.display = 'block';
    } else {
        badge.style.display = 'none';
    }
}

async function acceptSuggestion(id, contactId) {
    try {
        await fetch(`/api/ai/suggestions/${id}/accept`, { method: 'POST' });
        // Navegar para o contato se tiver
        if (contactId) {
            window.location.href = `/contato/${contactId}`;
        } else {
            loadAISuggestions();
        }
    } catch (error) {
        console.error('Erro ao aceitar sugestao:', error);
    }
}

async function dismissSuggestion(id) {
    try {
        await fetch(`/api/ai/suggestions/${id}/dismiss`, { method: 'POST' });
        // Remover card da lista
        document.querySelector(`.ai-suggestion-card[data-id="${id}"]`)?.remove();
        // Atualizar badge
        const remaining = document.querySelectorAll('.ai-suggestion-card').length;
        updateAIBadge(remaining);
    } catch (error) {
        console.error('Erro ao descartar sugestao:', error);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Carregar badge inicial
document.addEventListener('DOMContentLoaded', async () => {
    try {
        const response = await fetch('/api/ai/suggestions?limit=1');
        const data = await response.json();
        // Fazer segunda request para pegar total
        const fullResponse = await fetch('/api/ai/suggestions?limit=100');
        const fullData = await fullResponse.json();
        updateAIBadge(fullData.suggestions?.length || 0);
    } catch (e) {}
});
```

**Commit**: `git commit -m "Add AI suggestions panel with toggle button"`

---

## TAREFA 2: Modal de Detalhes da Sugestao

**Status**: PENDENTE
**Prioridade**: ALTA

### Criar modal para mostrar detalhes e acoes

```html
<!-- Modal de Sugestao -->
<div id="suggestion-modal" class="modal" style="display: none;">
    <div class="modal-backdrop" onclick="closeSuggestionModal()"></div>
    <div class="modal-content" style="max-width: 480px;">
        <div class="modal-header">
            <h3 id="modal-title">Sugestao AI</h3>
            <button onclick="closeSuggestionModal()" class="modal-close">&times;</button>
        </div>
        <div class="modal-body" id="modal-body">
            <!-- Content loaded dynamically -->
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="closeSuggestionModal()">Fechar</button>
            <button class="btn btn-primary" id="modal-action-btn">Executar Acao</button>
        </div>
    </div>
</div>
```

```css
.modal {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: center;
}

.modal-backdrop {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0,0,0,0.5);
}

.modal-content {
    position: relative;
    background: white;
    border-radius: 12px;
    width: 90%;
    max-height: 80vh;
    overflow: hidden;
    box-shadow: 0 20px 50px rgba(0,0,0,0.2);
}

.modal-header {
    padding: 16px 20px;
    border-bottom: 1px solid #e5e7eb;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.modal-body {
    padding: 20px;
    overflow-y: auto;
    max-height: 400px;
}

.modal-footer {
    padding: 16px 20px;
    border-top: 1px solid #e5e7eb;
    display: flex;
    justify-content: flex-end;
    gap: 12px;
}

.modal-close {
    background: none;
    border: none;
    font-size: 24px;
    cursor: pointer;
    color: #6b7280;
}
```

```javascript
let currentSuggestion = null;

function openSuggestionModal(suggestion) {
    currentSuggestion = suggestion;
    const modal = document.getElementById('suggestion-modal');
    const title = document.getElementById('modal-title');
    const body = document.getElementById('modal-body');
    const actionBtn = document.getElementById('modal-action-btn');

    title.textContent = suggestion.titulo;

    let bodyContent = `<p style="margin-bottom: 16px;">${escapeHtml(suggestion.descricao || '')}</p>`;

    if (suggestion.contact_name) {
        bodyContent += `
            <div style="display: flex; align-items: center; gap: 12px; padding: 12px; background: #f9fafb; border-radius: 8px; margin-bottom: 16px;">
                ${suggestion.contact_foto
                    ? `<img src="${suggestion.contact_foto}" style="width: 48px; height: 48px; border-radius: 50%;">`
                    : `<div style="width: 48px; height: 48px; border-radius: 50%; background: #e5e7eb; display: flex; align-items: center; justify-content: center; font-weight: 600;">${suggestion.contact_name[0]}</div>`
                }
                <div>
                    <div style="font-weight: 600;">${escapeHtml(suggestion.contact_name)}</div>
                    <div style="font-size: 13px; color: #6b7280;">Contato</div>
                </div>
            </div>
        `;
    }

    if (suggestion.contexto) {
        const ctx = typeof suggestion.contexto === 'string' ? JSON.parse(suggestion.contexto) : suggestion.contexto;
        if (ctx.health !== undefined) {
            bodyContent += `<div style="margin-bottom: 8px;"><strong>Health:</strong> ${ctx.health}%</div>`;
        }
        if (ctx.dias_sem_contato !== undefined) {
            bodyContent += `<div style="margin-bottom: 8px;"><strong>Dias sem contato:</strong> ${ctx.dias_sem_contato}</div>`;
        }
    }

    body.innerHTML = bodyContent;

    // Configurar botao de acao
    const acao = suggestion.acao_sugerida;
    if (acao?.action === 'send_message' || suggestion.tipo === 'reconnect') {
        actionBtn.textContent = 'Enviar Mensagem';
        actionBtn.onclick = () => goToContactMessage(suggestion.contact_id);
    } else if (acao?.action === 'send_birthday_message' || suggestion.tipo === 'birthday') {
        actionBtn.textContent = 'Enviar Parabens';
        actionBtn.onclick = () => goToContactMessage(suggestion.contact_id);
    } else {
        actionBtn.textContent = 'Ver Contato';
        actionBtn.onclick = () => window.location.href = `/contato/${suggestion.contact_id}`;
    }

    modal.style.display = 'flex';
}

function closeSuggestionModal() {
    document.getElementById('suggestion-modal').style.display = 'none';
    currentSuggestion = null;
}

function goToContactMessage(contactId) {
    closeSuggestionModal();
    window.location.href = `/contato/${contactId}#mensagem`;
}
```

**Commit**: `git commit -m "Add suggestion detail modal with actions"`

---

## TAREFA 3: Sugestoes de Mensagem no Chat

**Status**: PENDENTE
**Prioridade**: ALTA

### Adicionar sidebar de sugestoes em rap_inbox.html

```html
<!-- No detalhe da conversa, adicionar botao -->
<div class="message-composer">
    <button class="ai-suggest-btn" onclick="suggestMessage()" title="Sugerir mensagem com AI">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 0 1 7 7h1"/>
            <circle cx="8" cy="14" r="1"/><circle cx="16" cy="14" r="1"/>
        </svg>
    </button>
    <textarea id="message-input" placeholder="Digite sua mensagem..."></textarea>
    <button class="send-btn" onclick="sendMessage()">Enviar</button>
</div>

<!-- Popup de sugestao -->
<div id="message-suggestion-popup" class="message-suggestion-popup" style="display: none;">
    <div class="suggestion-popup-header">
        <span>Sugestao AI</span>
        <button onclick="closeSuggestionPopup()">&times;</button>
    </div>
    <div class="suggestion-popup-content" id="suggested-message">
        Carregando...
    </div>
    <div class="suggestion-popup-actions">
        <button onclick="useSuggestedMessage()">Usar</button>
        <button onclick="regenerateSuggestion()">Outra</button>
    </div>
</div>
```

```css
.ai-suggest-btn {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    border: none;
    padding: 8px 12px;
    border-radius: 8px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 4px;
}

.message-suggestion-popup {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    background: white;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    box-shadow: 0 -4px 20px rgba(0,0,0,0.1);
    margin-bottom: 8px;
    overflow: hidden;
}

.suggestion-popup-header {
    padding: 12px 16px;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-weight: 500;
}

.suggestion-popup-header button {
    background: none;
    border: none;
    color: white;
    font-size: 20px;
    cursor: pointer;
}

.suggestion-popup-content {
    padding: 16px;
    font-size: 14px;
    line-height: 1.5;
    min-height: 60px;
}

.suggestion-popup-actions {
    padding: 12px 16px;
    border-top: 1px solid #e5e7eb;
    display: flex;
    gap: 8px;
    justify-content: flex-end;
}

.suggestion-popup-actions button {
    padding: 6px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
}

.suggestion-popup-actions button:first-child {
    background: #10b981;
    color: white;
    border: none;
}

.suggestion-popup-actions button:last-child {
    background: white;
    border: 1px solid #e5e7eb;
}
```

```javascript
let currentContactId = null; // Set when selecting a conversation
let currentSuggestedMessage = '';

async function suggestMessage() {
    if (!currentContactId) {
        alert('Selecione uma conversa primeiro');
        return;
    }

    const popup = document.getElementById('message-suggestion-popup');
    const content = document.getElementById('suggested-message');

    popup.style.display = 'block';
    content.textContent = 'Gerando sugestao...';

    try {
        const response = await fetch(`/api/ai/message-suggest/${currentContactId}?tipo=reconnect`);
        const data = await response.json();

        if (data.message) {
            currentSuggestedMessage = data.message;
            content.textContent = data.message;
        } else {
            content.textContent = 'Nao foi possivel gerar sugestao';
        }
    } catch (error) {
        content.textContent = 'Erro ao gerar sugestao';
        console.error(error);
    }
}

function closeSuggestionPopup() {
    document.getElementById('message-suggestion-popup').style.display = 'none';
}

function useSuggestedMessage() {
    if (currentSuggestedMessage) {
        document.getElementById('message-input').value = currentSuggestedMessage;
        closeSuggestionPopup();
    }
}

async function regenerateSuggestion() {
    await suggestMessage();
}
```

**Commit**: `git commit -m "Add AI message suggestions in chat composer"`

---

## TAREFA 4: Pagina de Automacoes

**Status**: PENDENTE
**Prioridade**: MEDIA

### Criar arquivo: `app/templates/rap_automations.html`

```html
{% extends "rap_base.html" %}

{% block title %}Automacoes AI{% endblock %}

{% block content %}
<div class="page-header">
    <h1>Automacoes AI</h1>
    <p class="text-muted">Configure regras automaticas para gerenciar seus contatos</p>
</div>

<div class="automations-grid" id="automations-list">
    <!-- Loaded via JS -->
</div>

<style>
.automations-grid {
    display: grid;
    gap: 16px;
    max-width: 800px;
}

.automation-card {
    background: white;
    border-radius: 12px;
    padding: 20px;
    border: 1px solid #e5e7eb;
}

.automation-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
}

.automation-title {
    font-weight: 600;
    font-size: 16px;
}

.automation-toggle {
    position: relative;
    width: 48px;
    height: 24px;
}

.automation-toggle input {
    opacity: 0;
    width: 0;
    height: 0;
}

.toggle-slider {
    position: absolute;
    cursor: pointer;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: #e5e7eb;
    transition: .3s;
    border-radius: 24px;
}

.toggle-slider:before {
    position: absolute;
    content: "";
    height: 18px;
    width: 18px;
    left: 3px;
    bottom: 3px;
    background-color: white;
    transition: .3s;
    border-radius: 50%;
}

input:checked + .toggle-slider {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}

input:checked + .toggle-slider:before {
    transform: translateX(24px);
}

.automation-desc {
    color: #6b7280;
    font-size: 14px;
    margin-bottom: 12px;
}

.automation-stats {
    display: flex;
    gap: 16px;
    font-size: 13px;
    color: #9ca3af;
}

.automation-stat {
    display: flex;
    align-items: center;
    gap: 4px;
}
</style>

<script>
async function loadAutomations() {
    try {
        const response = await fetch('/api/ai/automations');
        const data = await response.json();
        renderAutomations(data.automations || []);
    } catch (error) {
        console.error('Erro ao carregar automacoes:', error);
    }
}

function renderAutomations(automations) {
    const container = document.getElementById('automations-list');

    if (!automations.length) {
        container.innerHTML = '<p class="text-muted">Nenhuma automacao configurada</p>';
        return;
    }

    container.innerHTML = automations.map(auto => `
        <div class="automation-card">
            <div class="automation-header">
                <span class="automation-title">${escapeHtml(auto.nome)}</span>
                <label class="automation-toggle">
                    <input type="checkbox" ${auto.enabled ? 'checked' : ''} onchange="toggleAutomation(${auto.id})">
                    <span class="toggle-slider"></span>
                </label>
            </div>
            <div class="automation-desc">${escapeHtml(auto.descricao || '')}</div>
            <div class="automation-stats">
                <span class="automation-stat">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                    </svg>
                    ${auto.run_count || 0} execucoes
                </span>
                ${auto.last_run ? `
                <span class="automation-stat">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>
                    </svg>
                    Ultima: ${new Date(auto.last_run).toLocaleDateString('pt-BR')}
                </span>
                ` : ''}
            </div>
        </div>
    `).join('');
}

async function toggleAutomation(id) {
    try {
        await fetch(`/api/ai/automations/${id}/toggle`, { method: 'POST' });
    } catch (error) {
        console.error('Erro ao alternar automacao:', error);
        loadAutomations(); // Reload to sync state
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

document.addEventListener('DOMContentLoaded', loadAutomations);
</script>
{% endblock %}
```

### Adicionar rota em main.py

```python
@app.get("/automations", response_class=HTMLResponse)
async def automations_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("rap_automations.html", {"request": request, "user": user})
```

**Commit**: `git commit -m "Add automations management page"`

---

## TAREFA 5: Widget Contatos em Risco

**Status**: PENDENTE
**Prioridade**: MEDIA

### Adicionar widget no dashboard ou sidebar

```html
<!-- Widget de Contatos em Risco -->
<div class="at-risk-widget" id="at-risk-widget">
    <div class="widget-header">
        <span class="widget-title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            Contatos em Risco
        </span>
        <a href="/contatos?filter=at-risk" class="widget-link">Ver todos</a>
    </div>
    <div class="at-risk-list" id="at-risk-list">
        <!-- Loaded via JS -->
    </div>
</div>
```

```css
.at-risk-widget {
    background: white;
    border-radius: 12px;
    border: 1px solid #fecaca;
    overflow: hidden;
}

.at-risk-widget .widget-header {
    padding: 12px 16px;
    background: #fef2f2;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #fecaca;
}

.at-risk-widget .widget-title {
    font-weight: 600;
    color: #dc2626;
    display: flex;
    align-items: center;
    gap: 8px;
}

.at-risk-widget .widget-link {
    font-size: 13px;
    color: #dc2626;
    text-decoration: none;
}

.at-risk-list {
    padding: 8px;
}

.at-risk-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px;
    border-radius: 8px;
    cursor: pointer;
}

.at-risk-item:hover {
    background: #fef2f2;
}

.at-risk-avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: #e5e7eb;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 600;
    font-size: 14px;
}

.at-risk-info {
    flex: 1;
    min-width: 0;
}

.at-risk-name {
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.at-risk-detail {
    font-size: 12px;
    color: #6b7280;
}

.at-risk-score {
    font-weight: 600;
    font-size: 13px;
    color: #dc2626;
}
```

```javascript
async function loadAtRiskContacts() {
    try {
        const response = await fetch('/api/ai/at-risk?limit=5');
        const data = await response.json();
        renderAtRiskContacts(data.contacts || []);
    } catch (error) {
        console.error('Erro ao carregar contatos em risco:', error);
    }
}

function renderAtRiskContacts(contacts) {
    const container = document.getElementById('at-risk-list');

    if (!contacts.length) {
        container.innerHTML = `
            <div style="padding: 20px; text-align: center; color: #10b981;">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin: 0 auto 8px;">
                    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                    <polyline points="22 4 12 14.01 9 11.01"/>
                </svg>
                <p style="margin: 0;">Nenhum contato em risco!</p>
            </div>
        `;
        return;
    }

    container.innerHTML = contacts.map(c => `
        <div class="at-risk-item" onclick="window.location.href='/contato/${c.contact_id}'">
            ${c.foto_url
                ? `<img src="${c.foto_url}" class="at-risk-avatar">`
                : `<div class="at-risk-avatar">${(c.nome || '?')[0]}</div>`
            }
            <div class="at-risk-info">
                <div class="at-risk-name">${escapeHtml(c.nome)}</div>
                <div class="at-risk-detail">
                    ${c.empresa ? escapeHtml(c.empresa) : `Circulo ${c.circulo}`}
                </div>
            </div>
            <div class="at-risk-score">${Math.round((c.risco_churn || 0) * 100)}%</div>
        </div>
    `).join('');
}

// Carregar ao iniciar
document.addEventListener('DOMContentLoaded', loadAtRiskContacts);
```

**Commit**: `git commit -m "Add at-risk contacts widget"`

---

## APOS COMPLETAR TODAS

```bash
git push origin main
```

Atualizar este arquivo marcando todas como **CONCLUIDAS**.

---

## Registro de Conclusao - Fila Anterior

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-27 | Pagina Inbox Unificado | CONCLUIDO |
| 2026-03-27 | Busca Global Cmd+K | CONCLUIDO |
| 2026-03-27 | Timeline Contato | CONCLUIDO |
| 2026-03-27 | Pagina Analytics | CONCLUIDO |
| 2026-03-27 | Mobile Responsive | CONCLUIDO |
