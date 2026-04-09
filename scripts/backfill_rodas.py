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

    prompt = f"""Voce e um assistente de CRM que extrai APENAS compromissos profissionais relevantes de mensagens.

MENSAGEM: "{message_text}"
REMETENTE: {contact_name}
DIRECAO: {"Enviada por Renato" if direction == "outgoing" else "Recebida (do contato para Renato)"}

EXTRAIA UMA RODA APENAS SE TODOS OS CRITERIOS FOREM ATENDIDOS:
1. E um contexto PROFISSIONAL ou de NETWORKING (nao familiar, nao romantico, nao rotina pessoal)
2. Acao concreta que pode ser esquecida se nao for registrada
3. Identificacao CLARA de quem fez/promete o que para quem

TIPOS DE RODAS VALIDAS:
- promessa: RENATO prometeu ENTREGAR algo concreto ao CONTATO
  Validos: "vou te enviar a proposta", "te apresento ao diretor"
  NAO VALIDO: promessas vagas

- favor_recebido: CONTATO ajudou RENATO. RENATO e o BENEFICIARIO.
  Validos:
    - Contato escreve (incoming): "te indiquei pro fulano", "vou te apresentar ao investidor"
    - Renato escreve (outgoing): "obrigado por me indicar", "valeu pela apresentacao"
  NAO VALIDO: contato AGRADECENDO Renato

- favor_feito: RENATO ajudou o CONTATO. RENATO e o DOADOR. NAO requer retribuicao.
  Validos:
    - Renato escreve (outgoing): "te indiquei a Wanelise", "vou te apresentar ao Joao"
    - Contato escreve (incoming): "obrigada pela indicacao", "valeu pela apresentacao"
  Este tipo e apenas marcador historico de boa vontade.

- topico: PROJETO ou NEGOCIO discutido que pode gerar oportunidade

- proximo_passo: COMPROMISSO PROFISSIONAL agendado

## REGRA CRITICA - BENEFICIARIO

Antes de classificar como favor_recebido OU favor_feito, identifique:
- Quem PERFORMOU a acao (subject)?
- Quem RECEBEU o beneficio (object)?

Mensagem INCOMING (do contato) com "obrigad[ao]/valeu pela indicacao/apresentacao/ajuda"
  → contato AGRADECENDO Renato → RENATO foi o doador
  → tipo = "favor_feito", beneficiario = "contato"
  → JAMAIS classifique como favor_recebido

Mensagem OUTGOING (do Renato) com "obrigado por me indicar"
  → Renato agradecendo contato → contato foi o doador
  → tipo = "favor_recebido", beneficiario = "renato"

## CONTEUDO da roda DEVE preservar o sujeito
  RUIM: "indicacao de advogada Wanelise"
  BOM: "Renato indicou a advogada Wanelise para o contato"
  BOM: "Contato indicou Renato como palestrante"

SEMPRE RETORNE rodas: [] SE:
- Mensagem entre familiares ou casal
- Conversa social/pessoal sem contexto de negocios
- Nao da pra identificar quem ajudou quem
- Na duvida sobre o beneficiario

Responda APENAS com JSON (sem explicacoes):
{{
    "rodas": []
}}

OU se encontrar algo REALMENTE relevante:
{{
    "rodas": [
        {{
            "tipo": "promessa|favor_recebido|favor_feito|topico|proximo_passo",
            "conteudo": "descricao com sujeito explicito",
            "beneficiario": "renato|contato|null",
            "prazo": "data ou null",
            "tags": ["negocio"],
            "confidence": 0.7 a 1.0
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

                # Heuristica defensiva: detectar mensagem de agradecimento
                import re
                thank_you_pattern = re.compile(
                    r'\b(obrigad[ao]|valeu|grat[ao]|thanks?|obg|gracias)\b',
                    re.IGNORECASE
                )
                is_thank_you_msg = bool(thank_you_pattern.search(message_text or ''))

                filtered = []
                for roda in rodas:
                    tipo = roda.get('tipo', '')
                    beneficiario = (roda.get('beneficiario') or '').lower()

                    if tipo == 'promessa' and direction != 'outgoing':
                        continue

                    if tipo == 'favor_recebido':
                        if beneficiario and beneficiario != 'renato':
                            continue
                        # Renato beneficiario: aceita incoming OU outgoing (Renato agradecendo)
                        # Se incoming + agradecimento sem beneficiario explicito = quase sempre Renato doou
                        if is_thank_you_msg and direction == 'incoming' and beneficiario != 'renato':
                            roda['tipo'] = 'favor_feito'
                            roda['beneficiario'] = 'contato'
                            if roda.get('confidence', 0) >= 0.6:
                                filtered.append(roda)
                            continue

                    if tipo == 'favor_feito':
                        if beneficiario and beneficiario == 'renato':
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
