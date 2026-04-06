#!/usr/bin/env python3
"""
Backfill Rodas - Processa mensagens historicas para extrair rodas

Uso:
    python scripts/backfill_rodas.py [--days 60] [--limit 1000] [--dry-run]

Opcoes:
    --days N      Processar mensagens dos ultimos N dias (default: 60)
    --limit N     Limite de mensagens a processar (default: 1000)
    --dry-run     Apenas mostra o que seria feito, sem persistir
"""
import sys
import os
import asyncio
import argparse
from datetime import datetime, timedelta

# Carregar .env ANTES de qualquer import do app
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value.strip('"').strip("'")

# Usar banco local para desenvolvimento
os.environ['USE_LOCAL_DB'] = '1'

# Adicionar diretorio app ao path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from database import get_db, init_db
from services.rodas_service import get_rodas_service, RODA_TYPES

# API do Claude
import httpx
import json

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


async def extract_rodas_from_message(message_text: str, contact_name: str, direction: str) -> list:
    """
    Chama Claude para extrair rodas de uma mensagem.
    Versao simplificada focada apenas em rodas.
    """
    if not ANTHROPIC_API_KEY:
        print("ERRO: ANTHROPIC_API_KEY nao configurada")
        return []

    prompt = f"""Analise esta mensagem e extraia "rodas de relacionamento" - fios de contexto.

MENSAGEM: "{message_text}"
DIRECAO: {"Enviada por Renato" if direction == "outgoing" else f"Recebida de {contact_name}"}

TIPOS DE RODAS:
- promessa: Renato PROMETE algo ("vou te enviar", "te mando amanha", "te apresento")
- favor_recebido: O contato FEZ UM FAVOR ("obrigado pela indicacao", "valeu por apresentar")
- topico: Assunto DISCUTIDO que pode ser retomado ("sobre o projeto X", "aquela ideia")
- proximo_passo: Compromisso futuro ("semana que vem", "depois conversamos")

REGRAS:
- 'promessa' so se a mensagem for ENVIADA por Renato
- 'favor_recebido' so se a mensagem for RECEBIDA
- Retorne array vazio se nao houver rodas relevantes
- Minimo de confianca: 0.6

Responda APENAS com JSON:
{{
    "rodas": [
        {{
            "tipo": "promessa|favor_recebido|topico|proximo_passo",
            "conteudo": "descricao curta",
            "prazo": "data/prazo se mencionado ou null",
            "tags": ["palavras", "chave"],
            "confidence": 0.0 a 1.0
        }}
    ]
}}"""

    try:
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
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30.0
            )

            if response.status_code == 200:
                data = response.json()
                text = data["content"][0]["text"].strip()

                # Limpar markdown
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                text = text.strip()

                result = json.loads(text)
                rodas = result.get('rodas', [])

                # Filtrar por direcao
                filtered = []
                for roda in rodas:
                    tipo = roda.get('tipo', '')
                    if tipo == 'promessa' and direction != 'outgoing':
                        continue
                    if tipo == 'favor_recebido' and direction != 'incoming':
                        continue
                    if roda.get('confidence', 0) >= 0.6:
                        filtered.append(roda)

                return filtered

    except Exception as e:
        print(f"  Erro na API Claude: {e}")

    return []


