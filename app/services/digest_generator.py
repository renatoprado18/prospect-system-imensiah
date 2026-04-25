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
        """Gera digest semanal acionavel com IA."""
        if week_start is None:
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
                "titulo": f"Resumo Semanal — {periodo_inicio.strftime('%d/%m')} a {(periodo_fim - timedelta(days=1)).strftime('%d/%m/%Y')}",
            }

            # 1. Contatos C1-C2 que NÃO tiveram interação na semana
            cursor.execute("""
                SELECT c.id, c.nome, c.empresa, c.circulo, c.health_score, c.ultimo_contato
                FROM contacts c
                WHERE c.circulo <= 2 AND c.ultimo_contato IS NOT NULL
                  AND c.id NOT IN (
                    SELECT DISTINCT cv.contact_id FROM messages m
                    JOIN conversations cv ON cv.id = m.conversation_id
                    WHERE m.enviado_em >= %s AND m.enviado_em < %s
                  )
                ORDER BY c.circulo, c.ultimo_contato ASC
                LIMIT 10
            """, (periodo_inicio, periodo_fim))
            digest["sem_contato"] = [dict(r) for r in cursor.fetchall()]

            # 2. Tarefas vencidas
            cursor.execute("""
                SELECT t.id, t.titulo, t.data_vencimento, p.nome as projeto,
                       c.nome as responsavel
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                LEFT JOIN contacts c ON c.id = t.contact_id
                WHERE t.status = 'pending' AND t.data_vencimento < NOW()
                ORDER BY t.data_vencimento ASC LIMIT 10
            """)
            digest["tarefas_vencidas"] = [dict(r) for r in cursor.fetchall()]

            # 3. Projetos com status critico/atencao
            cursor.execute("""
                SELECT p.id, p.nome, p.tipo,
                    COUNT(*) FILTER (WHERE t.status = 'pending' AND t.data_vencimento < NOW()) as vencidas
                FROM projects p
                LEFT JOIN tasks t ON t.project_id = p.id
                WHERE p.status = 'ativo'
                GROUP BY p.id, p.nome, p.tipo
                HAVING COUNT(*) FILTER (WHERE t.status = 'pending' AND t.data_vencimento < NOW()) > 0
                ORDER BY vencidas DESC LIMIT 5
            """)
            digest["projetos_atencao"] = [dict(r) for r in cursor.fetchall()]

            # 4. Mudanças de emprego detectadas na semana
            cursor.execute("""
                SELECT h.empresa_anterior, h.empresa_nova, h.cargo_nova, h.tipo_mudanca,
                       c.nome, c.id as contact_id
                FROM linkedin_enrichment_history h
                JOIN contacts c ON c.id = h.contact_id
                WHERE h.detectado_em >= %s AND h.detectado_em < %s
                ORDER BY h.detectado_em DESC LIMIT 5
            """, (periodo_inicio, periodo_fim))
            digest["mudancas_emprego"] = [dict(r) for r in cursor.fetchall()]

            # 5. Agenda da próxima semana
            prox_seg = periodo_fim
            prox_dom = prox_seg + timedelta(days=7)
            cursor.execute("""
                SELECT summary, start_datetime
                FROM calendar_events
                WHERE start_datetime >= %s AND start_datetime < %s
                ORDER BY start_datetime LIMIT 10
            """, (prox_seg, prox_dom))
            digest["agenda_proxima"] = [dict(r) for r in cursor.fetchall()]

            # 6. Propostas de ação pendentes
            cursor.execute("""
                SELECT ap.title, ap.urgency, c.nome as contact_nome
                FROM action_proposals ap
                LEFT JOIN contacts c ON c.id = ap.contact_id
                WHERE ap.status = 'pending'
                ORDER BY CASE ap.urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
                LIMIT 5
            """)
            digest["propostas_pendentes"] = [dict(r) for r in cursor.fetchall()]

            # 7. Tarefas concluídas na semana (wins)
            cursor.execute("""
                SELECT COUNT(*) as total FROM tasks
                WHERE status = 'completed' AND data_conclusao >= %s AND data_conclusao < %s
            """, (periodo_inicio, periodo_fim))
            digest["tarefas_concluidas"] = cursor.fetchone()["total"]

            # Generate AI summary
            digest["resumo"] = self._generate_ai_summary(digest)

            # Save
            cursor.execute("""
                INSERT INTO ai_digests
                (tipo, periodo_inicio, periodo_fim, titulo, resumo, highlights, metricas, sugestoes, contatos_destaque)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                digest["tipo"], periodo_inicio, periodo_fim, digest["titulo"],
                digest["resumo"],
                json.dumps(self._extract_highlights(digest)),
                json.dumps(self._extract_metricas(digest)),
                json.dumps(self._extract_sugestoes(digest)),
                json.dumps([])
            ))
            digest["id"] = cursor.fetchone()["id"]
            conn.commit()

        return digest

    def _generate_ai_summary(self, digest: Dict) -> str:
        """Gera resumo acionavel com IA."""
        import httpx as hx

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return self._build_fallback_summary(digest)

        # Build context
        parts = []

        sem_contato = digest.get("sem_contato", [])
        if sem_contato:
            names = [f"{c['nome']} (C{c['circulo']}, {c.get('empresa','')})" for c in sem_contato[:5]]
            parts.append(f"CONTATOS C1-C2 SEM INTERAÇÃO NA SEMANA:\n" + "\n".join(f"- {n}" for n in names))

        vencidas = digest.get("tarefas_vencidas", [])
        if vencidas:
            tasks = [f"{t['titulo']} (projeto: {t.get('projeto','?')}, venceu: {str(t.get('data_vencimento',''))[:10]})" for t in vencidas[:5]]
            parts.append(f"TAREFAS VENCIDAS:\n" + "\n".join(f"- {t}" for t in tasks))

        projetos = digest.get("projetos_atencao", [])
        if projetos:
            projs = [f"{p['nome']} ({p['vencidas']} tarefas vencidas)" for p in projetos]
            parts.append(f"PROJETOS COM ATENÇÃO:\n" + "\n".join(f"- {p}" for p in projs))

        mudancas = digest.get("mudancas_emprego", [])
        if mudancas:
            changes = [f"{m['nome']}: {m.get('empresa_anterior','?')} → {m.get('empresa_nova','?')}" for m in mudancas]
            parts.append(f"MUDANÇAS DE EMPREGO DETECTADAS:\n" + "\n".join(f"- {c}" for c in changes))

        agenda = digest.get("agenda_proxima", [])
        if agenda:
            events = [f"{e.get('summary','')} ({e['start_datetime'].strftime('%a %d/%m %H:%M') if hasattr(e.get('start_datetime',''), 'strftime') else str(e.get('start_datetime',''))[:16]})" for e in agenda[:5]]
            parts.append(f"AGENDA PRÓXIMA SEMANA:\n" + "\n".join(f"- {e}" for e in events))

        propostas = digest.get("propostas_pendentes", [])
        if propostas:
            props = [f"[{p.get('urgency','?')}] {p['title']}" for p in propostas]
            parts.append(f"AÇÕES PENDENTES:\n" + "\n".join(f"- {p}" for p in props))

        concluidas = digest.get("tarefas_concluidas", 0)
        parts.append(f"TAREFAS CONCLUÍDAS NA SEMANA: {concluidas}")

        context = "\n\n".join(parts)

        prompt = f"""Gere um RESUMO SEMANAL acionável para Renato, executivo de tecnologia e governança.

DADOS DA SEMANA:
{context}

FORMATO (Markdown):
## O que precisa de atenção
(máx 3 itens mais urgentes, com ação concreta)

## Relacionamentos
(quem precisa de contato, oportunidades de reconexão)

## Progresso
(tarefas concluídas, wins da semana)

## Próxima semana
(o que vem pela frente, preparação necessária)

REGRAS:
- Máximo 300 palavras
- Acionável: cada item deve ter uma ação clara
- Priorize por impacto, não por quantidade
- Português, direto, sem formalidades"""

        try:
            resp = hx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]},
                timeout=15.0
            )
            if resp.status_code == 200:
                return resp.json()["content"][0]["text"]
        except Exception:
            pass

        return self._build_fallback_summary(digest)

    def _build_fallback_summary(self, digest: Dict) -> str:
        """Resumo sem IA."""
        parts = []
        if digest.get("tarefas_vencidas"):
            parts.append(f"⚠️ {len(digest['tarefas_vencidas'])} tarefas vencidas")
        if digest.get("sem_contato"):
            parts.append(f"👥 {len(digest['sem_contato'])} contatos C1-C2 sem interação")
        if digest.get("propostas_pendentes"):
            parts.append(f"📋 {len(digest['propostas_pendentes'])} ações pendentes")
        parts.append(f"✅ {digest.get('tarefas_concluidas', 0)} tarefas concluídas")
        return " | ".join(parts)

    def _extract_highlights(self, digest: Dict) -> list:
        h = []
        if digest.get("tarefas_concluidas"):
            h.append(f"{digest['tarefas_concluidas']} tarefas concluídas")
        if digest.get("mudancas_emprego"):
            h.append(f"{len(digest['mudancas_emprego'])} mudanças de emprego detectadas")
        return h

    def _extract_metricas(self, digest: Dict) -> dict:
        return {
            "tarefas_vencidas": len(digest.get("tarefas_vencidas", [])),
            "tarefas_concluidas": digest.get("tarefas_concluidas", 0),
            "contatos_sem_interacao": len(digest.get("sem_contato", [])),
            "projetos_atencao": len(digest.get("projetos_atencao", [])),
            "propostas_pendentes": len(digest.get("propostas_pendentes", [])),
        }

    def _extract_sugestoes(self, digest: Dict) -> list:
        s = []
        for c in digest.get("sem_contato", [])[:3]:
            s.append(f"Reconectar com {c['nome']} (C{c['circulo']})")
        for t in digest.get("tarefas_vencidas", [])[:3]:
            s.append(f"Resolver: {t['titulo']}")
        return s

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
