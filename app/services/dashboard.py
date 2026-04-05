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
# get_contacts_needing_briefing removido - usamos COUNT direto para performance


def get_dashboard_stats() -> Dict:
    """
    Retorna estatisticas gerais do sistema para o dashboard.
    OTIMIZADO: Query unica com multiplos COUNTs.

    Returns:
        Dict com contagens e metricas principais
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # OTIMIZACAO: Todas as stats de contacts em uma unica query
        cursor.execute("""
            SELECT
                COUNT(*) as total_contatos,
                COUNT(*) FILTER (WHERE COALESCE(circulo, 5) <= 4) as circulos_ativos,
                COUNT(*) FILTER (WHERE COALESCE(circulo, 5) <= 3 AND COALESCE(health_score, 50) < 50) as precisam_atencao,
                COUNT(*) FILTER (WHERE COALESCE(circulo, 5) <= 3 AND COALESCE(health_score, 50) < 50) as briefings_pendentes,
                COUNT(*) FILTER (
                    WHERE aniversario IS NOT NULL
                    AND COALESCE(circulo, 5) <= 4
                    AND (
                        (EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
                         AND EXTRACT(DAY FROM aniversario) BETWEEN EXTRACT(DAY FROM CURRENT_DATE) AND EXTRACT(DAY FROM CURRENT_DATE) + 7)
                        OR
                        (EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE + INTERVAL '7 days')
                         AND EXTRACT(DAY FROM aniversario) <= EXTRACT(DAY FROM CURRENT_DATE + INTERVAL '7 days'))
                    )
                ) as aniversarios_proximos
            FROM contacts
        """)
        stats = cursor.fetchone()

        # Conversas ativas - query separada por ser JOIN
        conversas_ativas = 0
        try:
            cursor.execute("""
                SELECT COUNT(DISTINCT c.id) as count
                FROM conversations c
                JOIN messages m ON m.conversation_id = c.id
                WHERE m.enviado_em > NOW() - INTERVAL '7 days'
            """)
            result = cursor.fetchone()
            conversas_ativas = result["count"] if result else 0
        except:
            pass

        # Reunioes e tarefas - podem nao existir
        reunioes_hoje = 0
        tarefas_pendentes = 0
        try:
            cursor.execute("SELECT COUNT(*) as count FROM meetings WHERE DATE(data_reuniao) = CURRENT_DATE")
            reunioes_hoje = cursor.fetchone()["count"] or 0
        except:
            pass
        try:
            cursor.execute("SELECT COUNT(*) as count FROM tasks WHERE status = 'pending'")
            tarefas_pendentes = cursor.fetchone()["count"] or 0
        except:
            pass

        return {
            "total_contatos": stats["total_contatos"],
            "circulos_ativos": stats["circulos_ativos"],
            "precisam_atencao": stats["precisam_atencao"] + stats["aniversarios_proximos"],
            "briefings_pendentes": stats["briefings_pendentes"],
            "conversas_ativas": conversas_ativas,
            "reunioes_hoje": reunioes_hoje,
            "tarefas_pendentes": tarefas_pendentes
        }


def get_circulos_resumo() -> Dict:
    """
    Retorna resumo dos circulos para o dashboard.
    Versao otimizada com query unica.

    Returns:
        Dict com total e health medio por circulo
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Query unica otimizada
        cursor.execute("""
            SELECT
                COALESCE(circulo, 5) as circulo,
                COUNT(*) as total,
                ROUND(AVG(COALESCE(health_score, 50))::numeric, 1) as health_medio
            FROM contacts
            GROUP BY COALESCE(circulo, 5)
            ORDER BY circulo
        """)

        resumo = {}
        for row in cursor.fetchall():
            c = row["circulo"]
            resumo[str(c)] = {
                "total": row["total"],
                "health_medio": float(row["health_medio"] or 50)
            }

        # Preencher circulos vazios
        for c in range(1, 6):
            if str(c) not in resumo:
                resumo[str(c)] = {"total": 0, "health_medio": 50.0}

        return resumo