async def process_messages(days: int = 60, limit: int = 1000, dry_run: bool = False):
    """Processa mensagens historicas para extrair rodas."""

    print(f"\n{'='*60}")
    print(f"BACKFILL RODAS - Processando ultimos {days} dias")
    print(f"{'='*60}\n")

    # Inicializar banco (cria tabela se nao existir)
    init_db()

    since_date = datetime.now() - timedelta(days=days)

    # Buscar mensagens
    with get_db() as conn:
        cursor = conn.cursor()

        # Contar mensagens
        cursor.execute("""
            SELECT COUNT(*) as total
            FROM messages m
            JOIN contacts c ON c.id = m.contact_id
            WHERE m.enviado_em >= %s
            AND LENGTH(m.conteudo) > 20
        """, (since_date,))
        total = cursor.fetchone()['total']
        print(f"Total de mensagens elegíveis: {total}")

        # Buscar mensagens com contato
        cursor.execute("""
            SELECT
                m.id as message_id,
                m.contact_id,
                m.conteudo,
                m.direcao,
                m.enviado_em,
                c.nome as contact_name
            FROM messages m
            JOIN contacts c ON c.id = m.contact_id
            WHERE m.enviado_em >= %s
            AND LENGTH(m.conteudo) > 20
            ORDER BY m.enviado_em DESC
            LIMIT %s
        """, (since_date, limit))

        messages = cursor.fetchall()
        print(f"Processando: {len(messages)} mensagens\n")

    if not messages:
        print("Nenhuma mensagem encontrada.")
        return

    service = get_rodas_service()
    stats = {
        'processed': 0,
        'rodas_found': 0,
        'rodas_saved': 0,
        'errors': 0,
        'by_type': {t: 0 for t in RODA_TYPES}
    }

    for i, msg in enumerate(messages):
        message_id = msg['message_id']
        contact_id = msg['contact_id']
        conteudo = msg['conteudo']
        direcao = msg['direcao']
        contact_name = msg['contact_name']
        enviado_em = msg['enviado_em']

        # Truncar para display
        conteudo_short = conteudo[:50] + "..." if len(conteudo) > 50 else conteudo

        print(f"[{i+1}/{len(messages)}] {contact_name} ({direcao}): {conteudo_short}")

        try:
            rodas = await extract_rodas_from_message(conteudo, contact_name, direcao)
            stats['processed'] += 1

            if rodas:
                stats['rodas_found'] += len(rodas)

                for roda in rodas:
                    tipo = roda.get('tipo', '')
                    conteudo_roda = roda.get('conteudo', '')
                    tags = roda.get('tags', [])
                    prazo = roda.get('prazo')
                    confidence = roda.get('confidence', 0.5)

                    print(f"  → RODA: [{tipo}] {conteudo_roda} (conf: {confidence:.2f})")

                    if tipo in RODA_TYPES:
                        stats['by_type'][tipo] += 1

                    if not dry_run:
                        result = service.create_roda(
                            contact_id=contact_id,
                            tipo=tipo,
                            conteudo=conteudo_roda,
                            message_id=message_id,
                            tags=tags,
                            prazo=prazo,
                            confidence=confidence
                        )
                        if result:
                            stats['rodas_saved'] += 1
                    else:
                        stats['rodas_saved'] += 1  # Conta como se fosse salvar

            # Rate limiting - 1 req/sec para nao sobrecarregar API
            await asyncio.sleep(1)

        except Exception as e:
            print(f"  ERRO: {e}")
            stats['errors'] += 1

    # Resumo
    print(f"\n{'='*60}")
    print("RESUMO")
    print(f"{'='*60}")
    print(f"Mensagens processadas: {stats['processed']}")
    print(f"Rodas encontradas: {stats['rodas_found']}")
    print(f"Rodas salvas: {stats['rodas_saved']}")
    print(f"Erros: {stats['errors']}")
    print(f"\nPor tipo:")
    for tipo, count in stats['by_type'].items():
        if count > 0:
            print(f"  {tipo}: {count}")

    if dry_run:
        print("\n⚠️  DRY-RUN: Nenhuma roda foi realmente salva")


def main():
    parser = argparse.ArgumentParser(description='Backfill Rodas - Processa mensagens historicas')
    parser.add_argument('--days', type=int, default=60, help='Dias de historico (default: 60)')
    parser.add_argument('--limit', type=int, default=1000, help='Limite de mensagens (default: 1000)')
    parser.add_argument('--dry-run', action='store_true', help='Apenas simula, nao persiste')

    args = parser.parse_args()
    asyncio.run(process_messages(args.days, args.limit, args.dry_run))


if __name__ == '__main__':
    main()
