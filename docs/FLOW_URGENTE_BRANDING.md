# TAREFA URGENTE: Atualizar Branding para INTEL

**Status**: URGENTE - Executar imediatamente
**Branch**: `feature/flow-intel-branding`
**Prioridade**: CRITICA

## Problema

A pagina em intel.almeida-prado.com ainda mostra:
- Header: "Prospects ImensIAH" (errado)
- Sidebar: Menu antigo (Prospects, Reunioes, Analytics, ICP)
- Dashboard: Cards de vendas (Tier A, Convertidos, Receita, Funil)

## O Que Deve Mostrar

- Header: "INTEL" ou "INTEL | Assistente Pessoal"
- Sidebar novo:
  - Dashboard (/)
  - Contatos (/contacts)
  - Circulos (/circulos) - com badge "X precisam atencao"
  - Briefings (/briefings)
  - Configuracoes (/config)
- Dashboard novo:
  - Card de Circulos (resumo dos 5 circulos)
  - Card de Alertas (aniversarios, health baixo)
  - Card de Briefings pendentes
  - Contatos recentes

## Arquivos para Modificar

### 1. `app/templates/rap_dashboard.html`

Localizar e substituir:

```html
<!-- ANTES (linha ~150-160) -->
<span class="sidebar-logo">
    <span class="icon"><i class="bi bi-bullseye"></i></span>
    Prospects ImensIAH
</span>

<!-- DEPOIS -->
<span class="sidebar-logo">
    <span class="icon"><i class="bi bi-cpu"></i></span>
    INTEL
</span>
```

### 2. Atualizar Sidebar Navigation

Substituir os nav-items antigos por:

```html
<nav class="sidebar-nav">
    <a href="/" class="nav-item active">
        <i class="bi bi-grid-1x2"></i>
        <span>Dashboard</span>
    </a>
    <a href="/contacts" class="nav-item">
        <i class="bi bi-people"></i>
        <span>Contatos</span>
        <span class="badge bg-secondary">6699</span>
    </a>

    <div class="nav-section">Relacionamentos</div>

    <a href="/circulos" class="nav-item">
        <i class="bi bi-heart"></i>
        <span>Circulos</span>
        <span class="badge bg-warning text-dark" id="badge-atencao"></span>
    </a>
    <a href="/briefings" class="nav-item">
        <i class="bi bi-file-text"></i>
        <span>Briefings</span>
    </a>

    <div class="nav-section">Sistema</div>

    <a href="/config" class="nav-item">
        <i class="bi bi-gear"></i>
        <span>Configuracoes</span>
    </a>
</nav>
```

### 3. Atualizar Header (topbar)

```html
<!-- ANTES -->
<span class="navbar-brand">Prospects ImensIAH</span>

<!-- DEPOIS -->
<span class="navbar-brand">
    <i class="bi bi-cpu me-2"></i>INTEL
</span>
```

### 4. Atualizar Dashboard Cards

Remover cards antigos (Tier A, Reunioes, Convertidos, Receita, Funil).

Adicionar novos cards:

```html
<!-- Card Circulos -->
<div class="col-md-6 col-lg-3">
    <div class="card h-100">
        <div class="card-body">
            <h6 class="text-muted mb-2">Circulos</h6>
            <div class="d-flex align-items-center gap-2">
                <span class="badge" style="background: #FF6B6B">C1: 5</span>
                <span class="badge" style="background: #4ECDC4">C2: 12</span>
                <span class="badge" style="background: #45B7D1">C3: 48</span>
            </div>
            <a href="/circulos" class="stretched-link"></a>
        </div>
    </div>
</div>

<!-- Card Alertas -->
<div class="col-md-6 col-lg-3">
    <div class="card h-100 border-warning">
        <div class="card-body">
            <h6 class="text-muted mb-2">Precisam Atencao</h6>
            <h3 class="mb-0" id="count-atencao">--</h3>
            <small class="text-muted">contatos com health baixo</small>
        </div>
    </div>
</div>

<!-- Card Aniversarios -->
<div class="col-md-6 col-lg-3">
    <div class="card h-100">
        <div class="card-body">
            <h6 class="text-muted mb-2">Aniversarios</h6>
            <h3 class="mb-0" id="count-aniversarios">--</h3>
            <small class="text-muted">proximos 7 dias</small>
        </div>
    </div>
</div>

<!-- Card Briefings -->
<div class="col-md-6 col-lg-3">
    <div class="card h-100">
        <div class="card-body">
            <h6 class="text-muted mb-2">Briefings Pendentes</h6>
            <h3 class="mb-0" id="count-briefings">--</h3>
            <a href="/briefings" class="stretched-link"></a>
        </div>
    </div>
</div>
```

### 5. Adicionar JavaScript para carregar dados

```javascript
// Ao final do arquivo, adicionar:
async function loadDashboardData() {
    try {
        const resp = await fetch('/api/v1/dashboard');
        const data = await resp.json();

        // Atualizar badges
        document.getElementById('count-atencao').textContent = data.alertas?.total || 0;
        document.getElementById('count-aniversarios').textContent = data.alertas?.aniversarios?.length || 0;
        document.getElementById('count-briefings').textContent = data.briefings_pendentes || 0;

        // Badge sidebar
        const badge = document.getElementById('badge-atencao');
        if (badge && data.alertas?.total > 0) {
            badge.textContent = data.alertas.total;
        }
    } catch (e) {
        console.error('Erro ao carregar dashboard:', e);
    }
}

document.addEventListener('DOMContentLoaded', loadDashboardData);
```

## Criterio de Aceite

- [ ] Header mostra "INTEL" em vez de "Prospects ImensIAH"
- [ ] Sidebar tem: Dashboard, Contatos, Circulos, Briefings, Configuracoes
- [ ] Cards do dashboard mostram: Circulos, Alertas, Aniversarios, Briefings
- [ ] Dados carregam de /api/v1/dashboard
- [ ] Remover completamente referencias a "Prospects", "Tier", "Funil", "Reunioes"

## Comando para Testar

```bash
# Iniciar servidor local
cd /Users/rap/prospect-system
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000

# Acessar: http://localhost:8000/
```

## Depois de Concluir

```bash
git add .
git commit -m "feat(ui): Update branding from Prospects to INTEL"
git push origin feature/flow-intel-branding
```

Atualizar este arquivo com status PRONTO.
