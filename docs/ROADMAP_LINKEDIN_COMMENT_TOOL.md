# Roadmap — Ferramenta de Comentários LinkedIn

> Sessão de 12/05/2026. Origem: rascunhos manuais de comentários nesta sessão (Allan Gotsis/Synapse, Custódio Pereira/Lar São Francisco) expuseram que o processo é repetível e vale ser ferramentalizado. Renato pediu que a tool olhe tarefas "Curtir post de X" em background, faça curadoria, e só traga pra atenção os que valem.

## Objetivo

Reduzir o tempo decisório de "ver tarefa de curtir → ler post → julgar relevância → redigir comentário" de ~7 min/tarefa pra ~2 min/tarefa, e cortar tarefas baixas (curtir-basta ou dispensar) sem fadiga decisória.

## Princípios

1. **Curtir é cheap, comentar é valioso** — sistema preserva ambas as ações; só investe Claude/atenção em curadoria pros que merecem comentário
2. **Pipeline preempção** — análise roda em BG antes do horário de revisão, drafts aparecem prontos
3. **Threshold ajustável** — começa restritivo (≥7), calibra com feedback de uso
4. **Híbrido Sonnet+Opus** — Sonnet pra scoring rápido/barato, Opus só pra geração de draft dos posts qualificados
5. **Schema reaproveitado** — `linkedin_task_data` já provisionada (descoberta 12/05/2026); estende ao invés de criar nova
6. **Loop com outbound existente** — botão "Publiquei" registra em `linkedin_outbound_engagements` e dispara cron de monitor (Fase 1.5 já deployada)

## Estado descoberto (12/05/2026)

- ✅ Tabela `linkedin_task_data` provisionada (10 colunas, índices, FK)
- ❌ Tabela vazia (0 linhas)
- ❌ Nenhum service ou cron popula
- ✅ ~9 tarefas "LinkedIn: Curtir post de X" pendentes em produção (12/05)
- ✅ LinkdAPI integration funcionando (`/api/v1/posts/info`, ~5 créds/call)
- ✅ Sidecar pattern em uso (memory `project_campaigns.md`)
- ✅ `linkedin_outbound_engagements` ativa + cron de monitor de replies (Fase 1.5)
- ✅ Framework de comentários consolidado em memory `reference_linkedin_comment_framework.md`

---

## P1 — MVP completo (próximas 2-3 semanas)

### 1.1 Estender schema `linkedin_task_data`

```sql
ALTER TABLE linkedin_task_data
  ADD COLUMN score_numeric INT,
  ADD COLUMN draft_a TEXT,
  ADD COLUMN draft_b TEXT,
  ADD COLUMN draft_dm TEXT,
  ADD COLUMN draft_recommended TEXT,   -- 'A' / 'B'
  ADD COLUMN published BOOLEAN DEFAULT FALSE,
  ADD COLUMN published_version TEXT,    -- 'A' / 'B' / 'custom'
  ADD COLUMN published_text TEXT,
  ADD COLUMN published_at TIMESTAMP,
  ADD COLUMN outbound_engagement_id INT REFERENCES linkedin_outbound_engagements(id),
  ADD COLUMN dm_followup_task_id INT REFERENCES tasks(id);
```

Mantém os campos existentes (`ai_verdict`, `ai_rationale`, `ai_angle`) — o veredito qualitativo continua útil pra UI; o score numérico facilita filtros/ordenação.

### 1.2 Service `linkedin_comment_curator.py` (novo)

Pipeline:
1. `fetch_pending_linkedin_tasks()` — lista tasks com `titulo LIKE 'LinkedIn: Curtir post de%'` AND `status='pending'` AND sem entry em `linkedin_task_data` (ou com `ai_ran_at < NOW() - 24h`)
2. Pra cada task:
   - Extrai URL do post da `descricao` ou `tags->>'post_url'` (verificar onde tasks de campanhas armazenam — provavelmente em metadata; documentar e fixar convenção)
   - Chama LinkdAPI `/api/v1/posts/info?urn=<urn>` — cacheia `post_text`, `author_name`, `author_headline`, `post_engagements`, `post_posted_at`
   - **Scoring (Sonnet)**: input = post + autor + memory (CV + framework + ICP) + dossie_linkedin do autor se for contact; output JSON `{score: 1-10, vale_comentar: bool, angulo_recomendado, razao_md, ai_verdict, ai_angle}`
   - Se `score >= 7`: **Geração de drafts (Opus)** — gera draft_a + draft_b + draft_dm + draft_recommended
   - Persiste tudo em `linkedin_task_data`
3. Rate limiting: 30 req/min LinkdAPI; agrupar em batches de 5

### 1.3 Cron diário 7h BRT

`vercel.json` cron `0 10 * * *` (UTC) → endpoint `/api/cron/linkedin-curator` → invoca `linkedin_comment_curator.run_daily()`.

