"""
Task Reconciler — sweep periódico que FECHA tasks pending já resolvidas por
comunicação direta (Renato agindo no WhatsApp/email, ou o contato respondendo),
fechando o gap do action-blindness: o task_auto_resolver (task_auto_resolver.py)
só roda em AÇÃO DO BOT (whatsapp_sent/email_sent via intel_bot), nunca quando o
Renato age direto do celular.

Design (ratificado 21/07):
- Escopo apertado: só tasks pending COM contact_id. As sem contato ficam fora do
  v0 (não dá pra casar com uma comunicação de forma confiável) — a contagem é
  logada, sem cap silencioso.
- Match LLM semântico (Haiku), NÃO keyword — o keyword do auto-resolver gerou
  falso-positivo em 13/07. Barra de confiança 0.85.
- SÓ FECHA — nunca cria proposta/ação/pergunta (anti-gen-1: não vira ruído).
- Undo + audit via agent_actions.log_action(undo_hint) + mark_undone (já prontos).
- Kill-switch DB: analyzer_settings 'task_reconciler_enabled' (default ON; 'off'/
  'false'/'0' desliga sem deploy).
- Silêncio quando nada fecha. Surfacing passivo (pill via route_to_renato urg 3).

Calibração de fechamento (ratificada, ver feedback_timeline_triage / C1):
- Task de AÇÃO (enviar/mandar/cobrar/falar/contatar/responder): concluída quando
  há OUTBOUND do Renato que cumpre a ação.
- Task de ESPERA (aguardar/esperar retorno de X): concluída quando o CONTATO
  respondeu (INCOMING) o que era esperado.

FASE 2 — saída do `on_hold` (30/07, migration 058):
`on_hold` era um beco. A convenção de 29/07 (feedback_aguardar_terceiro_on_hold)
manda a task de espera ficar parqueada nos primeiros 7 dias de atraso, mas exige
que ela RESSURJA por dois gatilhos — e nenhum existia: a string "on_hold"
aparecia em 5 lugares em app/ inteiro e nenhum reabria. Task parqueada não
voltava nunca, e a CoS tinha de reverter à mão pra ela não sumir do portão.
Agora `sweep_on_hold` reabre por (a) resposta substantiva do contato depois do
parqueio e (b) janela de espera esgotada. `parqueio_indefinido` fica de fora do
aging de propósito — ver a migration.
"""
import json
import logging
import os
import re
from datetime import timedelta
from database import get_db
from services import llm, llm_usage

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.85
MAX_MSGS_PER_TASK = 8
MAX_MSG_CHARS = 500

# Janela de espera da convenção de 29/07: 7 dias de atraso sem resposta antes de
# a task voltar ao radar. Contada de GREATEST(data_vencimento, on_hold_since) —
# re-parquear concede janela nova, que é o que o gesto humano quer dizer.
ON_HOLD_WAIT_DAYS = 7

# Motivos de parqueio (coluna tasks.on_hold_reason, migration 058).
REASON_ESPERA = "espera_terceiro"
REASON_INDEFINIDO = "parqueio_indefinido"


def is_reconciler_enabled() -> bool:
    """Kill-switch DB. Default ON. Desliga com analyzer_settings
    'task_reconciler_enabled' = off/false/0 (sem deploy). Mesmo padrão do freeze
    de propostas (action_proposals.is_proposals_frozen)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT setting_value FROM analyzer_settings WHERE setting_key = 'task_reconciler_enabled' LIMIT 1"
            )
            row = cur.fetchone()
            if not row or row['setting_value'] is None:
                return True
            val = str(row['setting_value']).strip().strip('"').lower()
            return val not in ("off", "false", "0", "no")
    except Exception:
        return True


def is_on_hold_sweep_enabled() -> bool:
    """Kill-switch DB da FASE 2, separado do fechamento: 'on_hold_sweep_enabled'
    off/false/0 desliga só a reabertura, sem derrubar a reconciliação que fecha
    (e sem deploy)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT setting_value FROM analyzer_settings WHERE setting_key = 'on_hold_sweep_enabled' LIMIT 1"
            )
            row = cur.fetchone()
            if not row or row['setting_value'] is None:
                return True
            val = str(row['setting_value']).strip().strip('"').lower()
            return val not in ("off", "false", "0", "no")
    except Exception:
        return True


