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

## TAREFA 1: API de Busca Avancada de Contatos

**Status**: EXECUTAR AGORA
**Prioridade**: ALTA

**Criar arquivo**: `app/services/search.py`

```python
"""
Search Service - Busca avancada de contatos
"""
from typing import List, Dict, Optional
from database import get_db


class SearchService:
    def search_contacts(
        self,
        query: str = None,
        circulo: int = None,
        tags: List[str] = None,
        health_min: int = None,
        health_max: int = None,
        has_email: bool = None,
        has_whatsapp: bool = None,
        empresa: str = None,
        ordem: str = "nome",
        limit: int = 50,
        offset: int = 0
    ) -> Dict:
        """Busca avancada com multiplos filtros"""
        with get_db() as conn:
            cursor = conn.cursor()

            conditions = ["1=1"]
            params = []

            if query:
                conditions.append("""
                    (nome ILIKE %s OR empresa ILIKE %s OR
                     apelido ILIKE %s OR cargo ILIKE %s)
                """)
                like_query = f"%{query}%"
                params.extend([like_query, like_query, like_query, like_query])

            if circulo is not None:
                conditions.append("COALESCE(circulo, 5) = %s")
                params.append(circulo)

            if tags:
                conditions.append("tags ?| %s")
                params.append(tags)

            if health_min is not None:
                conditions.append("COALESCE(health_score, 50) >= %s")
                params.append(health_min)

            if health_max is not None:
                conditions.append("COALESCE(health_score, 50) <= %s")
                params.append(health_max)

            if has_email:
                conditions.append("jsonb_array_length(emails) > 0")

            if has_whatsapp:
                conditions.append("jsonb_array_length(telefones) > 0")

            if empresa:
                conditions.append("empresa ILIKE %s")
                params.append(f"%{empresa}%")

            where_clause = " AND ".join(conditions)

            # Ordenacao
            order_map = {
                "nome": "nome ASC",
                "empresa": "empresa ASC NULLS LAST",
                "circulo": "circulo ASC",
                "health": "health_score DESC",
                "ultimo_contato": "ultimo_contato DESC NULLS LAST",
                "recente": "atualizado_em DESC"
            }
            order_by = order_map.get(ordem, "nome ASC")

            # Contar total
            cursor.execute(f"""
                SELECT COUNT(*) as total FROM contacts WHERE {where_clause}
            """, params)
            total = cursor.fetchone()["total"]

            # Buscar resultados
            cursor.execute(f"""
                SELECT id, nome, apelido, empresa, cargo, circulo,
                       health_score, foto_url, ultimo_contato, tags,
                       emails, telefones, linkedin
                FROM contacts
                WHERE {where_clause}
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
            """, params + [limit, offset])

            contacts = [dict(row) for row in cursor.fetchall()]

            return {
                "contacts": contacts,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(contacts) < total
            }

    def get_search_suggestions(self, query: str, limit: int = 10) -> List[Dict]:
        """Sugestoes de autocomplete"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT ON (nome) id, nome, empresa, foto_url, circulo
                FROM contacts
                WHERE nome ILIKE %s
                ORDER BY nome, circulo ASC
                LIMIT %s
            """, (f"%{query}%", limit))
            return [dict(row) for row in cursor.fetchall()]


_search_service = None

def get_search_service() -> SearchService:
    global _search_service
    if _search_service is None:
        _search_service = SearchService()
    return _search_service
```

**Adicionar em main.py** (antes de `# Vercel handler`):

