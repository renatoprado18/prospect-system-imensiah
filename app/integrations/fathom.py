"""
Integração com Fathom API (atualizada 2026-05-07)

Migrada do endpoint antigo `api.fathom.video/v1` (DNS extinto) pro novo
`api.fathom.ai/external/v1`. Auth via X-Api-Key (3 chaves no .env: default,
PESSOAL, PROFISSIONAL). Endpoints validos:
  GET  /meetings                        — lista com filtros (created_after, etc)
                                          + includes (summary, transcript, action_items)
  GET  /recordings/{id}/summary         — markdown formatado
  GET  /recordings/{id}/transcript      — array de utterances
  POST /webhooks                        — registra callback (Svix-signed)
  Webhook event: new-meeting-content-ready
"""
import os
import httpx
import re
import hmac
import hashlib
import base64
import time
import logging
from typing import Optional, Dict, List, Tuple
from datetime import datetime
import json

logger = logging.getLogger(__name__)

logger_module = __name__

# API atual (2026-05-07). Antigo `api.fathom.video/v1` foi descontinuado.
FATHOM_BASE_URL = "https://api.fathom.ai/external/v1"


def _resolve_fathom_key(account: Optional[str] = None) -> Optional[str]:
    """Resolve API key por conta (personal | professional). Default: profissional.

    Aceita os 3 envs no padrao da CLAUDE.md:
    - FATHOM_API_KEY_PROFISSIONAL  (preferido pra trabalho)
    - FATHOM_API_KEY_PESSOAL       (familia/saude/lazer)
    - FATHOM_API_KEY               (legacy default)

    Strip() automatico (memory feedback_env_var_whitespace.md: Vercel cola \\n).
    """
    if account:
        alias = account.strip().lower()
        if alias in ("personal", "pessoal"):
            key = os.getenv("FATHOM_API_KEY_PESSOAL", "")
        elif alias in ("professional", "profissional", "work"):
            key = os.getenv("FATHOM_API_KEY_PROFISSIONAL", "")
        else:
            key = ""
        if key.strip():
            return key.strip()
    # Fallback: profissional > pessoal > default
    for env in ("FATHOM_API_KEY_PROFISSIONAL", "FATHOM_API_KEY_PESSOAL", "FATHOM_API_KEY"):
        v = os.getenv(env, "").strip()
        if v:
            return v
    return None


