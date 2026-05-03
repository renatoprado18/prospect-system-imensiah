"""
Servico de Circulos - Classificacao e Health Score

Sistema DUAL de classificacao de contatos:

PESSOAL (baseado em proximidade real, nao grau de parentesco):
- Circulo 1: Nucleo (convivio diario/semanal) - filhos, pais, namorada, ex-esposa
- Circulo 2: Proximo (convivio frequente) - irmaos, avos, amigos intimos
- Circulo 3: Relacionamento proximo - amigos, alguns primos, padrinhos
- Circulo 4: Ocasional - primos distantes, conhecidos
- Circulo 5: Distante/Arquivo

PROFISSIONAL (baseado em impacto no negocio):
- Circulo 1: Core business - clientes atuais, socios, parceiros ativos, fornecedores criticos
- Circulo 2: Influencia estrategica - conselheiros, mentores, investidores
- Circulo 3: Networking ativo - prospects, fornecedores, ex-clientes ativos
- Circulo 4: Ocasional - contatos de eventos, ex-clientes
- Circulo 5: Arquivo profissional

Contatos MISTOS participam de ambos os circulos.
Health Score usa a MENOR frequencia (mais urgente).

Autor: INTEL
Data: 2026-03-25 (Atualizado: 2026-03-29)
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from database import get_db
import json
import logging

logger = logging.getLogger(__name__)

# ============== CONFIGURACAO CIRCULOS PESSOAIS ==============
CIRCULO_PESSOAL_CONFIG = {
    1: {
        "badge": "P1",
        "nome": "Nucleo",
        "descricao": "Familia proxima, parceiros",
        "frequencia_dias": 7,
        "cor": "#FF6B6B",
        "icone": "heart-fill",
        "peso": 100
    },
    2: {
        "badge": "P2",
        "nome": "Proximo",
        "descricao": "Familia, amigos intimos",
        "frequencia_dias": 30,
        "cor": "#F87171",
        "icone": "heart",
        "peso": 80
    },
    3: {
        "badge": "P3",
        "nome": "Relacionamento",
        "descricao": "Amigos, parentes",
        "frequencia_dias": 30,
        "cor": "#FB923C",
        "icone": "people",
        "peso": 60
    },
    4: {
        "badge": "P4",
        "nome": "Ocasional",
        "descricao": "Conhecidos",
        "frequencia_dias": 90,
        "cor": "#FBBF24",
        "icone": "person",
        "peso": 40
    },
    5: {
        "badge": "P5",
        "nome": "Distante",
        "descricao": "Arquivo pessoal",
        "frequencia_dias": 180,
        "cor": "#A3A3A3",
        "icone": "archive",
        "peso": 20
    },
}

# ============== CONFIGURACAO CIRCULOS PROFISSIONAIS ==============
CIRCULO_PROFISSIONAL_CONFIG = {
    1: {
        "badge": "R1",
        "nome": "Core",
        "descricao": "Clientes, socios, parceiros",
        "frequencia_dias": 14,
        "cor": "#6366F1",
        "icone": "briefcase-fill",
        "peso": 100
    },
    2: {
        "badge": "R2",
        "nome": "Estrategico",
        "descricao": "Conselheiros, mentores",
        "frequencia_dias": 30,
        "cor": "#8B5CF6",
        "icone": "star-fill",
        "peso": 80
    },
    3: {
        "badge": "R3",
        "nome": "Networking",
        "descricao": "Prospects, fornecedores",
        "frequencia_dias": 45,
        "cor": "#0EA5E9",
        "icone": "diagram-3",
        "peso": 60
    },
    4: {
        "badge": "R4",
        "nome": "Ocasional",
        "descricao": "Contatos eventuais",
        "frequencia_dias": 90,
        "cor": "#14B8A6",
        "icone": "person-badge",
        "peso": 40
    },
    5: {
        "badge": "R5",
        "nome": "Arquivo",
        "descricao": "Arquivo profissional",
        "frequencia_dias": 365,
        "cor": "#A3A3A3",
        "icone": "archive",
        "peso": 20
    },
}

# Manter config antiga para compatibilidade (usa menor frequencia)
CIRCULO_CONFIG = CIRCULO_PESSOAL_CONFIG

# ============== TAGS PESSOAIS (Override para circulo especifico) ==============
TAG_PESSOAL_OVERRIDES = {
    1: [
        "filho", "filha", "pai", "mae", "namorada", "namorado",
        "esposa", "esposo", "marido", "wife", "husband",
        "ex-esposa", "ex-marido"  # co-parenting
    ],
    2: [
        "irmao", "irma", "avo", "neto", "neta",
        "amigo-intimo", "melhor-amigo"
    ],
    3: [
        "tio", "tia", "primo", "prima", "sobrinho", "sobrinha",
        "sogro", "sogra", "cunhado", "cunhada", "padrinho", "madrinha",
        "amigo", "amigo-proximo"
    ],
    4: [
        "conhecido", "vizinho"
    ],
}

# ============== TAGS PROFISSIONAIS (Override para circulo especifico) ==============
# R1 = Core: relacionamento ativo e crítico para MEU negócio
# R2 = Estratégico: clientes ativos, mentores, investidores (geram valor direto)
# R3 = Networking: conselheiros, prospects, fornecedores, rede profissional
# R4 = Ocasional: ex-clientes, contatos de eventos, sócios de outras empresas
TAG_PROFISSIONAL_OVERRIDES = {
    1: [
        "cliente-ativo", "co-founder",
        "parceiro-ativo", "fornecedor-critico"
    ],
    2: [
        "cliente", "client", "partner",
        "mentor", "investidor", "investor", "angel"
    ],
    3: [
        "conselheiro", "conselho", "advisor", "board",
        "prospect", "fornecedor", "ex-cliente-ativo",
        "parceiro", "networking"
    ],
    4: [
        "ex-cliente", "contato-evento", "lead",
        "socio"
    ],
}

# Tags que indicam contexto APENAS pessoal (não devem ter circulo profissional)
TAGS_SOMENTE_PESSOAL = [
    "filho", "filha", "pai", "mae", "irmao", "irma",
    "avo", "neto", "neta", "primo", "prima", "tio", "tia",
    "sobrinho", "sobrinha", "sogro", "sogra", "cunhado", "cunhada",
    "namorada", "namorado", "esposa", "esposo", "marido", "wife",
    "ex-esposa", "ex-marido", "padrinho", "madrinha",
    "familia", "family"
]

# Tags que identificam contexto (pessoal vs profissional)
TAGS_PESSOAIS = [
    "familia", "family", "filho", "filha", "pai", "mae", "irmao", "irma",
    "avo", "primo", "tio", "sogro", "cunhado", "amigo", "friend",
    "namorada", "namorado", "esposa", "esposo", "marido", "wife"
]

TAGS_PROFISSIONAIS = [
    "cliente", "client", "socio", "partner", "fornecedor", "supplier",
    "conselheiro", "advisor", "mentor", "investidor", "investor",
    "prospect", "lead", "networking", "profissional", "business"
]

# Tags antigas para compatibilidade
TAG_OVERRIDES = TAG_PESSOAL_OVERRIDES

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


# ============== FUNCOES PARA SISTEMA DUAL ==============

def detectar_contextos(contact: Dict) -> Dict[str, bool]:
    """
    Detecta se o contato pertence ao contexto pessoal, profissional ou ambos.

    Returns:
        Dict com 'pessoal' e 'profissional' como booleans
    """
    tags = parse_tags(contact.get("tags"))
    contexto_str = (contact.get("contexto") or "").lower()

    # Detectar por tags
    tem_tag_pessoal = any(tag in TAGS_PESSOAIS for tag in tags)
    tem_tag_profissional = any(tag in TAGS_PROFISSIONAIS for tag in tags)

    # Detectar por campo contexto
    is_personal = "personal" in contexto_str or "pessoal" in contexto_str
    is_professional = "professional" in contexto_str or "profissional" in contexto_str

    return {
        "pessoal": tem_tag_pessoal or is_personal,
        "profissional": tem_tag_profissional or is_professional or contact.get("empresa")
    }


def calcular_circulo_pessoal(contact: Dict) -> Tuple[Optional[int], List[str]]:
    """
    Calcula o circulo pessoal de um contato.

    Returns:
        Tuple[circulo ou None, lista de razoes]
    """
    tags = parse_tags(contact.get("tags"))
    reasons = []

    # Check tag overrides primeiro
    for circulo, override_tags in TAG_PESSOAL_OVERRIDES.items():
        matched = get_matching_tags(tags, override_tags)
        if matched:
            reasons.append(f"Tag pessoal: {', '.join(matched)}")
            return circulo, reasons

    # Se nao tem tag pessoal especifica, calcular por score
    contextos = detectar_contextos(contact)
    if not contextos["pessoal"]:
        return None, ["Sem contexto pessoal identificado"]

    # Score baseado em interacoes e recencia
    score = 0
    total_interacoes = contact.get("total_interacoes") or 0
    dias_sem_contato = calcular_dias_sem_contato(contact.get("ultimo_contato"))

    if total_interacoes >= 30:
        score += 30
    elif total_interacoes >= 10:
        score += 20
    elif total_interacoes >= 5:
        score += 10

    if dias_sem_contato is not None:
        if dias_sem_contato <= 7:
            score += 30
        elif dias_sem_contato <= 30:
            score += 20
        elif dias_sem_contato <= 90:
            score += 10

    # Mapear para circulo
    if score >= 50:
        return 2, [f"Score pessoal: {score}"]
    elif score >= 30:
        return 3, [f"Score pessoal: {score}"]
    elif score >= 15:
        return 4, [f"Score pessoal: {score}"]
    else:
        return 5, [f"Score pessoal: {score}"]


def calcular_circulo_profissional(contact: Dict) -> Tuple[Optional[int], List[str]]:
    """
    Calcula o circulo profissional de um contato.

    Returns:
        Tuple[circulo ou None, lista de razoes]
    """
    tags = parse_tags(contact.get("tags"))
    reasons = []

    # Se tem tag de familia, NAO deve ter circulo profissional
    familia_tags = get_matching_tags(tags, TAGS_SOMENTE_PESSOAL)
    if familia_tags:
        return None, [f"Contato pessoal/familiar: {', '.join(familia_tags)}"]

    # Check tag overrides primeiro
    for circulo, override_tags in TAG_PROFISSIONAL_OVERRIDES.items():
        matched = get_matching_tags(tags, override_tags)
        if matched:
            reasons.append(f"Tag profissional: {', '.join(matched)}")
            return circulo, reasons

    # Se nao tem tag profissional especifica, calcular por score
    contextos = detectar_contextos(contact)
    if not contextos["profissional"]:
        return None, ["Sem contexto profissional identificado"]

    # Score baseado em interacoes, recencia e dados profissionais
    score = 0
    total_interacoes = contact.get("total_interacoes") or 0
    dias_sem_contato = calcular_dias_sem_contato(contact.get("ultimo_contato"))

    if total_interacoes >= 30:
        score += 25
    elif total_interacoes >= 10:
        score += 15
    elif total_interacoes >= 5:
        score += 8

    if dias_sem_contato is not None:
        if dias_sem_contato <= 14:
            score += 25
        elif dias_sem_contato <= 45:
            score += 15
        elif dias_sem_contato <= 90:
            score += 8

    # Bonus por dados profissionais
    if contact.get("linkedin"):
        score += 10
    if contact.get("empresa"):
        score += 5
    if contact.get("cargo"):
        score += 5

    # Mapear para circulo
    if score >= 50:
        return 2, [f"Score profissional: {score}"]
    elif score >= 30:
        return 3, [f"Score profissional: {score}"]
    elif score >= 15:
        return 4, [f"Score profissional: {score}"]
    else:
        return 5, [f"Score profissional: {score}"]


def calcular_health_dual(contact: Dict) -> Dict:
    """
    Calcula health para ambos os contextos e retorna o efetivo (menor frequencia).

    Returns:
        Dict com health_pessoal, health_profissional, health_efetivo, frequencia_efetiva
    """
    circulo_pessoal = contact.get("circulo_pessoal")
    circulo_profissional = contact.get("circulo_profissional")
    dias_sem_contato = calcular_dias_sem_contato(contact.get("ultimo_contato"))

    result = {
        "health_pessoal": None,
        "health_profissional": None,
        "health_efetivo": 20,  # default se nao tem contato
        "frequencia_efetiva": 365
    }

    if dias_sem_contato is None:
        return result

    # Calcular health pessoal
    if circulo_pessoal:
        freq_pessoal = CIRCULO_PESSOAL_CONFIG.get(circulo_pessoal, {}).get("frequencia_dias", 180)
        if dias_sem_contato <= freq_pessoal:
            result["health_pessoal"] = 100
        else:
            excesso = dias_sem_contato - freq_pessoal
            result["health_pessoal"] = max(0, 100 - int(excesso / freq_pessoal * 100))

    # Calcular health profissional
    if circulo_profissional:
        freq_prof = CIRCULO_PROFISSIONAL_CONFIG.get(circulo_profissional, {}).get("frequencia_dias", 365)
        if dias_sem_contato <= freq_prof:
            result["health_profissional"] = 100
        else:
            excesso = dias_sem_contato - freq_prof
            result["health_profissional"] = max(0, 100 - int(excesso / freq_prof * 100))

    # Health efetivo = menor dos dois (mais urgente)
    healths = [h for h in [result["health_pessoal"], result["health_profissional"]] if h is not None]
    if healths:
        result["health_efetivo"] = min(healths)

        # Frequencia efetiva = menor frequencia
        freqs = []
        if circulo_pessoal:
            freqs.append(CIRCULO_PESSOAL_CONFIG.get(circulo_pessoal, {}).get("frequencia_dias", 180))
        if circulo_profissional:
            freqs.append(CIRCULO_PROFISSIONAL_CONFIG.get(circulo_profissional, {}).get("frequencia_dias", 365))
        if freqs:
            result["frequencia_efetiva"] = min(freqs)

    return result


def recalcular_circulos_dual(contact_id: int, force: bool = False) -> Dict:
    """
    Recalcula ambos os circulos (pessoal e profissional) de um contato.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, nome, tags, total_interacoes, ultimo_contato,
                   aniversario, linkedin, empresa, cargo, foto_url, contexto, score,
                   circulo_pessoal, circulo_profissional,
                   circulo_pessoal_manual, circulo_profissional_manual,
                   health_pessoal, health_profissional, health_score
            FROM contacts
            WHERE id = %s
        """, (contact_id,))

        row = cursor.fetchone()
        if not row:
            return {"error": "Contato nao encontrado", "contact_id": contact_id}

        contact = dict(row)

        # Self-heal: se ultimo_contato NULL mas existem mensagens, backfill com MAX(enviado_em).
        # Cobre casos onde merge/dedup/edicao manual zerou o campo.
        if not contact.get("ultimo_contato"):
            cursor.execute(
                "SELECT MAX(enviado_em) AS ultima FROM messages WHERE contact_id = %s",
                (contact_id,)
            )
            mrow = cursor.fetchone()
            ultima_msg = mrow["ultima"] if mrow else None
            if ultima_msg:
                cursor.execute(
                    "UPDATE contacts SET ultimo_contato = %s WHERE id = %s",
                    (ultima_msg, contact_id)
                )
                contact["ultimo_contato"] = ultima_msg

        # Calcular circulos
        circulo_pessoal, razoes_pessoal = calcular_circulo_pessoal(contact)
        circulo_prof, razoes_prof = calcular_circulo_profissional(contact)

        # Respeitar manual se nao force
        if contact.get("circulo_pessoal_manual") and not force:
            circulo_pessoal = contact.get("circulo_pessoal")
        if contact.get("circulo_profissional_manual") and not force:
            circulo_prof = contact.get("circulo_profissional")

        # Atualizar contact dict para calculo de health
        contact["circulo_pessoal"] = circulo_pessoal
        contact["circulo_profissional"] = circulo_prof

        # Calcular health
        health_result = calcular_health_dual(contact)

        # Circulo efetivo (para compatibilidade) = menor dos dois ou o que existir
        circulo_efetivo = 5
        if circulo_pessoal and circulo_prof:
            # Usar o que tem menor frequencia
            freq_p = CIRCULO_PESSOAL_CONFIG.get(circulo_pessoal, {}).get("frequencia_dias", 999)
            freq_r = CIRCULO_PROFISSIONAL_CONFIG.get(circulo_prof, {}).get("frequencia_dias", 999)
            circulo_efetivo = circulo_pessoal if freq_p <= freq_r else circulo_prof
        elif circulo_pessoal:
            circulo_efetivo = circulo_pessoal
        elif circulo_prof:
            circulo_efetivo = circulo_prof

        # Atualizar no banco
        cursor.execute("""
            UPDATE contacts
            SET circulo_pessoal = %s,
                circulo_profissional = %s,
                health_pessoal = %s,
                health_profissional = %s,
                circulo = %s,
                health_score = %s,
                ultimo_calculo_circulo = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (
            circulo_pessoal,
            circulo_prof,
            health_result["health_pessoal"],
            health_result["health_profissional"],
            circulo_efetivo,
            health_result["health_efetivo"],
            contact_id
        ))

        conn.commit()

        return {
            "contact_id": contact_id,
            "nome": contact.get("nome"),
            "circulo_pessoal": circulo_pessoal,
            "circulo_profissional": circulo_prof,
            "circulo_efetivo": circulo_efetivo,
            "health_pessoal": health_result["health_pessoal"],
            "health_profissional": health_result["health_profissional"],
            "health_efetivo": health_result["health_efetivo"],
            "frequencia_efetiva": health_result["frequencia_efetiva"],
            "razoes_pessoal": razoes_pessoal,
            "razoes_profissional": razoes_prof
        }


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

        # Self-heal: backfill ultimo_contato a partir de messages se NULL (merges/edicoes podem zerar)
        if not contact.get("ultimo_contato"):
            cursor.execute(
                "SELECT MAX(enviado_em) AS ultima FROM messages WHERE contact_id = %s",
                (contact_id,)
            )
            mrow = cursor.fetchone()
            ultima_msg = mrow["ultima"] if mrow else None
            if ultima_msg:
                cursor.execute(
                    "UPDATE contacts SET ultimo_contato = %s WHERE id = %s",
                    (ultima_msg, contact_id)
                )
                contact["ultimo_contato"] = ultima_msg

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
    Recalcula circulos DUAIS (pessoal + profissional) de todos os contatos.

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
                   circulo, circulo_manual, frequencia_ideal_dias, health_score,
                   circulo_pessoal, circulo_profissional,
                   circulo_pessoal_manual, circulo_profissional_manual,
                   health_pessoal, health_profissional
            FROM contacts
        """
        if not force:
            # Nao processar se AMBOS sao manuais
            query += " WHERE NOT (circulo_pessoal_manual IS TRUE AND circulo_profissional_manual IS TRUE)"

        if limit:
            query += f" LIMIT {int(limit)}"

        cursor.execute(query)
        contacts = cursor.fetchall()

        stats = {
            "total": len(contacts),
            "atualizados": 0,
            "ignorados_manual": 0,
            "por_circulo_pessoal": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, None: 0},
            "por_circulo_profissional": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, None: 0},
            "mudancas": []
        }

        for row in contacts:
            contact = dict(row)
            pessoal_anterior = contact.get("circulo_pessoal")
            profissional_anterior = contact.get("circulo_profissional")

            # Calcular circulo pessoal (se nao for manual ou force=True)
            circulo_pessoal = pessoal_anterior
            if force or not contact.get("circulo_pessoal_manual"):
                circulo_pessoal, _ = calcular_circulo_pessoal(contact)

            # Calcular circulo profissional (se nao for manual ou force=True)
            circulo_profissional = profissional_anterior
            if force or not contact.get("circulo_profissional_manual"):
                circulo_profissional, _ = calcular_circulo_profissional(contact)

            # Atualizar contact dict com os novos circulos para calcular health
            contact["circulo_pessoal"] = circulo_pessoal
            contact["circulo_profissional"] = circulo_profissional

            # Calcular health dual (retorna dict com health_pessoal, health_profissional, etc)
            health_result = calcular_health_dual(contact)
            health_pessoal = health_result.get("health_pessoal")
            health_profissional = health_result.get("health_profissional")
            health_efetivo = health_result.get("health_efetivo", 100)

            # Circulo principal (o mais importante = menor numero)
            circulos = [c for c in [circulo_pessoal, circulo_profissional] if c is not None]
            circulo_principal = min(circulos) if circulos else 5

            # Atualizar no banco
            cursor.execute("""
                UPDATE contacts
                SET circulo_pessoal = %s,
                    circulo_profissional = %s,
                    health_pessoal = %s,
                    health_profissional = %s,
                    circulo = %s,
                    health_score = %s,
                    ultimo_calculo_circulo = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (circulo_pessoal, circulo_profissional, health_pessoal, health_profissional,
                  circulo_principal, health_efetivo, contact["id"]))

            stats["atualizados"] += 1
            stats["por_circulo_pessoal"][circulo_pessoal] += 1
            stats["por_circulo_profissional"][circulo_profissional] += 1

            # Registrar mudanca se algum circulo mudou
            if circulo_pessoal != pessoal_anterior or circulo_profissional != profissional_anterior:
                stats["mudancas"].append({
                    "contact_id": contact["id"],
                    "nome": contact["nome"],
                    "pessoal": {"de": pessoal_anterior, "para": circulo_pessoal},
                    "profissional": {"de": profissional_anterior, "para": circulo_profissional}
                })

        logger.info(f"Recalculo dual: {stats['atualizados']} atualizados, {len(stats['mudancas'])} mudancas")

        return stats


