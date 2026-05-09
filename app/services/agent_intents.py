"""
Agent Intents — P6 Diligente (Fase 1)

Persiste intents abertos do INTEL bot. Cada mensagem nova checa intents abertos
ANTES de responder pra que ele cumpra palavra ao inves de prometer e esquecer.

Conceito (do design fechado em 08/05/2026 com Renato):
- Intent = compromisso multi-step que o bot abriu (write executou e pode ter mais
  a fazer; user pediu acao em massa; bot admitiu falta de tool).
- Status: open -> in_progress -> completed | cancelled | blocked.
- Detector automatico (write_tool_called OR imperativo plural OR bot admitiu falha)
  evita exigir que o bot lembre de chamar manage_intent.
- Auto-pickup em toda msg + cron 30min (Fase 2) garantem que o bot tenta progredir.

Memoria de design: project_inteligencia_real.md (secao P6).
Schema: scripts/migrations/007_agent_intents.sql.
"""
import re
import json
import logging
from typing import Dict, List, Optional, Any

from database import get_db

logger = logging.getLogger(__name__)


# ==================== HEURISTICAS DO DETECTOR ====================
# Imperativo verbal explicito + objeto plural ("as N", "todas", "todos", "tudo")
# -> usuario pediu acao em massa, abre intent pra rastrear ate completar.
# IMPORTANTE: imperativo (3a pessoa), nao futuro ("vou") nem passado ("fiz").
_IMPERATIVE_VERBS = (
    # Cobre 2a-pessoa (imperativo direto: "vincule"/"vincula") +
    # subjuntivo presente como imperativo polido (ex: "faca isso", "vincule por favor").
    # Ordem importa: alternativas mais especificas antes pra nao ser ofuscadas.
    "vincul[ae]|associ[ae]|relacion[ae]|conect[ae]|"
    "agend[ae]|marc[ae]|cri[ae]|"
    "envi[ae]|mand[ae]|despach[ae]|"
    "atualiz[ae]|edit[ae]|alter[ae]|modific[ae]|move|mov[ae]|remarc[ae]|reagend[ae]|"
    "delet[ae]|apag[ae]|remov[ae]|exclu[aei]|"
    "execut[ae]|fa[cç][ae]|f[ae]z|faz\\b|realiz[ae]|rod[ae]|process[ae]|"
    "salv[ae]|guard[ae]|persist[ae]|"
    "conclu[aei]|complet[ae]|finaliz[ae]|fech[ae]|encerr[ae]"
)
_PLURAL_OBJECTS = (
    r"as\s+\d+|os\s+\d+|"
    r"todas\s+as|todos\s+os|"
    r"todas|todos|"
    r"cada\s+(um|uma)|"
    r"tudo|"
    r"\d{2,}\s+(tarefas|itens|emails|mensagens|eventos|contatos|posts|notas)"
)
_USER_IMPERATIVE_RE = re.compile(
    rf"\b({_IMPERATIVE_VERBS})\s+([^.!?\n]{{0,80}}\b({_PLURAL_OBJECTS}))",
    re.IGNORECASE,
)

# Bot admitiu falta/falha — abre intent BLOCKED pra escalar depois.
_BLOCKER_PATTERNS = [
    r"\bn[ãa]o\s+(consigo|tenho\s+como|posso|sei\s+como)\b",
    r"\bfalta\s+(tool|ferramenta|permiss[ãa]o|acesso|integra[çc][ãa]o|API)\b",
    r"\bn[ãa]o\s+(est[áa]\s+dispon[íi]vel|tem\s+ferramenta|existe\s+tool)\b",
    r"\bfora\s+do\s+meu\s+alcance\b",
    r"\bn[ãa]o\s+sei\s+(qual|onde|como)\b.*(\bID|integra[çc][ãa]o|endpoint)",
]
_BLOCKER_RE = re.compile("|".join(_BLOCKER_PATTERNS), re.IGNORECASE)


# ==================== CRUD ====================

