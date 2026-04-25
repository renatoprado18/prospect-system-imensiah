# INTEL — Intelligence Roadmap

> Backlog dedicado para tornar o INTEL genuinamente inteligente.
> Organizado por nível de sofisticação.

---

## Nível 1 — Multimodal (input além de texto)

### Screenshots via WhatsApp
- [ ] Bot recebe imagens → Claude Vision analisa → age
- [ ] Foto de mensagem LinkedIn → identifica contato, sugere resposta
- [ ] Foto de cartão de visita → cria/atualiza contato
- [ ] Screenshot de email → analisa, cria tarefa, sugere follow-up
- [ ] Foto de documento/contrato → OCR + resume + salva no projeto
- [ ] Screenshot de bug/UI → salva como feedback do sistema

### Feedback Loop via WhatsApp
- [ ] Canal de feedback: "feedback: ..." salva para próxima sessão Claude Code
- [ ] Screenshots de melhorias: foto + comentário → feedback estruturado
- [ ] Tabela `system_feedback` consultada no início de cada sessão

---

## Nível 2 — Proativo (age sem pedir)

### Pré-reunião
- [ ] 1h antes de cada meeting: briefing do contato via WhatsApp
  - Últimas conversas, pendências, fatos pessoais, LinkedIn recente
  - Sugestão de pauta baseada em tarefas do projeto

### Detecção de Anomalias
- [ ] Silêncio incomum: "Fulano não respondeu há 3 dias, historicamente responde em 1"
- [ ] Pico de atividade: "Grupo ACFICTOR teve 50 mensagens hoje — algo aconteceu"
- [ ] Mudança de tom: detectar se relação está esfriando

### Digest de Grupos
- [ ] Resumo diário dos grupos WA com sync ativo
- [ ] Extrair decisões, pendências, menções a Renato

### Post-call Automático
- [ ] Detectar fim de evento no Calendar → perguntar "Como foi a reunião?"
- [ ] Criar nota/memória com base na resposta

---

## Nível 3 — Preditivo (conecta pontos)

### Relationship Intelligence
- [ ] Decay prediction: "Se não falar com X em 5 dias, health cai para 40%"
- [ ] Sentiment tracking: analisar tom das mensagens ao longo do tempo
- [ ] Mapa de influência: conectores entre grupos/projetos
- [ ] Oportunidade cruzada: "A precisa de X, B oferece X — apresente"

### Pattern Recognition
- [ ] Timing de negócios: "Você fecha deals em Q1, prepare pipeline agora"
- [ ] Perfil de resposta: tempo médio de resposta por contato/círculo
- [ ] Correlação atividade × resultados: o que funciona?

### Network Analysis
- [ ] Quem você deveria conhecer mas não conhece (2nd degree)
- [ ] Gaps na rede: setores/empresas sem cobertura
- [ ] Cluster analysis: subgrupos naturais na sua rede

---

## Nível 4 — Autônomo (age com mínima supervisão)

### Auto-draft
- [ ] Mensagens rotineiras: agradecimento, confirmação, follow-up
- [ ] Bot prepara rascunho → você aprova com "ok" → envia

### Auto-follow-up
- [ ] Sem resposta em N dias → bot sugere/envia follow-up
- [ ] Escalonamento: 1º lembrete suave, 2º direto, 3º alerta

### Auto-categorize
- [ ] Mensagem chega → classifica prioridade automaticamente
- [ ] Vincula a projeto relevante
- [ ] Sugere ação sem pedir

### Auto-enrich
- [ ] Contato novo detectado → enriquece automaticamente
- [ ] Mudança de emprego → atualiza + notifica + sugere reconexão

---

## Prioridade de Implementação

```
Sprint 1 (próxima sessão):
  - Screenshots via WhatsApp (Claude Vision)
  - Feedback loop via WhatsApp
  - Melhorar "Quem Contatar Hoje"

Sprint 2:
  - Pré-reunião automática
  - Digest de grupos diário
  - Post-call automático

Sprint 3:
  - Decay prediction
  - Sentiment tracking
  - Auto-draft com aprovação

Sprint 4:
  - Mapa de influência
  - Network analysis
  - Auto-follow-up
```
