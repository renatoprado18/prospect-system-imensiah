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

## TAREFA 1: Enriquecimento LinkedIn em Batch

**Status**: CONCLUIDO
**Prioridade**: CRITICA

**Criar script**: `scripts/enrich_linkedin_batch.py`

```python
#!/usr/bin/env python3
"""
Enriquecimento de LinkedIn em Batch

Busca dados do LinkedIn para contatos que tem URL mas faltam dados.
Usa Proxycurl API se disponivel, senao marca para enriquecimento manual.
"""
import os
import sys
import time
import httpx
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from dotenv import load_dotenv
load_dotenv()

from database import get_db

PROXYCURL_API_KEY = os.getenv("PROXYCURL_API_KEY", "")
PROXYCURL_URL = "https://nubela.co/proxycurl/api/v2/linkedin"


def fetch_linkedin_profile(linkedin_url: str) -> dict:
    """Busca dados do perfil via Proxycurl"""
    if not PROXYCURL_API_KEY:
        return {"error": "PROXYCURL_API_KEY not configured"}

    try:
        response = httpx.get(
            PROXYCURL_URL,
            params={"url": linkedin_url, "skills": "skip", "inferred_salary": "skip"},
            headers={"Authorization": f"Bearer {PROXYCURL_API_KEY}"},
            timeout=30.0
        )
        if response.status_code == 200:
            return response.json()
        else:
            return {"error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def enrich_batch(limit: int = 50, circulo_max: int = 3):
    """
    Enriquece contatos que tem LinkedIn URL mas faltam dados.

    Args:
        limit: Maximo de contatos por execucao
        circulo_max: Processar apenas circulos ate este valor
    """
    print("=" * 60)
    print("ENRIQUECIMENTO LINKEDIN - BATCH")
    print("=" * 60)
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Proxycurl API: {'Configurado' if PROXYCURL_API_KEY else 'NAO CONFIGURADO'}")
    print()

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contatos que precisam de enriquecimento
        cursor.execute("""
            SELECT id, nome, linkedin, linkedin_headline, empresa, cargo
            FROM contacts
            WHERE linkedin IS NOT NULL
            AND linkedin != ''
            AND COALESCE(circulo, 5) <= %s
            AND (
                linkedin_headline IS NULL
                OR empresa IS NULL
                OR cargo IS NULL
                OR ultimo_enriquecimento IS NULL
                OR ultimo_enriquecimento < NOW() - INTERVAL '90 days'
            )
            ORDER BY circulo ASC, ultimo_contato DESC NULLS LAST
            LIMIT %s
        """, (circulo_max, limit))

        contacts = cursor.fetchall()
        print(f"Contatos para enriquecer: {len(contacts)}")
        print()

        stats = {"success": 0, "skipped": 0, "error": 0}

        for contact in contacts:
            contact_id = contact["id"]
            nome = contact["nome"]
            linkedin_url = contact["linkedin"]

            print(f"Processando: {nome}")
            print(f"  LinkedIn: {linkedin_url}")

            if not PROXYCURL_API_KEY:
                # Sem API, apenas marcar como pendente
                cursor.execute("""
                    UPDATE contacts
                    SET enriquecimento_status = 'pending_manual',
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (contact_id,))
                stats["skipped"] += 1
                print(f"  -> Marcado para enriquecimento manual")
                continue

            # Buscar dados via API
            data = fetch_linkedin_profile(linkedin_url)

            if "error" in data:
                cursor.execute("""
                    UPDATE contacts
                    SET enriquecimento_status = %s,
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (f"error: {data['error']}", contact_id))
                stats["error"] += 1
                print(f"  -> Erro: {data['error']}")
                continue

            # Atualizar contato com dados do LinkedIn
            headline = data.get("headline") or data.get("occupation")
            company = data.get("experiences", [{}])[0].get("company") if data.get("experiences") else None
            position = data.get("experiences", [{}])[0].get("title") if data.get("experiences") else None
            location = data.get("city") or data.get("country_full_name")
            photo = data.get("profile_pic_url")
            summary = data.get("summary")

            updates = []
            params = []

            if headline and not contact["linkedin_headline"]:
                updates.append("linkedin_headline = %s")
                params.append(headline)

            if company and not contact["empresa"]:
                updates.append("empresa = %s")
                params.append(company)

            if position and not contact["cargo"]:
                updates.append("cargo = %s")
                params.append(position)

            if photo:
                updates.append("foto_url = COALESCE(foto_url, %s)")
                params.append(photo)

            if summary:
                updates.append("resumo_ai = COALESCE(resumo_ai, %s)")
                params.append(summary)

            updates.append("ultimo_enriquecimento = NOW()")
            updates.append("enriquecimento_status = 'success'")
            updates.append("atualizado_em = NOW()")

            if updates:
                query = f"UPDATE contacts SET {', '.join(updates)} WHERE id = %s"
                params.append(contact_id)
                cursor.execute(query, params)

            stats["success"] += 1
            print(f"  -> Enriquecido: {headline or 'N/A'}")

            # Rate limiting
            time.sleep(1)

        conn.commit()

        print()
        print("=" * 60)
        print("RESULTADO:")
        print(f"  Sucesso: {stats['success']}")
        print(f"  Pendente manual: {stats['skipped']}")
        print(f"  Erros: {stats['error']}")
        print("=" * 60)

        return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--circulo", type=int, default=3)
    args = parser.parse_args()

    enrich_batch(limit=args.limit, circulo_max=args.circulo)
```

