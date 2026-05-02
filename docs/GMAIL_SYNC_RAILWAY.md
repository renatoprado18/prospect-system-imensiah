# Gmail Sync via Railway Worker

> **Feature**: Migracao do `sync-gmail` pra Railway worker
> **Data**: 2026-05-02
> **Commits**: `8bb3157`, `7154942`, `e2453f3`, `17e0022`

## Por que existe

`services/gmail_sync.py:sync_all_contacts` faz O(N x M):
- ~3.5k contatos com email x 2 contas Google x ate 3 emails = ~21k chamadas Gmail API
- Cada email: 2 calls (`list_messages` + `get_message`)
- `await asyncio.sleep(0.1)` entre cada -> piso ~2100s
- **Vercel mata em 300s.** No smoke test rodava 290s e processava ~10% antes do timeout.

Como `daily-sync` chama `step_gmail` em paralelo via `asyncio.gather`, quando o gmail trava ele mata o daily-sync inteiro.

## Arquitetura

```
Vercel cron (/api/cron/sync-gmail ou step_gmail no daily-sync)
   |
   |-- enqueue_job('gmail_sync', payload, '/sync-gmail')
   |     |-- INSERT background_jobs (status='queued')
   |     |-- POST {AUDIO_WORKER_URL}/sync-gmail (fire-and-forget, timeout 8s)
   |     |-- return (job_id, dispatched, error)
   |
   `-- return 200 {"queued": true, "job_id": N}  (em ~50ms)

Railway worker (workers/audio-transcriber/main.py)
   |
   |-- POST /sync-gmail
   |     |-- valida WORKER_SECRET
   |     |-- idempotencia: se job 'gmail_sync' running ha < 1h, marca skipped e aborta
   |     |-- BackgroundTasks.add_task(_run_gmail_sync) -> retorna 202 imediato
   |
   `-- _run_gmail_sync(job_id, months_back):
         |-- UPDATE status='running'
         |-- loop contatos (com checkpoint a cada 50: processed_items, result jsonb)
         |-- por email: refresh_gmail_token, list_messages, get_message metadata
         |-- UPDATE contacts SET ultimo_contato, total_interacoes
         `-- UPDATE status='completed' (ou 'error', error=str(e))
```

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

Ja estao configuradas em prod (Vercel + Railway):

- `AUDIO_WORKER_URL` — URL publica do worker no Railway. Mantido o nome por compat (mesma URL serve transcribe, image, sync-gmail, etc)
- `WORKER_SECRET` — secret compartilhado pra autenticar dispatch -> worker

Nao foi adicionada nova var.

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
