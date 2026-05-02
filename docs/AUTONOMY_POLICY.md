# Autonomy Policy — INTEL CRM

Pilar P3 do projeto **Inteligência Real**. Define quando o sistema age sozinho (Auto), quando age e avisa (Notifica), e quando propõe e espera aprovação (Pergunta).

> **Quando atualizar este doc:** ao adicionar uma nova ação automatizada (cron, webhook, bot tool, message handler), classifique-a aqui antes de mergear. Sem classificação = bug de governança.

---

## Os três níveis

| Nível | Quando usar | Requer audit log? |
|---|---|---|
| **Auto** | Ação tem critérios claros, baixo risco de erro caro, e seria ruído se cada execução exigisse aprovação. Ex: limpar propostas expiradas, sincronizar contatos do Google. | **Sim** — toda Auto destrutiva ou que muda estado externo (Gmail, LinkedIn, Drive) DEVE chamar `log_action()` em `agent_actions`. |
| **Notifica** | Ação acontece sozinha mas o usuário precisa saber porque é informação relevante (não pra aprovar, pra ficar ciente). Ex: digest diário, briefing matinal. | Recomendado — útil pra debriefing 19h. |
| **Pergunta** | Critérios ambíguos, ação cara/irreversível, ou requer contexto humano. Cria `action_proposal` com `status='pending'`. | Não — proposta cria seu próprio audit (`action_proposal.proposal_created`). |

### Critérios de classificação

Quando estiver em dúvida, pergunte:

1. **Custo de erro:** ação dispara mensagem externa (LinkedIn, email, WhatsApp), apaga dado, ou escreve em sistema do cliente? → mínimo **Notifica**, idealmente **Pergunta** se confiança < 90%.
2. **Confiança da regra:** baseado em regex frágil ou string match? → **Pergunta**. Score determinístico ou flag explícita? → **Auto** ok.
3. **Frequência:** roda 100x/dia? → **Auto** (Pergunta seria spam). Roda 1x/dia? → **Notifica** ou **Pergunta** se bate critério 1.
4. **Reversibilidade:** ação tem `undo_hint`? → **Auto** mais defensável.

---

## Matriz por categoria

Estado em **2026-05-02**. Baseado em [audit do código](../app/services/) + cron config em [`vercel.json`](../vercel.json).

### Email

| Ação | Nível | Audit? | Notas |
|---|---|---|---|
| `gmail_sync.sync_all_contacts` (daily-sync) | Auto | ❌ | Sync ≠ ação destrutiva — exempt. |
| `email_triage.process_new_emails` | Auto | ❌ | Aplica labels/tags. **Gap:** deveria logar pra rastrear classificação errada. |
| `bot._manage_email` archive_non_urgent / archive_by_subject | **Auto** | ❌ **CRÍTICO** | Bot arquiva email no Gmail (destrutivo!) sem trilha. **Phase 3 obrigatório.** |
| `email_digest.generate_email_digest` (cron 21h) | Notifica | ❌ | Gera resumo, manda WhatsApp. |
| `payment_cycle.check_payment_replies` (daily-sync) | **Auto** | ❌ **CRÍTICO** | Auto-fecha milestones + cria próximo ciclo financeiro. **Phase 3 obrigatório.** |
| `smart_fup.check_pending_fups` (daily-sync) | Pergunta | n/a | Cria `action_proposal` — comportamento correto. |

### WhatsApp

