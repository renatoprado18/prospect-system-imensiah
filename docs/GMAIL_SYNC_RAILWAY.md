# Gmail Sync via Railway Worker

> **Feature**: Migracao do `sync-gmail` pra Railway worker (chunked + resumivel)
> **Data**: 2026-05-02 / 2026-05-03 (debug em prod)
> **Smoke validado**: job 18 completou em 43min, 7152 contatos, 0 errors
> **Commits**: `8bb3157`, `7154942`, `e2453f3`, `17e0022`, `87889ce`, `7d56d2d`, `373ee19`, `b874fe6`, `f42d2fe`, `3abe847`

## Por que existe

`services/gmail_sync.py:sync_all_contacts` faz O(N x M):
- ~3.5k contatos com email x 2 contas Google x ate 3 emails = ~21k chamadas Gmail API
- Cada email: 2 calls (`list_messages` + `get_message`)
- `await asyncio.sleep(0.1)` entre cada -> piso ~2100s
- **Vercel mata em 300s.** No smoke test rodava 290s e processava ~10% antes do timeout.

Como `daily-sync` chama `step_gmail` em paralelo via `asyncio.gather`, quando o gmail trava ele mata o daily-sync inteiro.

## Arquitetura (V7 — final que funcionou)

```
Vercel cron (/api/cron/sync-gmail ou step_gmail no daily-sync)
   |
   |-- enqueue_job('gmail_sync', payload, '/sync-gmail')
   |     |-- INSERT background_jobs (status='queued')
   |     |-- POST {AUDIO_WORKER_URL}/sync-gmail (fire-and-forget, timeout 8s)
   |
   `-- return 200 {"queued": true, "job_id": N}

Railway worker (workers/audio-transcriber/main.py)
   |
   |-- POST /sync-gmail
   |     |-- valida WORKER_SECRET
   |     |-- idempotencia atomica: UPDATE WHERE status='queued' RETURNING id
   |     |-- BackgroundTasks.add_task(_run_gmail_sync_loop) -> 202 imediato
   |
   `-- _run_gmail_sync_loop (max 50 iters):
         while not done:
           _run_gmail_sync_chunk(job_id, months_back)
           sleep(0.1)  # yield to event loop
           check status from DB (completed/error/skipped exits)

       _run_gmail_sync_chunk:
         |-- load state (cursor + stats from result jsonb)
         |-- if first time: count contacts × accounts -> total_items
         |-- if no current_account: pick next not-done -> set current
         |-- refresh token (capture error reason em error_samples)
         |-- SELECT contatos WHERE id > last_contact_id LIMIT 300
         |-- if 0 rows: account exhausted, force=True save cursor
         |-- else: process emails (count, update contacts ultimo_contato)
         |-- save_state: UPDATE WHERE processed_items < new (optimistic)
         |-- if all accounts done: mark status='completed'
```

### Decisoes nao-obvias

1. **Chunks de 300 contatos**: balance entre tempo de loop (1-2min/chunk) e robustez a crashes.
2. **Optimistic concurrency check** no save_state (`WHERE processed_items < new`): previne regressao de progresso se duas tasks paralelas tentarem escrever.
3. **`force=True` em transicoes de conta**: quando account esgota, processed_items nao avanca (so cursor muda); precisa pular o check otimista pra nao ficar preso.
4. **Loop interno na BackgroundTask** (V7): tentamos antes (a) HTTP self-dispatch (V3-V4) — perdia mensagens; (b) asyncio.create_task in-process (V5-V6) — task GC ou cancelamento. Loop dentro da mesma BackgroundTask elimina a transicao entre chunks.
5. **Cursor jsonb**: `{accounts_done: [], current_account_id: N, last_contact_id: M}` permite retomar de qualquer ponto se Railway matar o processo.

## Arquivos modificados

| Arquivo | O que mudou |
|---|---|
| `app/services/job_dispatcher.py` (novo, 155 linhas) | `enqueue_job(job_type, payload, dispatch_path)` + `get_job_status(job_id)` |
| `app/main.py:7353` | `step_gmail` no `daily-sync` agora enfileira (era await direto) |
| `app/main.py:7719` | `cron_sync_gmail` standalone agora enfileira |
| `app/main.py:7752` | Novo endpoint `GET /api/jobs/{id}` pra status |
| `workers/audio-transcriber/main.py:1972` | Novo endpoint `POST /sync-gmail` + funcao `_run_gmail_sync` portando logica de `gmail_sync.py:200-333` |