def get_contatos_precisando_atencao(limit: int = 10) -> List[Dict]:
    """
    Retorna contatos precisando atencao (versao legacy, usa a nova funcao).
    """
    resultado = get_prioridades_por_contexto(limit_per_context=limit)
    # Combina pessoal e profissional, ordena por score
    todos = resultado.get("pessoal", []) + resultado.get("profissional", [])
    todos.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    return todos[:limit]


def get_prioridades_por_contexto(limit_per_context: int = 15) -> Dict[str, List[Dict]]:
    """
    Retorna contatos ordenados por prioridade, separados por contexto.

    Novo algoritmo baseado em CONTEXTO (nao apenas tempo):
    priority_score = peso_circulo × (1 + soma_fatores)

    Fatores de prioridade:
    - Aniversario hoje: +100
    - Tarefa pendente: +80
    - Projeto/deal ativo: +60
    - Mensagem nao respondida: +50
    - Reuniao esta semana: +40
    - Tempo sem contato (fallback): +20

    Categorias visuais:
    - urgent: Aniversario hoje OU tarefa vencida OU C1/C2 com pendencia
    - important: Tarefa pendente OU projeto ativo OU C1-C3 com pendencia
    - attention: Tempo sem contato acima do baseline

    Returns:
        Dict com 'pessoal' e 'profissional', cada um com lista de contatos
    """
    try:
        return _get_prioridades_por_contexto_impl(limit_per_context)
    except Exception as e:
        logger.error(f"Error in get_prioridades_por_contexto: {e}")
        return {"pessoal": [], "profissional": []}