**Adicionar endpoint em main.py**:

```python
@app.post("/api/contacts/enrich-linkedin-batch")
async def enrich_linkedin_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = 20,
    circulo_max: int = 3
):
    """Inicia enriquecimento LinkedIn em batch (background)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    from scripts.enrich_linkedin_batch import enrich_batch

    background_tasks.add_task(enrich_batch, limit, circulo_max)

    return {
        "status": "started",
        "message": f"Enriquecimento iniciado para ate {limit} contatos (circulos 1-{circulo_max})"
    }
```

**Commit**: `git commit -m "Add LinkedIn batch enrichment script and API"`

---

## TAREFA 2: Geracao de Insights AI em Batch

**Status**: CONCLUIDO
**Prioridade**: CRITICA

**Criar script**: `scripts/generate_insights_batch.py`

```python
#!/usr/bin/env python3
"""
Geracao de Insights AI em Batch

Analisa contatos e gera:
- Resumo AI (bio)
- Fatos extraidos
- Insights de relacionamento
- Sugestoes de follow-up
"""
import os
import sys
import json
import httpx
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from dotenv import load_dotenv
load_dotenv()

from database import get_db

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


async def call_claude(prompt: str, max_tokens: int = 2000) -> str:
    """Chama API do Claude"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60.0
        )
        if response.status_code == 200:
            data = response.json()
            return data["content"][0]["text"]
        else:
            raise Exception(f"Claude API error: {response.status_code}")


def get_contact_context(cursor, contact_id: int) -> dict:
    """Coleta contexto do contato para analise"""
    # Dados basicos
    cursor.execute("""
        SELECT id, nome, apelido, empresa, cargo, linkedin_headline,
               contexto, tags, aniversario, ultimo_contato, total_interacoes
        FROM contacts WHERE id = %s
    """, (contact_id,))
    contact = dict(cursor.fetchone())

    # Ultimas mensagens
    cursor.execute("""
        SELECT direcao, conteudo, enviado_em
        FROM messages
        WHERE contact_id = %s
        ORDER BY enviado_em DESC
        LIMIT 30
    """, (contact_id,))
    messages = [dict(row) for row in cursor.fetchall()]

    # Fatos existentes
    cursor.execute("""
        SELECT categoria, fato, fonte
        FROM contact_facts
        WHERE contact_id = %s
        ORDER BY criado_em DESC
        LIMIT 20
    """, (contact_id,))
    facts = [dict(row) for row in cursor.fetchall()]

    # Memorias
    cursor.execute("""
        SELECT tipo, titulo, resumo, data_ocorrencia
        FROM contact_memories
        WHERE contact_id = %s
        ORDER BY data_ocorrencia DESC
        LIMIT 10
    """, (contact_id,))
    memories = [dict(row) for row in cursor.fetchall()]

    return {
        "contact": contact,
        "messages": messages,
        "facts": facts,
        "memories": memories
    }


def build_analysis_prompt(context: dict) -> str:
    """Constroi prompt para analise do contato"""
    contact = context["contact"]
    messages = context["messages"]
    facts = context["facts"]

    # Formatar mensagens
    msgs_text = ""
    for msg in messages[:20]:
        direction = "EU" if msg["direcao"] == "outbound" else contact["nome"]
        date = msg["enviado_em"].strftime("%d/%m") if msg["enviado_em"] else ""
        content = (msg["conteudo"] or "")[:200]
        msgs_text += f"[{date}] {direction}: {content}\n"

    # Formatar fatos
    facts_text = "\n".join([f"- {f['fato']} ({f['categoria']})" for f in facts]) or "Nenhum"

    prompt = f"""Analise este contato e gere insights.

CONTATO:
- Nome: {contact['nome']}
- Apelido: {contact.get('apelido') or 'N/A'}
- Empresa: {contact.get('empresa') or 'N/A'}
- Cargo: {contact.get('cargo') or 'N/A'}
- Headline: {contact.get('linkedin_headline') or 'N/A'}
- Contexto: {contact.get('contexto') or 'professional'}
- Ultimo contato: {contact.get('ultimo_contato') or 'N/A'}
- Total interacoes: {contact.get('total_interacoes') or 0}

FATOS CONHECIDOS:
{facts_text}

ULTIMAS MENSAGENS:
{msgs_text or 'Nenhuma mensagem'}

Gere um JSON com:
{{
    "resumo": "Bio/resumo de 2-3 frases sobre quem e essa pessoa",
    "novos_fatos": [
        {{"categoria": "profissional|pessoal|preferencia|relacionamento", "fato": "descricao do fato"}}
    ],
    "insights": {{
        "tom_relacionamento": "formal|informal|amigavel|distante",
        "nivel_engajamento": "alto|medio|baixo",
        "interesses_identificados": ["lista de interesses"],
        "pontos_conexao": ["assuntos em comum ou formas de conexao"]
    }},
    "sugestoes_followup": [
        "sugestao 1",
        "sugestao 2"
    ]
}}

Responda APENAS com o JSON, sem explicacoes."""

    return prompt


async def generate_insights_batch(limit: int = 20, circulo_max: int = 3):
    """Gera insights para contatos em batch"""
    print("=" * 60)
    print("GERACAO DE INSIGHTS AI - BATCH")
    print("=" * 60)
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    if not ANTHROPIC_API_KEY:
        print("ERRO: ANTHROPIC_API_KEY nao configurado")
        return {"error": "API key not configured"}

    with get_db() as conn:
        cursor = conn.cursor()

        # Contatos que precisam de insights
        cursor.execute("""
            SELECT id, nome
            FROM contacts
            WHERE COALESCE(circulo, 5) <= %s
            AND (
                resumo_ai IS NULL
                OR insights_ai IS NULL
                OR insights_ai = '{}'::jsonb
            )
            AND total_interacoes > 0
            ORDER BY circulo ASC, total_interacoes DESC
            LIMIT %s
        """, (circulo_max, limit))

        contacts = cursor.fetchall()
        print(f"Contatos para processar: {len(contacts)}")
        print()

        stats = {"success": 0, "error": 0}

        for contact in contacts:
            contact_id = contact["id"]
            nome = contact["nome"]

            print(f"Processando: {nome}")

            try:
                # Coletar contexto
                context = get_contact_context(cursor, contact_id)

                # Gerar insights via Claude
                prompt = build_analysis_prompt(context)
                response = await call_claude(prompt)

                # Parse JSON
                try:
                    insights = json.loads(response)
                except json.JSONDecodeError:
                    # Tentar extrair JSON da resposta
                    import re
                    match = re.search(r'\{.*\}', response, re.DOTALL)
                    if match:
                        insights = json.loads(match.group())
                    else:
                        raise ValueError("Nao foi possivel extrair JSON")

                # Atualizar contato
                resumo = insights.get("resumo", "")
                cursor.execute("""
                    UPDATE contacts
                    SET resumo_ai = %s,
                        insights_ai = %s,
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (resumo, json.dumps(insights), contact_id))

                # Salvar novos fatos
                novos_fatos = insights.get("novos_fatos", [])
                for fato in novos_fatos:
                    cursor.execute("""
                        INSERT INTO contact_facts (contact_id, categoria, fato, fonte, confianca)
                        VALUES (%s, %s, %s, 'ai_analysis', 0.7)
                        ON CONFLICT DO NOTHING
                    """, (contact_id, fato.get("categoria", "geral"), fato.get("fato", "")))

                stats["success"] += 1
                print(f"  -> OK: {resumo[:50]}...")

            except Exception as e:
                stats["error"] += 1
                print(f"  -> ERRO: {str(e)}")

        conn.commit()

        print()
        print("=" * 60)
        print("RESULTADO:")
        print(f"  Sucesso: {stats['success']}")
        print(f"  Erros: {stats['error']}")
        print("=" * 60)

        return stats


if __name__ == "__main__":
    import asyncio
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--circulo", type=int, default=3)
    args = parser.parse_args()

    asyncio.run(generate_insights_batch(limit=args.limit, circulo_max=args.circulo))
```