| Ação | Nível | Audit? | Notas |
|---|---|---|---|
| `whatsapp_sync.process_webhook` save msg + dismiss stale | Auto | ❌ | Sync — exempt. |
| `cron_sync_whatsapp_history` | Auto | ❌ | Sync — exempt. |
| `cron_daily_morning_briefing` (7h SP) | Notifica | ❌ | Digest matinal. |
| `cron_daily_evening_debriefing` (19h SP) | Notifica | ❌ | Digest 19h. |
| `group_digest.generate_daily_group_digests` | Notifica | ❌ | Resumo de grupos sync. |
| `daily_synthesis.run_daily_synthesis` (1h UTC) | Auto | ❌ | Salva em `system_memories`. **Gap:** logar. |
| `realtime_analyzer` urgent_alert / question / payment | Pergunta | n/a | Cria proposta — correto. |
| `bot._execute_intel_action` save_feedback / save_article | **Auto** | ❌ **CRÍTICO** | Bot escreve em INTEL sem trilha. **Phase 3.** |

### Tarefas

| Ação | Nível | Audit? | Notas |
|---|---|---|---|
| `task_auto_resolver.check_and_resolve_tasks` (>0.8 confidence) | Auto | ✅ | `task_resolved` audit log existe. **OK.** |
| `task_auto_resolver` ambíguo (0.4-0.8) | Pergunta | n/a | Vira pergunta no bot. |
| `tasks_sync.full_sync` Google Tasks | Auto | ❌ | Sync — exempt. |
| `bot._execute_intel_action` create_task / complete_task | **Auto** | ❌ **CRÍTICO** | Bot cria/completa tarefa sem trilha. **Phase 3.** |
| `editorial_metrics` API auto-completes related tasks | user-trigger | ❌ | Disparado por upload de métricas — n/a. |

### Contatos

| Ação | Nível | Audit? | Notas |
|---|---|---|---|
| `smart_message_processor._auto_update_email` | Auto | ✅ | `contact_email_updated`. **OK.** |
| `_create_email_proposal` (ambíguo) | Pergunta | n/a | Correto. |
| `sync_contacts_incremental` (daily-sync) | Auto | ❌ | Sync — exempt. |
| `linkedin_enrichment.enrich_contact` (job changes) | Notifica | ❌ | Detecta mudança → cria proposta urgente. **Gap:** sem cron agendado, só manual. Adicionar cron + audit em Phase 3+. |
| `contact_enrichment.auto_enrich_priority_contacts` | Auto | ❌ | Manual call — sem audit. |

### Editorial

| Ação | Nível | Audit? | Notas |
|---|---|---|---|
| `auto_publisher.publish_due_posts` (cron diário) | Auto | ✅ | `post_published`. **OK.** |
| `auto_publisher.select_weekly_posts` (cron domingo) | Pergunta | n/a | Cria `weekly_editorial` proposta. |
| `action_proposals.auto_resolve_weekly_editorial` | Auto | ❌ | Resolve proposta silenciosamente. **Gap baixo.** |
| `editorial_pdca.generate_weekly_briefing` (cron domingo 21h) | Notifica | ❌ | Cria tarefas + manda WA. |
| `_editorial_metrics_reminder_impl` (cron 14h, 23h) | Notifica | ❌ | Lembra coleta de métricas. |
| **Coleta de métricas editoriais** | **manual** | n/a | **Gap:** virar Auto agendado pós-deploy de cron de scraping LinkedIn. Listado no milestone P3. |

### Conselho (ConselhoOS)

| Ação | Nível | Audit? | Notas |
|---|---|---|---|
| `conselhoos_raci_sync.full_sync` (cron 3h SP) | **Auto** | ❌ **CRÍTICO** | Escreve em 2 bancos (RACI marca done + cria task INTEL). **Phase 3.** |
| `raci_weekly_report.send_raci_to_groups` (cron seg 11h UTC) | Notifica | ❌ | WA pro grupo do conselho. |
| `parse_raci_update` (de mensagem do grupo) | **Auto** | ❌ **RISCO** | Muda status RACI por regex em msg WA. **Sugestão:** virar Pergunta quando `confidence < 0.85`. **Phase 3.** |

### Calendar

