"""
Health Predictions Service - Previsao de saude de relacionamentos

Analisa tendencias e preve health futuro:
- Identifica contatos em risco
- Sugere acoes preventivas
- Acompanha acuracia das previsoes
"""
import json
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from database import get_db


class HealthPredictionsService:
    def predict_health(
        self,
        contact_id: int,
        dias_previsao: int = 30
    ) -> Dict:
        """Preve health score futuro de um contato"""
        with get_db() as conn:
            cursor = conn.cursor()

            # Obter dados atuais
            cursor.execute("""
                SELECT id, nome, circulo, health_score, ultimo_contato,
                       total_interacoes, criado_em
                FROM contacts
                WHERE id = %s
            """, (contact_id,))

            contact = cursor.fetchone()
            if not contact:
                return {"error": "Contato nao encontrado"}

            contact = dict(contact)
            health_atual = contact["health_score"] or 50
            circulo = contact["circulo"] or 5
            ultimo_contato = contact["ultimo_contato"]

            # Calcular fatores
            fatores = []
            score_ajuste = 0

            # Fator: Dias sem contato
            if ultimo_contato:
                dias_sem_contato = (datetime.now() - ultimo_contato).days
                dias_futuro = dias_sem_contato + dias_previsao

                # Decay por tempo sem contato
                if circulo == 1 and dias_futuro > 30:
                    decay = min(20, (dias_futuro - 30) * 0.5)
                    score_ajuste -= decay
                    fatores.append({
                        "tipo": "tempo_sem_contato",
                        "descricao": f"Sem contato ha {dias_futuro} dias (C1 precisa de contato a cada 30 dias)",
                        "impacto": -decay
                    })
                elif circulo == 2 and dias_futuro > 45:
                    decay = min(15, (dias_futuro - 45) * 0.4)
                    score_ajuste -= decay
                    fatores.append({
                        "tipo": "tempo_sem_contato",
                        "descricao": f"Sem contato ha {dias_futuro} dias (C2 precisa de contato a cada 45 dias)",
                        "impacto": -decay
                    })
                elif circulo == 3 and dias_futuro > 60:
                    decay = min(10, (dias_futuro - 60) * 0.3)
                    score_ajuste -= decay
                    fatores.append({
                        "tipo": "tempo_sem_contato",
                        "descricao": f"Sem contato ha {dias_futuro} dias",
                        "impacto": -decay
                    })

            # Fator: Baixo numero de interacoes
            if contact["total_interacoes"] < 5:
                score_ajuste -= 5
                fatores.append({
                    "tipo": "poucas_interacoes",
                    "descricao": f"Apenas {contact['total_interacoes']} interacoes registradas",
                    "impacto": -5
                })

            # Fator: Health ja baixo
            if health_atual < 40:
                score_ajuste -= 5
                fatores.append({
                    "tipo": "health_baixo",
                    "descricao": f"Health atual ja esta baixo ({health_atual}%)",
                    "impacto": -5
                })

            # Calcular health previsto
            health_previsto = max(0, min(100, health_atual + score_ajuste))

            # Determinar tendencia
            if score_ajuste > 5:
                tendencia = "improving"
            elif score_ajuste < -5:
                tendencia = "declining"
            else:
                tendencia = "stable"

            # Gerar recomendacoes
            recomendacoes = []
            if health_previsto < health_atual:
                if "tempo_sem_contato" in [f["tipo"] for f in fatores]:
                    recomendacoes.append({
                        "acao": "reconnect",
                        "descricao": f"Entre em contato com {contact['nome']} nas proximas semanas",
                        "urgencia": "alta" if health_previsto < 30 else "media"
                    })
                if "poucas_interacoes" in [f["tipo"] for f in fatores]:
                    recomendacoes.append({
                        "acao": "increase_engagement",
                        "descricao": "Aumente a frequencia de interacoes",
                        "urgencia": "media"
                    })

            # Salvar previsao
            cursor.execute("""
                INSERT INTO health_predictions
                (contact_id, health_atual, health_previsto, tendencia,
                 dias_previsao, fatores, recomendacoes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                contact_id,
                health_atual,
                health_previsto,
                tendencia,
                dias_previsao,
                json.dumps(fatores),
                json.dumps(recomendacoes)
            ))

            prediction_id = cursor.fetchone()["id"]
            conn.commit()

            return {
                "id": prediction_id,
                "contact_id": contact_id,
                "contact_name": contact["nome"],
                "health_atual": health_atual,
                "health_previsto": health_previsto,
                "tendencia": tendencia,
                "dias_previsao": dias_previsao,
                "fatores": fatores,
                "recomendacoes": recomendacoes
            }

    def get_at_risk_contacts(
        self,
        threshold: int = 40,
        circulo_max: int = 4,
        limit: int = 50
    ) -> List[Dict]:
        """
        Retorna contatos priorizados por importância e urgência.

        Sistema de scoring:
        - Peso do círculo: C1=5x, C2=4x, C3=3x, C4=2x, C5=1x
        - Urgência de health: quanto menor, mais urgente
        - Dias sem contato: acima do limite do círculo aumenta urgência
        - Bônus: aniversário próximo, etc.
        """
        contacts_at_risk = []

        # Pesos por círculo (importância do relacionamento)
        CIRCULO_WEIGHT = {1: 100, 2: 80, 3: 60, 4: 40, 5: 20}

        # Limites de dias sem contato por círculo
        DIAS_LIMITE = {1: 30, 2: 45, 3: 60, 4: 90, 5: 180}

        with get_db() as conn:
            cursor = conn.cursor()

            # Buscar contatos com dados relevantes incluindo aniversário
            cursor.execute("""
                SELECT
                    id, nome, empresa, cargo, circulo, health_score, ultimo_contato,
                    COALESCE(EXTRACT(DAY FROM NOW() - ultimo_contato)::int, 999) as dias_sem_contato,
                    aniversario,
                    CASE
                        WHEN aniversario IS NOT NULL THEN
                            CASE
                                WHEN TO_CHAR(aniversario, 'MM-DD') >= TO_CHAR(NOW(), 'MM-DD')
                                THEN TO_DATE(TO_CHAR(NOW(), 'YYYY') || '-' || TO_CHAR(aniversario, 'MM-DD'), 'YYYY-MM-DD') - CURRENT_DATE
                                ELSE TO_DATE(TO_CHAR(NOW(), 'YYYY')::int + 1 || '-' || TO_CHAR(aniversario, 'MM-DD'), 'YYYY-MM-DD') - CURRENT_DATE
                            END
                        ELSE 999
                    END as dias_ate_aniversario
                FROM contacts
                WHERE COALESCE(circulo, 5) <= %s
                AND (
                    COALESCE(health_score, 50) < %s
                    OR (circulo = 1 AND (ultimo_contato IS NULL OR ultimo_contato < NOW() - INTERVAL '30 days'))
                    OR (circulo = 2 AND (ultimo_contato IS NULL OR ultimo_contato < NOW() - INTERVAL '45 days'))
                    OR (circulo = 3 AND (ultimo_contato IS NULL OR ultimo_contato < NOW() - INTERVAL '60 days'))
                    OR (circulo = 4 AND (ultimo_contato IS NULL OR ultimo_contato < NOW() - INTERVAL '90 days'))
                )
                LIMIT 200
            """, (circulo_max, threshold))

            for row in cursor.fetchall():
                contact = dict(row)

                health = contact["health_score"] or 50
                dias = contact["dias_sem_contato"] or 0
                circulo = contact["circulo"] or 5
                dias_aniv = contact.get("dias_ate_aniversario") or 999

                # === NOVO SISTEMA DE SCORING ===

                # 1. Base: peso do círculo (0-100)
                base_score = CIRCULO_WEIGHT.get(circulo, 20)

                # 2. Fator de urgência de health (multiplicador 1.0 a 2.5)
                # Health 0 = 2.5x, Health 50 = 1.0x
                health_factor = 1.0 + (1.5 * (50 - min(health, 50)) / 50)

                # 3. Fator de dias sem contato (multiplicador 1.0 a 2.0)
                dias_limite = DIAS_LIMITE.get(circulo, 90)
                if dias > dias_limite:
                    days_factor = 1.0 + min(1.0, (dias - dias_limite) / dias_limite)
                else:
                    days_factor = 1.0

                # 4. Bônus especiais
                bonus = 0
                motivos = []

                # Aniversário em até 7 dias
                if dias_aniv <= 7:
                    bonus += 50
                    motivos.append(f"aniversário em {dias_aniv} dias" if dias_aniv > 0 else "aniversário HOJE!")

                # Health crítico (< 20)
                if health < 20:
                    bonus += 30
                    motivos.append("health crítico")
                elif health < 30:
                    motivos.append("health baixo")

                # Muito tempo sem contato
                if dias > dias_limite * 1.5:
                    bonus += 20
                    motivos.append(f"{dias} dias sem contato")
                elif dias > dias_limite:
                    motivos.append(f"{dias} dias sem contato")

                # C1/C2 sempre tem motivo especial
                if circulo <= 2 and not motivos:
                    motivos.append("relacionamento importante")

                # === SCORE FINAL ===
                priority_score = (base_score * health_factor * days_factor) + bonus

                # Normalizar para 0-100
                priority_score = min(100, priority_score / 3)

                # Determinar categoria
                if circulo <= 2 and (health < 30 or dias > dias_limite * 1.5):
                    category = "urgent"  # 🔴 URGENTE
                elif circulo <= 3 and (health < 40 or dias > dias_limite):
                    category = "important"  # 🟠 IMPORTANTE
                else:
                    category = "attention"  # 🟡 ATENÇÃO

                # Motivo principal para exibição
                if not motivos:
                    motivos.append("precisa atenção")

                contact["priority_score"] = round(priority_score, 1)
                contact["risk_score"] = round(priority_score, 1)  # Compatibilidade
                contact["risk_level"] = category
                contact["category"] = category
                contact["motivo_risco"] = motivos[0]
                contact["motivos"] = motivos

                contacts_at_risk.append(contact)

            # Ordenar por priority_score (maior = mais urgente)
            contacts_at_risk.sort(key=lambda x: x["priority_score"], reverse=True)

        return contacts_at_risk[:limit]

    def run_batch_predictions(
        self,
        circulo_max: int = 3,
        dias_previsao: int = 30,
        limit: int = 100
    ) -> Dict:
        """Executa previsoes em batch para contatos importantes"""
        results = {
            "started_at": datetime.now().isoformat(),
            "predictions": 0,
            "at_risk": 0,
            "errors": 0
        }

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id FROM contacts
                WHERE COALESCE(circulo, 5) <= %s
                ORDER BY circulo ASC, health_score ASC NULLS LAST
                LIMIT %s
            """, (circulo_max, limit))

            contact_ids = [row["id"] for row in cursor.fetchall()]

        for contact_id in contact_ids:
            try:
                prediction = self.predict_health(contact_id, dias_previsao)
                if "error" not in prediction:
                    results["predictions"] += 1
                    if prediction["tendencia"] == "declining":
                        results["at_risk"] += 1
                else:
                    results["errors"] += 1
            except Exception as e:
                results["errors"] += 1
                print(f"Error predicting for contact {contact_id}: {e}")

        results["completed_at"] = datetime.now().isoformat()
        return results

    def verify_past_predictions(self, days_back: int = 30) -> Dict:
        """Verifica acuracia de previsoes passadas"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT p.*, c.health_score as health_real
                FROM health_predictions p
                JOIN contacts c ON c.id = p.contact_id
                WHERE p.data_previsao < NOW() - INTERVAL '%s days'
                AND p.acerto IS NULL
            """, (days_back,))

            predictions = cursor.fetchall()
            verified = 0
            correct = 0

            for pred in predictions:
                health_real = pred["health_real"] or 50
                health_previsto = pred["health_previsto"]

                # Considerar acerto se diferenca for menor que 15 pontos
                acerto = abs(health_real - health_previsto) < 15

                cursor.execute("""
                    UPDATE health_predictions
                    SET acerto = %s
                    WHERE id = %s
                """, (acerto, pred["id"]))

                verified += 1
                if acerto:
                    correct += 1

            conn.commit()

            accuracy = round(correct / verified * 100, 1) if verified > 0 else 0

            return {
                "verified": verified,
                "correct": correct,
                "accuracy": accuracy
            }

    def get_prediction_history(
        self,
        contact_id: int,
        limit: int = 10
    ) -> List[Dict]:
        """Historico de previsoes para um contato"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT *
                FROM health_predictions
                WHERE contact_id = %s
                ORDER BY data_previsao DESC
                LIMIT %s
            """, (contact_id, limit))

            predictions = []
            for row in cursor.fetchall():
                p = dict(row)
                if p.get("data_previsao"):
                    p["data_previsao"] = p["data_previsao"].isoformat()
                predictions.append(p)

            return predictions


_health_predictions = None


def get_health_predictions() -> HealthPredictionsService:
    global _health_predictions
    if _health_predictions is None:
        _health_predictions = HealthPredictionsService()
    return _health_predictions
