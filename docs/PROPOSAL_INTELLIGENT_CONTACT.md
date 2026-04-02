# Proposta: Página de Contato com Inteligência Real

## Visão

Transformar a página de contato de um **repositório de dados** para um **assistente de relacionamento inteligente** que:

- **Entende** o contexto completo do relacionamento
- **Aprende** com cada interação
- **Sugere** ações contextualizadas
- **Responde** perguntas sobre a pessoa
- **Antecipa** necessidades

---

## O Conceito: INTEL Assistant por Contato

Cada contato tem um "assistente" que conhece toda a história do relacionamento e pode conversar sobre ele.

### Layout Proposto

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ← Voltar                                                    [⚙️] [📤]    │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  [👤]  ALFREDO VILLELA                                                   │
│        Sócio-Diretor @ TechCorp | Amigo de 15 anos                      │
│        📱 +55 11 99911-4301  |  📧 alfredo@email.com                    │
│        🔗 linkedin.com/in/alfredo                                        │
│                                                                          │
│        ♥️ P2 Próximo   💼 R1 Core   ❤️ Health 100%                       │
│        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%                           │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  🧠 INTEL INSIGHT                                                  │  │
│  │                                                                    │  │
│  │  Alfredo retomou contato hoje após 2 meses de silêncio.           │  │
│  │                                                                    │  │
│  │  Contexto da mensagem:                                            │  │
│  │  • Pediu desculpas por sumir                                      │  │
│  │  • Mencionou "solução interna" - possível projeto?                │  │
│  │  • Quer marcar conversa                                           │  │
│  │                                                                    │  │
│  │  💡 Recomendação: Responder com entusiasmo e propor encontro      │  │
│  │     presencial. Vocês costumam almoçar juntos.                    │  │
│  │                                                                    │  │
│  │  [💬 Responder] [📅 Propor Almoço] [📝 Criar Tarefa]              │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  💬 PERGUNTE SOBRE ALFREDO                                        │  │
│  │  ┌──────────────────────────────────────────────────────────────┐ │  │
│  │  │ Digite sua pergunta...                                    🎤 │ │  │
│  │  └──────────────────────────────────────────────────────────────┘ │  │
│  │                                                                    │  │
│  │  Sugestões:                                                       │  │
│  │  • "O que ele faz profissionalmente?"                             │  │
│  │  • "Quando nos encontramos pela última vez?"                      │  │
│  │  • "Temos projetos ou negócios em comum?"                         │  │
│  │  • "Qual o histórico do nosso relacionamento?"                    │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│  [📋 Resumo] [💬 Conversas] [📅 Encontros] [📁 Projetos] [📝 Notas]     │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  CONTEÚDO DA TAB SELECIONADA...                                         │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Funcionalidades Detalhadas

### 1. INTEL Insight (Análise Contextual Automática)

O sistema analisa automaticamente:

| Fonte | O que extrai |
|-------|--------------|
| Mensagens WhatsApp | Tom, intenções, pedidos, menções a projetos |
| Emails | Assuntos discutidos, compromissos, anexos importantes |
| Calendário | Frequência de encontros, tipos de reunião |
| Fatos registrados | Informações pessoais, preferências, histórico |
| Projetos | Colaborações, deals, oportunidades |
| LinkedIn | Cargo atual, empresa, conexões em comum |

**Output do Insight:**
```json
{
  "situacao_atual": "Retomando contato após período de distanciamento",
  "contexto_mensagem": [
    "Pediu desculpas por sumir",
    "Mencionou solução interna - possível oportunidade",
    "Quer marcar conversa"
  ],
  "recomendacao": "Responder com entusiasmo, propor encontro presencial",
  "base_recomendacao": "Histórico mostra que vocês preferem almoços presenciais",
  "acoes_sugeridas": [
    {"tipo": "responder", "prioridade": "alta", "sugestao_texto": "Alfredo! Que bom..."},
    {"tipo": "agendar", "prioridade": "media", "sugestao": "Almoço quinta ou sexta"},
    {"tipo": "tarefa", "prioridade": "baixa", "sugestao": "Follow-up em 3 dias"}
  ]
}
```

---

### 2. Chat com IA sobre o Contato

Interface de chat onde o usuário pode perguntar qualquer coisa sobre o relacionamento.

**Exemplos de perguntas e respostas:**

