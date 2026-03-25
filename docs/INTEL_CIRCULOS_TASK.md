# INTEL - Tarefa: Implementar Logica de Circulos

> **Instancia**: INTEL (Inteligencia)
> **Coordenador**: ARCH
> **Data**: 2026-03-25
> **Branch**: `feature/circulos-intel`

## Contexto

Estamos transformando o sistema de um foco B2B para um **Assistente Pessoal Inteligente**.
A primeira feature e o sistema de **Circulos** - classificacao dos 12k+ contatos em niveis de proximidade.

**Leia primeiro**: `docs/CIRCULOS_ARCHITECTURE.md` (arquitetura completa)

## Sua Responsabilidade

Implementar a **logica de negocio** do sistema de Circulos:
1. Algoritmo de classificacao automatica
2. Calculo de health score
3. Sistema de regras dinamicas

## Arquivos a Criar/Modificar

### 1. CRIAR: `app/services/circulos.py`

```python
"""
Servico de Circulos - Classificacao e Health Score
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from app.database import get_db
import json

# Configuracao padrao dos circulos
CIRCULO_CONFIG = {
    1: {"nome": "Intimo", "frequencia_dias": 7, "cor": "#FF6B6B"},
    2: {"nome": "Proximo", "frequencia_dias": 14, "cor": "#4ECDC4"},
    3: {"nome": "Ativo", "frequencia_dias": 30, "cor": "#45B7D1"},
    4: {"nome": "Conhecido", "frequencia_dias": 90, "cor": "#96CEB4"},
    5: {"nome": "Arquivo", "frequencia_dias": 365, "cor": "#DDA0DD"},
}

# Tags que fazem override direto para circulo especifico
TAG_OVERRIDES = {
    1: ["familia", "family", "esposa", "filho", "filha", "pai", "mae", "irmao", "irma"],
    2: ["conselho", "board", "advisor", "mentor", "socio", "partner"],
}


def has_tag(contact_tags: List[str], target_tags: List[str]) -> bool:
    """Verifica se contato tem alguma das tags alvo"""
    if not contact_tags:
        return False
    contact_tags_lower = [t.lower() for t in contact_tags]
    return any(tag in contact_tags_lower for tag in target_tags)


def calcular_score_circulo(contact: Dict) -> Tuple[int, int, List[str]]:
    """
    Calcula o circulo de um contato baseado em multiplos fatores.

    Returns:
        Tuple[circulo, score, reasons]: circulo (1-5), score (0-100), lista de razoes
    """
    score = 0
    reasons = []

    # Parse tags
    tags = contact.get("tags") or []
    if isinstance(tags, str):
        tags = json.loads(tags) if tags else []

    # 1. Check for tag overrides (familia, conselho, etc)
    for circulo, override_tags in TAG_OVERRIDES.items():
        if has_tag(tags, override_tags):
            matched = [t for t in tags if t.lower() in override_tags]
            reasons.append(f"Tag especial: {', '.join(matched)}")
            return circulo, 100, reasons

    # 2. Frequencia de interacao
    total_interacoes = contact.get("total_interacoes") or 0
    if total_interacoes >= 50:
        score += 40
        reasons.append(f"{total_interacoes} interacoes (muito alto)")
    elif total_interacoes >= 20:
        score += 30
        reasons.append(f"{total_interacoes} interacoes (alto)")
    elif total_interacoes >= 10:
        score += 20
        reasons.append(f"{total_interacoes} interacoes (medio)")
    elif total_interacoes >= 5:
        score += 10
        reasons.append(f"{total_interacoes} interacoes (baixo)")

    # 3. Recencia do contato
    ultimo_contato = contact.get("ultimo_contato")
    if ultimo_contato:
        if isinstance(ultimo_contato, str):
            ultimo_contato = datetime.fromisoformat(ultimo_contato.replace("Z", "+00:00"))
        dias_sem_contato = (datetime.now(ultimo_contato.tzinfo) - ultimo_contato).days if ultimo_contato.tzinfo else (datetime.now() - ultimo_contato).days

        if dias_sem_contato <= 7:
            score += 30
            reasons.append(f"Contato recente ({dias_sem_contato} dias)")
        elif dias_sem_contato <= 30:
            score += 20
            reasons.append(f"Contato no ultimo mes ({dias_sem_contato} dias)")
        elif dias_sem_contato <= 90:
            score += 10
            reasons.append(f"Contato nos ultimos 3 meses ({dias_sem_contato} dias)")

    # 4. Dados completos (indica importancia)
    if contact.get("aniversario"):
        score += 5
        reasons.append("Aniversario cadastrado")
    if contact.get("linkedin"):
        score += 5
        reasons.append("LinkedIn cadastrado")
    if contact.get("empresa"):
        score += 5
        reasons.append("Empresa cadastrada")

    # 5. Contexto pessoal tem bonus
    if contact.get("contexto") == "personal":
        score += 10
        reasons.append("Contexto pessoal")

    # 6. Tags especiais (nao override, mas bonus)
    bonus_tags = ["cliente", "client", "amigo", "friend", "vip", "importante"]
    if has_tag(tags, bonus_tags):
        score += 10
        reasons.append("Tag de importancia")

    # Mapear score para circulo
    if score >= 70:
        circulo = 2  # Proximo
    elif score >= 50:
        circulo = 3  # Ativo
    elif score >= 25:
        circulo = 4  # Conhecido
    else:
        circulo = 5  # Arquivo

    return circulo, score, reasons


def calcular_health_score(contact: Dict, circulo: int = None) -> int:
    """
    Calcula a saude do relacionamento (0-100).
    100 = em dia com frequencia ideal
    0 = muito tempo sem contato
    """
    if circulo is None:
        circulo = contact.get("circulo") or 5

    # Usar frequencia personalizada ou padrao do circulo
    frequencia_ideal = contact.get("frequencia_ideal_dias") or CIRCULO_CONFIG[circulo]["frequencia_dias"]

    ultimo_contato = contact.get("ultimo_contato")
    if not ultimo_contato:
        return 0  # Sem contato registrado = health 0

    if isinstance(ultimo_contato, str):
        ultimo_contato = datetime.fromisoformat(ultimo_contato.replace("Z", "+00:00"))

    dias_sem_contato = (datetime.now(ultimo_contato.tzinfo) - ultimo_contato).days if ultimo_contato.tzinfo else (datetime.now() - ultimo_contato).days

    if dias_sem_contato <= frequencia_ideal:
        return 100

    # Decai linearmente ate 2x a frequencia ideal
    excesso = dias_sem_contato - frequencia_ideal
    limite = frequencia_ideal  # 100% de excesso = 0 health

    health = max(0, 100 - int(excesso / limite * 100))
    return health


def recalcular_circulo_contato(contact_id: int, force: bool = False) -> Dict:
    """
    Recalcula o circulo de um contato especifico.

    Args:
        contact_id: ID do contato
        force: Se True, recalcula mesmo se circulo_manual=True

    Returns:
        Dict com resultado: {circulo, score, health_score, reasons, updated}
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contato
        cursor.execute("""
            SELECT id, nome, tags, total_interacoes, ultimo_contato,
                   aniversario, linkedin, empresa, contexto,
                   circulo, circulo_manual, frequencia_ideal_dias
            FROM contacts WHERE id = %s
        """, (contact_id,))

        contact = cursor.fetchone()
        if not contact:
            return {"error": "Contato nao encontrado"}

        contact = dict(contact)

        # Verificar se e manual e nao estamos forcando
        if contact.get("circulo_manual") and not force:
            return {
                "circulo": contact["circulo"],
                "updated": False,
                "reason": "Circulo definido manualmente"
            }

        # Calcular novo circulo
        circulo, score, reasons = calcular_score_circulo(contact)
        health = calcular_health_score(contact, circulo)

        # Atualizar no banco
        cursor.execute("""
            UPDATE contacts
            SET circulo = %s,
                health_score = %s,
                ultimo_calculo_circulo = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (circulo, health, contact_id))

        return {
            "contact_id": contact_id,
            "nome": contact["nome"],
            "circulo": circulo,
            "circulo_anterior": contact.get("circulo"),
            "score": score,
            "health_score": health,
            "reasons": reasons,
            "updated": True
        }


def recalcular_todos_circulos(force: bool = False, limit: int = None) -> Dict:
    """
    Recalcula circulos de todos os contatos.

    Args:
        force: Se True, recalcula mesmo os manuais
        limit: Limite de contatos a processar (para testes)

    Returns:
        Dict com estatisticas do recalculo
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar todos os contatos
        query = """
            SELECT id, nome, tags, total_interacoes, ultimo_contato,
                   aniversario, linkedin, empresa, contexto,
                   circulo, circulo_manual, frequencia_ideal_dias
            FROM contacts
        """
        if not force:
            query += " WHERE circulo_manual IS NOT TRUE"
        if limit:
            query += f" LIMIT {limit}"

        cursor.execute(query)
        contacts = cursor.fetchall()

        stats = {
            "total": len(contacts),
            "atualizados": 0,
            "por_circulo": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
            "mudancas": []
        }

        for contact in contacts:
            contact = dict(contact)
            circulo_anterior = contact.get("circulo") or 5

            circulo, score, reasons = calcular_score_circulo(contact)
            health = calcular_health_score(contact, circulo)

            # Atualizar
            cursor.execute("""
                UPDATE contacts
                SET circulo = %s,
                    health_score = %s,
                    ultimo_calculo_circulo = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (circulo, health, contact["id"]))

            stats["atualizados"] += 1
            stats["por_circulo"][circulo] += 1

            # Registrar mudanca se circulo mudou
            if circulo != circulo_anterior:
                stats["mudancas"].append({
                    "contact_id": contact["id"],
                    "nome": contact["nome"],
                    "de": circulo_anterior,
                    "para": circulo
                })

        return stats


def get_contatos_precisando_atencao(limit: int = 10) -> List[Dict]:
    """
    Retorna contatos com health_score baixo, priorizando circulos mais proximos.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, nome, empresa, circulo, health_score, ultimo_contato,
                   frequencia_ideal_dias
            FROM contacts
            WHERE circulo <= 4  -- Ignora arquivo
              AND health_score < 50
            ORDER BY circulo ASC, health_score ASC
            LIMIT %s
        """, (limit,))

        return [dict(row) for row in cursor.fetchall()]


def get_aniversarios_proximos(dias: int = 30) -> List[Dict]:
    """
    Retorna contatos com aniversario nos proximos N dias.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Query que funciona com qualquer ano
        cursor.execute("""
            SELECT id, nome, empresa, circulo, aniversario,
                   EXTRACT(DAY FROM aniversario) as dia,
                   EXTRACT(MONTH FROM aniversario) as mes
            FROM contacts
            WHERE aniversario IS NOT NULL
              AND (
                  -- Este ano
                  (EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
                   AND EXTRACT(DAY FROM aniversario) >= EXTRACT(DAY FROM CURRENT_DATE))
                  OR
                  -- Proximo mes se estamos perto do fim do mes
                  (EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE + INTERVAL '%s days'))
              )
            ORDER BY
                EXTRACT(MONTH FROM aniversario),
                EXTRACT(DAY FROM aniversario)
            LIMIT 20
        """, (dias,))

        results = []
        hoje = datetime.now().date()

        for row in cursor.fetchall():
            row = dict(row)
            # Calcular dias ate aniversario
            aniv = row["aniversario"]
            aniv_este_ano = aniv.replace(year=hoje.year)
            if aniv_este_ano < hoje:
                aniv_este_ano = aniv.replace(year=hoje.year + 1)

            dias_ate = (aniv_este_ano - hoje).days
            if dias_ate <= dias:
                row["dias_ate_aniversario"] = dias_ate
                results.append(row)

        return results


def get_dashboard_circulos() -> Dict:
    """
    Retorna dados para o dashboard de circulos.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Contagem por circulo
        cursor.execute("""
            SELECT
                COALESCE(circulo, 5) as circulo,
                COUNT(*) as total,
                AVG(health_score) as health_medio
            FROM contacts
            GROUP BY COALESCE(circulo, 5)
            ORDER BY circulo
        """)

        por_circulo = {}
        for row in cursor.fetchall():
            row = dict(row)
            por_circulo[row["circulo"]] = {
                "total": row["total"],
                "health_medio": round(row["health_medio"] or 0, 1)
            }

        # Preencher circulos vazios
        for c in range(1, 6):
            if c not in por_circulo:
                por_circulo[c] = {"total": 0, "health_medio": 0}

        # Contatos em risco (health < 30%)
        cursor.execute("""
            SELECT COUNT(*) FROM contacts
            WHERE health_score < 30 AND circulo <= 4
        """)
        em_risco = cursor.fetchone()[0]

        return {
            "por_circulo": por_circulo,
            "config": CIRCULO_CONFIG,
            "em_risco": em_risco,
            "precisam_atencao": get_contatos_precisando_atencao(5),
            "aniversarios": get_aniversarios_proximos(14)
        }
```

