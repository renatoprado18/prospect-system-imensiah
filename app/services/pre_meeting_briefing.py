"""
Pre-Meeting Briefing Service

Sends WhatsApp briefing to Renato 1h before each meeting.
Includes: participants, last interactions, pending tasks, relevant facts.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def check_upcoming_meetings() -> Dict:
    """
    Check for meetings starting in the next hour.
    Generate and send briefing for each.
    """
    results = {"checked": 0, "briefings_sent": 0, "errors": []}

    with get_db() as conn:
        cursor = conn.cursor()

        now = datetime.now()
        one_hour = now + timedelta(hours=1)

        # Find meetings starting in the next 45-75 minutes (window to avoid duplicates)
        cursor.execute("""
            SELECT ce.id, ce.summary, ce.start_datetime, ce.end_datetime,
                   ce.contact_id, ce.attendees, ce.location, ce.description,
                   c.nome as contact_nome, c.empresa as contact_empresa,
                   c.cargo as contact_cargo, c.circulo as contact_circulo
            FROM calendar_events ce
            LEFT JOIN contacts c ON c.id = ce.contact_id
            WHERE ce.start_datetime BETWEEN %s AND %s
              AND ce.status != 'cancelled'
            ORDER BY ce.start_datetime
        """, (now + timedelta(minutes=45), now + timedelta(minutes=75)))

        meetings = [dict(r) for r in cursor.fetchall()]
        results["checked"] = len(meetings)

        if not meetings:
            return results

        # Filter out trivial events
        skip_patterns = ['gym', 'treino', 'judô', 'judo', 'tênis', 'tennis',
                         'almoço', 'almoco', 'café', 'cafe', 'banho', 'cama',
                         'dormir', 'pessoal', 'flex', 'deslocamento']

        for meeting in meetings:
            summary = (meeting['summary'] or '').lower()
            if any(p in summary for p in skip_patterns):
                continue

            # Check if briefing already sent (avoid duplicates)
            cursor.execute("""
                SELECT id FROM contact_interactions
                WHERE titulo LIKE %s AND data_interacao::date = CURRENT_DATE
                LIMIT 1
            """, (f"Briefing: {meeting['summary'][:50]}%",))
            if cursor.fetchone():
                continue

            try:
                briefing = await _generate_meeting_briefing(meeting, cursor)
                if briefing:
                    from services.intel_bot import send_intel_notification
                    await send_intel_notification(briefing)
                    results["briefings_sent"] += 1

                    # Mark as sent
                    cursor.execute("""
                        INSERT INTO contact_interactions (contact_id, tipo, titulo, descricao, data_interacao)
                        VALUES (%s, 'briefing', %s, %s, NOW())
                    """, (meeting.get('contact_id'), f"Briefing: {meeting['summary'][:80]}", briefing[:500]))
                    conn.commit()
            except Exception as e:
                results["errors"].append(f"{meeting['summary']}: {e}")
                logger.error(f"Briefing error for {meeting['summary']}: {e}")

    return results


async def _generate_meeting_briefing(meeting: Dict, cursor) -> Optional[str]:
    """Generate a concise WhatsApp briefing for a meeting."""

    summary = meeting['summary'] or 'Reunião'
    start = meeting['start_datetime']
    start_str = start.strftime('%H:%M') if start else '?'

    # 1. Find related project
    project_info = ""
    cursor.execute("""
        SELECT p.id, p.nome FROM project_events pe
        JOIN projects p ON p.id = pe.project_id
        WHERE pe.calendar_event_id = %s LIMIT 1
    """, (meeting['id'],))
    project = cursor.fetchone()

    if not project:
        # Try matching by event name
        cursor.execute("""
            SELECT id, nome FROM projects WHERE status = 'ativo'
              AND (LOWER(nome) LIKE LOWER(%s) OR LOWER(%s) LIKE '%%' || LOWER(SPLIT_PART(nome, ' ', 1)) || '%%')
            LIMIT 1
        """, (f"%{summary.split(' ')[0]}%", summary))
        project = cursor.fetchone()

    if project:
        project = dict(project)
        # Get overdue tasks
        cursor.execute("""
            SELECT t.titulo, t.data_vencimento, c.nome as responsavel
            FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id
            WHERE t.project_id = %s AND t.status = 'pending' AND t.data_vencimento < NOW()
            ORDER BY t.data_vencimento LIMIT 5
        """, (project['id'],))
        overdue = [dict(r) for r in cursor.fetchall()]

        if overdue:
            tasks_text = "\n".join([f"  ⚠️ {t['titulo']} ({t.get('responsavel','?')})" for t in overdue])
            project_info = f"\n\n*Tarefas vencidas ({len(overdue)}):*\n{tasks_text}"

    # 2. Get contact info and last interactions
    contact_info = ""
    if meeting.get('contact_id'):
        cid = meeting['contact_id']
        cursor.execute("""
            SELECT m.conteudo, m.direcao, m.enviado_em
            FROM messages m JOIN conversations cv ON cv.id = m.conversation_id
            WHERE cv.contact_id = %s AND m.conteudo IS NOT NULL
            ORDER BY m.enviado_em DESC LIMIT 3
        """, (cid,))
        last_msgs = [dict(r) for r in cursor.fetchall()]

        # Get recent memories
        cursor.execute("""
            SELECT titulo, resumo FROM contact_memories
            WHERE contact_id = %s ORDER BY data_ocorrencia DESC NULLS LAST LIMIT 2
        """, (cid,))
        memories = [dict(r) for r in cursor.fetchall()]

        if last_msgs or memories:
            parts = []
            if memories:
                parts.append("*Memórias:*")
                for m in memories:
                    parts.append(f"  📌 {m['titulo']}")
            if last_msgs:
                parts.append("*Última conversa:*")
                msg = last_msgs[0]
                direction = "Você" if msg['direcao'] == 'outgoing' else meeting.get('contact_nome', '?')
                date_str = msg['enviado_em'].strftime('%d/%m') if msg.get('enviado_em') else '?'
                parts.append(f"  💬 {direction} ({date_str}): {(msg['conteudo'] or '')[:100]}")
            contact_info = "\n" + "\n".join(parts)

    # 3. Get attendees info
    attendees_text = ""
    attendees = meeting.get('attendees') or []
    if isinstance(attendees, str):
        try:
            attendees = json.loads(attendees)
        except Exception:
            attendees = []

    if attendees:
        names = []
        for a in attendees[:5]:
            email = a.get('email', '')
            # Try to find contact by email
            cursor.execute("SELECT nome, empresa FROM contacts WHERE emails::text ILIKE %s LIMIT 1", (f"%{email}%",))
            contact = cursor.fetchone()
            if contact:
                names.append(f"{contact['nome']} ({contact.get('empresa','')})")
            else:
                name = a.get('displayName') or email.split('@')[0]
                names.append(name)
        if names:
            attendees_text = f"\n*Participantes:* {', '.join(names)}"

    # 4. Build briefing
    location = f"\n📍 {meeting['location']}" if meeting.get('location') else ""

    briefing = (
        f"📋 *Reunião em 1h: {summary}*\n"
        f"🕐 {start_str}{location}"
        f"{attendees_text}"
        f"{contact_info}"
        f"{project_info}"
    )

    # 5. If we have enough context, add AI suggestion
    if project_info or contact_info:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            try:
                prompt = f"""Baseado no contexto abaixo, sugira 1 frase de ação prioritária para esta reunião.

{briefing}

Responda APENAS com a sugestão, máximo 1 linha. Português. Comece com emoji."""

                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 100,
                              "messages": [{"role": "user", "content": prompt}]}
                    )
                if resp.status_code == 200:
                    suggestion = resp.json()["content"][0]["text"].strip()
                    briefing += f"\n\n💡 *Foco:* {suggestion}"
            except Exception:
                pass

    return briefing
