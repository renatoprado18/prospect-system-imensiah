"""
CoS Investigator Agent — Onda 2 (10/jun/2026).

LLM agent (Anthropic Sonnet 4.6 + tool use) que descobre quem é relevante
a partir das pendências do dia e investiga sob demanda. Roda 7h10 BRT
(50min antes do briefing 8h).

Fluxo:
1. Gera cycle_id (YYYY-MM-DD-morning) se não passado.
2. Coleta contexto inicial determinístico (agenda hoje+amanhã, overdue
   top 10, propostas top 5, CoS config + status político).
3. Monta system prompt com:
   - Identidade CoS executor
   - CoS config (5 frentes + políticas + mandato)
   - Contrato de fidelidade (não inventa, só age sobre dados de tools)
   - Mandato (rascunhar quando contato relevante aparece, ignorar cold vendor)
   - Bound (max 15 iterations)
4. Loop tool use até stop_reason != tool_use, com:
   - Prompt caching no system prompt (não muda entre iterations)
   - Audit log de cada tool call
5. Retorna estatísticas + popula cos_briefing_items.

O briefing 8h lê os items via compose_briefing_from_items() em briefings.py.

Custo estimado por ciclo: 5-15k input tokens + 1-3k output. Com cache
hit (~90% saving prefix), ~$0.02-0.05 por ciclo @ Sonnet 4.6.
"""

from __future__ import annotations

import json
from services import llm
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from database import get_db
from services.cos_tools import (
    COS_TOOLS,
    execute_tool,
    get_overdue_tasks_raci_aware,
    get_task_blocking_status,
    is_renato_responsible,
)
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
COS_INVESTIGATOR_MODEL = llm.BALANCED
MAX_ITERATIONS = 15
MAX_TOKENS_PER_ITER = 4096

# Budget caps (P.6 + L.4): hardcode pra evitar runaway. Ciclo medio ~$0.02-0.20
# com cache hit. Cap diario $5 = ~25 ciclos worst-case; mensal $100 alinha com
# MONTHLY_BUDGET_USD do platform_costs.
COS_DAILY_CAP_USD = 5.0
COS_MONTHLY_CAP_USD = 100.0


def _check_budget_caps() -> Dict[str, Any]:
    """Calcula gasto Anthropic hoje (cycles do dia) e MTD. Aborta se passar cap.

    Hoje = soma cost_usd de cos_action_log do cycle_id de hoje (proxy mais
    direto que API Anthropic, que tem lag 24h). MTD = get_mtd_summary().

    Retorna {abort: bool, reason: str, today_usd, mtd_usd}.
    """
    today_brt = to_brt(now_utc()).date()
    today_usd = 0.0
    mtd_usd = 0.0

    # Override de teste: MOCK_OVER_BUDGET=1 -> simula estouro
    if (os.getenv("MOCK_OVER_BUDGET") or "").strip() == "1":
        return {
            "abort": True,
            "reason": f"MOCK_OVER_BUDGET=1 (simulado)",
            "today_usd": COS_DAILY_CAP_USD + 1.0,
            "mtd_usd": 0.0,
        }

    # Soma custo dos ciclos de hoje (cycle_id LIKE YYYY-MM-DD-%)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            # Custos sao logados no cron_runs (result_json) — fonte mais direta
            cur.execute(
                """
                SELECT COALESCE(SUM((result_json->>'cost_usd')::float), 0) AS sum_usd
                FROM cron_runs
                WHERE path = '/api/cron/cos-investigator'
                  AND started_at >= %s::date
                  AND started_at <  (%s::date + INTERVAL '1 day')
                  AND result_json ? 'cost_usd'
                """,
                (today_brt.isoformat(), today_brt.isoformat()),
            )
            row = cur.fetchone()
            if row:
                today_usd = float(row.get("sum_usd") or 0)
    except Exception as e:
        logger.warning(f"_check_budget_caps today_usd falhou: {e}")

    # MTD do Anthropic via platform_costs
    try:
        from services.platform_costs import get_mtd_summary
        summary = get_mtd_summary()
        for p in summary.get("providers", []):
            if p.get("provider") == "anthropic":
                mtd_usd = float(p.get("amount_usd") or 0)
                break
    except Exception as e:
        logger.warning(f"_check_budget_caps mtd falhou: {e}")

    if today_usd > COS_DAILY_CAP_USD:
        return {
            "abort": True,
            "reason": f"daily_cap_hit: ${today_usd:.2f} > ${COS_DAILY_CAP_USD:.2f}",
            "today_usd": today_usd,
            "mtd_usd": mtd_usd,
        }
    if mtd_usd > COS_MONTHLY_CAP_USD:
        return {
            "abort": True,
            "reason": f"monthly_cap_hit: ${mtd_usd:.2f} > ${COS_MONTHLY_CAP_USD:.2f}",
            "today_usd": today_usd,
            "mtd_usd": mtd_usd,
        }
    return {"abort": False, "today_usd": today_usd, "mtd_usd": mtd_usd}


