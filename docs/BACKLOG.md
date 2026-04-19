# INTEL - Backlog

## Prioridade Alta

### Grupos Sociais (WhatsApp como mapa de relacionamentos)
**Conceito**: Usar grupos de WhatsApp como fonte de verdade para mapear círculos sociais. Extrair participantes, cruzar com contatos INTEL, identificar interesses mútuos e oportunidades de conexão.

**Grupos identificados**:
- CAP: CHARUTO CAP (50), Judô CAP (20), CAP Travessias (226), Véios do CAP (15), Primos Sauna (3)
- Profissional: Assespro (4 grupos), Board Academy (3), Alba Consultoria, ImensIAH
- CELINT: Alumni CELINT SP (361), Clube do Charuto 24/10 (26)
- Fictor: ACFICTOR (2 grupos) — já vinculados ao projeto

**Casos de uso**:
- Reconexão com pretexto (interesses mútuos)
- Introduções estratégicas (cruzar membros entre grupos)
- Mapear influência (quem participa de mais grupos em comum)
- Networking pré-evento (quem do grupo precisa atenção)

**Implementação**:
- [ ] Extrair participantes dos grupos via Evolution API
- [ ] Cruzar telefones com contatos INTEL
- [ ] Página "Meus Grupos" com membros, health médio, sugestões
- [ ] Cruzamento entre grupos (quem está em A e B)
- [ ] Sugestão de introduções baseada em interesses

### Download docs grupos WhatsApp → Google Drive
- [x] Endpoint e botão implementados
- [ ] Testar em produção com docs dos grupos ACFICTOR
- [ ] Tratar imagens com caption (não só PDFs)

---

## Prioridade Média

### Contexto Persistente Avançado
- [x] Pareceres alimentam próximo parecer
- [x] Smart Update usa memória de pareceres
- [ ] Resumo acumulativo por projeto (condensar 10 pareceres em 1 resumo)
- [ ] "Assistente dedicado" por projeto com personalidade/contexto fixo

### Enriquecimento Contínuo
- [x] Auto-enrich C1-C2 no cron
- [x] Busca fotos WhatsApp no cron
- [ ] Enriquecer contatos C3 com LinkedIn (LinkdAPI)
- [ ] Detectar mudanças de emprego via LinkedIn

### Editorial Calendar
- [x] Artigos para reconexão na página do contato
- [ ] Sugerir artigo específico baseado no perfil do contato (IA)
- [ ] LinkedIn agendamento direto via API

---

## Prioridade Baixa

### UX
- [x] Mobile responsividade (base, dashboard, contato, projeto, contatos)
- [ ] Dark mode
- [ ] PWA com notificações push em mobile
- [ ] Atalhos de teclado (além do Cmd+K)

### Integrações
- [ ] Instagram suporte (carrossel, reels)
- [ ] Sync com ConselhoOS mais profundo (tarefas RACI no dashboard)
- [ ] Import de contatos via LinkedIn CSV

### Infra
- [ ] WhatsApp sync mais robusto (Evolution API lenta/instável)
- [ ] Cache de queries pesadas (dashboard, circles)
- [ ] Logs de auditoria (quem fez o quê)
