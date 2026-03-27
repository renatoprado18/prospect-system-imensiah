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
import asyncio
from datetime import datetime
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                value = value.strip('"').strip("'")
                os.environ.setdefault(key, value)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

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
            raise Exception(f"Claude API error: {response.status_code} - {response.text}")


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
    print("=" * 60, flush=True)
    print("GERACAO DE INSIGHTS AI - BATCH", flush=True)
    print("=" * 60, flush=True)
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"Anthropic API: {'Configurado' if ANTHROPIC_API_KEY else 'NAO CONFIGURADO'}", flush=True)
    print(flush=True)

    if not ANTHROPIC_API_KEY:
        print("ERRO: ANTHROPIC_API_KEY nao configurado", flush=True)
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
        print(f"Contatos para processar: {len(contacts)}", flush=True)
        print(flush=True)

        stats = {"success": 0, "error": 0}

        for contact in contacts:
            contact_id = contact["id"]
            nome = contact["nome"]

            print(f"Processando: {nome}", flush=True)

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

                # Salvar novos fatos (verificar se tabela existe)
                try:
                    novos_fatos = insights.get("novos_fatos", [])
                    for fato in novos_fatos:
                        cursor.execute("""
                            INSERT INTO contact_facts (contact_id, categoria, fato, fonte, confianca)
                            VALUES (%s, %s, %s, 'ai_analysis', 0.7)
                            ON CONFLICT DO NOTHING
                        """, (contact_id, fato.get("categoria", "geral"), fato.get("fato", "")))
                except Exception as e:
                    # Tabela pode nao existir
                    print(f"    Aviso: Nao foi possivel salvar fatos: {e}", flush=True)

                stats["success"] += 1
                print(f"  -> OK: {resumo[:50] if resumo else 'N/A'}...", flush=True)

            except Exception as e:
                stats["error"] += 1
                print(f"  -> ERRO: {str(e)}", flush=True)

            # Rate limiting - aguardar entre requisicoes
            await asyncio.sleep(1)

        conn.commit()

        print(flush=True)
        print("=" * 60, flush=True)
        print("RESULTADO:", flush=True)
        print(f"  Sucesso: {stats['success']}", flush=True)
        print(f"  Erros: {stats['error']}", flush=True)
        print("=" * 60, flush=True)

        return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--circulo", type=int, default=3)
    args = parser.parse_args()

    asyncio.run(generate_insights_batch(limit=args.limit, circulo_max=args.circulo))
