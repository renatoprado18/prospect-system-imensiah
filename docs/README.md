# Documentacao do Projeto - ImensIAH Prospects

## Para Instancias Claude Code

**LEIA PRIMEIRO**: [COORDINATION.md](./COORDINATION.md)

Este e o arquivo central de comunicacao entre instancias. Sempre leia antes de comecar e atualize apos mudancas.

## Arquivos de Documentacao

| Arquivo | Conteudo | Quando Ler |
|---------|----------|------------|
| [COORDINATION.md](./COORDINATION.md) | Status das instancias, mensagens, bloqueios | SEMPRE (antes de comecar) |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Visao geral tecnica, estrutura | Ao iniciar nova feature |
| [MODULES.md](./MODULES.md) | Detalhes de cada modulo | Ao modificar modulo especifico |

## Regras de Coordenacao

### 1. Antes de Comecar Qualquer Trabalho
```bash
# Atualize o repositorio
git fetch origin
git rebase origin/main

# Leia o arquivo de coordenacao
cat docs/COORDINATION.md
```

### 2. Arquivos que Requerem Coordenacao

**NAO MODIFIQUE sem avisar em COORDINATION.md**:
- `app/main.py`
- `app/models.py`
- `app/database.py`
- `requirements.txt`

### 3. Comunicar Mudancas
```bash
# Edite COORDINATION.md com sua mensagem
# Depois:
git add docs/COORDINATION.md
git commit -m "coord: [sua mensagem]"
git push origin sua-branch
```

### 4. Antes de Merge
1. Poste em COORDINATION.md
2. Aguarde OK do coordenador (COORD)
3. Faca merge
4. Avise as outras instancias

## Workflow Padrao

```
1. git fetch && git rebase origin/main
2. Ler docs/COORDINATION.md
3. Criar branch: git checkout -b feature/minha-feature
4. Desenvolver (commits frequentes)
5. Avisar em COORDINATION.md quando pronto
6. Merge coordenado
7. Todas instancias: git fetch && git rebase
```

## Instancias Atuais

- **COORD** (esta): Coordenacao, documentacao, consistencia
- **INST-1**: LinkedIn + Email integration
- **INST-2**: (a definir)
