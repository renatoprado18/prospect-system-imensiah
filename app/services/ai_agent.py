"""
AI Agent Service - Geracao automatica de sugestoes

Gera sugestoes inteligentes:
- Reconnect: Contatos que precisam de reconexao
- Birthday: Lembretes de aniversario
- Followup: Follow-ups pendentes
- Health: Contatos com health baixo
"""
import os
import json
import httpx
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from database import get_db

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


class AIAgentService:
    def __init__(self):
        self.api_key = ANTHROPIC_API_KEY

    async def call_claude(self, prompt: str, max_tokens: int = 1000) -> str:
        """Chama API do Claude para gerar sugestoes"""
        if not self.api_key:
            return None

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}]
                    },
                    timeout=30.0
                )
                if response.status_code == 200:
                    data = response.json()
                    return data["content"][0]["text"]
        except Exception as e:
            print(f"Claude API error: {e}")
        return None

    def generate_reconnect_suggestions(self, limit: int = 20) -> List[Dict]:
        """Gera sugestoes de reconexao para contatos importantes sem contato recente"""
        suggestions = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Contatos C1-C3 sem contato ha mais de 30/60/90 dias
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, ultimo_contato, health_score,
                       EXTRACT(DAY FROM NOW() - ultimo_contato)::int as dias_sem_contato
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                AND ultimo_contato IS NOT NULL
                AND ultimo_contato < NOW() - INTERVAL '30 days'
                AND id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'reconnect'
                    AND status = 'pending'
                    AND criado_em > NOW() - INTERVAL '7 days'
                )
                ORDER BY circulo ASC, ultimo_contato ASC
                LIMIT %s
            """, (limit,))

            contacts = cursor.fetchall()

            for contact in contacts:
                dias = contact["dias_sem_contato"] or 0
                circulo = contact["circulo"] or 5

                # Definir prioridade baseado no circulo e dias
                if circulo == 1:
                    prioridade = 9 if dias > 30 else 7
                elif circulo == 2:
                    prioridade = 8 if dias > 45 else 6
                else:
                    prioridade = 7 if dias > 60 else 5

                suggestion = {
                    "contact_id": contact["id"],
                    "tipo": "reconnect",
                    "titulo": f"Reconectar com {contact['nome']}",
                    "descricao": f"Faz {dias} dias sem contato. {contact['empresa'] or ''} - {contact['cargo'] or ''}".strip(" -"),
                    "razao": f"Contato C{circulo} sem interacao ha {dias} dias. Health: {contact['health_score'] or 'N/A'}%",
                    "dados": {
                        "dias_sem_contato": dias,
                        "circulo": circulo,
                        "health_score": contact["health_score"],
                        "empresa": contact["empresa"],
                        "cargo": contact["cargo"]
                    },
                    "prioridade": prioridade,
                    "confianca": 0.9
                }
                suggestions.append(suggestion)

        return suggestions

    def generate_birthday_suggestions(self, days_ahead: int = 7) -> List[Dict]:
        """Gera lembretes de aniversario para os proximos N dias"""
        suggestions = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Aniversarios nos proximos N dias
            cursor.execute("""
                WITH aniv_calc AS (
                    SELECT
                        id, nome, empresa, circulo, aniversario, foto_url,
                        CASE
                            WHEN EXTRACT(DOY FROM aniversario::date) >= EXTRACT(DOY FROM CURRENT_DATE)
                            THEN EXTRACT(DOY FROM aniversario::date) - EXTRACT(DOY FROM CURRENT_DATE)
                            ELSE 365 + EXTRACT(DOY FROM aniversario::date) - EXTRACT(DOY FROM CURRENT_DATE)
                        END as dias_ate
                    FROM contacts
                    WHERE aniversario IS NOT NULL
                      AND COALESCE(circulo, 5) <= 4
                )
                SELECT * FROM aniv_calc
                WHERE dias_ate >= 0 AND dias_ate <= %s
                AND id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'birthday'
                    AND status = 'pending'
                    AND criado_em > NOW() - INTERVAL '30 days'
                )
                ORDER BY dias_ate
            """, (days_ahead,))

            contacts = cursor.fetchall()

            for contact in contacts:
                dias = int(contact["dias_ate"])
                circulo = contact["circulo"] or 5

                if dias == 0:
                    titulo = f"HOJE: Aniversario de {contact['nome']}!"
                    prioridade = 10
                elif dias == 1:
                    titulo = f"AMANHA: Aniversario de {contact['nome']}"
                    prioridade = 9
                else:
                    titulo = f"Em {dias} dias: Aniversario de {contact['nome']}"
                    prioridade = 8 if dias <= 3 else 6

                suggestion = {
                    "contact_id": contact["id"],
                    "tipo": "birthday",
                    "titulo": titulo,
                    "descricao": f"Aniversario em {contact['aniversario'].strftime('%d/%m')}. Envie uma mensagem!",
                    "razao": f"Contato C{circulo} faz aniversario em {dias} dias",
                    "dados": {
                        "dias_ate": dias,
                        "circulo": circulo,
                        "data_aniversario": contact["aniversario"].strftime("%Y-%m-%d"),
                        "empresa": contact["empresa"]
                    },
                    "prioridade": prioridade,
                    "validade": (datetime.now() + timedelta(days=dias + 1)).isoformat(),
                    "confianca": 1.0
                }
                suggestions.append(suggestion)

        return suggestions

    def generate_followup_suggestions(self, limit: int = 20) -> List[Dict]:
        """Gera sugestoes de follow-up baseado em conversas recentes"""
        suggestions = []

        with get_db() as conn:
            cursor = conn.cursor()

            # Conversas que requerem resposta
            cursor.execute("""
                SELECT
                    c.id as conversation_id,
                    ct.id as contact_id,
                    ct.nome,
                    ct.empresa,
                    ct.circulo,
                    c.canal,
                    c.assunto,
                    c.ultimo_mensagem,
                    EXTRACT(DAY FROM NOW() - c.ultimo_mensagem)::int as dias_desde
                FROM conversations c
                JOIN contacts ct ON ct.id = c.contact_id
                WHERE c.requer_resposta = TRUE
                AND ct.id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'followup'
                    AND status = 'pending'
                    AND criado_em > NOW() - INTERVAL '3 days'
                )
                ORDER BY ct.circulo ASC, c.ultimo_mensagem ASC
                LIMIT %s
            """, (limit,))

            conversations = cursor.fetchall()

            for conv in conversations:
                dias = conv["dias_desde"] or 0
                circulo = conv["circulo"] or 5

                prioridade = min(9, 5 + dias)  # Aumenta prioridade com tempo

                suggestion = {
                    "contact_id": conv["contact_id"],
                    "tipo": "followup",
                    "titulo": f"Responder {conv['nome']} ({conv['canal']})",
                    "descricao": conv["assunto"] or f"Mensagem de {dias} dias atras aguardando resposta",
                    "razao": f"Conversa pendente ha {dias} dias via {conv['canal']}",
                    "dados": {
                        "conversation_id": conv["conversation_id"],
                        "canal": conv["canal"],
                        "dias_desde": dias,
                        "circulo": circulo,
                        "assunto": conv["assunto"]
                    },
                    "prioridade": prioridade,
                    "confianca": 0.95
                }
                suggestions.append(suggestion)

        return suggestions

    def generate_health_alert_suggestions(self, threshold: int = 30, limit: int = 20) -> List[Dict]:
        """Gera alertas para contatos com health baixo"""
        suggestions = []

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, nome, empresa, circulo, health_score, ultimo_contato,
                       EXTRACT(DAY FROM NOW() - ultimo_contato)::int as dias_sem_contato
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                AND COALESCE(health_score, 50) < %s
                AND id NOT IN (
                    SELECT contact_id FROM ai_suggestions
                    WHERE tipo = 'health_alert'
                    AND status = 'pending'
                    AND criado_em > NOW() - INTERVAL '7 days'
                )
                ORDER BY health_score ASC, circulo ASC
                LIMIT %s
            """, (threshold, limit))

            contacts = cursor.fetchall()

            for contact in contacts:
                health = contact["health_score"] or 0
                circulo = contact["circulo"] or 5
                dias = contact["dias_sem_contato"] or 0

                # Criticidade baseada no health
                if health < 20:
                    prioridade = 10
                    urgencia = "CRITICO"
                elif health < 30:
                    prioridade = 8
                    urgencia = "URGENTE"
                else:
                    prioridade = 6
                    urgencia = "ATENCAO"

                suggestion = {
                    "contact_id": contact["id"],
                    "tipo": "health_alert",
                    "titulo": f"[{urgencia}] {contact['nome']} - Health {health}%",
                    "descricao": f"Relacionamento em risco. {dias} dias sem contato.",
                    "razao": f"Health score abaixo de {threshold}% para contato C{circulo}",
                    "dados": {
                        "health_score": health,
                        "circulo": circulo,
                        "dias_sem_contato": dias,
                        "empresa": contact["empresa"]
                    },
                    "prioridade": prioridade,
                    "confianca": 0.85
                }
                suggestions.append(suggestion)

        return suggestions

    def save_suggestions(self, suggestions: List[Dict]) -> int:
        """Salva sugestoes no banco de dados"""
        if not suggestions:
            return 0

        saved = 0
        with get_db() as conn:
            cursor = conn.cursor()

            for s in suggestions:
                try:
                    cursor.execute("""
                        INSERT INTO ai_suggestions
                        (contact_id, tipo, titulo, descricao, razao, dados, prioridade, validade, confianca)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        s["contact_id"],
                        s["tipo"],
                        s["titulo"],
                        s.get("descricao"),
                        s.get("razao"),
                        json.dumps(s.get("dados", {})),
                        s.get("prioridade", 5),
                        s.get("validade"),
                        s.get("confianca", 0.8)
                    ))
                    saved += 1
                except Exception as e:
                    print(f"Error saving suggestion: {e}")

            conn.commit()

        return saved

    async def run_daily_generation(self) -> Dict:
        """Executa geracao diaria de todas as sugestoes"""
        print("=" * 60)
        print("AI AGENT - GERACAO DIARIA DE SUGESTOES")
        print("=" * 60)
        print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()

        results = {
            "started_at": datetime.now().isoformat(),
            "suggestions": {}
        }

        # 1. Reconnect suggestions
        print("[1/4] Gerando sugestoes de reconexao...", flush=True)
        reconnect = self.generate_reconnect_suggestions(limit=30)
        saved = self.save_suggestions(reconnect)
        results["suggestions"]["reconnect"] = {"generated": len(reconnect), "saved": saved}
        print(f"  -> {saved} sugestoes salvas", flush=True)

        # 2. Birthday suggestions
        print("[2/4] Gerando lembretes de aniversario...", flush=True)
        birthday = self.generate_birthday_suggestions(days_ahead=7)
        saved = self.save_suggestions(birthday)
        results["suggestions"]["birthday"] = {"generated": len(birthday), "saved": saved}
        print(f"  -> {saved} sugestoes salvas", flush=True)

        # 3. Followup suggestions
        print("[3/4] Gerando sugestoes de follow-up...", flush=True)
        followup = self.generate_followup_suggestions(limit=30)
        saved = self.save_suggestions(followup)
        results["suggestions"]["followup"] = {"generated": len(followup), "saved": saved}
        print(f"  -> {saved} sugestoes salvas", flush=True)

        # 4. Health alert suggestions
        print("[4/4] Gerando alertas de health...", flush=True)
        health = self.generate_health_alert_suggestions(threshold=30, limit=20)
        saved = self.save_suggestions(health)
        results["suggestions"]["health_alert"] = {"generated": len(health), "saved": saved}
        print(f"  -> {saved} sugestoes salvas", flush=True)

        results["completed_at"] = datetime.now().isoformat()
        total = sum(r["saved"] for r in results["suggestions"].values())

        print()
        print("=" * 60)
        print(f"TOTAL: {total} sugestoes geradas")
        print("=" * 60)

        return results

    def cleanup_expired_suggestions(self) -> int:
        """Remove sugestoes expiradas"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM ai_suggestions
                WHERE status = 'pending'
                AND validade IS NOT NULL
                AND validade < NOW()
            """)

            deleted = cursor.rowcount
            conn.commit()

            return deleted


_ai_agent = None


def get_ai_agent() -> AIAgentService:
    global _ai_agent
    if _ai_agent is None:
        _ai_agent = AIAgentService()
    return _ai_agent
