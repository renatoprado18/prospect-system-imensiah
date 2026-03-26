# Fila de Tarefas 3FLOW

**Atualizacao**: 2026-03-26
**Modo**: AUTONOMO TOTAL - executar todas as tarefas sem aguardar aprovacao

---

## TAREFAS CONCLUIDAS

| Tarefa | Commit | Resumo |
|--------|--------|--------|
| Branding INTEL | 78dd717 | Dashboard com cards Circulos, Alertas, Briefings |
| Configuracoes | b48f774 | Circulos frequency settings |
| Birthday Notif | 00d12e6 | Birthday banner + reminders |
| Pagina Contato | merged | Circulo badge, Health card |
| Sidebar Prospeccao | d55dd7b | Link /prospeccao no sidebar FERRAMENTAS |
| Pagina Duplicados | edab6ef | Pagina /duplicados com merge |
| Card Estatisticas | edab6ef | Distribuicao por circulo no dashboard |
| UI Agenda | 9130477 | Conecta /api/calendar/today |
| UI Tarefas | 9130477 | Card tarefas com checkbox |
| Badge Inbox | 9130477 | Contador nao lidos polling 60s |
| Busca Global | 9130477 | Cmd+K, debounce, dropdown |

---

## NOVAS TAREFAS (Executar em ordem)

### Tarefa 1: Pagina Inbox Completa

**Status**: PENDENTE
**Prioridade**: CRITICA

**Objetivo**: Finalizar pagina /inbox com todas as funcionalidades.

**Arquivo**: `app/templates/intel_inbox.html`

**Implementacao**:
```html
<!-- Layout master-detail -->
<div class="inbox-container">
    <!-- Sidebar com lista de conversas -->
    <div class="inbox-sidebar">
        <div class="inbox-filters">
            <button class="filter-btn active" data-filter="all">Todas</button>
            <button class="filter-btn" data-filter="email">Email</button>
            <button class="filter-btn" data-filter="whatsapp">WhatsApp</button>
            <button class="filter-btn" data-filter="unread">Nao lidas</button>
        </div>
        <div class="conversation-list" id="conversationList">
            <!-- Carrega via JS -->
        </div>
    </div>

    <!-- Area principal com mensagens -->
    <div class="inbox-main">
        <div class="conversation-header" id="convHeader">
            <!-- Nome, avatar, acoes -->
        </div>
        <div class="messages-container" id="messagesContainer">
            <!-- Mensagens -->
        </div>
        <div class="reply-box" id="replyBox">
            <textarea placeholder="Digite sua resposta..."></textarea>
            <button class="btn btn-primary">Enviar</button>
        </div>
    </div>
</div>
```

**APIs utilizadas** (2INTEL cria):
- `GET /api/inbox/conversations`
- `GET /api/inbox/conversations/{id}/messages`
- `POST /api/inbox/conversations/{id}/read`

**Criterios**:
- [ ] Lista de conversas com filtros
- [ ] Visualizar mensagens
- [ ] Marcar como lida ao abrir
- [ ] Campo de resposta (mesmo que nao envie ainda)

---

### Tarefa 2: Melhorar Pagina de Contato

**Status**: PENDENTE
**Prioridade**: ALTA

**Objetivo**: Adicionar timeline e melhorar layout.

**Arquivo**: `app/templates/rap_contact_detail.html`

**Adicionar**:
1. **Timeline de interacoes** (nova secao)
```html
<div class="card mt-4">
    <div class="card-header">
        <h5><i class="bi bi-clock-history"></i> Historico</h5>
    </div>
    <div class="card-body">
        <div class="timeline" id="contactTimeline">
            <!-- Carrega via /api/contacts/{id}/timeline -->
        </div>
    </div>
</div>
```

2. **Acoes rapidas no header**
```html
<div class="quick-actions">
    <button onclick="sendWhatsApp()"><i class="bi bi-whatsapp"></i></button>
    <button onclick="sendEmail()"><i class="bi bi-envelope"></i></button>
    <button onclick="scheduleCall()"><i class="bi bi-calendar-plus"></i></button>
    <button onclick="addNote()"><i class="bi bi-sticky"></i></button>
</div>
```

