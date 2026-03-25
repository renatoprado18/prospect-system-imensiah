"""
Servico de Circulos - Classificacao e Health Score

Sistema de classificacao de contatos em 5 niveis de proximidade:
- Circulo 1: Intimo (familia, amigos proximos)
- Circulo 2: Proximo (parceiros, mentores, conselheiros)
- Circulo 3: Ativo (networking ativo, clientes-chave)
- Circulo 4: Conhecido (contatos ocasionais)
- Circulo 5: Arquivo (demais contatos)

Autor: INTEL
Data: 2026-03-25
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from database import get_db
import json
import logging

logger = logging.getLogger(__name__)

# Configuracao padrao dos circulos
CIRCULO_CONFIG = {
    1: {
        "nome": "Intimo",
        "descricao": "Familia e amigos proximos",
        "frequencia_dias": 7,
        "cor": "#FF6B6B",
        "icone": "heart"
    },
    2: {
        "nome": "Proximo",
        "descricao": "Parceiros e mentores",
        "frequencia_dias": 14,
        "cor": "#4ECDC4",
        "icone": "star"
    },
    3: {
        "nome": "Ativo",
        "descricao": "Networking ativo",
        "frequencia_dias": 30,
        "cor": "#45B7D1",
        "icone": "briefcase"
    },
    4: {
        "nome": "Conhecido",
        "descricao": "Contatos ocasionais",
        "frequencia_dias": 90,
        "cor": "#96CEB4",
        "icone": "users"
    },
    5: {
        "nome": "Arquivo",
        "descricao": "Demais contatos",
        "frequencia_dias": 365,
        "cor": "#DDA0DD",
        "icone": "archive"
    },
}

# Tags que fazem override direto para circulo especifico
TAG_OVERRIDES = {
    1: [
        "familia", "family", "esposa", "wife", "marido", "husband",
        "filho", "filha", "son", "daughter", "pai", "mae", "father", "mother",
        "irmao", "irma", "brother", "sister", "avo", "grandparent",
        "primo", "prima", "cousin", "tio", "tia", "uncle", "aunt",
        "sogro", "sogra", "cunhado", "cunhada"
    ],
    2: [
        "conselho", "board", "advisor", "conselheiro", "mentor",
        "socio", "partner", "co-founder", "cofundador",
        "investidor", "investor", "angel"
    ],
}

# Tags que dao bonus (nao override)
BONUS_TAGS = {
    "cliente": 15,
    "client": 15,
    "vip": 20,
    "importante": 15,
    "key": 15,
    "amigo": 10,
    "friend": 10,
    "parceiro": 10,
    "partner": 10,
}


def parse_tags(tags: Any) -> List[str]:
    """Parse tags de diferentes formatos para lista de strings."""
    if not tags:
        return []
    if isinstance(tags, list):
        return [str(t).lower().strip() for t in tags if t]
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
            if isinstance(parsed, list):
                return [str(t).lower().strip() for t in parsed if t]
        except (json.JSONDecodeError, TypeError):
            pass
        # Tenta separar por virgula
        return [t.lower().strip() for t in tags.split(',') if t.strip()]
    return []


def has_tag(contact_tags: List[str], target_tags: List[str]) -> bool:
    """Verifica se contato tem alguma das tags alvo."""
    if not contact_tags or not target_tags:
        return False
    return any(tag in contact_tags for tag in target_tags)


def get_matching_tags(contact_tags: List[str], target_tags: List[str]) -> List[str]:
    """Retorna as tags que deram match."""
    if not contact_tags or not target_tags:
        return []
    return [tag for tag in contact_tags if tag in target_tags]


def parse_datetime(dt: Any) -> Optional[datetime]:
    """Parse datetime de diferentes formatos."""
    if not dt:
        return None
    if isinstance(dt, datetime):
        return dt
    if isinstance(dt, str):
        try:
            # ISO format com ou sem timezone
            if 'Z' in dt:
                dt = dt.replace('Z', '+00:00')
            if '+' in dt or '-' in dt[10:]:  # Has timezone
                return datetime.fromisoformat(dt)
            return datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            pass
    return None


def calcular_dias_sem_contato(ultimo_contato: Any) -> Optional[int]:
    """Calcula dias desde o ultimo contato."""
    dt = parse_datetime(ultimo_contato)
    if not dt:
        return None

    # Normalizar para naive datetime para comparacao
    now = datetime.now()
    if dt.tzinfo:
        dt = dt.replace(tzinfo=None)

    delta = now - dt
    return max(0, delta.days)


def calcular_score_circulo(contact: Dict) -> Tuple[int, int, List[str]]:
    """
    Calcula o circulo de um contato baseado em multiplos fatores.

    Args:
        contact: Dicionario com dados do contato

    Returns:
        Tuple[circulo, score, reasons]:
            - circulo (1-5)
            - score (0-100+)
            - lista de razoes para a classificacao
    """
    score = 0
    reasons = []

    # Parse tags
    tags = parse_tags(contact.get("tags"))

    # 1. Check for tag overrides (familia, conselho, etc) - PRIORIDADE MAXIMA
    for circulo, override_tags in TAG_OVERRIDES.items():
        matched = get_matching_tags(tags, override_tags)
        if matched:
            reasons.append(f"Tag especial: {', '.join(matched)}")
            return circulo, 100, reasons

    # 2. Frequencia de interacao
    total_interacoes = contact.get("total_interacoes") or 0
    if total_interacoes >= 50:
        score += 40
        reasons.append(f"{total_interacoes} interacoes (muito frequente)")
    elif total_interacoes >= 20:
        score += 30
        reasons.append(f"{total_interacoes} interacoes (frequente)")
    elif total_interacoes >= 10:
        score += 20
        reasons.append(f"{total_interacoes} interacoes (regular)")
    elif total_interacoes >= 5:
        score += 10
        reasons.append(f"{total_interacoes} interacoes (ocasional)")
    elif total_interacoes > 0:
        score += 5
        reasons.append(f"{total_interacoes} interacoes (raro)")

    # 3. Recencia do contato
    dias_sem_contato = calcular_dias_sem_contato(contact.get("ultimo_contato"))
    if dias_sem_contato is not None:
        if dias_sem_contato <= 7:
            score += 30
            reasons.append(f"Contato recente ({dias_sem_contato} dias)")
        elif dias_sem_contato <= 30:
            score += 20
            reasons.append(f"Contato no ultimo mes ({dias_sem_contato} dias)")
        elif dias_sem_contato <= 90:
            score += 10
            reasons.append(f"Contato nos ultimos 3 meses ({dias_sem_contato} dias)")
        elif dias_sem_contato <= 180:
            score += 5
            reasons.append(f"Contato nos ultimos 6 meses ({dias_sem_contato} dias)")

    # 4. Dados completos (indica que o contato e importante)
    completude_bonus = 0
    if contact.get("aniversario"):
        completude_bonus += 5
    if contact.get("linkedin"):
        completude_bonus += 5
    if contact.get("empresa"):
        completude_bonus += 3
    if contact.get("cargo"):
        completude_bonus += 2
    if contact.get("foto_url"):
        completude_bonus += 2

    if completude_bonus > 0:
        score += completude_bonus
        reasons.append(f"Perfil completo (+{completude_bonus})")

    # 5. Contexto pessoal tem bonus
    if contact.get("contexto") == "personal":
        score += 10
        reasons.append("Contexto pessoal")

    # 6. Bonus por tags especiais (nao override)
    tag_bonus = 0
    bonus_matched = []
    for tag in tags:
        if tag in BONUS_TAGS:
            tag_bonus += BONUS_TAGS[tag]
            bonus_matched.append(tag)

    if tag_bonus > 0:
        score += tag_bonus
        reasons.append(f"Tags bonus: {', '.join(bonus_matched)} (+{tag_bonus})")

    # 7. Score de sistema existente (se houver)
    existing_score = contact.get("score") or 0
    if existing_score >= 80:
        score += 15
        reasons.append(f"Score alto no sistema ({existing_score})")
    elif existing_score >= 60:
        score += 10
        reasons.append(f"Score medio-alto ({existing_score})")
    elif existing_score >= 40:
        score += 5
        reasons.append(f"Score medio ({existing_score})")

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
    0 = muito tempo sem contato (precisa atencao urgente)

    Args:
        contact: Dicionario com dados do contato
        circulo: Circulo do contato (se None, usa o do contact ou calcula)

    Returns:
        Health score de 0 a 100
    """
    if circulo is None:
        circulo = contact.get("circulo") or 5

    # Usar frequencia personalizada ou padrao do circulo
    frequencia_ideal = contact.get("frequencia_ideal_dias")
    if not frequencia_ideal:
        frequencia_ideal = CIRCULO_CONFIG.get(circulo, CIRCULO_CONFIG[5])["frequencia_dias"]

    dias_sem_contato = calcular_dias_sem_contato(contact.get("ultimo_contato"))

    if dias_sem_contato is None:
        # Sem registro de contato - health baixo mas nao zero
        return 20

    if dias_sem_contato <= frequencia_ideal:
        return 100

    # Decai linearmente ate 2x a frequencia ideal = 0 health
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
        Dict com resultado da operacao
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contato com todos os campos necessarios
        cursor.execute("""
            SELECT id, nome, tags, total_interacoes, ultimo_contato,
                   aniversario, linkedin, empresa, cargo, foto_url, contexto, score,
                   circulo, circulo_manual, frequencia_ideal_dias, health_score
            FROM contacts
            WHERE id = %s
        """, (contact_id,))

        row = cursor.fetchone()
        if not row:
            return {"error": "Contato nao encontrado", "contact_id": contact_id}

        contact = dict(row)

        # Verificar se e manual e nao estamos forcando
        if contact.get("circulo_manual") and not force:
            # Apenas recalcula health score
            health = calcular_health_score(contact, contact.get("circulo"))
            cursor.execute("""
                UPDATE contacts SET health_score = %s WHERE id = %s
            """, (health, contact_id))

            return {
                "contact_id": contact_id,
                "nome": contact["nome"],
                "circulo": contact["circulo"],
                "health_score": health,
                "updated": False,
                "reason": "Circulo definido manualmente (apenas health atualizado)"
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
                   aniversario, linkedin, empresa, cargo, foto_url, contexto, score,
                   circulo, circulo_manual, frequencia_ideal_dias, health_score
            FROM contacts
        """
        if not force:
            query += " WHERE circulo_manual IS NOT TRUE OR circulo_manual IS NULL"

        if limit:
            query += f" LIMIT {int(limit)}"

        cursor.execute(query)
        contacts = cursor.fetchall()

        stats = {
            "total": len(contacts),
            "atualizados": 0,
            "ignorados_manual": 0,
            "por_circulo": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
            "mudancas": []
        }

        for row in contacts:
            contact = dict(row)
            circulo_anterior = contact.get("circulo") or 5

            # Calcular novo circulo e health
            circulo, score, reasons = calcular_score_circulo(contact)
            health = calcular_health_score(contact, circulo)

            # Atualizar no banco
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
                    "para": circulo,
                    "reasons": reasons[:2]  # Limita para nao poluir
                })

        logger.info(f"Recalculo de circulos: {stats['atualizados']} atualizados, {len(stats['mudancas'])} mudancas")

        return stats


def get_contatos_precisando_atencao(limit: int = 10) -> List[Dict]:
    """
    Retorna contatos com health_score baixo, priorizando circulos mais proximos.

    Args:
        limit: Numero maximo de contatos a retornar

    Returns:
        Lista de contatos precisando atencao
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, nome, empresa, cargo, foto_url,
                   circulo, health_score, ultimo_contato,
                   frequencia_ideal_dias, emails, telefones
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 4
              AND COALESCE(health_score, 50) < 50
            ORDER BY
                COALESCE(circulo, 5) ASC,
                COALESCE(health_score, 50) ASC
            LIMIT %s
        """, (limit,))

        results = []
        for row in cursor.fetchall():
            contact = dict(row)
            # Calcular dias sem contato para exibicao
            contact["dias_sem_contato"] = calcular_dias_sem_contato(contact.get("ultimo_contato"))
            # Config do circulo
            contact["circulo_config"] = CIRCULO_CONFIG.get(contact.get("circulo") or 5)
            results.append(contact)

        return results


def get_aniversarios_proximos(dias: int = 30) -> List[Dict]:
    """
    Retorna contatos com aniversario nos proximos N dias.

    Args:
        dias: Numero de dias a frente para buscar

    Returns:
        Lista de contatos com aniversario proximo
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Query que funciona com qualquer ano usando EXTRACT
        cursor.execute("""
            SELECT id, nome, empresa, cargo, foto_url,
                   circulo, health_score, aniversario
            FROM contacts
            WHERE aniversario IS NOT NULL
            ORDER BY
                EXTRACT(MONTH FROM aniversario),
                EXTRACT(DAY FROM aniversario)
        """)

        results = []
        hoje = datetime.now().date()

        for row in cursor.fetchall():
            contact = dict(row)
            aniv = contact.get("aniversario")

            if not aniv:
                continue

            # Calcular proxima ocorrencia do aniversario
            try:
                aniv_este_ano = aniv.replace(year=hoje.year)
                if aniv_este_ano < hoje:
                    aniv_este_ano = aniv.replace(year=hoje.year + 1)

                dias_ate = (aniv_este_ano - hoje).days

                if 0 <= dias_ate <= dias:
                    contact["dias_ate_aniversario"] = dias_ate
                    contact["aniversario_formatado"] = aniv.strftime("%d/%m")
                    contact["circulo_config"] = CIRCULO_CONFIG.get(contact.get("circulo") or 5)
                    results.append(contact)
            except (ValueError, AttributeError):
                continue

        # Ordenar por dias ate aniversario
        results.sort(key=lambda x: x.get("dias_ate_aniversario", 999))

        return results[:20]  # Limita a 20


def get_dashboard_circulos() -> Dict:
    """
    Retorna dados consolidados para o dashboard de circulos.

    Returns:
        Dict com estatisticas e dados do dashboard
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Contagem e health medio por circulo
        cursor.execute("""
            SELECT
                COALESCE(circulo, 5) as circulo,
                COUNT(*) as total,
                AVG(COALESCE(health_score, 50)) as health_medio
            FROM contacts
            GROUP BY COALESCE(circulo, 5)
            ORDER BY circulo
        """)

        por_circulo = {}
        total_geral = 0
        for row in cursor.fetchall():
            c = row["circulo"]
            por_circulo[c] = {
                "total": row["total"],
                "health_medio": round(row["health_medio"] or 50, 1),
                "config": CIRCULO_CONFIG.get(c, CIRCULO_CONFIG[5])
            }
            total_geral += row["total"]

        # Preencher circulos vazios
        for c in range(1, 6):
            if c not in por_circulo:
                por_circulo[c] = {
                    "total": 0,
                    "health_medio": 0,
                    "config": CIRCULO_CONFIG[c]
                }

        # Contatos em risco (health < 30% em circulos 1-4)
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM contacts
            WHERE COALESCE(health_score, 50) < 30
              AND COALESCE(circulo, 5) <= 4
        """)
        em_risco = cursor.fetchone()["count"]

        # Contatos sem circulo definido
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM contacts
            WHERE circulo IS NULL
        """)
        sem_circulo = cursor.fetchone()["count"]

        return {
            "por_circulo": por_circulo,
            "config": CIRCULO_CONFIG,
            "total_contatos": total_geral,
            "em_risco": em_risco,
            "sem_circulo": sem_circulo,
            "precisam_atencao": get_contatos_precisando_atencao(5),
            "aniversarios": get_aniversarios_proximos(14)
        }


def definir_circulo_manual(
    contact_id: int,
    circulo: int,
    frequencia_ideal_dias: int = None
) -> Dict:
    """
    Define manualmente o circulo de um contato.

    Args:
        contact_id: ID do contato
        circulo: Circulo a definir (1-5)
        frequencia_ideal_dias: Frequencia personalizada (opcional)

    Returns:
        Dict com resultado da operacao
    """
    if circulo < 1 or circulo > 5:
        return {"error": "Circulo deve ser entre 1 e 5"}

    with get_db() as conn:
        cursor = conn.cursor()

        # Verificar se contato existe
        cursor.execute("SELECT id, nome FROM contacts WHERE id = %s", (contact_id,))
        contact = cursor.fetchone()
        if not contact:
            return {"error": "Contato nao encontrado"}

        # Definir frequencia se nao especificada
        if not frequencia_ideal_dias:
            frequencia_ideal_dias = CIRCULO_CONFIG[circulo]["frequencia_dias"]

        # Atualizar
        cursor.execute("""
            UPDATE contacts
            SET circulo = %s,
                circulo_manual = TRUE,
                frequencia_ideal_dias = %s,
                ultimo_calculo_circulo = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING id, nome, circulo, circulo_manual, frequencia_ideal_dias
        """, (circulo, frequencia_ideal_dias, contact_id))

        result = dict(cursor.fetchone())
        result["circulo_config"] = CIRCULO_CONFIG[circulo]

        return result


def remover_circulo_manual(contact_id: int) -> Dict:
    """
    Remove a definicao manual de circulo, permitindo recalculo automatico.

    Args:
        contact_id: ID do contato

    Returns:
        Dict com resultado da operacao
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE contacts
            SET circulo_manual = FALSE,
                frequencia_ideal_dias = NULL
            WHERE id = %s
            RETURNING id, nome
        """, (contact_id,))

        result = cursor.fetchone()
        if not result:
            return {"error": "Contato nao encontrado"}

        # Recalcular automaticamente
        return recalcular_circulo_contato(contact_id)


def get_contatos_por_circulo(
    circulo: int,
    sort_by: str = "health",
    limit: int = 50,
    offset: int = 0
) -> Dict:
    """
    Lista contatos de um circulo especifico com paginacao.

    Args:
        circulo: Numero do circulo (1-5)
        sort_by: Campo para ordenacao (health, nome, ultimo_contato)
        limit: Limite de resultados
        offset: Offset para paginacao

    Returns:
        Dict com contatos e metadados
    """
    # Validar sort_by
    sort_options = {
        "health": "COALESCE(health_score, 50) ASC",
        "nome": "nome ASC",
        "ultimo_contato": "ultimo_contato DESC NULLS LAST"
    }
    order_by = sort_options.get(sort_by, sort_options["health"])

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contatos
        cursor.execute(f"""
            SELECT id, nome, empresa, cargo, foto_url, emails, telefones,
                   circulo, health_score, ultimo_contato, total_interacoes,
                   frequencia_ideal_dias, circulo_manual, tags
            FROM contacts
            WHERE COALESCE(circulo, 5) = %s
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
        """, (circulo, limit, offset))

        contacts = []
        for row in cursor.fetchall():
            contact = dict(row)
            contact["dias_sem_contato"] = calcular_dias_sem_contato(contact.get("ultimo_contato"))
            contacts.append(contact)

        # Total count
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM contacts
            WHERE COALESCE(circulo, 5) = %s
        """, (circulo,))
        total = cursor.fetchone()["count"]

        return {
            "circulo": circulo,
            "config": CIRCULO_CONFIG.get(circulo, CIRCULO_CONFIG[5]),
            "total": total,
            "limit": limit,
            "offset": offset,
            "contacts": contacts
        }
