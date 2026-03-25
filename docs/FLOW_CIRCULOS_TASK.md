# FLOW - Tarefa: Implementar UI de Circulos

> **Instancia**: FLOW (Flow & UX)
> **Coordenador**: ARCH
> **Data**: 2026-03-25
> **Branch**: `feature/circulos-flow`

## Contexto

Estamos transformando o sistema de um foco B2B para um **Assistente Pessoal Inteligente**.
A primeira feature e o sistema de **Circulos** - classificacao dos 12k+ contatos em niveis de proximidade.

**Leia primeiro**: `docs/CIRCULOS_ARCHITECTURE.md` (arquitetura completa)

## Sua Responsabilidade

Implementar a **interface e API** do sistema de Circulos:
1. Endpoints REST para circulos
2. Dashboard de circulos
3. Integracao visual nas paginas existentes

## Arquivos a Criar/Modificar

### 1. MODIFICAR: `app/main.py`

Adicionar os seguintes endpoints:

```python
# ============== CIRCULOS ENDPOINTS ==============

from app.services.circulos import (
    recalcular_circulo_contato,
    recalcular_todos_circulos,
    get_dashboard_circulos,
    get_contatos_precisando_atencao,
    get_aniversarios_proximos,
    CIRCULO_CONFIG
)


@app.get("/api/circulos")
async def get_circulos():
    """Retorna configuracao e estatisticas dos circulos"""
    return get_dashboard_circulos()


@app.get("/api/circulos/{circulo}/contacts")
async def get_contacts_by_circulo(
    circulo: int,
    sort_by: str = "health",
    limit: int = 50,
    offset: int = 0
):
    """Lista contatos de um circulo especifico"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Validar sort_by
        sort_options = {
            "health": "health_score ASC",
            "nome": "nome ASC",
            "ultimo_contato": "ultimo_contato DESC NULLS LAST"
        }
        order_by = sort_options.get(sort_by, "health_score ASC")

        cursor.execute(f"""
            SELECT id, nome, empresa, cargo, foto_url, emails, telefones,
                   circulo, health_score, ultimo_contato, total_interacoes,
                   frequencia_ideal_dias, circulo_manual
            FROM contacts
            WHERE COALESCE(circulo, 5) = %s
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
        """, (circulo, limit, offset))

        contacts = [dict(row) for row in cursor.fetchall()]

        # Total count
        cursor.execute("""
            SELECT COUNT(*) FROM contacts WHERE COALESCE(circulo, 5) = %s
        """, (circulo,))
        total = cursor.fetchone()[0]

        return {
            "circulo": circulo,
            "config": CIRCULO_CONFIG.get(circulo),
            "total": total,
            "contacts": contacts
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
    from app.services.circulos import calcular_score_circulo, calcular_health_score

    with get_db() as conn:
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
async def update_contact_circulo(contact_id: int, data: dict):
    """Atualiza circulo de um contato manualmente"""
    circulo = data.get("circulo")
    frequencia = data.get("frequencia_ideal_dias")

    if circulo and (circulo < 1 or circulo > 5):
        raise HTTPException(status_code=400, detail="Circulo deve ser entre 1 e 5")

    with get_db() as conn:
        cursor = conn.cursor()

        updates = ["circulo_manual = TRUE"]
        params = []

        if circulo:
            updates.append("circulo = %s")
            params.append(circulo)

        if frequencia:
            updates.append("frequencia_ideal_dias = %s")
            params.append(frequencia)

        params.append(contact_id)

        cursor.execute(f"""
            UPDATE contacts
            SET {', '.join(updates)}
            WHERE id = %s
            RETURNING id, nome, circulo, circulo_manual, frequencia_ideal_dias
        """, params)

        result = cursor.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Contato nao encontrado")

        return dict(result)


@app.post("/api/circulos/recalculate")
async def recalculate_circulos(force: bool = False, limit: int = None):
    """Recalcula circulos de todos os contatos"""
    result = recalcular_todos_circulos(force=force, limit=limit)
    return result


@app.post("/api/contacts/{contact_id}/circulo/recalculate")
async def recalculate_contact_circulo(contact_id: int, force: bool = False):
    """Recalcula circulo de um contato especifico"""
    result = recalcular_circulo_contato(contact_id, force=force)
    return result
```