3. **Tags editaveis**
```html
<div class="tags-section">
    <span class="tag" data-tag="c-level">c-level <i class="bi bi-x"></i></span>
    <button class="add-tag-btn">+ Tag</button>
</div>
```

**Criterios**:
- [ ] Timeline funciona
- [ ] Acoes rapidas no header
- [ ] Tags editaveis
- [ ] Layout mais limpo

---

### Tarefa 3: Mobile Responsivo

**Status**: PENDENTE
**Prioridade**: ALTA

**Objetivo**: Todas as paginas funcionam bem no celular.

**Arquivos**: Todos os templates

**Verificar/Corrigir**:
1. Dashboard - cards empilham verticalmente
2. Sidebar - vira menu hamburger
3. Inbox - lista ocupa tela toda, detalhe em nova tela
4. Contatos - tabela vira cards
5. Busca global - funciona com touch

**CSS a adicionar**:
```css
@media (max-width: 768px) {
    .sidebar {
        position: fixed;
        left: -260px;
        transition: left 0.3s;
    }
    .sidebar.open { left: 0; }
    .main-content { margin-left: 0; }
    .stats-grid { grid-template-columns: 1fr; }
    /* etc */
}
```

**Criterios**:
- [ ] Dashboard responsivo
- [ ] Sidebar mobile com hamburger
- [ ] Inbox mobile-first
- [ ] Contatos em cards no mobile

---

### Tarefa 4: PWA - Progressive Web App

**Status**: PENDENTE
**Prioridade**: MEDIA

**Objetivo**: App instalavel no celular.

**Criar**:
1. `static/manifest.json`
```json
{
    "name": "INTEL - Assistente Pessoal",
    "short_name": "INTEL",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#1e293b",
    "theme_color": "#6366f1",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
    ]
}
```

2. `static/service-worker.js`
```javascript
// Cache de assets estaticos
// Offline fallback basico
```

3. **Adicionar no head** de todos os templates:
```html
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#6366f1">
<meta name="apple-mobile-web-app-capable" content="yes">
```

**Criterios**:
- [ ] manifest.json criado
- [ ] Service worker basico
- [ ] App instalavel no Android/iOS
- [ ] Icones criados

---

### Tarefa 5: Pagina de Analytics/Relatorios

**Status**: PENDENTE
**Prioridade**: BAIXA

**Objetivo**: Dashboard com metricas e graficos.

**Criar**: `app/templates/intel_analytics.html`

**Secoes**:
1. **Resumo do periodo**
   - Total de interacoes (email + whatsapp)
   - Novos contatos
   - Tarefas concluidas

2. **Graficos**
   - Interacoes por dia (linha)
   - Distribuicao por circulo (pizza)
   - Top 10 contatos mais ativos

3. **Health Score**
   - Contatos melhorando vs piorando
   - Alertas de relacionamentos esfriando

**Rota**: `GET /analytics`

**Criterios**:
- [ ] Pagina criada
- [ ] Pelo menos 3 graficos (usar Chart.js)
- [ ] Periodo selecionavel (7d, 30d, 90d)

---

## INSTRUCOES DE EXECUCAO

1. **Branch**: `git checkout -b feature/flow-ui-v3`
2. **Executar em ordem** (1 -> 5)
3. **Commit por tarefa**
4. **Merge direto em main**

## AUTONOMIA

- NAO aguardar aprovacao
- Se API nao existir, criar mock/fallback
- Atualizar este arquivo conforme progresso

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-26 | UI Agenda/Tarefas/Badge/Busca | **CONCLUIDO** |
| 2026-03-26 | Duplicados + Stats Card | **CONCLUIDO** |
| 2026-03-26 | Branding + Birthday | **CONCLUIDO** |
| 2026-03-25 | Settings + Contact Page | **MERGED** |
