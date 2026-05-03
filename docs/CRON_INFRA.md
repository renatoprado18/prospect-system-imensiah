# Infraestrutura de Crons

INTEL usa **dois orquestradores** de cron jobs em paralelo:

## 1. Vercel Cron Jobs (configurado em `vercel.json`)

- **Uso**: jobs que rodam **1x/dia** (limite do plano Hobby: 1 cron/dia por path).
- **Total atual**: 17 crons daily/weekly.
- **Vantagem**: roda no mesmo runtime do app, sem latencia extra.
- **Limitacao**: nao suporta sub-daily (hourly, 2x/dia, etc).

## 2. GitHub Actions (configurado em `.github/workflows/`)

- **Uso**: jobs que precisam rodar **mais de 1x/dia**.
- **Mecanismo**: workflow agendado dispara `curl` com `Authorization: Bearer $CRON_SECRET` pro endpoint Vercel correspondente.
- **Endpoints**: continuam em `app/main.py` — GH Actions so triggera.

### Workflows ativos

| Workflow | Frequencia | Endpoint |
|---|---|---|
| `cron-auto-publish-linkedin.yml` | 12:05 e 15:05 UTC (9h05 e 12h05 BRT) | `/api/cron/auto-publish-linkedin` |
| `cron-auto-collect-metrics.yml` | hourly (top of hour UTC) | `/api/cron/auto-collect-linkedin-metrics` |

### Trigger manual

GitHub repo -> aba **Actions** -> selecione o workflow -> botao **Run workflow** (verde, top-right).

### Setup inicial (uma vez)

1. **GitHub repo Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**
2. Nome: `CRON_SECRET`
3. Valor: o mesmo valor da env var `CRON_SECRET` configurada na Vercel (Project Settings -> Environment Variables)

### Custo

- Free tier GitHub Actions: 2000 min/mes (repos privados).
- Cada run = ~10-30s (curl + retry).
- Hourly = ~720 runs/mes * 30s = ~6h = 360 min/mes -> dentro do free tier folgado.

### Caveats

- **Latencia de schedule**: GH Actions pode atrasar 5-15min em horarios de pico (cron `0 * * * *` pode rodar `0:08`). OK pro nosso caso (auto-publish tolera atraso, metrics coletam janelas amplas).
- **Falhas silenciosas**: se o secret estiver errado, workflow falha com HTTP 401. Vai notificar via email (config GitHub).
- **Auth**: `verify_cron_auth` em `app/main.py:7396` aceita Bearer token OU User-Agent `vercel-cron/`.