`services/gmail_sync.py` continua existindo (legacy) — apenas os crons trocaram a chamada.

## Variaveis de ambiente

**Vercel** (prospect-system):
- `AUDIO_WORKER_URL` — URL publica do worker no Railway (mesma URL serve transcribe, image, sync-gmail)
- `WORKER_SECRET` — secret compartilhado

**Railway** (prospect-system-imensiah, root: `workers/audio-transcriber/`):
- `WORKER_SECRET` — mesmo do Vercel
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — **CRITICO**: precisa ser o MESMO OAuth client do Vercel (refresh tokens foram emitidos pra esse client_id). Sem isso, `_refresh_gmail_token_full` retorna `http_401: invalid_client`.
- `DATABASE_URL` — Neon Postgres (mesmo do Vercel)
- `RAILWAY_PUBLIC_DOMAIN` — auto-injected pelo Railway, nao precisa configurar

**Nao precisa mais**: `AUDIO_WORKER_URL` no Railway (V5+ usa loop interno, sem self-dispatch).

## Schema `background_jobs`

Tabela ja existia (`database.py:1527`). Nada novo. Estrutura usada:

| Coluna | Uso |
|---|---|
| `job_type` | `'gmail_sync'` |
| `status` | `queued` -> `running` -> `completed` / `error` / `skipped` |
| `total_items` | numero de contatos processados |
| `processed_items` | progresso atualizado a cada 50 |
| `success_count` / `failed_count` | stats por contato |
| `result` jsonb | payload inicial + stats finais (imported, updated, errors) |
| `error` | mensagem se status=error |
| `started_at` / `completed_at` | timestamps |

## Como testar

### 1. Smoke local (rapido)

Pre-req: `./dev.sh` rodando.

```bash
# Disparar o cron (precisa do header de auth do cron)
curl -s "http://localhost:8000/api/cron/sync-gmail?cron_secret=$(grep CRON_SECRET .env.local | cut -d= -f2)"
```

Deve retornar em < 100ms:
```json
{"job": "sync-gmail", "queued": true, "job_id": 123, "dispatched": false, "error": "worker_status_404"}
```

`dispatched: false` localmente e esperado — o worker do Railway nao esta acessivel da sua maquina (ou `AUDIO_WORKER_URL` nao tem `/sync-gmail` deployado ainda). O importante e:
1. Endpoint **retorna rapido** (sem timeout)
2. Cria registro em `background_jobs`

Conferir no banco:
```bash
psql -h localhost -d intel -c "SELECT id, job_type, status, started_at, error FROM background_jobs WHERE job_type='gmail_sync' ORDER BY id DESC LIMIT 5"
```

### 2. Smoke endpoint de status

```bash
curl -s http://localhost:8000/api/jobs/123 | jq
```

Deve retornar JSON com status do job. Se ID nao existe, 404.

### 3. Validacao em prod (apos deploy)

Vercel deploya em ~2min apos `git push origin main`. Railway tambem (auto via watch da pasta `workers/audio-transcriber/`).

**Disparar manual em prod:**
```bash
curl -s "https://intel.almeida-prado.com/api/cron/sync-gmail?cron_secret=$CRON_SECRET"
```

Esperado: HTTP 200 em < 1s, com `dispatched: true`.

**Acompanhar progresso:**
```bash
# Pegar job_id da resposta acima, depois:
curl -s "https://intel.almeida-prado.com/api/jobs/$JOB_ID" | jq

# Ou via SQL em prod:
# status evolui queued -> running -> completed (~5-10min)
# processed_items cresce de 50 em 50
```

**Validar no dashboard de cron health:**
- `https://intel.almeida-prado.com/api/admin/cron-health` (auth admin)
- Pill no dashboard deve continuar verde — agora o cron retorna 200 instantaneo em vez de timeout 504.

### 4. Validar daily-sync nao morre mais

Cron `daily-sync` roda 5h UTC (= 02h BRT). Apos primeiro ciclo:

```bash
psql -h $NEON_URL -c "SELECT * FROM cron_runs WHERE cron_name='daily-sync' ORDER BY started_at DESC LIMIT 3"
```

