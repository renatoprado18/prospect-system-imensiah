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
| COORD | main | Coordenacao geral, documentacao, consistencia | ATIVO | 2026-03-21 |
| INST-1 | feature/linkedin-email | LinkedIn integration + Email accounts | ATIVO | 2026-03-21 |
| INST-2 | (a definir) | (a definir) | AGUARDANDO | - |

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
1. Leia docs/ARCHITECTURE.md para visao geral
2. Atualize seu status neste arquivo quando fizer mudancas
3. Antes de modificar arquivos BLOQUEADOS, adicione mensagem aqui

[2026-03-21 COORD -> TODAS]
Protocolo de trabalho:
- Commits frequentes e pequenos
- Sempre puxar main antes de comecar: git fetch origin && git rebase origin/main
- Ao terminar feature, avise aqui antes de fazer merge
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

1. [ ] Definir escopo da INST-2
2. [ ] Definir escopo da INST-3 (se houver)
3. [ ] INST-1 confirmar recebimento da coordenacao
4. [ ] Primeiro ciclo de sync entre todas instancias
