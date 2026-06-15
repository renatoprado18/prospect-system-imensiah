"""
Tonha Brain — Fase 2A da rebuild (autonomous loop).

UMA cabeca: Sonnet 4.6 + extended thinking. Le `signals WHERE status='open'`,
decide cada um com tool loop, grava em `tonha_decisions`.

Modo Reactive (webhook WA / chat web) vira Fase 2B.

Default em SHADOW MODE (TONHA_SHADOW_MODE=1) — toda send_message/update_record
vira draft pra Renato revisar.

Ver docs/ARCHITECTURE_REBUILD.md sec 4 (camada 2).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import anthropic

from database import get_db
from services.tonha_tools import TOOLS, dispatch
from services.tz import now_utc

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

MAX_ITERATIONS = 8           # tool loop por signal
THINKING_BUDGET = 4000       # extended thinking tokens
MAX_TOKENS = THINKING_BUDGET + 4096  # texto+tools depois do thinking
MAX_SIGNALS_PER_TICK = 30


SYSTEM_PROMPT = """Você é a Tonha — Chief of Staff IA do Renato Almeida Prado.

# IDENTIDADE
- Matriarca tranquila, evoca a vó Margarida do Renato. Tom direto, sem ruído, sem servilismo.
- Não usa emoji. Não diz "Anotado". Não usa cabeçalho "🤖 CoS Patrol".
- Português Brasil com acentos completos.

# OS 6 TRABALHOS DA CoS (priorize nesta ordem)
1. PROTEGER ATENÇÃO — silencie ruído. 95% dos signals devem virar `silence` ou `auto_execute` sem incomodar Renato.
2. SUSTENTAR MEMÓRIA — atualize estado (signals, projetos, delegations) pra que nada caia entre cadeiras.
3. EXECUTAR MANDATO — aja com autonomia em coisas que ele já decidiu. Não pergunte o óbvio.
4. SINTETIZAR SINAL — quando agrupar, agrupe. Inbox digest 7h, RACI semanal 2ª 7h, briefing.
5. PROJETAR VOZ — drafts em tom dele, não corporativo.
6. ESCALAR COM SUBSTÂNCIA — quando precisar do Renato (2%), traga decisão estruturada com 1-2 opções e sua recomendação.

# REGRA #-1 — ANTI-ALUCINAÇÃO DE FERRAMENTAS
TOOLS REAIS DISPONÍVEIS (e SOMENTE estas):
  1. search_context — busca em contacts/projects/tasks/signals/delegations
  2. send_message — manda WA ou email (em shadow mode = vira draft)
  3. update_record — UPDATE em tasks/projects/delegations/signals/weekly_raci_renato
  4. delegate — cria delegation pra Andressa/João Piccino/Priscila/dev/evaluator/collector
  5. decide_and_log — registra decisão + marca signal resolved/dismissed

NÃO EXISTEM: web_search, fetch_url, execute_intel, query_intel, query_conselhoos,
delegate_to_claude_code (use delegate(to='dev') em vez), gmail_create_draft (use send_message).
Se precisar de algo fora desse catálogo, decide_and_log com type=escalate explicando o gap.

# AUTONOMY POLICY (não viola)
- 95% silence/auto_execute: hot take velho 30d → dismiss; task duplicada → auto_execute fechar; projeto stale → update_record status='paused'; aniversario de tier baixo sem histórico próximo → silence.
- 3% draft_and_send: cobrar pendência de delegado pelo collector; rascunhar resposta a email VIP com tom já mapeado.
- 2% escalate: decisão estratégica real, conflito que precisa ele decidir, alerta financial 95%+.

NUNCA escale signal sem ler contexto completo. NUNCA invente fatos sobre contato (cargo, papel, família). Se faltar dado, usa search_context primeiro.

# WORKFLOW POR SIGNAL
1. Leia o contexto JSONB completo do signal.
2. Se precisar de mais dado (contato, projeto, delegations relacionadas), USE search_context.
3. Decida: auto_execute / draft_and_send / escalate / silence / delegate.
4. Execute a ação (send_message com shadow ou update_record).
5. SEMPRE termine com decide_and_log — sem isso, signal fica orfão.

# SHADOW MODE
Estamos em shadow. send_message vira draft. update_record vira no-op com log. delegate cria row mas não manda. Renato vai revisar tonha_decisions pelas primeiras semanas. Isso NÃO muda sua autonomia interna — decida com confiança como se fosse real.

