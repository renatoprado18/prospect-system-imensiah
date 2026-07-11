# Runbook — F-2 Consolidação (cutover Railway + blind spot de email)

**Status:** preparado 11/07/2026 (sessão de firefight Tônia/sunset). Pra executar em **sessão dedicada, supervisionada, com Renato presente**. Sob **protocolo 2-sessões** (`feedback_parallel_sessions`) — briefing/urgent é lane da outra sessão.

Fonte: `project_arquitetura_consolidacao_09_07` + `project_dev_backlog` (F-2).

---

## Objetivo

Consolidar hosting: **INTEL API + worker + Tônia → Railway** (always-on). Vercel fica **só com ConselhoOS**. Hetzner mantém **só Evolution API** (sessão Baileys stateful, restart hazard, isolamento é virtude).

- Custo: ~+US$10-20/mês.
- Esforço: 2-3 dias + ~1 semana de **dual-run** antes do cutover.
- Regra de ouro: **cutover webhook→DNS por ÚLTIMO**, com Vercel vivo o tempo todo pra rollback instantâneo.

## Topologia ANTES (confirmada 11/07)

| Componente | Host hoje | Webhook / entrada |
|---|---|---|
| INTEL API | Vercel (`intel.almeida-prado.com`) | Evolution `rap-whatsapp` (5511984153337) → `/api/webhooks/whatsapp` |
| INTEL worker | Railway (`audio-transcriber`) | APScheduler (crons) |
| Tônia | Vercel (`tonia.almeida-prado.com`) | Evolution `intel-bot-v2` (5511915020192) → `/webhooks/evolution` |
| Evolution API | Hetzner (2 instâncias) | — |
| ConselhoOS | Vercel | — (fica) |

## Topologia DEPOIS

INTEL API + worker + Tônia num serviço Railway (ou serviços no mesmo projeto). Vercel só ConselhoOS. Evolution segue no Hetzner apontando pros endpoints Railway.

---

## Pré-flight (antes de qualquer cutover)

- [ ] **Snapshot Evolution volume** — já criado (F-0, VM 406584701). Confirmar que o `pg_dump` diário da sessão Baileys está rodando (restore sem QR é viável, F-0). **Fazer rsync recorrente dos dumps** (débito menor aberto).
- [ ] **Backup Neon** — PITR 5min ativo. Anotar timestamp pré-cutover.
- [ ] **Inventário de crons** — o worker Railway (`workers/audio-transcriber/main.py`) é a fonte única (migration 045). Listar todos os jobs + os GH Actions de backup (`workflow_dispatch` only desde 13/06).
- [ ] **Restart policy diário** no Railway (always-on ⇒ memory leaks — atenção do memo).
- [ ] **Envs** — mapear todas as envs Vercel (INTEL + Tônia) → Railway. Cuidado com `\n` literal (`feedback_env_var_whitespace`) e `DB_TARGET` (`reference_db_target_protocol`).

## Sequência do cutover (supervisionado, com rollback em cada passo)

1. **Subir em dual-run** — INTEL API + Tônia no Railway em paralelo, SEM cortar Vercel. URLs Railway temporárias.
2. **Validar Railway** (~1 semana dual-run): `/health`, crons disparando, briefing/urgent gerando, **WA E2E** (mandar msg → resposta). ⚠️ **LID**: garantir que o receptor WA no Railway herda o handling LID→`remoteJidAlt` (`reference_whatsapp_lid_migration`) — senão a Tônia fica muda de novo (foi a causa do incidente 11/07).
3. **WA Fase B — inverter webhook** (decisão pendente do que centraliza): repontar Evolution (`rap-whatsapp` e/ou `intel-bot-v2`) pros endpoints Railway. ⚠️ Alto blast radius — testar 1 instância por vez, com msg de teste imediata.
4. **Cutover DNS** — `intel.almeida-prado.com` + `tonia.almeida-prado.com` → Railway. TTL baixo antes.
5. **Desligar Vercel INTEL + Tônia** (manter ConselhoOS). Só depois de 24-48h estáveis.

## Rollback

- DNS revert (Vercel mantido vivo até passo 5). Evolution webhook revert pros endpoints Vercel.
- Neon PITR se corrupção de dados.
- Evolution: restore do dump Postgres (sem QR).

## Riscos

- **WA blackout** — reponto de webhook derruba o canal. Testar por instância + msg imediata.
- **Memory leaks** always-on (restart diário mitiga).
- **Cron drift/double-fire** na transição — desligar GH Actions backup só após Railway provado.
- **Coordenação 2-sessões** — não mexer em briefing/urgent/crons sem alinhar com a outra sessão.

---

## Frente paralela F-2 — blind spot de EMAIL (raw)

**NÃO é o cutover, mas faz parte de fechar o F-2.** Descoberto 11/07:

- **Estado:** `email_triage` guarda só a **decisão** de triagem (priority/classification/tags + `message_id`). O **corpo do email NÃO é capturado** ("Email sem raw (só classificação)" — memo). O schema `copilot` **não expõe email** (views: signals/tasks/contacts/messages/calendar_events/memories/action_proposals/group_messages). A Tônia é **cega pra conteúdo de email**.
- **Trabalho (precisa scoping antes de estimar):**
  1. Gmail sync capturar + persistir o **raw body** (decidir onde: `messages` canal=email, ou tabela nova).
  2. Expor via `copilot.emails` (ou estender `copilot.messages`).
  3. Tônia ler no briefing/urgent.
- **Não é leve** — é frente própria, candidata à sessão dedicada.

---

## Residuais F-2 já fechados

- ✅ **Atuadores C1+C2** (`af2b4be` INTEL + `5300407` Tônia) — provado E2E via WA 11/07 (task #999600 criada por comando).
- ✅ **WA anexos → Drive** (`54bee9d`).
- ✅ **ConselhoOS model bump** sonnet-4→5 (`1644155`, `llm-config.ts` = `claude-sonnet-5`) — commitado pela outra sessão.
- ⏸️ **read-only ConselhoOS wiring** — held, lane da outra sessão.
- ⬜ **wrapper LLM INTEL** (~46 sites) — adiado por decisão (colisão alta).