### 2. MODIFICAR: `app/database.py`

**IMPORTANTE**: Este arquivo e bloqueado. Coordene com ARCH antes de editar.

Adicione estas alteracoes no `init_db()`, apos a criacao da tabela contacts:

```python
# Adicionar colunas de Circulos
cursor.execute('''
    ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS circulo INTEGER DEFAULT 5,
    ADD COLUMN IF NOT EXISTS circulo_manual BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS frequencia_ideal_dias INTEGER,
    ADD COLUMN IF NOT EXISTS ultimo_calculo_circulo TIMESTAMP,
    ADD COLUMN IF NOT EXISTS health_score INTEGER DEFAULT 50
''')

cursor.execute('''
    CREATE INDEX IF NOT EXISTS idx_contacts_circulo ON contacts(circulo)
''')

cursor.execute('''
    CREATE INDEX IF NOT EXISTS idx_contacts_health ON contacts(health_score)
''')
```

## Testes Recomendados

Antes de marcar como pronto, teste:

1. **Classificacao manual**:
   - Contato com tag "familia" -> circulo 1
   - Contato com tag "conselho" -> circulo 2

2. **Classificacao automatica**:
   - Contato com 50+ interacoes e contato recente -> circulo 2/3
   - Contato sem interacoes -> circulo 5

3. **Health score**:
   - Contato do circulo 1 sem contato ha 10 dias -> health < 50
   - Contato do circulo 5 sem contato ha 100 dias -> health ainda alto

4. **Dashboard**:
   - Endpoint retorna dados corretos
   - Contatos precisando atencao ordenados corretamente

## Criterios de Conclusao

- [ ] `app/services/circulos.py` criado com todas as funcoes
- [ ] Testes manuais passando
- [ ] Codigo documentado
- [ ] Atualizar COORDINATION.md com status

## Comunicacao

Ao terminar, atualize `docs/COORDINATION.md`:

```
[DATA INTEL] **FEATURE: Circulos Logic**
Status: PRONTO PARA REVIEW
Arquivos criados:
- app/services/circulos.py (novo)
Arquivos a modificar (requer ARCH):
- app/database.py (schema changes)
Testado: [listar testes realizados]
```

---

**Duvidas?** Consulte `docs/CIRCULOS_ARCHITECTURE.md` ou pergunte ao ARCH.
