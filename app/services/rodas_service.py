"""
Rodas de Relacionamento Service

Gerencia "rodas" - fios de contexto extraidos de mensagens que conectam
voce a um contato (promessas, favores, topicos, proximos passos).
"""
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from database import get_db


RODA_TYPES = [
    'promessa',         # Renato prometeu entregar algo ao contato
    'favor_recebido',   # Contato ajudou Renato (Renato e o BENEFICIARIO) - sugere retribuir
    'favor_feito',     # Renato ajudou o contato (Renato e o DOADOR) - marcador de boa vontade, NAO sugere acao
    'topico',           # Assunto discutido que pode ser retomado
    'proximo_passo',    # "semana que vem", "depois conversamos"
]

# Prioridade por tipo (menor = mais urgente)
# favor_feito nao entra na priorizacao do dashboard - e apenas marcador historico
RODA_PRIORITY = {
    'promessa': 1,
    'favor_recebido': 2,
    'proximo_passo': 3,
    'topico': 4,
    'favor_feito': 99,
}


class RodasService:
    """Service para gerenciar rodas de relacionamento"""

    def create_roda(
        self,
        contact_id: int,
        tipo: str,
        conteudo: str,
        message_id: Optional[int] = None,
        tags: Optional[List[str]] = None,
        prazo: Optional[str] = None,
        confidence: float = 0.5
    ) -> Optional[Dict]:
        """
        Cria uma nova roda de relacionamento.

        Args:
            contact_id: ID do contato
            tipo: promessa, favor_recebido, favor_feito, topico, proximo_passo
            conteudo: Descricao curta do contexto
            message_id: ID da mensagem de origem (opcional)
            tags: Lista de tags/palavras-chave
            prazo: Data limite (YYYY-MM-DD) se aplicavel
            confidence: Confianca da IA (0.0 a 1.0)

        Returns:
            Dict com a roda criada ou None se falhar
        """
        if tipo not in RODA_TYPES:
            return None

        with get_db() as conn:
            cursor = conn.cursor()

            # Verificar se ja existe roda similar pendente (evitar duplicatas)
            cursor.execute('''
                SELECT id FROM contact_rodas
                WHERE contact_id = %s
                  AND tipo = %s
                  AND conteudo = %s
                  AND status = 'pendente'
            ''', (contact_id, tipo, conteudo))

            if cursor.fetchone():
                return None  # Ja existe

            cursor.execute('''
                INSERT INTO contact_rodas (
                    contact_id, message_id, tipo, conteudo, tags, prazo, ai_confidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, contact_id, message_id, tipo, conteudo, tags, status, prazo, ai_confidence, criado_em
            ''', (
                contact_id,
                message_id,
                tipo,
                conteudo,
                tags or [],
                prazo,
                confidence
            ))

            row = cursor.fetchone()
            conn.commit()

            if row:
                return dict(row)
            return None

    def get_rodas_pendentes(self, contact_id: int) -> List[Dict]:
        """
        Retorna todas as rodas pendentes de um contato.
        Ordenadas por prioridade (tipo) e data de criacao.
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, contact_id, message_id, tipo, conteudo, tags, status, prazo, ai_confidence, criado_em
                FROM contact_rodas
                WHERE contact_id = %s AND status = 'pendente'
                ORDER BY
                    CASE tipo
                        WHEN 'promessa' THEN 1
                        WHEN 'favor_recebido' THEN 2
                        WHEN 'proximo_passo' THEN 3
                        WHEN 'topico' THEN 4
                        WHEN 'favor_feito' THEN 99
                        ELSE 100
                    END,
                    criado_em ASC
            ''', (contact_id,))

            return [dict(row) for row in cursor.fetchall()]

    def get_roda_mais_relevante(self, contact_id: int) -> Optional[Dict]:
        """
        Retorna a roda mais relevante/urgente de um contato.
        Prioriza promessas antigas > favores > proximos passos > topicos
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, contact_id, message_id, tipo, conteudo, tags, status, prazo, ai_confidence, criado_em,
                       EXTRACT(DAY FROM NOW() - criado_em) as dias_pendente
                FROM contact_rodas
                WHERE contact_id = %s AND status = 'pendente'
                ORDER BY
                    CASE tipo
                        WHEN 'promessa' THEN 1
                        WHEN 'favor_recebido' THEN 2
                        WHEN 'proximo_passo' THEN 3
                        WHEN 'topico' THEN 4
                        WHEN 'favor_feito' THEN 99
                        ELSE 100
                    END,
                    criado_em ASC
                LIMIT 1
            ''', (contact_id,))

            row = cursor.fetchone()
            return dict(row) if row else None

    def marcar_cumprida(self, roda_id: int) -> bool:
        """Marca uma roda como cumprida"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE contact_rodas
                SET status = 'cumprido', atualizado_em = NOW()
                WHERE id = %s
            ''', (roda_id,))
            conn.commit()
            return cursor.rowcount > 0

    def marcar_expirada(self, roda_id: int) -> bool:
        """Marca uma roda como expirada"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE contact_rodas
                SET status = 'expirado', atualizado_em = NOW()
                WHERE id = %s
            ''', (roda_id,))
            conn.commit()
            return cursor.rowcount > 0

    def expirar_rodas_antigas(self, dias_promessa: int = 30, dias_outros: int = 90) -> int:
        """
        Expira automaticamente rodas muito antigas.

        Args:
            dias_promessa: Dias para expirar promessas (default 30)
            dias_outros: Dias para expirar outros tipos (default 90)

        Returns:
            Numero de rodas expiradas
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE contact_rodas
                SET status = 'expirado', atualizado_em = NOW()
                WHERE status = 'pendente'
                  AND (
                    (tipo = 'promessa' AND criado_em < NOW() - INTERVAL '%s days')
                    OR (tipo != 'promessa' AND criado_em < NOW() - INTERVAL '%s days')
                  )
            ''', (dias_promessa, dias_outros))
            conn.commit()
            return cursor.rowcount

    def get_rodas_para_dashboard(self, limit: int = 10) -> List[Dict]:
        """
        Retorna contatos com rodas pendentes para o dashboard.
        Inclui dados do contato e a roda mais relevante.

        Prioriza:
        1. Promessas antigas (> 3 dias)
        2. Favores recebidos
        3. Proximos passos vencidos
        4. Topicos
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                WITH ranked_rodas AS (
                    SELECT
                        r.id as roda_id,
                        r.contact_id,
                        r.tipo,
                        r.conteudo,
                        r.tags,
                        r.prazo,
                        r.criado_em,
                        r.ai_confidence,
                        EXTRACT(DAY FROM NOW() - r.criado_em) as dias_pendente,
                        c.nome,
                        c.apelido,
                        c.empresa,
                        c.cargo,
                        c.circulo,
                        c.foto_url,
                        c.health_score,
                        c.ultimo_contato,
                        ROW_NUMBER() OVER (
                            PARTITION BY r.contact_id
                            ORDER BY
                                CASE r.tipo
                                    WHEN 'promessa' THEN 1
                                    WHEN 'favor_recebido' THEN 2
                                    WHEN 'proximo_passo' THEN 3
                                    WHEN 'topico' THEN 4
                                    ELSE 99
                                END,
                                r.criado_em ASC
                        ) as rn
                    FROM contact_rodas r
                    JOIN contacts c ON c.id = r.contact_id
                    WHERE r.status = 'pendente'
                      AND r.tipo != 'favor_feito'  -- favor_feito e historico, nao gera acao
                      -- Filtrar rodas stale: proximo_passo/topico com >14 dias E comunicação posterior
                      AND NOT (
                          r.tipo IN ('proximo_passo', 'topico')
                          AND r.criado_em < NOW() - INTERVAL '14 days'
                          AND EXISTS (
                              SELECT 1 FROM messages m
                              JOIN conversations cv ON cv.id = m.conversation_id
                              WHERE cv.contact_id = r.contact_id
                                AND m.direcao = 'outgoing'
                                AND m.enviado_em > r.criado_em
                          )
                      )
                )
                SELECT *
                FROM ranked_rodas
                WHERE rn = 1
                ORDER BY
                    -- Prioriza promessas antigas
                    CASE
                        WHEN tipo = 'promessa' AND dias_pendente > 3 THEN 0
                        WHEN tipo = 'promessa' THEN 1
                        WHEN tipo = 'favor_recebido' THEN 2
                        WHEN tipo = 'proximo_passo' AND prazo IS NOT NULL AND prazo < CURRENT_DATE THEN 3
                        WHEN tipo = 'proximo_passo' THEN 4
                        ELSE 5
                    END,
                    dias_pendente DESC
                LIMIT %s
            ''', (limit,))

            results = []
            for row in cursor.fetchall():
                results.append({
                    'roda': {
                        'id': row['roda_id'],
                        'tipo': row['tipo'],
                        'conteudo': row['conteudo'],
                        'tags': row['tags'],
                        'prazo': str(row['prazo']) if row['prazo'] else None,
                        'criado_em': row['criado_em'].isoformat() if row['criado_em'] else None,
                        'dias_pendente': int(row['dias_pendente']) if row['dias_pendente'] else 0,
                        'confidence': row['ai_confidence'],
                    },
                    'contact': {
                        'id': row['contact_id'],
                        'nome': row['nome'],
                        'apelido': row['apelido'],
                        'empresa': row['empresa'],
                        'cargo': row['cargo'],
                        'circulo': row['circulo'],
                        'foto_url': row['foto_url'],
                        'health_score': row.get('health_score'),
                        'ultimo_contato': row['ultimo_contato'].isoformat() if row.get('ultimo_contato') else None,
                    }
                })

            return results

    def get_historico_contato(self, contact_id: int, include_all: bool = False) -> List[Dict]:
        """
        Retorna historico de rodas de um contato.

        Args:
            contact_id: ID do contato
            include_all: Se True, inclui rodas cumpridas e expiradas
        """
        with get_db() as conn:
            cursor = conn.cursor()

            status_filter = "" if include_all else "AND status = 'pendente'"

            cursor.execute(f'''
                SELECT id, tipo, conteudo, tags, status, prazo, ai_confidence, criado_em, atualizado_em
                FROM contact_rodas
                WHERE contact_id = %s {status_filter}
                ORDER BY criado_em DESC
                LIMIT 50
            ''', (contact_id,))

            return [dict(row) for row in cursor.fetchall()]


# Singleton
_rodas_service = None


def get_rodas_service() -> RodasService:
    """Retorna instancia singleton do RodasService"""
    global _rodas_service
    if _rodas_service is None:
        _rodas_service = RodasService()
    return _rodas_service
