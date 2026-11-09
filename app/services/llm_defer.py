"""Fila de trabalho LLM diferido — o que fazer com o julgamento enquanto o
canário de saldo Anthropic está aceso.

O PROBLEMA (medido 09/11/2026). A conta está sem saldo desde 28/10 19:33 BRT e o
flag `anthropic_credit_down` segue de pé há 284 horas. Nesse período cada um dos
11 sítios de `messages.create` do INTEL falha POR SI: o `_judge` do
`task_reconciler` devolve `judge_failed=True`, loga um warning e segue. **Nada
registra o julgamento que não foi feito.** Como o reconciler responde por 41% dos
fechamentos de task, 12 dias de canário aceso aparecem no board executivo como
"326 tasks atrasadas" sem nada apontando a causa.

O QUE ESTE MÓDULO FAZ. Com o flag aceso, o passo LLM é **enfileirado** em
`llm_defer_jobs` em vez de perdido. Uma sessão `/sys` pega o prompt, responde e
devolve a resposta; ela é aplicada pelo caminho NORMAL da função de origem — no
reconciler, `_finaliza_verdict` → `_close_task`, com conferência de citação,
`undo_hint` e `agent_actions` intactos. **A fila carrega o prompt, nunca a
decisão.** O que muda é quem responde, não o que se faz com a resposta.

ESCOPO DECLARADO — isto é MODO DEGRADADO, não caminho normal. Rotear a carga LLM
de produção por uma sessão de assinatura é aceitável como dreno enquanto a conta
está fora, operado pelo Renato, e não como arquitetura: a camada LLM inteira roda
num baseline de ~US$ 76/mês. Por isso o gate é o PRÓPRIO canário — quando o saldo
volta, `check_anthropic_credit_balance` apaga o flag e esta fila deixa de receber
trabalho sozinha, sem ninguém ter de lembrar de desligá-la.

POR QUE O GATE É SÓ O FLAG, e não "a chave falhou de qualquer jeito": chave
revogada e saldo zerado JÁ acendem o flag (`invalid_key` e `no_credits` são os
dois ramos `down` do canário). O caso que fica fora é `ANTHROPIC_API_KEY` ausente
do ambiente — e esse é erro de deploy, que merece alarme próprio, não uma fila
que o absorve em silêncio.
"""
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from database import get_db

logger = logging.getLogger(__name__)

# Lote velho não se julga: o reconciler monta o prompt a partir das mensagens de
# ATÉ AQUELE MOMENTO, e uma resposta aplicada dois dias depois fecharia a task com
# uma foto vencida. 12h cobre o ciclo diário do cron com folga e ainda deixa o
# job inútil morrer em vez de virar lixo acionável.
JOB_TTL_HOURS = 12

# Sessão que pegou um job e morreu antes de responder (o Renato fechou a janela,
# o /loop parou) não pode prender o trabalho até o TTL. 20 min é muito mais que o
# tempo de responder um prompt e pouco o bastante pra o job voltar à fila no tick
# seguinte do /loop.
CLAIM_TTL_MINUTES = 20

# Aplicadores por função. String em vez de referência pra não importar
# task_reconciler aqui — ele importa este módulo, e o par viraria ciclo.
_APPLIERS = {
    "task_reconciler.judge": ("services.task_reconciler", "apply_deferred_judgment"),
    "task_reconciler.judge_grupo": ("services.task_reconciler", "apply_deferred_judgment"),
}

_CANARY_CACHE_TTL = 60.0   # s
_canary_cache: tuple = (0.0, False)


def defer_enabled() -> bool:
    """Kill-switch `LLM_DEFER`. Default **ON**, e isso é deliberado.

    O padrão do repo pra comportamento novo é default OFF + flip explícito
    (`INBOX_SMART_LAYER`, `RACI_WRITE_TARGET`). Aqui não dá: o `railway` CLI está
    sem token nesta máquina (`invalid_grant`, 27/10), então uma flag default OFF
    só seria ligável pelo Renato à mão — e o canário está aceso AGORA.

    O que torna o default ON seguro é o gate ser o canário: com saldo, `enqueue`
    nunca é chamado e o caminho é byte-a-byte o de hoje. `LLM_DEFER=off` existe
    pra desligar o dreno sem esperar o saldo voltar.
    """
    return (os.getenv("LLM_DEFER") or "on").strip().lower() not in ("0", "off", "false", "no")


