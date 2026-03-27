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
        circulo_max: int = 3,
        limit: int = 50
    ) -> List[Dict]:
        """Retorna contatos em risco de queda de health"""
        contacts_at_risk = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Contatos importantes com health abaixo do threshold ou sem contato recente
            cursor.execute("""
                SELECT id, nome, empresa, circulo, health_score, ultimo_contato,
                       EXTRACT(DAY FROM NOW() - ultimo_contato)::int as dias_sem_contato
                FROM contacts
                WHERE COALESCE(circulo, 5) <= %s
                AND (
                    COALESCE(health_score, 50) < %s
                    OR (circulo = 1 AND ultimo_contato < NOW() - INTERVAL '30 days')
                    OR (circulo = 2 AND ultimo_contato < NOW() - INTERVAL '45 days')
                    OR (circulo = 3 AND ultimo_contato < NOW() - INTERVAL '60 days')
                )
                ORDER BY circulo ASC, health_score ASC NULLS LAST
                LIMIT %s
            """, (circulo_max, threshold, limit))

            for row in cursor.fetchall():
                contact = dict(row)

                # Calcular nivel de risco
                health = contact["health_score"] or 50
                dias = contact["dias_sem_contato"] or 0
                circulo = contact["circulo"] or 5

                risk_score = 0

                # Fator health
                if health < 20:
                    risk_score += 40
                elif health < 30:
                    risk_score += 30
                elif health < 40:
                    risk_score += 20

                # Fator tempo sem contato
                if circulo == 1:
                    risk_score += min(30, dias // 10 * 5)
                elif circulo == 2:
                    risk_score += min(25, dias // 15 * 5)
                else:
                    risk_score += min(20, dias // 20 * 5)

                # Fator circulo (mais importante = mais risco)
                risk_score += (4 - circulo) * 5

                contact["risk_score"] = min(100, risk_score)
                contact["risk_level"] = (
                    "critical" if risk_score >= 70 else
                    "high" if risk_score >= 50 else
                    "medium" if risk_score >= 30 else
                    "low"
                )

                contacts_at_risk.append(contact)

            # Ordenar por risk_score
            contacts_at_risk.sort(key=lambda x: x["risk_score"], reverse=True)

        return contacts_at_risk

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