class FathomIntegration:
    """Integração com Fathom (api.fathom.ai/external/v1, 2026)"""

    BASE_URL = FATHOM_BASE_URL

    def __init__(self, api_key: Optional[str] = None, account: Optional[str] = None):
        self.account = account
        self.api_key = (api_key or _resolve_fathom_key(account) or "").strip()
        self.headers = {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        }

    async def list_meetings(
        self,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        cursor: Optional[str] = None,
        include_summary: bool = True,
        include_transcript: bool = False,
        include_action_items: bool = True,
        recorded_by: Optional[List[str]] = None,
    ) -> Dict:
        """Lista reunioes com filtros + includes inline (1 call ja vem com tudo).

        Retorna {items: [...], next_cursor: str|None}. Cada item tem:
          recording_id, title, meeting_title, share_url, created_at,
          scheduled_start_time, scheduled_end_time, recording_start_time,
          recording_end_time, calendar_invitees [{email, email_domain, ...}],
          recorded_by, default_summary {markdown_formatted}, action_items, transcript.
        """
        if not self.api_key:
            return {"error": "no_api_key", "items": []}

        params: List[Tuple[str, str]] = []
        if created_after:
            params.append(("created_after", created_after))
        if created_before:
            params.append(("created_before", created_before))
        if cursor:
            params.append(("cursor", cursor))
        params.append(("include_summary", "true" if include_summary else "false"))
        params.append(("include_transcript", "true" if include_transcript else "false"))
        params.append(("include_action_items", "true" if include_action_items else "false"))
        for rb in (recorded_by or []):
            params.append(("recorded_by[]", rb))

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.BASE_URL}/meetings",
                    headers=self.headers,
                    params=params,
                )
                if resp.status_code == 200:
                    return resp.json()
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "items": []}
        except Exception as e:
            return {"error": str(e), "items": []}

    async def create_webhook(
        self,
        destination_url: str,
        triggered_for: List[str],
        include_summary: bool = True,
        include_action_items: bool = True,
        include_transcript: bool = False,
        include_crm_matches: bool = False,
    ) -> Dict:
        """POST /webhooks — registra callback. Retorna {id, url, secret, ...}.

        Pelo menos um dos include_* tem que ser true.
        triggered_for enum: my_recordings, shared_external_recordings,
                            my_shared_with_team_recordings, shared_team_recordings.
        Secret retornado precisa virar FATHOM_WEBHOOK_SECRET pra validar assinatura.
        """
        if not self.api_key:
            return {"error": "no_api_key"}
        body = {
            "destination_url": destination_url,
            "triggered_for": triggered_for,
            "include_summary": include_summary,
            "include_action_items": include_action_items,
            "include_transcript": include_transcript,
            "include_crm_matches": include_crm_matches,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.BASE_URL}/webhooks",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json=body,
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as e:
            return {"error": str(e)}

    async def list_webhooks(self) -> Dict:
        if not self.api_key:
            return {"error": "no_api_key", "items": []}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.BASE_URL}/webhooks", headers=self.headers)
                if resp.status_code == 200:
                    return resp.json()
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "items": []}
        except Exception as e:
            return {"error": str(e), "items": []}

    async def delete_webhook(self, webhook_id: str) -> Dict:
        if not self.api_key:
            return {"error": "no_api_key"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.delete(
                    f"{self.BASE_URL}/webhooks/{webhook_id}", headers=self.headers
                )
                if resp.status_code in (200, 204):
                    return {"deleted": True, "id": webhook_id}
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    # Alias retrocompat — get_meetings retornava lista, agora encapsula list_meetings
    async def get_meetings(self, limit: int = 50, after: Optional[str] = None) -> List[Dict]:
        result = await self.list_meetings(cursor=after)
        return result.get("items", [])

    async def get_recording_summary(self, recording_id: int) -> Optional[Dict]:
        """GET /recordings/{id}/summary — retorna {summary: {template_name, markdown_formatted}}."""
        if not self.api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.BASE_URL}/recordings/{recording_id}/summary",
                    headers=self.headers,
                )
                if resp.status_code == 200:
                    return resp.json().get("summary")
                return None
        except Exception:
            return None

    async def get_recording_transcript(self, recording_id: int) -> Optional[List[Dict]]:
        """GET /recordings/{id}/transcript — array de {speaker, text, timestamp}."""
        if not self.api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    f"{self.BASE_URL}/recordings/{recording_id}/transcript",
                    headers=self.headers,
                )
                if resp.status_code == 200:
                    return resp.json().get("transcript", [])
                return None
        except Exception:
            return None

    # Compat: assinatura antiga get_meeting_transcript(call_id) -> str
    async def get_meeting_transcript(self, call_id) -> Optional[str]:
        try:
            rec_id = int(call_id)
        except (TypeError, ValueError):
            return None
        utterances = await self.get_recording_transcript(rec_id)
        if not utterances:
            return None
        out = []
        for u in utterances:
            sp = u.get("speaker") or {}
            speaker = sp.get("display_name") if isinstance(sp, dict) else str(sp)
            ts = u.get("timestamp") or ""
            text = u.get("text") or ""
            out.append(f"[{ts}] {speaker}: {text}")
        return "\n".join(out)

    async def get_meeting_summary(self, recording_id) -> Optional[Dict]:
        """Adapter retrocompat. Retorna dict com title/summary/action_items/etc."""
        try:
            rec_id = int(recording_id)
        except (TypeError, ValueError):
            return None
        # Busca via /meetings filtrando + includes (1 call traz tudo)
        # Janela 90d cobre praticamente qualquer caso real
        from datetime import timedelta
        created_after = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")
        result = await self.list_meetings(
            created_after=created_after,
            include_summary=True,
            include_action_items=True,
        )
        for m in result.get("items", []):
            if m.get("recording_id") == rec_id:
                return _adapt_meeting_to_summary(m)
        # Fallback: busca summary direto se /meetings nao trouxe
        summary = await self.get_recording_summary(rec_id)
        if summary:
            return {
                "title": "",
                "summary": summary.get("markdown_formatted", ""),
                "action_items": [],
                "duration_seconds": 0,
                "participants": [],
                "date": "",
            }
        return None

    async def get_meeting_details(self, recording_id) -> Optional[Dict]:
        """Compat: retorna o item bruto da /meetings com tudo incluido."""
        try:
            rec_id = int(recording_id)
        except (TypeError, ValueError):
            return None
        from datetime import timedelta
        created_after = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")
        result = await self.list_meetings(
            created_after=created_after,
            include_summary=True,
            include_transcript=True,
            include_action_items=True,
        )
        for m in result.get("items", []):
            if m.get("recording_id") == rec_id:
                return m
        return None


    async def analyze_meeting_for_sales(self, call_id: str) -> Dict:
        """
        Analisa reunião para extrair insights de vendas

        Identifica:
        - Objeções levantadas
        - Features de interesse
        - Sentimento geral
        - Próximos passos mencionados
        - Nível de interesse

        Returns:
            Análise estruturada para alimentar o scoring
        """
        meeting = await self.get_meeting_details(call_id)
        transcript = await self.get_meeting_transcript(call_id)

        if not meeting:
            return {"error": "Meeting not found"}

        analysis = {
            "call_id": call_id,
            "date": meeting.get("started_at"),
            "duration_minutes": meeting.get("duration_seconds", 0) // 60,
            "summary": meeting.get("summary", ""),
            "objecoes": [],
            "features_interesse": [],
            "sentiment": "neutro",
            "interesse_level": "medio",
            "proximos_passos": [],
            "action_items": meeting.get("action_items", []),
            "key_insights": []
        }

        # Analisar transcrição para objeções e interesse
        if transcript:
            analysis["objecoes"] = self._extract_objections(transcript)
            analysis["features_interesse"] = self._extract_features_interest(transcript)
            analysis["sentiment"] = self._analyze_sentiment(transcript)
            analysis["interesse_level"] = self._analyze_interest_level(transcript)
            analysis["proximos_passos"] = self._extract_next_steps(transcript)

        return analysis

    def _extract_objections(self, transcript: str) -> List[str]:
        """Extrai objeções comuns da transcrição"""
        objection_keywords = [
            ("preço", "Preocupação com preço/custo"),
            ("caro", "Preocupação com preço/custo"),
            ("orçamento", "Limitação de orçamento"),
            ("budget", "Limitação de orçamento"),
            ("tempo", "Falta de tempo para implementar"),
            ("complexo", "Preocupação com complexidade"),
            ("difícil", "Preocupação com complexidade"),
            ("já temos", "Possui solução similar"),
            ("concorrente", "Avaliando concorrentes"),
            ("decidir", "Precisa de mais tempo para decidir"),
            ("aprovar", "Necessita aprovação interna"),
            ("board", "Necessita aprovação do conselho"),
            ("dados", "Preocupação com segurança de dados"),
            ("segurança", "Preocupação com segurança"),
            ("não sei", "Incerteza sobre necessidade"),
            ("talvez", "Interesse indefinido"),
        ]

        found = []
        transcript_lower = transcript.lower()

        for keyword, objection in objection_keywords:
            if keyword in transcript_lower:
                if objection not in found:
                    found.append(objection)

        return found

    def _extract_features_interest(self, transcript: str) -> List[str]:
        """Identifica features que geraram interesse"""
        feature_keywords = [
            ("ia", "Inteligência Artificial"),
            ("inteligência artificial", "Inteligência Artificial"),
            ("rapidez", "Velocidade de entrega"),
            ("48 horas", "Ciclo de 48 horas"),
            ("diagnóstico", "Diagnóstico rápido"),
            ("consultoria", "Expertise humana"),
            ("especialista", "Expertise humana"),
            ("decisão", "Suporte à decisão"),
            ("estratégi", "Análise estratégica"),
            ("governança", "Governança corporativa"),
            ("relatório", "Relatórios e análises"),
            ("dashboard", "Visualização de dados"),
            ("integração", "Integrações"),
            ("cnpj", "Enriquecimento via CNPJ"),
        ]

        found = []
        transcript_lower = transcript.lower()

        # Procurar por padrões de interesse positivo
        positive_indicators = ["interessante", "gostei", "legal", "bom", "excelente", "perfeito"]

        for keyword, feature in feature_keywords:
            if keyword in transcript_lower:
                # Verificar se há indicador positivo próximo
                idx = transcript_lower.find(keyword)
                context = transcript_lower[max(0, idx-50):idx+50]
                if any(ind in context for ind in positive_indicators):
                    if feature not in found:
                        found.append(feature)

        return found

    def _analyze_sentiment(self, transcript: str) -> str:
        """Analisa sentimento geral da conversa"""
        positive = ["ótimo", "excelente", "perfeito", "interessante", "gostei",
                   "vamos", "fechado", "quando podemos", "próximos passos"]
        negative = ["não", "difícil", "problema", "preocupa", "caro",
                   "não sei", "talvez", "vou pensar", "não agora"]

        transcript_lower = transcript.lower()

        pos_count = sum(1 for word in positive if word in transcript_lower)
        neg_count = sum(1 for word in negative if word in transcript_lower)

        if pos_count > neg_count * 2:
            return "muito_positivo"
        elif pos_count > neg_count:
            return "positivo"
        elif neg_count > pos_count * 2:
            return "negativo"
        elif neg_count > pos_count:
            return "cauteloso"
        return "neutro"

    def _analyze_interest_level(self, transcript: str) -> str:
        """Determina nível de interesse do prospect"""
        high_interest = ["quando podemos começar", "vamos fechar", "me manda proposta",
                        "próximos passos", "como funciona o contrato", "preço"]
        medium_interest = ["interessante", "gostaria de saber mais", "pode me mandar",
                          "vou avaliar", "vou conversar"]
        low_interest = ["não é momento", "não tenho interesse", "não preciso",
                       "já temos", "não agora", "talvez no futuro"]

        transcript_lower = transcript.lower()

        if any(phrase in transcript_lower for phrase in high_interest):
            return "alto"
        elif any(phrase in transcript_lower for phrase in low_interest):
            return "baixo"
        elif any(phrase in transcript_lower for phrase in medium_interest):
            return "medio"
        return "indefinido"

    def _extract_next_steps(self, transcript: str) -> List[str]:
        """Extrai próximos passos mencionados"""
        next_step_keywords = [
            "vou enviar", "mandar proposta", "agendar", "marcar",
            "segunda reunião", "demo", "apresentação", "contrato",
            "follow up", "retorno", "ligar"
        ]

        found = []
        transcript_lower = transcript.lower()

        for keyword in next_step_keywords:
            if keyword in transcript_lower:
                # Extrair contexto
                idx = transcript_lower.find(keyword)
                context = transcript[max(0, idx-20):idx+50].strip()
                if context and context not in found:
                    found.append(context)

        return found[:5]  # Limitar a 5 próximos passos

    async def process_recent_meetings(
        self,
        since_hours: int = 24
    ) -> List[Dict]:
        """
        Processa reuniões recentes para atualizar sistema

        Usado pelo cron job para manter dados atualizados
        """
        meetings = await self.get_meetings(limit=20)

        processed = []
        for meeting in meetings:
            # Verificar se é recente
            started_at = meeting.get("started_at")
            if started_at:
                meeting_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                hours_ago = (datetime.now(meeting_time.tzinfo) - meeting_time).total_seconds() / 3600

                if hours_ago <= since_hours:
                    analysis = await self.analyze_meeting_for_sales(meeting["id"])
                    processed.append({
                        "meeting": meeting,
                        "analysis": analysis
                    })

        return processed

    async def extract_from_share_link(self, share_url: str) -> Optional[Dict]:
        """Extrai dados de um link compartilhado do Fathom.

        API atual nao tem endpoint /shared/{token} — share_url so vem como
        campo do item /meetings. Estrategia: chama /meetings com janela 90d
        + includes, filtra client-side onde share_url contem o token.

        Args:
            share_url: URL no formato https://fathom.video/share/XXXXX

        Returns:
            Dict adaptado pro formato legacy (title, summary, action_items,
            participants, date, call_id, share_id, recording_id, transcript).
        """
        match = re.search(r'fathom\.video/share/([A-Za-z0-9_-]+)', share_url)
        if not match:
            return None
        share_id = match.group(1)

        # Janela 90d cobre praticamente qualquer caso real
        from datetime import timedelta
        created_after = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")
        result = await self.list_meetings(
            created_after=created_after,
            include_summary=True,
            include_action_items=True,
            include_transcript=False,  # transcript e pesado, busca on-demand
        )
        if result.get("error"):
            return None

        for m in result.get("items", []):
            m_share = m.get("share_url") or ""
            if share_id in m_share:
                adapted = _adapt_meeting_to_summary(m)
                adapted["share_id"] = share_id
                adapted["share_url"] = share_url
                adapted["call_id"] = m.get("recording_id")
                adapted["calendar_invitees"] = m.get("calendar_invitees", []) or []
                adapted["duration_minutes"] = adapted.get("duration_seconds", 0) // 60
                return adapted

        # Fallback: retornar metadata minima do link
        return {
            "share_id": share_id,
            "share_url": share_url,
            "title": "Reunião Fathom (não localizada na API)",
            "summary": "",
            "duration_minutes": 0,
            "participants": [],
            "action_items": [],
            "key_topics": [],
            "date": None,
            "call_id": share_id
        }

    async def get_unlinked_meetings(self, linked_ids: List[str]) -> List[Dict]:
        """
        Retorna reuniões que ainda não foram vinculadas a nenhum prospect

        Args:
            linked_ids: Lista de IDs de reuniões já vinculadas

        Returns:
            Lista de reuniões não vinculadas
        """
        all_meetings = await self.get_meetings(limit=50)

        unlinked = []
        for meeting in all_meetings:
            if meeting.get("id") not in linked_ids:
                unlinked.append({
                    "id": meeting.get("id"),
                    "title": meeting.get("title", "Sem título"),
                    "date": meeting.get("started_at"),
                    "duration_minutes": meeting.get("duration_seconds", 0) // 60,
                    "participants": meeting.get("participants", [])
                })

        return unlinked

    async def suggest_prospect_match(self, meeting: Dict, prospects_emails: List[Dict]) -> Optional[Dict]:
        """
        Sugere qual prospect corresponde a uma reunião baseado nos participantes

        Args:
            meeting: Dados da reunião com participantes
            prospects_emails: Lista de dicts com {id, nome, email} dos prospects

        Returns:
            Prospect sugerido ou None
        """
        participants = meeting.get("participants", [])
        participant_emails = [p.get("email", "").lower() for p in participants if p.get("email")]

        for prospect in prospects_emails:
            if prospect.get("email") and prospect["email"].lower() in participant_emails:
                return prospect

        # Tentar match por nome
        participant_names = [p.get("name", "").lower() for p in participants if p.get("name")]
        for prospect in prospects_emails:
            if prospect.get("nome"):
                prospect_name_parts = prospect["nome"].lower().split()
                for pname in participant_names:
                    if any(part in pname for part in prospect_name_parts if len(part) > 2):
                        return prospect

        return None


