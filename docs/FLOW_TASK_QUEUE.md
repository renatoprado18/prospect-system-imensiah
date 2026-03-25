# Fila de Tarefas 3FLOW

**Atualizacao**: 2026-03-25
**Modo**: Autonomo - sinalizar conclusao e continuar

## Como Trabalhar

1. Pegue a proxima tarefa PENDENTE
2. Crie branch: `feature/flow-{nome-curto}`
3. Implemente e teste
4. Commit e push para a branch
5. Atualize status para PRONTO e adicione resumo
6. **Continue para a proxima tarefa** (nao espere aprovacao)
7. ARCH fara review e merge em paralelo

---

## Tarefa 1: Atualizar Sidebar INTEL

**Status**: PENDENTE
**Branch**: `feature/flow-sidebar-intel`
**Prioridade**: ALTA

### Objetivo
Completar a migracao visual do sidebar para o novo branding.

### Implementacao

1. **Mudar logo "RAP" para "INTEL"** em todos os templates
2. **Adicionar badges dinamicos**:
   - Circulos: mostrar "X precisam atencao"
   - Inbox: mostrar contagem de nao lidos
3. **Destacar Circulos e Briefings** como features novas
4. **Remover ou ocultar** secao "Prospects" (sistema antigo)

### Arquivos
- `app/templates/rap_dashboard.html`
- `app/templates/components/sidebar.html` (se existir)
- Todos os templates que tem sidebar

### Criterio de Aceite
- [ ] Logo mostra "INTEL" em todas as paginas
- [ ] Badge de atencao no menu Circulos
- [ ] Secao Prospects removida/oculta

---

## Tarefa 2: Widget de Circulos no Dashboard

**Status**: PENDENTE
**Branch**: `feature/flow-dashboard-circulos`
**Prioridade**: ALTA

### Objetivo
Adicionar visualizacao rapida dos circulos no dashboard principal.

### Implementacao

```html
<!-- Widget de Circulos -->
<div class="circulos-widget">
    <h3>Saude dos Relacionamentos</h3>

    <!-- Barras por circulo -->
    <div class="circulo-row circulo-1">
        <span>Intimo</span>
        <div class="bar" style="width: 85%"></div>
        <span>12 contatos</span>
    </div>
    <!-- ... circulos 2-5 -->

    <!-- Alertas -->
    <div class="alertas">
        <span class="badge warning">5 precisam atencao</span>
        <span class="badge info">2 aniversarios esta semana</span>
    </div>

    <a href="/circulos">Ver detalhes</a>
</div>
```

### Criterio de Aceite
- [ ] Widget exibe resumo dos 5 circulos
- [ ] Mostra health medio por circulo
- [ ] Link para pagina de circulos
- [ ] Cores corretas por circulo

---

## Tarefa 3: Pagina de Configuracoes

**Status**: PENDENTE
**Branch**: `feature/flow-settings`
**Prioridade**: MEDIA

### Objetivo
Criar pagina /config para ajustes do sistema.

### Implementacao

Secoes:
1. **Perfil**: Nome, email, foto
2. **Circulos**: Ajustar frequencias ideais
3. **Notificacoes**: Email de alertas, lembretes
4. **Integracao**: Status Google, WhatsApp

### Arquivos
- `app/templates/intel_settings.html` (novo)
- `app/main.py`: Rota GET /config

### Criterio de Aceite
- [ ] Pagina acessivel em /config
- [ ] Exibe configuracoes atuais
- [ ] Permite editar frequencia dos circulos

---

## Tarefa 4: Notificacoes de Aniversario

**Status**: PENDENTE
**Branch**: `feature/flow-birthday-notifications`
**Prioridade**: MEDIA

### Objetivo
Mostrar alertas de aniversarios de forma mais visivel.

### Implementacao

1. **Banner no topo** quando ha aniversarios hoje
2. **Card especial** para aniversarios da semana
3. **Acao rapida**: "Enviar mensagem" direto do alerta

### Criterio de Aceite
- [ ] Banner aparece quando aniversario hoje
- [ ] Lista aniversarios proximos 7 dias
- [ ] Botao para enviar WhatsApp/Email

---

## Tarefa 5: Busca Global

**Status**: PENDENTE
**Branch**: `feature/flow-search`
**Prioridade**: MEDIA

### Objetivo
Implementar busca que funcione em todas as paginas.

### Implementacao

1. **Campo de busca no header** (ja existe parcialmente)
2. **Busca em tempo real** (debounce 300ms)
3. **Resultados**: contatos, empresas, tags
4. **Atalho de teclado**: Cmd+K ou /

### Criterio de Aceite
- [ ] Busca funciona de qualquer pagina
- [ ] Mostra resultados em dropdown
- [ ] Navega para contato ao clicar

---

## Tarefa 6: Melhorar Pagina de Contato

**Status**: PRONTO
**Branch**: `feature/intel-engajamento` (commit f5f3262)
**Prioridade**: BAIXA

### Objetivo
Adicionar info de Circulos e Health na pagina do contato.

### Implementacao

- Mostrar circulo atual com badge colorido
- Mostrar health score com barra visual
- Historico de mudancas de circulo
- Botao para gerar briefing

---

## Registro de Conclusao

| Tarefa | Branch | Status | Resumo |
|--------|--------|--------|--------|
| 1. Sidebar INTEL | - | PENDENTE | - |
| 2. Widget Circulos | - | PENDENTE | - |
| 3. Configuracoes | - | PENDENTE | - |
| 4. Notif. Aniversario | - | PENDENTE | - |
| 5. Busca Global | - | PENDENTE | - |
| 6. Pagina Contato | feature/intel-engajamento (f5f3262) | PRONTO | Circulo badge, Health card, Briefing button |
