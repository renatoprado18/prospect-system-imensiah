# Fila de Tarefas 2INTEL

**Atualizacao**: 2026-03-26
**Modo**: Autonomo - sinalizar conclusao e continuar

---

## TAREFAS CONCLUIDAS

| Tarefa | Commit | Resumo |
|--------|--------|--------|
| Recalcular Circulos | 620de30 | 6647 contatos, C1=5, C2=6, C3=44, C4=378, C5=6266 |
| Auto-Tags | 620de30 | 1242 contatos, 1526 tags (c-level, diretor, gerente, etc) |
| Verificar Duplicados | 620de30 | 15 email, 4 phone, 20 name groups |
| Briefing Context | 698214e | briefing_context.py + 5 endpoints |
| Engajamento | merged | engajamento.py + 4 endpoints |
| Duplicados Service | 8c93930 | duplicados.py + Levenshtein + 3 endpoints |

---

## NOVAS TAREFAS

### Tarefa 1: Sincronizar Gmail para Popular Interacoes

**Status**: PENDENTE
**Prioridade**: ALTA

**Objetivo**: A maioria dos contatos (93.5%) esta em C5 porque nao tem dados de interacao. Precisamos sincronizar emails do Gmail para popular `ultimo_contato` e `total_interacoes`.

**Implementacao**:
```python
# Usar app/integrations/gmail.py existente
# Para cada contato com email:
#   1. Buscar mensagens trocadas
#   2. Atualizar ultimo_contato
#   3. Atualizar total_interacoes
#   4. Recalcular circulo
```

**Criterios**:
- [ ] Sincronizar ultimos 12 meses de emails
- [ ] Atualizar campo ultimo_contato
- [ ] Atualizar campo total_interacoes
- [ ] Re-executar recalculo de circulos

---

### Tarefa 2: Resolver Duplicados Encontrados

**Status**: PENDENTE
**Prioridade**: MEDIA

**Objetivo**: 42 duplicados identificados. Criar script para merge automatico ou semi-automatico.

**Implementacao**:
```bash
# Ver duplicados
curl "https://intel.almeida-prado.com/api/contacts/duplicates?threshold=0.7"

# Merge automatico (keep mais completo)
curl -X POST "https://intel.almeida-prado.com/api/contacts/merge" \
  -H "Content-Type: application/json" \
  -d '{"keep_id": 123, "merge_id": 456}'
```

**Criterios**:
- [ ] Listar todos os 42 duplicados
- [ ] Merge automatico onde score > 0.9
- [ ] Relatorio de merges realizados

---

### Tarefa 3: Melhorar Scoring para Distribuicao

**Status**: PENDENTE
**Prioridade**: BAIXA

**Objetivo**: Ajustar algoritmo de scoring em circulos.py para melhor distribuicao (menos contatos em C5).

**Sugestoes**:
- Dar mais peso para tags c-level/diretor
- Considerar empresa conhecida como fator
- Ajustar thresholds de score

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-26 | Recalculo + Tags + Duplicados | **CONCLUIDO** |
| 2026-03-25 | Todas 5 tarefas INTEL | **MERGED** |
