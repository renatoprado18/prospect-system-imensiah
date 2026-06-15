"""
CoS EDITORIAL Agent — drafts + imagens + métricas LinkedIn (15/06/26).

Specialist #3 do swarm CoS. Roda 1x/dia (manhã), lê estado dos posts
editorial + métricas, e propõe ações via WA. Pode também chamar
`gsk image_generation` pra gerar capa quando post agendado sem imagem.

Sinais que detecta:
- Posts `scheduled` (= agendados) sem imagem → propor gerar (gsk image_generation)
- Posts `pending_approval` há >2 dias → propor revisar/dismiss
- Drafts acumulados (>10) → propor curadoria
- Última semana: métricas (impressões, engagement) caindo → alerta hipóteses
- Hot takes não viraram post há >7d → sugerir promover

Politica autonomia:
- Geração de imagem: Auto (é rascunho, Renato aprova publicação depois)
- Aprovacao/publicação: Propor SEMPRE (voz do Renato)
- Curadoria/dismiss em massa: Propor SEMPRE
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
GENSPARK_API_KEY = (os.getenv("GENSPARK_API_KEY") or "").strip()
MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 6
MAX_TOKENS_PER_ITER = 2500
DAILY_CAP_USD = float(os.getenv("COS_EDITORIAL_DAILY_CAP_USD", "0.75"))


def _load_editorial_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    now = datetime.now()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, status, tipo, article_title, titulo_adaptado, conteudo_adaptado,
                       data_publicacao::date as publica_em,
                       imagem_url IS NOT NULL AS tem_imagem,
                       prioridade, hashtags
                FROM editorial_posts
                WHERE status = 'scheduled'
                  AND data_publicacao BETWEEN NOW() AND NOW() + INTERVAL '14 days'
                ORDER BY data_publicacao ASC LIMIT 10
                """
            )
            state["scheduled_proximos"] = [
                {
                    "id": r["id"], "status": r["status"], "tipo": r["tipo"],
                    "title": (r["titulo_adaptado"] or r["article_title"] or "")[:120],
                    "publica_em": r["publica_em"].isoformat() if r["publica_em"] else None,
                    "tem_imagem": r["tem_imagem"],
                    "prioridade": r["prioridade"],
                    "conteudo_preview": (r["conteudo_adaptado"] or "")[:300],
                }
                for r in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT id, status, article_title, titulo_adaptado,
                       data_publicacao::date as publica_em,
                       imagem_url IS NOT NULL AS tem_imagem
                FROM editorial_posts
                WHERE status = 'pending_approval'
                ORDER BY data_publicacao ASC NULLS LAST LIMIT 10
                """
            )
            state["pending_approval"] = [
                {
                    "id": r["id"],
                    "title": (r["titulo_adaptado"] or r["article_title"] or "")[:120],
                    "publica_em": r["publica_em"].isoformat() if r["publica_em"] else None,
                    "tem_imagem": r["tem_imagem"],
                }
                for r in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT COUNT(*) as drafts_total,
                       COUNT(*) FILTER (WHERE atualizado_em > NOW() - INTERVAL '7 days') as drafts_7d
                FROM editorial_posts WHERE status = 'draft'
                """
            )
            r = cur.fetchone()
            state["drafts_summary"] = {
                "total": r["drafts_total"] if r else 0,
                "ultimos_7d": r["drafts_7d"] if r else 0,
            }

            # Hot takes nao virados em post >7d
            # Schema real: news_title, status='draft', created_at, editorial_post_id (FK reversa)
            try:
                cur.execute(
                    """
                    SELECT id, news_title, created_at::date as created
                    FROM hot_takes
                    WHERE status = 'draft'
                      AND created_at < NOW() - INTERVAL '7 days'
                      AND editorial_post_id IS NULL
                    ORDER BY created_at ASC LIMIT 5
                    """
                )
                state["hot_takes_orfaos"] = [
                    {"id": r["id"], "titulo": (r["news_title"] or "")[:120],
                     "criado_em": r["created"].isoformat() if r["created"] else None}
                    for r in cur.fetchall()
                ]
            except Exception as e:
                logger.warning(f"hot_takes query falhou: {e}")
                state["hot_takes_orfaos"] = []

            # Últimas métricas
            cur.execute(
                """
                SELECT id, periodo_inicio::date, periodo_fim::date,
                       impressoes_total, reacoes_total, comentarios_total,
                       posts_publicados
                FROM editorial_metrics_history
                ORDER BY periodo_inicio DESC LIMIT 3
                """
            )
            state["metricas_recentes"] = [dict(r) for r in cur.fetchall()]

            # Dedup: ultimas propostas Editorial 48h
            cur.execute(
                """
                SELECT id, content, created_at
                FROM bot_conversations
                WHERE role='assistant'
                  AND tool_calls->>'agent_label' = 'CoS Editorial'
                  AND created_at > NOW() - INTERVAL '48 hours'
                ORDER BY created_at DESC LIMIT 8
                """
            )
            state["recent_editorial_pushes"] = [
                {"id": r["id"], "preview": (r["content"] or "")[:250],
                 "ts": r["created_at"].isoformat() if r["created_at"] else None}
                for r in cur.fetchall()
            ]
    except Exception as e:
        logger.exception(f"_load_editorial_state falhou: {e}")
        state["erro"] = str(e)[:300]

    return state


