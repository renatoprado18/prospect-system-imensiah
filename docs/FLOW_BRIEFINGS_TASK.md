# FLOW - Tarefa: UI e Endpoints de Briefings

> **Instancia**: FLOW (Flow & UX)
> **Coordenador**: ARCH
> **Data**: 2026-03-25
> **Branch**: `feature/briefings-flow`

## Contexto

INTEL esta implementando `app/services/briefings.py` com a logica de geracao de briefings.
FLOW deve criar os endpoints e UI para consumir esse servico.

**Pode trabalhar em paralelo**: Use mocks ate INTEL terminar.

## Objetivo

1. Criar endpoints para briefings
2. Criar pagina de briefings
3. Integrar briefing na pagina de detalhe do contato

## Arquivos a Modificar/Criar

### 1. MODIFICAR: `app/main.py`

Adicionar endpoints:

```python
# ============== BRIEFINGS ENDPOINTS ==============

from services.briefings import (
    generate_briefing,
    get_contacts_needing_briefing
)

@app.get("/api/briefings/pending")
async def get_pending_briefings(limit: int = 10):
    """Lista contatos que precisam de briefing"""
    return get_contacts_needing_briefing(limit=limit)


@app.post("/api/contacts/{contact_id}/briefing")
async def create_contact_briefing(contact_id: int, data: dict = None):
    """Gera briefing para um contato"""
    contexto = data.get("contexto") if data else None
    result = generate_briefing(
        contact_id=contact_id,
        contexto_reuniao=contexto,
        incluir_sugestoes=True
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/rap/briefings", response_class=HTMLResponse)
async def rap_briefings_page(request: Request):
    """Pagina de briefings"""
    return templates.TemplateResponse("rap_briefings.html", {"request": request})
```

### 2. CRIAR: `app/templates/rap_briefings.html`