### 2. CRIAR: `app/templates/rap_circulos.html`

Dashboard visual dos circulos:

```html
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Circulos | RAP</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <style>
        .circulo-1 { --cor: #FF6B6B; }
        .circulo-2 { --cor: #4ECDC4; }
        .circulo-3 { --cor: #45B7D1; }
        .circulo-4 { --cor: #96CEB4; }
        .circulo-5 { --cor: #DDA0DD; }

        .circulo-badge {
            background-color: var(--cor);
            color: white;
            padding: 2px 8px;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
        }

        .health-bar {
            height: 6px;
            border-radius: 3px;
            background: #e5e7eb;
        }
        .health-bar-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.3s ease;
        }
        .health-high { background: #22c55e; }
        .health-medium { background: #eab308; }
        .health-low { background: #ef4444; }
    </style>
</head>
<body class="bg-gray-50 min-h-screen">
    <!-- Header -->
    <header class="bg-white shadow-sm border-b">
        <div class="max-w-7xl mx-auto px-4 py-4 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <a href="/rap" class="text-gray-500 hover:text-gray-700">
                    <i data-lucide="arrow-left" class="w-5 h-5"></i>
                </a>
                <h1 class="text-xl font-bold text-gray-800">Meus Circulos</h1>
            </div>
            <button onclick="recalcularTodos()" class="flex items-center gap-2 px-3 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg">
                <i data-lucide="refresh-cw" class="w-4 h-4"></i>
                Recalcular
            </button>
        </div>
    </header>

    <main class="max-w-7xl mx-auto px-4 py-6 space-y-6">
        <!-- Circulo Cards -->
        <div class="grid grid-cols-5 gap-4" id="circuloCards">
            <!-- Populated by JS -->
        </div>

        <!-- Two columns: Precisam Atencao + Aniversarios -->
        <div class="grid md:grid-cols-2 gap-6">
            <!-- Precisam Atencao -->
            <div class="bg-white rounded-xl shadow-sm border p-4">
                <div class="flex items-center justify-between mb-4">
                    <h2 class="font-semibold text-gray-800 flex items-center gap-2">
                        <i data-lucide="alert-circle" class="w-5 h-5 text-orange-500"></i>
                        Precisam Atencao
                    </h2>
                    <a href="#" class="text-sm text-blue-600 hover:underline">Ver todos</a>
                </div>
                <div class="space-y-3" id="precisamAtencao">
                    <!-- Populated by JS -->
                </div>
            </div>

            <!-- Aniversarios -->
            <div class="bg-white rounded-xl shadow-sm border p-4">
                <div class="flex items-center justify-between mb-4">
                    <h2 class="font-semibold text-gray-800 flex items-center gap-2">
                        <i data-lucide="cake" class="w-5 h-5 text-pink-500"></i>
                        Aniversarios Proximos
                    </h2>
                </div>
                <div class="space-y-3" id="aniversarios">
                    <!-- Populated by JS -->
                </div>
            </div>
        </div>

        <!-- Contacts List -->
        <div class="bg-white rounded-xl shadow-sm border p-4">
            <div class="flex items-center justify-between mb-4">
                <h2 class="font-semibold text-gray-800" id="contactsTitle">
                    Todos os Contatos
                </h2>
                <select id="sortBy" onchange="loadContacts()" class="text-sm border rounded-lg px-3 py-1.5">
                    <option value="health">Ordenar por Health</option>
                    <option value="nome">Ordenar por Nome</option>
                    <option value="ultimo_contato">Ordenar por Ultimo Contato</option>
                </select>
            </div>
            <div class="space-y-2" id="contactsList">
                <!-- Populated by JS -->
            </div>
            <div class="mt-4 text-center">
                <button onclick="loadMoreContacts()" id="loadMoreBtn" class="text-blue-600 hover:underline text-sm">
                    Carregar mais
                </button>
            </div>
        </div>
    </main>

    <script>
        let currentCirculo = null;
        let currentOffset = 0;
        const LIMIT = 20;

        async function loadDashboard() {
            try {
                const response = await fetch('/api/circulos');
                const data = await response.json();

                renderCirculoCards(data.por_circulo, data.config);
                renderPrecisamAtencao(data.precisam_atencao);
                renderAniversarios(data.aniversarios);

                // Load all contacts by default
                loadContacts();

            } catch (error) {
                console.error('Erro ao carregar dashboard:', error);
            }
        }

        function renderCirculoCards(porCirculo, config) {
            const container = document.getElementById('circuloCards');
            container.innerHTML = '';

            for (let i = 1; i <= 5; i++) {
                const stats = porCirculo[i] || { total: 0, health_medio: 0 };
                const cfg = config[i];
                const isActive = currentCirculo === i;

                container.innerHTML += `
                    <div onclick="selectCirculo(${i})"
                         class="bg-white rounded-xl shadow-sm border p-4 cursor-pointer transition hover:shadow-md ${isActive ? 'ring-2 ring-offset-2' : ''} circulo-${i}"
                         style="${isActive ? `ring-color: ${cfg.cor}` : ''}">
                        <div class="flex items-center justify-between mb-2">
                            <span class="circulo-badge circulo-${i}">${i}</span>
                            <span class="text-2xl font-bold text-gray-800">${formatNumber(stats.total)}</span>
                        </div>
                        <div class="text-sm text-gray-600 mb-2">${cfg.nome}</div>
                        <div class="flex items-center gap-2">
                            <div class="health-bar flex-1">
                                <div class="health-bar-fill ${getHealthClass(stats.health_medio)}"
                                     style="width: ${stats.health_medio}%"></div>
                            </div>
                            <span class="text-xs text-gray-500">${Math.round(stats.health_medio)}%</span>
                        </div>
                    </div>
                `;
            }

            lucide.createIcons();
        }

        function renderPrecisamAtencao(contacts) {
            const container = document.getElementById('precisamAtencao');

            if (!contacts || contacts.length === 0) {
                container.innerHTML = '<p class="text-gray-500 text-sm">Nenhum contato precisa de atencao urgente</p>';
                return;
            }

            container.innerHTML = contacts.map(c => `
                <a href="/rap/contatos/${c.id}" class="flex items-center gap-3 p-2 hover:bg-gray-50 rounded-lg">
                    <div class="w-10 h-10 rounded-full bg-gray-200 flex items-center justify-center text-gray-600 font-medium">
                        ${c.nome.charAt(0)}
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="font-medium text-gray-800 truncate">${c.nome}</div>
                        <div class="text-xs text-gray-500">
                            Circulo ${c.circulo} - ${formatDaysAgo(c.ultimo_contato)}
                        </div>
                    </div>
                    <div class="health-bar w-16">
                        <div class="health-bar-fill ${getHealthClass(c.health_score)}"
                             style="width: ${c.health_score}%"></div>
                    </div>
                </a>
            `).join('');
        }

        function renderAniversarios(aniversarios) {
            const container = document.getElementById('aniversarios');

            if (!aniversarios || aniversarios.length === 0) {
                container.innerHTML = '<p class="text-gray-500 text-sm">Nenhum aniversario nos proximos dias</p>';
                return;
            }

            container.innerHTML = aniversarios.map(a => `
                <a href="/rap/contatos/${a.id}" class="flex items-center gap-3 p-2 hover:bg-gray-50 rounded-lg">
                    <div class="w-10 h-10 rounded-full bg-pink-100 flex items-center justify-center">
                        <i data-lucide="cake" class="w-5 h-5 text-pink-500"></i>
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="font-medium text-gray-800 truncate">${a.nome}</div>
                        <div class="text-xs text-gray-500">
                            ${formatDate(a.aniversario)}
                        </div>
                    </div>
                    <div class="text-sm font-medium ${a.dias_ate_aniversario <= 3 ? 'text-pink-600' : 'text-gray-600'}">
                        ${a.dias_ate_aniversario === 0 ? 'Hoje!' : `${a.dias_ate_aniversario} dias`}
                    </div>
                </a>
            `).join('');

            lucide.createIcons();
        }

        function selectCirculo(circulo) {
            currentCirculo = currentCirculo === circulo ? null : circulo;
            currentOffset = 0;
            loadDashboard();
        }

        async function loadContacts() {
            currentOffset = 0;
            const sortBy = document.getElementById('sortBy').value;

            let url = currentCirculo
                ? `/api/circulos/${currentCirculo}/contacts?sort_by=${sortBy}&limit=${LIMIT}`
                : `/api/contacts?limit=${LIMIT}&sort_by=${sortBy}`;

            try {
                const response = await fetch(url);
                const data = await response.json();

                const contacts = data.contacts || data;
                const title = currentCirculo
                    ? `Circulo ${currentCirculo} - ${data.config?.nome || ''}`
                    : 'Todos os Contatos';

                document.getElementById('contactsTitle').textContent = title;
                renderContacts(contacts, false);

            } catch (error) {
                console.error('Erro ao carregar contatos:', error);
            }
        }

        async function loadMoreContacts() {
            currentOffset += LIMIT;
            const sortBy = document.getElementById('sortBy').value;

            let url = currentCirculo
                ? `/api/circulos/${currentCirculo}/contacts?sort_by=${sortBy}&limit=${LIMIT}&offset=${currentOffset}`
                : `/api/contacts?limit=${LIMIT}&offset=${currentOffset}&sort_by=${sortBy}`;

            try {
                const response = await fetch(url);
                const data = await response.json();

                const contacts = data.contacts || data;
                renderContacts(contacts, true);

            } catch (error) {
                console.error('Erro ao carregar mais contatos:', error);
            }
        }

        function renderContacts(contacts, append) {
            const container = document.getElementById('contactsList');

            if (!append) {
                container.innerHTML = '';
            }

            if (contacts.length === 0 && !append) {
                container.innerHTML = '<p class="text-gray-500 text-center py-4">Nenhum contato encontrado</p>';
                return;
            }

            contacts.forEach(c => {
                container.innerHTML += `
                    <a href="/rap/contatos/${c.id}" class="flex items-center gap-4 p-3 hover:bg-gray-50 rounded-lg border">
                        <div class="w-12 h-12 rounded-full bg-gray-200 flex items-center justify-center text-gray-600 font-medium text-lg">
                            ${c.foto_url ? `<img src="${c.foto_url}" class="w-12 h-12 rounded-full object-cover">` : c.nome.charAt(0)}
                        </div>
                        <div class="flex-1 min-w-0">
                            <div class="flex items-center gap-2">
                                <span class="font-medium text-gray-800">${c.nome}</span>
                                <span class="circulo-badge circulo-${c.circulo || 5}">${c.circulo || 5}</span>
                                ${c.circulo_manual ? '<i data-lucide="lock" class="w-3 h-3 text-gray-400" title="Circulo manual"></i>' : ''}
                            </div>
                            <div class="text-sm text-gray-500 truncate">
                                ${c.cargo ? c.cargo + (c.empresa ? ' @ ' + c.empresa : '') : c.empresa || 'Sem empresa'}
                            </div>
                        </div>
                        <div class="text-right">
                            <div class="health-bar w-20 mb-1">
                                <div class="health-bar-fill ${getHealthClass(c.health_score || 50)}"
                                     style="width: ${c.health_score || 50}%"></div>
                            </div>
                            <div class="text-xs text-gray-500">
                                ${c.ultimo_contato ? formatDaysAgo(c.ultimo_contato) : 'Sem contato'}
                            </div>
                        </div>
                    </a>
                `;
            });

            document.getElementById('loadMoreBtn').style.display = contacts.length < LIMIT ? 'none' : 'inline';
            lucide.createIcons();
        }

        async function recalcularTodos() {
            if (!confirm('Recalcular circulos de todos os contatos? Isso pode levar alguns minutos.')) {
                return;
            }

            try {
                const response = await fetch('/api/circulos/recalculate', { method: 'POST' });
                const data = await response.json();

                alert(`Recalculo concluido!\n\nContatos atualizados: ${data.atualizados}\nMudancas de circulo: ${data.mudancas?.length || 0}`);
                loadDashboard();

            } catch (error) {
                console.error('Erro ao recalcular:', error);
                alert('Erro ao recalcular circulos');
            }
        }

        // Helpers
        function formatNumber(n) {
            if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
            return n;
        }

        function getHealthClass(health) {
            if (health >= 70) return 'health-high';
            if (health >= 40) return 'health-medium';
            return 'health-low';
        }

        function formatDaysAgo(date) {
            if (!date) return 'Nunca';
            const days = Math.floor((new Date() - new Date(date)) / (1000 * 60 * 60 * 24));
            if (days === 0) return 'Hoje';
            if (days === 1) return 'Ontem';
            return `${days} dias atras`;
        }

        function formatDate(date) {
            if (!date) return '';
            return new Date(date).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
        }

        // Init
        document.addEventListener('DOMContentLoaded', () => {
            lucide.createIcons();
            loadDashboard();
        });
    </script>
</body>
</html>
```