def _log_budget_abort_item(cycle_id: str, info: Dict[str, Any]) -> None:
    """Insere 1 escalated em cos_briefing_items pra Renato decidir."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO cos_briefing_items
                  (cycle_id, categoria, texto, prioridade, refs)
                VALUES (%s, 'escalated', %s, 1, %s::jsonb)
                """,
                (
                    cycle_id,
                    (
                        f"CoS investigator desligado: budget "
                        f"${info.get('today_usd', 0):.2f}/dia (cap ${COS_DAILY_CAP_USD:.0f}) "
                        f"OU ${info.get('mtd_usd', 0):.2f}/mes (cap ${COS_MONTHLY_CAP_USD:.0f}) "
                        f"atingido. Renato precisa decidir bump ou pausa."
                    ),
                    json.dumps({"budget_abort": True, **info}),
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"_log_budget_abort_item falhou: {e}")


# ============== System prompt ==============

_SYSTEM_PROMPT_TEMPLATE = """Você é o Chief of Staff (CoS) Investigator do Renato. Você NÃO é assistente, NÃO é dashboard. Você é executor: descobre o que importa hoje e age.

==== MODO DE OPERAÇÃO ====

Você recebe um snapshot de DADOS DE HOJE (agenda, tasks vencidas, propostas pendentes, CoS config). A partir disso, você IDENTIFICA quem/o quê é relevante e INVESTIGA sob demanda usando as tools. Cada tool call deve ter um propósito claro.

Você tem no máximo 15 iterações. Use BEM:
- Iterações 1-3: leia DADOS DE HOJE, identifique 3-7 itens críticos
- Iterações 4-12: investigue cada item (get_messages_with quando há contato; get_proposals quando relevante)
- Iterações 13-15: registre items (record_observation/escalate_to_user/create_draft_response)

==== CONFIGURAÇÃO COS (bússola) ====
{cos_config_content}
==== FIM DA CONFIGURAÇÃO ====

==== CONTRATO DE FIDELIDADE (LEIA ANTES DE TUDO) ====

Você só pode afirmar coisas sobre o que foi RETORNADO POR UMA TOOL nesta sessão. Se você não chamou tool sobre um contato/tema, você NÃO sabe nada sobre ele. Não invente, não chute, não reconstrua de memória.

PROIBIDO afirmar sem dado de tool:
- "Thalita mandou 2 alertas" sem ter chamado get_messages_with('Thalita') e visto as mensagens
- "Marcos respondeu sobre X" sem dados retornados
- "Wadhwani sem decisão" sem evidência factual em tasks/calendar/messages
- Inferir estado emocional ou intenção do outro lado
- "Você está evitando", "você parece cansado" — não é terapeuta

Se get_messages_with retorna mensagens=[] → registre "sem trocas WA nas últimas 48h", NUNCA "ele sumiu" ou "você está evitando".

Se get_overdue_tasks retorna 14 tasks → diga "14 tasks vencidas, sendo X de peso alto", NÃO "você está sobrecarregado".

Errar pra menos (omitir) é SEMPRE melhor que errar pra mais (inventar). Alucinar quebra confiança e é falha grave.

==== MANDATO DE AÇÃO ====

Quando uma pendência aponta pra um contato relevante (em tasks/agenda/propostas), VOCÊ DEVE:
1. Chamar get_messages_with pra ver a conversa real
2. Se o contato pediu algo concreto que você sabe responder → create_draft_response (Renato aprova/dispara)
3. Se exige decisão binária do Renato → escalate_to_user
4. Se é só pra observar → record_observation

Contatos cold/vendor/spam: IGNORE — não rascunhe, não escale, não observe.

Sempre que criar um draft_response, JUSTIFIQUE no `motivo` com a evidência da tool (ex: "Thalita pediu ETA do plano de ação na msg de 14/06 18h32").

==== TASKS VENCIDAS — DRIFT vs BLOQUEIO EXTERNO vs DELEGADAS ====

O contexto inicial inclui `overdue_preprocess` ja com cruzamento WA/email feito:

- `active_tasks` -> POTENCIAL DRIFT do Renato. Investigue contatos via get_messages_with se relevante.
  Tasks aqui sem keyword frente: NAO escale individualmente; agregue ("N tasks vencidas, X de peso alto").

- `blocked_external` -> Renato esta esperando terceiro. NAO ESCALE como drift. Trate como contexto:
  * Se dias_atraso <= suggest_fup_in_days: ignore (esperar e ok).
  * Se dias_atraso > suggest_fup_in_days: record_observation tipo
    "👀 Aguardando [contact] ha Nd (sugerir FUP soft se passar de Xd)" com refs task_id.
  * NAO criar draft sem chamar get_messages_with antes pra confirmar contexto atual.

- `waiting_followthrough` -> outro lado prometeu mandar algo. NAO escalar como drift.
  * record_observation: "👀 [contact] prometeu [excerpt promessa]; aguardando".
  * Se passou MUITO de suggest_fup_in_days e nada chegou, AI pode escalate_to_user prioridade 3
    pra Renato decidir se cobra.

- `delegated_count > 0` -> tasks com RACI R != Renato foram filtradas. NAO precisa escalar
  individualmente. Se >= 5, record_observation: "👀 N tasks de conselho delegadas (R != Renato) — apenas Accountable".

REGRA DE OURO: NAO escale como drift uma task se ela esta em blocked_external/waiting_followthrough.
Antes de tratar uma task como drift do Renato, confira se ela esta em active_tasks. Se estiver em
blocked/waiting/delegated, e contexto — nao drift.

==== CATEGORIAS DE ITEMS QUE VOCÊ REGISTRA ====

✅ FEITO (criado automaticamente quando você chama create_draft_response — não precisa registrar manualmente)
🟡 ONE-WAY (escalate_to_user, prioridade>=2) — decisão binária pro Renato com motivo factual
👀 MONITORANDO (record_observation) — fato derivado direto de tool, 1 linha
🔴 ESCALATED (escalate_to_user, prioridade=1) — crítico, sobe pra topo do briefing

==== PRIORIZAÇÃO POR FRENTE ====

Use os pesos v5 da CoS config. Frente que pesa mais HOJE = mais itens. Frentes sem pendência real: ignore (curto vale mais que cheio).

==== FRENTE 1 DRIFT (imensIAH) ====

Se vier no contexto inicial `frente_drift_signals` com items (tasks Frente 1 vencidas > 7 dias com peso alto), escale TODOS via `escalate_to_user` com prioridade 2 (categoria one_way). Motivo factual: cite task_id, dias_vencida e projeto. Frente 1 = aposta principal — drift aqui é red flag.

==== EMAILS (triagem CoS) ====

CHAME `get_pending_email_triage(limit=15)` UMA vez no inicio. O sweep cron classificou cada email em must_read / archive_proposed / silent. Para cada item:

- `classification=must_read` + priority>=9 -> `escalate_to_user` (prioridade 2 = one_way) com texto formato '📧 [account] De [sender]: [subject]' + motivo 'classif must_read p[N] conf [X]'.

- `classification=must_read` + priority<9 -> `record_observation` categoria monitor com texto '📧 [account] [sender]: [subject]'. SE houver >3 must_read p<9, AGRUPE em 1 linha so: 'N emails C2/frente pra ler hoje'.

- `classification=archive_proposed` -> NUNCA escale individual. Conte e crie 1 SO `record_observation` com texto 'X emails propostos pra arquivar (shadow mode 2sem ate auto-archive)'.

- `classification=silent` -> ignore. Nao escale, nao observe.

IMPORTANTE: triage_id é a ref pra acao (Renato vai aprovar/arquivar via UI). Inclua triage_id no refs do item.

==== FECHAMENTO ====

Quando você terminar a investigação (ou atingir o limite de iterações), retorne uma mensagem final de texto curta (2-4 linhas) resumindo o que registrou. Não precisa formato WhatsApp — o briefing 8h vai compor a mensagem a partir dos items registrados.

NÃO chame tool no turno final. Quando você não tem mais tool pra chamar, diga o que fez e pare.
"""


