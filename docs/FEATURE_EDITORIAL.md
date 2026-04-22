# Feature: Editorial Calendar & LinkedIn Publishing

## Visão Geral

Sistema de gestão de conteúdo para LinkedIn com pipeline: importação de artigos → análise IA → adaptação → agendamento → publicação → métricas.

**Domínio**: `intel.almeida-prado.com/editorial`

## Estado Atual (22/04/2026)

| Métrica | Valor |
|---------|-------|
| Total de posts | 147 |
| Drafts | 141 (96%) |
| Publicados | 6 (4%) |
| Agendados | 0 |
| Canal | LinkedIn (único) |
| Tipos | repost (141), hot_take (5+1) |
| Hot Takes | 21 (2 publicados, 3 agendados, 16 drafts) |

**Diagnóstico**: Pipeline engasgado entre draft → agendamento. 141 posts prontos mas nenhum agendado. Taxa de conversão draft→publicado: 4%.

## Arquitetura

### Tabelas
- `editorial_posts` — Posts editoriais (37 colunas incluindo AI analysis)
- `hot_takes` — Hot takes para LinkedIn

### Service
- `app/services/editorial_calendar.py` — CRUD, filtros, calendário
- `app/services/hot_takes.py` — Geração de hot takes com IA
- `app/services/news_hub.py` — Feed de notícias (RSS, parcialmente funcional)

### Endpoints (17)
```
GET  /api/editorial                    # Listar posts com filtros
GET  /api/editorial/stats              # Estatísticas gerais
GET  /api/editorial/pending-tasks      # Posts pendentes de ação
GET  /api/editorial/calendar/{y}/{m}   # Calendário mensal
GET  /api/editorial/meta               # Metadata (canais, tipos, categorias)
GET  /api/editorial/ai/stats           # Stats da análise IA
GET  /api/editorial/ai-top-suggestions # Top sugestões para publicar
GET  /api/editorial/dashboard-tasks    # Widget do dashboard (hoje + próximos)
GET  /api/editorial/funnel             # Funil draft→scheduled→published
POST /api/editorial/ai/analyze         # Analisar post com IA
GET  /api/editorial/{id}/analysis      # Ver análise IA de um post
GET  /api/editorial/{id}               # Detalhe do post
POST /api/editorial                    # Criar post
PUT  /api/editorial/{id}               # Atualizar post
DELETE /api/editorial/{id}             # Deletar post
POST /api/editorial/{id}/schedule      # Agendar publicação
POST /api/editorial/{id}/publish       # Publicar manualmente
POST /api/editorial/bulk-schedule      # Agendar múltiplos
POST /api/editorial/import             # Importar artigos
```

### Templates
- `app/templates/editorial.html` — Página principal (calendário, lista, editor)
- `app/templates/hot_takes.html` — Hot takes
- Widget no dashboard (`rap_dashboard.html` → "LinkedIn Hoje")

## Pipeline de Conteúdo

```
1. IMPORTAÇÃO
   ├── Manual: criar post em /editorial
   ├── Import: POST /api/editorial/import (URL ou dados)
   └── Hot Take: POST /api/hot-takes/from-url (gera take de notícia)

2. ANÁLISE IA (Claude)
   ├── Categoria, subcategoria, público-alvo
   ├── Complexidade, tempo de leitura
   ├── Score de relevância (0-100)
   ├── Keywords, hashtags sugeridas
   ├── Gancho para LinkedIn
   └── Evergreen ou temporal

3. ADAPTAÇÃO
   ├── titulo_adaptado: versão LinkedIn do título
   ├── conteudo_adaptado: texto para o post
   ├── hashtags: tags sugeridas pela IA
   └── ai_gancho_linkedin: hook para engajamento

4. AGENDAMENTO
   ├── data_publicacao: quando publicar
   ├── Calendario visual mensal
   └── Bulk schedule: agendar múltiplos de uma vez

5. PUBLICAÇÃO
   ├── Manual: marcar como publicado + URL
   ├── url_publicado: link do post no LinkedIn
   └── linkedin_post_url: URL direta

6. MÉTRICAS
   ├── metricas: JSONB (impressões, likes, comentários)
   ├── linkedin_impressoes: campo dedicado
   └── Acompanhamento de performance
```

## Campos IA do Post

| Campo | Tipo | Descrição |
|-------|------|-----------|
| ai_categoria | text | Governança, Tecnologia, Estratégia, etc |
| ai_subcategoria | text | Subcategoria específica |
| ai_publico_alvo | JSONB | Lista de personas alvo |
| ai_tipo_conteudo | text | Artigo, opinião, estudo de caso, etc |
| ai_complexidade | text | Básico, intermediário, avançado |
| ai_evergreen | bool | Conteúdo atemporal ou temporal |
| ai_keywords | JSONB | Palavras-chave extraídas |
| ai_gancho_linkedin | text | Hook sugerido para o post |
| ai_tempo_leitura | int | Minutos estimados |
| ai_score_relevancia | int | 0-100, relevância para o público |
| ai_analise_completa | JSONB | Análise completa em JSON |
| ai_analisado_em | timestamp | Data da última análise |

## KPIs para Acompanhamento

### Pipeline
- **Taxa de conversão draft→published**: 4% (meta: >20%)
- **Drafts sem análise IA**: verificar quantos foram analisados
- **Posts agendados esta semana**: 0 (meta: 3-5/semana)
- **Cadência de publicação**: 6 posts em ~1 mês (meta: 3-4/semana)

### Engajamento (quando publicados)
- **Impressões médias por post**: TBD
- **Taxa de engajamento**: TBD (likes+comentários/impressões)
- **Melhor dia/horário**: TBD
- **Melhores hashtags**: TBD
- **Tipo mais engajante**: hot_take vs repost

### Qualidade
- **Score IA médio**: TBD
- **Posts evergreen vs temporais**: TBD
- **Distribuição por categoria**: TBD

## Problemas Identificados

1. **Pipeline travado**: 141 drafts parados, 0 agendados
2. **Sem cadência**: Publicação esporádica, sem rotina
3. **News hub inativo**: Feed de notícias retorna 0 itens
4. **Sem automação de agendamento**: Tudo manual
5. **Métricas vazias**: Nenhum post com dados de performance

## Melhorias Propostas

### Curto prazo
- [x] Dashboard: sugerir draft quando nada agendado
- [ ] Auto-agendar: IA seleciona melhor post + horário da semana
- [ ] Cruzar notícias do dia com drafts → sugerir post relevante
- [ ] Notificação: "Você não publicou essa semana"

### Médio prazo
- [ ] Publicação direta via LinkedIn API
- [ ] Auto-importar notícias via RSS (reativar news_hub)
- [ ] A/B testing de ganchos LinkedIn
- [ ] Métricas automáticas via LinkedIn API

### Longo prazo
- [ ] Multi-canal (Instagram, Twitter/X)
- [ ] Calendário colaborativo com equipe
- [ ] AI content generation (não só adaptação)
- [ ] Integração com Google Analytics
