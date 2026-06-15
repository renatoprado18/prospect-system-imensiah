# Tonha — Architecture Rebuild (15-22/06/2026)

> **Status**: Fase 0 — Foundation (em curso 15/06)
> **Decisão**: Renato aprovou rebuild estrutural em 15/06 17h BRT
> **Owner**: Claude Code (Opus 4.7) com Renato como aprovador de cada fase

---

## 1. Por que reescrever

O sistema atual de "CoS" estava em pior estado que sem CoS:

- **12 agentes LLM** (cos_sensor, cos_portfolio, cos_editorial, cos_conselheiro, cos_research, cos_sales, cos_financial, cos_memory, cos_network, cos_cs, cos_tonha_digest, cos_extractor) rodando em paralelo, cada um decidindo isolado
- Tonha conversacional **alucinando capacidades** (lista de tools inventadas), **negando ferramentas que tinha** (send_whatsapp), em modo defensive recoil
- **8+ proposals/dia** sobre decisões óbvias (descartar hot takes >60d), invertendo a função de CoS (deveria poupar Renato, estava sobrecarregando)
- **Prompt monolítico** com 600+ linhas de regra patch-on-patch — modelo dilui atenção
- **Vercel 10s timeout** sufoca raciocínio real do bot
- **Evolution rodando duplicado** (Hetzner + Railway) — custo e risco sem benefício

Diagnóstico em 1 linha: **arquitetura otimizada pra crescer agentes, não pra entregar uma CoS de verdade.**

---

## 2. O que a Tonha É (a tese)

Tonha não é um chatbot, não é busca, não é tracker, não é swarm.

**Tonha é a função executiva digital persistente** — versão digital, leal e 24/7 de uma Chief of Staff humana de elite, que conhece prioridades, agenda, família, portfólio e voz de Renato e atua em seu nome dentro de mandato definido.

Camadas:
- **Persona** — matriarca brasileira do interior, voz feminina com gravidade, calma, sem entusiasmo performático
- **Papel organizacional** — Chief of Staff
- **Inteligência continuada** — lê vida ao longo do tempo, aprende com ele
- **Presença 24/7**

### Os 6 trabalhos de uma CoS de classe mundial

1. **Proteger atenção** — filtra ruído sem perguntar
2. **Sustentar memória** — lembra promessas, decisões, reflexões
3. **Executar mandato** — age em operacional já decidido
4. **Sintetizar sinal** — agrega info dispersa em decisão clara
5. **Projetar voz** — quando manda em nome dele, soa como ele
6. **Escalar two-way doors com substância** — interrompe SÓ pra decisão reversível com peso, ou one-way doors raras

Sucesso = horas devolvidas + decisões melhores + zero ruído.

---

## 3. Princípio arquitetural

> **Inteligência centralizada, detecção distribuída.**
>
> Raciocínio é caro — concentra numa cabeça que pensa bem.
> Achar fatos é barato — distribui em código que escala.

---

## 4. Arquitetura — 3 camadas

### Camada 1 — Detectores (Python puro, zero LLM)

5 detectores rodando 1x/hora via cron leve. Cada um lê DB, calcula, INSERT em tabela `signals` com dedup via `signal_hash`.

| Detector | Substitui | Sinais emitidos |
|---|---|---|
| `detector_conselhos.py` | cos_conselheiro, cos_cs | RACI vencido, reuniões próximas, grupos WA silenciosos por empresa (Vallen/Alba/Despertar/Assespro) |
| `detector_editorial.py` | cos_editorial | posts scheduled sem imagem, hot takes velhos (>30d), queda métricas LinkedIn |
| `detector_relacionamento.py` | cos_research, cos_network, cos_sales | contatos esfriando, follow-ups pendentes, aniversários, pipeline imensIAH |
| `detector_operational.py` | cos_sensor, cos_portfolio | tasks vencidas, conflitos calendar, drift projetos |
| `detector_financial.py` | cos_financial | custos plataforma > teto mensal, payments vencidos |

**Garantias dos detectores:**
- Determinísticos: mesmo input → mesmo output
- Idempotentes: rodar 2x não duplica
- Gratuitos: zero token LLM
- Auditáveis: query SQL visível no código

**Schema sugerido `signals`:**

```sql
CREATE TABLE signals (
    id BIGSERIAL PRIMARY KEY,
    signal_hash TEXT UNIQUE NOT NULL,  -- hash(tipo + entidade + key_data) pra dedup
    tipo TEXT NOT NULL,  -- ex: 'raci_vencido', 'post_sem_imagem', 'contato_esfriando'
    urgencia INT NOT NULL CHECK (urgencia BETWEEN 1 AND 10),  -- 10 = ação imediata
    contexto JSONB NOT NULL,  -- payload estruturado pra Tonha decidir
    detector TEXT NOT NULL,  -- qual detector criou (audit)
    status TEXT NOT NULL DEFAULT 'open',  -- open | resolved | expired | dismissed
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMP,
    resolved_by TEXT,  -- 'tonha_auto' | 'tonha_escalated' | 'renato_direct'
    decision_id BIGINT REFERENCES tonha_decisions(id)
);
CREATE INDEX idx_signals_open ON signals(status, urgencia DESC) WHERE status='open';
CREATE INDEX idx_signals_tipo ON signals(tipo, criado_em DESC);
```

