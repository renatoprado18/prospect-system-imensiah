# Claude Code Delegator

Worker que executa tarefas complexas em nome da Tonha via **Claude Code headless**. Tonha (Haiku, rápida) dispatcha aqui quando detecta tarefa que requer Bash/Read/Edit/Agent/etc.

## Como funciona

```
WhatsApp msg → INTEL → Tonha
  ├─ tarefa simples → responde direto (Haiku)
  └─ tarefa complexa → delegate_to_claude_code(task, context)
                           ↓
                        Railway: este worker
                           ↓
                  spawn `claude` headless via SDK
                           ↓
                  Claude Code com tools completos roda
                           ↓
                  retorna resultado pra Tonha → user
```

## Deploy no Railway

### 1. Criar novo serviço

No projeto `intel` no Railway:
- Add Service → GitHub Repo → `renatoprado18/prospect-system-imensiah`
- Root Directory: `workers/claude-code-delegator`
- Start Command: deixar vazio (usa Procfile)

### 2. Env vars obrigatórias

```
CLAUDE_CODE_OAUTH_TOKEN=<seu token>
WORKER_SECRET=<igual ao do audio worker — mesmo valor do env WORKER_SECRET, sem default>
REPO_PATH=/app/repo
PORT=3000
```

Opcionais:
```
MAX_DURATION_MS=300000        # 5min default
DAILY_CALL_CAP=50             # cap de chamadas/dia
```

### 3. Como obter `CLAUDE_CODE_OAUTH_TOKEN`

Pelo seu laptop (onde Claude Code está logado):

```bash
claude /login   # se ainda não estiver logado
# Depois, gerar token de longa duração:
claude setup-token
# Copiar o token retornado pro Railway env
```

Token expira em ~1 ano. Refresh manual quando expirar.

### 4. Clone do INTEL repo dentro do worker

O worker precisa do repo INTEL em `/app/repo` pra Claude Code ter contexto. 2 opções:

**Opção A (simples): clone no deploy** — adicionar ao build:
```dockerfile
RUN git clone --depth 1 https://github.com/renatoprado18/prospect-system-imensiah.git /app/repo
```

**Opção B (atualizado): pull a cada chamada** — mais lento, mas sempre fresh. Implementar em server.js antes de cada delegate.

Pra MVP, vai de A.

### 5. Validar deploy

```bash
curl https://claude-code-delegator-production.up.railway.app/health
# {"status":"ok","oauth_configured":true,"secret_configured":true,...}
```

Teste manual:
```bash
curl -X POST https://claude-code-delegator-production.up.railway.app/delegate \
  -H "x-delegator-secret: $WORKER_SECRET" \
  -H "content-type: application/json" \
  -d '{"task":"liste os 5 arquivos mais recentes em app/services/ por mtime","mode":"investigate"}'
```

Resposta deve incluir os arquivos via Bash do Claude Code.

## Integração com Tonha

Após worker rodando, a Tonha precisa de uma action nova `delegate_to_claude_code` em `app/services/intel_bot.py` (execute_action). Documentada em `docs/CLAUDE_CODE_DELEGATION.md` (futuro).

## Segurança

- `WORKER_SECRET` obrigatório no header
- Modos:
  - `investigate` (default): read-only, sem edit
  - `edit`: pode editar arquivos, sem push direto pra main
  - `full`: bypassa permissões (USAR COM CUIDADO)
- Cap diário de chamadas
- Timeout por chamada (default 5min)
- Logs de cada chamada (tool_count, cost, duration)

Plano de futuro:
- Worktree isolado por chamada (atualmente todas usam `/app/repo`)
- Persistir counter em Redis (atualmente in-memory, reseta no restart)
- Audit log em DB (atualmente só console.log)

## Custo

Usa seu plano Claude Code (subscription Max $200). Cada chamada conta como uso normal de Claude Code — você pode acompanhar em claude.ai/settings.

Se passar do plano, cai pra pay-per-token via OAuth token. Cuidado com loops.
