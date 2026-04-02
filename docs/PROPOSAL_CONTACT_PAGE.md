# Proposta: Página de Contato Mais Acionável

## Problemas Identificados

### 1. Bug: "Responder agora" remove ação sem resposta
- Ao clicar "Responder agora" no dashboard, a ação é marcada como executada
- Usuário é redirecionado para página do contato
- Mas ele ainda não respondeu! A ação deveria permanecer até resposta real

### 2. Página de Contato Pouco Acionável
- Não mostra sugestões de ações imediatas
- Não destaca o que precisa de atenção
- Falta contexto sobre o relacionamento
- Informações importantes não estão visíveis

---

## Proposta de Solução

### A. Correção do Bug "Responder agora"

**Comportamento atual:**
```
Clica "Responder agora" → Marca como executada → Redireciona
```

**Comportamento proposto:**
```
Clica "Responder agora" → NÃO marca como executada → Redireciona
                          → Só marca quando usuário ENVIA resposta
```

**Opção alternativa:** Criar tarefa "Responder [Nome]" automaticamente

---

### B. Novo Painel "Ações Sugeridas" na Página de Contato

Adicionar um painel destacado no topo da página (antes das tabs) que analisa o contexto e sugere ações.

#### Localização no Layout:
```
┌─────────────────────────────────────────────────────┐
│  ← Voltar para Contatos                             │
├─────────────────────────────────────────────────────┤
│  [Avatar]  Alfredo Villela                          │
│            +55 11 99911-4301                        │
│            ♥ P2  💼 R1  ❤️ 100%     [📧][📱][+]... │
├─────────────────────────────────────────────────────┤
│  ⚡ AÇÕES SUGERIDAS                          [🔄]   │  ← NOVO PAINEL
│  ┌─────────────────────────────────────────────┐    │
│  │ 💬 Responder mensagem de hoje               │    │
│  │ "Oi Renatão, Td bem? Queria pedir..."       │    │
│  │ [Responder] [Criar Tarefa] [Ignorar]        │    │
│  └─────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────┐    │
│  │ 🔍 Enriquecer perfil                        │    │
│  │ Falta: LinkedIn, Empresa, Cargo, Email      │    │
│  │ [Buscar LinkedIn] [Preencher Manual]        │    │
│  └─────────────────────────────────────────────┘    │
├─────────────────────────────────────────────────────┤
│  [Resumo] [Timeline] [IA] [Reuniões]               │
│  ...                                                │
└─────────────────────────────────────────────────────┘
```

#### Tipos de Sugestões:

| Tipo | Condição | Ação Sugerida |
|------|----------|---------------|
| **Mensagem pendente** | Última msg recebida sem resposta | "Responder mensagem" |
| **Perfil incompleto** | Falta LinkedIn/email/empresa | "Enriquecer perfil" |
| **Agendar conversa** | Msg menciona "marcar/agendar" | "Agendar reunião" |
| **Aniversário próximo** | Aniversário em ≤7 dias | "Enviar felicitações" |
| **Tempo sem contato** | Dias > frequência ideal | "Fazer check-in" |
| **Tarefa vencida** | Tarefa pendente vencida | "Resolver tarefa" |
| **Círculo importante** | C1 ou C2 (ambos contextos) | Destacar importância |

---

### C. Painel de Contexto/Insights

Adicionar na sidebar ou abaixo do header um mini-painel com insights:

```
┌─────────────────────────────────┐
│ 📊 CONTEXTO                     │
├─────────────────────────────────┤
│ Círculos: P2 (Próximo) + R1 (Core)
│ → Relacionamento importante!    │
│                                 │
│ Última interação: Hoje          │
│ Frequência ideal: 7 dias        │
│ Status: Aguardando resposta     │
│                                 │
│ ⚠️ Perfil 40% completo         │
│ Falta: LinkedIn, Email, Empresa │
└─────────────────────────────────┘
```

---

### D. Análise de Contexto das Mensagens

Usar IA para extrair contexto das mensagens recentes:

**Para Alfredo Villela:**
- "Pediu desculpas por sumir" → Reativação de relacionamento
- "Avancei com solução interna" → Possível oportunidade de negócio
- "Quer marcar conversa" → Ação: Agendar reunião

---

## Implementação Técnica

### Arquivos a Modificar:

| Arquivo | Mudança |
|---------|---------|
| `app/services/action_executor.py` | Não marcar "open_conversation" como executada |
| `app/templates/rap_contact_detail.html` | Adicionar painel de ações sugeridas |
| `app/services/contact_suggestions.py` | **NOVO** - Gerar sugestões para contato |
| `app/main.py` | Endpoint `/api/contacts/{id}/suggestions` |

### Novo Endpoint:

```python
GET /api/contacts/{contact_id}/suggestions

Response:
{
  "suggestions": [
    {
      "type": "pending_message",
      "priority": "high",
      "title": "Responder mensagem",
      "description": "Mensagem recebida há 2 horas",
      "context": "Oi Renatão, Td bem?...",
      "actions": [
        {"id": "reply", "label": "Responder", "primary": true},
        {"id": "task", "label": "Criar Tarefa"},
        {"id": "dismiss", "label": "Ignorar"}
      ]
    },
    {
      "type": "incomplete_profile",
      "priority": "medium",
      "title": "Enriquecer perfil",
      "missing": ["linkedin", "email", "empresa", "cargo"],
      "actions": [
        {"id": "linkedin_search", "label": "Buscar LinkedIn"},
        {"id": "manual", "label": "Preencher Manual"}
      ]
    }
  ],
  "insights": {
    "relationship_status": "Reativando relacionamento",
    "circles_note": "Presente em P2 e R1 - relacionamento importante",
    "profile_completeness": 40,
    "last_interaction": "hoje",
    "awaiting_response": true
  }
}
```

---

## Prioridade de Implementação

1. **[CRÍTICO]** Corrigir bug "Responder agora" (5 min)
2. **[ALTO]** Painel de ações sugeridas básico (2h)
3. **[MÉDIO]** Insights de contexto (1h)
4. **[BAIXO]** Análise de mensagens com IA (2h)

---

## Perguntas para Decisão

1. **"Responder agora" deve criar tarefa automaticamente?**
   - Opção A: Não marca como executada, usuário decide
   - Opção B: Cria tarefa "Responder [Nome]" automaticamente

2. **Onde colocar o painel de ações?**
   - Opção A: Entre header e tabs (sempre visível)
   - Opção B: Dentro da tab Resumo (mais discreto)

3. **Usar IA para analisar mensagens?**
   - Aumenta custo mas dá insights melhores
   - Pode ser opcional (botão "Analisar com IA")

---

## Aprovação

- [ ] Corrigir bug "Responder agora"
- [ ] Implementar painel de ações sugeridas
- [ ] Adicionar insights de contexto
- [ ] Análise de mensagens com IA

**Aguardando aprovação para iniciar implementação.**