```python
# ============== SEARCH API ==============

from services.search import get_search_service

@app.get("/api/search/contacts")
async def search_contacts(
    request: Request,
    q: str = None,
    circulo: int = None,
    tags: str = None,
    health_min: int = None,
    health_max: int = None,
    has_email: bool = None,
    has_whatsapp: bool = None,
    empresa: str = None,
    ordem: str = "nome",
    limit: int = 50,
    offset: int = 0
):
    """Busca avancada de contatos com filtros"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    service = get_search_service()
    tags_list = tags.split(",") if tags else None

    result = service.search_contacts(
        query=q,
        circulo=circulo,
        tags=tags_list,
        health_min=health_min,
        health_max=health_max,
        has_email=has_email,
        has_whatsapp=has_whatsapp,
        empresa=empresa,
        ordem=ordem,
        limit=limit,
        offset=offset
    )
    return result


@app.get("/api/search/suggestions")
async def search_suggestions(
    request: Request,
    q: str,
    limit: int = 10
):
    """Autocomplete para busca de contatos"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    if len(q) < 2:
        return {"suggestions": []}

    service = get_search_service()
    suggestions = service.get_search_suggestions(q, limit)
    return {"suggestions": suggestions}
```

**Commit**: `git commit -m "Add advanced contact search API"`

---

## TAREFA 2: API de Exportacao de Dados

**Status**: PENDENTE
**Prioridade**: ALTA

**Adicionar em main.py**:

```python
# ============== EXPORT API ==============

@app.get("/api/export/contacts")
async def export_contacts(
    request: Request,
    format: str = "csv",
    circulo: int = None
):
    """Exporta contatos em CSV ou JSON"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        query = """
            SELECT id, nome, apelido, empresa, cargo,
                   emails, telefones, linkedin, circulo,
                   health_score, tags, aniversario,
                   ultimo_contato, contexto
            FROM contacts
        """
        params = []

        if circulo:
            query += " WHERE COALESCE(circulo, 5) = %s"
            params.append(circulo)

        query += " ORDER BY nome"
        cursor.execute(query, params)
        contacts = cursor.fetchall()

    if format == "json":
        return {
            "contacts": [dict(c) for c in contacts],
            "total": len(contacts),
            "exported_at": datetime.now().isoformat()
        }

    # CSV format
    import csv
    import io
    from fastapi.responses import StreamingResponse

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "ID", "Nome", "Apelido", "Empresa", "Cargo",
        "Email Principal", "Telefone Principal", "LinkedIn",
        "Circulo", "Health Score", "Tags", "Aniversario",
        "Ultimo Contato", "Contexto"
    ])

    # Rows
    for c in contacts:
        emails = c["emails"] or []
        telefones = c["telefones"] or []
        tags = c["tags"] or []

        writer.writerow([
            c["id"],
            c["nome"],
            c.get("apelido") or "",
            c.get("empresa") or "",
            c.get("cargo") or "",
            emails[0] if emails else "",
            telefones[0] if telefones else "",
            c.get("linkedin") or "",
            c.get("circulo") or 5,
            c.get("health_score") or 50,
            ", ".join(tags) if tags else "",
            c.get("aniversario") or "",
            c.get("ultimo_contato") or "",
            c.get("contexto") or "professional"
        ])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=contacts_{datetime.now().strftime('%Y%m%d')}.csv"
        }
    )


@app.get("/api/export/analytics")
async def export_analytics(
    request: Request,
    days: int = 30
):
    """Exporta relatorio de analytics"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    with get_db() as conn:
        cursor = conn.cursor()

        # Resumo por circulo
        cursor.execute("""
            SELECT
                COALESCE(circulo, 5) as circulo,
                COUNT(*) as total,
                ROUND(AVG(COALESCE(health_score, 50))::numeric, 1) as health_medio,
                COUNT(*) FILTER (WHERE ultimo_contato > NOW() - INTERVAL '%s days') as contatados
            FROM contacts
            GROUP BY COALESCE(circulo, 5)
            ORDER BY circulo
        """, (days,))
        por_circulo = [dict(row) for row in cursor.fetchall()]

        # Interacoes por canal
        cursor.execute("""
            SELECT
                c.canal,
                COUNT(*) as total_mensagens,
                COUNT(DISTINCT c.contact_id) as contatos_unicos
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.enviado_em > NOW() - INTERVAL '%s days'
            GROUP BY c.canal
        """, (days,))
        por_canal = [dict(row) for row in cursor.fetchall()]

        # Top contatos por interacao
        cursor.execute("""
            SELECT
                ct.id, ct.nome, ct.empresa, ct.circulo,
                COUNT(m.id) as total_mensagens
            FROM contacts ct
            JOIN conversations c ON c.contact_id = ct.id
            JOIN messages m ON m.conversation_id = c.id
            WHERE m.enviado_em > NOW() - INTERVAL '%s days'
            GROUP BY ct.id, ct.nome, ct.empresa, ct.circulo
            ORDER BY total_mensagens DESC
            LIMIT 20
        """, (days,))
        top_contatos = [dict(row) for row in cursor.fetchall()]

        return {
            "periodo_dias": days,
            "por_circulo": por_circulo,
            "por_canal": por_canal,
            "top_contatos": top_contatos,
            "gerado_em": datetime.now().isoformat()
        }
```

