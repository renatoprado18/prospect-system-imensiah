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


def _identify_contacts_for_event(event: Dict) -> List[Dict]:
    """Identifica contatos vinculados a um calendar_event via 2 caminhos:

    1. event.contact_id direto (caso ja vinculado pela UI/bot)
    2. Match dos attendees emails contra contacts.emails (jsonb)

    Filtra emails self=True (Renato proprio). Dedup por contact_id.

    Returns: lista de {id, nome, empresa} ordenada por circulo (C1 primeiro).
    """
    import json
    matches: Dict[int, Dict] = {}

    # Caminho 1: contact_id direto
    if event.get("contact_id") and event.get("contact_nome"):
        matches[event["contact_id"]] = {
            "id": event["contact_id"],
            "nome": event["contact_nome"],
            "empresa": event.get("contact_empresa"),
            "circulo": event.get("contact_circulo"),
            "_source": "contact_id",
        }

    # Caminho 2: attendees emails -> contacts.emails
    attendees_raw = event.get("attendees") or []
    if isinstance(attendees_raw, str):
        try:
            attendees_raw = json.loads(attendees_raw)
        except Exception:
            attendees_raw = []
    if not isinstance(attendees_raw, list):
        attendees_raw = []

    # Coleta emails (excl. self e organizers que sao o proprio Renato)
    emails_to_match = []
    for att in attendees_raw:
        if not isinstance(att, dict):
            continue
        if att.get("self") is True:
            continue
        email = att.get("email")
        if email and "@" in str(email):
            emails_to_match.append(str(email).lower().strip())

    if emails_to_match:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                # Match via @> em jsonb: contacts.emails contem
                # {"email": "x"} (case insensitive via lower)
                # Usar EXISTS subquery + jsonb_array_elements pra suportar
                # tanto formato [{"email": "x"}] quanto ["x"] quanto string
                placeholders = ",".join(["%s"] * len(emails_to_match))
                cur.execute(
                    f"""
                    SELECT DISTINCT c.id, c.nome, c.empresa, COALESCE(c.circulo, 5) AS circulo
                    FROM contacts c,
                         jsonb_array_elements(COALESCE(c.emails, '[]'::jsonb)) AS ce
                    WHERE LOWER(
                        CASE
                          WHEN jsonb_typeof(ce) = 'object' THEN ce->>'email'
                          WHEN jsonb_typeof(ce) = 'string' THEN ce#>>'{{}}'
                          ELSE NULL
                        END
                    ) IN ({placeholders})
                    """,
                    emails_to_match,
                )
                for row in cur.fetchall():
                    cid = row["id"]
                    if cid in matches:
                        continue  # ja veio do contact_id
                    matches[cid] = {
                        "id": cid,
                        "nome": row["nome"],
                        "empresa": row["empresa"],
                        "circulo": row["circulo"],
                        "_source": "attendees",
                    }
        except Exception as e:
            logger.warning(f"_identify_contacts attendees match err: {e}")

    # Ordena por circulo (C1 primeiro), depois por nome
    result = list(matches.values())
    result.sort(key=lambda c: (c.get("circulo") or 5, c["nome"]))
    return result


