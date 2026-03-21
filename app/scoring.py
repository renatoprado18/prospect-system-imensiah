"""
Sistema de Scoring Dinâmico para Prospects ImensIAH

Este sistema aprende com os resultados das reuniões e conversões
para melhorar continuamente a qualificação de prospects.

Melhorias v2.0:
- Fuzzy matching para detecção de cargos
- Normalização avançada de texto
- Novos fatores de scoring (origem, idade, engajamento)
- Análise ICP aprimorada com insights acionáveis
- Recálculo de scores em batch
"""
import json
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import re
import unicodedata

from database import get_connection


# =============================================================================
# FUNÇÕES AUXILIARES DE NORMALIZAÇÃO E FUZZY MATCHING
# =============================================================================

def normalize_text(text: str) -> str:
    """
    Normaliza texto removendo acentos, pontuação e convertendo para minúsculas.
    Útil para comparação fuzzy de cargos e setores.
    """
    if not text:
        return ""
    # Remove acentos
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    # Lowercase e remove pontuação extra
    text = text.lower().strip()
    # Normaliza espaços
    text = re.sub(r'\s+', ' ', text)
    return text


def expand_abbreviations(text: str) -> str:
    """Expande abreviações comuns em cargos."""
    abbreviations = {
        r'\bdir\.?\b': 'diretor',
        r'\bger\.?\b': 'gerente',
        r'\bcoord\.?\b': 'coordenador',
        r'\bsup\.?\b': 'supervisor',
        r'\bsr\.?\b': 'senior',
        r'\bjr\.?\b': 'junior',
        r'\bpres\.?\b': 'presidente',
        r'\bvp\b': 'vice presidente',
        r'\bti\b': 'tecnologia',
        r'\brh\b': 'recursos humanos',
        r'\bmkt\b': 'marketing',
    }
    result = text.lower()
    for pattern, replacement in abbreviations.items():
        result = re.sub(pattern, replacement, result)
    return result


def fuzzy_match_score(text: str, keyword: str) -> float:
    """
    Retorna score de similaridade entre texto e keyword (0.0 a 1.0).
    """
    text = normalize_text(text)
    keyword = normalize_text(keyword)

    # Match exato = 1.0
    if keyword in text:
        return 1.0

    # Verifica cada palavra do texto
    words = text.split()
    best_ratio = 0.0
    for word in words:
        if len(word) >= 3 and len(keyword) >= 3:
            ratio = SequenceMatcher(None, word, keyword).ratio()
            best_ratio = max(best_ratio, ratio)

    return best_ratio


def extract_cargo_components(cargo: str) -> Dict[str, Any]:
    """
    Extrai componentes do cargo (nível, área, senioridade).
    Ex: "Diretor de Marketing" -> {nivel: "diretor", area: "marketing", senior: False}
    """
    cargo = normalize_text(expand_abbreviations(cargo))

    niveis = {
        'c-level': ['ceo', 'cfo', 'cto', 'cmo', 'coo', 'cio', 'chief'],
        'diretor': ['diretor', 'director', 'diretora'],
        'gerente': ['gerente', 'manager', 'head'],
        'coordenador': ['coordenador', 'coordenadora', 'coordinator'],
        'supervisor': ['supervisor', 'supervisora'],
        'analista': ['analista', 'analyst'],
        'especialista': ['especialista', 'specialist'],
    }

    areas = {
        'executivo': ['executivo', 'executive', 'geral', 'general'],
        'comercial': ['comercial', 'vendas', 'sales', 'business'],
        'financeiro': ['financeiro', 'financas', 'finance', 'financial'],
        'tecnologia': ['tecnologia', 'technology', 'tech', 'ti', 'sistemas'],
        'marketing': ['marketing', 'mkt', 'growth', 'digital'],
        'operacoes': ['operacoes', 'operations', 'producao'],
        'rh': ['rh', 'recursos humanos', 'people', 'gente'],
    }

    result = {'nivel': None, 'area': None, 'senior': False, 'founder': False}

    for nivel, keywords in niveis.items():
        for kw in keywords:
            if kw in cargo:
                result['nivel'] = nivel
                break
        if result['nivel']:
            break

    for area, keywords in areas.items():
        for kw in keywords:
            if kw in cargo:
                result['area'] = area
                break
        if result['area']:
            break

    if any(s in cargo for s in ['senior', 'sr', 'pleno', 'principal']):
        result['senior'] = True

    if any(f in cargo for f in ['fundador', 'founder', 'owner', 'socio', 'sócio', 'proprietario']):
        result['founder'] = True

    return result


