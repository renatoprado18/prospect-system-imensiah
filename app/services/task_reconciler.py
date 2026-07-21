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
"""
import json
import logging
import os
import re
from database import get_db
from services import llm, llm_usage

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.85
MAX_MSGS_PER_TASK = 8
MAX_MSG_CHARS = 500


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
    """Pill passivo (urgência 3) — lista o que fechou. Não interrompe."""
    from services.notification_router import route_to_renato
    lines = "\n".join(f"  - #{c['id']} {c['titulo']}" for c in closed)
    msg = (
        f"✅ {len(closed)} tarefa(s) fechada(s) automaticamente "
        f"(resolvidas por conversa direta):\n{lines}\n\n"
        f"Se alguma foi engano, me avisa que reabro."
    )
    dedup = "tasks_reconciled:" + "-".join(str(c['id']) for c in sorted(closed, key=lambda x: x['id']))
    await route_to_renato(
        source="task_reconciler",
        payload={"title": "Tarefas fechadas pelo reconciler", "body": msg},
        msg_type="tasks_reconciled",
        urgency_score=3,
        dedup_key=dedup,
        message_text=msg,
    )


async def run_task_reconciler(dry_run: bool = False) -> dict:
    """Sweep. Fecha tasks pending resolvidas por comunicação direta.
    dry_run=True: julga e loga o que fecharia, mas NÃO fecha nem notifica."""
    if not is_reconciler_enabled():
        logger.info("task_reconciler: desligado (kill-switch)")
        return {"disabled": True}

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

    summary = {
        "disabled": False,
        "dry_run": dry_run,
        "scanned_with_contact": len(candidates),
        "skipped_no_contact": n_sem_contato,  # v0 boundary — logado, sem cap silencioso
        "judged": judged,
        "closed": len(closed),
        "would_close": len(would_close),
        "items": (would_close if dry_run else closed),
    }
    logger.info(f"task_reconciler: {summary}")
    return summary
