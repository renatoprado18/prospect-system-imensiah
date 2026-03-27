"""
Digest Generator Service - Resumos periodicos

Gera resumos:
- Diario: Atividades do dia anterior
- Semanal: Resumo da semana
- Mensal: Visao geral do mes
"""
import os
import json
import httpx
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from database import get_db

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


class DigestGeneratorService:
    def generate_daily_digest(self, date: datetime = None) -> Dict:
        """Gera digest diario"""
        if date is None:
            date = datetime.now() - timedelta(days=1)

        periodo_inicio = date.replace(hour=0, minute=0, second=0, microsecond=0)
        periodo_fim = periodo_inicio + timedelta(days=1)

        with get_db() as conn:
            cursor = conn.cursor()

            digest = {
                "tipo": "daily",
                "periodo_inicio": periodo_inicio.isoformat(),
                "periodo_fim": periodo_fim.isoformat(),
                "titulo": f"Resumo do Dia - {date.strftime('%d/%m/%Y')}",
                "highlights": [],
                "metricas": {},
                "sugestoes": [],
                "contatos_destaque": []
            }

            # Metricas do dia
            # Mensagens recebidas
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM messages
                WHERE enviado_em >= %s AND enviado_em < %s
                AND direcao = 'inbound'
            """, (periodo_inicio, periodo_fim))
            digest["metricas"]["mensagens_recebidas"] = cursor.fetchone()["total"]

            # Mensagens enviadas
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM messages
                WHERE enviado_em >= %s AND enviado_em < %s
                AND direcao = 'outbound'
            """, (periodo_inicio, periodo_fim))
            digest["metricas"]["mensagens_enviadas"] = cursor.fetchone()["total"]

            # Contatos atualizados
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM contacts
                WHERE atualizado_em >= %s AND atualizado_em < %s
            """, (periodo_inicio, periodo_fim))
            digest["metricas"]["contatos_atualizados"] = cursor.fetchone()["total"]

            # Sugestoes aceitas
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM ai_suggestions
                WHERE aceita_em >= %s AND aceita_em < %s
            """, (periodo_inicio, periodo_fim))
            digest["metricas"]["sugestoes_aceitas"] = cursor.fetchone()["total"]

            # Contatos com mais interacao
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, c.foto_url, COUNT(m.id) as total_mensagens
                FROM contacts c
                JOIN messages m ON m.contact_id = c.id
                WHERE m.enviado_em >= %s AND m.enviado_em < %s
                GROUP BY c.id, c.nome, c.empresa, c.foto_url
                ORDER BY total_mensagens DESC
                LIMIT 5
            """, (periodo_inicio, periodo_fim))

            for row in cursor.fetchall():
                digest["contatos_destaque"].append({
                    "id": row["id"],
                    "nome": row["nome"],
                    "empresa": row["empresa"],
                    "foto_url": row.get("foto_url"),
                    "mensagens": row["total_mensagens"]
                })

            # Highlights
            total_msg = digest["metricas"]["mensagens_recebidas"] + digest["metricas"]["mensagens_enviadas"]
            if total_msg > 0:
                digest["highlights"].append(f"{total_msg} mensagens trocadas")

            if digest["metricas"]["contatos_atualizados"] > 0:
                digest["highlights"].append(f"{digest['metricas']['contatos_atualizados']} contatos atualizados")

            # Aniversarios do dia
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM contacts
                WHERE aniversario IS NOT NULL
                AND EXTRACT(MONTH FROM aniversario) = %s
                AND EXTRACT(DAY FROM aniversario) = %s
            """, (date.month, date.day))
            aniversarios = cursor.fetchone()["total"]
            if aniversarios > 0:
                digest["highlights"].append(f"{aniversarios} aniversarios hoje")

            # Sugestoes pendentes
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM ai_suggestions
                WHERE status = 'pending'
            """)
            pendentes = cursor.fetchone()["total"]
            if pendentes > 0:
                digest["sugestoes"].append(f"Voce tem {pendentes} sugestoes pendentes para revisar")

            # Salvar digest
            resumo = self._build_summary(digest)
            digest["resumo"] = resumo

            cursor.execute("""
                INSERT INTO ai_digests
                (tipo, periodo_inicio, periodo_fim, titulo, resumo, highlights, metricas, sugestoes, contatos_destaque)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                digest["tipo"],
                periodo_inicio,
                periodo_fim,
                digest["titulo"],
                resumo,
                json.dumps(digest["highlights"]),
                json.dumps(digest["metricas"]),
                json.dumps(digest["sugestoes"]),
                json.dumps(digest["contatos_destaque"])
            ))

            digest["id"] = cursor.fetchone()["id"]
            conn.commit()

        return digest

    def generate_weekly_digest(self, week_start: datetime = None) -> Dict:
        """Gera digest semanal"""
        if week_start is None:
            # Semana passada
            today = datetime.now()
            week_start = today - timedelta(days=today.weekday() + 7)

        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        periodo_inicio = week_start
        periodo_fim = week_start + timedelta(days=7)

        with get_db() as conn:
            cursor = conn.cursor()

            digest = {
                "tipo": "weekly",
                "periodo_inicio": periodo_inicio.isoformat(),
                "periodo_fim": periodo_fim.isoformat(),
                "titulo": f"Resumo Semanal - {periodo_inicio.strftime('%d/%m')} a {(periodo_fim - timedelta(days=1)).strftime('%d/%m/%Y')}",
                "highlights": [],
                "metricas": {},
                "sugestoes": [],
                "contatos_destaque": []
            }

            # Metricas da semana
            cursor.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE direcao = 'inbound') as recebidas,
                    COUNT(*) FILTER (WHERE direcao = 'outbound') as enviadas
                FROM messages
                WHERE enviado_em >= %s AND enviado_em < %s
            """, (periodo_inicio, periodo_fim))
            row = cursor.fetchone()
            digest["metricas"]["mensagens_recebidas"] = row["recebidas"]
            digest["metricas"]["mensagens_enviadas"] = row["enviadas"]

            # Contatos unicos interagidos
            cursor.execute("""
                SELECT COUNT(DISTINCT contact_id) as total
                FROM messages
                WHERE enviado_em >= %s AND enviado_em < %s
            """, (periodo_inicio, periodo_fim))
            digest["metricas"]["contatos_interagidos"] = cursor.fetchone()["total"]

            # Sugestoes da semana
            cursor.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'accepted' OR status = 'executed') as aceitas,
                    COUNT(*) FILTER (WHERE status = 'dismissed') as descartadas,
                    COUNT(*) as total
                FROM ai_suggestions
                WHERE criado_em >= %s AND criado_em < %s
            """, (periodo_inicio, periodo_fim))
            row = cursor.fetchone()
            digest["metricas"]["sugestoes_geradas"] = row["total"]
            digest["metricas"]["sugestoes_aceitas"] = row["aceitas"]

            # Top contatos da semana
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, c.circulo, c.foto_url,
                       COUNT(m.id) as total_mensagens
                FROM contacts c
                JOIN messages m ON m.contact_id = c.id
                WHERE m.enviado_em >= %s AND m.enviado_em < %s
                GROUP BY c.id, c.nome, c.empresa, c.circulo, c.foto_url
                ORDER BY total_mensagens DESC
                LIMIT 10
            """, (periodo_inicio, periodo_fim))

            for row in cursor.fetchall():
                digest["contatos_destaque"].append({
                    "id": row["id"],
                    "nome": row["nome"],
                    "empresa": row["empresa"],
                    "circulo": row["circulo"],
                    "foto_url": row.get("foto_url"),
                    "mensagens": row["total_mensagens"]
                })

            # Highlights
            total_msg = digest["metricas"]["mensagens_recebidas"] + digest["metricas"]["mensagens_enviadas"]
            digest["highlights"].append(f"{total_msg} mensagens na semana")
            digest["highlights"].append(f"{digest['metricas']['contatos_interagidos']} contatos unicos")

            if digest["metricas"]["sugestoes_aceitas"] > 0:
                digest["highlights"].append(f"{digest['metricas']['sugestoes_aceitas']} sugestoes aceitas")

            # Health alerts
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 3
                AND COALESCE(health_score, 50) < 30
            """)
            low_health = cursor.fetchone()["total"]
            if low_health > 0:
                digest["sugestoes"].append(f"{low_health} contatos importantes com health baixo")

            # Stale contacts
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM contacts
                WHERE COALESCE(circulo, 5) <= 2
                AND ultimo_contato < NOW() - INTERVAL '30 days'
            """)
            stale = cursor.fetchone()["total"]
            if stale > 0:
                digest["sugestoes"].append(f"{stale} contatos C1-C2 sem interacao ha mais de 30 dias")

            # Salvar digest
            resumo = self._build_summary(digest)
            digest["resumo"] = resumo

            cursor.execute("""
                INSERT INTO ai_digests
                (tipo, periodo_inicio, periodo_fim, titulo, resumo, highlights, metricas, sugestoes, contatos_destaque)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                digest["tipo"],
                periodo_inicio,
                periodo_fim,
                digest["titulo"],
                resumo,
                json.dumps(digest["highlights"]),
                json.dumps(digest["metricas"]),
                json.dumps(digest["sugestoes"]),
                json.dumps(digest["contatos_destaque"])
            ))

            digest["id"] = cursor.fetchone()["id"]
            conn.commit()

        return digest

    def _build_summary(self, digest: Dict) -> str:
        """Constroi resumo textual do digest"""
        parts = []

        if digest["highlights"]:
            parts.append("Destaques: " + ", ".join(digest["highlights"]))

        if digest["contatos_destaque"]:
            top_contacts = [c["nome"] for c in digest["contatos_destaque"][:3]]
            parts.append("Principais interacoes: " + ", ".join(top_contacts))

        if digest["sugestoes"]:
            parts.append("Atencao: " + "; ".join(digest["sugestoes"]))

        return " | ".join(parts) if parts else "Sem atividades significativas no periodo."

    def get_digest(self, digest_id: int) -> Optional[Dict]:
        """Obtem digest por ID"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT * FROM ai_digests WHERE id = %s",
                (digest_id,)
            )

            row = cursor.fetchone()
            if row:
                d = dict(row)
                for key in ["periodo_inicio", "periodo_fim", "criado_em", "enviado_em"]:
                    if d.get(key) and hasattr(d[key], "isoformat"):
                        d[key] = d[key].isoformat()
                return d
            return None

    def get_recent_digests(
        self,
        tipo: str = None,
        limit: int = 10
    ) -> List[Dict]:
        """Lista digests recentes"""
        with get_db() as conn:
            cursor = conn.cursor()

            if tipo:
                cursor.execute("""
                    SELECT * FROM ai_digests
                    WHERE tipo = %s
                    ORDER BY criado_em DESC
                    LIMIT %s
                """, (tipo, limit))
            else:
                cursor.execute("""
                    SELECT * FROM ai_digests
                    ORDER BY criado_em DESC
                    LIMIT %s
                """, (limit,))

            digests = []
            for row in cursor.fetchall():
                d = dict(row)
                for key in ["periodo_inicio", "periodo_fim", "criado_em", "enviado_em"]:
                    if d.get(key) and hasattr(d[key], "isoformat"):
                        d[key] = d[key].isoformat()
                digests.append(d)

            return digests

    def get_latest_digest(self, tipo: str) -> Optional[Dict]:
        """Obtem digest mais recente de um tipo"""
        digests = self.get_recent_digests(tipo=tipo, limit=1)
        return digests[0] if digests else None

    def mark_as_sent(self, digest_id: int) -> bool:
        """Marca digest como enviado"""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE ai_digests
                SET enviado = TRUE, enviado_em = NOW()
                WHERE id = %s
                RETURNING id
            """, (digest_id,))

            result = cursor.fetchone()
            conn.commit()

            return result is not None

    async def generate_ai_summary(self, digest_id: int) -> Optional[str]:
        """Gera resumo com IA para um digest"""
        if not ANTHROPIC_API_KEY:
            return None

        digest = self.get_digest(digest_id)
        if not digest:
            return None

        prompt = f"""Gere um resumo executivo em portugues brasileiro para este digest de relacionamentos:

Tipo: {digest['tipo']}
Periodo: {digest['periodo_inicio']} a {digest['periodo_fim']}
Titulo: {digest['titulo']}

Metricas:
{json.dumps(digest.get('metricas', {}), indent=2)}

Destaques:
{json.dumps(digest.get('highlights', []), indent=2)}

Alertas:
{json.dumps(digest.get('sugestoes', []), indent=2)}

Contatos em Destaque:
{json.dumps(digest.get('contatos_destaque', []), indent=2)}

Gere um resumo de 2-3 paragrafos, destacando:
1. Visao geral da atividade
2. Pontos positivos
3. Areas que precisam de atencao

Seja conciso e direto."""

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
                        "max_tokens": 1000,
                        "messages": [{"role": "user", "content": prompt}]
                    },
                    timeout=30.0
                )

                if response.status_code == 200:
                    data = response.json()
                    summary = data["content"][0]["text"].strip()

                    # Atualizar digest com resumo AI
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            UPDATE ai_digests
                            SET resumo = %s
                            WHERE id = %s
                        """, (summary, digest_id))
                        conn.commit()

                    return summary

        except Exception as e:
            print(f"Error generating AI summary: {e}")

        return None


_digest_generator = None


def get_digest_generator() -> DigestGeneratorService:
    global _digest_generator
    if _digest_generator is None:
        _digest_generator = DigestGeneratorService()
    return _digest_generator