# =============================================================================
# CLASSES DE SCORING
# =============================================================================

@dataclass
class ScoringWeights:
    """Pesos dinâmicos baseados em aprendizado"""

    # Pesos base para cargos (ajustados pelo sistema)
    cargo_weights: Dict[str, int] = field(default_factory=lambda: {
        # Tier 1 - Decision Makers (28-30 pts)
        "ceo": 30, "chief executive officer": 30, "presidente": 30,
        "fundador": 30, "founder": 30, "co-founder": 30, "cofundador": 30,
        "sócio": 28, "socio": 28, "owner": 28, "proprietário": 28,
        "sócio proprietário": 30, "sócio-proprietário": 30,
        "dono": 28, "acionista": 25,

        # Tier 2 - C-Level & Directors (24-26 pts)
        "cfo": 25, "coo": 25, "cto": 25, "cmo": 25, "cio": 25,
        "chief": 25, "c-level": 25,
        "diretor": 24, "director": 24, "diretora": 24,
        "diretor geral": 26, "diretor executivo": 26,
        "managing director": 26, "diretor comercial": 24,
        "vice presidente": 24, "vice-presidente": 24, "vp": 24,

        # Tier 3 - Board & Advisory (18-24 pts)
        "conselheiro": 22, "conselheira": 22, "board member": 22,
        "conselheiro consultivo": 24, "conselheira consultiva": 24,
        "advisor": 20, "consultor": 18, "consultora": 18,

        # Tier 4 - Senior Management (12-18 pts)
        "gerente geral": 18, "general manager": 18,
        "gerente": 15, "manager": 15, "head": 16,
        "superintendente": 17, "coordenador": 12, "coordenadora": 12,

        # Tier 5 - Outros (16-20 pts)
        "partner": 20, "empreendedor": 18, "entrepreneur": 18,
        "investidor": 16, "investor": 16,
    })

    # Pesos base para setores
    setor_weights: Dict[str, int] = field(default_factory=lambda: {
        # Alta prioridade (18-20 pts)
        "consultoria": 20, "consulting": 20, "advisory": 20,
        "estratégia": 20, "strategy": 20,
        "governança": 20, "governance": 20,
        "finanças": 18, "financeiro": 18, "finance": 18,
        "startup": 18, "ventures": 16, "capital": 16,
        # Média prioridade (14-16 pts)
        "banco": 15, "bank": 15, "investment": 16,
        "tecnologia": 15, "tech": 15, "technology": 15, "software": 15,
        "energia": 14, "energy": 14,
        "saúde": 14, "health": 14, "healthcare": 14,
        # Baixa prioridade (10-12 pts)
        "indústria": 12, "industria": 12, "manufacturing": 12,
        "varejo": 12, "retail": 12, "comércio": 12,
        "serviços": 10, "services": 10,
        "educação": 10, "education": 10,
        "agro": 12, "agronegocio": 12, "agribusiness": 12,
    })

    # Pesos por origem do lead (NOVO)
    origem_weights: Dict[str, int] = field(default_factory=lambda: {
        "indicacao": 15,          # Indicação de cliente
        "referral": 15,
        "linkedin": 10,           # LinkedIn direto
        "linkedin_import": 8,     # Import em massa do LinkedIn
        "evento": 12,             # Conheceu em evento
        "ibgc": 12,               # Via IBGC
        "inbound": 8,             # Veio pelo site
        "outbound": 5,            # Prospecção ativa
        "csv_import": 3,          # Import genérico
    })

    # Pesos por região (NOVO)
    regiao_weights: Dict[str, int] = field(default_factory=lambda: {
        "sp": 10, "são paulo": 10, "sao paulo": 10,
        "rj": 8, "rio de janeiro": 8,
        "mg": 6, "minas gerais": 6, "belo horizonte": 6,
        "pr": 5, "curitiba": 5,
        "rs": 5, "porto alegre": 5,
        "sc": 5, "florianópolis": 5, "florianopolis": 5,
        "df": 7, "brasília": 7, "brasilia": 7,
    })

    # Multiplicadores de aprendizado
    cargo_multipliers: Dict[str, float] = field(default_factory=dict)
    setor_multipliers: Dict[str, float] = field(default_factory=dict)

    # Features que mais converteram
    high_value_indicators: List[str] = field(default_factory=list)

    # Configurações de scoring
    max_score: int = 100
    recency_bonus_days: int = 7
    recency_bonus_points: int = 5
    engagement_multiplier: float = 1.2


