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
- ✅ **wrapper LLM INTEL** — FEITO 12/07 (`llm.py` FAST/BALANCED/DEEP, 62 sites; commit `1457b92`). Idem Tônia (`b7042e2`).

---

## ⚡ CHECKLIST EXECUTÁVEL (preparado 13/07 — dual-run já validado)

**Pré-requisitos JÁ prontos:** dual-run Railway de pé + smoke/parity/resiliência VERDES · TTL DNS baixado pra 60 nos 2 domínios (`intel` já era; `tonia` = record `rec_da1d7468c7d42d5b9269380c` criado 12/07) · sessão Baileys vive no Evolution/Hetzner (reiniciar tonia NÃO pede QR) · gen-1/Tonha-H removida.

**URLs Railway:** intel-api = `https://intel-api-production-200b.up.railway.app` · tonia = `https://tonia-production.up.railway.app`.

### Passo 0 — Pré-flight (~5min)
```bash
# backup: anotar timestamp PITR (Neon PITR 5min ativo)
date "+%Y-%m-%d %H:%M:%S BRT"
# parity fresco (deve dar 4/4 idêntico):
cd /Users/rap/tonia && python3 scripts/railway_replay_harness.py --mode parity \
  --tonia-url https://tonia-production.up.railway.app --vercel-url https://tonia.almeida-prado.com
# confirmar TTL drenou (deve mostrar TTL 60 nos 2):
dig @8.8.8.8 tonia.almeida-prado.com +noall +answer; dig @8.8.8.8 intel.almeida-prado.com +noall +answer
```

### Passo 1 — Repontar WEBHOOK Evolution (o processamento; ~15min, 1 instância por vez)
Método: `POST {EVOLUTION_API_URL}/webhook/set/{instância}` header `apikey: {EVOLUTION_API_KEY}`, payload v2:
`{"webhook":{"url":"...","events":["MESSAGES_UPSERT","MESSAGES_UPDATE","SEND_MESSAGE","CONNECTION_UPDATE","QRCODE_UPDATED"],"enabled":true,"webhookByEvents":true,"webhookBase64":false}}`
- **intel-bot-v2** (Tônia) → `url = https://tonia-production.up.railway.app/webhooks/evolution`. Mandar 1 WA de teste → confirmar Tônia responde (LID incluso).
- **rap-whatsapp** (INTEL) → `url = https://intel-api-production-200b.up.railway.app/api/webhooks/whatsapp`. Testar depois da 1ª OK.
⚠️ Testar UMA por vez com msg imediata. Webhook = o crítico; DNS pode vir depois.

## ✅ PASSO 1 EXECUTADO — 13/07/2026 13:04-13:09 BRT (supervisionado, Renato presente)

Env audit passou **zero gap bloqueante** antes de tocar webhook:
- **INTEL** (intel-api): 4 chaves "faltantes" vs Vercel são todas **vazias `""` no próprio Vercel** (`WA_PERSIST_1TO1`, `NOTIFICATION_DIGEST_MODE`, `LINKDAPI_MONITOR_MIN_REACTIONS` = default idêntico) + `VERCEL_API_TOKEN` (não runtime). `DB_TARGET=prod`, `EVOLUTION_API_URL=wa.almeida-prado.com`, `INTEL_BOT_INSTANCE=intel-bot-v2` ✅.
- **Tônia**: `DATABASE_URL` faltante → coberto por fallback `NEON_DATABASE_URL` (db.py:119 + health verde). `CRON_SECRET` faltante → irrelevante: ticks autenticam por `TONIA_WEB_TOKEN` (bate Vercel↔Railway). **Nenhuma env adicionada.**
- **git stash@{0} NÃO popado**: mensagem promete Procfile/.python-version/.railwayignore mas eles nunca existiram (Railway roda por nixpacks auto-detect, verde); stash contém só versão **stale** do runbook. Deixado quieto.

⚠️ **Correção do método vs runbook original:** os webhooks vivos estavam com `webhookByEvents=**false**` (não `true`). Repontados **preservando `false`** + os events exatos de cada instância — setar `true` mudaria o roteamento de entrega (risco de blackout).

| Instância | url nova (Railway) | byEvents | events | validação E2E |
|---|---|---|---|---|
| `intel-bot-v2` | `tonia-production.up.railway.app/webhooks/evolution` | false | 4 (UPSERT/UPDATE/SEND/CONNECTION) | ✅ recebe 200 + `chat.respond` + `evolution.send_text ok` (respondeu WA) + repasse `wa-ingest` 200 |
| `rap-whatsapp` | `intel-api-production-200b.up.railway.app/api/webhooks/whatsapp` | false | 5 (+QRCODE_UPDATED) | ✅ `POST /api/webhooks/whatsapp 200 OK` (2 eventos), zero erro |