def is_canary_lit(use_cache: bool = True) -> bool:
    """O flag `anthropic_credit_down` está de pé? (= toda função LLM caída)

    Mesma linha de `system_memories` que `platform_costs.check_anthropic_credit_balance`
    escreve e apaga — não é detector novo, é leitura do que já existe.

    Cache de 60s porque o reconciler pergunta uma vez por bloco e um run tem
    dezenas; curto o bastante pra a recuperação do saldo valer no mesmo run.
    """
    global _canary_cache
    if use_cache:
        ts, val = _canary_cache
        if time.monotonic() - ts < _CANARY_CACHE_TTL:
            return val
    lit = False
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM system_memories WHERE titulo='anthropic_credit_down' "
                "AND tipo='credit_canary' LIMIT 1"
            )
            lit = cur.fetchone() is not None
    except Exception as e:
        # Não saber se o canário está aceso NÃO é "está apagado". Mas também não
        # pode virar fila: se o DB está fora, enfileirar falharia igual. Devolve
        # False (= tenta a API, que é o comportamento de hoje) e registra.
        logger.warning("llm_defer: leitura do canário falhou (%s) — assumindo apagado", e)
        return False
    _canary_cache = (time.monotonic(), lit)
    return lit


def should_defer() -> bool:
    """Diferir este passo LLM em vez de chamar a API?"""
    return defer_enabled() and is_canary_lit()


def enqueue(function: str, prompt: str, model: str, max_tokens: int,
            context: Dict[str, Any]) -> Optional[int]:
    """Enfileira um passo LLM. Devolve o id, ou o id do job VIVO equivalente.

    Dedup pelo SHA-256 do prompt: o reconciler roda todo dia e remontaria o mesmo
    bloco a cada passada. Sem isso a fila cresceria por run em vez de por
    trabalho — e a sessão `/sys` gastaria o turno respondendo a mesma pergunta.

    `None` = não foi possível enfileirar (DB fora, migration 089 ausente no alvo).
    Nunca levanta: quem chama está num caminho de best-effort e um erro aqui não
    pode ser pior que o silêncio que esta fila existe pra consertar.
    """
    dedup = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO llm_defer_jobs
                       (function, status, model, max_tokens, prompt, context, dedup_key)
                   VALUES (%s, 'pending', %s, %s, %s, %s::jsonb, %s)
                   ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL
                        AND status IN ('pending', 'claimed')
                   DO NOTHING
                   RETURNING id""",
                (function, model, int(max_tokens), prompt,
                 json.dumps(context, default=str), dedup),
            )
            row = cur.fetchone()
            conn.commit()
            if row:
                return row["id"]
            # Conflito: o job vivo equivalente já está na fila. Devolver o id dele
            # (e não None) separa "já enfileirado" de "falhou ao enfileirar" —
            # sem isso a telemetria contaria dedup como erro.
            cur.execute(
                "SELECT id FROM llm_defer_jobs WHERE dedup_key=%s "
                "AND status IN ('pending','claimed') LIMIT 1",
                (dedup,),
            )
            row = cur.fetchone()
            return row["id"] if row else None
    except Exception as e:
        logger.warning("llm_defer.enqueue falhou (function=%s): %s", function, e)
        return None


def housekeeping() -> Dict[str, int]:
    """Expira lote velho e solta claim travado. Idempotente, roda a cada peek/claim.

    Separado do `claim` de propósito: um job expirado tem de SAIR da fila mesmo
    que nenhuma sessão `/sys` abra — senão a fila acumula trabalho que ninguém
    mais pode aplicar e o `/dev` da semana seguinte lê isso como demanda viva.
    """
    out = {"expired": 0, "released": 0}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE llm_defer_jobs
                      SET status='expired',
                          error=COALESCE(error, 'lote venceu antes de alguém responder')
                    WHERE status IN ('pending','claimed')
                      AND criado_em < NOW() - (%s || ' hours')::interval""",
                (JOB_TTL_HOURS,),
            )
            out["expired"] = cur.rowcount or 0
            cur.execute(
                """UPDATE llm_defer_jobs
                      SET status='pending', claimed_at=NULL, claimed_by=NULL
                    WHERE status='claimed'
                      AND claimed_at < NOW() - (%s || ' minutes')::interval""",
                (CLAIM_TTL_MINUTES,),
            )
            out["released"] = cur.rowcount or 0
            conn.commit()
    except Exception as e:
        logger.warning("llm_defer.housekeeping falhou: %s", e)
    return out


def _counts(cur) -> Dict[str, int]:
    cur.execute("SELECT status, COUNT(*) AS n FROM llm_defer_jobs GROUP BY status")
    got = {r["status"]: int(r["n"]) for r in cur.fetchall()}
    for s in ("pending", "claimed", "applied", "failed", "expired"):
        got.setdefault(s, 0)
    return got


def peek(limit: int = 20) -> Dict[str, Any]:
    """Estado da fila, sem pegar nada. Read-only (o claim é POST)."""
    housekeeping()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            counts = _counts(cur)
            cur.execute(
                """SELECT id, function, model, status, criado_em,
                          LENGTH(prompt) AS prompt_chars,
                          jsonb_array_length(COALESCE(context->'tasks', '[]'::jsonb)) AS n_tasks
                     FROM llm_defer_jobs
                    WHERE status IN ('pending','claimed')
                    ORDER BY criado_em
                    LIMIT %s""",
                (limit,),
            )
            jobs = []
            for r in cur.fetchall():
                r = dict(r)
                if r.get("criado_em"):
                    r["criado_em"] = r["criado_em"].isoformat()
                jobs.append(r)
    except Exception as e:
        return {"disponivel": False, "nota": f"indisponivel: {e.__class__.__name__}",
                "canario_aceso": is_canary_lit(), "counts": {}, "jobs": []}
    return {"disponivel": True, "canario_aceso": is_canary_lit(),
            "defer_on": defer_enabled(), "counts": counts,
            "pendentes": counts["pending"], "jobs": jobs}


