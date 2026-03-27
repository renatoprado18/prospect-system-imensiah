# Fila de Tarefas 3FLOW - Dashboard Improvements

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

## TAREFA 1: Morning Briefing (AI Summary)

**Status**: PENDENTE
**Prioridade**: ALTA

### Objetivo
Adicionar um card no topo da dashboard com um resumo AI do dia, mostrando o que o usuario deve focar.

### Modificar `app/templates/rap_dashboard.html`

#### 1.1 Adicionar CSS (dentro do style existente)

```css
/* Morning Briefing */
.morning-briefing {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    border-radius: 16px;
    padding: 24px 28px;
    margin-bottom: 24px;
    color: white;
    position: relative;
    overflow: hidden;
}

.morning-briefing::before {
    content: '';
    position: absolute;
    top: -50%;
    right: -20%;
    width: 300px;
    height: 300px;
    background: rgba(255,255,255,0.1);
    border-radius: 50%;
}

.morning-briefing-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
}

.morning-briefing-header i {
    font-size: 1.5rem;
    opacity: 0.9;
}

.morning-briefing-greeting {
    font-size: 1.25rem;
    font-weight: 600;
}

.morning-briefing-date {
    font-size: 0.85rem;
    opacity: 0.8;
}

.morning-briefing-summary {
    font-size: 1rem;
    line-height: 1.6;
    margin-bottom: 16px;
    max-width: 800px;
}

.morning-briefing-stats {
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
}

.briefing-stat {
    display: flex;
    align-items: center;
    gap: 8px;
    background: rgba(255,255,255,0.15);
    padding: 8px 14px;
    border-radius: 20px;
    font-size: 0.85rem;
    font-weight: 500;
}

.briefing-stat i {
    font-size: 1rem;
}

.morning-briefing-focus {
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid rgba(255,255,255,0.2);
}

.focus-label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    opacity: 0.8;
    margin-bottom: 6px;
}

.focus-contact {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(255,255,255,0.2);
    padding: 6px 12px;
    border-radius: 8px;
    cursor: pointer;
    transition: background 0.2s;
}

.focus-contact:hover {
    background: rgba(255,255,255,0.3);
}

@media (max-width: 768px) {
    .morning-briefing {
        padding: 20px;
    }
    .morning-briefing-stats {
        gap: 12px;
    }
    .briefing-stat {
        padding: 6px 12px;
        font-size: 0.8rem;
    }
}
```

#### 1.2 Adicionar HTML (logo apos page-header, antes de stats-grid)

```html
<!-- Morning Briefing -->
<div class="morning-briefing" id="morningBriefing">
    <div class="morning-briefing-header">
        <i class="bi bi-sun"></i>
        <div>
            <div class="morning-briefing-greeting" id="briefingGreeting">Bom dia, Renato!</div>
            <div class="morning-briefing-date" id="briefingDate"></div>
        </div>
    </div>
    <div class="morning-briefing-summary" id="briefingSummary">
        Carregando seu resumo do dia...
    </div>
    <div class="morning-briefing-stats" id="briefingStats">
        <!-- Preenchido via JS -->
    </div>
    <div class="morning-briefing-focus" id="briefingFocus" style="display: none;">
        <div class="focus-label">Foco sugerido</div>
        <div class="focus-contact" id="focusContact" onclick="goToFocusContact()">
            <i class="bi bi-person"></i>
            <span id="focusContactName">-</span>
            <span id="focusContactReason" style="opacity: 0.8; font-size: 0.8rem;"></span>
        </div>
    </div>
</div>
```

#### 1.3 Adicionar JavaScript (dentro do script existente)