**Estado pós-Passo-1:** webhooks WA → Railway · crons/ticks/UI/health → ainda Vercel (via DNS, Passo 2 não feito) · Vercel vivo pra rollback · Neon único (DB_TARGET=prod) = sem double-processing (Evolution entrega a 1 endpoint só).

### Passo 2 — DNS (opcional, pode ficar pra depois; ~5min + ~60s propagação)
No Railway: adicionar custom domain `intel.almeida-prado.com` (svc intel-api) + `tonia.almeida-prado.com` (svc tonia) → Railway devolve um CNAME target. Trocar os 2 CNAMEs no Vercel DNS (`vercel dns rm <id>` + `vercel dns add almeida-prado.com <name> CNAME <railway-target>`). Propaga ~60s (TTL 60).

## ✅ PASSO 2 (DNS) EXECUTADO — 13/07/2026 13:12-13:16 BRT (supervisionado)

Custom domains criados no Railway + TXT `_railway-verify` adicionados no Vercel PRIMEIRO (ownership sem tocar tráfego), verificados, e só então CNAMEs trocados:

| Domínio | CNAME novo (Railway target) | rec Vercel novo | cert |
|---|---|---|---|
| `intel.almeida-prado.com` | `51bam3us.up.railway.app` (svc intel-api, domain 71dd3385) | `rec_3d64144cce9f599a7d3f751f` | VALID/COMPLETE |
| `tonia.almeida-prado.com` | `8l6d48iq.up.railway.app` (svc tonia, domain faedbd1a) | `rec_d5f5e6e6bc720f3edc5fbda4` | VALID/COMPLETE |

Validação: `server: railway-hikari` + `x-railway-edge` nos headers (confirma Railway, não cache Vercel) · intel `/api/health` ok (2300 prospects) · tonia `/health/deep` verde. **Downtime intel ~15-30s** (1 medição http=000 → 200); tonia sem interrupção.

**🎁 Bônus — crons do worker consolidados automaticamente:** o worker Railway (`prospect-system-imensiah`, ~40 jobs em `_SCHEDULER_JOBS`) dispara GETs pra `INTEL_API_URL = https://intel.almeida-prado.com/api/cron/*` (main.py:128). Como o DNS agora aponta pro Railway, **esses ~40 crons passaram a executar no Railway intel-api** (CRON_SECRET bate `…KGq34U` → autenticam OK). Sem ação extra.

**⚠️ DÉBITO PRO PASSO 5 (antes de desligar Vercel INTEL):** 7 crons vivem SÓ no `vercel.json` (disparados pelo scheduler interno do Vercel, **não** no worker) → `health-recalc`, `cleanup`, `editorial-metrics-reminder-evening`, `group-digest`, `platform-costs-snapshot`, `circulos-recalc`, `wa-backfill-1to1`. **Zero double-fire hoje** (nenhum sobrepõe o worker). Mas ao desligar o Vercel eles PARAM — migrar pra `_SCHEDULER_JOBS` do worker antes.

**Rollback DNS:** `vercel dns rm rec_3d64144cce9f599a7d3f751f` + `rec_d5f5e6e6bc720f3edc5fbda4`, depois `vercel dns add almeida-prado.com {intel,tonia} CNAME cname.vercel-dns.com`.

### Passo 3 — Validação (~15min) — ✅ EXECUTADO 13/07 13:20-13:33 BRT
Bateria completa, tudo verde:
- **T1** health+UI via domínios `200` (0.5-0.8s) — 1 blip de 10s pontual, **não recorreu** (Neon saudável, sem restart nos logs → provável cold-start/rede).
- **T2** parity Railway vs Vercel **4/4** (incl. LID ouro).
- **T3** writes no Neon: **9 mensagens** gravadas/40min (ingestão WA OK).
- **T4** crons do worker → Railway: **8 heartbeats `railway-worker`/20min, todos 200** (`tonia-delegate-pickup`, `process-scheduled-actions`, `detectors-run`, `run-social-groups`, `wa-drive-archive`) — prova da consolidação.
- **T5** latência DB endpoints 0.47-0.79s. **T6** Neon: **1 conn ativa / 901 max**.
- **R1** WA E2E (Tônia responde) ✅ · **R2** UI dashboard ✅ · **R3** `daily-morning-briefing` disparado via domínio → `status:sent` (overdue=11, today=4, events=3), WA entregue ✅.

