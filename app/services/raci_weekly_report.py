"""
RACI Weekly Report — Sends RACI status to WhatsApp groups.

For each ConselhoOS empresa with:
1. Pending RACI items
2. A linked WhatsApp group in INTEL

Generates a formatted status report and sends to the group.
Also captures responses to update RACI item status.
"""

import os
import json
import logging
import re
from datetime import datetime, date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CONSELHOOS_DATABASE_URL = os.getenv("CONSELHOOS_DATABASE_URL", "")


def generate_raci_report(empresa_id: str) -> Optional[Dict]:
    """Generate RACI status report for an empresa."""
    import psycopg2
    import psycopg2.extras

    if not CONSELHOOS_DATABASE_URL:
        logger.error("CONSELHOOS_DATABASE_URL not configured")
        return None

    try:
        conn = psycopg2.connect(CONSELHOOS_DATABASE_URL)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Get empresa info
        cur.execute("SELECT nome FROM empresas WHERE id = %s", (empresa_id,))
        empresa = cur.fetchone()
        if not empresa:
            conn.close()
            return None

        # Get RACI items — busca todos e ordena em Python pra usar buckets
        # de prioridade (urgente/atrasada-com-movimento/no-prazo/concluida).
        cur.execute("""
            SELECT id, area, acao, prazo, status, updated_at,
                   responsavel_r, responsavel_a, responsavel_c, responsavel_i
            FROM raci_itens
            WHERE empresa_id = %s
        """, (empresa_id,))
        raw_items = cur.fetchall()
        conn.close()

        if not raw_items:
            return None

        hoje = date.today()
        now = datetime.now()
        urgentes = []          # bucket 0 — vencido + status='pendente' (ninguem arregacou)
        atrasadas_mov = []     # bucket 1 — vencido + status='em_andamento' (comecou, falta entregar)
        no_prazo = []          # bucket 2 — prazo futuro, qualquer status nao-concluido
        concluidas = []        # bucket 3

        for item in raw_items:
            prazo_raw = item['prazo']
            prazo_date = prazo_raw if isinstance(prazo_raw, date) else None
            if prazo_date and isinstance(prazo_date, datetime):
                prazo_date = prazo_date.date()
            updated_at = item.get('updated_at')

            entry = {
                'id': item['id'],
                'area': item['area'],
                'acao': item['acao'],
                'prazo': prazo_date.strftime('%d/%m') if prazo_date else '—',
                'prazo_date': prazo_date,
                'responsavel': item['responsavel_r'] or '?',
                'status': item['status'],
                'updated_at': updated_at,
            }

            status = item['status']
            if status == 'concluido':
                concluidas.append(entry)
                continue

            is_vencido = bool(prazo_date and prazo_date < hoje)

            # Criterio status-driven (escolhido 11/05): semantica clara,
            # nao depende de heuristica de nota/updated_at. Status muda quando
            # alguem progride — "pendente vencido" = ninguem comecou.
            if is_vencido and status in ('pendente', 'atrasado'):
                urgentes.append(entry)
            elif is_vencido and status == 'em_andamento':
                atrasadas_mov.append(entry)
            else:
                no_prazo.append(entry)

        # Dentro de cada bucket: mais atrasado/antigo primeiro
        for bucket in (urgentes, atrasadas_mov, no_prazo):
            bucket.sort(key=lambda e: e['prazo_date'] or date.max)
        # Concluidas: mais recente primeiro (limita 5)
        concluidas.sort(key=lambda e: e.get('updated_at') or now, reverse=True)
        concluidas = concluidas[:5]

        return {
            'empresa_nome': empresa['nome'],
            'empresa_id': empresa_id,
            'urgentes': urgentes,
            'atrasadas_mov': atrasadas_mov,
            'no_prazo': no_prazo,
            'concluidas': concluidas,
            # Retrocompatibilidade pra qualquer caller antigo:
            'atrasados': urgentes + atrasadas_mov,
            'pendentes': [e for e in no_prazo if e['status'] == 'pendente'],
            'em_andamento': [e for e in no_prazo if e['status'] == 'em_andamento'],
            'concluidos': concluidas,
            'total': len(raw_items),
        }

    except Exception as e:
        logger.error(f"Error generating RACI report: {e}")
        return None


