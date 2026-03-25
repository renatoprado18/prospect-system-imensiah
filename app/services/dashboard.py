"""
Servico de Dashboard Unificado

Agrega dados de Circulos, Briefings e outras metricas para o Dashboard principal.
Endpoint principal: GET /api/v1/dashboard
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import json

from database import get_db
from services.circulos import (
    CIRCULO_CONFIG,
    get_dashboard_circulos,
    get_contatos_precisando_atencao,
    get_aniversarios_proximos,
    calcular_dias_sem_contato
)
from services.briefings import get_contacts_needing_briefing


def get_dashboard_stats() -> Dict:
    """
    Retorna estatisticas gerais do sistema para o dashboard.

    Returns:
        Dict com contagens e metricas principais
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Total de contatos
        cursor.execute("SELECT COUNT(*) as count FROM contacts")
        total_contatos = cursor.fetchone()["count"]

        # Circulos ativos (1-4, excluindo arquivo)
        cursor.execute("""
            SELECT COUNT(*) as count FROM contacts
            WHERE COALESCE(circulo, 5) <= 4
        """)
        circulos_ativos = cursor.fetchone()["count"]

        # Precisam atencao (circulo 1-3 com health < 50 OU aniversario 7 dias)
        cursor.execute("""
            SELECT COUNT(*) as count FROM contacts
            WHERE (COALESCE(circulo, 5) <= 3 AND COALESCE(health_score, 50) < 50)
        """)
        precisam_atencao_health = cursor.fetchone()["count"]

        # Aniversarios proximos (7 dias)
        aniversarios = get_aniversarios_proximos(7)
        precisam_atencao = precisam_atencao_health + len(aniversarios)

        # Briefings pendentes (contatos que precisam de briefing)
        briefings_pendentes = len(get_contacts_needing_briefing(20))

        # Conversas ativas (mensagens nos ultimos 7 dias)
        cursor.execute("""
            SELECT COUNT(DISTINCT c.id) as count
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
            WHERE m.enviado_em > NOW() - INTERVAL '7 days'
        """)
        result = cursor.fetchone()
        conversas_ativas = result["count"] if result else 0

        # Reunioes hoje (se tabela existir)
        reunioes_hoje = 0
        try:
            cursor.execute("""
                SELECT COUNT(*) as count FROM meetings
                WHERE DATE(data_reuniao) = CURRENT_DATE
            """)
            result = cursor.fetchone()
            reunioes_hoje = result["count"] if result else 0
        except:
            pass

        # Tarefas pendentes (se tabela existir)
        tarefas_pendentes = 0
        try:
            cursor.execute("""
                SELECT COUNT(*) as count FROM tasks
                WHERE status = 'pending'
            """)
            result = cursor.fetchone()
            tarefas_pendentes = result["count"] if result else 0
        except:
            pass

        return {
            "total_contatos": total_contatos,
            "circulos_ativos": circulos_ativos,
            "precisam_atencao": precisam_atencao,
            "briefings_pendentes": briefings_pendentes,
            "conversas_ativas": conversas_ativas,
            "reunioes_hoje": reunioes_hoje,
            "tarefas_pendentes": tarefas_pendentes
        }


def get_circulos_resumo() -> Dict:
    """
    Retorna resumo dos circulos para o dashboard.

    Returns:
        Dict com total e health medio por circulo
    """
    circulos_data = get_dashboard_circulos()
    por_circulo = circulos_data.get("por_circulo", {})

    # Simplificar para formato da API
    resumo = {}
    for c in range(1, 6):
        data = por_circulo.get(c, {"total": 0, "health_medio": 50})
        resumo[str(c)] = {
            "total": data["total"],
            "health_medio": round(data.get("health_medio", 50), 1)
        }

    return resumo