```
Você: "O que o Alfredo faz?"

IA: "Baseado nas informações disponíveis, Alfredo é Sócio-Diretor
da TechCorp, empresa de tecnologia. Não temos mais detalhes sobre
seu cargo atual.
[🔍 Buscar no LinkedIn]"

---

Você: "Quando foi nosso último encontro presencial?"

IA: "Encontrei 3 eventos com Alfredo no calendário:
• 15/12/2025 - Almoço no Fasano (2h)
• 03/11/2025 - Reunião na TechCorp (1h)
• 20/09/2025 - Happy Hour com time (3h)

O último encontro presencial foi há 3.5 meses."

---

Você: "Temos algum negócio em andamento?"

IA: "Não encontrei projetos ativos com Alfredo.
Porém, na mensagem de hoje ele mencionou 'solução interna'
- pode ser uma oportunidade.

Sugestão: Perguntar mais sobre isso na conversa.
[📝 Criar nota sobre possível oportunidade]"

---

Você: "Escreva uma resposta para ele"

IA: "Baseado no tom das conversas anteriores e no contexto:

'Alfredo! Que bom ter notícias suas!
Sem problemas, a vida corrida pega todos nós.

Fiquei curioso sobre essa solução interna que você
mencionou - conta mais!

Que tal um almoço essa semana para colocar o papo em dia?
Quinta ou sexta funcionam para você?

Abraço!'

[📋 Copiar] [📱 Enviar via WhatsApp] [✏️ Editar]"
```

---

### 3. Memória de Relacionamento

O sistema aprende e mantém uma "memória" que evolui:

```yaml
Relacionamento com Alfredo Villela:
  origem: "Amigos de infância, mesma escola"
  duracao: "~15 anos"

  contextos:
    pessoal:
      circulo: P2 (Próximo)
      frequencia_ideal: "Mensal"
      tipo_interacao_preferida: "Almoços presenciais"

    profissional:
      circulo: R1 (Core)
      empresa: "TechCorp"
      relacao: "Potencial parceiro de negócios"

  padroes_aprendidos:
    - "Prefere almoços a calls"
    - "Responde melhor no WhatsApp que email"
    - "Costuma sumir por períodos mas sempre volta"
    - "Interessado em soluções de tecnologia"

  historico_resumido:
    - "2024: Discutimos parceria que não avançou"
    - "2023: Ele indicou 2 clientes"
    - "2022: Viagem juntos para SP"

  alertas:
    - "Último contato foi há 2 meses (acima do normal)"
    - "Perfil incompleto: falta LinkedIn e email"
```

---

### 4. Ações Inteligentes

As ações não são genéricas - são contextualizadas:

| Situação | Ação Genérica | Ação Inteligente |
|----------|---------------|------------------|
| Mensagem recebida | "Responder" | "Responder com entusiasmo e propor almoço (vocês preferem presencial)" |
| Tempo sem contato | "Enviar mensagem" | "Fazer check-in - perguntar sobre o projeto que ele mencionou em dezembro" |
| Aniversário | "Enviar parabéns" | "Ligar para dar parabéns (você ligou nos últimos 3 anos)" |
| Perfil incompleto | "Completar dados" | "Buscar LinkedIn - ele trabalha na TechCorp" |

---

### 5. Aprendizado Contínuo

O sistema aprende com cada interação:

| Evento | O que aprende |
|--------|---------------|
| Usuário responde rápido | "Relacionamento prioritário" |
| Usuário agenda almoço (não call) | "Preferência por presencial" |
| Usuário adiciona fato | Enriquece memória do relacionamento |
| Usuário ignora sugestão | Ajusta relevância de sugestões similares |
| Usuário edita texto sugerido | Aprende estilo de comunicação |

---

## Arquitetura Técnica

### Novo Serviço: `ContactIntelligence`

```python
class ContactIntelligence:
    """Inteligência artificial para análise de relacionamentos."""

    async def get_insight(self, contact_id: int) -> dict:
        """Gera insight contextual sobre o contato."""
        # Coleta dados de todas as fontes
        context = await self._gather_context(contact_id)

        # Gera análise com Claude
        insight = await self._generate_insight(context)

        return insight

    async def chat(self, contact_id: int, question: str) -> str:
        """Responde perguntas sobre o contato."""
        context = await self._gather_context(contact_id)

        response = await self._chat_with_context(context, question)

        # Salva interação para aprendizado
        await self._log_interaction(contact_id, question, response)

        return response

    async def suggest_response(self, contact_id: int, message: str) -> str:
        """Sugere resposta baseada no contexto."""
        context = await self._gather_context(contact_id)
        history = await self._get_communication_style(contact_id)

        return await self._generate_response(context, history, message)

    async def _gather_context(self, contact_id: int) -> dict:
        """Coleta todo o contexto disponível."""
        return {
            "contact": await self._get_contact_data(contact_id),
            "messages": await self._get_recent_messages(contact_id, limit=50),
            "emails": await self._get_recent_emails(contact_id, limit=20),
            "events": await self._get_calendar_events(contact_id),
            "facts": await self._get_facts(contact_id),
            "projects": await self._get_projects(contact_id),
            "tasks": await self._get_tasks(contact_id),
            "relationship_memory": await self._get_memory(contact_id)
        }
```