# EXEMPLOS POR TIPO DE SIGNAL
- raci_vencido (urg 5-10): primeiro check se Renato é Responsável. Se R != Renato, delegate(to=collector) cobrança pra responsavel_r. Se R = Renato e urg >= 8, escalate. Senão, draft_and_send pra cobrança.
- editorial_post_sem_imagem (urg 5-9): se horas_ate < 6, escalate. Senão, delegate(to='dev', task='gerar imagem para post X', deadline=horas_ate).
- editorial_hot_take_velho (urg 3-7): silence + decide_and_log "expirado, não virou post".
- relacionamento_aniversario_hoje (urg 8): se tags contém "familia" ou "padrinho", escalate (Renato decide tom). Senão draft_and_send mensagem genérica.
- operational_task_vencida (urg 3-9): se ai_generated e sem evidência de execução em conversas, update_record status='cancelled' + silence. Se urg >= 8 e Renato dono, escalate. Senão silence.
- gov_projetos_duplicados: escalate sempre (decisão dele).
- inbox_atencao (urg 6-9): se urg >= 8, escalate com summary. Se 6-7 e contato VIP profissional, draft_and_send resposta curta.
- inbox_digest (urg 3-5): silence (já vai no briefing 7h).
- delegacao_vencida: collector cobra → send_message (draft em shadow) pra delegado.
- delegacao_sem_followup: collector cobra suave.
"""


def _load_signals(limit: int = MAX_SIGNALS_PER_TICK) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, tipo, urgencia, contexto, detector, criado_em
            FROM signals
            WHERE status = 'open'
            ORDER BY urgencia DESC, criado_em ASC
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def _signal_user_prompt(signal: Dict[str, Any]) -> str:
    return (
        f"# SIGNAL #{signal['id']}\n"
        f"- tipo: {signal['tipo']}\n"
        f"- urgência: {signal['urgencia']}/10\n"
        f"- detector: {signal['detector']}\n"
        f"- emitido: {signal['criado_em'].isoformat() if signal['criado_em'] else 'agora'}\n"
        f"- contexto:\n```json\n{signal['contexto']}\n```\n\n"
        f"Decida o que fazer. Use search_context se faltar dado. "
        f"TERMINE com decide_and_log."
    )


def _run_one_signal(client: anthropic.Anthropic, signal: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Loop de tool use pra um signal só."""
    user_prompt = _signal_user_prompt(signal)
    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    total_in = total_out = total_thinking = 0
    iterations = 0
    decision_logged = False
    tool_calls_log: List[Dict[str, Any]] = []
    final_text = ""

    for it in range(MAX_ITERATIONS):
        iterations += 1
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            logger.exception(f"signal {signal['id']} brain crashed at iter {it}")
            return {
                "signal_id": signal["id"],
                "error": str(e)[:300],
                "iterations": iterations,
                "decision_logged": decision_logged,
            }

        usage = resp.usage
        total_in += getattr(usage, "input_tokens", 0) or 0
        total_out += getattr(usage, "output_tokens", 0) or 0

        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                t = (getattr(b, "text", "") or "").strip()
                if t:
                    final_text = t
            elif getattr(b, "type", None) == "thinking":
                total_thinking += len(getattr(b, "thinking", "") or "")

        if not tool_uses:
            break

        tool_results = []
        for tu in tool_uses:
            tname = tu.name
            tinput = tu.input or {}
            res = dispatch(tname, tinput, ctx)
            if tname == "decide_and_log":
                decision_logged = True
            tool_calls_log.append({"tool": tname, "input_keys": list(tinput.keys()), "ok": res.get("ok")})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": str(res)[:3000],
            })
        messages.append({"role": "user", "content": tool_results})

        if resp.stop_reason == "end_turn":
            break

    return {
        "signal_id": signal["id"],
        "iterations": iterations,
        "decision_logged": decision_logged,
        "tool_calls": tool_calls_log,
        "tokens": {"in": total_in, "out": total_out, "thinking_chars": total_thinking},
        "final_text": final_text[:300],
    }


# ============================================================================
# Modo Reactive — Fase 2B (chat web + WA self via flag TONHA_REACTIVE_TARGETS)
# ============================================================================

REACTIVE_PROMPT_SUFFIX = """

# MODO REACTIVE
Renato falou agora. Responda diretamente.
- Se a mensagem é uma pergunta simples (status, fatos), use search_context, responda curto.
- Se é uma instrução ("manda X pra Y", "marca tal task"), use send_message / update_record / delegate.
- Se ela menciona signal aberto, use search_context scope='signals' query=<keyword>.
- Se não souber, escale com substância (1-2 opções) — nunca invente fato.
- Tom: matriarca direta, sem emoji, sem "Anotado", sem "🤖 CoS".
- decide_and_log NÃO é obrigatório em reactive — só se você de fato resolveu um signal.
- A última mensagem da história é a do Renato AGORA. Responda a ela."""


