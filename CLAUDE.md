# Instruções para Claude Code

## Desenvolvimento Local

**SEMPRE** use o script `./dev.sh` para iniciar o servidor de desenvolvimento:

```bash
cd /Users/rap/prospect-system
./dev.sh
```

Isso garante:
- Banco PostgreSQL local (rápido, ~100ms)
- Porta 8000
- Hot reload ativado

## Comandos Úteis

| Comando | Descrição |
|---------|-----------|
| `./dev.sh` | Inicia servidor local |
| `./dev.sh sync` | Sincroniza banco local com produção |
| `./dev.sh setup` | Setup inicial (primeira vez) |

## NUNCA faça

- ❌ `uvicorn main:app` direto (usa banco remoto, lento)
- ❌ `python -m uvicorn` (mesmo problema)
- ❌ Conectar ao Neon para desenvolvimento (3-30s por request)

## Testar se está rápido

```bash
curl -w "%{time_total}s\n" -o /dev/null -s http://localhost:8000/api/v1/dashboard
```

- ✅ Bom: < 0.5s
- ❌ Ruim: > 3s (está usando banco remoto)

## Estrutura

```
prospect-system/
├── app/
│   ├── main.py          # 548 endpoints (~16k linhas)
│   ├── database.py      # 65 tabelas PostgreSQL
│   ├── services/        # 62 módulos de lógica de negócio
│   ├── integrations/    # Google, WhatsApp, LinkedIn, Claude
│   ├── templates/       # 35 templates HTML (Jinja2)
│   └── static/          # CSS, JS, Service Worker
├── dev.sh               # Script de desenvolvimento
├── scripts/             # Scripts auxiliares
├── docs/                # Documentação
│   ├── ARCHITECTURE.md  # Visão geral técnica
│   ├── MODULES.md       # Referência de módulos (60 services)
│   └── BACKLOG.md       # Funcionalidades pendentes
└── vercel.json          # Deploy + cron jobs
```

## Banco de Dados

- **Local**: PostgreSQL 15 em `localhost:5432/intel`
- **Produção**: Neon PostgreSQL (Vercel)

O banco local é cópia do remoto. Para atualizar: `./dev.sh sync`

## Deploy

- **Automático**: `git push origin main` → Vercel detecta → deploy em ~2min
- **Domínio**: intel.almeida-prado.com

## Gotchas

- **Modais customizados**: Usar inline styles (Bootstrap conflita com `.modal-backdrop`)
- **Upload de imagens**: Comprimir client-side (Vercel limit 4.5MB body)
- **Google OAuth**: Reconectar após mudança de scopes
- **Mensagens WhatsApp**: `outgoing` = Renato enviou, `incoming` = contato enviou
- **Health scores**: Recalculados 2x/dia (cron 5h + 18h), podem ficar stale
- **Propostas de ação**: Auto-dedup por contato+tipo em 24h, auto-resolve on reply

## APIs Externas

| API | Uso | Variável |
|-----|-----|----------|
| Claude (Anthropic) | Briefings, OCR, análises, Smart Update | ANTHROPIC_API_KEY |
| Google APIs | Calendar, Gmail, Contacts, Tasks, Drive | GOOGLE_CLIENT_ID/SECRET |
| Evolution API | WhatsApp send/receive/groups | EVOLUTION_API_URL/KEY |
| LinkdAPI | Enriquecimento LinkedIn | LINKDAPI_KEY |
| Fathom | Import reuniões → projetos/tarefas | FATHOM_API_KEY_* |

## Antes de Implementar Qualquer Feature

**OBRIGATORIO**: Consulte `docs/FEATURES.md` antes de criar qualquer funcionalidade nova.
Esse arquivo mapeia TODAS as features existentes por página, integrações, scripts e crons.

Checklist:
1. Busque em `docs/FEATURES.md` se a feature já existe
2. Grep no codebase: `grep -rn "palavra-chave" app/templates/ app/services/ app/main.py`
3. Verifique se existe endpoint similar: `grep -n "palavra" app/main.py | grep "@app"`
4. Se criar feature nova, **atualize `docs/FEATURES.md`** no mesmo commit
