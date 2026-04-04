# Sistema de Templates - INTEL

## Visao Geral

O sistema usa **Jinja2 template inheritance** para evitar duplicacao de codigo. Todos os templates herdam de `rap_base.html`, que contem:

- Estrutura HTML base (head, meta tags, Bootstrap)
- Menu lateral (sidebar) unificado
- CSS comum (cores, tipografia, componentes)
- Scripts comuns (Bootstrap JS)

## Estrutura de Arquivos

```
app/templates/
├── rap_base.html           # Template base (NAO EDITAR diretamente, exceto para mudancas globais)
├── rap_dashboard.html      # Dashboard principal
├── rap_contacts.html       # Lista de contatos
├── rap_contact_detail.html # Detalhe do contato
├── rap_contact_edit.html   # Edicao de contato
├── rap_settings.html       # Configuracoes
├── rap_whatsapp.html       # Integracao WhatsApp
└── intel_duplicados.html   # Gestao de duplicados
```

## Template Base (rap_base.html)

### Blocos Disponiveis

| Bloco | Descricao | Obrigatorio |
|-------|-----------|-------------|
| `title` | Titulo da pagina (aparece na aba) | Sim |
| `page_title` | Titulo exibido no header da pagina | Nao |
| `page_subtitle` | Subtitulo no header | Nao |
| `header_actions` | Botoes no header (lado direito) | Nao |
| `extra_css` | CSS especifico da pagina | Nao |
| `content` | Conteudo principal da pagina | Sim |
| `modals` | Modals Bootstrap | Nao |
| `extra_js` | JavaScript especifico da pagina | Nao |

### Exemplo de Uso

```html
{% extends "rap_base.html" %}

{% block title %}INTEL - Minha Pagina{% endblock %}

{% block page_title %}Titulo da Pagina{% endblock %}
{% block page_subtitle %}<p class="page-subtitle">Descricao breve</p>{% endblock %}

{% block header_actions %}
<button class="btn btn-primary">Acao</button>
{% endblock %}

{% block extra_css %}
.meu-componente {
    background: var(--primary);
    border-radius: 12px;
}
{% endblock %}

{% block content %}
<div class="meu-componente">
    Conteudo aqui
</div>
{% endblock %}

{% block modals %}
<div class="modal fade" id="meuModal">...</div>
{% endblock %}

{% block extra_js %}
<script>
    // JavaScript especifico
</script>
{% endblock %}
```

## Menu Lateral (Sidebar)

### Estrutura

O menu lateral esta definido em `rap_base.html` e contem as seguintes secoes:

1. **Relacionamentos**: Dashboard, Contatos
2. **Gestao**: Projetos, Tarefas
3. **Conteudo**: Governanca, Newsletter, Hot Takes, Editorial
4. **Comunicacao**: Emails, WhatsApp
5. **Ferramentas**: Duplicados
6. **Sistema**: Configuracoes

### Como Alterar o Menu

Para modificar o menu lateral, edite **apenas** `rap_base.html`:

**Localização:** Procure por `<nav class="sidebar-nav">`

```html
<nav class="sidebar-nav">
    <div class="nav-section">RELACIONAMENTOS</div>
    <a href="/dashboard" class="nav-item {% if request.url.path == '/dashboard' %}active{% endif %}">
        <i class="bi bi-grid-1x2"></i>
        <span>Dashboard</span>
    </a>
    <!-- Mais itens... -->
</nav>
```

### Como Ajustar Fontes/Espacamentos do Menu

Edite o CSS em `rap_base.html`, procure por:

```css
/* Navigation */
.nav-section {
    padding: 6px 16px;        /* Espacamento vertical/horizontal */
    font-size: 0.65rem;       /* Tamanho da fonte das secoes */
    margin-top: 10px;         /* Espaco entre secoes */
}

.nav-item {
    gap: 10px;                /* Espaco entre icone e texto */
    padding: 8px 20px;        /* Espacamento do item */
    font-size: 0.875rem;      /* Tamanho da fonte dos itens */
}

.nav-item i {
    font-size: 1rem;          /* Tamanho do icone */
    width: 20px;              /* Largura fixa do icone */
}
```

## Variaveis CSS

O sistema usa variaveis CSS definidas em `rap_base.html`:

```css
:root {
    --primary: #6366f1;       /* Cor principal (indigo) */
    --primary-dark: #4f46e5;  /* Cor principal escura */
    --secondary: #0ea5e9;     /* Cor secundaria (azul) */
    --success: #10b981;       /* Verde */
    --warning: #f59e0b;       /* Amarelo */
    --danger: #ef4444;        /* Vermelho */
    --dark: #1e293b;          /* Texto escuro */
    --gray-100 a --gray-600;  /* Tons de cinza */
    --sidebar-width: 260px;   /* Largura do menu lateral */
}
```

## Boas Praticas

1. **Nunca duplique o sidebar** - Use `{% extends "rap_base.html" %}`

2. **CSS especifico no bloco `extra_css`** - Nao inclua tags `<style>`

3. **Use variaveis CSS** - Prefira `var(--primary)` em vez de `#6366f1`

4. **Mantenha consistencia** - Use os mesmos border-radius, shadows, etc.

5. **JavaScript no bloco `extra_js`** - Inclua as tags `<script>`

## Migracao de Templates Antigos

Se precisar migrar um template que nao usa heranca:

1. Remova todo o `<head>` e `</head>`
2. Remova o sidebar (`<aside class="sidebar">...</aside>`)
3. Remova o wrapper `<main class="main-content">`
4. Adicione `{% extends "rap_base.html" %}` no inicio
5. Coloque CSS especifico em `{% block extra_css %}`
6. Coloque conteudo em `{% block content %}`
7. Coloque modals em `{% block modals %}`
8. Coloque JS em `{% block extra_js %}`

## Historico

- **2024-04**: Unificacao dos templates com heranca
  - Convertidos: intel_duplicados, rap_settings, rap_contact_edit, rap_whatsapp, rap_contacts, rap_contact_detail, rap_dashboard
  - Reducao media de codigo: ~20%
  - Menu lateral agora gerenciado centralmente