def _fetch_candidate_tasks():
    """Tasks pending COM contact_id. Retorna (candidates, n_sem_contato)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, titulo, descricao, contact_id, project_id, data_criacao, data_vencimento
            FROM tasks
            WHERE status = 'pending' AND contact_id IS NOT NULL
            ORDER BY data_criacao ASC
        """)
        cands = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='pending' AND contact_id IS NULL")
        n_sem = cur.fetchone()['n']
    return cands, n_sem


def _fetch_messages_since(contact_id, since):
    """Mensagens (ambas direções) trocadas com o contato DEPOIS de `since`."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT direcao, conteudo, COALESCE(enviado_em, recebido_em, criado_em) AS ts
            FROM messages
            WHERE contact_id = %s
              AND conteudo IS NOT NULL AND conteudo <> ''
              AND COALESCE(enviado_em, recebido_em, criado_em) > %s
            ORDER BY ts DESC
            LIMIT %s
        """, (contact_id, since, MAX_MSGS_PER_TASK))
        return [dict(r) for r in cur.fetchall()]


def _judge(task, msgs) -> dict:
    """LLM (Haiku) decide se a task foi concluída à luz das mensagens. JSON estrito.
    Retorna {done, confidence, reason}. Best-effort: erro → done=false."""
    convo = "\n".join(
        f"[{'você→contato' if m['direcao'] == 'outgoing' else 'contato→você'} "
        f"{m['ts']:%d/%m %H:%M}] {(m['conteudo'] or '')[:MAX_MSG_CHARS]}"
        for m in reversed(msgs)  # mais antigas primeiro, pra leitura cronológica
    )
    prompt = f"""Você decide se uma TAREFA pendente já foi CONCLUÍDA, à luz das mensagens trocadas com o contato DEPOIS que a tarefa foi criada.

TAREFA:
Título: {task['titulo']}
Descrição: {task.get('descricao') or '(sem descrição)'}
Criada em: {task['data_criacao']:%d/%m/%Y}

MENSAGENS DESDE A CRIAÇÃO (cronológico):
{convo}

REGRAS:
- Tarefa de AÇÃO (enviar/mandar/cobrar/falar/contatar/responder): só está concluída se HÁ mensagem SUA (você→contato) que CUMPRE a ação.
- Tarefa de ESPERA (aguardar/esperar retorno de alguém): só está concluída se o CONTATO respondeu (contato→você) o que era esperado.
- Na dúvida, done=false. Conversa tangencial NÃO conclui a tarefa.
- Só done=true se as mensagens claramente satisfazem o que a tarefa pedia.

Responda APENAS um JSON: {{"done": true|false, "confidence": 0.0-1.0, "reason": "1 frase curta"}}"""

    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return {"done": False, "confidence": 0.0, "reason": "sem API key"}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=llm.FAST,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        try:  # F-E: custo por-funcao (telemetria nunca quebra a chamada real)
            llm_usage.record_response("task_reconciler.judge", llm.FAST, msg.model_dump())
        except Exception:
            pass
        raw = msg.content[0].text if msg.content else ""
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return {"done": False, "confidence": 0.0, "reason": "parse falhou"}
        data = json.loads(m.group(0))
        return {
            "done": bool(data.get("done")),
            "confidence": float(data.get("confidence") or 0.0),
            "reason": str(data.get("reason") or "")[:200],
        }
    except Exception as e:
        logger.warning(f"task_reconciler judge falhou (task {task['id']}): {e}")
        return {"done": False, "confidence": 0.0, "reason": f"erro: {e}"}