def open_intent(
    intent_text: str,
    intent_type: Optional[str] = None,
    related_message_id: Optional[int] = None,
    status: str = "open",
    blocker: Optional[str] = None,
    next_step_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Cria novo intent. Retorna dict com a row inserida."""
    if not intent_text or not intent_text.strip():
        raise ValueError("intent_text e obrigatorio")
    if status not in ("open", "in_progress", "blocked", "completed", "cancelled"):
        raise ValueError(f"status invalido: {status}")
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO agent_intents
                    (intent_text, intent_type, status, related_message_id, blocker, next_step_hint)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, intent_text, intent_type, status, steps_done,
                          next_step_hint, blocker, related_message_id,
                          created_at, updated_at, completed_at, escalated_at
                """,
                (intent_text.strip(), intent_type, status, related_message_id, blocker, next_step_hint),
            )
            row = cursor.fetchone()
            conn.commit()
        result = dict(row) if row else {}
        logger.info(
            f"agent_intent.open id={result.get('id')} type={intent_type} "
            f"status={status} text={intent_text[:80]}"
        )
        return result
    except Exception as e:
        logger.error(f"open_intent error: {e}")
        raise


def update_intent(
    intent_id: int,
    *,
    status: Optional[str] = None,
    steps_done: Optional[List[Any]] = None,
    next_step_hint: Optional[str] = None,
    blocker: Optional[str] = None,
    intent_text: Optional[str] = None,
) -> Dict[str, Any]:
    """UPDATE seletivo. Auto-set updated_at = NOW(). Auto-set completed_at quando status='completed'."""
    if not intent_id:
        raise ValueError("intent_id e obrigatorio")

    sets = []
    values: List[Any] = []

    if status is not None:
        if status not in ("open", "in_progress", "blocked", "completed", "cancelled"):
            raise ValueError(f"status invalido: {status}")
        sets.append("status = %s")
        values.append(status)
        if status == "completed":
            sets.append("completed_at = (NOW() AT TIME ZONE 'UTC')")
        # Quando blocker eh resetado (status passa de blocked pra outra coisa),
        # zera escalated_at pra que se virar blocked de novo (com novo blocker)
        # possa ser escalado novamente. Sem isso, blocker novo no mesmo intent
        # nao re-escalava. Trade-off aceito: cada vez que destrava+retrava re-escala.
        if status != "blocked":
            sets.append("escalated_at = NULL")
    if steps_done is not None:
        sets.append("steps_done = %s")
        values.append(json.dumps(steps_done))
    if next_step_hint is not None:
        sets.append("next_step_hint = %s")
        values.append(next_step_hint)
    if blocker is not None:
        sets.append("blocker = %s")
        values.append(blocker)
    if intent_text is not None:
        sets.append("intent_text = %s")
        values.append(intent_text)

    if not sets:
        raise ValueError("Nenhum campo passado pra update_intent")

    # Sempre atualiza updated_at
    sets.append("updated_at = (NOW() AT TIME ZONE 'UTC')")
    values.append(intent_id)

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE agent_intents SET {', '.join(sets)}
                WHERE id = %s
                RETURNING id, intent_text, intent_type, status, steps_done,
                          next_step_hint, blocker, related_message_id,
                          created_at, updated_at, completed_at, escalated_at
                """,
                tuple(values),
            )
            row = cursor.fetchone()
            conn.commit()
        if not row:
            raise ValueError(f"intent #{intent_id} nao encontrado")
        result = dict(row)
        logger.info(
            f"agent_intent.update id={intent_id} status={result.get('status')} "
            f"steps={len(result.get('steps_done') or [])}"
        )
        return result
    except Exception as e:
        logger.error(f"update_intent error id={intent_id}: {e}")
        raise


def cancel_intent(intent_id: int) -> Dict[str, Any]:
    """Atalho pra update_intent(status='cancelled')."""
    return update_intent(intent_id, status="cancelled")


def append_step(intent_id: int, step: Any, *, status: str = "in_progress") -> Dict[str, Any]:
    """Adiciona step ao steps_done. Default: marca status como in_progress.

    step pode ser str (descricao curta) ou dict (mais estruturado).
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT steps_done FROM agent_intents WHERE id = %s",
                (intent_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"intent #{intent_id} nao encontrado")
            existing = row.get("steps_done") if isinstance(row, dict) else row[0]
            if isinstance(existing, str):
                existing = json.loads(existing)
            elif existing is None:
                existing = []
        new_steps = list(existing) + [step]
        return update_intent(intent_id, steps_done=new_steps, status=status)
    except Exception as e:
        logger.error(f"append_step error id={intent_id}: {e}")
        raise


