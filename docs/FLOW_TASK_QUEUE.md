# Fila de Tarefas 3FLOW

**Atualizacao**: 2026-03-26
**Modo**: Autonomo - sinalizar conclusao e continuar

---

## TAREFAS CONCLUIDAS

| Tarefa | Commit | Resumo |
|--------|--------|--------|
| Branding INTEL | 78dd717 | Dashboard com cards Circulos, Alertas, Briefings |
| Configuracoes | b48f774 | Circulos frequency settings |
| Birthday Notif | 00d12e6 | Birthday banner + reminders |
| Pagina Contato | merged | Circulo badge, Health card |
| Sidebar Prospeccao | pendente | Link /prospeccao no sidebar FERRAMENTAS |

---

## NOVAS TAREFAS

### Tarefa 1: Adicionar Prospeccao ao Sidebar

**Status**: CONCLUIDO
**Prioridade**: ALTA

**Objetivo**: A rota /prospeccao foi criada mas nao aparece no menu. Adicionar link no sidebar do INTEL.

**Arquivo**: `app/templates/rap_dashboard.html`

**Implementacao**:
```html
<!-- Adicionar no sidebar, secao FERRAMENTAS -->
<div class="nav-section">FERRAMENTAS</div>

<a href="/prospeccao" class="nav-item">
    <i class="bi bi-funnel"></i>
    <span>Prospeccao</span>
</a>
```

**Criterios**:
- [x] Link Prospeccao aparece no sidebar
- [x] Icone de funil
- [x] Redireciona para /prospeccao

---

### Tarefa 2: Pagina de Duplicados

**Status**: PENDENTE
**Prioridade**: MEDIA

**Objetivo**: Criar interface para visualizar e fazer merge de duplicados.

**Arquivo**: `app/templates/intel_duplicados.html` (novo)

**Implementacao**:
- Lista de pares duplicados com score
- Botao "Merge" para cada par
- Preview dos dados de ambos contatos
- Confirmar qual manter

**Endpoint**: `GET /api/contacts/duplicates`

**Criterios**:
- [ ] Pagina /duplicados criada
- [ ] Lista duplicados da API
- [ ] Permite merge com confirmacao

---

### Tarefa 3: Card de Estatisticas no Dashboard

**Status**: PENDENTE
**Prioridade**: MEDIA

**Objetivo**: Adicionar card mostrando distribuicao dos circulos visualmente.

**Implementacao**:
```html
<!-- Card com barras de progresso por circulo -->
<div class="card">
    <h6>Distribuicao por Circulo</h6>
    <div class="circulo-bar c1" style="width: 0.1%">C1: 5</div>
    <div class="circulo-bar c2" style="width: 0.1%">C2: 6</div>
    <div class="circulo-bar c3" style="width: 0.7%">C3: 44</div>
    <div class="circulo-bar c4" style="width: 5.6%">C4: 378</div>
    <div class="circulo-bar c5" style="width: 93.5%">C5: 6266</div>
</div>
```

**Criterios**:
- [ ] Barras proporcionais
- [ ] Cores por circulo
- [ ] Dados da API /api/v1/dashboard

---

### Tarefa 4: Busca Global no Header

**Status**: PENDENTE
**Prioridade**: BAIXA

**Objetivo**: Campo de busca que funciona em todas as paginas.

**Implementacao**:
- Input no header
- Busca em tempo real (debounce 300ms)
- Dropdown com resultados
- Atalho Cmd+K

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-26 | Sidebar Prospeccao | **CONCLUIDO** |
| 2026-03-26 | Branding INTEL | **CONCLUIDO** |
| 2026-03-25 | Birthday + Settings + Contact | **MERGED** |
