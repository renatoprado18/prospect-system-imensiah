# UX Task Queue - 3FLOW (Frontend)

**Terminal**: 3FLOW
**Prioridade**: ALTA - Melhorias de UX
**Data**: 2026-03-28

---

## Contexto

Feedback do usuario sobre UX:
- Dashboard muito longo, precisa ser mais compacto
- Badges nao sao clicaveis (6 precisam atencao, Health 92%)
- Pagina de contato confusa e muito longa
- Briefings so permitem copiar, nao geram acoes
- "Health 92%" nao explica o que significa

---

## Tasks

### Task 1: Tornar Badges Clicaveis no Morning Briefing
**Status**: [x] CONCLUIDO - Commit b0ea66a
**Arquivo**: `app/templates/rap_dashboard.html`

**Problema**:
- Badge "6 precisam atencao" nao e clicavel
- Badge "Health 92%" nao explica o significado

**Solucao**:
1. Badge "X precisam atencao" -> clicar abre modal com lista dos contatos
2. Badge "Health X%" -> adicionar tooltip explicando:
   "Health Score medio dos seus contatos prioritarios (circulos 1-3)"
3. Clicar no Health -> vai para pagina de analytics ou filtro de contatos com baixo health

**CSS/JS**:
```css
.briefing-stat { cursor: pointer; }
.briefing-stat:hover { transform: scale(1.05); }
```

---

### Task 2: Tornar Stat Cards Clicaveis
**Status**: [x] CONCLUIDO - Commit b0ea66a
**Arquivo**: `app/templates/rap_dashboard.html`

**Problema**:
- Cards "6676 Contatos", "Circulos", "6 Precisam Atencao", "4 Briefings"
- Apenas Briefings e clicavel

**Solucao**:
1. "Contatos" -> vai para /contatos
2. "Circulos" -> vai para /circulos
3. "Precisam Atencao" -> vai para /contatos?filter=needs_attention
4. "Briefings" -> ja funciona, vai para /briefings

---

### Task 3: Redesign Pagina de Contato
**Status**: [ ] Pendente
**Arquivo**: `app/templates/rap_contact_detail.html`

**Problema**:
- Pagina muito longa
- Muitas secoes vazias (Timeline, Resumo IA, Reunioes, Fatos)
- UI confusa

**Solucao - Layout Compacto**:
```
+------------------------------------------+
| <- Voltar    Alba Consultoria     [Acoes]|
|   [Avatar]   Empresa | Cargo             |
|              Email | Tel | LinkedIn      |
|   [Circulo 2] [Health 20%] [Pessoal]     |
+------------------------------------------+
| [Tab: Resumo] [Tab: Timeline] [Tab: AI]  |
+------------------------------------------+
| Conteudo da tab selecionada              |
|                                          |
+------------------------------------------+
```

**Mudancas**:
1. Header mais compacto - tudo em uma area
2. Tabs em vez de scroll longo
3. Esconder secoes vazias ou mostrar CTA para preencher
4. Acoes principais em dropdown ou barra lateral fixa

---

### Task 4: Dashboard Mais Compacto
**Status**: [ ] Pendente
**Arquivo**: `app/templates/rap_dashboard.html`

**Problema**:
- Dashboard muito longo, usuario precisa scrollar muito

**Solucao**:
1. Morning Briefing - manter como esta (bem feito)
2. Stat Cards - manter (compactos)
3. **Reorganizar grid**:
   - Coluna 1 (60%): Agenda + Tarefas (combinados ou tabs)
   - Coluna 2 (40%): Quem Contatar Hoje + Sugestoes IA
4. Remover widgets duplicados ou pouco uteis
5. "Ver tudo" links para paginas dedicadas

---

### Task 5: Briefing Actions UI
**Status**: [ ] Pendente
**Arquivo**: `app/templates/rap_briefings.html`

**Problema**:
- Briefing gerado so permite copiar
- Usuario quer gerar acoes a partir do briefing

**Solucao - Apos gerar briefing, mostrar**:
```
+------------------------------------------+
| Briefing para Alba Consultoria           |
| [Texto do briefing...]                   |
|                                          |
| Acoes:                                   |
| [Criar Tarefa] [Agendar Reuniao]         |
| [Rascunho Email] [Enviar WhatsApp]       |
| [Copiar Texto]                           |
+------------------------------------------+
```

**Botoes**:
- "Criar Tarefa" -> modal para criar tarefa vinculada ao contato
- "Agendar Reuniao" -> abre modal de agendamento
- "Rascunho Email" -> gera email com AI baseado no briefing
- "Enviar WhatsApp" -> abre modal de WhatsApp com sugestao de mensagem

---

### Task 6: Tooltip para Health Score
**Status**: [x] CONCLUIDO - Commit b0ea66a
**Arquivo**: `app/templates/rap_dashboard.html`, `app/templates/rap_contact_detail.html`

**Problema**:
- Usuario nao sabe o que significa "Health 92%"

**Solucao**:
1. Adicionar tooltip em todos os lugares que mostram Health Score
2. Texto: "Health Score indica a saude do relacionamento. Baseado em: frequencia de contato, circulo, ultimo contato, interacoes recentes."
3. Cores: Verde (80-100), Amarelo (50-79), Vermelho (0-49)

---

## Ordem de Execucao

1. **Task 1** - Badges clicaveis (quick win)
2. **Task 2** - Stat cards clicaveis (quick win)
3. **Task 6** - Tooltip Health Score (quick win)
4. **Task 4** - Dashboard compacto
5. **Task 3** - Redesign pagina contato (maior esforco)
6. **Task 5** - Briefing actions UI (depende do backend)

---

## Verificacao

Apos completar, testar:
- [ ] Clicar em "6 precisam atencao" abre lista
- [ ] Clicar em "Health 92%" mostra tooltip ou vai para analytics
- [ ] Todos os stat cards sao clicaveis
- [ ] Dashboard cabe em 1 tela (sem scroll excessivo)
- [ ] Pagina de contato e compacta com tabs
- [ ] Briefing tem botoes de acao