```javascript
// ============== Morning Briefing ==============
async function loadMorningBriefing() {
    const greetingEl = document.getElementById('briefingGreeting');
    const dateEl = document.getElementById('briefingDate');
    const summaryEl = document.getElementById('briefingSummary');
    const statsEl = document.getElementById('briefingStats');
    const focusEl = document.getElementById('briefingFocus');

    // Saudacao baseada na hora
    const hour = new Date().getHours();
    let greeting = 'Bom dia';
    let icon = 'bi-sun';
    if (hour >= 12 && hour < 18) {
        greeting = 'Boa tarde';
        icon = 'bi-brightness-high';
    } else if (hour >= 18 || hour < 6) {
        greeting = 'Boa noite';
        icon = 'bi-moon-stars';
    }
    greetingEl.textContent = `${greeting}, Renato!`;
    document.querySelector('.morning-briefing-header i').className = `bi ${icon}`;

    // Data formatada
    const options = { weekday: 'long', day: 'numeric', month: 'long' };
    const dateStr = new Date().toLocaleDateString('pt-BR', options);
    dateEl.textContent = dateStr.charAt(0).toUpperCase() + dateStr.slice(1);

    try {
        // Buscar dados para o briefing
        const [dashResponse, atRiskResponse] = await Promise.all([
            fetch('/api/v1/dashboard'),
            fetch('/api/ai/at-risk?limit=1')
        ]);

        const dashData = await dashResponse.json();
        const atRiskData = await atRiskResponse.json().catch(() => ({ contacts: [] }));

        // Construir resumo
        const stats = dashData.stats || {};
        const aniversarios = dashData.aniversarios || [];
        const alertas = dashData.alertas || {};
        const atRiskContacts = atRiskData.contacts || [];

        // Contagem de eventos hoje
        let reunioesHoje = 0;
        try {
            const calResp = await fetch('/api/calendar/today');
            if (calResp.ok) {
                const calData = await calResp.json();
                reunioesHoje = Array.isArray(calData) ? calData.length : 0;
            }
        } catch (e) {}

        // Montar frase de resumo
        let summaryParts = [];

        if (reunioesHoje > 0) {
            summaryParts.push(`${reunioesHoje} reuniao${reunioesHoje > 1 ? 'oes' : ''}`);
        }

        const aniversariosHoje = aniversarios.filter(a => a.dias_ate_aniversario === 0);
        if (aniversariosHoje.length > 0) {
            const nomes = aniversariosHoje.slice(0, 2).map(a => a.nome.split(' ')[0]).join(' e ');
            summaryParts.push(`aniversario de ${nomes}`);
        }

        const precisamAtencao = stats.precisam_atencao || alertas.total || 0;
        if (precisamAtencao > 0) {
            summaryParts.push(`${precisamAtencao} contato${precisamAtencao > 1 ? 's' : ''} precisando atencao`);
        }

        let summaryText = '';
        if (summaryParts.length === 0) {
            summaryText = 'Dia tranquilo! Todos os seus contatos estao em dia. Que tal aproveitar para fortalecer relacionamentos?';
        } else {
            summaryText = `Hoje voce tem: ${summaryParts.join(', ')}. `;
            if (atRiskContacts.length > 0) {
                summaryText += `Priorize ${atRiskContacts[0].nome.split(' ')[0]} que esta com health baixo.`;
            }
        }
        summaryEl.textContent = summaryText;

        // Stats badges
        let statsHtml = '';
        if (reunioesHoje > 0) {
            statsHtml += `<div class="briefing-stat"><i class="bi bi-calendar-event"></i> ${reunioesHoje} reuniao${reunioesHoje > 1 ? 'oes' : ''}</div>`;
        }
        if (aniversariosHoje.length > 0) {
            statsHtml += `<div class="briefing-stat"><i class="bi bi-cake2"></i> ${aniversariosHoje.length} aniversario${aniversariosHoje.length > 1 ? 's' : ''}</div>`;
        }
        if (precisamAtencao > 0) {
            statsHtml += `<div class="briefing-stat"><i class="bi bi-exclamation-circle"></i> ${precisamAtencao} precisam atencao</div>`;
        }

        // Health medio
        const circulosResumo = dashData.circulos_resumo || {};
        let totalHealth = 0;
        let countHealth = 0;
        for (let i = 1; i <= 4; i++) {
            if (circulosResumo[i]?.health_medio) {
                totalHealth += circulosResumo[i].health_medio * (circulosResumo[i].total || 1);
                countHealth += circulosResumo[i].total || 0;
            }
        }
        const avgHealth = countHealth > 0 ? Math.round(totalHealth / countHealth) : null;
        if (avgHealth) {
            const healthColor = avgHealth >= 70 ? '#10b981' : (avgHealth >= 40 ? '#f59e0b' : '#ef4444');
            statsHtml += `<div class="briefing-stat" style="background: ${healthColor}33;"><i class="bi bi-heart-pulse"></i> Health ${avgHealth}%</div>`;
        }

        statsEl.innerHTML = statsHtml || '<div class="briefing-stat"><i class="bi bi-check-circle"></i> Tudo em dia!</div>';

        // Foco sugerido
        if (atRiskContacts.length > 0) {
            const focusC = atRiskContacts[0];
            document.getElementById('focusContactName').textContent = focusC.nome;
            document.getElementById('focusContactReason').textContent = `(Health ${focusC.health_score || Math.round((1 - focusC.risco_churn) * 100)}%)`;
            window.focusContactId = focusC.id;
            focusEl.style.display = 'block';
        }

    } catch (error) {
        console.error('Error loading morning briefing:', error);
        summaryEl.textContent = 'Pronto para mais um dia produtivo? Confira suas prioridades abaixo.';
        statsEl.innerHTML = '';
    }
}

function goToFocusContact() {
    if (window.focusContactId) {
        window.location.href = `/contatos/${window.focusContactId}`;
    }
}

// Chamar no init
loadMorningBriefing();
```