### 3. MODIFICAR: `app/templates/rap_contact_detail.html`

Adicionar card de Circulo na pagina de detalhe do contato:

```html
<!-- Adicionar apos o card de informacoes basicas -->
<div class="bg-white rounded-xl shadow-sm border p-4">
    <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-gray-800">Circulo</h3>
        <button onclick="editarCirculo()" class="text-sm text-blue-600 hover:underline">
            Editar
        </button>
    </div>
    <div id="circuloInfo">
        <!-- Populated by JS -->
    </div>
</div>
```

E adicionar o JavaScript correspondente para carregar e editar o circulo.

### 4. Adicionar rota no main.py

```python
@app.get("/rap/circulos", response_class=HTMLResponse)
async def rap_circulos_page(request: Request):
    return templates.TemplateResponse("rap_circulos.html", {"request": request})
```

## Dependencias

Esta tarefa **depende** de INTEL ter completado `app/services/circulos.py`.
Voce pode comecar pela UI e endpoints, usando dados mockados ate o servico estar pronto.

## Testes Recomendados

1. Dashboard carrega corretamente
2. Clicar em circulo filtra contatos
3. Ordenacao funciona (health, nome, ultimo_contato)
4. Precisam atencao mostra contatos corretos
5. Aniversarios mostra datas corretas
6. Editar circulo manualmente funciona
7. Recalcular todos funciona

## Criterios de Conclusao

- [ ] Endpoints em main.py funcionando
- [ ] rap_circulos.html criado e funcional
- [ ] Rota /rap/circulos configurada
- [ ] Integracao com rap_contact_detail.html
- [ ] Testes manuais passando

## Comunicacao

Ao terminar, atualize `docs/COORDINATION.md`:

```
[DATA FLOW] **FEATURE: Circulos UI**
Status: PRONTO PARA REVIEW
Arquivos modificados:
- app/main.py (endpoints)
- app/templates/rap_circulos.html (novo)
- app/templates/rap_contact_detail.html (card circulo)
Testado: [listar testes realizados]
Depende de: INTEL circulos.py
```

---

**Duvidas?** Consulte `docs/CIRCULOS_ARCHITECTURE.md` ou pergunte ao ARCH.
