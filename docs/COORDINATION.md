# Coordenacao entre Instancias Claude Code

> **IMPORTANTE**: Este arquivo e o ponto central de comunicacao entre instancias.
> Leia SEMPRE antes de comecar qualquer trabalho.
> Atualize SEMPRE apos fazer mudancas significativas.

## Status Atual

**Ultima atualizacao**: 2026-03-25
**Instancias ativas**: 3 (nova estrutura)

## Nova Estrutura de Instancias (2026-03-25)

O sistema esta evoluindo de B2B sales para **Assistente Pessoal Inteligente**.
Nova nomenclatura:

| ID | Nome | Responsabilidades | Branch Atual | Status |
|----|------|-------------------|--------------|--------|
| 1ARCH | Arquiteto/Coordenador | Coordenacao, arquitetura, revisao | main | ATIVO |
| 2INTEL | Inteligencia | AI, scoring, algoritmos, classificacao | main | **MERGED** |
| 3FLOW | Flow & UX | UI, API endpoints, canais, automacao | main | **MERGED** |

## Feature Atual: Sistema de Circulos

**Documentacao**: `docs/CIRCULOS_ARCHITECTURE.md`
**Instrucoes INTEL**: `docs/INTEL_CIRCULOS_TASK.md`
**Instrucoes FLOW**: `docs/FLOW_CIRCULOS_TASK.md`

### Descricao
Sistema de classificacao de 12k+ contatos em niveis de proximidade (1-5).
Inclui health score para monitorar saude dos relacionamentos.

### Dependencias
```
1. INTEL implementa logica (circulos.py)
2. ARCH aprova e faz schema changes (database.py)
3. FLOW implementa UI/API
```

## Arquivos Bloqueados (Nao modificar sem coordenar)

```
BLOQUEADO - Coordenar com ARCH antes de editar:
- app/main.py          (rotas - todas as features adicionam aqui)
- app/models.py        (schemas - impacta todos os modulos)
- app/database.py      (tabelas - mudancas de schema)
- requirements.txt     (dependencias - pode quebrar deploy)
```

## Mensagens Pendentes

```
[2026-03-25 ARCH] **NOVA FEATURE: Sistema de Circulos**
Arquitetura definida em docs/CIRCULOS_ARCHITECTURE.md

[2026-03-25 INTEL] **PRONTO PARA REVIEW: circulos.py**
Branch: feature/circulos-intel
Commit: 7d64aba
Arquivo: app/services/circulos.py (762 linhas)

Funcoes implementadas:
  - calcular_score_circulo: Classificacao baseada em tags, interacoes, recencia
  - calcular_health_score: Saude do relacionamento (0-100)
  - recalcular_circulo_contato: Recalcula um contato
  - recalcular_todos_circulos: Recalculo em lote
  - get_contatos_precisando_atencao: Lista contatos com health baixo
  - get_aniversarios_proximos: Lista aniversarios proximos
  - get_dashboard_circulos: Dados do dashboard
  - definir_circulo_manual: Override manual
  - get_contatos_por_circulo: Lista paginada por circulo

Testes realizados:
  ✓ Tag familia -> Circulo 1
  ✓ Tag conselho -> Circulo 2
  ✓ 30 interacoes + recente -> Circulo 3
  ✓ Sem dados -> Circulo 5
  ✓ Health score calculado corretamente
  ✓ VIP + cliente bonus funcionando

[2026-03-25 ARCH] **APROVADO E MERGED: INTEL circulos.py**
Schema changes em database.py: commit 5d320ed
Merge de feature/circulos-intel: concluido
Colunas adicionadas: circulo, circulo_manual, frequencia_ideal_dias,
                     ultimo_calculo_circulo, health_score
Indices: idx_contacts_circulo, idx_contacts_health

FLOW - Sua tarefa:
  - Ler docs/FLOW_CIRCULOS_TASK.md
  - Criar branch: feature/circulos-flow
  - Implementar endpoints e UI
  - INTEL concluiu - pode usar circulos.py

[2026-03-25 ARCH] **ANALISE: ConselhoOS**
Analise completa em docs/CONSELHOOS_ANALISE.md
Integracao planejada para fase futura.
ConselhoOS repo: /Users/rap/conselhoos

[2026-03-25 FLOW] **PRONTO PARA REVIEW: Circulos UI/API**
Branch: feature/circulos-flow
Arquivos modificados:
  - app/main.py: Endpoints de Circulos (9 endpoints)
    GET /api/circulos - Dashboard
    GET /api/circulos/{n}/contacts - Contatos por circulo
    GET /api/circulos/health - Saude dos relacionamentos
    GET /api/contacts/{id}/circulo - Detalhes circulo contato
    POST /api/contacts/{id}/circulo - Atualizar circulo manual
    POST /api/circulos/recalculate - Recalcular todos
    POST /api/contacts/{id}/circulo/recalculate - Recalcular um
    GET /rap/circulos - Pagina HTML

Arquivos criados:
  - app/templates/rap_circulos.html (novo) - Dashboard visual

Testes planejados:
  - Dashboard carrega corretamente
  - Filtro por circulo funciona
  - Ordenacao (health, nome, ultimo_contato)
  - Precisam atencao mostra contatos
  - Recalcular funciona

Depende de: INTEL circulos.py (MERGED)

[2026-03-25 ARCH] **APROVADO E MERGED: FLOW Circulos UI/API**
Merge concluido em main.
Feature Circulos 100% completa:
  - 2INTEL: circulos.py (logica)
  - 1ARCH: database.py (schema)
  - 3FLOW: main.py + rap_circulos.html (UI/API)

Proxima tarefa 2INTEL: Briefings Inteligentes
  - Ler docs/INTEL_BRIEFINGS_TASK.md
  - Implementar app/services/briefings.py

Proxima tarefa 3FLOW: Briefings UI
  - Ler docs/FLOW_BRIEFINGS_TASK.md
  - Implementar endpoints e rap_briefings.html
  - Pode usar mocks enquanto INTEL nao termina

[2026-03-25 INTEL] **PRONTO PARA REVIEW: briefings.py**
Branch: feature/briefings-intel
Commit: e3fba41
Arquivo: app/services/briefings.py (537 linhas)

Funcoes implementadas:
  - get_contact_data(): Busca dados completos (mensagens, fatos, memorias, tasks)
  - format_contact_context(): Formata para prompt AI com contexto ImensIAH
  - generate_briefing(): Gera briefing usando Claude API
  - get_contacts_needing_briefing(): Lista contatos que precisam atencao
  - get_briefing_summary(): Resumo rapido sem chamar AI

Features:
  - Integracao com health score do Circulos
  - Alertas de aniversario
  - Deteccao de oportunidades de negocio
  - Contexto sobre Renato/ImensIAH no prompt
  - Output estruturado para UI

Aguardando: Aprovacao do ARCH

[2026-03-25 FLOW] **PRONTO PARA REVIEW: Briefings UI/API**
Branch: feature/briefings-flow
Arquivos modificados:
  - app/main.py: Endpoints de Briefings (3 endpoints)
    GET /api/briefings/pending - Contatos que precisam briefing
    POST /api/contacts/{id}/briefing - Gerar briefing com AI
    GET /rap/briefings - Pagina HTML

Arquivos criados:
  - app/templates/rap_briefings.html (novo) - Interface de briefings

Features implementadas:
  - Lista contatos precisando briefing
  - Busca de contato
  - Geracao de briefing com contexto opcional
  - Visualizacao formatada do briefing
  - Botao copiar

Depende de: INTEL briefings.py (disponivel)
Aguardando: Aprovacao do ARCH
```