def _get_prioridades_por_contexto_impl(limit_per_context: int = 15) -> Dict[str, List[Dict]]:
    """Implementation of get_prioridades_por_contexto."""
    with get_db() as conn:
        cursor = conn.cursor()

        hoje = datetime.now().date()

        # === BUSCAR TAREFAS PENDENTES POR CONTATO ===
        tarefas_por_contato = {}
        try:
            cursor.execute("""
                SELECT contact_id, COUNT(*) as count,
                       MIN(data_vencimento) as proxima_vencimento
                FROM tasks
                WHERE status = 'pending' AND contact_id IS NOT NULL
                GROUP BY contact_id
            """)
            for row in cursor.fetchall():
                prox = row["proxima_vencimento"]
                # Handle both date and datetime types
                vencida = False
                if prox:
                    try:
                        prox_date = prox.date() if hasattr(prox, 'date') else prox
                        vencida = prox_date < hoje
                    except (AttributeError, TypeError):
                        pass
                tarefas_por_contato[row["contact_id"]] = {
                    "count": row["count"],
                    "vencida": vencida,
                    "proxima": prox
                }
        except Exception as e:
            logger.warning(f"Error fetching tasks: {e}")
            conn.rollback()

        # === BUSCAR PROJETOS ATIVOS POR CONTATO ===
        projetos_por_contato = {}
        try:
            # projects uses owner_contact_id, and project_members links contacts
            cursor.execute("""
                SELECT owner_contact_id as contact_id, COUNT(*) as count
                FROM projects
                WHERE status = 'active' AND owner_contact_id IS NOT NULL
                GROUP BY owner_contact_id
            """)
            projetos_por_contato = {row["contact_id"]: row["count"] for row in cursor.fetchall()}

            # Also check project_members for linked contacts
            cursor.execute("""
                SELECT pm.contact_id, COUNT(DISTINCT pm.project_id) as count
                FROM project_members pm
                JOIN projects p ON p.id = pm.project_id
                WHERE p.status = 'active' AND pm.contact_id IS NOT NULL
                GROUP BY pm.contact_id
            """)
            for row in cursor.fetchall():
                cid = row["contact_id"]
                if cid in projetos_por_contato:
                    projetos_por_contato[cid] += row["count"]
                else:
                    projetos_por_contato[cid] = row["count"]
        except Exception as e:
            logger.warning(f"Error fetching projects: {e}")
            conn.rollback()

        # === BUSCAR MENSAGENS NAO RESPONDIDAS (ultimos 7 dias) ===
        # Filtra mensagens classificadas como "nao precisa resposta" (rule/LLM/manual).
        # Mensagens NAO classificadas continuam contando (conservador — defaultam
        # como "talvez precisa", classificador roda async no daily-sync).
        mensagens_pendentes = set()
        try:
            cursor.execute("""
                SELECT DISTINCT c.contact_id
                FROM conversations c
                JOIN messages m ON m.conversation_id = c.id
                LEFT JOIN message_classifications mc
                    ON mc.message_id = m.id AND mc.source_table = 'messages'
                WHERE m.direcao = 'incoming'
                  AND m.enviado_em > NOW() - INTERVAL '7 days'
                  AND (mc.requires_reply IS NULL OR mc.requires_reply = TRUE)
                  AND NOT EXISTS (
                      SELECT 1 FROM messages m2
                      WHERE m2.conversation_id = c.id
                        AND m2.direcao = 'outgoing'
                        AND m2.enviado_em > m.enviado_em
                  )
            """)
            mensagens_pendentes = {row["contact_id"] for row in cursor.fetchall()}
        except Exception as e:
            logger.warning(f"Error fetching messages: {e}")
            conn.rollback()

        # === BUSCAR TODOS OS CONTATOS COM CIRCULO ===
        # Filtra contatos snoozed (usuario marcou "adiar ate X dias")
        cursor.execute("""
            SELECT c.id, c.nome, c.empresa, c.cargo, c.foto_url, c.contexto,
                   c.circulo, c.circulo_pessoal, c.circulo_profissional,
                   c.health_score, c.ultimo_contato, c.aniversario,
                   c.frequencia_ideal_dias
            FROM contacts c
            WHERE c.circulo IS NOT NULL
              AND COALESCE(c.circulo, 5) <= 4
              AND NOT EXISTS (
                  SELECT 1 FROM contact_snoozes s
                  WHERE s.contact_id = c.id AND s.ate >= CURRENT_DATE
              )
            ORDER BY c.circulo ASC
        """)

        pessoal = []
        profissional = []

        for row in cursor.fetchall():
            contact = dict(row)
            contact_id = contact["id"]
            contexto = contact.get("contexto") or "professional"

            # Determinar circulo e config baseado no contexto
            if contexto == "personal":
                circulo = contact.get("circulo_pessoal") or contact.get("circulo") or 5
                circulo_cfg = CIRCULO_PESSOAL_CONFIG.get(circulo, CIRCULO_PESSOAL_CONFIG[5])
            else:
                circulo = contact.get("circulo_profissional") or contact.get("circulo") or 5
                circulo_cfg = CIRCULO_PROFISSIONAL_CONFIG.get(circulo, CIRCULO_PROFISSIONAL_CONFIG[5])

            peso_base = circulo_cfg.get("peso", 50)
            badge = circulo_cfg.get("badge", f"C{circulo}")
            contact["badge"] = badge
            contact["circulo_config"] = circulo_cfg

            # === CALCULAR FATORES ===
            fatores = []
            priority_score = peso_base

            # 1. Aniversario hoje (+100)
            aniversario = contact.get("aniversario")
            aniversario_hoje = False
            if aniversario:
                try:
                    if aniversario.month == hoje.month and aniversario.day == hoje.day:
                        priority_score += 100
                        fatores.append({"tipo": "birthday", "label": "Aniversario!"})
                        aniversario_hoje = True
                except (ValueError, AttributeError):
                    pass

            # 2. Tarefa pendente — so vencida vira fator (acao imediata).
            # Tarefa futura ainda boosta priority_score (ordena melhor) mas nao
            # entra como trigger standalone — bate com principio "inbox de acoes".
            tarefa_info = tarefas_por_contato.get(contact_id)
            if tarefa_info:
                if tarefa_info["vencida"]:
                    priority_score += 100
                    fatores.append({"tipo": "task", "label": "Tarefa vencida"})
                else:
                    priority_score += 80  # boost only, no factor

            # 3. Projeto ativo (+60)
            projeto_count = projetos_por_contato.get(contact_id, 0)
            if projeto_count > 0:
                priority_score += 60
                fatores.append({"tipo": "project", "label": "Projeto ativo"})

            # 4. Mensagem nao respondida (+50)
            if contact_id in mensagens_pendentes:
                priority_score += 50
                fatores.append({"tipo": "message", "label": "Responder msg"})

            # 5. Tempo sem contato — boost de prioridade pra contatos JA com fator,
            # mas NAO conta como fator standalone (passive timeout != trigger acionavel).
            # Antes: virava fator 'time' quando sem outros — gerava 130+ contatos sem motivo claro.
            dias_sem_contato = calcular_dias_sem_contato(contact.get("ultimo_contato"))
            contact["dias_sem_contato"] = dias_sem_contato
            frequencia_ideal = contact.get("frequencia_ideal_dias") or circulo_cfg.get("frequencia_dias", 30)

            if dias_sem_contato and dias_sem_contato > frequencia_ideal:
                priority_score += 20
                # Nao adiciona como fator — so boosta priority_score de quem ja tem trigger

            contact["priority_score"] = round(priority_score, 1)
            contact["fatores"] = fatores

            # === CATEGORIA VISUAL ===
            if aniversario_hoje or (tarefa_info and tarefa_info["vencida"]) or (circulo <= 2 and fatores):
                contact["category"] = "urgent"
            elif fatores and circulo <= 3:
                contact["category"] = "important"
            elif fatores:
                contact["category"] = "attention"
            else:
                continue  # Sem fatores = nao precisa atencao

            # Separar por contexto
            if contexto == "personal":
                pessoal.append(contact)
            else:
                profissional.append(contact)

        # Ordenar por priority_score
        pessoal.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
        profissional.sort(key=lambda x: x.get("priority_score", 0), reverse=True)

        return {
            "pessoal": pessoal[:limit_per_context],
            "profissional": profissional[:limit_per_context]
        }


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