# =============================================================================
# Webhook signature verification (Svix scheme: webhook-id, webhook-timestamp,
# webhook-signature). Secret format: whsec_<base64>. Signed content:
# {webhook_id}.{webhook_timestamp}.{raw_body}, HMAC-SHA256, base64-encoded.
# =============================================================================

def verify_webhook_signature(
    secret: str,
    raw_body: bytes,
    headers: Dict[str, str],
    tolerance_seconds: int = 300,
) -> bool:
    """Valida assinatura Svix. Headers case-insensitive (passe lowercased)."""
    if not secret:
        return False
    msg_id = headers.get("webhook-id") or headers.get("Webhook-Id") or ""
    msg_ts = headers.get("webhook-timestamp") or headers.get("Webhook-Timestamp") or ""
    sig_header = headers.get("webhook-signature") or headers.get("Webhook-Signature") or ""
    if not msg_id or not msg_ts or not sig_header:
        return False
    try:
        ts = int(msg_ts)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > tolerance_seconds:
        return False

    if secret.startswith("whsec_"):
        try:
            key = base64.b64decode(secret[len("whsec_"):])
        except Exception:
            return False
    else:
        key = secret.encode()

    signed = msg_id.encode() + b"." + msg_ts.encode() + b"." + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    for part in sig_header.split():
        if "," not in part:
            continue
        version, sig = part.split(",", 1)
        if version != "v1":
            continue
        if hmac.compare_digest(expected, sig):
            return True
    return False