def get_alertas(limit: int = 10) -> List[Dict]:
    """
    Retorna alertas priorizados para o dashboard.

    Prioridade:
    1. Aniversarios proximos (3 dias) - prioridade alta
    2. Health score critico (< 30) em circulos 1-3 - prioridade alta
    3. Contatos sem interacao alem do esperado - prioridade media

    Args:
        limit: Numero maximo de alertas

    Returns:
        Lista de alertas ordenados por prioridade
    """
    alertas = []
    seen_ids = set()

    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Aniversarios proximos (3 dias) - ALTA prioridade
        hoje = datetime.now().date()
        cursor.execute("""
            SELECT id, nome, empresa, circulo, aniversario, foto_url
            FROM contacts
            WHERE aniversario IS NOT NULL
              AND COALESCE(circulo, 5) <= 4
        """)

        for row in cursor.fetchall():
            contact = dict(row)
            aniv = contact['aniversario']
            try:
                if isinstance(aniv, str):
                    aniv = datetime.fromisoformat(aniv).date()
                elif hasattr(aniv, 'date') and callable(getattr(aniv, 'date')):
                    aniv = aniv.date()

                aniv_este_ano = aniv.replace(year=hoje.year)
                if aniv_este_ano < hoje:
                    aniv_este_ano = aniv.replace(year=hoje.year + 1)

                dias_ate = (aniv_este_ano - hoje).days

                if 0 <= dias_ate <= 3:
                    alertas.append({
                        "tipo": "aniversario",
                        "contato_id": contact['id'],
                        "nome": contact['nome'],
                        "empresa": contact.get('empresa'),
                        "foto_url": contact.get('foto_url'),
                        "mensagem": f"Aniversario {'HOJE!' if dias_ate == 0 else f'em {dias_ate} dia(s)'}",
                        "prioridade": "alta",
                        "dias": dias_ate
                    })
                    seen_ids.add(contact['id'])
            except:
                continue

        # 2. Health critico (< 30) em circulos 1-3 - ALTA prioridade
        cursor.execute("""
            SELECT id, nome, empresa, circulo, health_score, ultimo_contato, foto_url
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 3
              AND COALESCE(health_score, 50) < 30
            ORDER BY circulo ASC, health_score ASC
            LIMIT %s
        """, (limit,))

        for row in cursor.fetchall():
            contact = dict(row)
            if contact['id'] in seen_ids:
                continue

            dias = calcular_dias_sem_contato(contact.get('ultimo_contato'))
            dias_str = f", sem contato ha {dias} dias" if dias else ""

            alertas.append({
                "tipo": "health_critico",
                "contato_id": contact['id'],
                "nome": contact['nome'],
                "empresa": contact.get('empresa'),
                "foto_url": contact.get('foto_url'),
                "mensagem": f"Circulo {contact['circulo']}, health {contact['health_score']}%{dias_str}",
                "prioridade": "alta",
                "health_score": contact['health_score'],
                "circulo": contact['circulo']
            })
            seen_ids.add(contact['id'])

        # 3. Health baixo (< 50) em circulos 1-3 - MEDIA prioridade
        cursor.execute("""
            SELECT id, nome, empresa, circulo, health_score, ultimo_contato, foto_url
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 3
              AND COALESCE(health_score, 50) >= 30
              AND COALESCE(health_score, 50) < 50
            ORDER BY circulo ASC, health_score ASC
            LIMIT %s
        """, (limit,))

        for row in cursor.fetchall():
            contact = dict(row)
            if contact['id'] in seen_ids:
                continue

            dias = calcular_dias_sem_contato(contact.get('ultimo_contato'))
            dias_str = f", sem contato ha {dias} dias" if dias else ""

            alertas.append({
                "tipo": "health_baixo",
                "contato_id": contact['id'],
                "nome": contact['nome'],
                "empresa": contact.get('empresa'),
                "foto_url": contact.get('foto_url'),
                "mensagem": f"Circulo {contact['circulo']}, health {contact['health_score']}%{dias_str}",
                "prioridade": "media",
                "health_score": contact['health_score'],
                "circulo": contact['circulo']
            })
            seen_ids.add(contact['id'])

        # 4. Aniversarios proximos (7 dias) - MEDIA prioridade
        for row in cursor.execute("""
            SELECT id, nome, empresa, circulo, aniversario, foto_url
            FROM contacts
            WHERE aniversario IS NOT NULL
              AND COALESCE(circulo, 5) <= 4
        """).fetchall() if hasattr(cursor, 'execute') else []:
            pass  # Ja processado acima com prioridade alta para <= 3 dias

    # Ordenar: alta primeiro, depois media
    prioridade_ordem = {"alta": 0, "media": 1, "baixa": 2}
    alertas.sort(key=lambda x: (prioridade_ordem.get(x["prioridade"], 2), x.get("dias", 999)))

    return alertas[:limit]