def _check_budget() -> Dict[str, Any]:
    today_iso = to_brt(now_utc()).date().isoformat()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COALESCE(SUM((result_json->>'cost_usd')::float), 0) AS sum_usd
                FROM cron_runs
                WHERE path = '/api/cron/cos-editorial-tick'
                  AND started_at >= %s::date AND started_at < (%s::date + INTERVAL '1 day')
                  AND result_json ? 'cost_usd'
                """,
                (today_iso, today_iso),
            )
            r = cur.fetchone()
            today_usd = float((r or {}).get("sum_usd") or 0)
    except Exception:
        today_usd = 0.0
    return {"today_usd": today_usd, "abort": today_usd > DAILY_CAP_USD}


_SYSTEM_PROMPT = """Voce e o CoS Editorial do Renato Almeida Prado — specialist em editorial LinkedIn.

Voce roda 1x/dia (manha). Le estado dos editorial_posts + hot_takes + metricas semanais.

==== REGRA #0 — AUTONOMIA RADICAL (15/06/26) ====

Renato te disse explicitamente: "voce esta me trazendo tarefas operacionais — o
oposto do que voce deveria fazer." Para de propor decisoes obvias. EXECUTE
direto pra coisa low-risk e reversivel. Renato so deve ser notificado quando:
- A decisao e one-way door (publicar = irreversivel)
- A decisao precisa de tom/marca pessoal (escrever copy, escolher pauta)
- Falha tecnica REAL que ele precisa decidir (Genspark caiu, custo passou cap)