### Camada 2 — Tonha Brain (Sonnet 4.6 + extended thinking)

UMA cabeça. Roda em 2 modos:

#### Modo Reactive (vc fala)
- Webhook WA ou chat web aciona
- Hospedado em **Railway worker** (sem 10s Vercel timeout)
- Carrega contexto: snapshot + L1 memories + recent conversations + ongoing intents + signals abertos
- Extended thinking permite raciocínio profundo antes de responder
- Latência alvo: 5-15s

#### Modo Autonomous Loop
- Roda 3-4x/dia (manhã 8h, almoço 12h, tarde 17h, noite 21h BRT)
- Pulls `signals WHERE status='open' ORDER BY urgencia DESC LIMIT 30`
- UM passe de raciocínio decide cada signal:
  - **95% dos casos** — executa silencioso (dismiss hot take velho, fecha task duplicada, update status projeto stale)
  - **3% dos casos** — rascunha + envia + registra (email pra cliente óbvio, WA pra Amadeo confirmando RACI)
  - **2% dos casos** — escala UMA mensagem pra Renato com substância (decisão estratégica real)
- Toda decisão grava em `tonha_decisions` (audit completo)
- Custo alvo: $2-3/loop × 4 loops/dia = $8-12/dia

**Schema sugerido `tonha_decisions`:**

```sql
CREATE TABLE tonha_decisions (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT REFERENCES signals(id),
    decision_type TEXT NOT NULL,  -- 'auto_execute' | 'draft_and_send' | 'escalate' | 'silence'
    decision_summary TEXT NOT NULL,  -- 1 linha do que decidiu
    reasoning TEXT,  -- pensamento que levou à decisão (truncado, 500 chars)
    action_taken JSONB,  -- {tool: 'send_message', params: {...}, result: '...'}
    cost_usd FLOAT,
    model TEXT,  -- 'claude-sonnet-4-6'
    iteration_count INT,  -- quantos turns de tool_use
    mode TEXT NOT NULL,  -- 'reactive' | 'autonomous'
    triggered_by TEXT,  -- 'wa_msg' | 'chat_web' | 'cron_loop'
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_decisions_signal ON tonha_decisions(signal_id);
CREATE INDEX idx_decisions_recent ON tonha_decisions(criado_em DESC);
```

### Camada 3 — Tool Catalog (5 tools limpos)

Atual: `query_intel`, `execute_action` com 19 sub-actions, `query_conselhoos`, `execute_conselhoos`, `draft_message`, `project_chat` — confuso e Tonha aluciana.

Novo:

| Tool | Substitui | O que faz |
|---|---|---|
| `search_context(query, scope?)` | query_intel, query_conselhoos, search_system_memories | Busca semântica + keyword em contacts, projects, messages, memories, conselhoos. Retorna estruturado. |
| `send_message(channel, target, content, draft?)` | send_whatsapp, send_email, draft_message | WA (contato/grupo) + email (personal/professional) unificado. `draft=true` salva sem enviar. |
| `update_record(entity, id, fields)` | update_task, update_contact, update_calendar_event, save_note, save_memory, execute_conselhoos UPDATE/INSERT | Qualquer tabela com schema validator. Audit automático. |
| `decide_and_log(decision_type, summary, reasoning, signal_id?)` | (novo) | Registra decisão em `tonha_decisions`. Mandatory pra modo autonomous, opcional pra reactive. |
| `delegate_to_claude_code(task, mode)` | já existe | Delegação pro Claude Code Railway worker pra tarefas pesadas (debug, análise de código, pesquisa web profunda). |

**Princípios do tool catalog:**
- Schemas restritos (Pydantic-style validation)
- Descrições com exemplo end-to-end em cada tool
- Sem `execute_action(action="X", params={...})` wrapper — cada ação é tool própria
- Nenhuma tool pode mentir sobre o que fez (toda return inclui evidência)

---

## 5. Migração de infra

### Hosting

| Componente | Hoje | Pós-rebuild |
|---|---|---|
| Vercel | INTEL FastAPI (548 endpoints) + ~25 crons | Idem, **menos** o bot conversacional |
| Railway `audio-transcriber` | Audio + bot dispatch + scheduler (mistura) | Vira `tonha-worker`: bot processing + scheduler + audio + Tonha autonomous loop |
| Railway `claude-code-delegator` | Headless Claude Code | Mantém igual |
| Railway `whatsapp-evolution` | Evolution dup (vai ser morto) | **DELETADO Fase 4** |
| Hetzner `wa.almeida-prado.com` | Evolution ativo | Único Evolution ativo |
| Neon INTEL DB | DB principal | Mantém |
| Neon ConselhoOS DB | DB separado | Mantém |

