"""
Integração com Fathom API

Processa gravações de reuniões para extrair insights, objeções,
e feedback que alimentam o sistema de scoring.

Docs: https://docs.fathom.video/api
"""
import os
import httpx
from typing import Optional, Dict, List
from datetime import datetime
import json

class FathomIntegration:
    """Integração com Fathom para análise de reuniões"""

    BASE_URL = "https://api.fathom.video/v1"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("FATHOM_API_KEY")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    async def get_meetings(
        self,
        limit: int = 50,
        after: Optional[str] = None
    ) -> List[Dict]:
        """
        Lista reuniões gravadas no Fathom

        Args:
            limit: Número máximo de reuniões
            after: Cursor para paginação

        Returns:
            Lista de reuniões
        """
        params = {"limit": limit}
        if after:
            params["after"] = after

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/calls",
                headers=self.headers,
                params=params
            )

            if response.status_code == 200:
                return response.json().get("data", [])
            else:
                print(f"Fathom API error: {response.status_code} - {response.text}")
                return []

    async def get_meeting_details(self, call_id: str) -> Optional[Dict]:
        """
        Obtém detalhes de uma reunião específica

        Args:
            call_id: ID da reunião no Fathom

        Returns:
            Detalhes da reunião incluindo transcrição e summary
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/calls/{call_id}",
                headers=self.headers
            )

            if response.status_code == 200:
                return response.json()
            return None

    async def get_meeting_transcript(self, call_id: str) -> Optional[str]:
        """
        Obtém transcrição completa da reunião

        Args:
            call_id: ID da reunião

        Returns:
            Transcrição em texto
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/calls/{call_id}/transcript",
                headers=self.headers
            )

            if response.status_code == 200:
                data = response.json()
                # Concatenar utterances
                transcript = ""
                for utterance in data.get("transcript", []):
                    speaker = utterance.get("speaker", "Unknown")
                    text = utterance.get("text", "")
                    transcript += f"{speaker}: {text}\n"
                return transcript
            return None

    async def get_meeting_summary(self, call_id: str) -> Optional[Dict]:
        """
        Obtém resumo AI da reunião

        Returns:
            Summary estruturado com key points e action items
        """
        meeting = await self.get_meeting_details(call_id)
        if not meeting:
            return None

        return {
            "title": meeting.get("title", ""),
            "summary": meeting.get("summary", ""),
            "key_topics": meeting.get("key_topics", []),
            "action_items": meeting.get("action_items", []),
            "duration_seconds": meeting.get("duration_seconds", 0),
            "participants": meeting.get("participants", []),
            "date": meeting.get("started_at", "")
        }

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


# Webhook handler para Fathom callbacks
async def handle_fathom_webhook(payload: Dict) -> Dict:
    """
    Processa webhook do Fathom quando reunião é concluída

    Pode ser configurado no Fathom para chamar este endpoint
    """
    event_type = payload.get("type")
    data = payload.get("data", {})

    if event_type == "call.completed":
        call_id = data.get("id")
        fathom = FathomIntegration()
        analysis = await fathom.analyze_meeting_for_sales(call_id)

        return {
            "status": "processed",
            "call_id": call_id,
            "analysis": analysis
        }

    return {"status": "ignored", "event_type": event_type}
