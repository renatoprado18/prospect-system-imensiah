# Fila de Tarefas 2INTEL

**Atualizacao**: 2026-03-25
**Modo**: Autonomo - sinalizar conclusao e continuar

## Como Trabalhar

1. Pegue a proxima tarefa PENDENTE
2. Crie branch: `feature/intel-{nome-curto}`
3. Implemente e teste
4. Commit e push para a branch
5. Atualize status para PRONTO e adicione resumo
6. **Continue para a proxima tarefa** (nao espere aprovacao)
7. ARCH fara review e merge em paralelo

---

## Tarefa 1: Recalcular Circulos (URGENTE)

**Status**: PENDENTE
**Branch**: `feature/intel-recalc-circulos`
**Prioridade**: ALTA

### Objetivo
Todos os 6699 contatos estao em Circulo 5. Precisamos recalcular.

### Implementacao
Criar script/endpoint para recalculo em lote:

```python
# app/services/circulos.py - adicionar ou melhorar

def recalcular_todos_circulos_batch(batch_size: int = 100) -> Dict:
    """
    Recalcula circulos em lotes para evitar timeout.
    Retorna progresso para chamadas subsequentes.
    """
    # Implementar com offset/limit
    # Atualizar circulo, health_score, ultimo_calculo_circulo
    # Retornar: {"processados": X, "total": Y, "concluido": bool}
```

### Criterio de Aceite
- [ ] Endpoint POST /api/circulos/recalculate-batch
- [ ] Processa em lotes de 100
- [ ] Retorna progresso
- [ ] Log de quantos mudaram de circulo

---

## Tarefa 2: Tags Automaticas por Contexto

**Status**: PENDENTE
**Branch**: `feature/intel-auto-tags`
**Prioridade**: MEDIA

### Objetivo
Analisar dados do contato e sugerir/aplicar tags automaticamente.

### Implementacao
```python
# app/services/auto_tags.py

def analisar_contato_para_tags(contact_id: int) -> List[str]:
    """
    Analisa:
    - Empresa (se banco -> tag 'financeiro')
    - Cargo (se CEO/Diretor -> tag 'c-level')
    - Email domain (se @gov -> tag 'governo')
    - Historico de mensagens (keywords)
    """

def aplicar_tags_em_lote(limit: int = 100) -> Dict:
    """Aplica tags sugeridas em lote"""
```

### Criterio de Aceite
- [ ] Detecta pelo menos 10 categorias
- [ ] Endpoint GET /api/contacts/{id}/suggested-tags
- [ ] Endpoint POST /api/contacts/apply-auto-tags

---

## Tarefa 3: Deteccao de Duplicados

**Status**: PENDENTE
**Branch**: `feature/intel-duplicados`
**Prioridade**: MEDIA

### Objetivo
Identificar contatos duplicados usando fuzzy matching.

### Implementacao
```python
# app/services/duplicados.py

def encontrar_duplicados(threshold: float = 0.85) -> List[Dict]:
    """
    Compara:
    - Nome (fuzzy match)
    - Email (exato)
    - Telefone (normalizado)
    Retorna pares de possiveis duplicados com score
    """

def merge_contatos(keep_id: int, merge_id: int) -> Dict:
    """Merge dois contatos, mantendo dados mais completos"""
```

### Criterio de Aceite
- [ ] Algoritmo de similaridade funcionando
- [ ] Endpoint GET /api/contacts/duplicates
- [ ] Endpoint POST /api/contacts/merge

---

## Tarefa 4: Analise de Engajamento

**Status**: PENDENTE
**Branch**: `feature/intel-engajamento`
**Prioridade**: BAIXA

### Objetivo
Calcular score de engajamento baseado em interacoes.

### Implementacao
```python
# app/services/engajamento.py

def calcular_engajamento(contact_id: int) -> Dict:
    """
    Fatores:
    - Frequencia de mensagens
    - Taxa de resposta
    - Tempo medio de resposta
    - Diversidade de canais (email, whatsapp)

    Retorna: score 0-100 e breakdown
    """
```

### Criterio de Aceite
- [ ] Score calculado corretamente
- [ ] Coluna engagement_score na tabela contacts
- [ ] Endpoint GET /api/contacts/{id}/engagement

---

## Tarefa 5: Contexto Enriquecido para Briefings

**Status**: PENDENTE
**Branch**: `feature/intel-briefing-context`
**Prioridade**: BAIXA

### Objetivo
Melhorar briefings com mais contexto automatico.

### Implementacao
- Detectar tom das ultimas mensagens
- Identificar topicos recorrentes
- Sugerir assuntos para retomar
- Alertar sobre promessas/compromissos pendentes

---

## Registro de Conclusao

| Tarefa | Branch | Status | Resumo |
|--------|--------|--------|--------|
| 1. Recalcular Circulos | - | PENDENTE | - |
| 2. Tags Automaticas | - | PENDENTE | - |
| 3. Duplicados | - | PENDENTE | - |
| 4. Engajamento | - | PENDENTE | - |
| 5. Briefing Context | - | PENDENTE | - |
