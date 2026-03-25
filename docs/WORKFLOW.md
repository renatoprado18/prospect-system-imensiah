# Fluxo de Trabalho - Instancias Claude Code

> **Objetivo**: Minimizar intervencao manual do usuario
> **Autor**: 1ARCH
> **Data**: 2026-03-25

## Principios

1. **Autonomia**: Instancias devem trabalhar de forma autonoma
2. **Git como Sinal**: Push = pronto para review
3. **Testes Automaticos**: Rodar antes de pedir merge
4. **Usuario Aprova**: Usuario so intervem para aprovar merges

---

## Fluxo Padrao

```
┌─────────────────────────────────────────────────────────────────┐
│                         FLUXO DE TRABALHO                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. INSTANCIA INICIA                                            │
│     └─> git pull origin main                                    │
│     └─> Ler docs/COORDINATION.md                                │
│     └─> Ler docs/{INSTANCIA}_*_TASK.md                         │
│                                                                  │
│  2. INSTANCIA TRABALHA                                          │
│     └─> Criar branch: feature/{feature}-{instancia}            │
│     └─> Implementar codigo                                      │
│     └─> Criar/atualizar testes                                  │
│                                                                  │
│  3. INSTANCIA TESTA                                             │
│     └─> Rodar: python -m pytest tests/ -v                      │
│     └─> Rodar: python scripts/test_feature.py (se existir)     │
│     └─> Verificar que todos passam                              │
│                                                                  │
│  4. INSTANCIA FINALIZA                                          │
│     └─> git push origin feature/...                            │
│     └─> Atualizar docs/COORDINATION.md com status              │
│     └─> Commit final com "PRONTO PARA REVIEW"                  │
│                                                                  │
│  5. 1ARCH DETECTA (automatico)                                  │
│     └─> Ve push no repositorio                                  │
│     └─> Revisa codigo                                           │
│     └─> Roda testes                                             │
│     └─> Aprova ou pede ajustes                                  │
│                                                                  │
│  6. USUARIO APROVA MERGE                                        │
│     └─> Usuario confirma: "aprovar merge de X"                 │
│     └─> 1ARCH faz merge para main                              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Estrutura de Testes

### Diretorio de Testes

```
tests/
├── __init__.py
├── conftest.py              # Fixtures compartilhadas
├── test_circulos.py         # Testes do sistema de circulos
├── test_briefings.py        # Testes de briefings
└── test_integration.py      # Testes de integracao
```

### Comando para Rodar Testes

```bash
# Todos os testes
python -m pytest tests/ -v

# Teste especifico
python -m pytest tests/test_circulos.py -v

# Com coverage
python -m pytest tests/ -v --cov=app
```

### Script de Teste Pre-Merge

```bash
# Rodar antes de pedir merge
./scripts/pre_merge_check.sh
```

---

## Comunicacao via Git

### Commits Padrao

```bash
# Durante desenvolvimento
git commit -m "feat(feature): descricao curta"

# Quando pronto para review
git commit -m "feat(feature): PRONTO PARA REVIEW

Implementado:
- item 1
- item 2

Testes:
- [ ] test_x passou
- [ ] test_y passou
"
```

### Branch Naming

```
feature/{feature}-{instancia}

Exemplos:
- feature/briefings-intel
- feature/briefings-flow
- feature/alertas-intel
```

---

## Checklist por Instancia

### 2INTEL (Logica/AI)

Antes de pedir merge:
- [ ] Codigo implementado em `app/services/`
- [ ] Funcoes documentadas com docstrings
- [ ] Testes unitarios em `tests/test_{feature}.py`
- [ ] Todos os testes passando
- [ ] COORDINATION.md atualizado

### 3FLOW (UI/API)

Antes de pedir merge:
- [ ] Endpoints implementados em `app/main.py`
- [ ] Template HTML em `app/templates/`
- [ ] Testes de endpoint em `tests/`
- [ ] UI testada manualmente (listar acoes)
- [ ] COORDINATION.md atualizado

### 1ARCH (Review)

Antes de aprovar:
- [ ] Codigo revisado
- [ ] Testes passando
- [ ] Sem conflitos com main
- [ ] Schema changes aplicados (se necessario)

---

## Plano de Testes por Feature

Cada feature deve ter um arquivo `docs/{FEATURE}_TEST_PLAN.md`:

```markdown
# Plano de Testes - {Feature}

## Testes Automaticos
- test_funcao_1: Verifica X
- test_funcao_2: Verifica Y

## Testes Manuais (UI)
1. Acessar /pagina
2. Clicar em botao X
3. Verificar que Y acontece

## Criterios de Aceitacao
- [ ] Criterio 1
- [ ] Criterio 2
```

---

## Quando Usuario Intervem

O usuario **SO** precisa intervir para:

1. **Aprovar merges**: "aprovar merge de 2INTEL"
2. **Resolver conflitos criticos**: Se 1ARCH nao conseguir
3. **Decisoes de negocio**: Escolhas que afetam UX/funcionalidade
4. **Deploy**: Quando quiser colocar em producao

O usuario **NAO** precisa:

- Enviar mensagens entre terminais
- Rodar testes manualmente (instancias fazem)
- Verificar sincronizacao (1ARCH monitora)
- Criar branches (instancias criam)

---

## Monitoramento por 1ARCH

1ARCH deve periodicamente:

```bash
# Ver branches remotas
git fetch origin
git branch -r -v

# Ver commits pendentes
git log main..origin/feature/X --oneline

# Ver status no COORDINATION.md
cat docs/COORDINATION.md | grep -A 5 "PRONTO"
```

---

## Inicio de Sessao

Quando usuario inicia uma sessao com multiplas instancias:

1. Abrir terminal 1 -> `claude` -> "Sou 1ARCH"
2. Abrir terminal 2 -> `claude` -> "Sou 2INTEL, leia COORDINATION.md"
3. Abrir terminal 3 -> `claude` -> "Sou 3FLOW, leia COORDINATION.md"

Cada instancia automaticamente:
- Le COORDINATION.md
- Identifica sua tarefa
- Comeca a trabalhar
- Reporta via git push quando pronto