async def check_post_meeting() -> Dict:
    """Trigger #335: detecta reunioes terminadas nos ultimos 15-180min e
    pergunta ao Renato 'como foi?'.

    Filtros:
    - end_datetime entre NOW()-180min e NOW()-15min (janela ampla pra cobrir
      reunioes que duraram mais ou crons que rodaram tarde — antes era 60min
      mas eventos como Lobo/Gasparino 07/05 ficaram fora porque ja tinham
      passado da janela quando user contou ao bot)
    - duracao >= 15min
    - status = 'confirmed'
    - >= 1 contato identificado: ou via calendar_events.contact_id (caminho
      antigo) OU via match dos attendees emails contra contacts.emails
      (caminho novo — eventos importados do Google Calendar nao tem contact_id
      preenchido, gap real do MVP)

    Mensagem WA proativa lista TODOS os contatos identificados, nao so um.
    """
    from services.intel_bot import send_intel_notification

    stats = {"detected": 0, "sent": 0, "skipped_dedup": 0, "errors": 0, "skipped_no_contact": 0}

    try:
        with get_db() as conn:
            cur = conn.cursor()
            # Eventos elegiveis (sem filtro de contato — matching feito via attendees)
            cur.execute(
                """
                SELECT e.id, e.summary, e.start_datetime, e.end_datetime,
                       e.contact_id, e.status, e.location, e.attendees,
                       c.nome AS contact_nome, c.empresa AS contact_empresa,
                       c.circulo AS contact_circulo,
                       EXTRACT(EPOCH FROM (NOW() - e.end_datetime))/60 AS min_atras,
                       EXTRACT(EPOCH FROM (e.end_datetime - e.start_datetime))/60 AS duracao_min
                FROM calendar_events e
                LEFT JOIN contacts c ON c.id = e.contact_id
                WHERE e.end_datetime BETWEEN NOW() - INTERVAL '180 minutes' AND NOW() - INTERVAL '15 minutes'
                  AND COALESCE(e.status, 'confirmed') = 'confirmed'
                  AND EXTRACT(EPOCH FROM (e.end_datetime - e.start_datetime))/60 >= 15
                  AND (e.contact_id IS NOT NULL OR jsonb_array_length(COALESCE(e.attendees, '[]'::jsonb)) > 0)
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

        # Identifica contatos: pelo contact_id direto + via attendees emails
        identified = _identify_contacts_for_event(ev)
        if not identified:
            # Sem contato vinculado nem match em attendees -> nao gera signal
            stats["skipped_no_contact"] += 1
            continue

        # Monta mensagem
        try:
            inicio = ev["start_datetime"]
            hora_str = inicio.strftime("%H:%M") if hasattr(inicio, "strftime") else str(inicio)[:5]
            duracao = int(ev.get("duracao_min") or 0)
            min_atras = int(ev.get("min_atras") or 0)
            summary = ev.get("summary") or "Reuniao"

            # Lista contatos identificados (max 4 pra nao poluir)
            contatos_lines = []
            for c in identified[:4]:
                line = c["nome"]
                if c.get("empresa"):
                    line += f" — {c['empresa']}"
                contatos_lines.append(line)
            if len(identified) > 4:
                contatos_lines.append(f"+ {len(identified) - 4} outros")

            contatos_block = "\n".join(f"• {l}" for l in contatos_lines)

            # Politica feedback_notifications: so notificar quando precisa
            # acao manual. "Como foi?" e convite reflexivo, baixo valor.
            # Renato ja recebe pre-meeting + Fathom dispara import async com
            # action items. Skip se nao houver contato C0/C1 (escalation)
            # entre os identified.
            has_c0_c1 = any(
                (c.get("circulo") or 99) in (1, 2)
                for c in identified
            )
            if not has_c0_c1:
                # Sem escalation: skip WA, deixa Fathom callback fazer o resumo.
                logger.info(f"post-meeting: skip notify (sem C0/C1 nos {len(identified)} contatos)")
                continue

            msg = (
                f"🎯 Reuniao com C1 terminou {min_atras}min atras:\n"
                f"*{summary}* as {hora_str} ({duracao}min)\n"
                f"{contatos_block}\n\n"
                f"Action items via Fathom em ~10min."
            )

            ok = await send_intel_notification(msg)
            if ok:
                _record_sent(
                    "post_meeting",
                    str(event_id),
                    {
                        "summary": summary,
                        "contact_ids": [c["id"] for c in identified],
                        "contact_names": [c["nome"] for c in identified],
                        "duracao_min": duracao,
                        "match_source": "attendees" if not ev.get("contact_id") else "contact_id",
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