def get_open_intents(limit: int = 10) -> List[Dict[str, Any]]:
    """SELECT WHERE status IN ('open','in_progress') ORDER BY created_at DESC."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, intent_text, intent_type, status, steps_done,
                       next_step_hint, blocker, related_message_id,
                       created_at, updated_at, completed_at, escalated_at
                FROM agent_intents
                WHERE status IN ('open', 'in_progress')
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall() or []
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_open_intents error: {e}")
        return []


def get_intent(intent_id: int) -> Optional[Dict[str, Any]]:
    """SELECT por id (qualquer status)."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, intent_text, intent_type, status, steps_done,
                       next_step_hint, blocker, related_message_id,
                       created_at, updated_at, completed_at, escalated_at
                FROM agent_intents
                WHERE id = %s
                """,
                (intent_id,),
            )
            row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_intent error id={intent_id}: {e}")
        return None


# ==================== FORMATTING / DEDUP ====================

def _steps_count(steps_done: Any) -> int:
    """Robusto: steps_done pode ser list, str JSON, ou None."""
    if steps_done is None:
        return 0
    if isinstance(steps_done, list):
        return len(steps_done)
    if isinstance(steps_done, str):
        try:
            parsed = json.loads(steps_done)
            return len(parsed) if isinstance(parsed, list) else 0
        except (json.JSONDecodeError, TypeError):
            return 0
    return 0


def format_intents_for_prompt(intents: List[Dict[str, Any]]) -> str:
    """Formato conciso pra injetar no system prompt do bot.

    Cada intent: '- [#ID] texto (status, N passos feitos)' + linha extra
    com next_step_hint OR blocker quando presentes.
    """
    if not intents:
        return ""
    lines = []
    for it in intents:
        steps = _steps_count(it.get("steps_done"))
        text = (it.get("intent_text") or "").strip().replace("\n", " ")
        if len(text) > 140:
            text = text[:137] + "..."
        line = f"- [#{it['id']}] {text} ({it.get('status', 'open')}, {steps} passos feitos)"
        hint = (it.get("next_step_hint") or "").strip()
        blocker = (it.get("blocker") or "").strip()
        if blocker:
            line += f"\n    bloqueado: {blocker[:120]}"
        elif hint:
            line += f"\n    proximo: {hint[:120]}"
        lines.append(line)
    return "\n".join(lines)


def find_similar_open_intent(intent_text: str) -> Optional[Dict[str, Any]]:
    """Acha intent open/in_progress com texto similar pra evitar duplicatas.

    Heuristica: prefix-match das primeiras 30 chars (case-insensitive).
    Se as duas frases comecam parecido, considera mesmo intent.
    Cobre casos reais: "vincular todas as tarefas LinkedIn ao projeto editorial"
    vs "vincular todas as tarefas LinkedIn ao editorial" — ambos batem no
    prefix "vincular todas as tarefas link".

    Trade-off conhecido: pode dar FP em pedidos diferentes que comecam igual
    ("agendar reuniao com X" vs "agendar reuniao com Y"). Aceitavel pra Fase 1
    porque dedup e melhor que duplicata; user pode cancelar manual se errar.
    """
    if not intent_text or not intent_text.strip():
        return None
    needle = intent_text.strip().lower()[:30]
    if len(needle) < 10:
        # Texto curto demais — chance de FP gigante
        return None
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # Match prefix bidirecional: novo prefix bate com algum aberto, OU
            # algum aberto tem prefix que bate em alguma parte do novo.
            cursor.execute(
                """
                SELECT id, intent_text, intent_type, status, steps_done,
                       next_step_hint, blocker, related_message_id,
                       created_at, updated_at, completed_at, escalated_at
                FROM agent_intents
                WHERE status IN ('open', 'in_progress')
                  AND (
                      LOWER(intent_text) LIKE %s
                      OR LOWER(SUBSTRING(intent_text, 1, 30)) = %s
                      OR %s LIKE LOWER(SUBSTRING(intent_text, 1, 30)) || '%%'
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (f"{needle}%", needle, needle),
            )
            row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"find_similar_open_intent error: {e}")
        return None