**Adicionar endpoint em main.py**:

```python
@app.post("/api/contacts/generate-insights-batch")
async def generate_insights_batch_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = 10,
    circulo_max: int = 3
):
    """Inicia geracao de insights AI em batch (background)"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Nao autenticado")

    import asyncio
    from scripts.generate_insights_batch import generate_insights_batch

    async def run_task():
        await generate_insights_batch(limit, circulo_max)

    background_tasks.add_task(asyncio.run, run_task())

    return {
        "status": "started",
        "message": f"Geracao de insights iniciada para ate {limit} contatos (circulos 1-{circulo_max})"
    }
```

**Commit**: `git commit -m "Add AI insights batch generation script and API"`

---

## TAREFA 3: API de Busca Avancada de Contatos

**Status**: CONCLUIDO
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

            order_map = {
                "nome": "nome ASC",
                "empresa": "empresa ASC NULLS LAST",
                "circulo": "circulo ASC",
                "health": "health_score DESC",
                "ultimo_contato": "ultimo_contato DESC NULLS LAST",
                "recente": "atualizado_em DESC"
            }
            order_by = order_map.get(ordem, "nome ASC")

            cursor.execute(f"""
                SELECT COUNT(*) as total FROM contacts WHERE {where_clause}
            """, params)
            total = cursor.fetchone()["total"]

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