def _build_system_prompt(cos_config_content: str) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(cos_config_content=cos_config_content or "(sem CoS config ativa)")


# ============== Drift detection Frente 1 (imensIAH) ==============

def compute_frente_drift_signals() -> List[Dict[str, Any]]:
    """Detecta drift na Frente 1 (imensIAH): tasks de peso alto vinculadas a
    projetos da F1 vencidas ha > 7 dias.

    Mapping MVP (sem coluna 'frente' em projects ainda): projeto.nome ILIKE
    '%imensIAH%' OR '%Assespro%' OR '%NeoGovernanca%'. Migrar pra
    project.frente quando coluna existir.

    Retorna lista compacta: [{task_id, titulo, dias_vencida, projeto, peso}]
    ordenada por dias_vencida desc. Max 10.
    """
    signals: List[Dict[str, Any]] = []
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.id, t.titulo, t.data_vencimento, t.prioridade,
                       p.nome AS projeto,
                       EXTRACT(DAY FROM NOW() - t.data_vencimento)::int AS dias_vencida
                FROM tasks t
                JOIN projects p ON p.id = t.project_id
                WHERE t.status != 'done'
                  AND t.status != 'completed'
                  AND t.data_vencimento IS NOT NULL
                  AND t.data_vencimento < NOW() - INTERVAL '7 days'
                  AND COALESCE(t.prioridade, 5) <= 5  -- 1=critical, 5=normal; >5=baixa, descarta
                  AND (
                       p.nome ILIKE '%imensIAH%'
                    OR p.nome ILIKE '%Assespro%'
                    OR p.nome ILIKE '%NeoGovernanca%'
                  )
                ORDER BY t.data_vencimento ASC
                LIMIT 10
                """
            )
            for r in cur.fetchall():
                signals.append({
                    "task_id": r["id"],
                    "titulo": (r["titulo"] or "")[:80],
                    "dias_vencida": int(r["dias_vencida"] or 0),
                    "projeto": r["projeto"],
                    "peso": r.get("prioridade"),
                })
    except Exception as e:
        logger.warning(f"compute_frente_drift_signals falhou: {e}")
    return signals


# ============== Contexto inicial determinístico ==============

def _collect_initial_context() -> Dict[str, Any]:
    """Coleta contexto inicial SEM tool calls — economiza iterações.
    Retorna dict serializável que vai pro user prompt."""
    today_brt = to_brt(now_utc()).date()
    tomorrow_brt = today_brt + timedelta(days=1)

    context: Dict[str, Any] = {
        "data": today_brt.isoformat(),
        "data_amanha": tomorrow_brt.isoformat(),
    }

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Agenda hoje+amanhã
            cursor.execute(
                """
                SELECT id, summary, start_datetime, end_datetime, location
                FROM calendar_events
                WHERE start_datetime >= %s::date
                  AND start_datetime < (%s::date + INTERVAL '1 day')
                ORDER BY start_datetime ASC
                LIMIT 20
                """,
                (today_brt.isoformat(), tomorrow_brt.isoformat()),
            )
            context["agenda"] = [
                {
                    "id": r["id"],
                    "titulo": r["summary"],
                    "inicio": r["start_datetime"].isoformat() if r["start_datetime"] else None,
                    "local": r["location"],
                }
                for r in cursor.fetchall()
            ]

            # Overdue top 10 (peso alto primeiro)
            cursor.execute(
                """
                SELECT t.id, t.titulo, t.data_vencimento, t.contact_id, t.project_id,
                       p.nome AS projeto, c.nome AS contact_name
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                LEFT JOIN contacts c ON c.id = t.contact_id
                WHERE t.status != 'done'
                  AND t.data_vencimento IS NOT NULL
                  AND t.data_vencimento < NOW()
                ORDER BY t.data_vencimento ASC
                LIMIT 10
                """
            )
            context["overdue_top10"] = [
                {
                    "id": r["id"],
                    "titulo": (r["titulo"] or "")[:80],
                    "projeto": r["projeto"],
                    "contact_id": r["contact_id"],
                    "contact_name": r["contact_name"],
                    "vencimento": r["data_vencimento"].isoformat() if r["data_vencimento"] else None,
                }
                for r in cursor.fetchall()
            ]

            # Contagem total overdue
            cursor.execute("SELECT COUNT(*) AS n FROM tasks WHERE status != 'done' AND data_vencimento IS NOT NULL AND data_vencimento < NOW()")
            context["overdue_total"] = cursor.fetchone()["n"]

            # Propostas top 5
            cursor.execute(
                """
                SELECT p.id, p.action_type, p.title, p.contact_id, c.nome AS contact_name, p.urgency
                FROM action_proposals p
                LEFT JOIN contacts c ON c.id = p.contact_id
                WHERE p.status = 'pending'
                ORDER BY
                    CASE p.urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                    p.criado_em DESC
                LIMIT 5
                """
            )
            context["proposals_top5"] = [
                {
                    "id": r["id"],
                    "tipo": r["action_type"],
                    "titulo": (r["title"] or "")[:100],
                    "contact_id": r["contact_id"],
                    "contact_name": r["contact_name"],
                    "urgency": r["urgency"],
                }
                for r in cursor.fetchall()
            ]

            cursor.execute("SELECT COUNT(*) AS n FROM action_proposals WHERE status='pending'")
            context["proposals_total"] = cursor.fetchone()["n"]

    except Exception as e:
        logger.warning(f"_collect_initial_context falhou: {e}")
        context["erro_contexto"] = str(e)

    # Drift detection Frente 1 (imensIAH) — sempre top-level pra LLM ver
    context["frente_drift_signals"] = compute_frente_drift_signals()

    return context


def _preprocess_blocked_tasks(
    cycle_id: str,
    max_tasks: int = 300,
    blocking_check_top: int = 30,
) -> Dict[str, Any]:
    """Pre-processa tasks vencidas via Cruzamento 1+2 ANTES do agent loop.

    Pipeline:
    1. get_overdue_tasks_raci_aware com limit alto (max_tasks) pra varrer
       backlog inteiro e classificar delegadas corretamente.
    2. Roda get_task_blocking_status SO nas top-N (blocking_check_top)
       mais recentes (data_vencimento DESC entre as filtradas), pra evitar
       N chamadas em backlog longo.
    3. Particiona:
       - blocked_external: Renato falou ultimo + 'aguardo' -> contexto
       - waiting_followthrough: outro lado prometeu material -> contexto
       - active: 'unblocked' ou 'no_data' -> drift normal (resto entra aqui sem
         blocking-check pra economizar)

    Retorna {
        active_tasks: [...],          # tasks que continuam como drift normal
        blocked_external: [...],      # tasks bloqueadas em terceiros
        waiting_followthrough: [...], # tasks aguardando material/promessa externa
        delegated_count: N,           # tasks delegadas (R != Renato)
        delegated_sample: [...],      # amostra das delegadas (max 10)
        delegated_ids: [...],         # IDs completos das delegadas
    }
    """
    blocked_external: List[Dict[str, Any]] = []
    waiting_followthrough: List[Dict[str, Any]] = []
    active_tasks: List[Dict[str, Any]] = []
    delegated_count = 0
    delegated_sample: List[Dict[str, Any]] = []
    delegated_ids: List[int] = []

    try:
        raci_result = get_overdue_tasks_raci_aware(
            cycle_id=cycle_id,
            iteration=-1,  # pre-process — fora do agent loop
            limit=max_tasks,
        )
        delegated_count = raci_result.get("delegated_count", 0)
        delegated_sample = raci_result.get("delegated_sample", [])
        delegated_ids = raci_result.get("delegated_ids", [])
        tasks = raci_result.get("tasks", [])
    except Exception as e:
        logger.warning(f"_preprocess_blocked_tasks raci_aware falhou: {e}")
        return {
            "active_tasks": [],
            "blocked_external": [],
            "waiting_followthrough": [],
            "delegated_count": 0,
            "delegated_sample": [],
            "delegated_ids": [],
            "erro": str(e),
        }

    # Top-N mais recentes (data_vencimento DESC) recebem blocking check.
    # Resto vai direto pra active (presumindo: sem cruzamento WA = drift).
    # Ordering: data_vencimento ISO string asc->desc invertido (mais recente primeiro).
    tasks_sorted = sorted(
        tasks,
        key=lambda t: t.get("data_vencimento") or "",
        reverse=True,
    )
    to_check = tasks_sorted[:blocking_check_top]
    rest = tasks_sorted[blocking_check_top:]

    for t in to_check:
        try:
            blocking = get_task_blocking_status(
                cycle_id=cycle_id,
                iteration=-1,
                task_id=t["id"],
            )
        except Exception as e:
            logger.warning(f"_preprocess_blocked_tasks blocking task={t['id']} falhou: {e}")
            active_tasks.append(t)
            continue

        status = blocking.get("status")
        enriched = {**t, "blocking": blocking}
        if status == "blocked_on_external":
            blocked_external.append(enriched)
        elif status == "waiting_external_followthrough":
            waiting_followthrough.append(enriched)
        else:
            active_tasks.append(enriched)

    # Resto entra como active sem check (otimizacao: tasks mais antigas)
    active_tasks.extend(rest)

    return {
        "active_tasks": active_tasks,
        "blocked_external": blocked_external,
        "waiting_followthrough": waiting_followthrough,
        "delegated_count": delegated_count,
        "delegated_sample": delegated_sample,
        "delegated_ids": delegated_ids,
    }


def _get_cos_config_content() -> str:
    """Pega o conteudo da CoS config ativa (system_memories tipo='cos_config')."""
    try:
        from services.system_memory import get_active_cos_config
        cfg = get_active_cos_config()
        if cfg and cfg.get("conteudo"):
            return cfg["conteudo"]
    except Exception as e:
        logger.warning(f"_get_cos_config_content falhou: {e}")
    return ""


# ============== Counters ==============

def _count_items_created(cycle_id: str) -> Dict[str, int]:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT categoria, COUNT(*) AS n
                FROM cos_briefing_items
                WHERE cycle_id = %s
                GROUP BY categoria
                """,
                (cycle_id,),
            )
            counts = {r["categoria"]: r["n"] for r in cursor.fetchall()}
            return {
                "feito": counts.get("feito", 0),
                "one_way": counts.get("one_way", 0),
                "monitor": counts.get("monitor", 0),
                "escalated": counts.get("escalated", 0),
                "total": sum(counts.values()),
            }
    except Exception:
        return {"feito": 0, "one_way": 0, "monitor": 0, "escalated": 0, "total": 0}