def claim(limit: int = 3, by: str = "sys") -> List[Dict[str, Any]]:
    """Pega até `limit` jobs pendentes, atômico. Devolve prompt + instruções.

    `FOR UPDATE SKIP LOCKED` porque duas sessões `/sys` abertas ao mesmo tempo é
    cenário normal (o Renato roda `/loop` em mais de uma janela) — sem isso as
    duas responderiam o mesmo prompt e a segunda resposta seria aplicada sobre
    uma task já fechada.
    """
    housekeeping()
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE llm_defer_jobs SET status='claimed', claimed_at=NOW(),
                          claimed_by=%s
                    WHERE id IN (
                        SELECT id FROM llm_defer_jobs WHERE status='pending'
                         ORDER BY criado_em LIMIT %s FOR UPDATE SKIP LOCKED)
                   RETURNING id, function, model, max_tokens, prompt, context""",
                (by[:80], max(1, min(int(limit), 10))),
            )
            jobs = [dict(r) for r in cur.fetchall()]
            conn.commit()
    except Exception as e:
        logger.warning("llm_defer.claim falhou: %s", e)
        return []
    for j in jobs:
        j["instrucoes"] = (
            "Responda o prompt abaixo EXATAMENTE como o modelo responderia: só o "
            "JSON pedido, sem comentário, sem cerca de código, sem preâmbulo. O "
            "JSON vai direto pro parser da função de origem, e a citação que você "
            "devolver é conferida contra as mensagens do próprio prompt — trecho "
            "parafraseado ou inventado faz o veredito ser DESCARTADO."
        )
    return jobs


async def submit_answer(job_id: int, answer: str) -> Dict[str, Any]:
    """Grava a resposta e a entrega ao aplicador da função de origem.

    O aplicador é quem decide o que a resposta significa — aqui não há nenhuma
    regra de negócio de propósito. Resposta que o aplicador recusa vira
    `failed` com o motivo, não `applied`: um dreno que carimba sucesso sobre
    recusa é pior que dreno nenhum.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, function, status, context FROM llm_defer_jobs WHERE id=%s",
                (int(job_id),),
            )
            job = cur.fetchone()
    except Exception as e:
        return {"ok": False, "erro": f"leitura falhou: {e.__class__.__name__}"}
    if not job:
        return {"ok": False, "erro": "job inexistente"}
    job = dict(job)
    if job["status"] not in ("pending", "claimed"):
        # Dois /sys respondendo o mesmo job, ou retry depois de expirar. Recusar é
        # o certo: aplicar de novo reabriria a decisão sobre um lote já vencido.
        return {"ok": False, "erro": f"job já está '{job['status']}'",
                "status": job["status"]}

    alvo = _APPLIERS.get(job["function"])
    if not alvo:
        _finish(job_id, "failed", answer, None, f"sem aplicador para {job['function']}")
        return {"ok": False, "erro": f"sem aplicador para {job['function']}"}

    ctx = job["context"] or {}
    if isinstance(ctx, str):
        ctx = json.loads(ctx)
    try:
        mod = __import__(alvo[0], fromlist=[alvo[1]])
        fn = getattr(mod, alvo[1])
        result = fn(ctx, answer)
        if hasattr(result, "__await__"):
            result = await result
    except Exception as e:
        logger.exception("llm_defer: aplicador de %s morreu", job["function"])
        _finish(job_id, "failed", answer, None, f"{e.__class__.__name__}: {e}")
        return {"ok": False, "erro": f"aplicador falhou: {e.__class__.__name__}: {e}"}

    ok = bool(result.get("ok", True))
    _finish(job_id, "applied" if ok else "failed", answer, result,
            None if ok else str(result.get("erro") or "aplicador recusou"))
    return {"ok": ok, "job_id": job_id, "function": job["function"], "resultado": result}


def _finish(job_id: int, status: str, answer: Optional[str],
            result: Optional[dict], error: Optional[str]) -> None:
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE llm_defer_jobs
                      SET status=%s, answer=%s, result=%s::jsonb, error=%s,
                          answered_at=NOW()
                    WHERE id=%s""",
                (status, answer, json.dumps(result, default=str) if result else None,
                 error, int(job_id)),
            )
            conn.commit()
    except Exception:
        logger.exception("llm_defer: não consegui gravar o desfecho do job %s", job_id)