**Commit**: `git commit -m "feat(dashboard): Add Morning Briefing AI summary widget"`

---

## TAREFA 2: Quem Contatar Hoje

**Status**: PENDENTE
**Prioridade**: ALTA

### Objetivo
Substituir ou complementar o widget "Contatos em Risco" com uma lista priorizada de quem contatar hoje, incluindo:
- Aniversariantes
- Follow-ups de reunioes
- Health critico
- Tempo sem contato

### Modificar `app/templates/rap_dashboard.html`

#### 2.1 Adicionar CSS

```css
/* Quem Contatar Hoje */
.contact-today-card {
    border: 2px solid var(--primary);
    border-radius: 16px;
    overflow: hidden;
}

.contact-today-header {
    background: linear-gradient(135deg, var(--primary), var(--secondary));
    color: white;
    padding: 16px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.contact-today-header h2 {
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
}

.contact-today-count {
    background: rgba(255,255,255,0.2);
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 0.8rem;
    font-weight: 600;
}

.contact-today-list {
    padding: 16px;
}

.contact-today-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px;
    border-radius: 12px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: all 0.2s;
    background: var(--gray-100);
    text-decoration: none;
    color: inherit;
}

.contact-today-item:hover {
    background: var(--gray-200);
    transform: translateX(4px);
}

.contact-today-item:last-child {
    margin-bottom: 0;
}

.contact-today-avatar {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 600;
    font-size: 0.85rem;
    flex-shrink: 0;
}

.contact-today-avatar.birthday {
    background: linear-gradient(135deg, #f472b6, #ec4899);
    color: white;
}

.contact-today-avatar.followup {
    background: linear-gradient(135deg, #60a5fa, #3b82f6);
    color: white;
}

.contact-today-avatar.critical {
    background: linear-gradient(135deg, #f87171, #ef4444);
    color: white;
}

.contact-today-avatar.attention {
    background: linear-gradient(135deg, #fbbf24, #f59e0b);
    color: white;
}

.contact-today-info {
    flex: 1;
    min-width: 0;
}

.contact-today-name {
    font-weight: 600;
    font-size: 0.9rem;
    color: var(--dark);
    margin-bottom: 2px;
}

.contact-today-reason {
    font-size: 0.8rem;
    color: var(--gray-500);
    display: flex;
    align-items: center;
    gap: 4px;
}

.contact-today-reason i {
    font-size: 0.75rem;
}

.contact-today-badge {
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
}

.contact-today-badge.birthday {
    background: rgba(236, 72, 153, 0.1);
    color: #ec4899;
}

.contact-today-badge.followup {
    background: rgba(59, 130, 246, 0.1);
    color: #3b82f6;
}

.contact-today-badge.critical {
    background: rgba(239, 68, 68, 0.1);
    color: #ef4444;
}

.contact-today-badge.attention {
    background: rgba(245, 158, 11, 0.1);
    color: #f59e0b;
}

.contact-today-empty {
    text-align: center;
    padding: 32px 16px;
    color: var(--gray-500);
}

.contact-today-empty i {
    font-size: 2.5rem;
    color: var(--success);
    margin-bottom: 12px;
}
```

#### 2.2 Substituir widget "Contatos em Risco" por "Quem Contatar Hoje"

Localizar o card com id="atRiskContacts" e substituir por:

```html
<!-- Quem Contatar Hoje -->
<div class="card contact-today-card mb-4">
    <div class="contact-today-header">
        <h2><i class="bi bi-person-check"></i> Quem Contatar Hoje</h2>
        <span class="contact-today-count" id="contactTodayCount">0</span>
    </div>
    <div class="contact-today-list" id="contactTodayList">
        <div class="text-center py-3">
            <div class="spinner-border spinner-border-sm text-primary"></div>
            <span class="ms-2 text-muted">Analisando prioridades...</span>
        </div>
    </div>
</div>
```

#### 2.3 Adicionar JavaScript