**Commit**: `git commit -m "Add data export API endpoints"`

---

## TAREFA 3: API de Acoes em Lote

**Status**: PENDENTE
**Prioridade**: MEDIA

**Adicionar em main.py**:

```python
# ============== BATCH OPERATIONS API ==============

class BatchOperation(BaseModel):
    contact_ids: List[int]
    action: str
    value: Optional[str] = None

@app.post("/api/contacts/batch")
async def batch_contact_operation(
    request: Request,
    operation: BatchOperation
):
    """Operacoes em lote para contatos"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    if not operation.contact_ids:
        raise HTTPException(status_code=400, detail="Nenhum contato selecionado")

    with get_db() as conn:
        cursor = conn.cursor()
        updated = 0

        if operation.action == "set_circulo":
            circulo = int(operation.value)
            if circulo not in [1, 2, 3, 4, 5]:
                raise HTTPException(status_code=400, detail="Circulo invalido")

            cursor.execute("""
                UPDATE contacts
                SET circulo = %s, circulo_manual = TRUE, atualizado_em = NOW()
                WHERE id = ANY(%s)
            """, (circulo, operation.contact_ids))
            updated = cursor.rowcount

        elif operation.action == "add_tag":
            if not operation.value:
                raise HTTPException(status_code=400, detail="Tag nao especificada")

            cursor.execute("""
                UPDATE contacts
                SET tags = COALESCE(tags, '[]'::jsonb) || %s::jsonb,
                    atualizado_em = NOW()
                WHERE id = ANY(%s)
                AND NOT (COALESCE(tags, '[]'::jsonb) ? %s)
            """, (json.dumps([operation.value]), operation.contact_ids, operation.value))
            updated = cursor.rowcount

        elif operation.action == "remove_tag":
            if not operation.value:
                raise HTTPException(status_code=400, detail="Tag nao especificada")

            cursor.execute("""
                UPDATE contacts
                SET tags = COALESCE(tags, '[]'::jsonb) - %s,
                    atualizado_em = NOW()
                WHERE id = ANY(%s)
            """, (operation.value, operation.contact_ids))
            updated = cursor.rowcount

        elif operation.action == "set_contexto":
            if operation.value not in ["professional", "personal", "family"]:
                raise HTTPException(status_code=400, detail="Contexto invalido")

            cursor.execute("""
                UPDATE contacts
                SET contexto = %s, atualizado_em = NOW()
                WHERE id = ANY(%s)
            """, (operation.value, operation.contact_ids))
            updated = cursor.rowcount

        elif operation.action == "delete":
            cursor.execute("""
                DELETE FROM contacts WHERE id = ANY(%s)
            """, (operation.contact_ids,))
            updated = cursor.rowcount

        else:
            raise HTTPException(status_code=400, detail=f"Acao desconhecida: {operation.action}")

        conn.commit()

        return {
            "success": True,
            "action": operation.action,
            "updated": updated,
            "requested": len(operation.contact_ids)
        }
```

