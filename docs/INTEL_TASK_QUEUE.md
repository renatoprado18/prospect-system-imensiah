# Fila de Tarefas 2INTEL

**Atualizacao**: 2026-03-27
**Modo**: AUTONOMO TOTAL - executar TODAS as tarefas sem parar

---

## INSTRUCOES GERAIS

- NAO perguntar nada, apenas executar
- Se encontrar erro, resolver e continuar
- Commit apos cada tarefa concluida
- Push direto em main
- Atualizar este arquivo marcando tarefas como CONCLUIDAS

---

## TAREFA 1: API Inbox Unificado

**Status**: EXECUTAR AGORA
**Prioridade**: CRITICA

**Criar arquivo**: `app/services/inbox.py`

```python
"""
Inbox Service - Unifica emails e WhatsApp
"""
from typing import List, Dict, Optional
from database import get_db

class InboxService:
    def get_conversations(self, limit: int = 50, filter_type: str = None) -> List[Dict]:
        """Lista conversas ordenadas por data"""
        with get_db() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    c.id,
                    c.contact_id,
                    ct.nome as contact_name,
                    ct.foto_url,
                    c.channel,
                    c.last_message_preview,
                    c.unread_count,
                    c.updated_at
                FROM conversations c
                LEFT JOIN contacts ct ON ct.id = c.contact_id
                WHERE 1=1
            """
            params = []

            if filter_type and filter_type != 'all':
                if filter_type == 'unread':
                    query += " AND c.unread_count > 0"
                else:
                    query += " AND c.channel = %s"
                    params.append(filter_type)

            query += " ORDER BY c.updated_at DESC LIMIT %s"
            params.append(limit)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_messages(self, conversation_id: int, limit: int = 100) -> List[Dict]:
        """Mensagens de uma conversa"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, conversation_id, direction, content,
                       enviado_em, lida, metadata
                FROM messages
                WHERE conversation_id = %s
                ORDER BY enviado_em DESC
                LIMIT %s
            """, (conversation_id, limit))
            return [dict(row) for row in cursor.fetchall()]

    def get_unread_count(self) -> int:
        """Total de nao lidos"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(SUM(unread_count), 0) as count FROM conversations")
            row = cursor.fetchone()
            return row["count"] if row else 0

    def mark_as_read(self, conversation_id: int) -> bool:
        """Marca conversa como lida"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE conversations SET unread_count = 0 WHERE id = %s
            """, (conversation_id,))
            cursor.execute("""
                UPDATE messages SET lida = TRUE WHERE conversation_id = %s
            """, (conversation_id,))
            conn.commit()
            return True

_inbox_service = None

def get_inbox_service() -> InboxService:
    global _inbox_service
    if _inbox_service is None:
        _inbox_service = InboxService()
    return _inbox_service
```

**Adicionar em main.py** (apos imports existentes):

```python
from services.inbox import get_inbox_service

@app.get("/api/inbox/conversations")
async def list_conversations(limit: int = 50, filter_type: str = None):
    service = get_inbox_service()
    conversations = service.get_conversations(limit, filter_type)
    return {"conversations": conversations}

@app.get("/api/inbox/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: int, limit: int = 100):
    service = get_inbox_service()
    messages = service.get_messages(conversation_id, limit)
    return {"messages": messages}

@app.get("/api/inbox/unread")
async def get_inbox_unread():
    service = get_inbox_service()
    count = service.get_unread_count()
    return {"unread": count}

@app.post("/api/inbox/conversations/{conversation_id}/read")
async def mark_conversation_read(conversation_id: int):
    service = get_inbox_service()
    service.mark_as_read(conversation_id)
    return {"success": True}
```

**Commit**: `git commit -m "Add Inbox API endpoints"`

---

## TAREFA 2: API Timeline de Contato

**Status**: PENDENTE
**Prioridade**: ALTA

**Criar arquivo**: `app/services/timeline.py`