**✅ Sweep de crons de baixa freq (13/07 13:37-13:46, 19 jobs diários/semanais disparados manualmente via domínio Railway — "observação de 24h" comprimida):**
- **16/19 → `200` limpo** (email-triage-aging, auto-archive-gate-eval, wa-triage-sweep, news-watchers, news-digest, daily-sync 25s, daily-clipping, index-drive-documents 270 docs, linkedin-curator/outbound, daily-synthesis, auto-resolve-editorial, raci-weekly-report, weekly-digest id17, editorial-weekly-briefing).
- **3/19 timeout de CLIENTE (120s), não quebrados** — `run-daily-ai`, `run-auto-enrich` (log confirma `>240s` processando server-side), `sync-whatsapp-history` (~43min por design). Pré-existente, não regressão.
- **Serviço saudável pós-19-disparos**: zero crash/OOM/traceback, health 200, Neon 1 conn.
- **2 achados p/ backlog (não-cutover):** (a) `sync-conselhoos-raci` erro "pessoas Vallen" (empresas OK; `CONSELHOOS_DATABASE_URL` presente → dados/lógica pré-existente); (b) jobs >180s podem exceder o read-timeout do worker → candidatos a async.

**🔧 ACHADO não-bloqueante — pool DB app-level ligado no Railway:** `database.py:144` só desliga o pool psycopg2 (`ThreadedConnectionPool maxconn=10`, código de dev) quando `VERCEL` está no env. No Railway `VERCEL` não existe → pool liga, satura sob carga consolidada, e cai em `[DB] Pool failed, falling back to direct` **em toda request**. **Não é risco**: a `DATABASE_URL` é o **pooler Neon (pgbouncer, 901 max)** e o fallback direct é justamente o modo serverless correto (Neon com 1 conn ativa). É **ruído de log + micro-latência**. Fix P3: estender a guarda pra `RAILWAY_ENVIRONMENT` (`if not VERCEL and not RAILWAY_ENVIRONMENT`) em `_create_connection`/`_return_to_pool`, OU subir `maxconn`. Baixo ROI, cosmético.

## ✅ PASSO 5 · PARTE 1 EXECUTADA — 13/07/2026 13:56-14:05 BRT

Migração dos 7 crons Vercel-only pro worker Railway (`prospect-system-imensiah`), **sem desligar o Vercel** (rollback preservado):
- Worker `main.py`: +7 jobs em `_SCHEDULER_JOBS` (health-recalc 18h, cleanup dom 4h `day_of_week="sun"`, editorial-metrics-reminder-evening 23h, group-digest 0h, platform-costs-snapshot dia2 12h, circulos-recalc 9h, wa-backfill-1to1 9h30 — tudo UTC).
- `vercel.json`: `crons: []` (removidos os 7 no mesmo commit `03f9c05`, política anti-double-fire).
- Deploy via `git push --no-verify` (pulou `sync-to-remote.sh` que empurraria banco local stale→Neon prod — prod foi modificado nesta sessão pelos crons do sweep).
- **Validação:** `cron_registry` = **50 jobs** (43+7), 7 novos presentes · worker heartbeats vivos pós-deploy · intel-api/tonia **200 durante todo o deploy (zero downtime)** · Vercel redeployado Ready com `crons=[]` (origin/main=03f9c05) → parou de disparar os 7 · webhooks Evolution seguem Railway.
- **Double-fire eliminado**; **rollback preservado** (Vercel vivo, alias `intel.almeida-prado.com` ainda mapeado no Vercel mas DNS→Railway — config órfã, some no desligamento).

### ⏸️ PASSO 5 · PARTE 2 (desligar Vercel INTEL/Tônia) — PENDENTE, deliberadamente adiada
Runbook pede "24-48h estáveis" antes. Desligar mata o rollback instantâneo do cutover; custo de manter idle ~zero. Fazer numa próxima sessão após janela estável. ConselhoOS fica no Vercel de qualquer forma.

### ROLLBACK (a qualquer momento, ~minutos — Vercel fica vivo)
Repetir Passo 1 com as URLs Vercel: intel-bot-v2 → `https://tonia.almeida-prado.com/webhooks/evolution`, rap-whatsapp → `https://intel.almeida-prado.com/api/webhooks/whatsapp`. Se mexeu no DNS: reverter os CNAMEs. Neon PITR se houver corrupção de dados.

### Ainda pra preparar na sessão de cutover (antes do Passo 1)
- Auditar env completeness dos 2 serviços Railway vs Vercel (`railway variables` — chaves, não valores).
- Retomar a config Railway parqueada em `git stash@{0}` (Procfile + `.python-version` + `.railwayignore`) e decidir se commita.
- Confirmar `EVOLUTION_API_URL`/`EVOLUTION_API_KEY` à mão (do `.env`).