def get_alertas(limit: int = 10) -> List[Dict]:
    """
    Retorna alertas priorizados para o dashboard.
    Versao otimizada com queries diretas no banco.

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

        # 1. Aniversarios proximos (3 dias) - calculado no banco
        cursor.execute("""
            WITH aniv_calc AS (
                SELECT
                    id, nome, empresa, circulo, aniversario, foto_url,
                    CASE
                        WHEN EXTRACT(DOY FROM aniversario::date) >= EXTRACT(DOY FROM CURRENT_DATE)
                        THEN EXTRACT(DOY FROM aniversario::date) - EXTRACT(DOY FROM CURRENT_DATE)
                        ELSE 365 + EXTRACT(DOY FROM aniversario::date) - EXTRACT(DOY FROM CURRENT_DATE)
                    END as dias_ate
                FROM contacts
                WHERE aniversario IS NOT NULL
                  AND COALESCE(circulo, 5) <= 4
            )
            SELECT * FROM aniv_calc
            WHERE dias_ate <= 3
            ORDER BY dias_ate
            LIMIT %s
        """, (limit,))

        for row in cursor.fetchall():
            dias_ate = int(row['dias_ate'])
            alertas.append({
                "tipo": "aniversario",
                "contato_id": row['id'],
                "nome": row['nome'],
                "empresa": row.get('empresa'),
                "foto_url": row.get('foto_url'),
                "mensagem": f"Aniversario {'HOJE!' if dias_ate == 0 else f'em {dias_ate} dia(s)'}",
                "prioridade": "alta",
                "dias": dias_ate
            })
            seen_ids.add(row['id'])

        # 2. Health critico (< 30) em circulos 1-3 - ALTA prioridade
        if len(alertas) < limit:
            cursor.execute("""
                SELECT id, nome, empresa, circulo, health_score, ultimo_contato, foto_url,
                       EXTRACT(DAY FROM NOW() - ultimo_contato)::int as dias_sem_contato
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                  AND COALESCE(health_score, 50) < 30
                ORDER BY circulo ASC, health_score ASC
                LIMIT %s
            """, (limit - len(alertas),))

            for row in cursor.fetchall():
                if row['id'] in seen_ids:
                    continue

                dias = row.get('dias_sem_contato')
                dias_str = f", sem contato ha {dias} dias" if dias else ""

                alertas.append({
                    "tipo": "health_critico",
                    "contato_id": row['id'],
                    "nome": row['nome'],
                    "empresa": row.get('empresa'),
                    "foto_url": row.get('foto_url'),
                    "mensagem": f"Circulo {row['circulo']}, health {row['health_score']}%{dias_str}",
                    "prioridade": "alta",
                    "health_score": row['health_score'],
                    "circulo": row['circulo']
                })
                seen_ids.add(row['id'])

        # 3. Health baixo (< 50) em circulos 1-3 - MEDIA prioridade
        if len(alertas) < limit:
            cursor.execute("""
                SELECT id, nome, empresa, circulo, health_score, ultimo_contato, foto_url,
                       EXTRACT(DAY FROM NOW() - ultimo_contato)::int as dias_sem_contato
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                  AND COALESCE(health_score, 50) >= 30
                  AND COALESCE(health_score, 50) < 50
                ORDER BY circulo ASC, health_score ASC
                LIMIT %s
            """, (limit - len(alertas),))

            for row in cursor.fetchall():
                if row['id'] in seen_ids:
                    continue

                dias = row.get('dias_sem_contato')
                dias_str = f", sem contato ha {dias} dias" if dias else ""

                alertas.append({
                    "tipo": "health_baixo",
                    "contato_id": row['id'],
                    "nome": row['nome'],
                    "empresa": row.get('empresa'),
                    "foto_url": row.get('foto_url'),
                    "mensagem": f"Circulo {row['circulo']}, health {row['health_score']}%{dias_str}",
                    "prioridade": "media",
                    "health_score": row['health_score'],
                    "circulo": row['circulo']
                })
                seen_ids.add(row['id'])

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
    Retorna todos os dados do dashboard em UMA UNICA conexao.
    SUPER OTIMIZADO: Uma conexao, queries paralelas com CTEs.

    Returns:
        Dict completo com stats, circulos, alertas e contatos recentes
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Query mega-otimizada: tudo em uma unica chamada
        cursor.execute("""
            WITH
            -- Stats gerais
            stats AS (
                SELECT
                    COUNT(*) as total_contatos,
                    COUNT(*) FILTER (WHERE COALESCE(circulo, 5) <= 4) as circulos_ativos,
                    COUNT(*) FILTER (WHERE COALESCE(circulo, 5) <= 3 AND COALESCE(health_score, 50) < 50) as precisam_atencao
                FROM contacts
            ),
            -- Circulos resumo
            circulos AS (
                SELECT
                    COALESCE(circulo, 5) as circulo,
                    COUNT(*) as total,
                    ROUND(AVG(COALESCE(health_score, 50))::numeric, 1) as health_medio
                FROM contacts
                GROUP BY COALESCE(circulo, 5)
            ),
            -- Aniversarios proximos (alertas)
            aniversarios AS (
                SELECT id, nome, empresa, foto_url, circulo,
                    CASE
                        WHEN EXTRACT(DOY FROM aniversario::date) >= EXTRACT(DOY FROM CURRENT_DATE)
                        THEN EXTRACT(DOY FROM aniversario::date) - EXTRACT(DOY FROM CURRENT_DATE)
                        ELSE 365 + EXTRACT(DOY FROM aniversario::date) - EXTRACT(DOY FROM CURRENT_DATE)
                    END as dias_ate
                FROM contacts
                WHERE aniversario IS NOT NULL AND COALESCE(circulo, 5) <= 4
            ),
            aniv_alertas AS (
                SELECT * FROM aniversarios WHERE dias_ate <= 3 ORDER BY dias_ate LIMIT 5
            ),
            -- Health critico (alertas)
            health_alertas AS (
                SELECT id, nome, empresa, foto_url, circulo, health_score
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3 AND COALESCE(health_score, 50) < 30
                ORDER BY circulo, health_score
                LIMIT 5
            ),
            -- Contatos recentes
            recentes AS (
                SELECT id, nome, apelido, empresa, cargo, circulo, health_score, ultimo_contato, foto_url, linkedin
                FROM contacts
                WHERE ultimo_contato IS NOT NULL
                ORDER BY ultimo_contato DESC
                LIMIT 5
            )
            SELECT
                (SELECT row_to_json(stats) FROM stats) as stats,
                (SELECT COALESCE(json_agg(row_to_json(circulos)), '[]') FROM circulos) as circulos,
                (SELECT COALESCE(json_agg(row_to_json(aniv_alertas)), '[]') FROM aniv_alertas) as aniv_alertas,
                (SELECT COALESCE(json_agg(row_to_json(health_alertas)), '[]') FROM health_alertas) as health_alertas,
                (SELECT COALESCE(json_agg(row_to_json(recentes)), '[]') FROM recentes) as recentes
        """)

        row = cursor.fetchone()

        # Processar stats
        stats = row['stats'] or {"total_contatos": 0, "circulos_ativos": 0, "precisam_atencao": 0}

        # Processar circulos
        circulos_list = row['circulos'] or []
        circulos_resumo = {str(c['circulo']): {"total": c['total'], "health_medio": float(c['health_medio'] or 50)} for c in circulos_list}
        for i in range(1, 6):
            if str(i) not in circulos_resumo:
                circulos_resumo[str(i)] = {"total": 0, "health_medio": 50.0}

        # Processar alertas
        alertas = []
        for a in (row['aniv_alertas'] or []):
            dias = int(a['dias_ate'])
            msg = "Aniversario HOJE!" if dias == 0 else f"Aniversario em {dias} dia(s)"
            alertas.append({
                "tipo": "aniversario",
                "contato_id": a['id'],
                "nome": a['nome'],
                "empresa": a.get('empresa'),
                "foto_url": a.get('foto_url'),
                "mensagem": msg,
                "prioridade": "alta",
                "dias": dias
            })
        for h in (row['health_alertas'] or []):
            alertas.append({
                "tipo": "health_critico",
                "contato_id": h['id'],
                "nome": h['nome'],
                "empresa": h.get('empresa'),
                "foto_url": h.get('foto_url'),
                "mensagem": f"Circulo {h['circulo']}, health {h['health_score']}%",
                "prioridade": "alta",
                "health_score": h['health_score'],
                "circulo": h['circulo']
            })

        # Processar contatos recentes
        contatos_recentes = []
        for c in (row['recentes'] or []):
            dias = 0
            if c.get('ultimo_contato'):
                try:
                    uc = c['ultimo_contato']
                    if isinstance(uc, str):
                        uc = datetime.fromisoformat(uc.replace('Z', '+00:00'))
                    dias = (datetime.now() - uc.replace(tzinfo=None)).days
                except:
                    pass
            contatos_recentes.append({
                **c,
                'dias_sem_contato': dias,
                'circulo_nome': CIRCULO_CONFIG.get(c.get('circulo') or 5, {}).get('nome', 'Arquivo')
            })

        return {
            "stats": stats,
            "circulos_resumo": circulos_resumo,
            "alertas": alertas[:10],
            "contatos_recentes": contatos_recentes,
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