```python
"""
Timeline Service - Historico unificado de interacoes
"""
from typing import List, Dict
from datetime import datetime
from database import get_db

class TimelineService:
    def get_contact_timeline(self, contact_id: int, limit: int = 50) -> List[Dict]:
        """Retorna timeline unificada do contato"""
        timeline = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Mensagens (email + whatsapp)
            cursor.execute("""
                SELECT
                    'message' as type,
                    m.id,
                    CASE WHEN m.direction = 'inbound' THEN 'Mensagem recebida' ELSE 'Mensagem enviada' END as title,
                    LEFT(m.content, 200) as content,
                    m.enviado_em as timestamp,
                    c.channel as metadata
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.contact_id = %s
                ORDER BY m.enviado_em DESC
                LIMIT %s
            """, (contact_id, limit))

            for row in cursor.fetchall():
                timeline.append(dict(row))

            # Memorias/Notas
            cursor.execute("""
                SELECT
                    'note' as type,
                    id,
                    'Nota' as title,
                    content,
                    created_at as timestamp,
                    category as metadata
                FROM contact_memories
                WHERE contact_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (contact_id, limit))

            for row in cursor.fetchall():
                timeline.append(dict(row))

        # Ordenar por timestamp
        timeline.sort(key=lambda x: x.get('timestamp') or datetime.min, reverse=True)
        return timeline[:limit]

_timeline_service = None

def get_timeline_service() -> TimelineService:
    global _timeline_service
    if _timeline_service is None:
        _timeline_service = TimelineService()
    return _timeline_service
```

**Adicionar em main.py**:

```python
from services.timeline import get_timeline_service

@app.get("/api/contacts/{contact_id}/timeline")
async def get_contact_timeline(contact_id: int, limit: int = 50):
    service = get_timeline_service()
    timeline = service.get_contact_timeline(contact_id, limit)
    return {"timeline": timeline, "contact_id": contact_id}
```

**Commit**: `git commit -m "Add Timeline API for contacts"`

---

## TAREFA 3: API Notificacoes

**Status**: PENDENTE
**Prioridade**: ALTA

**Criar arquivo**: `app/services/notifications.py`

```python
"""
Notifications Service
"""
from typing import List, Dict
from datetime import datetime, timedelta
from database import get_db

class NotificationService:
    def get_notifications(self, limit: int = 20) -> List[Dict]:
        """Retorna notificacoes priorizadas"""
        notifications = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Aniversarios hoje
            cursor.execute("""
                SELECT id, nome, foto_url, aniversario
                FROM contacts
                WHERE aniversario IS NOT NULL
                AND EXTRACT(MONTH FROM aniversario) = EXTRACT(MONTH FROM CURRENT_DATE)
                AND EXTRACT(DAY FROM aniversario) = EXTRACT(DAY FROM CURRENT_DATE)
                AND COALESCE(circulo, 5) <= 4
            """)
            for row in cursor.fetchall():
                notifications.append({
                    "type": "birthday_today",
                    "title": f"Aniversario de {row['nome']}",
                    "contact_id": row["id"],
                    "foto_url": row.get("foto_url"),
                    "priority": "high",
                    "timestamp": datetime.now().isoformat()
                })

            # Health baixo
            cursor.execute("""
                SELECT id, nome, foto_url, health_score, circulo
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                AND COALESCE(health_score, 50) < 30
                ORDER BY health_score ASC
                LIMIT 5
            """)
            for row in cursor.fetchall():
                notifications.append({
                    "type": "low_health",
                    "title": f"{row['nome']} precisa de atencao",
                    "contact_id": row["id"],
                    "foto_url": row.get("foto_url"),
                    "priority": "medium",
                    "health_score": row["health_score"],
                    "timestamp": datetime.now().isoformat()
                })

            # Mensagens nao lidas
            cursor.execute("""
                SELECT COUNT(*) as count FROM messages WHERE lida = FALSE
            """)
            unread = cursor.fetchone()["count"]
            if unread > 0:
                notifications.append({
                    "type": "unread_messages",
                    "title": f"{unread} mensagens nao lidas",
                    "priority": "medium",
                    "count": unread,
                    "timestamp": datetime.now().isoformat()
                })

        return notifications[:limit]

    def get_unread_count(self) -> int:
        """Total de notificacoes"""
        notifications = self.get_notifications(100)
        return len(notifications)

_notification_service = None

def get_notification_service() -> NotificationService:
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service
```

