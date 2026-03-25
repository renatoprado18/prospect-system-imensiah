"""
Servico de Contexto Enriquecido para Briefings

Melhora os briefings com analise automatica de:
- Tom das ultimas mensagens (positivo, neutro, negativo)
- Topicos recorrentes nas conversas
- Assuntos sugeridos para retomar
- Promessas/compromissos pendentes

Autor: INTEL
Data: 2026-03-25
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
import json
import re
import logging
from collections import Counter

from database import get_db

logger = logging.getLogger(__name__)


# ============== CONFIGURACAO ==============

# Palavras-chave para deteccao de tom
TOM_KEYWORDS = {
    "positivo": [
        "obrigado", "thanks", "otimo", "great", "excelente", "excellent",
        "perfeito", "perfect", "maravilhoso", "wonderful", "incrivel",
        "parabens", "congratulations", "adorei", "love", "gostei",
        "sucesso", "success", "feliz", "happy", "animado", "excited",
        "top", "show", "demais", "sensacional", "fantastico"
    ],
    "negativo": [
        "problema", "issue", "erro", "error", "bug", "falha",
        "atraso", "delay", "infelizmente", "unfortunately", "desculpa",
        "sorry", "preocupado", "worried", "frustrado", "frustrated",
        "decepcionado", "disappointed", "cancelar", "cancel", "urgente",
        "urgent", "critico", "critical", "grave", "serio"
    ],
    "profissional": [
        "reuniao", "meeting", "projeto", "project", "proposta", "proposal",
        "contrato", "contract", "deadline", "prazo", "entrega", "delivery",
        "equipe", "team", "budget", "orcamento", "report", "relatorio"
    ]
}

# Palavras-chave para identificar topicos
TOPICO_KEYWORDS = {
    "negocios": [
        "negocio", "business", "empresa", "company", "cliente", "client",
        "venda", "sale", "compra", "purchase", "investimento", "investment",
        "parceria", "partnership", "contrato", "contract"
    ],
    "projetos": [
        "projeto", "project", "tarefa", "task", "sprint", "milestone",
        "entrega", "delivery", "desenvolvimento", "development", "implementacao"
    ],
    "reunioes": [
        "reuniao", "meeting", "call", "ligacao", "agenda", "pauta",
        "discussao", "discussion", "apresentacao", "presentation"
    ],
    "financeiro": [
        "pagamento", "payment", "fatura", "invoice", "orcamento", "budget",
        "custo", "cost", "preco", "price", "valor", "investir"
    ],
    "pessoal": [
        "familia", "family", "ferias", "vacation", "saude", "health",
        "aniversario", "birthday", "viagem", "trip", "hobby"
    ],
    "tecnologia": [
        "sistema", "system", "software", "app", "integracao", "integration",
        "api", "banco de dados", "database", "cloud", "automacao"
    ],
    "estrategia": [
        "estrategia", "strategy", "planejamento", "planning", "objetivo",
        "goal", "meta", "visao", "missao", "roadmap"
    ]
}

# Padroes para identificar promessas/compromissos
PROMESSA_PATTERNS = [
    r"(?:vou|irei|prometo|comprometo)\s+\w+",
    r"(?:mandarei|enviarei|farei|prepararei)\s+\w+",
    r"(?:te\s+(?:ligo|mando|envio|retorno))",
    r"(?:fica\s+combinado|combinamos|acertamos)",
    r"(?:segunda|terca|quarta|quinta|sexta|semana\s+que\s+vem)",
    r"(?:proximo\s+(?:mes|semana|dia))",
    r"(?:ate\s+(?:amanha|sexta|segunda))"
]


def normalize_text(text: str) -> str:
    """Normaliza texto para analise."""
    if not text:
        return ""
    return text.lower().strip()


def detectar_tom_mensagem(texto: str) -> Dict:
    """
    Detecta o tom de uma mensagem individual.

    Returns:
        Dict com scores para cada tipo de tom
    """
    texto_lower = normalize_text(texto)
    scores = {
        "positivo": 0,
        "negativo": 0,
        "profissional": 0
    }

    for tom, keywords in TOM_KEYWORDS.items():
        for keyword in keywords:
            if keyword in texto_lower:
                scores[tom] += 1

    return scores


def analisar_tom_conversas(contact_id: int, dias: int = 30) -> Dict:
    """
    Analisa o tom das ultimas conversas com um contato.

    Args:
        contact_id: ID do contato
        dias: Quantos dias de historico analisar

    Returns:
        Dict com tom geral, scores e tendencia
    """
    with get_db() as conn:
        cursor = conn.cursor()

        data_inicio = datetime.now() - timedelta(days=dias)

        try:
            cursor.execute("""
                SELECT conteudo, direcao, enviado_em
                FROM messages
                WHERE contact_id = %s
                  AND enviado_em >= %s
                  AND conteudo IS NOT NULL
                ORDER BY enviado_em DESC
            """, (contact_id, data_inicio))

            messages = cursor.fetchall()
        except:
            messages = []

    if not messages:
        return {
            "tom_geral": "neutro",
            "confianca": 0,
            "scores": {"positivo": 0, "negativo": 0, "profissional": 0},
            "total_mensagens": 0,
            "tendencia": "estavel"
        }

    # Agregar scores
    total_scores = {"positivo": 0, "negativo": 0, "profissional": 0}

    for msg in messages:
        conteudo = msg.get("conteudo", "")
        if conteudo:
            msg_scores = detectar_tom_mensagem(conteudo)
            for tom, score in msg_scores.items():
                total_scores[tom] += score

    # Determinar tom geral
    total = sum(total_scores.values())
    if total == 0:
        tom_geral = "neutro"
        confianca = 0
    else:
        if total_scores["positivo"] > total_scores["negativo"] * 1.5:
            tom_geral = "positivo"
        elif total_scores["negativo"] > total_scores["positivo"] * 1.5:
            tom_geral = "negativo"
        elif total_scores["profissional"] > (total_scores["positivo"] + total_scores["negativo"]):
            tom_geral = "profissional"
        else:
            tom_geral = "neutro"

        confianca = min(100, int((total / len(messages)) * 20))

    # Analisar tendencia (primeira vs segunda metade)
    meio = len(messages) // 2
    if meio > 0:
        primeira_metade = messages[:meio]
        segunda_metade = messages[meio:]

        pos_primeira = sum(detectar_tom_mensagem(m.get("conteudo", ""))["positivo"] for m in primeira_metade)
        pos_segunda = sum(detectar_tom_mensagem(m.get("conteudo", ""))["positivo"] for m in segunda_metade)

        neg_primeira = sum(detectar_tom_mensagem(m.get("conteudo", ""))["negativo"] for m in primeira_metade)
        neg_segunda = sum(detectar_tom_mensagem(m.get("conteudo", ""))["negativo"] for m in segunda_metade)

        if (pos_segunda - neg_segunda) > (pos_primeira - neg_primeira):
            tendencia = "melhorando"
        elif (pos_segunda - neg_segunda) < (pos_primeira - neg_primeira):
            tendencia = "piorando"
        else:
            tendencia = "estavel"
    else:
        tendencia = "estavel"

    return {
        "tom_geral": tom_geral,
        "confianca": confianca,
        "scores": total_scores,
        "total_mensagens": len(messages),
        "periodo_dias": dias,
        "tendencia": tendencia
    }


def identificar_topicos_recorrentes(contact_id: int, dias: int = 90) -> Dict:
    """
    Identifica topicos recorrentes nas conversas.

    Returns:
        Dict com topicos principais e frequencia
    """
    with get_db() as conn:
        cursor = conn.cursor()

        data_inicio = datetime.now() - timedelta(days=dias)

        try:
            cursor.execute("""
                SELECT conteudo
                FROM messages
                WHERE contact_id = %s
                  AND enviado_em >= %s
                  AND conteudo IS NOT NULL
            """, (contact_id, data_inicio))

            messages = cursor.fetchall()
        except:
            messages = []

    if not messages:
        return {
            "topicos_principais": [],
            "total_mensagens": 0
        }

    # Concatenar todo conteudo
    all_content = " ".join([
        normalize_text(m.get("conteudo", ""))
        for m in messages
        if m.get("conteudo")
    ])

    # Contar ocorrencias de cada topico
    topico_counts = {}
    for topico, keywords in TOPICO_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in all_content)
        if count > 0:
            topico_counts[topico] = count

    # Ordenar por frequencia
    sorted_topicos = sorted(topico_counts.items(), key=lambda x: x[1], reverse=True)

    # Retornar top 5
    topicos_principais = [
        {"topico": topico, "frequencia": count}
        for topico, count in sorted_topicos[:5]
    ]

    return {
        "topicos_principais": topicos_principais,
        "total_mensagens": len(messages),
        "periodo_dias": dias
    }


def sugerir_assuntos_retomar(contact_id: int) -> Dict:
    """
    Sugere assuntos para retomar com o contato.

    Baseado em:
    - Topicos recorrentes que nao foram mencionados recentemente
    - Ultimas mensagens nao respondidas
    - Tasks pendentes
    """
    sugestoes = []

    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Verificar ultima mensagem (se foi incoming sem resposta)
        try:
            cursor.execute("""
                SELECT conteudo, direcao, enviado_em
                FROM messages
                WHERE contact_id = %s
                ORDER BY enviado_em DESC
                LIMIT 1
            """, (contact_id,))

            ultima = cursor.fetchone()
            if ultima and ultima.get("direcao") == "incoming":
                dias_sem_resposta = (datetime.now() - ultima["enviado_em"]).days
                if dias_sem_resposta >= 1:
                    sugestoes.append({
                        "tipo": "mensagem_pendente",
                        "prioridade": "alta",
                        "descricao": f"Mensagem recebida ha {dias_sem_resposta} dias sem resposta",
                        "contexto": ultima.get("conteudo", "")[:100] if ultima.get("conteudo") else ""
                    })
        except:
            pass

        # 2. Verificar tasks pendentes
        try:
            cursor.execute("""
                SELECT titulo, data_vencimento
                FROM tasks
                WHERE contact_id = %s AND status = 'pending'
                ORDER BY data_vencimento ASC
                LIMIT 3
            """, (contact_id,))

            tasks = cursor.fetchall()
            for task in tasks:
                prioridade = "alta" if task.get("data_vencimento") and task["data_vencimento"] < datetime.now() else "media"
                sugestoes.append({
                    "tipo": "task_pendente",
                    "prioridade": prioridade,
                    "descricao": f"Task pendente: {task.get('titulo', '')}"
                })
        except:
            pass

        # 3. Verificar aniversario proximo
        try:
            cursor.execute("""
                SELECT aniversario, nome
                FROM contacts
                WHERE id = %s AND aniversario IS NOT NULL
            """, (contact_id,))

            contact = cursor.fetchone()
            if contact and contact.get("aniversario"):
                aniv = contact["aniversario"]
                hoje = datetime.now().date()
                aniv_este_ano = aniv.replace(year=hoje.year)
                if aniv_este_ano < hoje:
                    aniv_este_ano = aniv.replace(year=hoje.year + 1)

                dias_ate = (aniv_este_ano - hoje).days
                if 0 <= dias_ate <= 14:
                    sugestoes.append({
                        "tipo": "aniversario",
                        "prioridade": "alta" if dias_ate <= 3 else "media",
                        "descricao": f"Aniversario em {dias_ate} dias ({aniv.strftime('%d/%m')})"
                    })
        except:
            pass

    # 4. Sugerir baseado em topicos recorrentes
    topicos = identificar_topicos_recorrentes(contact_id, dias=180)
    for t in topicos.get("topicos_principais", [])[:2]:
        sugestoes.append({
            "tipo": "topico_recorrente",
            "prioridade": "baixa",
            "descricao": f"Topico frequente: {t['topico']}"
        })

    # Ordenar por prioridade
    prioridade_order = {"alta": 0, "media": 1, "baixa": 2}
    sugestoes.sort(key=lambda x: prioridade_order.get(x.get("prioridade", "baixa"), 2))

    return {
        "sugestoes": sugestoes[:5],
        "total": len(sugestoes)
    }


def detectar_promessas_pendentes(contact_id: int, dias: int = 60) -> Dict:
    """
    Detecta possiveis promessas/compromissos nas mensagens.

    Returns:
        Dict com lista de promessas detectadas
    """
    with get_db() as conn:
        cursor = conn.cursor()

        data_inicio = datetime.now() - timedelta(days=dias)

        try:
            cursor.execute("""
                SELECT conteudo, direcao, enviado_em
                FROM messages
                WHERE contact_id = %s
                  AND enviado_em >= %s
                  AND conteudo IS NOT NULL
                ORDER BY enviado_em DESC
            """, (contact_id, data_inicio))

            messages = cursor.fetchall()
        except:
            messages = []

    promessas = []

    for msg in messages:
        conteudo = msg.get("conteudo", "")
        if not conteudo:
            continue

        conteudo_lower = normalize_text(conteudo)
        direcao = msg.get("direcao")

        for pattern in PROMESSA_PATTERNS:
            matches = re.findall(pattern, conteudo_lower)
            for match in matches:
                # Extrair contexto (30 chars antes e depois)
                idx = conteudo_lower.find(match)
                start = max(0, idx - 30)
                end = min(len(conteudo), idx + len(match) + 30)
                contexto = conteudo[start:end]

                promessas.append({
                    "texto": match,
                    "contexto": f"...{contexto}...",
                    "direcao": "voce" if direcao == "outgoing" else "contato",
                    "data": msg.get("enviado_em").strftime("%d/%m/%Y") if msg.get("enviado_em") else None
                })

    # Remover duplicatas
    seen = set()
    promessas_unicas = []
    for p in promessas:
        key = p["texto"] + p["direcao"]
        if key not in seen:
            seen.add(key)
            promessas_unicas.append(p)

    return {
        "promessas": promessas_unicas[:10],
        "total": len(promessas_unicas),
        "periodo_dias": dias
    }


def get_contexto_enriquecido(contact_id: int) -> Dict:
    """
    Retorna contexto completo enriquecido para briefings.

    Combina todas as analises em um unico resultado.
    """
    tom = analisar_tom_conversas(contact_id)
    topicos = identificar_topicos_recorrentes(contact_id)
    sugestoes = sugerir_assuntos_retomar(contact_id)
    promessas = detectar_promessas_pendentes(contact_id)

    # Gerar resumo
    alertas = []

    if tom["tom_geral"] == "negativo":
        alertas.append("Tom das conversas esta negativo - abordar com cuidado")
    if tom["tendencia"] == "piorando":
        alertas.append("Tendencia do relacionamento esta piorando")

    if sugestoes["sugestoes"]:
        alta_prioridade = [s for s in sugestoes["sugestoes"] if s.get("prioridade") == "alta"]
        if alta_prioridade:
            alertas.append(f"{len(alta_prioridade)} assunto(s) de alta prioridade para retomar")

    if promessas["promessas"]:
        minhas_promessas = [p for p in promessas["promessas"] if p["direcao"] == "voce"]
        if minhas_promessas:
            alertas.append(f"{len(minhas_promessas)} promessa(s) feita(s) por voce")

    return {
        "contact_id": contact_id,
        "tom_conversas": tom,
        "topicos_recorrentes": topicos,
        "assuntos_sugeridos": sugestoes,
        "promessas_pendentes": promessas,
        "alertas": alertas,
        "resumo": {
            "tom": tom["tom_geral"],
            "tendencia": tom["tendencia"],
            "topicos_principais": [t["topico"] for t in topicos.get("topicos_principais", [])[:3]],
            "total_alertas": len(alertas)
        }
    }
