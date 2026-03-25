"""
Servico de Tags Automaticas - Classificacao Inteligente de Contatos

Analisa dados do contato e sugere/aplica tags automaticamente baseado em:
- Empresa (setor, tipo)
- Cargo (nivel hierarquico)
- Email domain (governo, educacao, etc)
- Historico de mensagens (keywords)

Autor: INTEL
Data: 2026-03-25
"""

from datetime import datetime
from typing import Dict, List, Optional, Set
import json
import re
import logging

from database import get_db

logger = logging.getLogger(__name__)

# ============== REGRAS DE DETECCAO ==============

# Mapeamento de empresas/keywords para tags de setor
EMPRESA_TAGS = {
    "financeiro": [
        "banco", "bank", "itau", "bradesco", "santander", "btg", "xp",
        "nubank", "inter", "c6", "modal", "safra", "credit", "capital",
        "investimento", "investment", "asset", "gestora", "fundo",
        "seguradora", "insurance", "previdencia"
    ],
    "tecnologia": [
        "tech", "software", "sistemas", "digital", "data", "cloud",
        "microsoft", "google", "amazon", "aws", "oracle", "sap",
        "totvs", "linx", "stone", "pag", "ifood", "uber", "99",
        "startup", "fintech", "healthtech", "edtech", "agtech"
    ],
    "consultoria": [
        "consult", "advisory", "mckinsey", "bain", "bcg", "kpmg",
        "deloitte", "pwc", "ey", "accenture", "strategy"
    ],
    "juridico": [
        "advogado", "lawyer", "advocacia", "law", "legal", "juridico",
        "escritorio", "mattos filho", "machado meyer", "pinheiro neto",
        "tozzini", "veirano"
    ],
    "saude": [
        "hospital", "clinica", "clinic", "health", "saude", "medic",
        "pharma", "farmaceutic", "biotech", "einstein", "sirio",
        "dasa", "fleury", "hapvida", "notredame"
    ],
    "varejo": [
        "retail", "varejo", "loja", "store", "magazine", "luiza",
        "americanas", "casas bahia", "carrefour", "atacadao",
        "mercado", "supermercado"
    ],
    "industria": [
        "industria", "industrial", "manufacturing", "fabrica",
        "producao", "metalurgica", "siderurgica", "vale", "gerdau",
        "embraer", "weg", "usiminas"
    ],
    "energia": [
        "energia", "energy", "eletric", "petrol", "oil", "gas",
        "petrobras", "shell", "enel", "cpfl", "eletrobras",
        "renovavel", "solar", "eolica"
    ],
    "educacao": [
        "universidade", "university", "faculdade", "college",
        "escola", "school", "educacao", "education", "ensino",
        "kroton", "cogna", "yduqs", "estacio"
    ],
    "imobiliario": [
        "imobiliaria", "real estate", "construtora", "construction",
        "incorporadora", "cyrela", "mrv", "even", "eztec",
        "gafisa", "tenda"
    ]
}

# Mapeamento de cargos para tags de nivel
CARGO_TAGS = {
    "c-level": [
        "ceo", "cfo", "cto", "coo", "cmo", "cio", "chro",
        "chief", "presidente", "president"
    ],
    "diretor": [
        "diretor", "director", "vp", "vice-president",
        "vice presidente", "head of", "head"
    ],
    "gerente": [
        "gerente", "manager", "coordenador", "coordinator",
        "supervisor", "lider", "leader", "lead"
    ],
    "socio": [
        "socio", "partner", "founding", "fundador", "founder",
        "co-founder", "cofundador", "owner", "proprietario"
    ],
    "conselheiro": [
        "conselheiro", "board", "advisor", "membro do conselho",
        "board member", "chairman", "presidente do conselho"
    ],
    "executivo": [
        "executivo", "executive", "officer", "senior"
    ]
}

# Dominios de email para tags
EMAIL_DOMAIN_TAGS = {
    "governo": [
        ".gov.br", ".gov", ".mil.br", ".jus.br", ".leg.br",
        ".mp.br", ".def.br"
    ],
    "educacao": [
        ".edu.br", ".edu", ".ac.br", "usp.br", "unicamp.br",
        "ufrj.br", "ufmg.br", "puc"
    ],
    "organizacao": [
        ".org.br", ".org", ".ong"
    ]
}