def _close_task(task, verdict):
    """Fecha a task + registra em agent_actions com undo_hint (reverte via mark_undone)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET status='completed', data_conclusao=NOW() WHERE id=%s AND status='pending'",
            (task["id"],),
        )
        conn.commit()
    from services.agent_actions import log_action
    log_action(
        action_type='task_resolved',
        category='tasks',
        title=f"Tarefa concluída (reconciler): {task['titulo']}",
        details=f"Fechada por comunicação direta. Confiança {verdict['confidence']:.2f}. {verdict['reason']}",
        scope_ref={'task_id': task['id'], 'contact_id': task.get('contact_id'), 'project_id': task.get('project_id')},
        source='task_reconciler',
        payload={'confidence': verdict['confidence'], 'reason': verdict['reason']},
        undo_hint=f"UPDATE tasks SET status='pending', data_conclusao=NULL WHERE id={task['id']}",
    )


async def _notify_closed(closed):
    """Pill passivo (urgência 3) — 'te conto o que fiz': lista o que fechou, POR QUÊ
    (a conversa que resolveu) e COMO DESFAZER (comando desfaz N, reusa run_smart_undo
    do agent_actions — o _close_task loga task_resolved+undo_hint). Não interrompe.
    Padrão espelha a camada esperta do inbox (email)."""
    from services.notification_router import notify
    lines = "\n".join(
        f"  • #{c['id']} {c['titulo']}" +
        (f" — {(c.get('reason') or '').strip()[:80]}" if c.get('reason') else "")
        for c in closed
    )
    msg = (
        f"🤖 Reconciliação (conversa/WA) — fechei {len(closed)} tarefa(s) que você "
        f"resolveu direto:\n{lines}\n\n"
        f"Se alguma foi engano, responde \"desfaz N\" (N = nº da tarefa) que eu reabro."
    )
    dedup = "tasks_reconciled:" + "-".join(str(c['id']) for c in sorted(closed, key=lambda x: x['id']))
    await notify(
        "task_reconciler", "Reconciliação: tarefas fechadas", msg, 3,
        msg_type="tasks_reconciled", dedup=dedup,
    )


# ==================== FASE 2: saída do on_hold ====================

def _fetch_on_hold_tasks():
    """Tasks parqueadas, com o ciclo de vida da migration 058."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, titulo, contact_id, project_id, data_vencimento,
                   atualizado_em, on_hold_since, on_hold_reason
            FROM tasks
            WHERE status = 'on_hold'
            ORDER BY id ASC
        """)
        return [dict(r) for r in cur.fetchall()]


def _classify_on_hold(task) -> None:
    """Carimba `on_hold_since`/`on_hold_reason` numa task parqueada que ainda não
    os tem — parqueio feito por SQL manual, pela UI ou pelo snooze da Tônia, que
    não declaram intenção.

    O default é `espera_terceiro`, porque é o que a convenção de 29/07 descreve e
    o que a CoS de fato faz ao parquear. `on_hold_since` recebe NOW(), não
    `atualizado_em`: o carimbo tem que valer a partir de AGORA, senão uma task
    parqueada hoje com vencimento antigo já nasceria fora da janela e reabriria
    na mesma run — parqueio sem efeito nenhum.

    Quem carimba nesta run NÃO é avaliado nela (carência de 1 run): a janela de
    espera precisa existir antes de poder estar esgotada."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE tasks
            SET on_hold_since = COALESCE(on_hold_since, NOW()),
                on_hold_reason = COALESCE(on_hold_reason, %s)
            WHERE id = %s AND status = 'on_hold'
        """, (REASON_ESPERA, task["id"]))
        conn.commit()


def _last_substantive_incoming(contact_id, since):
    """Mensagem do contato (INCOMING) posterior ao parqueio que NÃO é cortesia.

    O filtro de cortesia é o mesmo de raci_smart_updates, já calibrado em prod:
    sem ele, um "obrigado, bom domingo" reabre a task e a convenção vira ruído —
    é exatamente o falso-positivo que o check G da /cos levou um dia pra corrigir
    (uma cortesia às 16:11 ressuscitou item respondido às 16:10)."""
    from services.raci_smart_updates import _is_courtesy_only

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT conteudo, COALESCE(enviado_em, recebido_em, criado_em) AS ts
            FROM messages
            WHERE contact_id = %s
              AND direcao = 'incoming'
              AND conteudo IS NOT NULL AND conteudo <> ''
              AND COALESCE(enviado_em, recebido_em, criado_em) > %s
            ORDER BY ts DESC
            LIMIT %s
        """, (contact_id, since, MAX_MSGS_PER_TASK))
        msgs = [dict(r) for r in cur.fetchall()]

    for m in msgs:
        if not _is_courtesy_only(m["conteudo"] or ""):
            return m
    return None


def _now_naive_utc():
    """Agora em UTC SEM tzinfo, pra comparar com as colunas de `tasks`, que são
    TIMESTAMP naive gravadas em UTC.

    `datetime.now()` aqui seria naive LOCAL: coincide por acidente no Railway
    (UTC) e erra 3h rodando da máquina do Renato — a classe de bug do `eeb41ce`.
    `utcnow()` está deprecated em 3.12+. Ver a convenção no CLAUDE.md."""
    from services.tz import now_utc
    return now_utc().replace(tzinfo=None)