```html
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Briefings | RAP</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <style>
        .briefing-content h2 { font-weight: 600; margin-top: 1rem; }
        .briefing-content ul { list-style: disc; margin-left: 1.5rem; }
        .briefing-content li { margin: 0.25rem 0; }
    </style>
</head>
<body class="bg-gray-50 min-h-screen">
    <!-- Header -->
    <header class="bg-white shadow-sm border-b">
        <div class="max-w-4xl mx-auto px-4 py-4 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <a href="/rap" class="text-gray-500 hover:text-gray-700">
                    <i data-lucide="arrow-left" class="w-5 h-5"></i>
                </a>
                <h1 class="text-xl font-bold text-gray-800">Briefings</h1>
            </div>
        </div>
    </header>

    <main class="max-w-4xl mx-auto px-4 py-6 space-y-6">
        <!-- Precisam Briefing -->
        <div class="bg-white rounded-xl shadow-sm border p-4">
            <h2 class="font-semibold text-gray-800 mb-4 flex items-center gap-2">
                <i data-lucide="users" class="w-5 h-5 text-blue-500"></i>
                Contatos que Precisam Briefing
            </h2>
            <div class="space-y-3" id="pendingList">
                <p class="text-gray-500 text-sm">Carregando...</p>
            </div>
        </div>

        <!-- Gerar Briefing Manual -->
        <div class="bg-white rounded-xl shadow-sm border p-4">
            <h2 class="font-semibold text-gray-800 mb-4 flex items-center gap-2">
                <i data-lucide="file-text" class="w-5 h-5 text-green-500"></i>
                Gerar Briefing
            </h2>
            <div class="space-y-4">
                <div>
                    <label class="block text-sm text-gray-600 mb-1">Buscar Contato</label>
                    <input type="text" id="searchContact" placeholder="Digite o nome..."
                           class="w-full border rounded-lg px-3 py-2"
                           onkeyup="searchContacts(this.value)">
                    <div id="searchResults" class="mt-2 space-y-1 hidden"></div>
                </div>
                <div id="selectedContact" class="hidden">
                    <div class="flex items-center gap-3 p-3 bg-blue-50 rounded-lg">
                        <div id="contactAvatar" class="w-10 h-10 rounded-full bg-blue-200"></div>
                        <div>
                            <div id="contactName" class="font-medium"></div>
                            <div id="contactInfo" class="text-sm text-gray-500"></div>
                        </div>
                        <button onclick="clearContact()" class="ml-auto text-gray-400 hover:text-gray-600">
                            <i data-lucide="x" class="w-5 h-5"></i>
                        </button>
                    </div>
                </div>
                <div>
                    <label class="block text-sm text-gray-600 mb-1">Contexto (opcional)</label>
                    <input type="text" id="contexto" placeholder="Ex: Reuniao de conselho"
                           class="w-full border rounded-lg px-3 py-2">
                </div>
                <button onclick="generateBriefing()" id="generateBtn"
                        class="w-full bg-blue-600 text-white py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50"
                        disabled>
                    <i data-lucide="sparkles" class="w-4 h-4 inline mr-2"></i>
                    Gerar Briefing
                </button>
            </div>
        </div>

        <!-- Briefing Result -->
        <div id="briefingResult" class="hidden">
            <div class="bg-white rounded-xl shadow-sm border p-6">
                <div class="flex items-center justify-between mb-4">
                    <h2 class="font-semibold text-gray-800 flex items-center gap-2">
                        <i data-lucide="file-text" class="w-5 h-5 text-purple-500"></i>
                        <span id="briefingTitle">Briefing</span>
                    </h2>
                    <div class="flex items-center gap-2">
                        <span id="briefingCirculo" class="text-sm px-2 py-1 rounded-full"></span>
                        <span id="briefingHealth" class="text-sm"></span>
                    </div>
                </div>
                <div id="briefingContent" class="briefing-content prose prose-sm max-w-none">
                </div>
                <div class="mt-4 pt-4 border-t flex items-center justify-between text-sm text-gray-500">
                    <span id="briefingMeta"></span>
                    <button onclick="copyBriefing()" class="text-blue-600 hover:underline">
                        <i data-lucide="copy" class="w-4 h-4 inline mr-1"></i>
                        Copiar
                    </button>
                </div>
            </div>
        </div>
    </main>

    <script>
        let selectedContactId = null;
        let currentBriefing = null;

        async function loadPending() {
            try {
                const response = await fetch('/api/briefings/pending?limit=10');
                const data = await response.json();
                renderPending(data);
            } catch (error) {
                console.error('Erro ao carregar pendentes:', error);
            }
        }

        function renderPending(contacts) {
            const container = document.getElementById('pendingList');
            if (!contacts || contacts.length === 0) {
                container.innerHTML = '<p class="text-gray-500 text-sm">Nenhum contato precisa de briefing urgente</p>';
                return;
            }

            container.innerHTML = contacts.map(c => `
                <div class="flex items-center gap-3 p-3 hover:bg-gray-50 rounded-lg cursor-pointer"
                     onclick="selectContact(${c.id}, '${c.nome}', '${c.empresa || ''}')">
                    <div class="w-10 h-10 rounded-full bg-gray-200 flex items-center justify-center font-medium">
                        ${c.nome.charAt(0)}
                    </div>
                    <div class="flex-1">
                        <div class="font-medium">${c.nome}</div>
                        <div class="text-sm text-gray-500">${c.razao || ''}</div>
                    </div>
                    <span class="text-xs px-2 py-1 rounded-full ${c.prioridade === 'alta' ? 'bg-red-100 text-red-700' : 'bg-yellow-100 text-yellow-700'}">
                        ${c.prioridade || 'media'}
                    </span>
                </div>
            `).join('');
        }

        async function searchContacts(query) {
            if (query.length < 2) {
                document.getElementById('searchResults').classList.add('hidden');
                return;
            }

            try {
                const response = await fetch(`/api/contacts?search=${encodeURIComponent(query)}&limit=5`);
                const contacts = await response.json();

                const container = document.getElementById('searchResults');
                container.classList.remove('hidden');
                container.innerHTML = contacts.map(c => `
                    <div class="p-2 hover:bg-gray-100 rounded cursor-pointer"
                         onclick="selectContact(${c.id}, '${c.nome}', '${c.empresa || ''}')">
                        <span class="font-medium">${c.nome}</span>
                        ${c.empresa ? `<span class="text-gray-500 text-sm"> - ${c.empresa}</span>` : ''}
                    </div>
                `).join('');
            } catch (error) {
                console.error('Erro na busca:', error);
            }
        }

        function selectContact(id, nome, empresa) {
            selectedContactId = id;
            document.getElementById('searchContact').value = '';
            document.getElementById('searchResults').classList.add('hidden');
            document.getElementById('selectedContact').classList.remove('hidden');
            document.getElementById('contactName').textContent = nome;
            document.getElementById('contactInfo').textContent = empresa;
            document.getElementById('contactAvatar').textContent = nome.charAt(0);
            document.getElementById('generateBtn').disabled = false;
            lucide.createIcons();
        }

        function clearContact() {
            selectedContactId = null;
            document.getElementById('selectedContact').classList.add('hidden');
            document.getElementById('generateBtn').disabled = true;
        }

        async function generateBriefing() {
            if (!selectedContactId) return;

            const btn = document.getElementById('generateBtn');
            btn.disabled = true;
            btn.innerHTML = '<i data-lucide="loader" class="w-4 h-4 inline mr-2 animate-spin"></i> Gerando...';

            try {
                const contexto = document.getElementById('contexto').value;
                const response = await fetch(`/api/contacts/${selectedContactId}/briefing`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ contexto: contexto || null })
                });

                if (!response.ok) throw new Error('Erro ao gerar briefing');

                const data = await response.json();
                currentBriefing = data;
                renderBriefing(data);

            } catch (error) {
                console.error('Erro:', error);
                alert('Erro ao gerar briefing');
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i data-lucide="sparkles" class="w-4 h-4 inline mr-2"></i> Gerar Briefing';
                lucide.createIcons();
            }
        }

        function renderBriefing(data) {
            document.getElementById('briefingResult').classList.remove('hidden');
            document.getElementById('briefingTitle').textContent = `Briefing: ${data.nome}`;

            // Circulo badge
            const circuloBadge = document.getElementById('briefingCirculo');
            const circulo = data.circulo || 5;
            const cores = { 1: 'bg-red-100 text-red-700', 2: 'bg-teal-100 text-teal-700', 3: 'bg-blue-100 text-blue-700', 4: 'bg-green-100 text-green-700', 5: 'bg-purple-100 text-purple-700' };
            circuloBadge.className = `text-sm px-2 py-1 rounded-full ${cores[circulo]}`;
            circuloBadge.textContent = `Circulo ${circulo}`;

            // Health
            const health = data.health_score || 50;
            document.getElementById('briefingHealth').textContent = `Health: ${health}%`;

            // Content (convert markdown-ish to HTML)
            let content = data.briefing || '';
            content = content.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            content = content.replace(/^- /gm, '• ');
            content = content.replace(/\n/g, '<br>');
            document.getElementById('briefingContent').innerHTML = content;

            // Meta
            const dias = data.dias_sem_contato;
            document.getElementById('briefingMeta').textContent =
                `Ultimo contato: ${dias !== null ? dias + ' dias atras' : 'desconhecido'} | Gerado: agora`;

            // Scroll to result
            document.getElementById('briefingResult').scrollIntoView({ behavior: 'smooth' });
        }

        function copyBriefing() {
            if (!currentBriefing) return;
            navigator.clipboard.writeText(currentBriefing.briefing);
            alert('Briefing copiado!');
        }

        // Init
        document.addEventListener('DOMContentLoaded', () => {
            lucide.createIcons();
            loadPending();
        });
    </script>
</body>
</html>
```