def format_raci_whatsapp(report: Dict) -> str:
    """Format RACI report for WhatsApp message.

    Formato priority-grouped (alinhado com numeracao do report):
      🚨 Urgentes (atrasada + sem update há +1 semana)
      ⚠️ Atrasadas com movimento (alguem mexeu na semana)
      🔄 No prazo (em andamento / pendente)
      ✅ Concluidas
    """
    hoje = date.today().strftime('%d/%m/%Y')
    lines = [
        f"📋 *RACI Semanal — {report['empresa_nome']}*",
        f"_{hoje}_",
        "",
    ]
    n = 0  # contador continuo pra resposta tipo "3 concluido"

    if report.get('urgentes'):
        lines.append(f"🚨 *Urgentes — atrasadas e sem update há +1 semana ({len(report['urgentes'])}):*")
        for item in report['urgentes']:
            n += 1
            resp = _short_name(item['responsavel'])
            lines.append(f"{n}. {item['acao'][:80]} — *{resp}* (prazo: {item['prazo']})")
        lines.append("")

    if report.get('atrasadas_mov'):
        lines.append(f"⚠️ *Atrasadas — preciso de update ({len(report['atrasadas_mov'])}):*")
        for item in report['atrasadas_mov']:
            n += 1
            resp = _short_name(item['responsavel'])
            lines.append(f"{n}. {item['acao'][:80]} — *{resp}* (prazo: {item['prazo']})")
        lines.append("")

    if report.get('no_prazo'):
        lines.append(f"🔄 *No prazo ({len(report['no_prazo'])}):*")
        for item in report['no_prazo']:
            n += 1
            resp = _short_name(item['responsavel'])
            lines.append(f"{n}. {item['acao'][:80]} — *{resp}* ({item['prazo']})")
        lines.append("")

    if report.get('concluidas'):
        lines.append(f"✅ *Concluídas ({len(report['concluidas'])}):*")
        for item in report['concluidas']:
            n += 1
            resp = _short_name(item['responsavel'])
            lines.append(f"{n}. {item['acao'][:80]} — *{resp}* ✓")
        lines.append("")

    lines.append(f"_Total: {report['total']} | Responda com o nº + status (ex: \"3 concluído\")_")

    return "\n".join(lines)


def _short_name(name: str) -> str:
    """Shorten 'Renato de Faria e Almeida Prado' to 'Renato A.'"""
    parts = name.strip().split()
    if len(parts) <= 2:
        return name
    # First name + last initial
    return f"{parts[0]} {parts[-1][0]}."


async def send_raci_to_groups() -> Dict:
    """Send RACI reports to all empresa WhatsApp groups."""
    from database import get_db
    from integrations.evolution_api import get_evolution_client

    results = {"sent": 0, "skipped": 0, "errors": 0}

    if not CONSELHOOS_DATABASE_URL:
        return {"error": "CONSELHOOS_DATABASE_URL not configured"}

    # Get all empresas from ConselhoOS
    import psycopg2
    import psycopg2.extras
    try:
        conn = psycopg2.connect(CONSELHOOS_DATABASE_URL)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id, nome FROM empresas")
        empresas = cur.fetchall()
        conn.close()
    except Exception as e:
        return {"error": str(e)}

    # For each empresa, find linked WA group in INTEL
    with get_db() as conn:
        cursor = conn.cursor()

        for empresa in empresas:
            # Find INTEL project for this empresa
            cursor.execute("""
                SELECT p.id FROM projects p
                WHERE LOWER(p.nome) LIKE LOWER(%s)
                   OR p.nome ILIKE %s
                LIMIT 1
            """, (f"%{empresa['nome']}%", f"%{empresa['nome']}%"))
            project = cursor.fetchone()

            if not project:
                results["skipped"] += 1
                continue

            # Find WA group
            cursor.execute("""
                SELECT group_jid, group_name FROM project_whatsapp_groups
                WHERE project_id = %s AND ativo = TRUE
                LIMIT 1
            """, (project['id'],))
            group = cursor.fetchone()

            if not group:
                results["skipped"] += 1
                continue

            # Generate report
            report = generate_raci_report(empresa['id'])
            if not report or (not report['atrasados'] and not report['pendentes'] and not report['em_andamento']):
                results["skipped"] += 1
                continue

            # Format and send
            message = format_raci_whatsapp(report)
            try:
                client = get_evolution_client()
                await client.send_text(
                    group['group_jid'],
                    message,
                    instance_name="rap-whatsapp"
                )
                results["sent"] += 1
                logger.info(f"RACI report sent to {group['group_name']} for {empresa['nome']}")
            except Exception as e:
                logger.error(f"Error sending RACI to group: {e}")
                results["errors"] += 1

    return results


