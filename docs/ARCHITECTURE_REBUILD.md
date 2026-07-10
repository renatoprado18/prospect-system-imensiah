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
| `detector_governanca_pessoal.py` | (novo — Ritual 3 sec. 4.6) | drift de projetos sem owner/prioridade/update, tasks órfãs, duplicação semântica, frentes CoS Config sem atividade, compromisso verbal detectado em conversa sem virar task |
| `detector_delegacoes.py` | (novo — sec. 4.5) | delegações vencidas, sem resposta, em escalation, pra cobrança automática |

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

### Camada 3 — Tool Catalog (8 tools limpos)

Atual: `query_intel`, `execute_action` com 19 sub-actions, `query_conselhoos`, `execute_conselhoos`, `draft_message`, `project_chat` — confuso e Tonha aluciana.

Novo:

| Tool | Substitui | O que faz |
|---|---|---|
| `search_context(query, scope?)` | query_intel, query_conselhoos, search_system_memories | Busca semântica + keyword em contacts, projects, messages, memories, conselhoos. Retorna estruturado. |
| `search_inbox(query, channel, direction?)` | (novo — gap atual) | Busca em **emails enviados + recebidos** (Gmail via INTEL store) E **WhatsApp 1:1 + grupos**. Params: `query` texto, `channel` ∈ {email, wa, all}, `direction` ∈ {sent, received, both}, `days?` janela. Usa quando Tonha precisa verificar "Roger respondeu?", "mandei pra Amadeo já?", "Veridiana falou algo no grupo?". |
| `web_research(query, depth?)` | (novo — gap atual) | Pesquisa web ao vivo (WebSearch + WebFetch) pra coisas que ela não sabe: contexto de mercado, dados públicos de empresas, jurisprudência, notícias. `depth` ∈ {quick, deep}. Quick = 1 query. Deep = delegate_to_claude_code com escopo "investigação web". |
| `send_message(channel, target, content, draft?)` | send_whatsapp, send_email, draft_message | WA (contato/grupo) + email (personal/professional) unificado. `draft=true` salva sem enviar. |
| `update_record(entity, id, fields)` | update_task, update_contact, update_calendar_event, save_note, save_memory, execute_conselhoos UPDATE/INSERT | Qualquer tabela com schema validator. Audit automático. |
| `delegate(to, task, context, deadline?)` | (novo — gap atual) | **Delegação real** pra time humano + Dev. `to` ∈ {andressa, joao_piccino, priscila_contadora, dev, evaluator, collector}. Cria tarefa em `delegations` (nova tabela), manda WA/email pra pessoa com contexto, agenda follow-up automático, cobra se não responder até deadline. Ver seção 4.5. |
| `decide_and_log(decision_type, summary, reasoning, signal_id?)` | (novo) | Registra decisão em `tonha_decisions`. Mandatory pra modo autonomous, opcional pra reactive. |
| `delegate_to_claude_code(task, mode)` | já existe | (Alias de `delegate(to='dev', ...)` quando a tarefa é técnica). Mantém pra invocação direta de tarefa de código/debug/análise técnica. |

**Princípios do tool catalog:**
- Schemas restritos (Pydantic-style validation)
- Descrições com exemplo end-to-end em cada tool
- Sem `execute_action(action="X", params={...})` wrapper — cada ação é tool própria
- Nenhuma tool pode mentir sobre o que fez (toda return inclui evidência)

### 4.5 — Delegação a time humano + Dev (capacidade nova)

A Tonha precisa **agir através de outros agentes** — humanos e IA — quando não é ela quem deve executar. CoS de verdade não faz tudo: delega bem.

**Time atual mapeado:**

| Alvo (`to=`) | Quem é | Contact ID | Canal preferido | Função |
|---|---|---|---|---|
| `andressa` | Andressa Santos | #313 | WA | Assistente virtual — tarefas operacionais, follow-ups, marcar coisas |
| `joao_piccino` | João Carlos A P Piccino | #2869 | Email + WA | Advogado — contratos, processos, análise jurídica |
| `priscila_contadora` | Priscila Aquino | #4734 | WA (+5514991792675) | Contadora — balancetes, demonstrações, questões fiscais |
| `dev` | Claude Code (Railway delegator) | — | Tool nativa | Código, debug, análise técnica, pesquisa web profunda |
| `evaluator` | Tonha-mesma em modo análise | — | Interno | Avalia qualidade, viabilidade, risco de algo (loop reflexivo) |
| `collector` | Tonha-mesma em modo cobrança | — | Interno | Faz follow-up + escalation de delegações abertas |

**Schema sugerido `delegations`:**