# =============================================================================
# Persistência de meeting recebido (webhook) → contact_memories + tasks
# Mesma logica do tool import_fathom_meeting (intel_bot.py), mas sem retorno
# no formato de bot — so insere no banco.
# =============================================================================

async def process_fathom_meeting(meeting_payload: Dict, project_id: Optional[int] = None) -> Dict:
    """Processa um Meeting do Fathom (payload do webhook ou /meetings) e persiste.

    - Adapta com `_adapt_meeting_to_summary`
    - Match attendees externos contra contacts.emails (jsonb)
    - Insere contact_memories pra cada contato identificado
    - Insere tasks (cap 10) pros action_items
    - Se project_id passado: tambem insere project_notes

    Retorna stats {recording_id, matched_contacts, memorias_criadas,
                   tarefas_criadas, nota_projeto_id}.
    """
    from database import get_db  # local import: evita ciclo no boot

    adapted = _adapt_meeting_to_summary(meeting_payload)
    rec_id = adapted.get("recording_id")
    title = adapted.get("title") or "Reuniao Fathom"
    summary_md = adapted.get("summary") or ""
    action_items = adapted.get("action_items") or []
    date_iso = adapted.get("date") or ""
    share_url = adapted.get("share_url") or ""
    attendees = meeting_payload.get("calendar_invitees") or []

    emails_to_match: List[str] = []
    for att in attendees:
        if att.get("is_external") is False:
            continue  # Renato proprio
        em = att.get("email")
        if em and "@" in str(em):
            emails_to_match.append(str(em).lower().strip())

    matched_contacts: List[Dict] = []
    if emails_to_match:
        with get_db() as conn:
            cur = conn.cursor()
            ph = ",".join(["%s"] * len(emails_to_match))
            cur.execute(
                f"""
                SELECT DISTINCT c.id, c.nome, c.empresa
                FROM contacts c,
                     jsonb_array_elements(COALESCE(c.emails, '[]'::jsonb)) AS ce
                WHERE LOWER(
                    CASE
                      WHEN jsonb_typeof(ce) = 'object' THEN ce->>'email'
                      WHEN jsonb_typeof(ce) = 'string' THEN ce#>>'{{}}'
                      ELSE NULL
                    END
                ) IN ({ph})
                """,
                emails_to_match,
            )
            matched_contacts = [dict(r) for r in cur.fetchall()]

    memorias_criadas: List[Dict] = []
    if matched_contacts:
        outros_nomes = [c["nome"] for c in matched_contacts]
        with get_db() as conn:
            cur = conn.cursor()
            for c in matched_contacts:
                outros = [n for n in outros_nomes if n != c["nome"]]
                outros_str = ", ".join(outros) if outros else "Renato"
                resumo_curto = (summary_md[:600] + "...") if len(summary_md) > 600 else summary_md
                cur.execute(
                    """
                    INSERT INTO contact_memories
                      (contact_id, tipo, source_table, titulo, resumo,
                       conteudo_completo, data_ocorrencia, criado_em)
                    VALUES (%s, 'reuniao', 'fathom', %s, %s, %s, %s, NOW())
                    RETURNING id
                    """,
                    (
                        c["id"],
                        f"{title} (com {outros_str})",
                        resumo_curto or f"Reuniao Fathom em {date_iso}",
                        f"{summary_md}\n\n---\nGravacao Fathom: {share_url}",
                        date_iso or None,
                    ),
                )
                mem_id = cur.fetchone()["id"]
                memorias_criadas.append({"contact_id": c["id"], "nome": c["nome"], "memoria_id": mem_id})
            conn.commit()

    tarefas_criadas: List[Dict] = []
    primeiro_contact_id = matched_contacts[0]["id"] if matched_contacts else None
    if action_items:
        with get_db() as conn:
            cur = conn.cursor()
            for ai in action_items[:10]:
                desc = ai.get("description") or ai.get("text") or ""
                playback = ai.get("recording_playback_url") or ""
                if not desc:
                    continue
                full_desc = desc
                if playback:
                    full_desc += f"\n\nMomento na gravacao: {playback}"
                full_desc += f"\nReuniao: {title} ({date_iso})"
                cur.execute(
                    """
                    INSERT INTO tasks (titulo, descricao, status, project_id, contact_id,
                                       prioridade, ai_generated, origem, data_criacao)
                    VALUES (%s, %s, 'pending', %s, %s, 7, true, 'reuniao_fathom', NOW())
                    RETURNING id
                    """,
                    (desc[:200], full_desc, project_id, primeiro_contact_id),
                )
                tid = cur.fetchone()["id"]
                tarefas_criadas.append({"task_id": tid, "titulo": desc[:80]})
            conn.commit()

    nota_id = None
    if project_id and matched_contacts:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor, criado_em)
                VALUES (%s, 'reuniao', %s, %s, 'fathom_webhook', NOW())
                RETURNING id
                """,
                (
                    project_id,
                    f"{title} — {date_iso[:10] if date_iso else ''}",
                    f"Reuniao com {', '.join([c['nome'] for c in matched_contacts])}\n\n{summary_md}\n\nGravacao: {share_url}",
                ),
            )
            nota_id = cur.fetchone()["id"]
            conn.commit()

    return {
        "recording_id": rec_id,
        "title": title,
        "matched_contacts": [{"id": c["id"], "nome": c["nome"]} for c in matched_contacts],
        "memorias_criadas": len(memorias_criadas),
        "tarefas_criadas": len(tarefas_criadas),
        "nota_projeto_id": nota_id,
    }


# Webhook handler para Fathom callbacks (event: new-meeting-content-ready)
async def handle_fathom_webhook(
    payload: Dict,
    raw_body: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict:
    """Processa webhook do Fathom — schema novo (api.fathom.ai/external/v1).

    O Fathom envia o objeto Meeting completo no body (sem wrapper {type, data}).
    Validacao de assinatura via FATHOM_WEBHOOK_SECRET (Svix) — opcional: se a env
    nao estiver setada, loga warning e aceita (modo bootstrap).

    Compat: ainda detecta payload no formato antigo {type: call.completed, data}
    pra evitar quebrar caso algo chame essa funcao com payload legado.
    """
    # Suporta multiplos secrets (1 por conta Fathom: PROFISSIONAL + PESSOAL).
    # Tenta cada um — se algum bater, aceita.
    secrets = []
    for env in ("FATHOM_WEBHOOK_SECRET_PROFISSIONAL",
                "FATHOM_WEBHOOK_SECRET_PESSOAL",
                "FATHOM_WEBHOOK_SECRET"):
        v = (os.getenv(env) or "").strip()
        if v:
            secrets.append(v)
    headers_norm = {k.lower(): v for k, v in (headers or {}).items()}

    if secrets:
        if raw_body is None:
            return {"status": "rejected", "reason": "no_raw_body_for_signature_check"}
        if not any(verify_webhook_signature(s, raw_body, headers_norm) for s in secrets):
            logger.warning("Fathom webhook signature invalida (id=%s ts=%s, tried %d secrets)",
                           headers_norm.get("webhook-id"), headers_norm.get("webhook-timestamp"),
                           len(secrets))
            return {"status": "rejected", "reason": "invalid_signature"}
    else:
        logger.warning("Nenhuma FATHOM_WEBHOOK_SECRET* setada — aceitando sem validar assinatura")

    # Compat: payload legado {type: call.completed, data: {...}}
    if payload.get("type") == "call.completed":
        call_id = payload.get("data", {}).get("id")
        return {"status": "ignored_legacy", "call_id": call_id}

    # Novo schema: payload e o Meeting direto. Heuristica: tem recording_id ou
    # campos de meeting (default_summary, action_items, calendar_invitees).
    if not (payload.get("recording_id") or payload.get("default_summary")
            or payload.get("calendar_invitees") or payload.get("action_items")):
        logger.warning("Fathom webhook payload sem campos esperados de meeting (keys=%s)",
                       list(payload.keys())[:10])
        return {"status": "ignored", "reason": "unrecognized_schema"}

    try:
        stats = await process_fathom_meeting(payload, project_id=None)
        logger.info("Fathom webhook processado: rec_id=%s contatos=%s memorias=%s tarefas=%s",
                    stats.get("recording_id"),
                    len(stats.get("matched_contacts", [])),
                    stats.get("memorias_criadas"),
                    stats.get("tarefas_criadas"))
        return {"status": "processed", **stats}
    except Exception as e:
        logger.exception("Falha ao processar Fathom webhook")
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


# =============================================================================
# Helpers internos (top-level pra ficar fora da classe)
# =============================================================================

def _adapt_meeting_to_summary(meeting: Dict) -> Dict:
    """Converte item da /meetings pro formato legacy de get_meeting_summary."""
    summary_obj = meeting.get("default_summary") or {}
    return {
        "title": meeting.get("meeting_title") or meeting.get("title", ""),
        "summary": summary_obj.get("markdown_formatted", ""),
        "key_topics": [],
        "action_items": meeting.get("action_items", []) or [],
        "duration_seconds": _calc_duration(meeting),
        "participants": [
            {
                "email": p.get("email"),
                "name": p.get("name"),
                "is_external": p.get("is_external"),
            }
            for p in (meeting.get("calendar_invitees") or [])
        ],
        "date": meeting.get("scheduled_start_time") or meeting.get("recording_start_time", ""),
        "recording_id": meeting.get("recording_id"),
        "share_url": meeting.get("share_url"),
    }


def _calc_duration(meeting: Dict) -> int:
    """Duracao em segundos via recording/scheduled timestamps."""
    try:
        start = meeting.get("recording_start_time") or meeting.get("scheduled_start_time")
        end = meeting.get("recording_end_time") or meeting.get("scheduled_end_time")
        if not start or not end:
            return 0
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return int((e - s).total_seconds())
    except Exception:
        return 0