# ==================== DETECTOR ====================

def detect_intent_from_turn(
    user_message: str,
    write_action_called: bool,
    response_text: str,
    *,
    write_partial: bool = False,
) -> Optional[Dict[str, Any]]:
    """Heuristica: deve abrir intent pra esse turn?

    Retorna dict com {should_open, intent_text, intent_type, status, blocker}
    OU None quando o turn e puro chat/coach/query e nao gera compromisso.

    Regras (do design 08/05) — ordem de prioridade no codigo:
    1. response_text admite falta/falha ("nao consigo", "falta tool") -> abre BLOCKED.
       (PRIORIDADE: gap precisa de visibilidade mais que progresso parcial.)
    2. write_action_called=True -> abre multi_step_action (bot agiu, talvez mais a fazer).
       Quando write_partial=True (ex: postpone afetou N de M), abre como in_progress.
    3. user_message tem imperativo plural ("vincule todas", "faca os 5", "atualiza tudo")
       -> abre multi_step_action (user pediu acao em massa, rastrear ate completar).
    4. Caso contrario -> None (chat/coach/query puro, sem compromisso).
    """
    user_msg = (user_message or "").strip()
    response = (response_text or "").strip()

    # 3) Bot admitiu bloqueio — prioridade alta, sempre escala mesmo sem write
    blocker_match = _BLOCKER_RE.search(response) if response else None
    if blocker_match:
        excerpt = response[max(0, blocker_match.start() - 30): blocker_match.end() + 80].strip()
        return {
            "should_open": True,
            "intent_text": user_msg[:300] or "(intent sem texto do user)",
            "intent_type": "blocked_request",
            "status": "blocked",
            "blocker": excerpt[:300],
        }

    # 1) Write executou — abre intent pra rastrear se ha mais a fazer
    if write_action_called:
        return {
            "should_open": True,
            "intent_text": user_msg[:300] or "(intent sem texto do user)",
            "intent_type": "multi_step_action",
            "status": "in_progress" if write_partial else "open",
            "blocker": None,
        }

    # 2) User pediu acao em massa via imperativo, mas bot nao executou ainda
    if user_msg and _USER_IMPERATIVE_RE.search(user_msg):
        return {
            "should_open": True,
            "intent_text": user_msg[:300],
            "intent_type": "multi_step_action",
            "status": "open",
            "blocker": None,
        }

    return None


def maybe_open_intent_for_turn(
    user_message: str,
    write_action_called: bool,
    response_text: str,
    *,
    related_message_id: Optional[int] = None,
    write_partial: bool = False,
) -> Optional[Dict[str, Any]]:
    """Wrapper alto-nivel: detecta + dedup + insere.

    1. Roda detect_intent_from_turn.
    2. Se sim, checa find_similar_open_intent — se ja existe, atualiza
       updated_at via append_step (registra nova ocorrencia) ao inves de
       criar duplicata.
    3. Caso contrario insere novo.

    Returns: dict do intent (novo ou existente atualizado), OU None.
    """
    decision = detect_intent_from_turn(
        user_message, write_action_called, response_text, write_partial=write_partial
    )
    if not decision or not decision.get("should_open"):
        return None

    intent_text = decision["intent_text"]
    try:
        existing = find_similar_open_intent(intent_text)
        if existing:
            # Anexa step "user reiterou pedido" pra manter rastro temporal
            try:
                step = {
                    "kind": "user_reiterated",
                    "message_id": related_message_id,
                    "snippet": intent_text[:120],
                }
                # Mantem status existente — nao force in_progress se ja era blocked
                current_status = existing.get("status", "in_progress")
                target_status = current_status if current_status in ("blocked", "in_progress") else "in_progress"
                return append_step(existing["id"], step, status=target_status)
            except Exception as e:
                logger.warning(f"maybe_open_intent_for_turn append_step failed: {e}")
                return existing

        return open_intent(
            intent_text=intent_text,
            intent_type=decision.get("intent_type"),
            related_message_id=related_message_id,
            status=decision.get("status", "open"),
            blocker=decision.get("blocker"),
        )
    except Exception as e:
        logger.error(f"maybe_open_intent_for_turn error: {e}")
        return None