```sql
CREATE TABLE delegations (
    id BIGSERIAL PRIMARY KEY,
    delegated_to TEXT NOT NULL,             -- 'andressa' | 'joao_piccino' | 'priscila_contadora' | 'dev' | 'evaluator' | 'collector'
    contact_id INT,                          -- FK contacts.id quando humano
    task_summary TEXT NOT NULL,
    task_full TEXT NOT NULL,                 -- contexto + instrução
    deadline DATE,
    status TEXT NOT NULL DEFAULT 'open',     -- open | in_progress | completed | overdue | escalated
    response TEXT,                           -- resposta humana se houver
    response_at TIMESTAMP,
    last_followup_at TIMESTAMP,
    followup_count INT DEFAULT 0,
    decision_id BIGINT REFERENCES tonha_decisions(id),
    signal_id BIGINT REFERENCES signals(id),
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**Padrões de uso (instrução no prompt):**

1. **Avaliar algo** (proposta, contrato, número, post): `delegate(to='evaluator', task='avalia este X', context=...)` → loop reflexivo dela mesma com extended thinking, gera laudo estruturado (qualidade, riscos, recomendação).

2. **Cobrar pendência** (alguém ficou de fazer algo e não fez): `delegate(to='collector', task='cobrar Y de X', context=...)` → ela busca o histórico, decide o tom (delicado/firme/escalonar), manda WA/email apropriado, agenda próximo follow-up.

3. **Tarefa operacional** (marcar reunião, atualizar planilha, ligar pra prestador): `delegate(to='andressa', task='...', deadline='...')` → rascunha WA pra Andressa com contexto suficiente pra ela executar sozinha.

4. **Questão jurídica** (revisar contrato, opinião legal): `delegate(to='joao_piccino', task='...', context=...)` → manda email formal com docs anexos OU draft WA dependendo da formalidade.

5. **Questão contábil/fiscal**: `delegate(to='priscila_contadora', ...)`.

6. **Tarefa técnica/código/pesquisa profunda**: `delegate(to='dev', task='...')` → invoca Claude Code Railway delegator.

**Cobrança automática:**

- Cron leve 1x/dia verifica `delegations WHERE status='open' AND deadline < NOW()`.
- Cria signal tipo `delegacao_vencida` com `contexto` apontando pra delegation_id.
- Próximo loop da Tonha lê signal, decide: (a) cobrar de novo (delicado se primeira vez, firme se segunda), (b) escalar pro Renato, (c) cancelar/atualizar deadline.

### 4.6 — Governança pessoal do Renato (capacidade nova)

A Tonha não é só CoS dos negócios — ela faz governança **do próprio trabalho dele**. Olha proativamente o portfolio de projetos, tarefas e objetivos. Sugere reorganização. Mantém ritmo.

**3 rituais permanentes:**

#### Ritual 1 — RACI semanal pessoal (toda segunda 7h BRT)

Não confundir com RACI dos conselhos (Vallen/Alba/etc.). Esse é o RACI **dele**.

**Canal: grupo WA "Governança APCE"** (criado 15/06/26 — group_jid `120363408627197480@g.us`, vinculado ao projeto INTEL #34 "Governança APCE", pessoal).

Participantes:
- Renato (admin)
- Tonha (intel-bot)
- Andressa Santos (assistente humana)

Tonha posta toda segunda 7h BRT 1 mensagem agrupada:
- ✅ **Concluído na semana passada** (do que ele se comprometeu)
- 🔄 **Em andamento** (prazo + último signal de movimentação)
- 🔴 **Sem movimento +14d** (precisa decidir: descomprometer ou destravar)
- 🆕 **Surgiu novo** (decisões da semana, compromissos verbais detectados em conversas, áudios, atas)

Renato responde **assincronamente** ao longo da semana via WA — atualiza item por item, ou em lote. Andressa pode atualizar status do que tocar. Tonha mantém a tabela em `weekly_raci_renato` viva.

#### Ritual 2 — Reunião mensal (1º útil do mês, 8h BRT)

Tonha prepara **pauta no domingo à noite** baseada em:
- Drift no portfolio (projetos parados >30d, sem owner, sem prioridade)
- Frentes da CoS Config v5 vs realocação real de horas (audit: ele tá gastando 30% em Vallen mas Vallen é peso 15%?)
- Pendências de delegações abertas a humanos (Andressa, João, Priscila)
- Decisões one-way door em maturação (SP/Japão, Wadhwani, etc.)
- Métricas de saúde (sono, treino, família — política C2)

Ele responde "ok pauta" → Tonha agenda reunião no calendar. Durante a reunião ele dita áudios curtos pra cada item, Tonha sintetiza e atualiza projetos/prioridades/delegações.

#### Ritual 3 — Reorganização proativa contínua

Detector `detector_governanca_pessoal` roda 1x/dia 6h BRT. Emite signals quando:
- Projeto sem owner_contact_id
- Projeto sem prioridade definida >7d
- Projeto sem nota de atualização >30d com tasks ativas
- Tasks órfãs (sem projeto vinculado) >7d
- Tasks duplicadas semanticamente (mesmo título variação)
- Compromisso verbal detectado em conversa (msg WA, áudio transcrito) sem virar task
- Frente CoS Config com peso alto mas zero atividade no mês

Tonha, no autonomous loop, **decide automaticamente**:
- Projeto sem prioridade >7d com 0 tasks abertas há 30d → status=pausado (silêncio)
- Tasks duplicadas → consolida na mais recente, marca a outra resolved (silêncio)
- Task órfã >30d → propõe ele em 1 frase no próximo RACI semanal (sem push imediato)
- Frente com peso alto sem atividade → escala uma vez/mês na reunião mensal

**Schema sugerido `weekly_raci_renato`:**

```sql
CREATE TABLE weekly_raci_renato (
    id BIGSERIAL PRIMARY KEY,
    semana_inicio DATE NOT NULL,            -- segunda da semana
    item_tipo TEXT NOT NULL,                -- 'concluido' | 'em_andamento' | 'sem_movimento' | 'novo'
    titulo TEXT NOT NULL,
    fonte_ref JSONB,                        -- {project_id: ..., task_id: ..., conversation_id: ...}
    frente_cos TEXT,                        -- frente da CoS Config v5
    status TEXT DEFAULT 'open',
    renato_response TEXT,                   -- resposta assincrona dele (lote ou item)
    response_at TIMESTAMP,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_raci_renato_semana ON weekly_raci_renato(semana_inicio DESC);
```

---

### 4.7 — Triagem de inbox (email + WhatsApp DM)

**Problema:** Hoje Renato usa label `!!Renato` no Gmail pra marcar emails que ele precisa olhar. Manual, virou bottleneck. WhatsApp DMs idem — chegam misturadas com grupos, sinal alto se perde no volume.

**Princípio:** Tonha NÃO encaminha cada mensagem. Ela classifica, escala só os top 2-3/dia (urgência ≥7) e batela o resto num digest diário 7h BRT.

#### Detector `detector_inbox`

Roda no scheduler (hourly), zero LLM. Lê emails novos (últimas 4h) + WA DMs novas, classifica em 3 baldes:

| Balde | Emite signal? | Critério |
|---|---|---|
| **Alto sinal** | `inbox_atencao` (urg 6-9) | Remetente VIP (contact tier=A/B), palavra-chave de decisão/cobrança/RACI, projeto ativo mencionado, Renato citado em grupo |
| **Médio** | `inbox_digest` (urg 3-5) | Mid-tier, info-only, newsletters relevantes |
| **Ruído** | (nada) | Marketing, automatizado, social, lista quente sem ação |

Schema dos signals `inbox_atencao` / `inbox_digest`:
```json
{
  "fonte": "gmail|wa_dm",
  "thread_id": "...",
  "from": {"name": "...", "email_or_jid": "...", "contact_id": 313, "tier": "A"},
  "subject": "...",
  "preview": "primeiros 500 chars",
  "received_at": "ISO",
  "match_reason": ["vip_remetente", "palavra_chave:cobranca", "projeto_ativo:Vallen"],
  "thread_size": 4
}
```

#### Tonha brain — ações por signal

Tonha lê `inbox_atencao` + `inbox_digest` e decide por mensagem:

| Decisão | Quando | Ferramenta |
|---|---|---|
| `escalate` | urg ≥7 + ação necessária do Renato | post no chat Tonha com contexto + sugestão |
| `draft` | urg 5-7 + tom já conhecido | `gmail_create_draft` + label `Tonha rascunhou` (FASE 2, ver tradeoff abaixo) |
| `digest` | urg 3-5 | acumula em `inbox_digest_buffer` (tabela), entra no briefing 7h BRT |
| `silence` | médio mas já tratado em outra thread | label `tonha tratou` |

#### `!!Renato` como gold-label de treino

Cada vez que Renato labela `!!Renato` num email que Tonha NÃO escalou → `tonha_decisions.reverted_by='user'`, alimenta calibragem. Cada `!!Renato` em email que ela JÁ marcou alto = positivo. Ao longo de 2 semanas isso ajusta limiares por remetente.

#### Fase A vs Fase B (tradeoff)

- **Fase A (lançamento):** só classifica + escala + digesta. Não rascunha resposta. Risco baixo de tom errado.
- **Fase B (após 2 semanas):** ativa `draft` automático **só pra remetentes onde ela tem ≥10 reverts positivos** (você confirmou tom). Limiar conservador.

#### WA DM (novo, não tinha cobertura)

`detector_inbox` lê `whatsapp_messages` onde `from_jid` é DM (não grupo) e `direction='incoming'`, últimas 4h. Mesma classificação. Bonus: se Renato citado em grupo (`@5511...`) também vira `inbox_atencao`.

```sql
CREATE TABLE inbox_digest_buffer (
    id BIGSERIAL PRIMARY KEY,
    fonte TEXT NOT NULL,                    -- gmail | wa_dm
    ref_id TEXT NOT NULL,                   -- thread_id ou message_id
    preview TEXT,
    from_label TEXT,
    received_at TIMESTAMP NOT NULL,
    delivered_in_digest_at TIMESTAMP,       -- quando entrou no briefing 7h
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_inbox_buffer_pending ON inbox_digest_buffer(received_at) WHERE delivered_in_digest_at IS NULL;
```

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
| 4 | Extractor noturno (cos_extractor): MATA | ✅ (10/07 — decisão de ROI: só 12 memórias em 2 runs; aprender vira função da Tônia única F1. Autonomous tick da Tonha também morto no mesmo dia — experimento 7d OFF encerrado, ~25% falha, alinhado ao sunset 05/09) |
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
