# Tarefa 2INTEL: API Dashboard Unificado

**Data**: 2026-03-25
**Branch**: `feature/dashboard-api`
**Prioridade**: Alta

## Contexto

O sistema está migrando para `intel.almeida-prado.com`.
Precisamos de uma API unificada para o Dashboard que agregue dados de Círculos, Briefings e outras métricas.

## Objetivo

Criar endpoint `/api/v1/dashboard` que retorne todos os dados necessários para o Dashboard principal.

## Arquivo a Criar

`app/services/dashboard.py`

## Especificação da API

### GET /api/v1/dashboard

Response:
```json
{
  "stats": {
    "total_contatos": 6699,
    "circulos_ativos": 847,
    "precisam_atencao": 23,
    "briefings_pendentes": 5,
    "conversas_ativas": 0,
    "reunioes_hoje": 0,
    "tarefas_pendentes": 0
  },
  "circulos_resumo": {
    "1": {"total": 12, "health_medio": 85},
    "2": {"total": 45, "health_medio": 72},
    "3": {"total": 120, "health_medio": 65},
    "4": {"total": 670, "health_medio": 50},
    "5": {"total": 5852, "health_medio": 50}
  },
  "alertas": [
    {
      "tipo": "aniversario",
      "contato_id": 1360,
      "nome": "Douglas Bassi",
      "mensagem": "Aniversário em 2 dias",
      "prioridade": "alta"
    },
    {
      "tipo": "health_baixo",
      "contato_id": 123,
      "nome": "João Silva",
      "mensagem": "Círculo 2, sem contato há 30 dias",
      "prioridade": "media"
    }
  ],
  "contatos_recentes": [
    {"id": 1, "nome": "Eduan Lenine", "cargo": "Staff Software Engineer", "foto_url": "..."}
  ]
}
```

## Funções a Implementar

```python
def get_dashboard_stats() -> Dict:
    """Retorna estatísticas gerais do sistema"""

def get_alertas(limit: int = 10) -> List[Dict]:
    """
    Retorna alertas priorizados:
    1. Aniversários próximos (3 dias)
    2. Health score crítico (< 30) em círculos 1-3
    3. Contatos sem interação além do esperado
    """

def get_contatos_recentes(limit: int = 5) -> List[Dict]:
    """Últimos contatos interagidos"""
```

## Critérios de "Precisam Atenção"

Um contato precisa de atenção se:
- Círculo 1-3 E health_score < 50
- OU aniversário nos próximos 7 dias
- OU marcado como VIP E health_score < 70

## Dependências

- Usar funções existentes de `circulos.py`
- Usar funções existentes de `briefings.py`

## Checklist

- [ ] Criar `app/services/dashboard.py`
- [ ] Implementar `get_dashboard_stats()`
- [ ] Implementar `get_alertas()`
- [ ] Implementar `get_contatos_recentes()`
- [ ] Testar localmente
- [ ] Atualizar COORDINATION.md
- [ ] Push para branch

## Após Conclusão

```bash
git checkout -b feature/dashboard-api
# ... implementar ...
git add -A
git commit -m "Add unified dashboard API"
git push origin feature/dashboard-api
```

Atualizar COORDINATION.md com "PRONTO PARA REVIEW".