# Keywords em mensagens para tags
MESSAGE_KEYWORDS = {
    "investidor": [
        "investimento", "investment", "aporte", "rodada", "round",
        "equity", "valuation", "cap table", "term sheet"
    ],
    "cliente": [
        "proposta", "orcamento", "contrato", "projeto", "servico",
        "entrega", "prazo", "pagamento", "fatura"
    ],
    "parceiro": [
        "parceria", "partnership", "colaboracao", "acordo",
        "joint venture", "integracao"
    ],
    "mentor": [
        "conselho", "orientacao", "mentoria", "feedback",
        "experiencia", "aprendizado"
    ],
    "networking": [
        "evento", "conferencia", "meetup", "apresentacao",
        "introducao", "conhecer", "conectar"
    ]
}


def normalize_text(text: str) -> str:
    """Normaliza texto para comparacao."""
    if not text:
        return ""
    return text.lower().strip()


def parse_tags(tags) -> List[str]:
    """Parse tags de diferentes formatos."""
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
        return [t.lower().strip() for t in tags.split(',') if t.strip()]
    return []


def check_keywords(text: str, keywords: List[str]) -> bool:
    """Verifica se texto contem alguma keyword."""
    text_lower = normalize_text(text)
    return any(kw in text_lower for kw in keywords)


def analisar_empresa(empresa: str) -> Set[str]:
    """Analisa nome da empresa e retorna tags sugeridas."""
    tags = set()
    if not empresa:
        return tags

    empresa_lower = normalize_text(empresa)

    for tag, keywords in EMPRESA_TAGS.items():
        if check_keywords(empresa_lower, keywords):
            tags.add(tag)

    return tags


def analisar_cargo(cargo: str) -> Set[str]:
    """Analisa cargo e retorna tags de nivel hierarquico."""
    tags = set()
    if not cargo:
        return tags

    cargo_lower = normalize_text(cargo)

    for tag, keywords in CARGO_TAGS.items():
        if check_keywords(cargo_lower, keywords):
            tags.add(tag)

    return tags


def analisar_email_domain(emails) -> Set[str]:
    """Analisa dominio do email e retorna tags."""
    tags = set()
    if not emails:
        return tags

    # Parse emails
    email_list = []
    if isinstance(emails, str):
        try:
            email_list = json.loads(emails)
        except:
            email_list = [{"email": emails}]
    elif isinstance(emails, list):
        email_list = emails

    for email_obj in email_list:
        email = email_obj.get("email", "") if isinstance(email_obj, dict) else str(email_obj)
        email_lower = normalize_text(email)

        for tag, domains in EMAIL_DOMAIN_TAGS.items():
            if any(domain in email_lower for domain in domains):
                tags.add(tag)

    return tags


def analisar_mensagens(contact_id: int) -> Set[str]:
    """Analisa historico de mensagens e retorna tags."""
    tags = set()

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar mensagens do contato
            cursor.execute("""
                SELECT conteudo
                FROM messages
                WHERE contact_id = %s
                ORDER BY enviado_em DESC
                LIMIT 50
            """, (contact_id,))

            messages = cursor.fetchall()

            # Concatenar conteudo
            all_content = " ".join([
                m.get("conteudo", "") or ""
                for m in messages
                if m.get("conteudo")
            ])

            if not all_content:
                return tags

            content_lower = normalize_text(all_content)

            for tag, keywords in MESSAGE_KEYWORDS.items():
                if check_keywords(content_lower, keywords):
                    tags.add(tag)
    except Exception as e:
        # Tabela messages pode nao existir
        logger.warning(f"Erro ao analisar mensagens: {e}")

    return tags


def analisar_contato_para_tags(contact_id: int) -> Dict:
    """
    Analisa um contato e sugere tags automaticas.

    Args:
        contact_id: ID do contato

    Returns:
        Dict com:
        - contact_id
        - nome
        - tags_atuais: tags existentes
        - tags_sugeridas: novas tags detectadas
        - tags_novas: tags sugeridas que nao existem ainda
        - detalhes: breakdown por fonte
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, nome, empresa, cargo, emails, tags
            FROM contacts
            WHERE id = %s
        """, (contact_id,))

        row = cursor.fetchone()
        if not row:
            return {"error": "Contato nao encontrado", "contact_id": contact_id}

        contact = dict(row)

    # Tags atuais
    tags_atuais = set(parse_tags(contact.get("tags")))

    # Analisar cada fonte
    tags_empresa = analisar_empresa(contact.get("empresa"))
    tags_cargo = analisar_cargo(contact.get("cargo"))
    tags_email = analisar_email_domain(contact.get("emails"))
    tags_mensagens = analisar_mensagens(contact_id)

    # Consolidar
    tags_sugeridas = tags_empresa | tags_cargo | tags_email | tags_mensagens
    tags_novas = tags_sugeridas - tags_atuais

    return {
        "contact_id": contact_id,
        "nome": contact.get("nome"),
        "tags_atuais": list(tags_atuais),
        "tags_sugeridas": list(tags_sugeridas),
        "tags_novas": list(tags_novas),
        "detalhes": {
            "empresa": list(tags_empresa),
            "cargo": list(tags_cargo),
            "email_domain": list(tags_email),
            "mensagens": list(tags_mensagens)
        }
    }


