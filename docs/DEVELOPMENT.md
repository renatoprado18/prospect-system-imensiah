# INTEL CRM - Guia de Desenvolvimento

## Setup Inicial (apenas primeira vez)

```bash
# 1. Clonar o repositório
git clone <repo-url>
cd prospect-system

# 2. Executar setup (instala PostgreSQL, dependências)
./dev.sh setup

# 3. Sincronizar dados de produção para banco local
./dev.sh sync
```

## Desenvolvimento Diário

```bash
# Iniciar servidor de desenvolvimento
./dev.sh

# Servidor estará em http://localhost:8000
```

## Comandos Disponíveis

| Comando | Descrição |
|---------|-----------|
| `./dev.sh` | Inicia servidor local (porta 8000) |
| `./dev.sh sync` | Sincroniza banco local com produção (Neon) |
| `./dev.sh setup` | Setup inicial (dependências, PostgreSQL) |

## Arquitetura de Banco de Dados

### Desenvolvimento (Local)
- **PostgreSQL 15** rodando localmente via Homebrew
- Conexão: `postgresql://rap@localhost:5432/intel`
- **Vantagem**: ~200ms de resposta (vs 3-30s remoto)

### Produção (Vercel)
- **Neon PostgreSQL 17** (serverless)
- Conexão via `POSTGRES_URL` no `.env`
- Deploy automático via Vercel

### Sincronização

O banco local é uma **cópia** do banco de produção. Para atualizar:

```bash
./dev.sh sync
```

Isso executa:
1. `pg_dump` do Neon (schema + dados)
2. Recria banco local
3. Importa tudo

**Nota**: Mudanças feitas localmente NÃO são enviadas para produção.

## Estrutura do Projeto

```
prospect-system/
├── app/                    # Código principal
│   ├── main.py            # FastAPI app
│   ├── database.py        # Conexão com banco
│   ├── services/          # Lógica de negócio
│   ├── templates/         # HTML (Jinja2)
│   └── integrations/      # Google, WhatsApp, etc.
├── scripts/
│   └── sync-local-db.sh   # Script de sincronização
├── dev.sh                 # Script de desenvolvimento
├── .env                   # Variáveis de ambiente (não commitar)
└── docs/
    └── DEVELOPMENT.md     # Este arquivo
```

## Variáveis de Ambiente

Copie `.env.example` para `.env` e configure:

```bash
# Banco de dados (Neon - produção)
POSTGRES_URL="postgresql://..."

# Google APIs
GOOGLE_CLIENT_ID="..."
GOOGLE_CLIENT_SECRET="..."

# Anthropic (Claude AI)
ANTHROPIC_API_KEY="..."
```

## Troubleshooting

### Servidor lento (>3s por request)
Verifique se está usando banco local:
```bash
# Deve mostrar USE_LOCAL_DB=1
ps aux | grep uvicorn
```

Se não, reinicie com `./dev.sh`

### PostgreSQL não inicia
```bash
brew services restart postgresql@15
```

### Banco local vazio
```bash
./dev.sh sync
```

### Erro de conexão com Neon
Verifique `POSTGRES_URL` no `.env`

## Performance

| Ambiente | Tempo médio API |
|----------|-----------------|
| Local (PostgreSQL) | ~200ms |
| Remoto (Neon) | 3-30s |

O banco local é **15-150x mais rápido** para desenvolvimento.