# ==================== ESCALATION (Fase 2) ====================

# Threshold pra escalar: blocker velho. 60min = 2 ticks de 30min sem progresso.
# Renato pode rever se virar ruidoso — easy ajuste.
ESCALATION_AGE_MINUTES = 60


def get_blocked_intents_to_escalate(age_minutes: int = ESCALATION_AGE_MINUTES) -> List[Dict[str, Any]]:
    """SELECT intents blocked + nao escalados + updated_at > age_minutes atras.

    Usado pelo cron tick pra disparar WhatsApp ao Renato. Index parcial
    `idx_agent_intents_escalated` cobre o filtro pra performance.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, intent_text, intent_type, status, steps_done,
                       next_step_hint, blocker, related_message_id,
                       created_at, updated_at, completed_at, escalated_at
                FROM agent_intents
                WHERE status = 'blocked'
                  AND escalated_at IS NULL
                  AND updated_at < (NOW() AT TIME ZONE 'UTC') - (%s || ' minutes')::interval
                ORDER BY updated_at ASC
                """,
                (str(age_minutes),),
            )
            rows = cursor.fetchall() or []
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_blocked_intents_to_escalate error: {e}")
        return []


def mark_escalated(intent_id: int) -> bool:
    """Seta escalated_at = NOW() pra dedup. Retorna True em sucesso."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_intents
                SET escalated_at = (NOW() AT TIME ZONE 'UTC')
                WHERE id = %s
                RETURNING id
                """,
                (intent_id,),
            )
            row = cursor.fetchone()
            conn.commit()
        return bool(row)
    except Exception as e:
        logger.error(f"mark_escalated error id={intent_id}: {e}")
        return False


async def escalate_blocked_intents(age_minutes: int = ESCALATION_AGE_MINUTES) -> List[Dict[str, Any]]:
    """Escala intents blocked velhos via WhatsApp pro Renato.

    - Filtra por status='blocked' AND escalated_at IS NULL AND updated_at < NOW - age.
    - Pra cada um, manda WA + UPDATE escalated_at = NOW().
    - Audit log `agent_intent.escalated`.

    Retorna lista de intents escalados nesta rodada (pra que o cron mostre no resumo).
    Falha silenciosa de WA nao bloqueia outros — registra no log e segue.
    """
    intents = get_blocked_intents_to_escalate(age_minutes=age_minutes)
    if not intents:
        return []

    # Imports locais — evita ciclo (intel_bot importa daqui).
    try:
        from services.intel_bot import send_intel_notification  # noqa: WPS433
    except Exception as e:
        logger.error(f"escalate_blocked_intents: cannot import send_intel_notification: {e}")
        return []
    try:
        from services.agent_actions import log_action  # noqa: WPS433
    except Exception:
        log_action = None  # type: ignore[assignment]

    escalated: List[Dict[str, Any]] = []
    for it in intents:
        text = (it.get("intent_text") or "").strip().replace("\n", " ")
        blocker = (it.get("blocker") or "(sem motivo registrado)").strip()
        msg = (
            f"🚧 Intent #{it['id']} travado:\n"
            f"*{text[:120]}*\n"
            f"Motivo: {blocker[:200]}\n\n"
            f"Reponde \"destrava {it['id']}\" pra retomar, "
            f"\"esquece {it['id']}\" pra cancelar, ou ignora."
        )
        ok = False
        try:
            ok = await send_intel_notification(msg)
        except Exception as e:
            logger.error(f"escalate_blocked_intents WA send error id={it['id']}: {e}")

        if ok:
            mark_escalated(it["id"])
            escalated.append(it)
            if log_action:
                try:
                    log_action(
                        action_type="agent_intent.escalated",
                        category="system",
                        title=f"Intent #{it['id']} escalado via WhatsApp",
                        details=f"Blocker: {blocker[:200]}",
                        scope_ref={"intent_id": it["id"]},
                        source="agent_intents_tick",
                        payload={"intent_text": text[:300], "blocker": blocker[:300]},
                    )
                except Exception as e:
                    logger.warning(f"escalate_blocked_intents log_action failed: {e}")
        else:
            logger.warning(f"escalate_blocked_intents: WA send failed id={it['id']}, deixando pra proxima rodada")

    return escalated