def aplicar_tags_contato(contact_id: int, tags_novas: List[str]) -> Dict:
    """
    Aplica novas tags a um contato.

    Args:
        contact_id: ID do contato
        tags_novas: Lista de novas tags a adicionar

    Returns:
        Dict com resultado
    """
    if not tags_novas:
        return {"contact_id": contact_id, "tags_adicionadas": 0}

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar tags atuais
        cursor.execute("SELECT tags FROM contacts WHERE id = %s", (contact_id,))
        row = cursor.fetchone()
        if not row:
            return {"error": "Contato nao encontrado"}

        tags_atuais = set(parse_tags(row.get("tags")))

        # Adicionar novas
        tags_final = list(tags_atuais | set(tags_novas))

        # Atualizar
        cursor.execute("""
            UPDATE contacts
            SET tags = %s
            WHERE id = %s
        """, (json.dumps(tags_final), contact_id))

        return {
            "contact_id": contact_id,
            "tags_adicionadas": len(set(tags_novas) - tags_atuais),
            "tags_total": len(tags_final)
        }


def aplicar_tags_em_lote(
    batch_size: int = 100,
    offset: int = 0,
    auto_apply: bool = False
) -> Dict:
    """
    Analisa e opcionalmente aplica tags em lote.

    Args:
        batch_size: Contatos por lote
        offset: Offset para paginacao
        auto_apply: Se True, aplica as tags automaticamente

    Returns:
        Dict com progresso e estatisticas
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Total de contatos
        cursor.execute("SELECT COUNT(*) as count FROM contacts")
        total = cursor.fetchone()["count"]

        # Buscar lote
        cursor.execute("""
            SELECT id, nome, empresa, cargo, emails, tags
            FROM contacts
            ORDER BY id
            LIMIT %s OFFSET %s
        """, (batch_size, offset))

        contacts = cursor.fetchall()

    stats = {
        "processados": 0,
        "com_sugestoes": 0,
        "total_tags_sugeridas": 0,
        "tags_aplicadas": 0 if auto_apply else None,
        "total": total,
        "offset_atual": offset,
        "proximo_offset": offset + len(contacts),
        "concluido": (offset + len(contacts)) >= total,
        "progresso_percent": round((offset + len(contacts)) / total * 100, 1) if total > 0 else 100,
        "contatos_com_sugestoes": []
    }

    for row in contacts:
        contact = dict(row)
        contact_id = contact["id"]

        # Analisar
        resultado = analisar_contato_para_tags(contact_id)
        stats["processados"] += 1

        if resultado.get("tags_novas"):
            stats["com_sugestoes"] += 1
            stats["total_tags_sugeridas"] += len(resultado["tags_novas"])

            # Guardar para retorno (limita a 20 para nao poluir)
            if len(stats["contatos_com_sugestoes"]) < 20:
                stats["contatos_com_sugestoes"].append({
                    "contact_id": contact_id,
                    "nome": contact.get("nome"),
                    "tags_novas": resultado["tags_novas"]
                })

            # Aplicar se auto_apply
            if auto_apply:
                aplicar_tags_contato(contact_id, resultado["tags_novas"])
                stats["tags_aplicadas"] += len(resultado["tags_novas"])

    logger.info(
        f"Auto-tags batch: {stats['processados']} processados, "
        f"{stats['com_sugestoes']} com sugestoes, "
        f"progresso: {stats['progresso_percent']}%"
    )

    return stats


def get_tag_statistics() -> Dict:
    """
    Retorna estatisticas de uso das tags no sistema.

    Returns:
        Dict com contagem por tag
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT tags FROM contacts WHERE tags IS NOT NULL")
        rows = cursor.fetchall()

    tag_counts = {}
    for row in rows:
        tags = parse_tags(row.get("tags"))
        for tag in tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Ordenar por frequencia
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)

    return {
        "total_contatos_com_tags": len(rows),
        "total_tags_unicas": len(tag_counts),
        "por_tag": dict(sorted_tags[:50])  # Top 50
    }