def parse_raci_update(message: str, empresa_id: str) -> Optional[Dict]:
    """Parse a WhatsApp message that updates a RACI item status.

    Formats recognized:
    - "3 concluído"
    - "item 5 em andamento"
    - "5 em andamento: detalhes aqui"
    - "#3 feito"
    """
    import psycopg2
    import psycopg2.extras

    # Match patterns like "3 concluído", "item 5 em andamento: details"
    patterns = [
        r'(?:item\s*)?#?(\d+)\s+(conclu[ií]do|feito|pronto|done|completo)',
        r'(?:item\s*)?#?(\d+)\s+(em andamento|iniciado|trabalhando|in progress)(?:\s*[:\-]\s*(.+))?',
        r'(?:item\s*)?#?(\d+)\s+(cancelado|removido|n[aã]o aplic[aá]vel)',
    ]

    for pattern in patterns:
        m = re.search(pattern, message.lower().strip())
        if m:
            item_num = int(m.group(1))
            status_text = m.group(2)
            notes = m.group(3) if m.lastindex >= 3 else None

            # Map to status
            if any(w in status_text for w in ['conclu', 'feito', 'pronto', 'done', 'completo']):
                new_status = 'concluido'
            elif any(w in status_text for w in ['andamento', 'iniciado', 'trabalhando', 'progress']):
                new_status = 'em_andamento'
            elif any(w in status_text for w in ['cancelado', 'removido']):
                new_status = 'cancelado'
            else:
                continue

            # Get the nth RACI item for this empresa
            if not CONSELHOOS_DATABASE_URL:
                return None

            try:
                conn = psycopg2.connect(CONSELHOOS_DATABASE_URL)
                cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

                # Get items na MESMA ordem do report (priority-grouped). Reusa
                # generate_raci_report pra garantir alinhamento entre o que o user
                # ve no WhatsApp e o item que vai ser atualizado.
                conn.close()
                report = generate_raci_report(empresa_id)
                if not report:
                    return None
                ordered = (
                    report.get('urgentes', []) +
                    report.get('atrasadas_mov', []) +
                    report.get('no_prazo', []) +
                    report.get('concluidas', [])
                )

                if item_num < 1 or item_num > len(ordered):
                    return None

                target = ordered[item_num - 1]
                # Reabre conexao pra UPDATE
                conn = psycopg2.connect(CONSELHOOS_DATABASE_URL)
                cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

                # Update status
                update_fields = {"status": new_status, "updated_at": datetime.now()}
                if notes:
                    cur.execute(
                        "UPDATE raci_itens SET status = %s, notas = COALESCE(notas, '') || %s, updated_at = NOW() WHERE id = %s",
                        (new_status, f"\n[{datetime.now().strftime('%d/%m')}] {notes}", target['id'])
                    )
                else:
                    cur.execute(
                        "UPDATE raci_itens SET status = %s, updated_at = NOW() WHERE id = %s",
                        (new_status, target['id'])
                    )
                conn.commit()
                conn.close()

                # Audit log (P3): RACI status mudou por regex em msg WA — quero rastro.
                try:
                    from services.agent_actions import log_action
                    log_action(
                        action_type='raci_status_updated',
                        category='conselho',
                        title=f"RACI: '{(target['acao'] or '')[:60]}' → {new_status}",
                        scope_ref={'raci_item_id': str(target['id']), 'empresa_id': str(empresa_id)},
                        source='raci_weekly_report.parse_raci_update',
                        payload={'old_status': target['status'], 'new_status': new_status, 'item_num': item_num, 'notes': notes},
                        undo_hint=f"UPDATE raci_itens SET status='{target['status']}' WHERE id='{target['id']}'::uuid;",
                    )
                except Exception as e:
                    logger.warning(f"audit log failed for raci_update: {e}")

                return {
                    'item_id': target['id'],
                    'acao': target['acao'],
                    'old_status': target['status'],
                    'new_status': new_status,
                    'notes': notes,
                }

            except Exception as e:
                logger.error(f"Error updating RACI from message: {e}")
                return None

    return None