### Crons — atual vs futuro

Hoje: ~25 crons Vercel + ~15 jobs APScheduler no audio-transcriber.

Pós-rebuild:
- Vercel crons: ficam só os operacionais (sync, health, daily-synthesis, etc.) — ~15
- APScheduler Railway: cron leve de detectores (1x/h) + 4 ticks Tonha autonomous loop
- 11 jobs CoS LLM: **eliminados** (já desligados em 15/06 commit 65abd33)

---

## 6. Cronograma — 5 dias úteis

| Fase | Data | Trabalho | Aprovação Renato |
|---|---|---|---|
| **0** | 15/06 (hoje, resto) | Doc + migrations signals/tonha_decisions | Revisa doc amanhã 8h |
| **1** | 16/06 seg | 5 detectores deterministas + cron 1x/h | Sample saída detectores |
| **2** | 17-18/06 ter-qua | Brain Tonha em Railway worker + prompt limpo + 5 tools | Shadow run 24h |
| **3** | 19/06 qui | Cutover faseado feature flag → produção | Live com observação |
| **4** | 20/06 sex | Cleanup 11 cos_*.py + consolida Evolution Hetzner | Confirma kill Railway evolution |
| **5** | 21-22/06 weekend | Observação + tuning prompt | Feedback livre |

---

## 7. Decisões aprovadas em 15/06

| # | Decisão | Status |
|---|---|---|
| 1 | Modelo: Sonnet 4.6 + extended thinking | ✅ |
| 2 | Bot processing migra Vercel → Railway worker | ✅ |
| 3 | Evolution: mantém **Hetzner**, mata Railway whatsapp-evolution | ✅ (Renato: "cost-mitigation") |
| 4 | Extractor noturno (cos_extractor): MANTÉM | ✅ |
| 5 | Strangler pattern com rollback 1 semana | ✅ |
| 6 | Ear mode (cos_ear_mode): MATA | ✅ |
| 7 | Daily synthesis (22h): MANTÉM | ✅ |
| 8 | CoS Investigator (briefing matinal): integra ao loop novo | ✅ |

---

## 8. Custo estimado pós-rebuild

| Item | Hoje | Pós-rebuild |
|---|---|---|
| Tonha conversacional | Haiku ~$2-3/dia | Sonnet ~$3-5/dia |
| 11 LLM agents proativos | ~$3-5/dia | **$0** (eliminados) |
| Tonha autonomous loop (Sonnet 4x/dia) | — | ~$8-12/dia |
| Extractor noturno | $0.03/dia | $0.03/dia |
| Daily synthesis | $0.05/dia | $0.05/dia |
| **Total** | **~$5-8/dia** | **~$12-18/dia** |

~3x custo, mas resolve o problema. <$500/mês.

---

## 9. Riscos & mitigações

| Risco | Mitigação |
|---|---|
| Migração quebra envio WA proativo importante | Strangler pattern + rollback 1 semana + alertas se queda volume |
| Reescanear QR no iPhone se mexer Evolution errado | Confirmar qual está ativo + avisar Renato antes de qualquer ação |
| Sonnet + extended thinking latência ruim | Benchmark Fase 2, ajustar budget thinking |
| Detectores escrevem signals demais | Dedup signal_hash + scoring urgência + prompt prioriza top-N |
| Custo Sonnet explode | Budget cap diário + alerta se >$20 |
| Bot down durante migração | Feature flag — feature_flags.use_new_tonha. Volta atrás com 1 toggle. |

---

## 10. Critérios de sucesso

Pós-rebuild, Renato consegue declarar:
1. "Ela aprende com o tempo" — memórias persistentes, retrieval funciona
2. "Decisões dela importam" — quando ela escala, é one/two-way door real
3. "Acabou o lixo" — máximo 1-2 mensagens dela por dia
4. "Voz consistente" — mesma Tonha no WA e no chat web
5. "Manda WA em meu nome de verdade" — send_message funciona em grupos + 1:1
6. "Análise de demonstrações financeiras dá pra fazer" — delegate_to_claude_code engatilha
7. "Custo cabe" — <$500/mês

---

## 11. Próxima ação (imediata)

1. Renato revisa este doc amanhã 8h BRT (16/06)
2. Aprovação → eu disparo Fase 1 (detectores) imediatamente
3. Migrations de `signals` e `tonha_decisions` aplicadas em produção Neon INTEL

**Pra rastreabilidade**: este doc é a referência canônica. Mudanças no plano = update aqui + commit + nota no audit_log.