class DynamicScorer:
    """Sistema de scoring que aprende com conversões"""

    def __init__(self):
        self.weights = ScoringWeights()
        self._load_learned_weights()

    def _load_learned_weights(self):
        """Carrega pesos aprendidos do banco de dados"""
        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Analisar conversões por cargo
            cursor.execute('''
                SELECT cargo,
                       COUNT(*) as total,
                       SUM(CASE WHEN converted = true THEN 1 ELSE 0 END) as convertidos
                FROM prospects
                WHERE cargo IS NOT NULL AND cargo != ''
                GROUP BY cargo
                HAVING COUNT(*) >= 3
            ''')

            for row in cursor.fetchall():
                cargo = row['cargo']
                total = row['total']
                convertidos = row['convertidos']
                taxa = convertidos / total if total > 0 else 0
                # Ajustar multiplicador baseado na taxa de conversão
                if taxa > 0.3:
                    self.weights.cargo_multipliers[cargo.lower()] = 1.5
                elif taxa > 0.2:
                    self.weights.cargo_multipliers[cargo.lower()] = 1.2
                elif taxa < 0.05 and total >= 5:
                    self.weights.cargo_multipliers[cargo.lower()] = 0.7

            # Carregar high value indicators salvos
            cursor.execute('''
                SELECT DISTINCT cargo, empresa FROM prospects
                WHERE converted = true AND deal_value > 10000
            ''')
            for row in cursor.fetchall():
                cargo_words = (row['cargo'] or '').lower().split()
                empresa_words = (row['empresa'] or '').lower().split()
                for word in cargo_words + empresa_words:
                    if len(word) > 4 and word not in self.weights.high_value_indicators:
                        self.weights.high_value_indicators.append(word)

            conn.close()
        except:
            pass  # DB não existe ainda

    def _score_cargo_fuzzy(self, cargo: str) -> Tuple[int, str]:
        """
        Calcula score do cargo usando fuzzy matching.
        Retorna (score, keyword_matched)
        """
        if not cargo:
            return 0, ""

        cargo_normalized = normalize_text(expand_abbreviations(cargo))
        best_score = 0
        best_keyword = ""

        for keyword, points in self.weights.cargo_weights.items():
            # Match exato primeiro
            if keyword in cargo_normalized:
                if points > best_score:
                    best_score = points
                    best_keyword = keyword
            else:
                # Fuzzy match para variações
                similarity = fuzzy_match_score(cargo_normalized, keyword)
                if similarity >= 0.85:
                    adjusted_points = int(points * similarity)
                    if adjusted_points > best_score:
                        best_score = adjusted_points
                        best_keyword = keyword

        # Aplicar multiplicador aprendido
        if best_keyword in self.weights.cargo_multipliers:
            best_score = int(best_score * self.weights.cargo_multipliers[best_keyword])

        # Bonus para founders detectados via componentes
        components = extract_cargo_components(cargo)
        if components['founder'] and best_score < 28:
            best_score = max(best_score, 28)
            best_keyword = "founder (detectado)"

        return best_score, best_keyword

    def calculate_score(self, prospect: Dict) -> Tuple[int, Dict[str, int], List[str]]:
        """
        Calcula o score de um prospect com fuzzy matching e novos fatores.

        Returns:
            Tuple[score_total, breakdown, reasons]
        """
        score = 0
        breakdown = {}
        reasons = []

        cargo = prospect.get('cargo') or ''
        empresa = (prospect.get('empresa') or '').lower()
        combined = f"{normalize_text(cargo)} {empresa}"

        # 1. CARGO (0-30 pts) - COM FUZZY MATCHING
        cargo_score, matched_cargo = self._score_cargo_fuzzy(cargo)

        if cargo_score > 0:
            breakdown['cargo'] = cargo_score
            score += cargo_score
            reasons.append(f"Cargo executivo: {cargo} (+{cargo_score}pts)")

        # 2. SETOR (0-20 pts)
        setor_score = 0
        matched_setor = ""
        for keyword, points in self.weights.setor_weights.items():
            if keyword in combined:
                if points > setor_score:
                    setor_score = points
                    matched_setor = keyword

        if setor_score > 0:
            breakdown['setor'] = setor_score
            score += setor_score
            reasons.append(f"Setor estratégico: {matched_setor} (+{setor_score}pts)")

        # 3. GOVERNANÇA/IBGC (0-15 pts)
        ibgc_keywords = ["ibgc", "governança corporativa", "corporate governance", "conselho"]
        for kw in ibgc_keywords:
            if kw in combined:
                breakdown['governanca'] = 15
                score += 15
                reasons.append("Conexão com governança corporativa (+15pts)")
                break

        # 4. COMPLETUDE (0-15 pts)
        completeness = 0
        contact_items = []
        if prospect.get('email'):
            completeness += 7
            contact_items.append("email")
        if prospect.get('telefone'):
            completeness += 5
            contact_items.append("telefone")
        if prospect.get('empresa'):
            completeness += 3
            contact_items.append("empresa")

        if completeness > 0:
            breakdown['completude'] = completeness
            score += completeness
            reasons.append(f"Dados completos: {', '.join(contact_items)} (+{completeness}pts)")

        # 5. PME vs Grande Empresa
        pme_indicators = ["ltda", "eireli", "me ", "epp", "startup", "ventures"]
        large_corp = ["s.a.", " sa ", "bnp", "itaú", "bradesco", "santander", "microsoft", "google"]

        is_pme = any(ind in empresa for ind in pme_indicators)
        is_large = any(ind in empresa for ind in large_corp)

        if is_pme and not is_large:
            breakdown['pme'] = 10
            score += 10
            reasons.append("Indicador de PME (+10pts)")
        elif is_large:
            breakdown['grande_empresa'] = -5
            score -= 5
            reasons.append("Grande corporação - menor fit (-5pts)")

        # 6. PERFIL ESTRATÉGICO
        strategic = ["estratégia", "strategy", "decisão", "transformação", "inovação", "growth"]
        for kw in strategic:
            if kw in combined:
                breakdown['estrategico'] = 8
                score += 8
                reasons.append("Perfil estratégico identificado (+8pts)")
                break

        # 7. HIGH VALUE INDICATORS (aprendidos)
        for indicator in self.weights.high_value_indicators:
            if indicator.lower() in combined:
                breakdown['high_value'] = 10
                score += 10
                reasons.append(f"Indicador de alto valor: {indicator} (+10pts)")
                break

        # 8. ORIGEM DO LEAD (NOVO)
        origem = (prospect.get('origem') or '').lower()
        if origem:
            for keyword, points in self.weights.origem_weights.items():
                if keyword in origem:
                    breakdown['origem'] = points
                    score += points
                    reasons.append(f"Origem qualificada: {origem} (+{points}pts)")
                    break

        # 9. RECÊNCIA (NOVO) - Leads mais recentes ganham bonus
        data_criacao = prospect.get('data_criacao')
        if data_criacao:
            if isinstance(data_criacao, str):
                try:
                    data_criacao = datetime.fromisoformat(data_criacao.replace('Z', '+00:00'))
                except:
                    data_criacao = None
            if data_criacao:
                days_old = (datetime.now() - data_criacao.replace(tzinfo=None)).days
                if days_old <= self.weights.recency_bonus_days:
                    breakdown['recencia'] = self.weights.recency_bonus_points
                    score += self.weights.recency_bonus_points
                    reasons.append(f"Lead recente ({days_old} dias) (+{self.weights.recency_bonus_points}pts)")

        # 10. LINKEDIN (NOVO) - Ter perfil LinkedIn é positivo
        if prospect.get('linkedin'):
            breakdown['linkedin'] = 5
            score += 5
            reasons.append("Perfil LinkedIn disponível (+5pts)")

        # 11. DADOS ENRIQUECIDOS (NOVO)
        dados_enriquecidos = prospect.get('dados_enriquecidos')
        if dados_enriquecidos:
            if isinstance(dados_enriquecidos, str):
                try:
                    dados_enriquecidos = json.loads(dados_enriquecidos)
                except:
                    dados_enriquecidos = {}
            if dados_enriquecidos and len(dados_enriquecidos) > 0:
                breakdown['enriquecido'] = 5
                score += 5
                reasons.append("Dados enriquecidos disponíveis (+5pts)")

        return max(0, min(score, self.weights.max_score)), breakdown, reasons

    def determine_tier(self, score: int) -> str:
        """Determina o tier baseado no score"""
        if score >= 50:
            return "A"
        elif score >= 35:
            return "B"
        elif score >= 25:
            return "C"
        elif score >= 15:
            return "D"
        return "E"

    def update_weights_from_conversion(self, prospect: Dict, converted: bool, deal_value: float = 0):
        """
        Atualiza os pesos baseado em uma conversão (ou não conversão)
        """
        cargo = (prospect.get('cargo') or '').lower()
        empresa = (prospect.get('empresa') or '').lower()

        if converted:
            for keyword in self.weights.cargo_weights.keys():
                if keyword in cargo:
                    current = self.weights.cargo_multipliers.get(keyword, 1.0)
                    self.weights.cargo_multipliers[keyword] = min(2.0, current * 1.1)

            for keyword in self.weights.setor_weights.keys():
                if keyword in empresa:
                    current = self.weights.setor_multipliers.get(keyword, 1.0)
                    self.weights.setor_multipliers[keyword] = min(2.0, current * 1.1)

            if deal_value > 10000:
                words = set(cargo.split() + empresa.split())
                for word in words:
                    if len(word) > 4 and word not in self.weights.high_value_indicators:
                        self.weights.high_value_indicators.append(word)
                        break

    def recalculate_all_scores(self) -> Dict[str, Any]:
        """
        Recalcula os scores de todos os prospects.
        Útil após ajustes nos pesos ou novos aprendizados.

        Returns:
            Estatísticas do recálculo
        """
        stats = {
            "total_processados": 0,
            "scores_aumentados": 0,
            "scores_diminuidos": 0,
            "tiers_alterados": 0,
            "erros": 0,
            "tempo_execucao": 0
        }

        start_time = datetime.now()

        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Buscar todos os prospects
            cursor.execute('''
                SELECT id, nome, empresa, cargo, email, telefone, linkedin,
                       score as old_score, tier as old_tier, data_criacao,
                       dados_enriquecidos
                FROM prospects
            ''')

            prospects = cursor.fetchall()
            stats["total_processados"] = len(prospects)

            for row in prospects:
                try:
                    prospect_data = dict(row)
                    new_score, breakdown, reasons = self.calculate_score(prospect_data)
                    new_tier = self.determine_tier(new_score)

                    old_score = row['old_score'] or 0
                    old_tier = row['old_tier'] or 'E'

                    if new_score > old_score:
                        stats["scores_aumentados"] += 1
                    elif new_score < old_score:
                        stats["scores_diminuidos"] += 1

                    if new_tier != old_tier:
                        stats["tiers_alterados"] += 1

                    # Atualizar no banco
                    cursor.execute('''
                        UPDATE prospects
                        SET score = %s,
                            tier = %s,
                            score_breakdown = %s,
                            reasons = %s
                        WHERE id = %s
                    ''', (
                        new_score,
                        new_tier,
                        json.dumps(breakdown),
                        json.dumps(reasons),
                        row['id']
                    ))

                except Exception as e:
                    stats["erros"] += 1

            conn.commit()
            conn.close()

        except Exception as e:
            stats["error"] = str(e)

        stats["tempo_execucao"] = (datetime.now() - start_time).total_seconds()
        return stats

    def analyze_icp(self) -> Dict:
        """
        Analisa os dados para identificar o Perfil Ideal de Cliente.
        Versão aprimorada com mais insights acionáveis.
        """
        analysis = {
            "data_analise": datetime.now().isoformat(),
            "total_prospects": 0,
            "total_convertidos": 0,
            "taxa_conversao_geral": 0,
            "cargos_top_conversao": [],
            "setores_top_conversao": [],
            "origens_top_conversao": [],
            "objecoes_mais_comuns": [],
            "features_mais_valorizadas": [],
            "tempo_medio_conversao_dias": 0,
            "ticket_medio": 0,
            "taxa_conversao_por_tier": {},
            "distribuicao_tiers": {},
            "insights_acionaveis": [],
            "recomendacoes_icp": [],
            "score_medio_convertidos": 0,
            "score_medio_nao_convertidos": 0,
        }

        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Total de prospects e conversões
            cursor.execute('SELECT COUNT(*) as count FROM prospects')
            analysis["total_prospects"] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM prospects WHERE converted = true')
            analysis["total_convertidos"] = cursor.fetchone()['count']

            if analysis["total_prospects"] > 0:
                analysis["taxa_conversao_geral"] = round(
                    analysis["total_convertidos"] / analysis["total_prospects"] * 100, 2
                )

            # Score médio de convertidos vs não convertidos
            cursor.execute('''
                SELECT
                    AVG(CASE WHEN converted = true THEN score ELSE NULL END) as avg_convertidos,
                    AVG(CASE WHEN converted = false THEN score ELSE NULL END) as avg_nao_convertidos
                FROM prospects
            ''')
            row = cursor.fetchone()
            analysis["score_medio_convertidos"] = round(float(row['avg_convertidos'] or 0), 1)
            analysis["score_medio_nao_convertidos"] = round(float(row['avg_nao_convertidos'] or 0), 1)

            # Top cargos por conversão
            cursor.execute('''
                SELECT cargo,
                       COUNT(*) as total,
                       SUM(CASE WHEN converted = true THEN 1 ELSE 0 END) as convertidos,
                       ROUND(AVG(CASE WHEN converted = true THEN deal_value ELSE NULL END)::numeric, 2) as ticket_medio
                FROM prospects
                WHERE cargo IS NOT NULL AND cargo != ''
                GROUP BY cargo
                HAVING COUNT(*) >= 2
                ORDER BY (SUM(CASE WHEN converted = true THEN 1 ELSE 0 END)::float / COUNT(*)) DESC
                LIMIT 10
            ''')

            for row in cursor.fetchall():
                taxa = round(row['convertidos'] / row['total'] * 100, 1) if row['total'] > 0 else 0
                analysis["cargos_top_conversao"].append({
                    "cargo": row['cargo'],
                    "total": row['total'],
                    "convertidos": row['convertidos'],
                    "taxa_conversao": taxa,
                    "ticket_medio": float(row['ticket_medio']) if row['ticket_medio'] else 0
                })

            # Taxa de conversão por tier
            cursor.execute('''
                SELECT tier,
                       COUNT(*) as total,
                       SUM(CASE WHEN converted = true THEN 1 ELSE 0 END) as convertidos
                FROM prospects
                GROUP BY tier
                ORDER BY tier
            ''')

            for row in cursor.fetchall():
                tier = row['tier'] or 'E'
                total = row['total']
                convertidos = row['convertidos']
                analysis["taxa_conversao_por_tier"][tier] = round(
                    convertidos / total * 100, 1
                ) if total > 0 else 0
                analysis["distribuicao_tiers"][tier] = total

            # Tempo médio de conversão
            cursor.execute('''
                SELECT AVG(EXTRACT(EPOCH FROM (data_reuniao - data_criacao)) / 86400) as dias
                FROM prospects
                WHERE converted = true AND data_reuniao IS NOT NULL AND data_criacao IS NOT NULL
            ''')
            result = cursor.fetchone()
            if result and result['dias']:
                analysis["tempo_medio_conversao_dias"] = round(float(result['dias']), 1)

            # Objeções mais comuns
            cursor.execute('''
                SELECT objecoes FROM prospects
                WHERE objecoes IS NOT NULL AND objecoes != '[]'
            ''')

            objecao_count = {}
            for row in cursor.fetchall():
                try:
                    objecoes = json.loads(row['objecoes'])
                    for obj in objecoes:
                        objecao_count[obj] = objecao_count.get(obj, 0) + 1
                except:
                    pass

            analysis["objecoes_mais_comuns"] = sorted(
                [{"objecao": k, "frequencia": v} for k, v in objecao_count.items()],
                key=lambda x: x["frequencia"],
                reverse=True
            )[:10]

            # Features mais valorizadas
            cursor.execute('''
                SELECT interesse_features FROM prospects
                WHERE converted = true AND interesse_features IS NOT NULL AND interesse_features != '[]'
            ''')

            feature_count = {}
            for row in cursor.fetchall():
                try:
                    features = json.loads(row['interesse_features'])
                    for feat in features:
                        feature_count[feat] = feature_count.get(feat, 0) + 1
                except:
                    pass

            analysis["features_mais_valorizadas"] = sorted(
                [{"feature": k, "frequencia": v} for k, v in feature_count.items()],
                key=lambda x: x["frequencia"],
                reverse=True
            )[:10]

            # Ticket médio
            cursor.execute('''
                SELECT AVG(deal_value) as avg FROM prospects
                WHERE converted = true AND deal_value > 0
            ''')
            result = cursor.fetchone()
            analysis["ticket_medio"] = round(float(result['avg']), 2) if result['avg'] else 0

            # Gerar insights acionáveis
            analysis["insights_acionaveis"] = self._generate_actionable_insights(analysis)

            # Gerar recomendações de ICP
            analysis["recomendacoes_icp"] = self._generate_icp_recommendations(analysis)

            conn.close()

        except Exception as e:
            analysis["error"] = str(e)

        return analysis

    def _generate_actionable_insights(self, analysis: Dict) -> List[Dict[str, Any]]:
        """Gera insights acionáveis baseados na análise"""
        insights = []

        # Insight sobre diferença de score
        score_diff = analysis["score_medio_convertidos"] - analysis["score_medio_nao_convertidos"]
        if score_diff > 10:
            insights.append({
                "tipo": "validacao",
                "prioridade": "alta",
                "titulo": "Sistema de scoring está funcionando bem",
                "descricao": f"Clientes convertidos têm score médio {score_diff:.0f} pontos maior que não convertidos.",
                "acao": "Continue priorizando prospects de alto score."
            })
        elif score_diff < 5:
            insights.append({
                "tipo": "alerta",
                "prioridade": "alta",
                "titulo": "Scoring precisa de ajuste",
                "descricao": "A diferença de score entre convertidos e não convertidos é muito pequena.",
                "acao": "Revisar critérios de scoring e adicionar novos fatores discriminantes."
            })

        # Insight sobre tiers
        tier_a_taxa = analysis["taxa_conversao_por_tier"].get("A", 0)
        tier_b_taxa = analysis["taxa_conversao_por_tier"].get("B", 0)
        tier_c_taxa = analysis["taxa_conversao_por_tier"].get("C", 0)

        if tier_a_taxa > 0 and tier_a_taxa < 25:
            insights.append({
                "tipo": "alerta",
                "prioridade": "alta",
                "titulo": f"Tier A convertendo apenas {tier_a_taxa}%",
                "descricao": "Os melhores prospects não estão convertendo como esperado.",
                "acao": "Investigar objeções comuns e ajustar abordagem de vendas."
            })

        if tier_c_taxa > tier_a_taxa * 0.5:
            insights.append({
                "tipo": "oportunidade",
                "prioridade": "media",
                "titulo": f"Tier C tem boa conversão ({tier_c_taxa}%)",
                "descricao": "Prospects de tier médio estão convertendo bem.",
                "acao": "Considerar aumentar investimento em prospects Tier C."
            })

        # Insight sobre tempo de conversão
        if analysis["tempo_medio_conversao_dias"] > 30:
            insights.append({
                "tipo": "otimizacao",
                "prioridade": "media",
                "titulo": f"Ciclo de vendas longo ({analysis['tempo_medio_conversao_dias']:.0f} dias)",
                "descricao": "O tempo médio até conversão está alto.",
                "acao": "Implementar cadência de follow-up mais agressiva."
            })

        # Insight sobre objeções
        if analysis["objecoes_mais_comuns"]:
            top_objecao = analysis["objecoes_mais_comuns"][0]
            insights.append({
                "tipo": "preparacao",
                "prioridade": "alta",
                "titulo": f"Objeção frequente: '{top_objecao['objecao']}'",
                "descricao": f"Esta objeção apareceu {top_objecao['frequencia']} vezes.",
                "acao": "Desenvolver script específico para contornar esta objeção."
            })

        return insights

    def _generate_icp_recommendations(self, analysis: Dict) -> List[str]:
        """Gera recomendações baseadas na análise"""
        recs = []

        # Baseado em cargos
        if analysis["cargos_top_conversao"]:
            top_cargo = analysis["cargos_top_conversao"][0]
            if top_cargo["taxa_conversao"] > 20:
                recs.append(
                    f"PRIORIZAR: Prospects com cargo '{top_cargo['cargo']}' "
                    f"têm taxa de conversão de {top_cargo['taxa_conversao']}%"
                )

        # Baseado em tiers
        if analysis.get("taxa_conversao_por_tier"):
            for tier, taxa in analysis["taxa_conversao_por_tier"].items():
                if tier == "A" and taxa < 30:
                    recs.append(
                        f"REVISAR CRITÉRIOS: Tier A está convertendo apenas {taxa}%. "
                        "Considere ajustar os pesos do scoring."
                    )
                if tier in ["C", "D"] and taxa > 15:
                    recs.append(
                        f"OPORTUNIDADE: Tier {tier} está convertendo {taxa}%. "
                        "Considere dar mais atenção a este segmento."
                    )

        # Baseado em objeções
        if analysis["objecoes_mais_comuns"]:
            top_objecao = analysis["objecoes_mais_comuns"][0]
            recs.append(
                f"PREPARAR RESPOSTA: '{top_objecao['objecao']}' é a objeção mais comum "
                f"({top_objecao['frequencia']} vezes). Desenvolver argumentação específica."
            )

        # Baseado em features
        if analysis["features_mais_valorizadas"]:
            top_feature = analysis["features_mais_valorizadas"][0]
            recs.append(
                f"DESTACAR NA ABORDAGEM: Feature '{top_feature['feature']}' é mais valorizada "
                f"por clientes convertidos ({top_feature['frequencia']} menções)"
            )

        # Recomendação de score mínimo
        if analysis["score_medio_convertidos"] > 0:
            score_minimo_sugerido = int(analysis["score_medio_convertidos"] * 0.7)
            recs.append(
                f"FILTRO SUGERIDO: Considere focar em prospects com score >= {score_minimo_sugerido} "
                f"(70% do score médio de convertidos)"
            )

        return recs

    def generate_sales_arguments(self) -> List[Dict]:
        """Gera argumentos de venda otimizados baseados em dados"""
        arguments = []

        try:
            conn = get_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT argumento, categoria, efetividade_score, vezes_usado, vezes_converteu
                FROM sales_arguments
                ORDER BY efetividade_score DESC
                LIMIT 20
            ''')

            for row in cursor.fetchall():
                arguments.append({
                    "argumento": row['argumento'],
                    "categoria": row['categoria'],
                    "efetividade": row['efetividade_score'],
                    "vezes_usado": row['vezes_usado'],
                    "taxa_conversao": round(row['vezes_converteu'] / row['vezes_usado'] * 100, 1) if row['vezes_usado'] > 0 else 0
                })

            conn.close()

        except:
            # Retornar argumentos base se não houver dados
            arguments = [
                {
                    "argumento": "Decisões estratégicas em 48 horas com combinação de IA + expertise humana",
                    "categoria": "proposta_valor",
                    "efetividade": 0,
                    "vezes_usado": 0
                },
                {
                    "argumento": "Diagnóstico rápido que começa em minutos com dados mínimos",
                    "categoria": "facilidade",
                    "efetividade": 0,
                    "vezes_usado": 0
                },
                {
                    "argumento": "Para quando há risco real, pouco tempo e mais de um caminho possível",
                    "categoria": "dor",
                    "efetividade": 0,
                    "vezes_usado": 0
                },
                {
                    "argumento": "Análise enriquecida com dados públicos via CNPJ",
                    "categoria": "feature",
                    "efetividade": 0,
                    "vezes_usado": 0
                }
            ]

        return arguments

    def get_scoring_stats(self) -> Dict[str, Any]:
        """
        Retorna estatísticas sobre o sistema de scoring atual.
        """
        stats = {
            "total_cargo_weights": len(self.weights.cargo_weights),
            "total_setor_weights": len(self.weights.setor_weights),
            "total_origem_weights": len(self.weights.origem_weights),
            "cargo_multipliers_learned": len(self.weights.cargo_multipliers),
            "high_value_indicators": len(self.weights.high_value_indicators),
            "high_value_list": self.weights.high_value_indicators[:10],
            "top_cargo_multipliers": dict(sorted(
                self.weights.cargo_multipliers.items(),
                key=lambda x: x[1],
                reverse=True
            )[:5])
        }
        return stats
