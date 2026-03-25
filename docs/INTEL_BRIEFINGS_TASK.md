# INTEL - Tarefa: Sistema de Briefings Inteligentes

> **Instancia**: INTEL (Inteligencia)
> **Coordenador**: ARCH
> **Data**: 2026-03-25
> **Branch**: `feature/briefings-intel`

## Contexto

Antes de reunioes ou contatos importantes, Renato precisa de um **briefing inteligente**
que resuma tudo sobre a pessoa: historico, health score, fatos importantes, e sugestoes.

## Objetivo

Criar servico que gera briefings automaticos para contatos, especialmente:
1. Antes de reunioes agendadas (Google Calendar)
2. Para contatos do Circulo 1-3 precisando atencao
3. Sob demanda para qualquer contato

## Arquivos a Criar

### 1. CRIAR: `app/services/briefings.py`

```python
"""
Servico de Briefings Inteligentes

Gera resumos contextuais sobre contatos para preparacao de reunioes.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional
from app.database import get_db
from app.services.circulos import (
    CIRCULO_CONFIG,
    calcular_health_score,
    calcular_dias_sem_contato
)
import json
import os
from anthropic import Anthropic

# Cliente Anthropic
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def get_contact_data(contact_id: int) -> Optional[Dict]:
    """Busca dados completos do contato para briefing."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Dados basicos do contato
        cursor.execute("""
            SELECT id, nome, apelido, empresa, cargo, emails, telefones,
                   linkedin, foto_url, contexto, categorias, tags,
                   aniversario, circulo, health_score, ultimo_contato,
                   total_interacoes, resumo_ai, insights_ai
            FROM contacts
            WHERE id = %s
        """, (contact_id,))

        contact = cursor.fetchone()
        if not contact:
            return None

        contact = dict(contact)

        # Ultimas mensagens (WhatsApp + Email)
        cursor.execute("""
            SELECT m.conteudo, m.direcao, m.enviado_em, c.canal
            FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE m.contact_id = %s
            ORDER BY m.enviado_em DESC
            LIMIT 10
        """, (contact_id,))
        contact['ultimas_mensagens'] = [dict(r) for r in cursor.fetchall()]

        # Fatos importantes
        cursor.execute("""
            SELECT categoria, fato, fonte, confianca
            FROM contact_facts
            WHERE contact_id = %s
            ORDER BY confianca DESC, criado_em DESC
            LIMIT 15
        """, (contact_id,))
        contact['fatos'] = [dict(r) for r in cursor.fetchall()]

        # Memorias relevantes
        cursor.execute("""
            SELECT tipo, titulo, resumo, data_ocorrencia, importancia
            FROM contact_memories
            WHERE contact_id = %s
            ORDER BY importancia DESC, data_ocorrencia DESC
            LIMIT 10
        """, (contact_id,))
        contact['memorias'] = [dict(r) for r in cursor.fetchall()]

        # Tasks pendentes relacionadas
        cursor.execute("""
            SELECT titulo, descricao, data_vencimento, prioridade
            FROM tasks
            WHERE contact_id = %s AND status = 'pending'
            ORDER BY prioridade DESC, data_vencimento ASC
            LIMIT 5
        """, (contact_id,))
        contact['tasks_pendentes'] = [dict(r) for r in cursor.fetchall()]

        return contact


def format_contact_context(contact: Dict) -> str:
    """Formata dados do contato para contexto do AI."""
    parts = []

    # Info basica
    parts.append(f"CONTATO: {contact['nome']}")
    if contact.get('apelido'):
        parts.append(f"Apelido: {contact['apelido']}")
    if contact.get('empresa'):
        cargo = contact.get('cargo', '')
        parts.append(f"Trabalha: {cargo} @ {contact['empresa']}" if cargo else f"Empresa: {contact['empresa']}")

    # Circulo e Health
    circulo = contact.get('circulo') or 5
    health = contact.get('health_score') or 50
    config = CIRCULO_CONFIG.get(circulo, {})
    parts.append(f"Circulo: {circulo} ({config.get('nome', 'Arquivo')})")
    parts.append(f"Health Score: {health}%")

    # Ultimo contato
    dias = calcular_dias_sem_contato(contact.get('ultimo_contato'))
    if dias is not None:
        parts.append(f"Ultimo contato: {dias} dias atras")
    else:
        parts.append("Ultimo contato: desconhecido")

    parts.append(f"Total interacoes: {contact.get('total_interacoes', 0)}")

    # Aniversario
    if contact.get('aniversario'):
        aniv = contact['aniversario']
        parts.append(f"Aniversario: {aniv.strftime('%d/%m')}")

    # Tags
    tags = contact.get('tags')
    if tags:
        if isinstance(tags, str):
            tags = json.loads(tags) if tags else []
        if tags:
            parts.append(f"Tags: {', '.join(tags[:10])}")

    # Contexto
    if contact.get('contexto'):
        parts.append(f"Contexto: {contact['contexto']}")

    # Fatos importantes
    if contact.get('fatos'):
        parts.append("\nFATOS CONHECIDOS:")
        for f in contact['fatos'][:10]:
            parts.append(f"- [{f['categoria']}] {f['fato']}")

    # Memorias
    if contact.get('memorias'):
        parts.append("\nHISTORICO DE INTERACOES:")
        for m in contact['memorias'][:5]:
            data = m['data_ocorrencia'].strftime('%d/%m/%Y') if m.get('data_ocorrencia') else '?'
            parts.append(f"- [{data}] {m['titulo'] or m['resumo'][:50]}")

    # Ultimas mensagens
    if contact.get('ultimas_mensagens'):
        parts.append("\nULTIMAS MENSAGENS:")
        for msg in contact['ultimas_mensagens'][:5]:
            direcao = ">>>" if msg['direcao'] == 'outbound' else "<<<"
            canal = msg.get('canal', '?')
            conteudo = (msg.get('conteudo') or '')[:100]
            parts.append(f"- {direcao} [{canal}] {conteudo}...")

    # Tasks pendentes
    if contact.get('tasks_pendentes'):
        parts.append("\nTASKS PENDENTES:")
        for t in contact['tasks_pendentes']:
            parts.append(f"- {t['titulo']}")

    # Resumo AI existente
    if contact.get('resumo_ai'):
        parts.append(f"\nRESUMO ANTERIOR:\n{contact['resumo_ai']}")

    return "\n".join(parts)


def generate_briefing(
    contact_id: int,
    contexto_reuniao: str = None,
    incluir_sugestoes: bool = True
) -> Dict:
    """
    Gera briefing inteligente para um contato.

    Args:
        contact_id: ID do contato
        contexto_reuniao: Contexto adicional (ex: "Reuniao de conselho Vallen")
        incluir_sugestoes: Se deve incluir sugestoes de pauta/conversa

    Returns:
        Dict com briefing estruturado
    """
    # Buscar dados do contato
    contact = get_contact_data(contact_id)
    if not contact:
        return {"error": "Contato nao encontrado"}

    # Formatar contexto
    contact_context = format_contact_context(contact)

    # Construir prompt
    system_prompt = """Voce e um assistente pessoal que prepara briefings para reunioes.
Seu objetivo e ajudar o usuario a se preparar para interacoes com seus contatos.

Seja conciso e pratico. Foque em informacoes acionaveis.
Use bullet points. Nao seja excessivamente formal.

O usuario e um executivo brasileiro que valoriza relacionamentos pessoais e profissionais."""

    user_prompt = f"""Prepare um briefing para minha proxima interacao com este contato:

{contact_context}

{"CONTEXTO DA REUNIAO: " + contexto_reuniao if contexto_reuniao else ""}

Por favor, gere um briefing com:

1. **RESUMO** (2-3 frases sobre quem e a pessoa e nosso relacionamento)

2. **PONTOS DE ATENCAO** (o que devo lembrar/ter cuidado)

3. **HISTORICO RECENTE** (resumo das ultimas interacoes relevantes)

{"4. **SUGESTOES DE PAUTA** (3-5 topicos para conversar)" if incluir_sugestoes else ""}

{"5. **OPORTUNIDADES** (como posso agregar valor ou fortalecer a relacao)" if incluir_sugestoes else ""}

Seja direto e pratico. Maximo 300 palavras."""

    # Chamar Claude
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
            system=system_prompt
        )

        briefing_text = response.content[0].text

    except Exception as e:
        return {
            "error": f"Erro ao gerar briefing: {str(e)}",
            "contact_id": contact_id
        }

    # Montar resposta
    return {
        "contact_id": contact_id,
        "nome": contact['nome'],
        "empresa": contact.get('empresa'),
        "circulo": contact.get('circulo'),
        "health_score": contact.get('health_score'),
        "dias_sem_contato": calcular_dias_sem_contato(contact.get('ultimo_contato')),
        "briefing": briefing_text,
        "gerado_em": datetime.now().isoformat(),
        "contexto_reuniao": contexto_reuniao,
        # Dados extras para UI
        "foto_url": contact.get('foto_url'),
        "aniversario": contact.get('aniversario').isoformat() if contact.get('aniversario') else None,
        "tags": contact.get('tags'),
        "tasks_pendentes": len(contact.get('tasks_pendentes', []))
    }


def generate_briefings_for_calendar(
    data_inicio: datetime = None,
    data_fim: datetime = None
) -> List[Dict]:
    """
    Gera briefings para todas as reunioes no periodo.

    Busca eventos do Google Calendar e gera briefings para contatos identificados.
    """
    if data_inicio is None:
        data_inicio = datetime.now()
    if data_fim is None:
        data_fim = data_inicio + timedelta(days=7)

    # TODO: Integrar com Google Calendar API
    # Por enquanto, retorna lista vazia
    # Implementacao futura:
    # 1. Buscar eventos do calendario no periodo
    # 2. Para cada evento, identificar participantes
    # 3. Buscar contatos correspondentes por email
    # 4. Gerar briefing para cada contato

    return []


def get_contacts_needing_briefing(limit: int = 10) -> List[Dict]:
    """
    Retorna contatos que precisam de atencao e se beneficiariam de briefing.

    Criterios:
    - Circulo 1-3 com health < 50
    - Aniversario nos proximos 7 dias
    - Task pendente vencendo
    """
    with get_db() as conn:
        cursor = conn.cursor()

        results = []

        # Contatos precisando atencao (health baixo)
        cursor.execute("""
            SELECT id, nome, empresa, circulo, health_score, ultimo_contato
            FROM contacts
            WHERE COALESCE(circulo, 5) <= 3
              AND COALESCE(health_score, 50) < 50
            ORDER BY circulo ASC, health_score ASC
            LIMIT %s
        """, (limit,))

        for row in cursor.fetchall():
            contact = dict(row)
            contact['razao'] = f"Health {contact['health_score']}% - precisa contato"
            contact['prioridade'] = 'alta'
            results.append(contact)

        # Aniversarios proximos
        cursor.execute("""
            SELECT id, nome, empresa, circulo, aniversario
            FROM contacts
            WHERE aniversario IS NOT NULL
              AND COALESCE(circulo, 5) <= 4
        """)

        hoje = datetime.now().date()
        for row in cursor.fetchall():
            contact = dict(row)
            aniv = contact['aniversario']
            try:
                aniv_este_ano = aniv.replace(year=hoje.year)
                if aniv_este_ano < hoje:
                    aniv_este_ano = aniv.replace(year=hoje.year + 1)
                dias_ate = (aniv_este_ano - hoje).days
                if 0 <= dias_ate <= 7:
                    contact['razao'] = f"Aniversario em {dias_ate} dias"
                    contact['prioridade'] = 'media'
                    contact['dias_ate_aniversario'] = dias_ate
                    results.append(contact)
            except:
                continue

        # Remover duplicatas (mesmo contact_id)
        seen = set()
        unique_results = []
        for r in results:
            if r['id'] not in seen:
                seen.add(r['id'])
                unique_results.append(r)

        return unique_results[:limit]
```