def get_dashboard_circulos(contexto: str = None) -> Dict:
    """
    Retorna dados consolidados para o dashboard de circulos.

    Novo formato com dados separados para Pessoal e Profissional.

    Returns:
        Dict com estatisticas para ambos os contextos
    """
    # Default response structure
    default_response = {
        "pessoal": {
            "total": 0,
            "por_circulo": {c: {"total": 0, "health_medio": 0, "config": CIRCULO_PESSOAL_CONFIG[c]} for c in range(1, 6)},
            "config": CIRCULO_PESSOAL_CONFIG,
            "prioridades": []
        },
        "profissional": {
            "total": 0,
            "por_circulo": {c: {"total": 0, "health_medio": 0, "config": CIRCULO_PROFISSIONAL_CONFIG[c]} for c in range(1, 6)},
            "config": CIRCULO_PROFISSIONAL_CONFIG,
            "prioridades": []
        },
        "total": 0,
        "by_context": {"personal": 0, "professional": 0}
    }

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # === STATS PESSOAL ===
            cursor.execute("""
                SELECT
                    COALESCE(circulo_pessoal, circulo, 5) as circulo,
                    COUNT(*) as total,
                    AVG(COALESCE(health_score, 50)) as health_medio
                FROM contacts
                WHERE contexto = 'personal'
                GROUP BY COALESCE(circulo_pessoal, circulo, 5)
                ORDER BY circulo
            """)

            pessoal_por_circulo = {}
            pessoal_total = 0
            for row in cursor.fetchall():
                c = row["circulo"]
                cfg = CIRCULO_PESSOAL_CONFIG.get(c, CIRCULO_PESSOAL_CONFIG[5])
                pessoal_por_circulo[c] = {
                    "total": row["total"],
                    "health_medio": round(row["health_medio"] or 50, 1),
                    "config": cfg
                }
                pessoal_total += row["total"]

            # Preencher circulos vazios pessoal
            for c in range(1, 6):
                if c not in pessoal_por_circulo:
                    pessoal_por_circulo[c] = {
                        "total": 0,
                        "health_medio": 0,
                        "config": CIRCULO_PESSOAL_CONFIG[c]
                    }

            # === STATS PROFISSIONAL ===
            cursor.execute("""
                SELECT
                    COALESCE(circulo_profissional, circulo, 5) as circulo,
                    COUNT(*) as total,
                    AVG(COALESCE(health_score, 50)) as health_medio
                FROM contacts
                WHERE COALESCE(contexto, 'professional') != 'personal'
                GROUP BY COALESCE(circulo_profissional, circulo, 5)
                ORDER BY circulo
            """)

            profissional_por_circulo = {}
            profissional_total = 0
            for row in cursor.fetchall():
                c = row["circulo"]
                cfg = CIRCULO_PROFISSIONAL_CONFIG.get(c, CIRCULO_PROFISSIONAL_CONFIG[5])
                profissional_por_circulo[c] = {
                    "total": row["total"],
                    "health_medio": round(row["health_medio"] or 50, 1),
                    "config": cfg
                }
                profissional_total += row["total"]

            # Preencher circulos vazios profissional
            for c in range(1, 6):
                if c not in profissional_por_circulo:
                    profissional_por_circulo[c] = {
                        "total": 0,
                        "health_medio": 0,
                        "config": CIRCULO_PROFISSIONAL_CONFIG[c]
                    }

        # === PRIORIDADES POR CONTEXTO === (outside db connection)
        prioridades = get_prioridades_por_contexto(limit_per_context=15)

        return {
            "pessoal": {
                "total": pessoal_total,
                "por_circulo": pessoal_por_circulo,
                "config": CIRCULO_PESSOAL_CONFIG,
                "prioridades": prioridades.get("pessoal", [])
            },
            "profissional": {
                "total": profissional_total,
                "por_circulo": profissional_por_circulo,
                "config": CIRCULO_PROFISSIONAL_CONFIG,
                "prioridades": prioridades.get("profissional", [])
            },
            # Legacy fields for compatibility
            "total": pessoal_total + profissional_total,
            "by_context": {
                "personal": pessoal_total,
                "professional": profissional_total
            }
        }

    except Exception as e:
        logger.error(f"Error in get_dashboard_circulos: {e}")
        return default_response


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
    offset: int = 0,
    contexto: str = None
) -> Dict:
    """
    Lista contatos de um circulo especifico com paginacao.

    Args:
        circulo: Numero do circulo (1-5)
        sort_by: Campo para ordenacao (health, nome, ultimo_contato)
        limit: Limite de resultados
        offset: Offset para paginacao
        contexto: 'pessoal' ou 'profissional' (usa circulo_pessoal/circulo_profissional)

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

    # Determine which circle field and context filter to use
    if contexto == "pessoal":
        circulo_field = "COALESCE(circulo_pessoal, circulo, 5)"
        context_filter = "AND contexto = 'personal'"
        config = CIRCULO_PESSOAL_CONFIG.get(circulo, CIRCULO_PESSOAL_CONFIG[5])
    elif contexto == "profissional":
        circulo_field = "COALESCE(circulo_profissional, circulo, 5)"
        context_filter = "AND COALESCE(contexto, 'professional') != 'personal'"
        config = CIRCULO_PROFISSIONAL_CONFIG.get(circulo, CIRCULO_PROFISSIONAL_CONFIG[5])
    else:
        # Legacy: use general circulo field
        circulo_field = "COALESCE(circulo, 5)"
        context_filter = ""
        config = CIRCULO_CONFIG.get(circulo, CIRCULO_CONFIG[5])

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contatos
        cursor.execute(f"""
            SELECT id, nome, empresa, cargo, foto_url, emails, telefones,
                   circulo, circulo_pessoal, circulo_profissional, contexto,
                   health_score, ultimo_contato, total_interacoes,
                   frequencia_ideal_dias, circulo_manual, tags, aniversario
            FROM contacts
            WHERE {circulo_field} = %s {context_filter}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
        """, (circulo, limit, offset))

        contacts = []
        for row in cursor.fetchall():
            contact = dict(row)
            contact["dias_sem_contato"] = calcular_dias_sem_contato(contact.get("ultimo_contato"))
            # Add badge based on context
            if contexto == "pessoal":
                contact["badge"] = CIRCULO_PESSOAL_CONFIG.get(circulo, {}).get("badge", f"P{circulo}")
            elif contexto == "profissional":
                contact["badge"] = CIRCULO_PROFISSIONAL_CONFIG.get(circulo, {}).get("badge", f"R{circulo}")
            contacts.append(contact)

        # Total count
        cursor.execute(f"""
            SELECT COUNT(*) as count
            FROM contacts
            WHERE {circulo_field} = %s {context_filter}
        """, (circulo,))
        total = cursor.fetchone()["count"]

        return {
            "circulo": circulo,
            "contexto": contexto,
            "config": config,
            "total": total,
            "limit": limit,
            "offset": offset,
            "contacts": contacts
        }
