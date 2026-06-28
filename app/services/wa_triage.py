"""
F3.1 — WhatsApp triage shadow classifier (28/06/2026).

Sweep periódico (4h via cron Vercel) das WA msgs incoming nao classificadas.
Cada msg lida com janela de 5 turnos prior pra contexto. Batched Claude
call com prompt cache: 1 system prompt + N msgs por call (custo amortizado).

Output: registra em wa_triage (status='shadow') sem criar action_proposal.

Política por contact_circulo:
- 1 (familia proxima): default must_read, alta sensibilidade
- 2 (proximo): default follow_up
- 3 (profissional ativo): default follow_up
- 4 (network amplo): default silent (a menos que sinal)
- 5 (cold): default archive
- NULL: default follow_up
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from database import get_db
from services.tz import now_utc

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-6"
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip().rstrip("\\n").strip()

# Pricing Sonnet 4.6 (USD per milhao tokens). Atualizar com model bump.
PRICE_INPUT_PER_M = 3.00
PRICE_OUTPUT_PER_M = 15.00
PRICE_CACHE_READ_PER_M = 0.30
PRICE_CACHE_WRITE_PER_M = 3.75

DEFAULT_WINDOW_HOURS = 4
THREAD_WINDOW_TURNS = 5
MAX_MSGS_PER_BATCH = 20

SYSTEM_PROMPT = """\
Voce e um classificador de mensagens WhatsApp incoming do Renato Almeida Prado.

Pra cada msg recebe:
- conteudo: texto da msg
- contato: nome, apelido, circulo (1=familia, 2=proximo, 3=trabalho ativo, 4=network, 5=cold), cargo, empresa
- janela_prior: ate 5 turnos anteriores da mesma conversa (dir=incoming/outgoing)

CLASSIFICATION (uma das 4):
- must_read: exige atencao manual em breve. Decisao explicita, pergunta direta, info critica. Priority 7-10.
- follow_up: provavel acao mas nao critica. Pode aguardar 24-48h. Cobrança suave, status update. Priority 4-6.
- silent: informacional, nao exige resposta. Confirmação, agradecimento, FYI. Priority 1-3.
- archive: broadcast/spam/forward/social devocional/cumprimento sem destinatario. Pode passar batido. Priority 1.

INTENT (uma): pergunta | combinacao | broadcast | social | informacao | outro

REGRAS CRITICAS:
1. Se janela_prior mostra que Renato JA RESPONDEU apos a msg (outgoing depois de incoming), classification=silent (ja tratado).
2. Broadcast (mesma msg pra muitos / devocional / forward / "PÃO DA PALAVRA" / corrente) = archive.
3. Familia (circulo=1) com pergunta concreta = must_read. Família com broadcast = archive.
4. Cold (circulo=5) com pergunta vaga = silent (alto ruido).
5. NUNCA marque must_read sem evidencia clara — em duvida, preferir follow_up.
6. Mensagem <10 chars que e so emoji/agradecimento = silent.

OUTPUT: JSON array, 1 objeto por input. Schema:
[{"msg_id": int, "classification": "must_read|follow_up|silent|archive", "intent": "...", "priority": 1-10, "confidence": 0.0-1.0, "reasoning": "max 200 chars"}]

