# Tarefa 3FLOW: Reestruturação UI para intel.almeida-prado.com

**Data**: 2026-03-25
**Branch**: `feature/intel-ui`
**Prioridade**: Alta

## Contexto

O sistema está migrando de `prospects.almeida-prado.com/rap` para `intel.almeida-prado.com`.
Precisamos reestruturar as rotas e atualizar a UI.

## Objetivos

1. Migrar rotas de `/rap/*` para `/*`
2. Adicionar Círculos e Briefings ao sidebar
3. Atualizar branding de "RAP" para "INTEL"
4. Integrar novos cards no Dashboard

## Mudanças de Rotas

| Rota Atual | Nova Rota |
|------------|-----------|
| `/rap` | `/` |
| `/rap/circulos` | `/circulos` |
| `/rap/briefings` | `/briefings` |
| `/rap/contatos` | `/contatos` |
| `/rap/inbox` | `/inbox` |
| `/rap/calendario` | `/calendario` |
| `/rap/tarefas` | `/tarefas` |

## Arquivos a Modificar

### 1. `app/main.py` - Rotas

Duplicar rotas existentes com novos paths (manter antigos para retrocompatibilidade):

```python
# Novas rotas (sem /rap)
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("rap_dashboard.html", {...})

@app.get("/circulos", response_class=HTMLResponse)
async def circulos_page(request: Request):
    return templates.TemplateResponse("rap_circulos.html", {...})

@app.get("/briefings", response_class=HTMLResponse)
async def briefings_page(request: Request):
    return templates.TemplateResponse("rap_briefings.html", {...})

# API v1
@app.get("/api/v1/dashboard")
async def dashboard_api():
    from services.dashboard import get_dashboard_stats, get_alertas, get_contatos_recentes
    return {
        "stats": get_dashboard_stats(),
        "alertas": get_alertas(),
        "contatos_recentes": get_contatos_recentes()
    }
```

### 2. `app/templates/components/sidebar.html` (ou equivalente)

Atualizar sidebar:

```html
<!-- Logo -->
<div class="logo">
    <span class="text-xl font-bold">INTEL</span>
</div>

<!-- Menu Principal -->
<nav>
    <a href="/">Dashboard</a>
    <a href="/circulos" class="new-feature">
        Círculos
        <span class="badge badge-new">Novo</span>
    </a>
    <a href="/contatos">
        Contatos
        <span class="badge">6,699</span>
    </a>
    <a href="/briefings" class="new-feature">
        Briefings
        <span class="badge badge-new">Novo</span>
    </a>
    <a href="/inbox">Inbox</a>
    <a href="/calendario">Calendário</a>
    <a href="/tarefas">Tarefas</a>
</nav>

<!-- Ferramentas -->
<nav class="section">
    <span class="section-title">FERRAMENTAS</span>
    <a href="/limpeza">Limpeza</a>
    <a href="/linkedin">LinkedIn</a>
    <a href="https://conselhoos.vercel.app" target="_blank">ConselhoOS ↗</a>
</nav>
```

### 3. `app/templates/rap_dashboard.html`

Adicionar novos cards:

```html
<!-- Stats Cards - Linha 1 -->
<div class="grid grid-cols-4 gap-4">
    <div class="stat-card">
        <div class="stat-value">{{ stats.total_contatos }}</div>
        <div class="stat-label">Contatos</div>
    </div>
    <div class="stat-card highlight-warning">
        <div class="stat-value">{{ stats.precisam_atencao }}</div>
        <div class="stat-label">Precisam Atenção</div>
    </div>
    <div class="stat-card">
        <div class="stat-value">{{ stats.reunioes_hoje }}</div>
        <div class="stat-label">Reuniões Hoje</div>
    </div>
    <div class="stat-card">
        <div class="stat-value">{{ stats.tarefas_pendentes }}</div>
        <div class="stat-label">Tarefas Pendentes</div>
    </div>
</div>

<!-- Círculos Mini-Dashboard -->
<div class="circulos-preview">
    <h3>Saúde dos Relacionamentos</h3>
    <div class="circulos-bars">
        {% for circulo, data in circulos_resumo.items() %}
        <div class="circulo-bar circulo-{{ circulo }}">
            <span>C{{ circulo }}</span>
            <div class="bar" style="width: {{ data.health_medio }}%"></div>
            <span>{{ data.total }}</span>
        </div>
        {% endfor %}
    </div>
    <a href="/circulos">Ver detalhes →</a>
</div>
```

### 4. Atualizar Links Internos

Em todos os templates, atualizar links:
- `href="/rap"` → `href="/"`
- `href="/rap/circulos"` → `href="/circulos"`
- `href="/rap/contatos"` → `href="/contatos"`
- etc.

## Cores dos Círculos (Referência)

```css
.circulo-1 { background: #FF6B6B; } /* Íntimo - Vermelho */
.circulo-2 { background: #4ECDC4; } /* Próximo - Turquesa */
.circulo-3 { background: #45B7D1; } /* Ativo - Azul */
.circulo-4 { background: #96CEB4; } /* Conhecido - Verde */
.circulo-5 { background: #DDA0DD; } /* Arquivo - Lilás */
```

## Remover/Ocultar

- Seção "Prospects" do sidebar (sistema não é mais B2B)
- Referências a "RAP" substituir por "INTEL"

## Checklist

- [ ] Adicionar novas rotas em main.py
- [ ] Atualizar sidebar com Círculos e Briefings
- [ ] Mudar branding "RAP" → "INTEL"
- [ ] Adicionar cards de Círculos no Dashboard
- [ ] Atualizar todos os links internos
- [ ] Testar navegação
- [ ] Atualizar COORDINATION.md
- [ ] Push para branch

## Dependência

- Aguardar INTEL concluir `dashboard.py` para integração completa
- Pode usar mocks enquanto espera

## Após Conclusão

```bash
git checkout -b feature/intel-ui
# ... implementar ...
git add -A
git commit -m "Restructure UI for intel.almeida-prado.com"
git push origin feature/intel-ui
```

Atualizar COORDINATION.md com "PRONTO PARA REVIEW".
