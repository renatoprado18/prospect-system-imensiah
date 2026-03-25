# Sistema de Circulos - Arquitetura

> **Autor**: ARCH (Coordenador/Arquiteto)
> **Data**: 2026-03-25
> **Status**: Em desenvolvimento

## Visao Geral

O sistema de Circulos organiza os 12k+ contatos em niveis de proximidade,
permitindo gestao inteligente de relacionamentos.

## Definicao dos Circulos

| Circulo | Nome | Descricao | Qtd Estimada | Frequencia Ideal |
|---------|------|-----------|--------------|------------------|
| 1 | Intimo | Familia, amigos proximos, socios | 20-50 | Semanal |
| 2 | Proximo | Parceiros, mentores, conselheiros | 50-100 | Quinzenal |
| 3 | Ativo | Networking ativo, clientes-chave | 200-500 | Mensal |
| 4 | Conhecido | Contatos ocasionais, ex-colegas | 1000-2000 | Trimestral |
| 5 | Arquivo | Resto dos contatos | 9000+ | Sob demanda |

## Schema do Banco de Dados

### Alteracoes na tabela `contacts`

```sql
ALTER TABLE contacts
ADD COLUMN IF NOT EXISTS circulo INTEGER DEFAULT 5,
ADD COLUMN IF NOT EXISTS circulo_manual BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS frequencia_ideal_dias INTEGER,
ADD COLUMN IF NOT EXISTS ultimo_calculo_circulo TIMESTAMP,
ADD COLUMN IF NOT EXISTS health_score INTEGER DEFAULT 50;

-- Health score: 0-100, indica saude do relacionamento
-- 100 = dentro da frequencia ideal
-- 0 = muito tempo sem contato

CREATE INDEX IF NOT EXISTS idx_contacts_circulo ON contacts(circulo);
```

### Nova tabela `circulo_config`

```sql
CREATE TABLE IF NOT EXISTS circulo_config (
    circulo INTEGER PRIMARY KEY,
    nome TEXT NOT NULL,
    descricao TEXT,
    frequencia_padrao_dias INTEGER NOT NULL,
    cor TEXT,
    icone TEXT
);

INSERT INTO circulo_config VALUES
(1, 'Intimo', 'Familia e amigos proximos', 7, '#FF6B6B', 'heart'),
(2, 'Proximo', 'Parceiros e mentores', 14, '#4ECDC4', 'star'),
(3, 'Ativo', 'Networking ativo', 30, '#45B7D1', 'briefcase'),
(4, 'Conhecido', 'Contatos ocasionais', 90, '#96CEB4', 'users'),
(5, 'Arquivo', 'Demais contatos', 365, '#DDA0DD', 'archive');
```

### Nova tabela `circulo_rules`

```sql
CREATE TABLE IF NOT EXISTS circulo_rules (
    id SERIAL PRIMARY KEY,
    nome TEXT NOT NULL,
    descricao TEXT,
    prioridade INTEGER DEFAULT 0,
    condicoes JSONB NOT NULL,
    circulo_resultado INTEGER NOT NULL,
    ativo BOOLEAN DEFAULT TRUE
);

-- Regras de exemplo
INSERT INTO circulo_rules (nome, condicoes, circulo_resultado, prioridade) VALUES
('Familia', '{"tags_contem": ["familia", "family"]}', 1, 100),
('Amigo proximo', '{"tags_contem": ["amigo", "friend"], "interacoes_min": 10}', 1, 90),
('Conselheiro', '{"tags_contem": ["conselho", "board"]}', 2, 80),
('Cliente ativo', '{"tags_contem": ["cliente"], "dias_sem_contato_max": 60}', 3, 70),
('Interacao frequente', '{"interacoes_min": 20, "dias_sem_contato_max": 30}', 3, 50),
('Interacao media', '{"interacoes_min": 5, "dias_sem_contato_max": 90}', 4, 30);
```

## Algoritmo de Classificacao

### Fatores de Pontuacao

```python
def calcular_circulo(contact):
    score = 0

    # 1. Tags especiais (familia, amigo, etc)
    if has_tag(contact, ['familia', 'family']):
        return 1  # Override direto
    if has_tag(contact, ['conselho', 'board', 'advisor']):
        return 2  # Override direto

    # 2. Frequencia de interacao
    interacoes = contact.total_interacoes
    if interacoes >= 50:
        score += 40
    elif interacoes >= 20:
        score += 30
    elif interacoes >= 10:
        score += 20
    elif interacoes >= 5:
        score += 10

    # 3. Recencia do contato
    dias_sem_contato = (now - contact.ultimo_contato).days
    if dias_sem_contato <= 7:
        score += 30
    elif dias_sem_contato <= 30:
        score += 20
    elif dias_sem_contato <= 90:
        score += 10

    # 4. Dados completos (indica importancia)
    if contact.aniversario:
        score += 5
    if contact.linkedin:
        score += 5
    if contact.empresa:
        score += 5

    # 5. Contexto pessoal tem bonus
    if contact.contexto == 'personal':
        score += 10

    # Mapear score para circulo
    if score >= 70:
        return 2  # Proximo
    elif score >= 50:
        return 3  # Ativo
    elif score >= 25:
        return 4  # Conhecido
    else:
        return 5  # Arquivo
```

