"""
Proactive Signals — INTEL bot inicia conversa quando detecta sinal acionavel.

Inteligencia Real P4 (proatividade). MVP: trigger pos-reuniao (#335).
Roadmap: #336 grupo WA pico, #337 decay relacionamento, #338 oportunidade
cruzada — todos compartilham a tabela proactive_signals pra dedup.

Cada signal_type usa ref_id como chave de dedup (ex: post_meeting -> event_id).
UNIQUE(signal_type, ref_id) garante que cada sinal so dispara uma vez.

Politica:
- So manda WA quando faz sentido interromper (acionavel + recente)
- Limite implicito: trigger pos-reuniao roda a cada 30min e so pega o que
  acabou nos ultimos 60min — naturalmente bounded
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from database import get_db

logger = logging.getLogger(__name__)


def _already_sent(signal_type: str, ref_id: str) -> bool:
    """Checa dedup via tabela proactive_signals."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM proactive_signals WHERE signal_type=%s AND ref_id=%s LIMIT 1",
                (signal_type, str(ref_id)),
            )
            return cur.fetchone() is not None
    except Exception as e:
        logger.warning(f"_already_sent err: {e}")
        return False


def _record_sent(signal_type: str, ref_id: str, payload: Optional[Dict] = None) -> None:
    """Marca sinal como enviado (idempotente via UNIQUE)."""
    try:
        import json
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO proactive_signals (signal_type, ref_id, payload, sent_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (signal_type, ref_id) DO NOTHING
                """,
                (signal_type, str(ref_id), json.dumps(payload or {})),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"_record_sent err: {e}")


async def check_post_meeting() -> Dict:
    """Trigger #335: detecta reunioes terminadas nos ultimos 30-60min e
    pergunta ao Renato 'como foi?'.

    Filtros (conservadores pra evitar spam):
    - end_datetime entre NOW()-60min e NOW()-15min (janela de pos-reuniao
      imediata; antes de 15min ainda e cedo, depois de 60min ja perdeu o timing)
    - duracao >= 15min (skip blocos curtos / pausas)
    - contact_id IS NOT NULL (eventos pessoais sem contato linkado nao geram)
    - status = 'confirmed' (pula tentativas de eventos cancelados)
    - signal nao registrado em proactive_signals com signal_type='post_meeting'

    Mensagem WA proativa:
        🎯 Reuniao terminou {Xmin} atras:
        *{summary}* as {HH:MM} ({duration}min)
        {contact_name} - {empresa}

        Como foi? Posso:
        - Salvar memoria da conversa
        - Criar tarefa de follow-up
        - Atualizar o contato

        So me conta o que rolou que eu organizo.
    """
    from services.intel_bot import send_intel_notification

    stats = {"detected": 0, "sent": 0, "skipped_dedup": 0, "errors": 0}

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT e.id, e.summary, e.start_datetime, e.end_datetime,
                       e.contact_id, e.status, e.location,
                       c.nome AS contact_nome, c.empresa AS contact_empresa,
                       EXTRACT(EPOCH FROM (NOW() - e.end_datetime))/60 AS min_atras,
                       EXTRACT(EPOCH FROM (e.end_datetime - e.start_datetime))/60 AS duracao_min
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                WHERE e.end_datetime BETWEEN NOW() - INTERVAL '60 minutes' AND NOW() - INTERVAL '15 minutes'
                  AND e.contact_id IS NOT NULL
                  AND COALESCE(e.status, 'confirmed') = 'confirmed'
                  AND EXTRACT(EPOCH FROM (e.end_datetime - e.start_datetime))/60 >= 15
                ORDER BY e.end_datetime DESC
                """
            )
            events = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"check_post_meeting query err: {e}")
        stats["errors"] += 1
        return stats

    stats["detected"] = len(events)

    for ev in events:
        event_id = ev["id"]
        if _already_sent("post_meeting", str(event_id)):
            stats["skipped_dedup"] += 1
            continue

        # Monta mensagem
        try:
            inicio = ev["start_datetime"]
            hora_str = inicio.strftime("%H:%M") if hasattr(inicio, "strftime") else str(inicio)[:5]
            duracao = int(ev.get("duracao_min") or 0)
            min_atras = int(ev.get("min_atras") or 0)

            nome = ev.get("contact_nome") or "contato"
            empresa = ev.get("contact_empresa")
            contato_line = f"{nome}" + (f" — {empresa}" if empresa else "")

            summary = ev.get("summary") or "Reuniao"

            msg = (
                f"🎯 Reuniao terminou {min_atras}min atras:\n"
                f"*{summary}* as {hora_str} ({duracao}min)\n"
                f"{contato_line}\n\n"
                f"Como foi? Posso:\n"
                f"• Salvar memoria da conversa\n"
                f"• Criar tarefa de follow-up\n"
                f"• Atualizar o contato\n\n"
                f"So me conta o que rolou que eu organizo."
            )

            ok = await send_intel_notification(msg)
            if ok:
                _record_sent(
                    "post_meeting",
                    str(event_id),
                    {
                        "summary": summary,
                        "contact_id": ev.get("contact_id"),
                        "contact_nome": nome,
                        "duracao_min": duracao,
                    },
                )
                stats["sent"] += 1
            else:
                stats["errors"] += 1
        except Exception as e:
            logger.warning(f"check_post_meeting send err event={event_id}: {e}")
            stats["errors"] += 1

    return stats


async def run_all_checks() -> Dict:
    """Roda todos os checks proativos. Endpoint /api/cron/proactive-check
    chama isso. Triggers futuros (decay, grupo WA pico) entram aqui.
    """
    results = {}
    try:
        results["post_meeting"] = await check_post_meeting()
    except Exception as e:
        logger.exception("check_post_meeting failed")
        results["post_meeting"] = {"error": str(e)}
    return results
