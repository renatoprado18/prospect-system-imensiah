# Arquitetura do Sistema INTEL

## Visao Geral

Assistente pessoal inteligente para gestao de relacionamentos, projetos, veiculos e financas pessoais. Combina CRM pessoal com IA (Claude) para analises, briefings e automacoes.

**Dominio**: `intel.almeida-prado.com`

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
     |   (524 endpoints)|         |    (Neon, 65     |
     +--------+--------+          |     tabelas)     |
              |                   +------------------+
    +---------+---------+---------+---------+
    |         |         |         |         |
+---v---+ +---v---+ +---v---+ +---v---+ +---v---+
|Google | |Evolu  | |Claude | |Linked | |Google |
|APIs   | |tion   | |  AI   | |  In   | |Drive  |
+-------+ +-------+ +-------+ +-------+ +-------+
 Calendar   WhatsApp   Vision    Enrich    Storage
 Gmail      Messages   Analysis  Profiles  Documents
 Contacts   Groups     Briefings
 Tasks      Webhooks   OCR
```

## Estrutura de Diretorios

```
prospect-system/
├── api/
│   └── index.py              # Entry point Vercel
├── app/
│   ├── main.py               # [CORE] 524 endpoints (~16k linhas)
│   ├── database.py           # [CORE] 65 tabelas PostgreSQL
│   ├── auth.py               # Google OAuth + sessions
│   │
│   ├── integrations/         # APIs externas
│   │   ├── google_calendar.py
│   │   ├── google_contacts.py
│   │   ├── google_drive.py
│   │   ├── google_tasks.py
│   │   ├── gmail.py
│   │   ├── evolution_api.py  # WhatsApp (Evolution API)
│   │   ├── whatsapp.py       # WhatsApp helpers
│   │   └── linkedin.py
│   │
│   ├── services/             # 60 modulos de logica de negocio
│   │   ├── # -- Relacionamentos --
│   │   ├── circulos.py       # Circulos de proximidade (C1-C5)
│   │   ├── briefings.py      # Briefings inteligentes pre-reuniao
│   │   ├── contact_enrichment.py
│   │   ├── contact_dedup.py
│   │   ├── contact_intelligence.py
│   │   ├── timeline.py
│   │   ├── search.py
│   │   │
│   │   ├── # -- Projetos --
│   │   ├── projects.py       # CRUD projetos + milestones
│   │   ├── project_enrichment.py
│   │   ├── project_smart_update.py  # Smart Update: analisa msgs → atualiza tarefas
│   │   ├── payment_cycle.py  # Ciclo financeiro automatizado
│   │   │
│   │   ├── # -- Comunicacao --
│   │   ├── whatsapp_sync.py  # Sync WhatsApp individual + grupos
│   │   ├── gmail_sync.py     # Sync Gmail
│   │   ├── inbox.py          # Inbox unificado
│   │   ├── realtime_analyzer.py  # Analise de intencao em msgs
│   │   ├── smart_fup.py      # Follow-up inteligente automatico
│   │   │
│   │   ├── # -- IA & Automacao --
│   │   ├── ai_agent.py       # Orquestrador de sugestoes AI
│   │   ├── action_proposals.py   # Propostas de acao (dedup, auto-resolve)
│   │   ├── action_executor.py
│   │   ├── smart_triggers.py
│   │   ├── health_predictions.py
│   │   ├── message_suggestions.py
│   │   ├── digest_generator.py
│   │   │
│   │   ├── # -- Conteudo --
│   │   ├── editorial_calendar.py
│   │   ├── hot_takes.py
│   │   ├── news_hub.py
│   │   ├── campaign_service.py
│   │   │
│   │   ├── # -- Veiculos --
│   │   ├── veiculos.py       # Manutencao + upload NF via foto/OCR
│   │   ├── oficinas.py       # Cadastro de oficinas
│   │   │
│   │   ├── # -- Integracao --
│   │   ├── conselhoos_sync.py # Sync com ConselhoOS
│   │   ├── linkedin_enrichment.py
│   │   ├── calendar_sync.py
│   │   ├── tasks_sync.py
│   │   └── ... (60 arquivos total)
│   │
│   ├── templates/            # 34 templates HTML (Jinja2)
│   └── static/               # CSS, JS, Service Worker
│
├── scripts/                  # Jobs e manutencao
├── docs/                     # Documentacao
├── dev.sh                    # Script de desenvolvimento local
└── vercel.json               # Config deploy + crons
```

## Modulos Principais

### Relacionamentos (CRM)
| Modulo | Funcao |
|--------|--------|
| circulos.py | Classificacao C1-C5 por proximidade + health score |
| briefings.py | Briefings pre-reuniao com Claude AI |
| contact_enrichment.py | Enriquecimento via LinkedIn, web search |
| contact_dedup.py | Deteccao e merge de duplicatas |
| dashboard.py | API unificada do dashboard |

### Projetos
| Modulo | Funcao |
|--------|--------|
| projects.py | CRUD projetos, milestones, tarefas, notas |
| project_smart_update.py | Analisa msgs → sugere completar tarefas + cria novas |
| payment_cycle.py | Ciclo financeiro: email cobranca → detecta pagamento → abre ciclo |
| project_enrichment.py | Enriquecimento de projetos com IA |

### Comunicacao
| Modulo | Funcao |
|--------|--------|
| whatsapp_sync.py | Sync WhatsApp individual + grupos vinculados |
| gmail_sync.py | Sync Gmail com contatos |
| realtime_analyzer.py | Detecta intencao em mensagens (urgente, reuniao, etc) |
| action_proposals.py | Propostas de acao com dedup e auto-resolve |
| smart_fup.py | Follow-up automatico para emails sem resposta |

### Veiculos
| Modulo | Funcao |
|--------|--------|
| veiculos.py | Controle manutencao + upload NF via foto com OCR |
| oficinas.py | Cadastro de oficinas e servicos |

### Conteudo
| Modulo | Funcao |
|--------|--------|
| editorial_calendar.py | Calendario editorial multi-plataforma |
| hot_takes.py | Hot takes para LinkedIn |
| news_hub.py | Agregador de noticias relevantes |
| campaign_service.py | Campanhas de comunicacao |

## Deploy

- **Plataforma**: Vercel (Serverless)
- **Regiao**: IAD1
- **Dominio**: intel.almeida-prado.com
- **Deploy**: AUTOMATICO via `git push origin main` (~2min)
- **Banco Local**: PostgreSQL 15 em localhost:5432/intel
- **Banco Producao**: Neon PostgreSQL

### Cron Jobs (vercel.json)
| Schedule | Job | Funcao |
|----------|-----|--------|
| 0 5 * * * | daily-sync | Health recalc + Contacts + Calendar + Tasks + Gmail + WhatsApp + AI suggestions + Campaigns + Smart FUP + Payment cycle check |
| 0 18 * * * | health-recalc | Recalculo de health scores |
| 0 8 * * 1 | weekly-digest | Digest semanal |
| 0 4 * * 0 | cleanup | Expirar propostas + limpar notificacoes |

## Gotchas Importantes

1. **FastAPI Route Ordering**: Rotas especificas ANTES de parametrizadas
2. **PostgreSQL Vercel**: `similarity()` NAO disponivel, usar `ILIKE`
3. **Google OAuth Scopes**: Reconectar apos mudanca de scope
4. **Claude API 529/400**: Implementar retry + verificar creditos
5. **Vercel Body Limit**: ~4.5MB max, comprimir imagens client-side
6. **WhatsApp Groups**: `@g.us` descartado por padrao, habilitar por projeto
7. **Modal CSS**: Bootstrap conflita com custom modals, usar inline styles
8. **Health Score Stale**: Recalcular 2x/dia via cron (5h + 18h)

## Variaveis de Ambiente

```
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
ANTHROPIC_API_KEY
POSTGRES_URL
EVOLUTION_API_URL
EVOLUTION_API_KEY
EVOLUTION_INSTANCE
CONSELHOOS_DATABASE_URL
CRON_SECRET
```
