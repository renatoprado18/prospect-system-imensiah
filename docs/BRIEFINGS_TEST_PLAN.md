# Plano de Testes - Briefings Inteligentes

> **Feature**: Briefings
> **Responsaveis**: 2INTEL (logica), 3FLOW (UI)
> **Data**: 2026-03-25

## Testes Automaticos (pytest)

### 2INTEL - `tests/test_briefings.py`

| Teste | Descricao | Criterio |
|-------|-----------|----------|
| `test_get_contact_data` | Busca dados completos | Retorna dict com campos esperados |
| `test_format_contact_context` | Formata contexto para AI | String contem nome, circulo, fatos |
| `test_generate_briefing_success` | Gera briefing com dados | Retorna dict com 'briefing' |
| `test_generate_briefing_not_found` | Contato inexistente | Retorna {'error': ...} |
| `test_contacts_needing_briefing` | Lista contatos prioritarios | Lista ordenada por prioridade |

### 3FLOW - `tests/test_briefings_api.py`

| Teste | Descricao | Criterio |
|-------|-----------|----------|
| `test_get_pending_briefings` | GET /api/briefings/pending | Status 200, lista de contatos |
| `test_create_briefing` | POST /api/contacts/{id}/briefing | Status 200, briefing gerado |
| `test_create_briefing_not_found` | POST com ID invalido | Status 404 |
| `test_briefings_page` | GET /rap/briefings | Status 200, HTML valido |

## Testes Manuais (UI)

### Pagina de Briefings (`/rap/briefings`)

| # | Acao | Resultado Esperado |
|---|------|-------------------|
| 1 | Acessar /rap/briefings | Pagina carrega, lista de pendentes aparece |
| 2 | Digitar nome no campo busca | Resultados aparecem apos 2 caracteres |
| 3 | Clicar em contato da busca | Contato selecionado, botao habilitado |
| 4 | Clicar "Gerar Briefing" | Loading aparece, briefing gerado em ~5s |
| 5 | Verificar briefing gerado | Contem: resumo, pontos atencao, sugestoes |
| 6 | Clicar "Copiar" | Texto copiado para clipboard |
| 7 | Clicar em contato pendente | Seleciona e permite gerar briefing |

### Integracao com Circulos

| # | Acao | Resultado Esperado |
|---|------|-------------------|
| 1 | Gerar briefing de Circulo 1 | Health score alto, info familia |
| 2 | Gerar briefing de Circulo 5 | Info basica, menos detalhes |
| 3 | Contato sem historico | Briefing indica falta de dados |

## Criterios de Aceitacao

### Minimos (MVP)

- [ ] Gerar briefing para qualquer contato
- [ ] Briefing contem: resumo, pontos de atencao, sugestoes
- [ ] UI permite buscar e selecionar contato
- [ ] Briefing exibido formatado na tela
- [ ] Botao copiar funciona

### Desejados

- [ ] Lista de contatos precisando briefing
- [ ] Contexto opcional (ex: "reuniao de conselho")
- [ ] Integracao com dados de Circulos (health, frequencia)
- [ ] Historico de mensagens no contexto
- [ ] Fatos importantes no contexto

### Futuros

- [ ] Briefing automatico para eventos do calendario
- [ ] Cache de briefings recentes
- [ ] Exportar briefing como PDF
- [ ] Enviar briefing por email/WhatsApp

## Como Rodar os Testes

```bash
# Testes automaticos
python -m pytest tests/test_briefings.py -v

# Pre-merge check completo
./scripts/pre_merge_check.sh

# Teste manual da UI
# 1. Iniciar servidor: uvicorn app.main:app --reload
# 2. Acessar: http://localhost:8000/rap/briefings
# 3. Seguir checklist de testes manuais acima
```

## Dependencias

- 2INTEL deve terminar `app/services/briefings.py` primeiro
- 3FLOW pode usar mocks enquanto 2INTEL trabalha
- Ambos devem ter Circulos merged (ja esta)
