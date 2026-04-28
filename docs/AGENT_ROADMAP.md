# INTEL Agent — Roadmap para Inteligência Autônoma

> De bot conversacional para agente autônomo que pensa, executa e pergunta.

---

## Fase 1 — Agente Poderoso (Sprint atual)
**Objetivo:** Bot capaz de executar tarefas complexas multi-step.

### 1.1 Expandir tools do worker
- [ ] **Web search** — pesquisar na internet (Brave API, gratuito)
- [ ] **URL fetch + resumo** — buscar qualquer URL e resumir
- [ ] **Python execution** — rodar scripts de análise no Railway
- [ ] **File generation** — gerar relatórios, CSVs, PDFs

### 1.2 Aumentar capacidade
- [ ] **15 iterações** (hoje: 3) — permite raciocínio complexo
- [ ] **Detecção de complexidade** — bot decide se responde direto ou delega ao agente
- [ ] **Feedback em tempo real** — "Analisando..." a cada 30s durante tarefas longas
- [ ] **Plano antes de executar** — agente mostra o plano e pede OK antes de agir

### 1.3 Email management (em andamento)
- [x] Email digest diário
- [x] Arquivar não-urgentes via bot
- [ ] Responder emails via bot ("responda ao email do João agradecendo")
- [ ] Encaminhar emails ("encaminhe isso pro Amadeo")

---

## Fase 2 — Proatividade por Triggers
**Objetivo:** Sistema reage automaticamente a eventos, pensa, e age ou pergunta.

### 2.1 Triggers de entrada
| Trigger | Detecção | Ação |
|---------|----------|------|
| **Email recebido** | Gmail webhook / polling | Classifica, sugere resposta, cria tarefa |
| **WhatsApp recebido** | Evolution webhook | Analisa contexto, sugere ação, alerta se urgente |
| **Reunião terminou** | Calendar event ended | Pergunta "como foi?", sugere registrar notas |
| **Tarefa venceu** | Cron check | Alerta + sugere reagendar ou delegar |
| **Contato esfriando** | Health score drop | Sugere reconexão com gancho personalizado |
| **Mudança LinkedIn** | Enrichment cron | Notifica + sugere mensagem de parabéns |
| **Artigo relevante** | News clipping match | Sugere compartilhar com stakeholders |
| **Grupo WA ativo** | Message sync | Resumo + alerta se mencionado |

### 2.2 Pipeline de processamento
```
Trigger detectado
    ↓
Agente analisa contexto (quem, o quê, projeto, histórico)
    ↓
Decide: agir automaticamente OU perguntar ao Renato
    ↓
Se automático: executa + confirma via WhatsApp
Se precisa input: pergunta via WhatsApp com opções
    ↓
Renato responde (texto, áudio, foto)
    ↓
Agente executa e confirma
```

### 2.3 Regras de autonomia (níveis)
| Nível | Ação | Exemplo |
|-------|------|---------|
| **Auto** | Executa sem perguntar | Arquivar newsletter, salvar memória de ligação |
| **Notifica** | Faz e avisa | Criou tarefa de follow-up, atualizou contato |
| **Sugere** | Propõe e espera OK | "Quer que eu responda ao João?" |
| **Pergunta** | Pede input | "Como foi a reunião com Thalita?" |

---

## Fase 3 — Agente Estratégico
**Objetivo:** Conecta pontos que humanos não veem. Antecipa necessidades.

### 3.1 Análise de padrões
- [ ] **Timing ótimo** — "Você costuma fechar deals em março. Pipeline está vazio."
- [ ] **Relação decaying** — "3 contatos C1 sem interação há 20 dias. Historicamente isso leva a perda."
- [ ] **Sentiment shift** — "Tom das mensagens do João mudou — 3 últimas foram curtas e formais."
- [ ] **Network gaps** — "Você não tem nenhum contato no setor de energia. 3 projetos recentes tocam isso."

### 3.2 Briefings proativos (sem pedir)
- [ ] **Pré-reunião** — context pack completo enviado automaticamente
- [ ] **Pós-reunião** — "Notei que a reunião com Alba acabou. Quer registrar decisões?"
- [ ] **Domingo à noite** — "Sua semana: 8 reuniões, 4 tarefas vencidas, 2 contatos precisam atenção"
- [ ] **Alertas urgentes** — "Rodrigo Pretola mudou de empresa! Reconectar agora?"

### 3.3 Execução autônoma com supervisão
- [ ] **Auto-draft respostas** — bot prepara rascunho, Renato aprova com "ok" ou edita
- [ ] **Auto-follow-up** — sem resposta em N dias, bot envia follow-up gradual
- [ ] **Auto-agendar** — detecta proposta de reunião em email/WA, cria evento
- [ ] **Auto-categorizar** — mensagem chega, vincula a projeto, classifica prioridade

---

## Fase 4 — Multi-agente
**Objetivo:** Agentes especializados trabalhando em paralelo.

### 4.1 Agentes especializados
| Agente | Responsabilidade |
|--------|-----------------|
| **Relationship Agent** | Monitora saúde dos relacionamentos, sugere reconexões |
| **Project Agent** | Acompanha projetos, detecta riscos, sugere ações |
| **Content Agent** | Curadoria de conteúdo, scheduling, métricas |
| **Finance Agent** | Cobranças, pagamentos, despesas |
| **Calendar Agent** | Otimiza agenda, detecta conflitos, prepara reuniões |

### 4.2 Orquestrador
- [ ] Agent central recebe mensagem → delega ao agente certo
- [ ] Agentes se comunicam entre si ("Relationship Agent detectou oportunidade → Project Agent cria tarefa")
- [ ] Dashboard de agentes no INTEL (status, ações tomadas, pendências)

---

## Infraestrutura necessária

### Fase 1 (Railway worker expandido)
- Mais tools no worker existente
- Brave API key (web search, gratuito)
- Aumento de iterações para 15

### Fase 2 (Event-driven)
- Gmail push notifications (webhook) OU polling a cada 5min
- Evolution API webhook já funciona
- Calendar API push notifications
- Fila de eventos (pode ser tabela PostgreSQL simples)

### Fase 3 (Analysis engine)
- Cron jobs de análise (rodam no Railway)
- Tabela `agent_insights` para armazenar descobertas
- Sistema de "urgência" para decidir quando alertar

### Fase 4 (Multi-agent)
- Anthropic Agent SDK ou framework custom
- Fila de mensagens entre agentes
- Dashboard de monitoramento

---

## Prioridade de Implementação

```
Semana 1 (agora):
  ✅ Email digest + archive
  → Web search no bot
  → URL fetch no bot  
  → Aumentar iterações para 15

Semana 2:
  → Gmail polling (emails novos a cada 5min)
  → Auto-classificar emails recebidos
  → Post-call automático (Calendar trigger)
  → Auto-draft de respostas simples

Semana 3:
  → Pipeline de triggers completo
  → Níveis de autonomia configuráveis
  → Agente estratégico (análise de padrões)

Semana 4:
  → Multi-agente (Relationship + Project)
  → Dashboard de agentes
  → Refinamento baseado em uso real
```