## Funcoes a Implementar

| Funcao | Descricao |
|--------|-----------|
| `get_contact_data()` | Busca dados completos do contato |
| `format_contact_context()` | Formata dados para o prompt AI |
| `generate_briefing()` | Gera briefing usando Claude |
| `generate_briefings_for_calendar()` | Briefings para eventos do calendario |
| `get_contacts_needing_briefing()` | Lista contatos que precisam briefing |

## Estrutura do Briefing

O briefing gerado deve conter:

1. **Resumo**: Quem e a pessoa e como se relacionam
2. **Pontos de Atencao**: Cuidados, sensibilidades, lembretes
3. **Historico Recente**: Ultimas interacoes relevantes
4. **Sugestoes de Pauta**: Topicos para conversar
5. **Oportunidades**: Como agregar valor ao relacionamento

## Integracao com Circulos

O briefing deve usar dados do sistema de Circulos:
- Health score para indicar urgencia
- Frequencia ideal para contexto
- Dias sem contato para alerta

## Testes Recomendados

1. Gerar briefing para contato com muitos dados
2. Gerar briefing para contato com poucos dados
3. Verificar que fatos e memorias aparecem no contexto
4. Testar com diferentes circulos (1 vs 5)

## Criterios de Conclusao

- [ ] `app/services/briefings.py` criado
- [ ] Funcao `generate_briefing()` funcionando
- [ ] Contexto inclui: fatos, memorias, mensagens
- [ ] Testes manuais passando
- [ ] Atualizar COORDINATION.md com status

## Comunicacao

Ao terminar, atualize `docs/COORDINATION.md`:

```
[DATA INTEL] **FEATURE: Briefings Inteligentes**
Status: PRONTO PARA REVIEW
Arquivo: app/services/briefings.py
Testado: [listar testes]
```