def _wait_deadline(task):
    """Fim da janela de espera: 7 dias após o MAIOR entre vencimento e parqueio.

    Usar só `data_vencimento` faria uma task re-parqueada com atraso antigo
    reabrir na run seguinte, para sempre (feedback_livelock_reprocessamento).
    Usar só `on_hold_since` daria 7 dias novos a quem já estava atrasado há um
    mês quando foi parqueada — a janela é de ESPERA, e a espera começou no
    vencimento."""
    base = task["on_hold_since"]
    dv = task.get("data_vencimento")
    if dv and dv > base:
        base = dv
    return base + timedelta(days=ON_HOLD_WAIT_DAYS)


def _reopen_task(task, reason_code: str, reason: str) -> None:
    """on_hold → pending, com undo e trilha.

    Limpa `on_hold_since`/`on_hold_reason`: sem isso, a mensagem que causou a
    reabertura seguiria sendo "posterior ao parqueio" para sempre e a task
    reabriria toda run mesmo depois de re-parqueada. Zerar significa que um
    re-parqueio futuro começa uma janela nova.

    `atualizado_em = NOW()` é deliberado — é o que arma a guarda de conflito do
    tasks_sync (atualizado_em > last_synced_at) e impede o pull do Google de
    passar por cima da decisão que acabou de ser tomada aqui."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE tasks
            SET status = 'pending',
                on_hold_since = NULL,
                on_hold_reason = NULL,
                atualizado_em = NOW()
            WHERE id = %s AND status = 'on_hold'
        """, (task["id"],))
        conn.commit()

    since = task["on_hold_since"]
    since_sql = f"'{since:%Y-%m-%d %H:%M:%S}'" if since else "NULL"
    prev_reason = task.get("on_hold_reason") or REASON_ESPERA
    from services.agent_actions import log_action
    log_action(
        action_type='task_reopened',
        category='tasks',
        title=f"Tarefa reaberta ({reason_code}): {task['titulo']}",
        details=reason,
        scope_ref={'task_id': task['id'], 'contact_id': task.get('contact_id'), 'project_id': task.get('project_id')},
        source='task_reconciler.on_hold_sweep',
        payload={'trigger': reason_code, 'reason': reason, 'on_hold_since': str(since)},
        undo_hint=(
            f"UPDATE tasks SET status='on_hold', on_hold_since={since_sql}, "
            f"on_hold_reason='{prev_reason}' WHERE id={task['id']}"
        ),
    )


def sweep_on_hold(dry_run: bool = False) -> dict:
    """FASE 2 — devolve ao radar a task parqueada cuja espera terminou.

    Dois gatilhos, ambos determinísticos (sem LLM: são fatos, não julgamento):
      (a) RESPOSTA — o contato mandou algo substantivo depois do parqueio. A bola
          voltou pro Renato, então a espera acabou (tenha ela resolvido ou não —
          se resolveu, a fase de fechamento fecha no mesmo run).
      (b) JANELA — passaram os 7 dias sem resposta. O terceiro sumiu; volta a
          vencida pro Renato decidir se cobra.

    `parqueio_indefinido` está fora dos DOIS de propósito: é frente que o Renato
    tirou do radar sem prazo, e reabri-la por tempo devolveria ao portão o que
    foi silenciado por decisão. Ela só sai de lá por gesto humano."""
    if not is_on_hold_sweep_enabled():
        logger.info("on_hold_sweep: desligado (kill-switch)")
        return {"disabled": True}

    tasks = _fetch_on_hold_tasks()
    classified, reopened, held = [], [], []

    for task in tasks:
        # Parqueio sem intenção declarada: carimba e deixa maturar 1 run.
        if not task.get("on_hold_since") or not task.get("on_hold_reason"):
            if not dry_run:
                _classify_on_hold(task)
            classified.append({"id": task["id"], "titulo": task["titulo"]})
            continue

        if task["on_hold_reason"] == REASON_INDEFINIDO:
            held.append({"id": task["id"], "motivo": REASON_INDEFINIDO})
            continue

        trigger, reason = None, None

        if task.get("contact_id"):
            msg = _last_substantive_incoming(task["contact_id"], task["on_hold_since"])
            if msg:
                trigger = "reply"
                reason = (
                    f"O contato respondeu em {msg['ts']:%d/%m %H:%M} "
                    f"(parqueada em {task['on_hold_since']:%d/%m}): "
                    f"\"{(msg['conteudo'] or '')[:120]}\""
                )

        if not trigger:
            deadline = _wait_deadline(task)
            agora = _now_naive_utc()
            if agora > deadline:
                dias = (agora - deadline).days + ON_HOLD_WAIT_DAYS
                trigger = "aging"
                reason = (
                    f"{dias} dias de espera sem resposta (janela de "
                    f"{ON_HOLD_WAIT_DAYS} dias esgotada em {deadline:%d/%m}) — "
                    f"hora de decidir se cobra."
                )

        if trigger:
            rec = {"id": task["id"], "titulo": task["titulo"],
                   "trigger": trigger, "reason": reason}
            if not dry_run:
                _reopen_task(task, trigger, reason)
            reopened.append(rec)
        else:
            held.append({"id": task["id"], "motivo": "dentro da janela",
                         "ate": f"{_wait_deadline(task):%d/%m}"})

    summary = {
        "disabled": False,
        "dry_run": dry_run,
        "scanned": len(tasks),
        "classified": len(classified),      # carimbadas agora, avaliadas no próximo run
        "reopened": len(reopened),
        "still_held": len(held),
        "items": reopened,
        "classified_items": classified,
        "held_items": held,
    }
    logger.info(f"on_hold_sweep: {summary}")
    return summary