Idempotente. Reanálise manual via botão na UI.

### 1.4 UI nas tarefas (rota `/tarefas-pendentes`)

Cada item LinkedIn ganha:
- **Badge de score:** 🟢 ≥7 / 🟡 5-6 / ⚪ <5
- **Linha resumo** com tema + ângulo recomendado
- **Click expande inline**: post excerpt + drafts A/B + DM
- **Botões inline:**
  - `Copiar A` / `Copiar B`
  - `✓ Publiquei` (modal pede URL do comentário pra capturar URN → registra em `linkedin_outbound_engagements` → cria task D+2 com DM rascunho)
  - `Dispensar tarefa` (marca task como `completed` com nota "dispensada via curator")

### 1.5 Dashboard pill (sem WhatsApp)

Cobertura passiva — sem push: o dashboard ganha um pill no header tipo:

> 🟢 LinkedIn · 3 posts altamente alinhados aguardando comentário

Click no pill leva pra `/tarefas-pendentes` filtrado por score ≥ 7. Pill some quando count = 0.

Alinhado com a filosofia de notificações do sistema (memory `feedback_notifications.md`): notificar só quando precisa de ação manual urgente; cobertura passiva via dashboard pill diário.

Implementação: 1 query no endpoint do dashboard somando `linkedin_task_data.score_numeric >= 7 AND linkedin_task_data.published = false AND tasks.status = 'pending'`.

### 1.6 Rota `/linkedin/comentar` (web only — análise ad-hoc)

Pra posts que **não vieram via tarefa** (você descobriu por outro caminho). Form: paste URL ou texto + autor opcional. Mesmo pipeline do curator BG, executado on-demand. Resultado salvo em `linkedin_task_data` com `task_id=NULL` (campo já é nullable? ajustar se não for) OU em tabela auxiliar `linkedin_adhoc_drafts` espelhando o schema.

**Decisão:** começar com tabela auxiliar `linkedin_adhoc_drafts` (mesmo schema, sem FK pra tasks) — evita poluir `linkedin_task_data` com entries sem task associada.

### 1.7 Configurações

- `LINKEDIN_CURATOR_SCORE_THRESHOLD=7` (env, default 7)
- `LINKEDIN_CURATOR_MODEL_SCORING=claude-sonnet-4-6`
- `LINKEDIN_CURATOR_MODEL_DRAFT=claude-opus-4-7`
- `LINKEDIN_CURATOR_MAX_DAILY=15` (cap diário de análise pra controlar custo)
- (sem `NOTIFY_HOUR` — notificação é passiva via pill no dashboard)

### Custo estimado (10 tasks analisadas/dia)

Verificado contra consumo real de LinkdAPI (12/05/2026): ~2 créds/call média em produção (saldo 2.38k → 2.12k em 5 dias com ~27 calls/dia).

| Componente | Cálculo | Custo/mês |
|---|---|---|
| LinkdAPI (`posts/info` × 10/dia) | 600 créds × $1/120 | **~$5** |
| Sonnet scoring (10/dia × $0,01) | $0,01 × 300 | **~$3** |
| Opus drafts (só score ≥7, ~3/dia × $0,10) | $0,10 × 90 | **~$9** |
| **Total híbrido** | | **~$17/mês** |
| Alternativa: Sonnet para drafts também | $0,03 × 90 | $3 (vs $9) → **total ~$11/mês** |

**Saldo LinkdAPI atual (2.12k créds, 12/05) cobre ~3,5-4 meses de curator sem refill.** Primeiros meses custam só Claude (~$11-12/mês). Refill avulso depois conforme uso.

Custo se BG curator dispara também a tool `/linkedin/comentar` ad-hoc: +1-3 calls/dia (variável conforme uso ad-hoc).

---

## P2 — Calibração e expansão (próximos 2 meses)

### 2.1 Loop de feedback estruturado

Adicionar em `linkedin_task_data`:
- `gerou_resposta_autor BOOLEAN` (preenche cron `linkedin-outbound-check` existente)
- `gerou_dm_aberta BOOLEAN` (preenche manual quando você manda DM follow-up)
- `gerou_demo BOOLEAN` (preenche manual quando vira demo imensIAH)

Com ~30 entries publicadas, virar análise: "drafts A geraram X% de reply vs B com Y%". Alimenta calibragem do scoring.

### 2.2 Calibragem do threshold

Após 1-2 semanas:
- Quantos posts/dia caíram em cada faixa?
- Quantos 🟡 acabaram virando comentário valioso (você overrode o threshold)?
- Quantos 🟢 foram dispensados (false positive)?

Ajustar threshold conforme. Provavelmente 6-7 é o sweet spot dependendo do mix de tasks.

### 2.3 Detecção de "post sensível" mais robusta

