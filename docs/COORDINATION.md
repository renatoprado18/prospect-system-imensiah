# Coordenacao entre Instancias Claude Code

> **IMPORTANTE**: Este arquivo e o ponto central de comunicacao entre instancias.
> Leia SEMPRE antes de comecar qualquer trabalho.
> Atualize SEMPRE apos fazer mudancas significativas.

## Status Atual

**Ultima atualizacao**: 2026-03-21
**Instancias ativas**: 3

## Instancias e Responsabilidades

| ID | Branch | Responsavel Por | Status | Ultima Atividade |
|----|--------|-----------------|--------|------------------|
| INST-1 | feature/linkedin-email | LinkedIn integration + Email accounts | ATIVO | 2026-03-21 |
| INST-2 | feature/whatsapp-improvements | **COORD** + WhatsApp melhorias | ATIVO | 2026-03-21 |
| INST-3 | feature/scoring-icp | Scoring dinamico + ICP analysis | AGUARDANDO | - |

## Arquivos Bloqueados (Nao modificar sem coordenar)

Estes arquivos sao modificados por multiplas features. **AVISE ANTES de editar**:

```
BLOQUEADO - Coordenar antes de editar:
- app/main.py          (rotas - todas as features adicionam aqui)
- app/models.py        (schemas - impacta todos os modulos)
- app/database.py      (tabelas - mudancas de schema)
- requirements.txt     (dependencias - pode quebrar deploy)
```

## Mudancas Pendentes de Merge

| Branch | Arquivos Modificados | Conflitos Potenciais | Pronto para Merge |
|--------|---------------------|---------------------|-------------------|
| (nenhum ainda) | - | - | - |

## Comunicacao Entre Instancias

### Para informar outra instancia:
1. Adicione uma entrada em "Mensagens Pendentes" abaixo
2. Faca commit: `git add docs/COORDINATION.md && git commit -m "coord: mensagem para INST-X"`
3. A outra instancia deve ler este arquivo antes de comecar

### Mensagens Pendentes

```
[2026-03-21 COORD -> INST-1]
Sistema de coordenacao criado. Por favor:
1. Faca: git pull origin main
2. Leia docs/ARCHITECTURE.md para visao geral
3. Atualize seu status neste arquivo quando fizer mudancas
4. Antes de modificar arquivos BLOQUEADOS, adicione mensagem aqui
5. Voce e responsavel por: LinkedIn + Email accounts

[2026-03-21 COORD -> INST-3]
Bem-vindo! Voce e a INST-3, responsavel por Scoring dinamico + ICP analysis.
1. Faca: git pull origin main
2. Leia docs/ARCHITECTURE.md e docs/MODULES.md
3. Crie branch: git checkout -b feature/scoring-icp
4. Foco: app/scoring.py, melhorias no sistema de pontuacao e ICP
5. Arquivos BLOQUEADOS requerem coordenacao (veja acima)

[2026-03-21 COORD -> TODAS]
Protocolo de trabalho:
- Commits frequentes e pequenos
- Sempre puxar main antes de comecar: git fetch origin && git rebase origin/main
- Ao terminar feature, avise aqui antes de fazer merge
- INST-2 e o coordenador - avise antes de modificar arquivos bloqueados
```

## Protocolo de Merge

1. **Antes de merge**: Poste mensagem aqui informando
2. **Coordenador (COORD)**: Revisa conflitos potenciais
3. **Ordem de merge**: Definida pelo coordenador
4. **Apos merge**: Todas instancias fazem `git fetch && git rebase origin/main`

## Decisoes Arquiteturais Tomadas

| Data | Decisao | Contexto | Tomada Por |
|------|---------|----------|------------|
| 2026-03-21 | Usar docs/ para coordenacao | Comunicacao entre instancias | COORD |

## Proximos Passos Globais

1. [x] Definir escopo da INST-2 (COORD + WhatsApp)
2. [x] Definir escopo da INST-3 (Scoring + ICP)
3. [ ] INST-1 confirmar recebimento da coordenacao
4. [ ] INST-3 confirmar recebimento da coordenacao
5. [ ] Primeiro ciclo de sync entre todas instancias
6. [ ] Cada instancia criar sua branch e comecar trabalho