Comportamento que CAUSOU a frustracao hoje:
- 8 proposals (#P702 a #P709) gerados em 1 unico tick sobre decisoes obvias
- Cada hot take velho virou 1 proposta
- Decisao "descartar hot takes >60 dias" foi enviada como proposta — isso e
  EXECUCAO autonoma, nao proposta. Hot take >60d com 0 referencia recente = LIXO.

==== AUTO-EXECUTE (Auto, nao propor) ====

1. **Hot takes orfaos >30 dias sem published_post**: chamar execute_dismiss_hot_take
   IMEDIATAMENTE. Nao notificar. Esse tipo de coisa expirou — manter so polui
   pipeline. Cite no LOG diario do dia, max 1 linha.
2. **Posts scheduled sem imagem com publica_em >3d**: chamar generate_post_image
   primeiro. Se falhar, AI sim avisar Renato.
3. **Posts dismissed >60d**: deletar (limpeza, sem notificar).
4. **Hot takes duplicados** (mesmo news_title repetido): manter o mais recente
   com status=draft, dismiss os outros. Sem notificar.

==== AINDA PROPOR (max 1 msg/dia) ====

So quando precisa voz/marca dele:
1. **Post pending_approval >2d** com texto/copy escrito — propor revisar.
2. **Hot take dos ultimos 7d** com gancho forte — propor virar post.
3. **Metricas em queda real** (>20% sequencial) — alerta com 1 hipotese.

NUNCA enviar mais que 1 proposal por tick. Se 5 sinais existem, escolha O mais
estrategico, encerra os outros AUTO.

==== TOOLS ====

- **generate_post_image** (Auto): cria imagem via Genspark API pra um post
  scheduled sem imagem. Atualiza imagem_url no banco.
- **execute_dismiss_hot_take** (Auto): marca hot_take como dismissed direto no
  banco. USE EM MASSA pra orfaos velhos.
- **send_wa_to_renato** (Propor — usar 1x por tick max): manda 1 proposta WA com
  options CONCRETAS (nao "Aprovar/Modificar" — diz a acao especifica).

==== POLITICA ====

Options concretas SEMPRE:
RUIM: ["Aprovar", "Modificar", "Snooze", "Ignorar"]
BOM:  ["Revisa post #20 agora", "Reagenda pra 25/06", "Dismiss",
       "Descartar alerta"]

Max 1 mensagem por tick (CAI de 2 pra 1 — 15/06/26).
Dedup contra recent_editorial_pushes.
Domingo: nao agendar publicacao pra domingo.

Se nada novo OU so tem decisoes obvias (>30d, duplicado, scheduled OK), execute
e responda 1-2 linhas em texto sem tool call de send_wa.
"""


SENSOR_TOOLS = [
    {
        "name": "generate_post_image",
        "description": (
            "Gera imagem de capa pra um post agendado via Genspark API. "
            "Atualiza editorial_posts.imagem_url. POLITICA: Auto. Use pra posts "
            "com publica_em <= 7d sem imagem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "integer"},
                "prompt": {
                    "type": "string",
                    "description": "Prompt da imagem alinhado com o titulo/conteudo do post.",
                },
                "style": {
                    "type": "string",
                    "description": "Estilo visual (ex: 'minimalist professional', 'tech editorial', 'brazil tropical')",
                    "default": "minimalist professional",
                },
            },
            "required": ["post_id", "prompt"],
        },
    },
    {
        "name": "send_wa_to_renato",
        "description": "Manda proposta WA. Use options CONCRETAS sempre.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "action_hint": {"type": "string"},
                        },
                        "required": ["label"],
                    },
                },
                "urgency": {"type": "string", "enum": ["high", "medium", "low"], "default": "medium"},
                "proposed_action": {"type": "object"},
                "post_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["title", "summary"],
        },
    },
]


def _tool_generate_post_image(post_id: int, prompt: str, style: str = "minimalist professional") -> Dict[str, Any]:
    """Chama Genspark image_generation (endpoint /api/tool_cli/image_generation,
    NDJSON streaming) e atualiza editorial_posts.imagem_url."""
    if not GENSPARK_API_KEY:
        return {"success": False, "error": "GENSPARK_API_KEY ausente"}
    try:
        import httpx
        from services.audit_log import log as audit_log

        full_prompt = f"{prompt}, {style} style, editorial cover"
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(
                "https://www.genspark.ai/api/tool_cli/image_generation",
                headers={
                    "Authorization": f"Bearer {GENSPARK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"query": full_prompt},
            )
        if resp.status_code >= 400:
            return {"success": False, "error": f"Genspark HTTP {resp.status_code}: {resp.text[:300]}"}

        # NDJSON streaming: várias linhas JSON. A última com status=ok contém o resultado.
        last_payload = None
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("status") == "ok":
                last_payload = obj
        if not last_payload:
            return {"success": False, "error": f"Genspark sem status=ok: {resp.text[:300]}"}

        gen = (last_payload.get("data", {}) or {}).get("generated_images") or []
        if not gen:
            return {"success": False, "error": "Genspark resposta sem generated_images"}
        first = gen[0]
        if first.get("status") != "SUCCESS":
            reason = first.get("failure_reason") or "sem failure_reason"
            return {"success": False, "error": f"Genspark gen FAILURE: {reason}"}
        urls_nowm = first.get("image_urls_nowatermark") or []
        urls_wm = first.get("image_urls") or []
        image_url = (urls_nowm[0] if urls_nowm else None) or (urls_wm[0] if urls_wm else None)
        if not image_url:
            return {"success": False, "error": "Genspark sem image_urls"}

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE editorial_posts SET imagem_url=%s, atualizado_em=NOW() WHERE id=%s RETURNING id, status",
                (image_url, post_id),
            )
            row = cur.fetchone()
            conn.commit()

        aid = audit_log(
            "cos_editorial.generate_post_image",
            entity_type="editorial_post",
            entity_id=post_id,
            actor="cos_editorial",
            details={"prompt": full_prompt[:300], "image_url": image_url[:200]},
        )
        return {
            "success": True,
            "result": {"post_id": post_id, "image_url": image_url},
            "audit_log_id": aid,
        }
    except Exception as e:
        logger.exception(f"generate_post_image falhou: {e}")
        return {"success": False, "error": str(e)}


def _exec_tool(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if name == "generate_post_image":
        return _tool_generate_post_image(
            post_id=int(params["post_id"]),
            prompt=params.get("prompt", ""),
            style=params.get("style", "minimalist professional"),
        )
    if name == "send_wa_to_renato":
        try:
            from services.cos_sensor import _tool_send_wa_to_renato
            pa = dict(params.get("proposed_action") or {})
            pa.setdefault("agent", "cos_editorial")
            if params.get("post_ids"):
                pa["post_ids"] = params["post_ids"]
            return _tool_send_wa_to_renato(
                title=params.get("title", ""),
                summary=params.get("summary", ""),
                options=params.get("options"),
                urgency=params.get("urgency", "medium"),
                contact_id=None,
                context_link="editorial",
                proposed_action=pa,
                agent_label="CoS Editorial",
            )
        except Exception as e:
            return {"success": False, "error": str(e)}
    return {"success": False, "error": f"tool desconhecida: {name}"}


def tick() -> Dict[str, Any]:
    started = time.time()
    budget = _check_budget()
    if budget["abort"]:
        return {"status": "aborted_budget", "today_usd": budget["today_usd"]}

    if not ANTHROPIC_API_KEY:
        return {"status": "skipped", "reason": "no_api_key"}
    try:
        import anthropic
    except ImportError:
        return {"status": "error", "reason": "anthropic_sdk_missing"}

    state = _load_editorial_state()
    now_brt_iso = to_brt(now_utc()).isoformat()

    user_prompt = (
        f"Tick CoS Editorial em {now_brt_iso}.\n\n"
        f"Estado editorial:\n```json\n"
        f"{json.dumps(state, default=str, ensure_ascii=False, indent=2)[:12000]}\n"
        f"```\n\n"
        f"Analise os 4 sinais (scheduled sem img, pending_approval >2d, hot_takes orfaos, "
        f"metricas em queda). Gere imagem se aplicavel (Auto). Propor via WA com options "
        f"concretas. Max 2 mensagens. Dedup."
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_param = [
        {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    total_input = total_output = cache_creation = cache_read = 0
    tool_calls_log: List[Dict[str, Any]] = []
    final_text = ""
    last_stop_reason = None
    error_msg = None

    try:
        for _ in range(MAX_ITERATIONS):
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS_PER_ITER,
                system=system_param,
                tools=SENSOR_TOOLS,
                messages=messages,
            )
            usage = resp.usage
            total_input += getattr(usage, "input_tokens", 0) or 0
            total_output += getattr(usage, "output_tokens", 0) or 0
            cache_creation += getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            last_stop_reason = resp.stop_reason

            messages.append({"role": "assistant", "content": resp.content})

            tu_blocks = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    txt = (getattr(b, "text", "") or "").strip()
                    if txt:
                        final_text = txt

            if resp.stop_reason != "tool_use" or not tu_blocks:
                break

            tool_results = []
            for tu in tu_blocks:
                tname = getattr(tu, "name", "")
                tin = dict(getattr(tu, "input", {}) or {})
                tid = getattr(tu, "id", "")
                tres = _exec_tool(tname, tin)
                tool_calls_log.append({"tool": tname, "input": tin, "result": tres})
                result_str = json.dumps(tres, default=str, ensure_ascii=False)[:3000]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": result_str,
                })
            messages.append({"role": "user", "content": tool_results})

    except Exception as e:
        error_msg = f"loop_failed: {e}"
        logger.exception("cos_editorial.tick loop falhou")

    duration_ms = int((time.time() - started) * 1000)
    cost_usd = (
        (total_input * 3.0 / 1_000_000)
        + (total_output * 15.0 / 1_000_000)
        + (cache_creation * 3.75 / 1_000_000)
        + (cache_read * 0.30 / 1_000_000)
    )

    return {
        "status": "error" if error_msg and not tool_calls_log else ("success" if not error_msg else "partial"),
        "tool_count": len(tool_calls_log),
        "tool_calls": tool_calls_log,
        "tokens": {
            "input": total_input, "output": total_output,
            "cache_creation": cache_creation, "cache_read": cache_read,
        },
        "cost_usd": round(cost_usd, 4),
        "duration_ms": duration_ms,
        "last_stop_reason": last_stop_reason,
        "final_text": (final_text or "")[:500],
        "error_message": error_msg,
    }


def tick_safe() -> Dict[str, Any]:
    import traceback
    try:
        from services.audit_log import log as audit_log
    except Exception:
        audit_log = None

    try:
        result = tick()
        if audit_log:
            audit_log(
                "cos_editorial.tick",
                actor="cos_editorial",
                details={
                    "status": result.get("status"),
                    "tool_count": result.get("tool_count"),
                    "cost_usd": result.get("cost_usd"),
                    "error": result.get("error_message"),
                },
            )
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception(f"cos_editorial.tick_safe crashed: {e}")
        if audit_log:
            audit_log(
                "cos_editorial.tick_error",
                actor="cos_editorial",
                details={"error": str(e), "type": type(e).__name__, "traceback": tb[:3000]},
            )
        return {
            "status": "error", "error": str(e),
            "error_type": type(e).__name__,
            "traceback": tb.splitlines()[-12:],
        }