Filtros adicionais no scoring:
- Política partidária explícita
- Crise pessoal (luto, doença)
- Religião
- Conteúdo controverso
- Self-promotion sem substância (anúncio puro de produto)

Esses sempre score baixo independente do tema.

### 2.4 Dossier do autor enriquecido no draft

Se `author.linkedin_url` corresponder a um `contacts.linkedin`:
- Puxar `dossie_linkedin` cacheado (Fase 3 já deployada)
- Injetar no prompt do Opus: "Renato e você (autor) já tiveram contato em X. Conexão de 1º grau. Ele atuou em [empresas/cargos]. Ponto de paridade: [tópico]."
- Drafts ficam mais pessoais sem virar bajulação

---

## P3 — Médio prazo

### 3.1 Bookmarklet de captura

Quando você publicar o comentário no LinkedIn, bookmarklet captura URN do comment + URL do post automaticamente, posta em `/api/linkedin/published` → registra outbound sem precisar colar a URL do comment manualmente.

### 3.2 Auto-likes em massa pra ⚪

Tasks score <5 que viraram "curtir basta": botão "Curtir todas 5" que abre cada post em nova aba pra você bater o 👍 (não há API pra curtir programaticamente, mas o batch abrir reduz fricção).

Alternativa: marca todas como `completed` com nota "curtido manualmente" sem abrir nada — assumindo que "curtir" é commodity e não precisa rastrear individual.

### 3.3 Sugestão proativa de tarefas LinkedIn

Hoje as tarefas "Curtir post de X" são criadas externamente (via campanha, manual, sync de notificação?). Investigar fonte e ver se vale alimentar com **posts descobertos pela Fase 1 (monitor de tema)** quando autor já está nos círculos de proximidade.

---

## Decisões abertas

1. ✅ **Onde a URL do post está na task hoje?** Confirmado 12/05/2026 — URL está em `tasks.descricao` no formato:
   ```
   Curta este post de [Autor]:
   "[trecho do post entre aspas]"
   Abrir post: https://www.linkedin.com/feed/update/urn:li:activity:XXX
   ```
   Service extrai via regex `r'Abrir post:\s*(https://www\.linkedin\.com/feed/update/urn:li:activity:\d+)'`. Campo `tags` vem `[]` — não usado pra URL.

2. **Fonte das tasks LinkedIn** — quem as cria hoje? Manual, campanha, cron? Documentar pra que o curator possa filtrar (não analisar tasks fakes/teste).

3. **`linkedin_task_data` com `task_id` NOT NULL** — schema atual exige task. Pra ad-hoc (rota `/linkedin/comentar`), criar tabela espelho `linkedin_adhoc_drafts`. Confirmado nesta sessão.

---

## Mapa de integrações com features existentes

| Já existe | Como conecta |
|---|---|
| `linkedin_task_data` (schema) | Estende com 9 colunas; o curator popula |
| LinkdAPI ($10/mês, ~2.4k cred) | Endpoint `/posts/info` pra cada task analisada |
| `dossie_linkedin` cache 30d | Enriquece prompt do Opus se autor for contact |
| Fase 1.5 outbound monitor | Cron já existente captura replies sem alteração |
| `intel_bot.py` notify WA | Reusa pra resumo diário 8h BRT |
| Tasks recorrentes | D+2 DM follow-up gerada automaticamente |
| `reference_linkedin_comment_framework.md` | Prompt-fonte do Sonnet (scoring) e Opus (drafts) |
| `auto_publisher.py` pattern | Padrão de cron já estabelecido |

---

## Esforço estimado MVP

- 1.1 Schema migration: 30 min
- 1.2 Service curator: 4h (parsing URL + LinkdAPI + Sonnet scoring + Opus drafts)
- 1.3 Cron + endpoint: 1h
- 1.4 UI badges + expand inline + botões: 3h
- 1.5 Dashboard pill: 30 min
- 1.6 Rota `/linkedin/comentar` ad-hoc: 2h
- Testes + ajustes: 2h
- **Total: ~13h** (1.5-2 dias focados)

---

## Próximos passos imediatos

1. ✅ Confirmar fonte da URL na task (rodar query de inspeção)
2. ✅ Confirmar threshold inicial = 7 (Renato 12/05)
3. ✅ Confirmar híbrido Sonnet+Opus (Renato 12/05)
4. ⏳ Aplicar migration 1.1
5. ⏳ Implementar service curator
6. ⏳ Cron + endpoint + UI

---

*Documento criado em 12/05/2026 após sessão exploratória que produziu 2 rascunhos manuais (Allan Gotsis/Synapse + Custódio Pereira/Lar São Francisco). Decisões fechadas: web only, paste manual da URL, estrutura crua sem feedback ML inicial, P1 inclui BG curator desde dia 1.*
