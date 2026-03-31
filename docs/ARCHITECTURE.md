# Arquitetura do Sistema ImensIAH Prospects

## Visao Geral

Sistema de gestao de prospects B2B com scoring inteligente baseado em IA.

```
                    +------------------+
                    |     Vercel       |
                    |  (Serverless)    |
                    +--------+---------+
                             |
              +--------------+--------------+
              |                             |
     +--------v--------+          +---------v--------+
     |    FastAPI      |          |   PostgreSQL     |
     |   (api/index)   |          | (Vercel Postgres)|
     +--------+--------+          +------------------+
              |
    +---------+---------+---------+---------+
    |         |         |         |         |
+---v---+ +---v---+ +---v---+ +---v---+ +---v---+
|Google | |Fathom | |Whats  | |Linked | |Claude |
|OAuth  | |  AI   | | App   | |  In   | |  AI   |
+-------+ +-------+ +-------+ +-------+ +-------+
```

## Estrutura de Diretorios

```
prospect-system/
├── api/
│   └── index.py              # Entry point Vercel
│
├── app/
│   ├── main.py               # [CORE] Todas as rotas (~85 endpoints)
│   ├── models.py             # [CORE] Schemas Pydantic
│   ├── database.py           # [CORE] Conexao PostgreSQL
│   ├── scoring.py            # Sistema de pontuacao dinamica
│   ├── auth.py               # Google OAuth
│   │
│   ├── integrations/         # APIs externas
│   │   ├── google_calendar.py
│   │   ├── google_contacts.py
│   │   ├── fathom.py
│   │   ├── linkedin.py
│   │   └── whatsapp.py
│   │
│   ├── services/             # Logica de negocio
│   │   ├── contact_dedup.py
│   │   └── linkedin_import.py
│   │
│   ├── templates/            # HTML (Jinja2)
│   └── static/               # CSS, JS
│
├── scripts/                  # Jobs e manutencao
├── docs/                     # Documentacao (NOVO)
└── data/                     # Arquivos de dados
```

## Modulos e Dependencias

### Core (Alto Acoplamento - Cuidado!)

| Arquivo | Funcao | Modificado Por |
|---------|--------|----------------|
| main.py | Rotas | TODAS features |
| models.py | Schemas | Maioria |
| database.py | DB Schema | Mudancas de tabela |

### Integracoes (Baixo Acoplamento - Seguro modificar)

| Modulo | Funcao | Independente? |
|--------|--------|---------------|
| google_calendar.py | Agendar reunioes | Sim |
| google_contacts.py | Sync contatos | Sim |
| fathom.py | Gravacao reunioes | Sim |
| linkedin.py | Enriquecimento | Sim |
| whatsapp.py | Mensagens | Sim |

### Services (Medio Acoplamento)

| Servico | Funcao | Depende de |
|---------|--------|------------|
| contact_dedup.py | Deduplicacao | models.py |
| linkedin_import.py | Import LinkedIn | models.py |
| scoring.py | Pontuacao IA | models.py, database.py |

## Fluxos Principais

### 1. Autenticacao
```
Usuario -> /login -> Google OAuth -> /auth/google/callback -> Session Cookie
```

### 2. Prospect Pipeline
```
CSV/Manual -> Pendente -> Admin Aprova -> NOVO -> Contato -> Reuniao -> Convertido
```

### 3. Scoring Dinamico
```
Prospect -> scoring.py -> Fatores (cargo, setor, dados) -> Tier A/B/C/D/E
                              ^
                              |
                    learned_weights (feedback de conversoes)
```

## Usuarios do Sistema

| Email | Role | Acesso |
|-------|------|--------|
| renato@almeida-prado.com | Admin | /admin, /rap/* |
| andressa@almeida-prado.com | Operador | /, /prospect/* |

## Endpoints por Categoria

### Autenticacao (4)
- GET /login, /logout
- GET /auth/google/login, /auth/google/callback

### UI Pages (12)
- GET /, /admin, /prospect/{id}
- GET /rap/* (6 paginas customizadas)

### API Prospects (8)
- GET/POST/PATCH /api/prospects/*
- POST /api/prospects/{id}/convert

### API Admin (4)
- GET/POST /api/admin/*

### API Integracoes (15+)
- /api/whatsapp/*
- /api/fathom/*
- /api/webhooks/*
- /api/contacts/*

## Banco de Dados

### Tabelas Principais
- `users` - Usuarios do sistema
- `prospects` - Dados de prospects + score
- `interactions` - Historico de contatos
- `meetings` - Reunioes agendadas/realizadas
- `learned_weights` - Pesos de ML para scoring

## Deploy

- **Plataforma**: Vercel (Serverless)
- **Regiao**: GRU1 (Brasil)
- **Dominio**: intel.almeida-prado.com
- **Deploy**: AUTOMATICO via GitHub push (nao precisa acao manual)
- **Cron**: Sync contatos 9h diario

### Fluxo de Deploy
```
git push origin main -> GitHub -> Vercel detecta push -> Build automatico -> Deploy em ~2min
```

## Gotchas e Conhecimento Importante

### FastAPI Route Ordering
- Rotas especificas DEVEM vir ANTES de rotas parametrizadas
- Ex: `/api/contacts/suggestions` ANTES de `/api/contacts/{contact_id}`
- Caso contrario: erro 422 (FastAPI tenta validar "suggestions" como int)

### Google OAuth Scopes
- Para sync de Tasks (leitura+escrita): usar `https://www.googleapis.com/auth/tasks`
- NAO usar `tasks.readonly` se precisar criar/atualizar tasks
- Usuario precisa reconectar conta Google apos mudanca de scope

### PostgreSQL
- Funcao `similarity()` requer extensao `pg_trgm` (nao disponivel no Vercel Postgres)
- Usar `ILIKE` para buscas fuzzy simples

### Desenvolvimento Local
- Servidor local: `uvicorn app.main:app --reload` (porta 8000)
- Hot reload automatico com WatchFiles

## Variaveis de Ambiente

```
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_CALENDAR_ID
FATHOM_API_KEY
ANTHROPIC_API_KEY
POSTGRES_URL
EVOLUTION_API_URL
EVOLUTION_API_KEY
```