Esperado: `status='healthy'`, `duration_ms < 30000` (era timeout 300s antes).

E em paralelo:
```bash
psql -h $NEON_URL -c "SELECT id, status, processed_items, total_items, EXTRACT(EPOCH FROM (completed_at - started_at)) as secs FROM background_jobs WHERE job_type='gmail_sync' ORDER BY id DESC LIMIT 3"
```

Deve aparecer 1 job por dia, completed em ~2000-3000s (Railway sem timeout).

## Edge cases tratados

- **Worker URL ausente**: `enqueue_job` cria registro mas marca `error='AUDIO_WORKER_URL not configured'` imediato
- **Worker offline / 5xx**: marca `error=worker_status_NNN`, status='error'
- **Dispatch timeout (8s)**: NAO marca erro — o worker pode ter recebido mesmo assim. Status fica `queued` ate o worker assumir
- **Job duplicado**: worker valida `WHERE status='running' AND started_at > NOW() - INTERVAL '1 hour'`. Se ja tem rodando, marca o novo como `skipped`
- **Token Gmail expirado**: `_refresh_gmail_token_full` no worker reusa logica do INTEL — refresh automatico via OAuth refresh_token

## Reverter (se necessario)

`gmail_sync.py` continua intacto. Pra voltar ao comportamento antigo:

1. Em `app/main.py:7353` (`step_gmail`), substituir por:
   ```python
   from services.gmail_sync import get_gmail_sync_service
   return await get_gmail_sync_service().sync_all_contacts(months_back=1)
   ```
2. Mesmo em `app/main.py:7719` (`cron_sync_gmail`)
3. Push

Isso reativa o timeout — so faz se descobrir bug critico no worker.

## Padrao reutilizavel

`enqueue_job()` foi desenhado pra ser reutilizado. Pra qualquer cron que estoure 300s:

```python
from services.job_dispatcher import enqueue_job

job_id, dispatched, error = await enqueue_job(
    job_type='whatsapp_history_full',
    payload={'days': 90},
    dispatch_path='/sync-whatsapp-history',
)
```

E adicionar endpoint correspondente no worker que valida `WORKER_SECRET` e faz `BackgroundTasks.add_task`.

Candidatos: WhatsApp full history sync, refetch de avatares em massa, recalc health para >5k contatos.

## Postmortem do debug em prod (jobs 2-18)

Tomou ~17 jobs e 7 versoes do worker pra chegar no padrao funcional. Resumo dos becos sem saida:

| Versao | Pattern | Que falhou | Aprendizado |
|---|---|---|---|
| V1 (`8bb3157`) | Monolitico em BackgroundTask | Vercel timeout 300s | Esperado |
| V2 (`7d56d2d`) | Chunks 300, HTTP self-dispatch | Continuation HTTP perdia mensagens apos 4-5 chunks | Railway/proxy intermitente |
| V3 (`373ee19`) | + optimistic concurrency | Mesma falha de V2 | Continuation ainda problemática |
| V4 (`b874fe6`) | + force=True em transicoes | Mesma falha | |
| V5 (`a8a2a92`) | asyncio.create_task in-process | Tasks paravam apos chunk 1 | GC ou cancelamento |
| V6 (`f42d2fe`) | + ref salvo em set global | Tasks ainda paravam | Nao era GC; FastAPI/Starlette cancela tasks filhas no fim do BackgroundTask |
| V7 (`3abe847`) | **Loop dentro da BackgroundTask** | ✅ Completou 7152/7152 em 43min | |

**Outras issues uteis**:
- `GOOGLE_CLIENT_ID/SECRET` nao estavam no Railway env (sd worker, novo dependencia)
- Job 14 nunca rodou seu chunk (BackgroundTask spawnou mas nao chegou ao chunk start log) — Railway transient, retentar resolveu
- `RAILWAY_PUBLIC_DOMAIN` env var injectada pelo Railway é util pra self-URLs (V3-V4 usavam, V7 dispensa)

**Ferramentas que ajudaram**:
- `/version` endpoint pra confirmar build deployada
- `WORKER_BUILD` constant atualizada a cada versao (assim a gente nao confunde "redeploy ok" com "deploy efetivo")
- `error_samples` em `result.stats` pra capturar causa real de token failures
- `cursor` jsonb com `current_account_id` + `last_contact_id` pra retomar de qualquer ponto