def get_contatos_recentes(limit: int = 5) -> List[Dict]:
    """
    Retorna os ultimos contatos interagidos.

    Args:
        limit: Numero maximo de contatos

    Returns:
        Lista de contatos ordenados por ultimo_contato desc
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, nome, apelido, empresa, cargo, circulo, health_score,
                   ultimo_contato, foto_url, linkedin
            FROM contacts
            WHERE ultimo_contato IS NOT NULL
            ORDER BY ultimo_contato DESC
            LIMIT %s
        """, (limit,))

        contatos = []
        for row in cursor.fetchall():
            contact = dict(row)
            dias = calcular_dias_sem_contato(contact.get('ultimo_contato'))
            contact['dias_sem_contato'] = dias
            contact['circulo_nome'] = CIRCULO_CONFIG.get(contact.get('circulo') or 5, {}).get('nome', 'Arquivo')
            contatos.append(contact)

        return contatos


def get_full_dashboard() -> Dict:
    """
    Retorna todos os dados do dashboard em uma unica chamada.

    Returns:
        Dict completo com stats, circulos, alertas e contatos recentes
    """
    return {
        "stats": get_dashboard_stats(),
        "circulos_resumo": get_circulos_resumo(),
        "alertas": get_alertas(10),
        "contatos_recentes": get_contatos_recentes(5),
        "gerado_em": datetime.now().isoformat()
    }


def get_dashboard_health_trend(dias: int = 30) -> Dict:
    """
    Retorna tendencia de health scores nos ultimos N dias.
    Util para graficos de evolucao.

    Args:
        dias: Periodo para analisar

    Returns:
        Dict com dados de tendencia
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Health medio por circulo atual
        cursor.execute("""
            SELECT
                COALESCE(circulo, 5) as circulo,
                AVG(COALESCE(health_score, 50)) as health_medio,
                COUNT(*) as total
            FROM contacts
            GROUP BY COALESCE(circulo, 5)
            ORDER BY circulo
        """)

        atual = {}
        for row in cursor.fetchall():
            atual[row["circulo"]] = {
                "health_medio": round(row["health_medio"], 1),
                "total": row["total"]
            }

        # Contatos que melhoraram/pioraram recentemente (baseado em ultimo_contato)
        cursor.execute("""
            SELECT
                COUNT(*) FILTER (WHERE ultimo_contato > NOW() - INTERVAL '%s days') as contatados_periodo,
                COUNT(*) FILTER (WHERE ultimo_contato <= NOW() - INTERVAL '%s days' OR ultimo_contato IS NULL) as nao_contatados
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 4
        """, (dias, dias))

        result = cursor.fetchone()

        return {
            "periodo_dias": dias,
            "por_circulo": atual,
            "contatados_periodo": result["contatados_periodo"] if result else 0,
            "nao_contatados": result["nao_contatados"] if result else 0,
            "gerado_em": datetime.now().isoformat()
        }


def get_quick_stats() -> Dict:
    """
    Retorna estatisticas rapidas para widgets.
    Versao leve do dashboard para carregamento rapido.

    Returns:
        Dict com metricas essenciais
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Query unica para performance
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE COALESCE(circulo, 5) <= 4) as ativos,
                COUNT(*) FILTER (WHERE COALESCE(circulo, 5) <= 3 AND COALESCE(health_score, 50) < 50) as precisam_atencao,
                AVG(COALESCE(health_score, 50)) FILTER (WHERE COALESCE(circulo, 5) <= 4) as health_medio_ativos
            FROM contacts
        """)

        result = cursor.fetchone()

        return {
            "total_contatos": result["total"],
            "circulos_ativos": result["ativos"],
            "precisam_atencao": result["precisam_atencao"],
            "health_medio": round(result["health_medio_ativos"] or 50, 1),
            "gerado_em": datetime.now().isoformat()
        }