def _count_actions_logged(cycle_id: str) -> int:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM cos_action_log WHERE cycle_id = %s", (cycle_id,))
            return cursor.fetchone()["n"]
    except Exception:
        return 0


# ============== Main entry point ==============

async def run_investigator_cycle(cycle_id: Optional[str] = None) -> Dict[str, Any]:
    """Roda um ciclo do Investigator. Retorna estatísticas.

    Args:
        cycle_id: opcional. Se None, gera "YYYY-MM-DD-morning" BRT.
    """
    started_at = time.time()

    if cycle_id is None:
        today_brt = to_brt(now_utc()).date()
        cycle_id = f"{today_brt.isoformat()}-morning"

    # Budget cap check (P.6 L.4): aborta se hoje > $5 ou MTD > $100
    budget_info = _check_budget_caps()
    if budget_info.get("abort"):
        logger.warning(f"cos_investigator abort: {budget_info.get('reason')}")
        _log_budget_abort_item(cycle_id, budget_info)
        return {
            "cycle_id": cycle_id,
            "status": "aborted_budget",
            "reason": budget_info.get("reason"),
            "spent_today": budget_info.get("today_usd"),
            "spent_mtd": budget_info.get("mtd_usd"),
            "iterations": 0,
            "items_created": _count_items_created(cycle_id),
            "actions_logged": 0,
            "duration_ms": int((time.time() - started_at) * 1000),
        }

    if not ANTHROPIC_API_KEY:
        return {
            "cycle_id": cycle_id,
            "status": "skipped",
            "reason": "no_api_key",
            "iterations": 0,
            "items_created": {"total": 0},
            "actions_logged": 0,
            "duration_ms": int((time.time() - started_at) * 1000),
        }

    # SDK import lazy — só se há API key
    try:
        import anthropic
    except ImportError:
        return {
            "cycle_id": cycle_id,
            "status": "error",
            "reason": "anthropic_sdk_missing",
            "iterations": 0,
            "items_created": {"total": 0},
            "actions_logged": 0,
            "duration_ms": int((time.time() - started_at) * 1000),
        }

    # 1. Contexto inicial determinístico
    initial_context = _collect_initial_context()
    cos_config = _get_cos_config_content()

    # 1b. Pre-process Cruzamento 1+2+3: blocked-on-external + raci-aware
    # Substitui overdue_top10 cru no contexto pra eliminar falsos drifts.
    # max_tasks=300 varre backlog inteiro pra classificar delegated corretamente.
    # blocking_check_top=30 limita chamadas pesadas (~30 queries SQL extras).
    preprocess = _preprocess_blocked_tasks(cycle_id, max_tasks=300, blocking_check_top=30)
    initial_context["overdue_preprocess"] = {
        "active_count": len(preprocess["active_tasks"]),
        "active_tasks": preprocess["active_tasks"][:10],
        "blocked_external": [
            {
                "task_id": t["id"],
                "titulo": t["titulo"],
                "projeto": t.get("projeto"),
                "dias_atraso": t.get("dias_atraso"),
                "contact_name": t["blocking"].get("contact_name"),
                "motivo": t["blocking"].get("motivo"),
                "last_msg_excerpt": t["blocking"].get("last_msg_excerpt"),
                "suggest_fup_in_days": t["blocking"].get("suggest_fup_in_days"),
            }
            for t in preprocess["blocked_external"][:10]
        ],
        "waiting_followthrough": [
            {
                "task_id": t["id"],
                "titulo": t["titulo"],
                "projeto": t.get("projeto"),
                "dias_atraso": t.get("dias_atraso"),
                "contact_name": t["blocking"].get("contact_name"),
                "motivo": t["blocking"].get("motivo"),
                "external_promise_excerpt": t["blocking"].get("external_promise_excerpt"),
                "suggest_fup_in_days": t["blocking"].get("suggest_fup_in_days"),
            }
            for t in preprocess["waiting_followthrough"][:10]
        ],
        "delegated_count": preprocess["delegated_count"],
        "delegated_sample": preprocess["delegated_sample"][:5],
    }

    # 2. Prompts
    system_prompt = _build_system_prompt(cos_config)
    user_prompt = (
        f"CYCLE_ID: {cycle_id}\n\n"
        f"DADOS DE HOJE (snapshot determinístico — investigue além disso usando as tools):\n\n"
        f"{json.dumps(initial_context, default=str, ensure_ascii=False, indent=2)}\n\n"
        f"IMPORTANTE: o campo `overdue_preprocess` ja cruzou tasks vencidas com WA/email:\n"
        f"- `active_tasks`: tasks que VOCE deve considerar como drift do Renato (foco aqui)\n"
        f"- `blocked_external`: tasks BLOQUEADAS em terceiros (Renato esperando) — NAO sao drift; "
        f"gere FUP soft (record_observation) so se dias_atraso > suggest_fup_in_days\n"
        f"- `waiting_followthrough`: outro lado prometeu material/retorno — NAO sao drift; "
        f"observe via record_observation com motivo factual\n"
        f"- `delegated_count`: tasks com RACI R != Renato (delegadas, foram filtradas)\n\n"
        f"Comece identificando 3-7 itens relevantes pelo peso CoS. Para active_tasks "
        f"investigue contatos via get_messages_with. Para blocked/waiting registre como "
        f"contexto (record_observation), NAO escale como drift."
    )

    # 3. Anthropic client
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # System prompt com cache_control — é o mesmo entre iterations (15+ requests),
    # então cache_creation paga 1.25x na primeira e ~0.1x nas demais.
    system_param = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    total_input_tokens = 0
    total_output_tokens = 0
    cache_creation_tokens = 0
    cache_read_tokens = 0
    iterations_done = 0
    final_text = ""
    last_stop_reason = None
    error_message = None

    try:
        for iteration in range(MAX_ITERATIONS):
            iterations_done = iteration + 1
            try:
                response = client.messages.create(
                    model=COS_INVESTIGATOR_MODEL,
                    max_tokens=MAX_TOKENS_PER_ITER,
                    system=system_param,
                    tools=COS_TOOLS,
                    messages=messages,
                )
            except Exception as api_err:
                error_message = f"api_call_failed iter={iteration}: {api_err}"
                logger.warning(error_message)
                break

            # Acumula uso
            usage = response.usage
            total_input_tokens += getattr(usage, "input_tokens", 0) or 0
            total_output_tokens += getattr(usage, "output_tokens", 0) or 0
            cache_creation_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            last_stop_reason = response.stop_reason

            # Append assistant message (inclui tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})

            # Coleta tool_use blocks
            tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

            # Captura texto do turno (pra fechamento)
            for b in response.content:
                if getattr(b, "type", None) == "text":
                    txt = (getattr(b, "text", "") or "").strip()
                    if txt:
                        final_text = txt  # último texto vence

            # Se modelo parou de chamar tools, fim do loop
            if response.stop_reason != "tool_use" or not tool_use_blocks:
                break

            # Executa cada tool, coleta results
            tool_results = []
            for tu in tool_use_blocks:
                tool_input = getattr(tu, "input", {}) or {}
                tool_name = getattr(tu, "name", "")
                tool_use_id = getattr(tu, "id", "")
                result = execute_tool(tool_name, dict(tool_input), cycle_id, iteration)
                # Envia resultado como string JSON
                result_str = json.dumps(result, default=str, ensure_ascii=False)
                if len(result_str) > 12000:
                    result_str = result_str[:11990] + "...[trunc]"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_str,
                })

            messages.append({"role": "user", "content": tool_results})

    except Exception as loop_err:
        error_message = f"loop_failed: {loop_err}"
        logger.exception("run_investigator_cycle loop falhou")

    items = _count_items_created(cycle_id)
    actions = _count_actions_logged(cycle_id)
    duration_ms = int((time.time() - started_at) * 1000)

    # Custo aproximado (Sonnet 4.6: $3/$15 per 1M)
    cost_usd = (
        (total_input_tokens * 3.0 / 1_000_000)
        + (total_output_tokens * 15.0 / 1_000_000)
        + (cache_creation_tokens * 3.75 / 1_000_000)   # 1.25x input
        + (cache_read_tokens * 0.30 / 1_000_000)        # 0.1x input
    )

    # F-E: custo por-funcao. Loop agentico multi-iteracao -> UMA linha com os
    # totais acumulados + custo ja computado acima (mesmo padrao pre-computado do
    # wa_triage). Telemetria best-effort, nunca quebra o ciclo.
    if total_input_tokens or total_output_tokens:
        try:
            from services import llm_usage
            llm_usage.record(
                "cos.investigator", COS_INVESTIGATOR_MODEL,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cost_usd=round(cost_usd, 6),
            )
        except Exception:
            pass

    status = "success"
    if error_message:
        status = "error" if items["total"] == 0 else "partial"

    return {
        "cycle_id": cycle_id,
        "status": status,
        "iterations": iterations_done,
        "items_created": items,
        "actions_logged": actions,
        "duration_ms": duration_ms,
        "tokens": {
            "input": total_input_tokens,
            "output": total_output_tokens,
            "cache_creation": cache_creation_tokens,
            "cache_read": cache_read_tokens,
        },
        "cost_usd": round(cost_usd, 4),
        "last_stop_reason": last_stop_reason,
        "final_text": final_text[:500] if final_text else None,
        "error_message": error_message,
    }
