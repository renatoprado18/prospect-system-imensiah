"""
Notifications Service - Sistema de notificacoes priorizadas

Endpoint: GET /api/notifications
Endpoint: GET /api/notifications/count
"""
from typing import List, Dict
from datetime import datetime, timedelta
from database import get_db


class NotificationService:
    def get_notifications(self, limit: int = 20) -> List[Dict]:
        """
        Retorna notificacoes priorizadas.

        Tipos:
        - birthday_today: Aniversario hoje
        - birthday_upcoming: Aniversario nos proximos 3 dias
        - low_health: Contato com health baixo (< 30)
        - needs_attention: Contato precisando de atencao
        - unread_messages: Mensagens nao lidas
        """
        notifications = []

        with get_db() as conn:
            cursor = conn.cursor()

            # 1. Aniversarios HOJE (prioridade ALTA)
            cursor.execute("""
                SELECT id, nome, empresa, foto_url, aniversario, circulo
                FROM contacts
                WHERE aniversario IS NOT NULL
                AND EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
                AND EXTRACT(DAY FROM aniversario) = EXTRACT(DAY FROM CURRENT_DATE)
                AND COALESCE(circulo, 5) <= 4
            """)
            for row in cursor.fetchall():
                notifications.append({
                    "type": "birthday_today",
                    "title": f"Aniversario de {row['nome']}!",
                    "subtitle": row.get('empresa') or '',
                    "contact_id": row["id"],
                    "foto_url": row.get("foto_url"),
                    "circulo": row.get("circulo"),
                    "priority": "high",
                    "icon": "cake",
                    "action": f"/contato/{row['id']}",
                    "timestamp": datetime.now().isoformat()
                })

            # 2. Aniversarios nos proximos 3 dias
            cursor.execute("""
                WITH aniv_calc AS (
                    SELECT
                        id, nome, empresa, foto_url, aniversario, circulo,
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
                WHERE dias_ate > 0 AND dias_ate <= 3
                ORDER BY dias_ate
                LIMIT 5
            """)
            for row in cursor.fetchall():
                dias = int(row['dias_ate'])
                notifications.append({
                    "type": "birthday_upcoming",
                    "title": f"Aniversario de {row['nome']}",
                    "subtitle": f"Em {dias} dia{'s' if dias > 1 else ''}",
                    "contact_id": row["id"],
                    "foto_url": row.get("foto_url"),
                    "circulo": row.get("circulo"),
                    "priority": "medium",
                    "icon": "cake",
                    "days_until": dias,
                    "action": f"/contato/{row['id']}",
                    "timestamp": datetime.now().isoformat()
                })

            # 3. Health score critico (< 30) em circulos 1-3
            cursor.execute("""
                SELECT id, nome, empresa, foto_url, health_score, circulo,
                       EXTRACT(DAY FROM NOW() - ultimo_contato)::int as dias_sem_contato
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                AND COALESCE(health_score, 50) < 30
                ORDER BY health_score ASC
                LIMIT 5
            """)
            for row in cursor.fetchall():
                dias = row.get('dias_sem_contato') or 0
                notifications.append({
                    "type": "low_health",
                    "title": f"{row['nome']} precisa de atencao",
                    "subtitle": f"Health {row['health_score']}% - {dias} dias sem contato",
                    "contact_id": row["id"],
                    "foto_url": row.get("foto_url"),
                    "health_score": row["health_score"],
                    "circulo": row.get("circulo"),
                    "priority": "high",
                    "icon": "heart-pulse",
                    "action": f"/contato/{row['id']}",
                    "timestamp": datetime.now().isoformat()
                })

            # 4. Conversas que requerem resposta
            cursor.execute("""
                SELECT
                    c.id as conversation_id,
                    ct.id as contact_id,
                    ct.nome,
                    ct.foto_url,
                    c.canal,
                    c.assunto,
                    c.ultimo_mensagem
                FROM conversations c
                JOIN contacts ct ON ct.id = c.contact_id
                WHERE c.requer_resposta = TRUE
                ORDER BY c.ultimo_mensagem DESC
                LIMIT 5
            """)
            for row in cursor.fetchall():
                notifications.append({
                    "type": "needs_response",
                    "title": f"Responder {row['nome']}",
                    "subtitle": row.get('assunto') or f"Via {row['canal']}",
                    "contact_id": row["contact_id"],
                    "conversation_id": row["conversation_id"],
                    "foto_url": row.get("foto_url"),
                    "channel": row.get("canal"),
                    "priority": "medium",
                    "icon": "message-circle",
                    "action": f"/inbox/{row['conversation_id']}",
                    "timestamp": row['ultimo_mensagem'].isoformat() if row.get('ultimo_mensagem') else datetime.now().isoformat()
                })

            # 5. Tarefas pendentes para hoje
            cursor.execute("""
                SELECT
                    t.id,
                    t.titulo,
                    t.descricao,
                    t.prioridade,
                    t.contact_id,
                    c.nome as contact_name
                FROM tasks t
                LEFT JOIN contacts c ON c.id = t.contact_id
                WHERE t.status = 'pending'
                AND t.data_vencimento IS NOT NULL
                AND DATE(t.data_vencimento) <= CURRENT_DATE
                ORDER BY t.prioridade DESC, t.data_vencimento ASC
                LIMIT 5
            """)
            for row in cursor.fetchall():
                notifications.append({
                    "type": "task_due",
                    "title": row['titulo'],
                    "subtitle": row.get('contact_name') or row.get('descricao') or '',
                    "task_id": row["id"],
                    "contact_id": row.get("contact_id"),
                    "priority": "high" if row.get('prioridade', 5) >= 7 else "medium",
                    "icon": "check-square",
                    "action": "/tasks",
                    "timestamp": datetime.now().isoformat()
                })

        # Ordenar por prioridade
        priority_order = {"high": 0, "medium": 1, "low": 2}
        notifications.sort(key=lambda x: (
            priority_order.get(x.get("priority", "low"), 2),
            x.get("timestamp", "")
        ))

        return notifications[:limit]

    def get_notification_count(self) -> Dict:
        """Retorna contagem de notificacoes por tipo"""
        with get_db() as conn:
            cursor = conn.cursor()

            counts = {
                "birthdays_today": 0,
                "low_health": 0,
                "needs_response": 0,
                "tasks_due": 0,
                "total": 0
            }

            # Aniversarios hoje
            cursor.execute("""
                SELECT COUNT(*) as count FROM contacts
                WHERE aniversario IS NOT NULL
                AND EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
                AND EXTRACT(DAY FROM aniversario) = EXTRACT(DAY FROM CURRENT_DATE)
                AND COALESCE(circulo, 5) <= 4
            """)
            counts["birthdays_today"] = cursor.fetchone()["count"]

            # Health baixo
            cursor.execute("""
                SELECT COUNT(*) as count FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                AND COALESCE(health_score, 50) < 30
            """)
            counts["low_health"] = cursor.fetchone()["count"]

            # Requerem resposta
            cursor.execute("""
                SELECT COUNT(*) as count FROM conversations
                WHERE requer_resposta = TRUE
            """)
            counts["needs_response"] = cursor.fetchone()["count"]

            # Tarefas vencidas
            cursor.execute("""
                SELECT COUNT(*) as count FROM tasks
                WHERE status = 'pending'
                AND data_vencimento IS NOT NULL
                AND DATE(data_vencimento) <= CURRENT_DATE
            """)
            counts["tasks_due"] = cursor.fetchone()["count"]

            counts["total"] = sum([
                counts["birthdays_today"],
                counts["low_health"],
                counts["needs_response"],
                counts["tasks_due"]
            ])

            return counts


_notification_service = None


def get_notification_service() -> NotificationService:
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service