```javascript
// ============== Quem Contatar Hoje ==============
async function loadContactToday() {
    const container = document.getElementById('contactTodayList');
    const countBadge = document.getElementById('contactTodayCount');

    try {
        // Buscar dados de multiplas fontes
        const [dashResponse, atRiskResponse, calendarResponse] = await Promise.all([
            fetch('/api/v1/dashboard'),
            fetch('/api/ai/at-risk?limit=10'),
            fetch('/api/calendar/today').catch(() => ({ json: () => [] }))
        ]);

        const dashData = await dashResponse.json();
        const atRiskData = await atRiskResponse.json().catch(() => ({ contacts: [] }));
        const calendarData = await calendarResponse.json().catch(() => []);

        const priorityContacts = [];

        // 1. Aniversariantes de hoje (prioridade maxima)
        const aniversarios = dashData.aniversarios || [];
        const aniversariosHoje = aniversarios.filter(a => a.dias_ate_aniversario === 0);
        aniversariosHoje.forEach(a => {
            priorityContacts.push({
                id: a.id,
                nome: a.nome,
                reason: 'Aniversario hoje!',
                reasonIcon: 'bi-cake2',
                type: 'birthday',
                priority: 1
            });
        });

        // 2. Follow-ups de reunioes de ontem/hoje
        if (Array.isArray(calendarData)) {
            calendarData.forEach(event => {
                if (event.contact_id && event.contact_name) {
                    // Verificar se nao esta duplicado
                    if (!priorityContacts.find(p => p.id === event.contact_id)) {
                        priorityContacts.push({
                            id: event.contact_id,
                            nome: event.contact_name,
                            reason: 'Reuniao hoje - fazer follow-up',
                            reasonIcon: 'bi-camera-video',
                            type: 'followup',
                            priority: 2
                        });
                    }
                }
            });
        }

        // 3. Contatos em risco critico (health < 20)
        const atRiskContacts = atRiskData.contacts || [];
        atRiskContacts.forEach(c => {
            if (!priorityContacts.find(p => p.id === c.id)) {
                const health = c.health_score || Math.round((1 - (c.risco_churn || 0.5)) * 100);
                const isCritical = health < 20;

                priorityContacts.push({
                    id: c.id,
                    nome: c.nome,
                    reason: `Health ${health}% - ${c.motivo_risco || 'precisa atencao'}`,
                    reasonIcon: 'bi-heart-pulse',
                    type: isCritical ? 'critical' : 'attention',
                    priority: isCritical ? 3 : 4,
                    health: health
                });
            }
        });

        // 4. Contatos com muito tempo sem interacao (dos circulos 1-2)
        // Ja incluidos no at-risk normalmente

        // Ordenar por prioridade
        priorityContacts.sort((a, b) => a.priority - b.priority);

        // Limitar a 6 contatos
        const displayContacts = priorityContacts.slice(0, 6);

        countBadge.textContent = displayContacts.length;

        if (displayContacts.length === 0) {
            container.innerHTML = `
                <div class="contact-today-empty">
                    <i class="bi bi-check-circle"></i>
                    <p class="mb-0">Parabens! Voce esta em dia com todos os contatos.</p>
                </div>
            `;
            return;
        }

        container.innerHTML = displayContacts.map(c => {
            const initials = (c.nome || 'XX').split(' ').map(n => n[0]).slice(0, 2).join('').toUpperCase();

            return `
                <a href="/contatos/${c.id}" class="contact-today-item">
                    <div class="contact-today-avatar ${c.type}">${initials}</div>
                    <div class="contact-today-info">
                        <div class="contact-today-name">${c.nome}</div>
                        <div class="contact-today-reason">
                            <i class="bi ${c.reasonIcon}"></i>
                            ${c.reason}
                        </div>
                    </div>
                    <span class="contact-today-badge ${c.type}">
                        ${c.type === 'birthday' ? 'Aniversario' :
                          c.type === 'followup' ? 'Follow-up' :
                          c.type === 'critical' ? 'Critico' : 'Atencao'}
                    </span>
                </a>
            `;
        }).join('');

    } catch (error) {
        console.error('Error loading contact today:', error);
        container.innerHTML = `
            <div class="contact-today-empty">
                <i class="bi bi-info-circle"></i>
                <p class="mb-0">Nao foi possivel carregar as prioridades</p>
            </div>
        `;
    }
}

// Chamar no init (substituir loadAtRiskContacts)
loadContactToday();
```

#### 2.4 Remover chamada antiga

No bloco setTimeout que chama `loadAtRiskContacts()`, substituir por `loadContactToday()`:

```javascript
// Load AI features on page load
setTimeout(() => {
    loadAISuggestions();
    loadContactToday();  // Era loadAtRiskContacts()
    loadMeetingSuggestions();
}, 500);
```

**Commit**: `git commit -m "feat(dashboard): Add 'Quem Contatar Hoje' prioritized contact list"`

---

## TAREFA 3: Ajustes Finais e Responsividade

**Status**: PENDENTE
**Prioridade**: MEDIA

### 3.1 Reorganizar layout da coluna direita

Ordem recomendada dos widgets:
1. Quem Contatar Hoje (novo)
2. Sugestoes IA
3. Agendar Reuniao
4. Lembretes
5. Contatos Recentes

### 3.2 Verificar responsividade mobile

Testar e ajustar se necessario.

### 3.3 Remover widget duplicado "Contatos em Risco"

Ja substituido por "Quem Contatar Hoje".

**Commit**: `git commit -m "style(dashboard): Reorganize widgets and improve responsiveness"`

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
| - | Morning Briefing AI Summary | PENDENTE |
| - | Quem Contatar Hoje | PENDENTE |
| - | Ajustes Finais | PENDENTE |