def is_reactive_enabled(channel: str, phone: Optional[str] = None) -> bool:
    """Flag env: none|chat|wa|all. Compatible com bot dispatcher."""
    targets = (os.getenv("TONHA_REACTIVE_TARGETS") or "none").strip().lower()
    if targets == "none" or not targets:
        return False
    if targets == "all":
        return True
    if targets == "chat" and channel == "chat":
        return True
    if targets == "wa" and channel in ("whatsapp", "wa"):
        return True
    return False


async def run_reactive(
    message: str,
    channel: str = "chat",
    phone: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Brain reactive entry. Usa mesmo tool loop do autonomous.
    Retorna texto final pra enviar ao Renato.

    `history` opcional: lista [{role: 'user'|'assistant', content: str}, ...]
    Usado pra dar contexto multi-turn.
    """
    if not ANTHROPIC_API_KEY:
        return "Brain offline — ANTHROPIC_API_KEY ausente."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    sys_prompt = SYSTEM_PROMPT + REACTIVE_PROMPT_SUFFIX

    messages: List[Dict[str, Any]] = []
    if history:
        for h in history[-20:]:
            role = h.get("role")
            content = h.get("content") or ""
            if role in ("user", "assistant") and content.strip():
                messages.append({"role": role, "content": content[:4000]})
    messages.append({"role": "user", "content": message})

    ctx = {
        "mode": "reactive",
        "triggered_by": f"{channel}:{phone or 'unknown'}",
        "shadow": (os.getenv("TONHA_SHADOW_MODE") or "1").strip() != "0",
        "started_at": now_utc().isoformat(),
    }

    total_in = total_out = 0
    iterations = 0
    final_text = ""
    tool_calls_log: List[Dict[str, Any]] = []

    for it in range(MAX_ITERATIONS):
        iterations += 1
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
                system=sys_prompt,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            logger.exception(f"reactive brain crashed at iter {it}")
            return f"Tonha tropeçou: {str(e)[:150]}"

        usage = resp.usage
        total_in += getattr(usage, "input_tokens", 0) or 0
        total_out += getattr(usage, "output_tokens", 0) or 0

        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                t = (getattr(b, "text", "") or "").strip()
                if t:
                    final_text = t

        if not tool_uses:
            break

        tool_results = []
        for tu in tool_uses:
            res = dispatch(tu.name, tu.input or {}, ctx)
            tool_calls_log.append({"tool": tu.name, "ok": res.get("ok")})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": str(res)[:3000],
            })
        messages.append({"role": "user", "content": tool_results})

        if resp.stop_reason == "end_turn":
            break

    # Log decision summary pra audit (modo reactive)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            import json as _json
            cur.execute("""
                INSERT INTO tonha_decisions (
                    decision_type, decision_summary, action_taken, mode, triggered_by
                )
                VALUES ('auto_execute', %s, %s::jsonb, 'reactive', %s)
            """, (
                f"reactive reply ({iterations} iter)",
                _json.dumps({
                    "user_message_preview": message[:200],
                    "tool_calls": tool_calls_log,
                    "tokens": {"in": total_in, "out": total_out},
                }),
                ctx["triggered_by"],
            ))
            conn.commit()
    except Exception as _e:
        logger.warning(f"reactive log falhou: {_e}")

    return final_text or "Sem resposta gerada."


def run_autonomous_tick(triggered_by: str = "cron_loop", limit: int = MAX_SIGNALS_PER_TICK) -> Dict[str, Any]:
    """Pull signals -> Brain decide cada um -> grava decisions."""
    if not ANTHROPIC_API_KEY:
        return {"ok": False, "error": "ANTHROPIC_API_KEY ausente"}

    t0 = time.time()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    signals = _load_signals(limit=limit)

    ctx_base = {
        "mode": "autonomous",
        "triggered_by": triggered_by,
        "shadow": (os.getenv("TONHA_SHADOW_MODE") or "1").strip() != "0",
        "started_at": now_utc().isoformat(),
    }

    results: List[Dict[str, Any]] = []
    for s in signals:
        ctx = {**ctx_base, "signal_id": s["id"]}
        r = _run_one_signal(client, s, ctx)
        results.append(r)

    n_decided = sum(1 for r in results if r.get("decision_logged"))
    n_errors = sum(1 for r in results if r.get("error"))
    duration_ms = int((time.time() - t0) * 1000)

    return {
        "ok": True,
        "shadow_mode": ctx_base["shadow"],
        "signals_total": len(signals),
        "signals_decided": n_decided,
        "signals_errored": n_errors,
        "duration_ms": duration_ms,
        "triggered_by": triggered_by,
        "details": results[:15],
    }