Retorne SOMENTE o array JSON, sem texto antes/depois.
"""


def _compute_cost(usage_in: int, usage_out: int, cache_read: int = 0, cache_create: int = 0) -> float:
    return (
        usage_in * PRICE_INPUT_PER_M
        + usage_out * PRICE_OUTPUT_PER_M
        + cache_read * PRICE_CACHE_READ_PER_M
        + cache_create * PRICE_CACHE_WRITE_PER_M
    ) / 1_000_000


def _fetch_incoming_msgs(window_hours: int) -> List[Dict]:
    """WA incoming ultimas N horas, AINDA nao classificadas."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.conversation_id, m.contact_id, m.conteudo, m.enviado_em,
                   c.nome AS contact_nome, c.apelido, c.circulo, c.cargo, c.empresa
            FROM messages m
            JOIN conversations conv ON conv.id = m.conversation_id
            LEFT JOIN contacts c ON c.id = m.contact_id
            LEFT JOIN wa_triage wt ON wt.message_id = m.id
            WHERE conv.canal = 'whatsapp'
              AND m.direcao = 'incoming'
              AND m.enviado_em > NOW() - (%s || ' hours')::interval
              AND wt.id IS NULL
              AND m.conteudo IS NOT NULL
              AND LENGTH(TRIM(m.conteudo)) > 0
            ORDER BY m.contact_id, m.enviado_em
            LIMIT %s
            """,
            (window_hours, MAX_MSGS_PER_BATCH * 5),
        )
        return [dict(r) for r in cur.fetchall()]


def _fetch_thread_window(conversation_id: int, before_ts: datetime, turns: int = THREAD_WINDOW_TURNS) -> List[Dict]:
    """Pega ultimos N turnos da conversa ANTES da msg. Ordem cronologica."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT direcao, conteudo, enviado_em
            FROM messages
            WHERE conversation_id = %s
              AND enviado_em < %s
              AND conteudo IS NOT NULL
            ORDER BY enviado_em DESC
            LIMIT %s
            """,
            (conversation_id, before_ts, turns),
        )
        return list(reversed([dict(r) for r in cur.fetchall()]))


def _format_msg_for_prompt(msg: Dict, window: List[Dict]) -> Dict:
    return {
        "msg_id": msg["id"],
        "conteudo": (msg.get("conteudo") or "")[:1000],
        "contato": {
            "nome": msg.get("contact_nome") or "Desconhecido",
            "apelido": msg.get("apelido"),
            "circulo": msg.get("circulo"),
            "cargo": msg.get("cargo"),
            "empresa": msg.get("empresa"),
        },
        "janela_prior": [
            {"dir": w["direcao"], "txt": (w["conteudo"] or "")[:300]}
            for w in window
        ],
    }


def _parse_claude_array(text: str) -> Optional[List[Dict]]:
    """Robust JSON array parser. Lida com code fences e texto extra."""
    text = text.strip()
    if text.startswith("```"):
        lines = [ln for ln in text.split("\n") if not ln.startswith("```")]
        text = "\n".join(lines).strip()
    # Acha primeiro `[` e ultimo `]` pra ser permissivo
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        result = json.loads(text[start:end + 1])
        return result if isinstance(result, list) else None
    except json.JSONDecodeError:
        return None


def _call_claude_batch(formatted: List[Dict]) -> Optional[Dict]:
    """Batch Claude call. Retorna {classifications, usage, cost_usd}."""
    if not formatted or not ANTHROPIC_API_KEY:
        return None

    user_content = (
        "Mensagens a classificar:\n\n"
        + json.dumps(formatted, ensure_ascii=False, indent=2)
        + "\n\nClassifique cada msg. Retorne array JSON."
    )

    try:
        with httpx.Client() as client:
            r = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 4000,
                    "system": [
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    "messages": [{"role": "user", "content": user_content}],
                },
                timeout=120.0,
            )
    except Exception as e:
        logger.error(f"wa_triage Claude call exception: {e}")
        return None

    if r.status_code != 200:
        logger.error(f"wa_triage Claude HTTP {r.status_code}: {r.text[:300]}")
        return None

    data = r.json()
    text = data["content"][0]["text"]
    classifications = _parse_claude_array(text)
    if classifications is None:
        logger.error(f"wa_triage JSON parse failed. Response: {text[:500]!r}")
        return None

    usage = data.get("usage", {})
    cost = _compute_cost(
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0),
    )

    return {"classifications": classifications, "usage": usage, "cost_usd": cost}


def _persist_classification(
    msg: Dict, cls: Dict, batch_id: str, window_size: int,
    batch_usage: Dict, share_usage_first: bool,
) -> int:
    """INSERT em wa_triage. batch_usage anexado SO na primeira msg do batch
    (share_usage_first=True), zerado nas outras pra evitar dupla contagem."""
    if share_usage_first:
        in_tok = batch_usage.get("input_tokens", 0)
        out_tok = batch_usage.get("output_tokens", 0)
        cache_r = batch_usage.get("cache_read_input_tokens", 0)
        cache_w = batch_usage.get("cache_creation_input_tokens", 0)
    else:
        in_tok = out_tok = cache_r = cache_w = 0

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO wa_triage (
                message_id, conversation_id, contact_id, contact_circulo,
                classification, intent, priority, ai_confidence, reasoning,
                thread_window_size,
                llm_input_tokens, llm_output_tokens,
                llm_cache_read_tokens, llm_cache_creation_tokens,
                batch_id, trigger_source, status, processed_em
            ) VALUES (%s, %s, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s,
                      %s, %s, %s, %s,
                      %s, %s, 'shadow', NOW())
            ON CONFLICT (message_id) DO NOTHING
            RETURNING id
            """,
            (
                msg["id"], msg["conversation_id"], msg["contact_id"], msg.get("circulo"),
                cls.get("classification"), cls.get("intent"),
                int(cls.get("priority") or 5),
                float(cls.get("confidence") or 0.0),
                (cls.get("reasoning") or "")[:500],
                window_size,
                in_tok, out_tok, cache_r, cache_w,
                batch_id, "sweep_4h",
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return row["id"] if row else 0


def sweep_wa_triage(window_hours: int = DEFAULT_WINDOW_HOURS) -> Dict:
    """Sweep entry. Pega msgs incoming nao classificadas, batched Claude call,
    persist shadow. Retorna stats."""
    batch_id = f"sweep_{now_utc().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    msgs = _fetch_incoming_msgs(window_hours)
    if not msgs:
        return {"batch_id": batch_id, "msgs_processed": 0, "skipped": "no_incoming"}

    msgs = msgs[:MAX_MSGS_PER_BATCH]

    # Pra cada msg: cacheia janela (1 query por msg, 1x soh)
    windows = {m["id"]: _fetch_thread_window(m["conversation_id"], m["enviado_em"]) for m in msgs}
    formatted = [_format_msg_for_prompt(m, windows[m["id"]]) for m in msgs]

    result = _call_claude_batch(formatted)
    if not result:
        return {"batch_id": batch_id, "msgs_processed": 0, "error": "claude_call_failed"}

    cls_by_id = {c.get("msg_id"): c for c in result["classifications"] if c.get("msg_id")}

    persisted = 0
    by_class: Dict[str, int] = {}
    first = True
    for msg in msgs:
        cls = cls_by_id.get(msg["id"])
        if not cls:
            logger.warning(f"wa_triage: msg #{msg['id']} sem classification do LLM")
            continue
        wt_id = _persist_classification(
            msg, cls, batch_id, len(windows[msg["id"]]),
            result["usage"], share_usage_first=first,
        )
        if wt_id:
            persisted += 1
            first = False
            k = cls.get("classification", "?")
            by_class[k] = by_class.get(k, 0) + 1

    return {
        "batch_id": batch_id,
        "window_hours": window_hours,
        "msgs_found": len(msgs),
        "msgs_persisted": persisted,
        "by_class": by_class,
        "usage": result["usage"],
        "cost_usd": round(result["cost_usd"], 6),
    }