### Health Score

```python
def calcular_health_score(contact):
    """
    Calcula saude do relacionamento (0-100)
    100 = em dia, 0 = precisa atencao urgente
    """
    config = get_circulo_config(contact.circulo)
    frequencia_ideal = contact.frequencia_ideal_dias or config.frequencia_padrao_dias

    dias_sem_contato = (now - contact.ultimo_contato).days

    if dias_sem_contato <= frequencia_ideal:
        return 100

    # Decai linearmente ate 2x a frequencia ideal
    excesso = dias_sem_contato - frequencia_ideal
    limite = frequencia_ideal  # 100% de excesso = 0 health

    health = max(0, 100 - (excesso / limite * 100))
    return int(health)
```

## API Endpoints

### Leitura

```
GET /api/circulos
    -> Lista config dos circulos com contagem

GET /api/circulos/{circulo}/contacts
    -> Lista contatos de um circulo
    -> Query params: sort_by (health, nome, ultimo_contato), limit, offset

GET /api/contacts/{id}/circulo
    -> Detalhes do circulo de um contato

GET /api/circulos/health
    -> Dashboard de saude geral
    -> Retorna: contatos precisando atencao por circulo
```

### Escrita

```
POST /api/contacts/{id}/circulo
    -> Atualiza circulo manualmente
    -> Body: { "circulo": 2, "frequencia_ideal_dias": 14 }
    -> Seta circulo_manual = true

POST /api/circulos/recalculate
    -> Recalcula circulos de todos contatos (exceto manuais)
    -> Query param: force=true para incluir manuais

POST /api/circulos/rules
    -> Adiciona nova regra de classificacao
```

## UI Components

### Dashboard de Circulos (`rap_circulos.html`)

```
+----------------------------------------------------------+
|  MEUS CIRCULOS                                    [Gear] |
+----------------------------------------------------------+
|                                                          |
|  [1 Intimo]  [2 Proximo]  [3 Ativo]  [4 Conhecido]  [5]  |
|     32          78          245         1.2k         9k  |
|                                                          |
+----------------------------------------------------------+
|  PRECISAM ATENCAO                              Ver todos |
+----------------------------------------------------------+
|  [!] Joao Silva (Circulo 2) - 45 dias sem contato       |
|  [!] Maria Santos (Circulo 1) - 21 dias sem contato     |
|  [!] Carlos Mendes (Circulo 3) - 60 dias sem contato    |
+----------------------------------------------------------+
|  ANIVERSARIOS PROXIMOS                                   |
+----------------------------------------------------------+
|  [Cake] Pedro Lima - 28/03 (3 dias)                     |
|  [Cake] Ana Costa - 02/04 (8 dias)                      |
+----------------------------------------------------------+
```

### Card de Contato com Circulo

```
+----------------------------------+
|  [Foto]  Joao Silva         [2] |
|          CEO @ Empresa X        |
|                                  |
|  Ultimo contato: 15/03 (10 dias)|
|  Health: [========--] 80%       |
|                                  |
|  [WhatsApp] [Email] [Ligar]     |
+----------------------------------+
```

## Integracao com Briefings

Quando um contato tem reuniao agendada:

1. Sistema detecta evento no Google Calendar
2. Busca contato pelo email
3. Gera briefing com:
   - Circulo do contato
   - Health score atual
   - Historico resumido
   - Alertas (ex: "Aniversario em 5 dias")
   - Sugestoes de pauta

## Cron Jobs

```
# Recalculo diario de health scores (6h)
0 6 * * * /api/cron/circulos-health

# Recalculo semanal de circulos (domingo 3h)
0 3 * * 0 /api/cron/circulos-recalculate

# Alertas diarios (8h)
0 8 * * * /api/cron/circulos-alertas
```

## Metricas e KPIs

- **Contatos por circulo**: Distribuicao atual
- **Health medio por circulo**: Indica se estou cuidando bem
- **Contatos em risco**: Health < 30%
- **Taxa de contato**: % de contatos dentro da frequencia ideal
- **Tendencia**: Comparativo semanal/mensal

---

## Proximos Passos (Implementacao)

### INTEL (Inteligencia)
1. Implementar algoritmo de classificacao
2. Criar funcao de health score
3. Sistema de regras dinamicas

### FLOW (UX/Canais)
1. Criar UI de dashboard de circulos
2. Implementar endpoints da API
3. Integrar alertas com notificacoes

### ARCH (Coordenacao)
1. Revisar PRs
2. Definir regras iniciais com Renato
3. Analisar dados atuais para calibrar algoritmo
