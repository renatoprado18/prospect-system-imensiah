"""
Daily synthesis — cron noturno que lê as conversas das últimas 24h
(WhatsApp + chat web, mesma tabela bot_conversations) e gera um digest
estruturado salvo em system_memories.

Why: bot perdia o "fio" da conversa quando mensagens saíam da janela de
20. Síntese diária resolve: cada manhã o bot já entra sabendo o que
foi conversado ontem.

How to apply: rodado por cron diário (~22h SP). Output entra no snapshot
do system prompt.
"""
import logging
import os
from datetime import datetime, timedelta, date
from typing import Dict, Optional

import httpx

from database import get_db
from services.system_memory import save_system_memory

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"


SYNTHESIS_PROMPT = """Você está lendo as conversas do INTEL com o Renato nas últimas 24h.
Sua tarefa: produzir uma SÍNTESE estruturada que vai virar contexto pro bot
amanhã. Não é resumo cronológico — é leitura de coach do que importa.

FORMATO (siga exatamente):

## Síntese {periodo}

### Temas dominantes
- (1 a 4 temas que apareceram no dia, descritos em 1 linha cada)

### Decisões tomadas
- (decisões concretas que ficaram registradas — cada uma em 1 linha)

### Estados notados
- (observações sobre como ele estava: tensão, clareza, fadiga, energia, ambivalência)

### Compromissos surgiram
- (coisas que ele disse que ia fazer, com prazo se houver)

### Padrões/recorrências
- (coisas que se repetem nas conversas — pode ficar vazio se não houver sinal)

### Itens pra possível projeto/task
- (sugestões de virar projeto/task formal — máximo 3, ou "(nenhum)")

### Aberto pra próxima conversa
- (perguntas que ficaram pendentes ou que vale o coach trazer de volta amanhã)

REGRAS:
- Português, prosa direta. Sem emoji. Sem decoração.
- Se um campo não tem material, escreva "(nenhum)" — não invente.
- Foco em SINAL: o que importa, não o que foi dito.
- Não repita literalmente o que o Renato falou — interprete.
- Tamanho ideal: 200-400 palavras no total.

CONVERSAS DAS ÚLTIMAS 24H:
{transcript}
"""


def _load_recent_conversations(hours: int = 24) -> list:
    """Load bot_conversations from last N hours."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT role, content, created_at
                FROM bot_conversations
                WHERE created_at > NOW() - INTERVAL '%s hours'
                  AND role IN ('user', 'assistant')
                  AND content IS NOT NULL
                  AND length(content) > 0
                ORDER BY created_at ASC
                """,
                (hours,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"_load_recent_conversations error: {e}")
        return []


def _format_transcript(rows: list) -> str:
    """Format conversation rows as a readable transcript."""
    lines = []
    for r in rows:
        ts = r["created_at"].strftime("%H:%M") if r.get("created_at") else "??:??"
        role = "Renato" if r["role"] == "user" else "INTEL"
        content = (r["content"] or "").strip()
        if not content:
            continue
        # Truncate very long messages
        if len(content) > 1500:
            content = content[:1500] + "...[truncado]"
        lines.append(f"[{ts}] {role}: {content}")
    return "\n\n".join(lines)


async def run_daily_synthesis(hours: int = 24) -> Dict:
    """Read last N hours of conversations, generate synthesis, save it.

    Returns: { status, memory_id?, conversations_count, error? }
    """
    if not ANTHROPIC_API_KEY:
        return {"status": "error", "error": "ANTHROPIC_API_KEY not set"}

    rows = _load_recent_conversations(hours)
    if not rows:
        return {"status": "skipped", "reason": "no conversations in window", "conversations_count": 0}

    # Skip if too few messages — not worth a digest
    if len(rows) < 4:
        return {"status": "skipped", "reason": "too few messages", "conversations_count": len(rows)}

    transcript = _format_transcript(rows)
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(hours=hours)
    periodo = f"{start_dt.strftime('%d/%m %H:%M')} → {end_dt.strftime('%d/%m %H:%M')}"

    prompt = SYNTHESIS_PROMPT.format(periodo=periodo, transcript=transcript)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            logger.error(f"Synthesis Claude error: {resp.status_code} {resp.text[:300]}")
            return {"status": "error", "error": f"Claude {resp.status_code}"}

        result = resp.json()
        synthesis_text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                synthesis_text += block["text"]
        synthesis_text = synthesis_text.strip()

        if not synthesis_text:
            return {"status": "error", "error": "empty synthesis"}

        titulo = f"Síntese {start_dt.strftime('%d/%m')} → {end_dt.strftime('%d/%m')}"
        mid = save_system_memory(
            titulo=titulo,
            conteudo=synthesis_text,
            tipo="sintese_diaria",
            fonte="sintese",
            referencia_inicio=start_dt.date(),
            referencia_fim=end_dt.date(),
            tags=["sintese", "auto"],
        )

        return {
            "status": "success",
            "memory_id": mid,
            "conversations_count": len(rows),
            "transcript_chars": len(transcript),
            "synthesis_chars": len(synthesis_text),
        }
    except Exception as e:
        logger.exception(f"Synthesis error: {e}")
        return {"status": "error", "error": str(e)}
