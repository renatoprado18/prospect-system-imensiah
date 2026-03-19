"""
Sistema de Scoring Dinâmico para Prospects ImensIAH

Este sistema aprende com os resultados das reuniões e conversões
para melhorar continuamente a qualificação de prospects.
"""
import json
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from datetime import datetime
import re

from database import get_connection

@dataclass
class ScoringWeights:
    """Pesos dinâmicos baseados em aprendizado"""

    # Pesos base (ajustados pelo sistema)
    cargo_weights: Dict[str, int] = field(default_factory=lambda: {
        # Tier 1 - Decision Makers
        "ceo": 30, "chief executive officer": 30, "presidente": 30,
        "fundador": 30, "founder": 30, "co-founder": 30, "cofundador": 30,
        "sócio": 28, "socio": 28, "owner": 28, "proprietário": 28,
        "sócio proprietário": 30, "sócio-proprietário": 30,
        "dono": 28, "acionista": 25,

        # Tier 2 - C-Level & Directors
        "cfo": 25, "coo": 25, "cto": 25, "cmo": 25, "cio": 25,
        "chief": 25, "c-level": 25,
        "diretor": 24, "director": 24, "diretora": 24,
        "diretor geral": 26, "diretor executivo": 26,
        "managing director": 26, "diretor comercial": 24,
        "vice presidente": 24, "vice-presidente": 24, "vp": 24,

        # Tier 3 - Board & Advisory
        "conselheiro": 22, "conselheira": 22, "board member": 22,
        "conselheiro consultivo": 24, "conselheira consultiva": 24,
        "advisor": 20, "consultor": 18, "consultora": 18,

        # Tier 4 - Senior Management
        "gerente geral": 18, "general manager": 18,
        "gerente": 15, "manager": 15, "head": 16,
        "superintendente": 17, "coordenador": 12, "coordenadora": 12,

        # Tier 5 - Outros
        "partner": 20, "empreendedor": 18, "entrepreneur": 18,
        "investidor": 16, "investor": 16,
    })

    setor_weights: Dict[str, int] = field(default_factory=lambda: {
        "consultoria": 20, "consulting": 20, "advisory": 20,
        "estratégia": 20, "strategy": 20,
        "governança": 20, "governance": 20,
        "finanças": 18, "financeiro": 18, "finance": 18,
        "banco": 15, "bank": 15, "investment": 16,
        "tecnologia": 15, "tech": 15, "technology": 15, "software": 15,
        "startup": 18, "ventures": 16, "capital": 16,
        "indústria": 12, "industria": 12, "manufacturing": 12,
        "varejo": 12, "retail": 12, "comércio": 12,
        "serviços": 10, "services": 10,
        "energia": 14, "energy": 14,
        "saúde": 14, "health": 14,
    })

    # Multiplicadores de aprendizado
    cargo_multipliers: Dict[str, float] = field(default_factory=dict)
    setor_multipliers: Dict[str, float] = field(default_factory=dict)

    # Features que mais converteram
    high_value_indicators: List[str] = field(default_factory=list)

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

            conn.close()
        except:
            pass  # DB não existe ainda

    def calculate_score(self, prospect: Dict) -> Tuple[int, Dict[str, int], List[str]]:
        """
        Calcula o score de um prospect

        Returns:
            Tuple[score_total, breakdown, reasons]
        """
        score = 0
        breakdown = {}
        reasons = []

        cargo = (prospect.get('cargo') or '').lower()
        empresa = (prospect.get('empresa') or '').lower()
        combined = f"{cargo} {empresa}"

        # 1. CARGO (0-30 pts)
        cargo_score = 0
        matched_cargo = ""
        for keyword, points in self.weights.cargo_weights.items():
            if keyword in cargo:
                if points > cargo_score:
                    cargo_score = points
                    matched_cargo = keyword

        # Aplicar multiplicador aprendido
        if matched_cargo in self.weights.cargo_multipliers:
            cargo_score = int(cargo_score * self.weights.cargo_multipliers[matched_cargo])

        if cargo_score > 0:
            breakdown['cargo'] = cargo_score
            score += cargo_score
            reasons.append(f"Cargo executivo: {prospect.get('cargo', '')} (+{cargo_score}pts)")

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

        return max(0, score), breakdown, reasons

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

        Este é o coração do sistema de aprendizado
        """
        cargo = (prospect.get('cargo') or '').lower()
        empresa = (prospect.get('empresa') or '').lower()

        # Se converteu, aumentar peso do cargo/setor
        if converted:
            # Encontrar keywords que matcharam
            for keyword in self.weights.cargo_weights.keys():
                if keyword in cargo:
                    current = self.weights.cargo_multipliers.get(keyword, 1.0)
                    # Aumentar em 10%
                    self.weights.cargo_multipliers[keyword] = min(2.0, current * 1.1)

            for keyword in self.weights.setor_weights.keys():
                if keyword in empresa:
                    current = self.weights.setor_multipliers.get(keyword, 1.0)
                    self.weights.setor_multipliers[keyword] = min(2.0, current * 1.1)

            # Se deal value alto, adicionar como high value indicator
            if deal_value > 10000:  # Ajustar threshold conforme necessário
                # Extrair palavras-chave únicas
                words = set(cargo.split() + empresa.split())
                for word in words:
                    if len(word) > 4 and word not in self.weights.high_value_indicators:
                        self.weights.high_value_indicators.append(word)
                        break

    def analyze_icp(self) -> Dict:
        """
        Analisa os dados para identificar o Perfil Ideal de Cliente

        Returns:
            Análise completa do ICP baseada em dados reais
        """
        analysis = {
            "data_analise": datetime.now().isoformat(),
            "total_prospects": 0,
            "total_convertidos": 0,
            "taxa_conversao_geral": 0,
            "cargos_top_conversao": [],
            "setores_top_conversao": [],
            "objecoes_mais_comuns": [],
            "features_mais_valorizadas": [],
            "argumentos_efetivos": [],
            "ticket_medio": 0,
            "recomendacoes_icp": []
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

            # Top cargos por conversão
            cursor.execute('''
                SELECT cargo,
                       COUNT(*) as total,
                       SUM(CASE WHEN converted = true THEN 1 ELSE 0 END) as convertidos,
                       ROUND(AVG(CASE WHEN converted = true THEN deal_value ELSE 0 END)::numeric, 2) as ticket_medio
                FROM prospects
                WHERE cargo IS NOT NULL AND cargo != ''
                GROUP BY cargo
                HAVING COUNT(*) >= 2
                ORDER BY (SUM(CASE WHEN converted = true THEN 1 ELSE 0 END)::float / COUNT(*)) DESC
                LIMIT 10
            ''')

            for row in cursor.fetchall():
                analysis["cargos_top_conversao"].append({
                    "cargo": row['cargo'],
                    "total": row['total'],
                    "convertidos": row['convertidos'],
                    "taxa_conversao": round(row['convertidos'] / row['total'] * 100, 1) if row['total'] > 0 else 0,
                    "ticket_medio": float(row['ticket_medio']) if row['ticket_medio'] else 0
                })

            # Taxa de conversão por tier
            cursor.execute('''
                SELECT tier,
                       COUNT(*) as total,
                       SUM(CASE WHEN converted = true THEN 1 ELSE 0 END) as convertidos
                FROM prospects
                GROUP BY tier
            ''')

            taxa_por_tier = {}
            for row in cursor.fetchall():
                taxa_por_tier[row['tier']] = round(row['convertidos'] / row['total'] * 100, 1) if row['total'] > 0 else 0

            analysis["taxa_conversao_por_tier"] = taxa_por_tier

            # Objeções mais comuns
            cursor.execute('''
                SELECT objecoes FROM prospects
                WHERE objecoes IS NOT NULL AND objecoes != '[]'
            ''')

            objecao_count = {}
            for row in cursor.fetchall():
                objecoes = json.loads(row['objecoes'])
                for obj in objecoes:
                    objecao_count[obj] = objecao_count.get(obj, 0) + 1

            analysis["objecoes_mais_comuns"] = sorted(
                [{"objecao": k, "frequencia": v} for k, v in objecao_count.items()],
                key=lambda x: x["frequencia"],
                reverse=True
            )[:10]

            # Features mais valorizadas (de prospects convertidos)
            cursor.execute('''
                SELECT interesse_features FROM prospects
                WHERE converted = true AND interesse_features IS NOT NULL AND interesse_features != '[]'
            ''')

            feature_count = {}
            for row in cursor.fetchall():
                features = json.loads(row['interesse_features'])
                for feat in features:
                    feature_count[feat] = feature_count.get(feat, 0) + 1

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

            # Gerar recomendações de ICP
            analysis["recomendacoes_icp"] = self._generate_icp_recommendations(analysis)

            conn.close()

        except Exception as e:
            analysis["error"] = str(e)

        return analysis

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
                        "Considere promover prospects com perfil similar."
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

        return recs

    def generate_sales_arguments(self) -> List[Dict]:
        """
        Gera argumentos de venda otimizados baseados em dados

        Returns:
            Lista de argumentos com sua efetividade
        """
        arguments = []

        try:
            conn = get_connection()
            cursor = conn.cursor()

            # Buscar argumentos com melhor conversão
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
