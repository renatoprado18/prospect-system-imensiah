"""
Tonha — Ear Mode (pergunta proativa fim de tarde).

Cron diario ~18h SP. A Tonha manda 1 mensagem aberta convidando o Renato
a contar como foi o dia / divagar / refletir. A resposta dele vira input
prioritario pro extractor noturno (cos_extractor.py rodando 22:30 SP).

Why: a maioria das interacoes diarias com o bot e transacional ("faz X",
"me lembra Y"). Falta espaco pro Renato falar fora da pauta — que e onde
moram os fatos que viram boa memoria (preferencias, padroes, glossario,
relacionamentos). Ear mode abre 1 janela por dia.

Policy CoS Config:
- Skip domingo (politica C2 — zero trabalho/notificacao no domingo)
- Skip se ja teve >5 turns do Renato no bot hoje (ele ja conversou bastante)
- Skip se Renato pediu silencio (correcao tipo silencio)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from database import get_db
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)


# Variantes do convite — Tonha escolhe pseudo-aleatoria por dia da semana
# pra nao parecer roboticamente repetitiva. Tom: calmo, sem pressao.
_INVITES = [
    "Como foi o dia? Tem alguma coisa que voce queira me contar?",
    "Fim de tarde. Se quiser desabafar, refletir em voz alta, ou so partilhar como esta — estou aqui.",
    "Se voce quiser falar algo da semana — coisa que ficou pendente na cabeca, decisao que esta amadurecendo, ou so contar como foi o dia — me conta. Sem pauta.",
    "Tudo bem por ai? Se tiver algo que voce queira pensar em voz alta comigo, manda — texto ou audio.",
    "Cheguei pra perguntar do dia. Voce conta o que quiser, do jeito que quiser.",
    "Fim do expediente. Tem algo do dia que esta ficando contigo? Pode contar.",
    "Como esta a cabeca hoje?",
]


def _pick_invite() -> str:
    """Escolhe convite pelo dia do ano — variacao previsivel mas nao repetitiva."""
    day_of_year = to_brt(now_utc()).timetuple().tm_yday
    return _INVITES[day_of_year % len(_INVITES)]


def _should_skip() -> Dict[str, Any]:
    """Aplica politica CoS antes de pingar."""
    now_brt_dt = to_brt(now_utc())
    weekday = now_brt_dt.weekday()  # 0=segunda, 6=domingo

    # Domingo: politica C2, zero trabalho
    if weekday == 6:
        return {"skip": True, "reason": "domingo (politica C2)"}

    # Ja conversou bastante hoje? Pula pra nao ser invasivo.
    try:
        from services.intel_bot import RENATO_PHONE
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM bot_conversations
                WHERE phone = %s AND role = 'user'
                  AND created_at >= (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                                    AT TIME ZONE 'America/Sao_Paulo'
                """,
                (RENATO_PHONE,),
            )
            row = cur.fetchone()
            n_user_turns = (row.get("n") if row else 0) or 0
    except Exception as e:
        logger.warning(f"ear_mode: falha ao contar turns ({e}); seguindo")
        n_user_turns = 0

    if n_user_turns >= 5:
        return {"skip": True, "reason": f"ja teve {n_user_turns} turns hoje"}

    # Ja mandei o invite hoje? (idempotencia em caso de cron disparar 2x)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id FROM bot_conversations
                WHERE phone = %s AND role = 'assistant'
                  AND tool_calls::text LIKE '%%"ear_mode"%%'
                  AND created_at >= (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                                    AT TIME ZONE 'America/Sao_Paulo'
                LIMIT 1
                """,
                (os.getenv("RENATO_PHONE_OVERRIDE", "5511984153337"),),
            )
            if cur.fetchone():
                return {"skip": True, "reason": "ear_mode ja enviado hoje"}
    except Exception as e:
        logger.warning(f"ear_mode: idempotencia check falhou ({e}); seguindo")

    return {"skip": False}


def run_ear_mode_prompt() -> Dict[str, Any]:
    """Manda 1 mensagem proativa convidando Renato a falar do dia.

    Reusa _tool_send_wa_to_renato do cos_sensor (modo natural text — sem
    header CoS Patrol, ja na voz da Tonha).
    """
    started_brt = to_brt(now_utc())

    gate = _should_skip()
    if gate.get("skip"):
        return {
            "status": "skipped",
            "reason": gate.get("reason"),
            "at_brt": started_brt.isoformat(),
        }

    invite_text = _pick_invite()

    try:
        from services.cos_sensor import _tool_send_wa_to_renato
        result = _tool_send_wa_to_renato(
            text=invite_text,
            urgency="low",
            proposed_action={"action": "ear_mode", "type": "open_invite"},
        )
        # Marca o turn com flag ear_mode na metadata pra extractor priorizar
        # e pra idempotencia checar amanha.
        if result.get("success"):
            try:
                bid = result.get("result", {}).get("bot_conversation_id")
                if bid:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            """
                            UPDATE bot_conversations
                            SET tool_calls = jsonb_set(
                                COALESCE(tool_calls, '{}'::jsonb),
                                '{ear_mode}', 'true'::jsonb
                            )
                            WHERE id = %s
                            """,
                            (bid,),
                        )
                        conn.commit()
            except Exception as e:
                logger.warning(f"ear_mode: nao consegui taggear bot_conversation: {e}")

        return {
            "status": "sent" if result.get("success") else "failed",
            "invite_text": invite_text,
            "send_result": result,
            "at_brt": started_brt.isoformat(),
        }
    except Exception as e:
        logger.exception(f"ear_mode falhou: {e}")
        return {
            "status": "error",
            "error": str(e),
            "at_brt": started_brt.isoformat(),
        }