### 3. MODIFICAR: `app/templates/rap_contact_detail.html`

Adicionar botao/card de briefing na pagina de detalhe do contato:

```html
<!-- Card de Briefing (adicionar apos info basica) -->
<div class="bg-white rounded-xl shadow-sm border p-4">
    <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-gray-800 flex items-center gap-2">
            <i data-lucide="file-text" class="w-5 h-5 text-purple-500"></i>
            Briefing
        </h3>
        <button onclick="gerarBriefing()" id="btnBriefing"
                class="text-sm text-blue-600 hover:underline">
            Gerar Briefing
        </button>
    </div>
    <div id="briefingContainer">
        <p class="text-gray-500 text-sm">Clique em "Gerar Briefing" para preparar um resumo.</p>
    </div>
</div>
```

## Mock para Desenvolvimento

Enquanto INTEL nao termina, use este mock em `services/briefings.py`:

```python
def generate_briefing(contact_id, contexto_reuniao=None, incluir_sugestoes=True):
    return {
        "contact_id": contact_id,
        "nome": "Contato Teste",
        "briefing": "**RESUMO**\nEste e um briefing de teste.\n\n**PONTOS DE ATENCAO**\n- Item 1\n- Item 2",
        "circulo": 3,
        "health_score": 75,
        "dias_sem_contato": 15,
        "gerado_em": "2026-03-25T15:00:00"
    }

def get_contacts_needing_briefing(limit=10):
    return [
        {"id": 1, "nome": "Teste 1", "empresa": "Empresa A", "razao": "Health baixo", "prioridade": "alta"},
        {"id": 2, "nome": "Teste 2", "empresa": "Empresa B", "razao": "Aniversario", "prioridade": "media"}
    ]
```

## Criterios de Conclusao

- [ ] Endpoints em main.py funcionando
- [ ] rap_briefings.html criado
- [ ] Busca de contato funciona
- [ ] Geracao de briefing (mock ou real)
- [ ] Exibicao do briefing formatado
- [ ] Botao copiar funciona
- [ ] Atualizar COORDINATION.md

## Comunicacao

Ao terminar, atualize `docs/COORDINATION.md`:

```
[DATA FLOW] **FEATURE: Briefings UI**
Status: PRONTO PARA REVIEW
Arquivos: main.py, rap_briefings.html
Testado: [listar testes]
Depende de: INTEL briefings.py
```