## Protocolo de Merge (OBRIGATORIO)

### Passo a Passo:

```
1. INSTANCIA termina feature
   - Atualiza COORDINATION.md: "PRONTO PARA REVIEW"
   - git push origin feature/sua-branch (NAO main!)
   - AGUARDA aprovacao do ARCH

2. ARCH revisa
   - Verifica codigo e conflitos
   - Atualiza COORDINATION.md: "APROVADO" ou "REQUER AJUSTES"
   - Faz mudancas em arquivos bloqueados se necessario

3. INSTANCIA executa merge (SOMENTE apos aprovacao)
   - git checkout main
   - git pull origin main
   - git merge feature/sua-branch
   - git push origin main
   - Atualiza COORDINATION.md: "MERGED"

4. TODAS instancias sincronizam
   - git fetch origin && git rebase origin/main
```

## Decisoes Arquiteturais

| Data | Decisao | Contexto | Tomada Por |
|------|---------|----------|------------|
| 2026-03-21 | Usar docs/ para coordenacao | Comunicacao entre instancias | COORD |
| 2026-03-21 | Protocolo merge hibrido | Instancia faz merge apos aprovacao | COORD |
| 2026-03-25 | Renomear instancias ARCH/INTEL/FLOW | Evolucao para assistente pessoal | ARCH |
| 2026-03-25 | Circulos como primeira feature | Organizar 12k contatos | ARCH |
| 2026-03-25 | ConselhoOS integracao via API | Manter sistemas separados | ARCH |

## Historico de Sessoes

### Sessao 2026-03-21 (Concluida)
- INST-1: Gmail + Contact Enrichment v2 -> MERGED
- INST-2: WhatsApp 4 features -> MERGED
- INST-3: Scoring v2.0 + Contacts Scoring -> MERGED

### Sessao 2026-03-25 (Em Andamento)
- 1ARCH: Arquitetura + Coordenacao + Workflow -> CONCLUIDO
- 2INTEL: Circulos + Briefings -> **MERGED**
- 3FLOW: Circulos UI + Briefings UI -> **MERGED**

**Feature Circulos: 100% COMPLETA**
**Feature Briefings: 100% COMPLETA**

## Feature Briefings: COMPLETA

**2INTEL**: `app/services/briefings.py` (537 linhas) -> **MERGED**
**3FLOW**: `app/templates/rap_briefings.html` + endpoints -> **MERGED**

### Funcionalidades Implementadas
- `GET /api/briefings/pending` - Lista contatos precisando briefing
- `POST /api/contacts/{id}/briefing` - Gera briefing com AI
- `GET /rap/briefings` - Pagina de briefings
- Integracao com Circulos (health score, frequencia)
- Contexto inclui: fatos, memorias, mensagens, tasks

## Proximos Passos Globais

1. [x] Definir nova estrutura de instancias (ARCH/INTEL/FLOW)
2. [x] Criar arquitetura do sistema de Circulos
3. [x] Criar instrucoes para INTEL e FLOW
4. [x] Analisar ConselhoOS para integracao futura
5. [x] INTEL implementar circulos.py
6. [x] ARCH aprovar e fazer schema changes em database.py
7. [x] FLOW implementar UI e endpoints Circulos
8. [x] INTEL implementar briefings.py
9. [x] FLOW implementar UI briefings
10. [ ] Testar sistema completo
11. [ ] Deploy em producao