### Novos Endpoints

```
GET  /api/contacts/{id}/intelligence/insight
     → Retorna insight contextual atual

POST /api/contacts/{id}/intelligence/chat
     → Processa pergunta e retorna resposta
     Body: { "question": "..." }

POST /api/contacts/{id}/intelligence/suggest-response
     → Sugere resposta para última mensagem

POST /api/contacts/{id}/intelligence/learn
     → Registra feedback para aprendizado
     Body: { "action": "...", "feedback": "..." }
```

### Tabelas Novas

```sql
-- Memória de relacionamento
CREATE TABLE relationship_memory (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    memory_type TEXT, -- 'pattern', 'preference', 'history', 'insight'
    content JSONB,
    confidence FLOAT DEFAULT 0.5,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Histórico de chat com IA
CREATE TABLE contact_ai_chat (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    question TEXT,
    answer TEXT,
    context_used JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Feedback para aprendizado
CREATE TABLE ai_feedback (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER,
    action_type TEXT,
    suggestion TEXT,
    user_action TEXT, -- 'accepted', 'modified', 'rejected'
    modification TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## Fluxo de Uso

### Cenário: Alfredo manda mensagem

```
1. Mensagem chega via WhatsApp
   ↓
2. Sistema detecta: contato importante (P2+R1), sem resposta há 2 meses
   ↓
3. Ação Sugerida aparece no Dashboard: "Responder Alfredo"
   ↓
4. Usuário clica → Vai para página do contato
   ↓
5. INTEL Insight mostra:
   - Contexto da mensagem (ele quer reconectar)
   - Recomendação (responder com entusiasmo, propor almoço)
   - Ações: [Responder] [Propor Almoço] [Criar Tarefa]
   ↓
6. Usuário clica "Responder"
   ↓
7. IA sugere texto baseado no histórico e tom das conversas
   ↓
8. Usuário edita/aprova e envia
   ↓
9. Sistema aprende: resposta rápida, texto modificado
   ↓
10. Ação Sugerida desaparece (só após resposta real)
```

---

## Fases de Implementação

### Fase 1: Fundação (1 semana)
- [ ] Corrigir bug "Responder agora"
- [ ] Criar serviço `ContactIntelligence` básico
- [ ] Endpoint de insight (análise simples)
- [ ] Layout novo da página com painel de insight

### Fase 2: Chat com IA (1 semana)
- [ ] Interface de chat na página
- [ ] Endpoint de chat com contexto
- [ ] Perguntas sugeridas baseadas no contexto
- [ ] Histórico de perguntas/respostas

### Fase 3: Ações Inteligentes (1 semana)
- [ ] Sugestão de resposta contextualizada
- [ ] Ações com contexto (não genéricas)
- [ ] Integração com envio de WhatsApp
- [ ] Criação de tarefas/eventos com contexto

### Fase 4: Aprendizado (1 semana)
- [ ] Tabela de memória de relacionamento
- [ ] Registro de feedback
- [ ] Ajuste de sugestões baseado em histórico
- [ ] Padrões aprendidos por contato

---

## Custos Estimados

| Operação | Tokens (aprox) | Custo por uso |
|----------|----------------|---------------|
| Gerar insight | ~2000 input + ~500 output | ~$0.01 |
| Chat pergunta | ~3000 input + ~300 output | ~$0.01 |
| Sugerir resposta | ~2000 input + ~200 output | ~$0.008 |

**Estimativa mensal (uso intenso):**
- 100 insights/dia × 30 dias = 3000 × $0.01 = $30
- 50 chats/dia × 30 dias = 1500 × $0.01 = $15
- Total: ~$45-60/mês

---

## Decisões Necessárias

1. **Começar por qual fase?**
   - Recomendo Fase 1 (fundação) para ter algo funcionando rápido

2. **Chat com IA: modal ou inline?**
   - Modal: mais espaço, foco na conversa
   - Inline: mais rápido, menos fricção

3. **Onde armazenar memória?**
   - Banco local (PostgreSQL) - mais controle
   - Combinar com resumos no campo `resumo_ai` existente

4. **Limite de contexto para IA?**
   - Últimas 50 mensagens? 100?
   - Últimos 6 meses? 1 ano?

---

**Aguardando validação para iniciar implementação.**
