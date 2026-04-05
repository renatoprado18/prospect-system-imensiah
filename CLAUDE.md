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
├── app/           # Código FastAPI
├── dev.sh         # Script de desenvolvimento
├── scripts/       # Scripts auxiliares
└── docs/          # Documentação
```

## Banco de Dados

- **Local**: PostgreSQL 15 em `localhost:5432/intel`
- **Produção**: Neon PostgreSQL (Vercel)

O banco local é cópia do remoto. Para atualizar: `./dev.sh sync`