**Commit**: `git commit -m "Add advanced contact search API"`

---

## TAREFA 4: API de Exportacao de Dados

**Status**: CONCLUIDO
**Prioridade**: ALTA

_(codigo igual ao anterior)_

**Commit**: `git commit -m "Add data export API endpoints"`

---

## TAREFA 5: API de Acoes em Lote

**Status**: CONCLUIDO
**Prioridade**: MEDIA

_(codigo igual ao anterior)_

**Commit**: `git commit -m "Add batch operations API for contacts"`

---

## TAREFA 6: Cron de Manutencao Diaria

**Status**: CONCLUIDO
**Prioridade**: MEDIA

_(codigo igual ao anterior)_

**Commit**: `git commit -m "Add daily maintenance cron job"`

---

## TAREFA 7: SSE para Notificacoes

**Status**: CONCLUIDO
**Prioridade**: BAIXA

_(codigo igual ao anterior)_

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
| 2026-03-27 | Enriquecimento LinkedIn Batch | CONCLUIDO |
| 2026-03-27 | Geracao Insights AI Batch | CONCLUIDO |
| 2026-03-27 | API Busca Avancada | CONCLUIDO |
| 2026-03-27 | API Exportacao | CONCLUIDO |
| 2026-03-27 | API Batch Operations | CONCLUIDO |
| 2026-03-27 | Cron Manutencao | CONCLUIDO |
| 2026-03-27 | SSE Notifications | CONCLUIDO |