**Adicionar em main.py**:

```python
from services.notifications import get_notification_service

@app.get("/api/notifications")
async def list_notifications(limit: int = 20):
    service = get_notification_service()
    notifications = service.get_notifications(limit)
    return {"notifications": notifications, "total": len(notifications)}

@app.get("/api/notifications/count")
async def get_notification_count():
    service = get_notification_service()
    count = service.get_unread_count()
    return {"count": count}
```

**Commit**: `git commit -m "Add Notifications API"`

---

## TAREFA 4: Recalcular Health Scores

**Status**: PENDENTE
**Prioridade**: MEDIA

**Criar script**: `scripts/recalc_health.py`

```python
#!/usr/bin/env python3
"""Recalcula health scores de todos os contatos"""
import sys
sys.path.insert(0, 'app')
from dotenv import load_dotenv
load_dotenv()

from database import get_db
from datetime import datetime, timedelta

def recalc_health():
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar todos os contatos dos circulos 1-4
        cursor.execute("""
            SELECT id, circulo, ultimo_contato, frequencia_ideal_dias
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 4
        """)

        updated = 0
        for row in cursor.fetchall():
            contact_id = row["id"]
            circulo = row["circulo"] or 5
            ultimo = row["ultimo_contato"]
            freq = row["frequencia_ideal_dias"] or 30

            # Calcular health baseado em dias sem contato
            if ultimo:
                dias = (datetime.now() - ultimo).days
                health = max(0, min(100, 100 - (dias / freq * 50)))
            else:
                health = 20  # Sem contato registrado

            cursor.execute("""
                UPDATE contacts SET health_score = %s WHERE id = %s
            """, (int(health), contact_id))
            updated += 1

        conn.commit()
        print(f"Health scores atualizados: {updated} contatos")

if __name__ == "__main__":
    recalc_health()
```

**Executar**: `python scripts/recalc_health.py`

**Commit**: `git commit -m "Add health score recalculation script"`

---

## TAREFA 5: Criar Endpoint de Estatisticas

**Status**: PENDENTE
**Prioridade**: BAIXA

**Adicionar em main.py**:

```python
@app.get("/api/analytics/summary")
async def get_analytics_summary(days: int = 30):
    """Estatisticas para dashboard de analytics"""
    from database import get_db

    with get_db() as conn:
        cursor = conn.cursor()

        # Total contatos por circulo
        cursor.execute("""
            SELECT COALESCE(circulo, 5) as circulo, COUNT(*) as total
            FROM contacts GROUP BY COALESCE(circulo, 5)
        """)
        por_circulo = {row["circulo"]: row["total"] for row in cursor.fetchall()}

        # Interacoes no periodo
        cursor.execute("""
            SELECT COUNT(*) as total FROM messages
            WHERE enviado_em > NOW() - INTERVAL '%s days'
        """, (days,))
        interacoes = cursor.fetchone()["total"]

        # Health medio
        cursor.execute("""
            SELECT AVG(COALESCE(health_score, 50)) as avg
            FROM contacts WHERE COALESCE(circulo, 5) <= 4
        """)
        health_medio = cursor.fetchone()["avg"] or 50

        return {
            "periodo_dias": days,
            "por_circulo": por_circulo,
            "total_interacoes": interacoes,
            "health_medio": round(float(health_medio), 1)
        }
```

**Commit**: `git commit -m "Add analytics summary endpoint"`

---

## APOS COMPLETAR TODAS

```bash
git push origin main
```

Atualizar este arquivo marcando todas como **CONCLUIDAS**.

---

## Registro de Conclusao

| Data | Tarefa | Status |
|------|--------|--------|
| 2026-03-26 | Gmail/WhatsApp/Calendar/Tasks | CONCLUIDO |
| 2026-03-27 | API Inbox | _aguardando_ |
| 2026-03-27 | API Timeline | _aguardando_ |
| 2026-03-27 | API Notifications | _aguardando_ |
| 2026-03-27 | Recalc Health | _aguardando_ |
| 2026-03-27 | Analytics | _aguardando_ |
