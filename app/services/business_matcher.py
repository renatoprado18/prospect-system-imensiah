"""
Business Matcher Service

Identifica qual negócio/produto é mais relevante para cada contato,
baseado em tags, cargo, empresa, setor e contexto da roda.
"""

from typing import Optional, List, Dict, Any
from database import get_db


def get_business_match(
    contact_tags: Optional[List[str]] = None,
    contact_cargo: Optional[str] = None,
    contact_empresa: Optional[str] = None,
    roda_tags: Optional[List[str]] = None,
    roda_conteudo: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Encontra o negócio mais relevante para um contato.

    Returns:
        Dict com: negocio, score, razao, talking_points
    """
    contact_tags = contact_tags or []
    roda_tags = roda_tags or []

    # Normalizar tudo para lowercase
    all_context = set()
    for tag in contact_tags + roda_tags:
        if tag:
            all_context.add(tag.lower().strip())

    if contact_cargo:
        all_context.add(contact_cargo.lower())
    if contact_empresa:
        all_context.add(contact_empresa.lower())
    if roda_conteudo:
        # Extrair palavras-chave do conteúdo da roda
        for word in roda_conteudo.lower().split():
            if len(word) > 4:
                all_context.add(word)

    # Buscar negócios e calcular score
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT slug, nome, descricao_curta, proposta_valor,
                   keywords, publico_alvo, talking_points, diferenciais, url
            FROM business_lines
            WHERE ativo = TRUE
        """)

        best_match = None
        best_score = 0
        best_reasons = []

        for row in cursor.fetchall():
            biz = dict(row)
            score = 0
            reasons = []

            keywords = biz.get('keywords') or []
            publico = biz.get('publico_alvo') or []

            # Match keywords (exige strings >= 4 chars para substring match)
            for kw in keywords:
                kw_lower = kw.lower()
                for ctx in all_context:
                    # Match exato ou substring apenas se ambos >= 4 chars
                    if kw_lower == ctx:
                        score += 10
                        reasons.append(f"keyword '{kw}'")
                        break
                    elif len(kw_lower) >= 4 and len(ctx) >= 4:
                        if kw_lower in ctx or ctx in kw_lower:
                            score += 10
                            reasons.append(f"keyword '{kw}'")
                            break

            # Match público-alvo (exige strings >= 4 chars para substring match)
            for pub in publico:
                pub_lower = pub.lower()
                for ctx in all_context:
                    if pub_lower == ctx:
                        score += 5
                        reasons.append(f"público '{pub}'")
                        break
                    elif len(pub_lower) >= 4 and len(ctx) >= 4:
                        if pub_lower in ctx or ctx in pub_lower:
                            score += 5
                            reasons.append(f"público '{pub}'")
                            break

            # Boost específico para FusIAH se contexto M&A
            if biz['slug'] == 'fusiah':
                ma_terms = ['m&a', 'fusão', 'aquisição', 'deal', 'transação',
                           'investimento', 'private equity', 'assessoria financeira',
                           'pactor', 'boutique']
                for term in ma_terms:
                    if any(term in ctx for ctx in all_context):
                        score += 15
                        reasons.append(f"contexto M&A")
                        break

            # Boost para consultoria se contexto governança
            if biz['slug'] == 'consultoria-conselhos':
                gov_terms = ['governança', 'conselho', 'board', 'sucessão', 'familiar']
                for term in gov_terms:
                    if any(term in ctx for ctx in all_context):
                        score += 15
                        reasons.append(f"contexto governança")
                        break

            # Jabô Café - MUITO restritivo, só com contexto explícito de café
            if biz['slug'] == 'jabo-cafe':
                cafe_terms = ['café', 'cafe', 'jabô', 'jabo']
                has_cafe_context = any(term in ctx for term in cafe_terms for ctx in all_context)
                if not has_cafe_context:
                    score = 0  # Zera score se não há contexto de café
                    reasons = []

            if score > best_score:
                best_score = score
                best_reasons = list(set(reasons))[:3]
                best_match = biz

        if best_match and best_score >= 15:
            return {
                "negocio": {
                    "slug": best_match['slug'],
                    "nome": best_match['nome'],
                    "descricao": best_match['descricao_curta'],
                    "url": best_match['url']
                },
                "score": best_score,
                "razao": f"Match: {', '.join(best_reasons)}",
                "talking_points": best_match.get('talking_points') or [],
                "diferenciais": best_match.get('diferenciais') or []
            }

    return None


def get_business_briefing(contact_id: int) -> Optional[Dict[str, Any]]:
    """
    Gera briefing de negócio para um contato específico.
    Busca dados do contato e suas rodas para fazer o match.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contato
        cursor.execute("""
            SELECT nome, empresa, cargo, tags
            FROM contacts
            WHERE id = %s
        """, (contact_id,))
        contact = cursor.fetchone()

        if not contact:
            return None

        c = dict(contact)

        # Buscar rodas pendentes
        cursor.execute("""
            SELECT tipo, conteudo, tags
            FROM contact_rodas
            WHERE contact_id = %s AND status = 'pendente'
            ORDER BY criado_em DESC
            LIMIT 3
        """, (contact_id,))
        rodas = cursor.fetchall()

        # Combinar tags das rodas
        roda_tags = []
        roda_conteudo = ""
        for r in rodas:
            rd = dict(r)
            if rd.get('tags'):
                roda_tags.extend(rd['tags'])
            if rd.get('conteudo'):
                roda_conteudo += " " + rd['conteudo']

        # Fazer match
        match = get_business_match(
            contact_tags=c.get('tags'),
            contact_cargo=c.get('cargo'),
            contact_empresa=c.get('empresa'),
            roda_tags=roda_tags,
            roda_conteudo=roda_conteudo
        )

        if match:
            return {
                "contact_nome": c['nome'],
                "contact_empresa": c.get('empresa'),
                **match
            }

        return None


# Teste direto
if __name__ == "__main__":
    # Testar com Mauricio (Pactor - M&A)
    result = get_business_match(
        contact_tags=["c-level", "consultoria"],
        contact_cargo="Sócio",
        contact_empresa="Pactor Finanças Corporativas",
        roda_tags=["networking", "reuniao"],
        roda_conteudo="Café de networking marcado na Pactor para apresentação do sistema de IA e MVP de Pitch de M&A"
    )

    if result:
        print(f"✅ Match encontrado!")
        print(f"   Negócio: {result['negocio']['nome']}")
        print(f"   Score: {result['score']}")
        print(f"   Razão: {result['razao']}")
        print(f"\n   Talking Points:")
        for tp in result['talking_points'][:3]:
            print(f"   • {tp}")
    else:
        print("❌ Nenhum match encontrado")