async def _notify_reopened(reopened):
    """Pill passivo (urgência 3), mesmo padrão do _notify_closed: conta o que
    voltou pro radar, POR QUÊ, e como desfazer. Reabertura NÃO interrompe — ela
    devolve a task pro portão, que é onde o Renato já olha."""
    from services.notification_router import notify
    lines = "\n".join(
        f"  • #{r['id']} {r['titulo']} — "
        f"{'o contato respondeu' if r['trigger'] == 'reply' else 'passou a janela de espera'}"
        for r in reopened
    )
    msg = (
        f"⏰ Espera encerrada — devolvi {len(reopened)} tarefa(s) parqueada(s) "
        f"pro radar:\n{lines}\n\n"
        f"Se alguma deve continuar parada, responde \"desfaz N\" que eu parqueio de volta."
    )
    dedup = "tasks_reopened:" + "-".join(str(r['id']) for r in sorted(reopened, key=lambda x: x['id']))
    await notify(
        "task_reconciler", "Espera encerrada: tarefas de volta ao radar", msg, 3,
        msg_type="tasks_reopened", dedup=dedup,
    )


async def run_task_reconciler(dry_run: bool = False) -> dict:
    """Sweep. Fecha tasks pending resolvidas por comunicação direta.
    dry_run=True: julga e loga o que fecharia, mas NÃO fecha nem notifica."""
    if not is_reconciler_enabled():
        logger.info("task_reconciler: desligado (kill-switch)")
        return {"disabled": True}

    # FASE 2 ANTES da fase 1, de propósito: task reaberta por resposta do contato
    # vira `pending` e entra no escopo de fechamento no MESMO run. Sem essa ordem,
    # uma espera que a resposta já resolveu passaria 24h como pendente vencida
    # antes de o julgamento a fechar — o pior dos dois mundos pro Renato.
    on_hold = sweep_on_hold(dry_run=dry_run)

    candidates, n_sem_contato = _fetch_candidate_tasks()
    judged = 0
    closed = []
    would_close = []

    for task in candidates:
        msgs = _fetch_messages_since(task["contact_id"], task["data_criacao"])
        if not msgs:
            continue
        verdict = _judge(task, msgs)
        judged += 1
        if verdict["done"] and verdict["confidence"] >= CONFIDENCE_THRESHOLD:
            rec = {
                "id": task["id"], "titulo": task["titulo"],
                "confidence": verdict["confidence"], "reason": verdict["reason"],
            }
            if dry_run:
                would_close.append(rec)
            else:
                _close_task(task, verdict)
                closed.append(rec)

    if closed and not dry_run:
        await _notify_closed(closed)

    # Task reaberta que a fase 1 fechou no mesmo run não é "de volta ao radar" —
    # é resolvida. Sai do aviso de reabertura pra não contar a mesma história duas
    # vezes com desfechos diferentes.
    closed_ids = {c["id"] for c in closed}
    reopened_net = [r for r in (on_hold.get("items") or []) if r["id"] not in closed_ids]
    if reopened_net and not dry_run:
        await _notify_reopened(reopened_net)

    summary = {
        "disabled": False,
        "dry_run": dry_run,
        "scanned_with_contact": len(candidates),
        "skipped_no_contact": n_sem_contato,  # v0 boundary — logado, sem cap silencioso
        "judged": judged,
        "closed": len(closed),
        "would_close": len(would_close),
        "items": (would_close if dry_run else closed),
        "on_hold_sweep": on_hold,
        "reopened_net": len(reopened_net),
    }
    logger.info(f"task_reconciler: {summary}")
    return summary
