# TAREFA URGENTE: Recalcular Circulos com Dados de Familia

**Status**: URGENTE - Executar imediatamente
**Branch**: `main` (pode rodar direto)
**Prioridade**: ALTA

## Contexto

1. Importamos arvore genealogica do MyHeritage (102 familiares)
2. 52 contatos foram atualizados com circulos 1-3 e tag "familia"
3. Os demais 6600+ contatos ainda estao em Circulo 5
4. Precisamos recalcular usando a funcao existente

## Tarefa 1: Executar Recalculo em Lote

Ja existe endpoint em main: `POST /api/circulos/recalculate-batch`

### Opcao A: Via Script Python

```python
#!/usr/bin/env python3
"""Recalcula todos os circulos em lotes"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from database import get_db
from services.circulos import recalcular_todos_circulos_batch

# Recalcula em lotes de 500
offset = 0
total_processados = 0

with get_db() as conn:
    while True:
        result = recalcular_todos_circulos_batch(
            conn,
            batch_size=500,
            offset=offset,
            skip_manual=True  # Nao recalcular quem tem circulo_manual=true
        )

        total_processados += result['processados']
        print(f"Processados: {total_processados}/{result['total']}")
        print(f"  Mudaram: {result['mudaram']}")

        if result['concluido']:
            break

        offset += result['processados']

print(f"\nRecalculo concluido! Total: {total_processados}")
```

### Opcao B: Via curl (chamadas repetidas)

```bash
# Primeira chamada
curl -X POST "https://intel.almeida-prado.com/api/circulos/recalculate-batch?batch_size=500&offset=0"

# Continuar ate concluido=true
curl -X POST "https://intel.almeida-prado.com/api/circulos/recalculate-batch?batch_size=500&offset=500"
curl -X POST "https://intel.almeida-prado.com/api/circulos/recalculate-batch?batch_size=500&offset=1000"
# ... etc
```

## Tarefa 2: Verificar Resultados

Depois do recalculo, verificar distribuicao:

```sql
SELECT circulo, COUNT(*) as total
FROM contacts
GROUP BY circulo
ORDER BY circulo;
```

Esperado:
- Circulo 1: ~5-10 (familia imediata)
- Circulo 2: ~15-30 (familia proxima, parceiros)
- Circulo 3: ~100-200 (networking ativo)
- Circulo 4: ~500-1000 (conhecidos)
- Circulo 5: ~5000+ (arquivo)

## Tarefa 3: Aplicar Tags Automaticas

Usar o sistema de auto-tags ja implementado:

```bash
# Analisar e aplicar tags em lote
curl -X POST "https://intel.almeida-prado.com/api/contacts/apply-auto-tags?batch_size=500&auto_apply=true"
```

Isso vai adicionar tags como:
- `financeiro` para quem trabalha em banco
- `c-level` para CEOs/Diretores
- `governo` para emails @gov.br
- `tecnologia` para empresas tech
- etc.

## Tarefa 4: Verificar Duplicados

```bash
# Ver estatisticas de duplicados
curl "https://intel.almeida-prado.com/api/contacts/duplicates/stats"

# Listar possiveis duplicados
curl "https://intel.almeida-prado.com/api/contacts/duplicates?threshold=0.7&limit=20"
```

## Criterio de Aceite

- [ ] Recalculo executado para todos os contatos
- [ ] Distribuicao de circulos faz sentido (nao mais todos em 5)
- [ ] Tags automaticas aplicadas
- [ ] Relatorio de duplicados gerado

## Depois de Concluir

Atualizar COORDINATION.md com:
```
[2026-03-25 2INTEL] **RECALCULO DE CIRCULOS CONCLUIDO**
- Total processados: X
- Distribuicao: C1=X, C2=X, C3=X, C4=X, C5=X
- Tags aplicadas: X contatos
- Duplicados encontrados: X pares
```