| Ação | Nível | Audit? | Notas |
|---|---|---|---|
| `calendar_sync.incremental_sync` (daily-sync) | Auto | ❌ | Sync — exempt. |
| `cron_pre_meeting_briefings` | Notifica | ❌ | WA briefing antes da reunião. |
| `conselhoos_briefing` auto-generate 24h ahead | Auto | ❌ | Cria briefing record. |
| `realtime_analyzer` reschedule/cancel/confirm | Pergunta | n/a | Cria proposta — correto. |
| `action_executor.reschedule_event` etc | user-trigger | ❌ | Disparado por aprovação — exempt da policy de Auto. |

### Outro / Infra

| Ação | Nível | Audit? | Notas |
|---|---|---|---|
| `cron_cleanup` (cron domingo 4h UTC) | Auto | ❌ | Apaga sugestões/notificações expiradas. |
| `cron_health_recalc` (18h UTC + daily-sync) | Auto | ❌ | Recompute health_score. |
| `cron_index_drive_documents` | Auto | ❌ | Embeddings. |
| `campaign_executor.process_pending_steps` (daily-sync) | **Auto** | ❌ **CRÍTICO** | Manda WhatsApp por cron. **Phase 3.** |
| `smart_triggers.run_automations` (manual/scheduled) | Auto | ❌ | Cria sugestões/tasks. |
| `drive-webhook` Google Drive push | Auto | ❌ | Indexa novos docs. |
| `fathom-webhook` (criação automática de project/tasks) | **Auto** | ❌ | Cria projeto + tarefas a partir de reunião. **Phase 3 candidato.** |

---

## Exempções da regra de audit

Estas categorias são **Auto sem audit log obrigatório** porque o custo de logar superaria o benefício:

- **Sync jobs** (Google contacts/calendar/tasks/gmail, WhatsApp history) — alto volume, baixo risco. Auditadas via tabelas `*_sync_state` que já registram `last_incremental_sync`.
- **Recompute jobs** (`health-recalc`, embeddings) — idempotentes, sem efeito externo.
- **Cleanup jobs** — só apagam dados expirados, sem efeito externo.

Tudo o mais Auto deve logar.

---

## Top 5 gaps prioritários (P3 Fase 3)

Em ordem de risco:

1. **`bot._manage_email`** (`workers/audio-transcriber/main.py`) — bot arquiva email no Gmail. Destrutivo + invisível. Logar com `undo_hint` (lista de Gmail message IDs arquivados).
2. **`bot._execute_intel_action`** (`workers/audio-transcriber/main.py`) — bot cria task/nota/memória/feedback/artigo no INTEL. Logar cada ação com `entity_id`.
3. **`payment_cycle.check_payment_replies`** (`app/services/payment_cycle.py`) — auto-fecha milestone financeiro + cria próximo ciclo. Logar com `scope_ref` apontando pro projeto.
4. **`campaign_executor.process_pending_steps`** (`app/services/campaign_executor.py`) — manda WA por cron. Logar com `entity_id` da step.
5. **`parse_raci_update`** (`app/services/conselhoos_raci_sync.py`) — muda status RACI por regex. **Mudar para Pergunta quando confidence < 0.85**, logar quando Auto.

Bonus (não-crítico mas worthwhile):
- `email_triage.process_new_emails` — logar classificação aplicada.
- `auto_resolve_weekly_editorial` — logar resolução silenciosa.
- `daily_synthesis.run_daily_synthesis` — logar quando salva em system_memories.

---

## Histórico

- **2026-05-01** — P1 entregue: tabela `agent_actions`, service `log_action()`, página `/agente`, cron de debriefing 19h, briefing matinal aumentado. 4 callsites instrumentados (auto_publisher x2, smart_message_processor, task_auto_resolver).
- **2026-05-02** — P2 entregue: snapshot context expandido no bot. Auditoria de autonomia revelou ~5% de cobertura de audit log; doc P3 (este arquivo) escrito.
- **2026-05-02** — P3 Fase 3 pendente (top 5 gaps).