**Commit**: `git commit -m "Add batch operations API for contacts"`

---

## TAREFA 4: Cron de Manutencao Diaria

**Status**: PENDENTE
**Prioridade**: MEDIA

**Adicionar em main.py**:

```python
@app.get("/api/cron/daily-maintenance")
async def daily_maintenance():
    """
    Cron diario de manutencao.
    Executar via Vercel Cron: 0 6 * * *
    """
    results = {
        "health_recalc": 0,
        "stale_conversations": 0,
        "notifications_cleaned": 0
    }

    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Recalcular health scores
        cursor.execute("""
            UPDATE contacts
            SET health_score = GREATEST(0, LEAST(100,
                100 - (EXTRACT(DAY FROM NOW() - ultimo_contato)::int /
                       COALESCE(frequencia_ideal_dias, 30) * 50)
            ))
            WHERE COALESCE(circulo, 5) <= 4
            AND ultimo_contato IS NOT NULL
        """)
        results["health_recalc"] = cursor.rowcount

        # 2. Fechar conversas antigas sem atividade (30 dias)
        cursor.execute("""
            UPDATE conversations
            SET status = 'closed', atualizado_em = NOW()
            WHERE status = 'open'
            AND ultimo_mensagem < NOW() - INTERVAL '30 days'
        """)
        results["stale_conversations"] = cursor.rowcount

        # 3. Limpar lembretes antigos ja notificados
        cursor.execute("""
            DELETE FROM reminders
            WHERE status = 'completed'
            AND notificado_em < NOW() - INTERVAL '90 days'
        """)
        results["notifications_cleaned"] = cursor.rowcount

        conn.commit()

    return {
        "success": True,
        "results": results,
        "executed_at": datetime.now().isoformat()
    }
```

**Adicionar ao vercel.json**:

```json
{
  "crons": [
    {
      "path": "/api/cron/sync-contacts",
      "schedule": "0 9 * * *"
    },
    {
      "path": "/api/cron/daily-maintenance",
      "schedule": "0 6 * * *"
    }
  ]
}
```

**Commit**: `git commit -m "Add daily maintenance cron job"`

---

## TAREFA 5: WebSocket para Notificacoes Real-time

**Status**: PENDENTE
**Prioridade**: BAIXA

**Nota**: Vercel nao suporta WebSockets nativamente. Implementar usando polling ou Server-Sent Events (SSE).

**Adicionar em main.py**:

```python
from fastapi.responses import StreamingResponse
import asyncio

@app.get("/api/notifications/stream")
async def notification_stream(request: Request):
    """
    Server-Sent Events para notificacoes em tempo real.
    Uso: EventSource('/api/notifications/stream')
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    async def event_generator():
        from services.notifications import get_notification_service
        service = get_notification_service()
        last_count = 0

        while True:
            # Verificar se cliente desconectou
            if await request.is_disconnected():
                break

            # Buscar contagem atual
            counts = service.get_notification_count()
            current_count = counts["total"]

            # Se mudou, enviar evento
            if current_count != last_count:
                data = json.dumps(counts)
                yield f"data: {data}\n\n"
                last_count = current_count

            # Aguardar 30 segundos
            await asyncio.sleep(30)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )
```

**Commit**: `git commit -m "Add SSE endpoint for real-time notifications"`

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
| 2026-03-27 | API Inbox/Timeline/Notifications/Analytics | CONCLUIDO |
| 2026-03-27 | API Busca Avancada | _aguardando_ |
| 2026-03-27 | API Exportacao | _aguardando_ |
| 2026-03-27 | API Batch Operations | _aguardando_ |
| 2026-03-27 | Cron Manutencao | _aguardando_ |
| 2026-03-27 | SSE Notifications | _aguardando_ |
