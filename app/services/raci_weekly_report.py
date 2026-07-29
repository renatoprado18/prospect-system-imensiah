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
import textwrap
from datetime import datetime, date
from typing import Dict, List, Optional

from services.tz import now_utc, to_brt


def _clip(s: Optional[str], width: int = 120) -> str:
    """Quebra em palavra com reticencias, evita cortar frases no meio."""
    s = (s or '').strip()
    if not s:
        return ''
    return textwrap.shorten(s, width=width, placeholder='…')

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
        # Filtro de concluidos: so mostra os ainda nao relatados em report
        # anterior (concluido_relatado_em IS NULL). Apos enviar, chamar
        # mark_concluidos_as_reported(empresa_id) pra marcar.
        cur.execute("""
            SELECT id, area, acao, prazo, status, updated_at, notas,
                   responsavel_r, responsavel_a, responsavel_c, responsavel_i,
                   concluido_relatado_em
            FROM raci_itens
            WHERE empresa_id = %s
              AND (status != 'concluido' OR concluido_relatado_em IS NULL)
        """, (empresa_id,))
        raw_items = cur.fetchall()
        conn.close()

        if not raw_items:
            return None

        hoje = date.today()
        now = datetime.now()
        urgentes = []          # bucket 0 — vencido + pendente + SEM update recente
        atrasadas_mov = []     # bucket 1 — vencido + (em_andamento OU pendente-com-update-recente)
        no_prazo = []          # bucket 2 — prazo futuro, qualquer status nao-concluido
        concluidas = []        # bucket 3
        recent_updates = []    # ⚡ updates dos ultimos 7 dias pra header

        # 08/06/26: cooldown de 72h em update_at — se mexeram recentemente,
        # nao e mais "ninguem arregacou", e sim "tem movimento mas falta entregar"
        update_cooldown_hours = 72

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
                'notas': item.get('notas') or '',
            }

            # A: coletar updates recentes (ultimas 7 dias) pra header
            if updated_at:
                hours_since = (now - updated_at).total_seconds() / 3600
                if hours_since <= 7 * 24:
                    # Pega ultima linha de notas (formato "[DD/MM] texto\n[DD/MM] texto")
                    last_note = ''
                    for ln in (entry['notas'] or '').splitlines()[::-1]:
                        ln = ln.strip()
                        if ln:
                            last_note = ln
                            break
                    if last_note:
                        recent_updates.append({
                            'acao': item['acao'],
                            'responsavel': item['responsavel_r'] or '?',
                            'last_note': last_note,
                            'new_status': item['status'],
                            'updated_at': updated_at,
                        })

            status = item['status']
            if status == 'concluido':
                concluidas.append(entry)
                continue

            is_vencido = bool(prazo_date and prazo_date < hoje)

            # C (08/06/26): cooldown de 72h. Se vencido+pendente mas teve update
            # recente, vai pra atrasadas_mov (tem movimento), nao urgentes.
            has_recent_update = bool(
                updated_at and (now - updated_at).total_seconds() / 3600 <= update_cooldown_hours
            )

            if is_vencido and status in ('pendente', 'atrasado'):
                if has_recent_update:
                    atrasadas_mov.append(entry)
                else:
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

        # Recent updates: mais recente primeiro (limita 8 pra nao inflar mensagem)
        recent_updates.sort(key=lambda e: e['updated_at'], reverse=True)
        recent_updates = recent_updates[:8]

        return {
            'empresa_nome': empresa['nome'],
            'empresa_id': empresa_id,
            'urgentes': urgentes,
            'atrasadas_mov': atrasadas_mov,
            'no_prazo': no_prazo,
            'concluidas': concluidas,
            'recent_updates': recent_updates,
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


def format_raci_whatsapp(report: Dict, interactive: bool = True) -> str:
    """Format RACI report for WhatsApp message.

    Formato priority-grouped (alinhado com numeracao do report):
      🚨 Urgentes (atrasada + sem update há +1 semana)
      ⚠️ Atrasadas com movimento (alguem mexeu na semana)
      🔄 No prazo (em andamento / pendente)
      ✅ Concluidas

    interactive=True (default, ConselhoOS): rodape convida resposta "nº + status"
    (captada por parse_raci_update). interactive=False (Jabô): sem loop de
    resposta — governanca familiar so recebe o preview.
    """
    hoje = date.today().strftime('%d/%m/%Y')
    lines = [
        f"📋 *RACI Semanal — {report['empresa_nome']}*",
        f"_{hoje}_",
        "",
    ]

    # A (08/06/26): seção de atualizações recentes (capturadas das msgs do grupo
    # via smart_updates) no topo. Da contexto antes de chegar nos atrasados.
    if report.get('recent_updates'):
        lines.append(f"📝 *Atualizações desta semana ({len(report['recent_updates'])}):*")
        for u in report['recent_updates']:
            resp = _short_name(u['responsavel'])
            acao = _clip(u['acao'], 100)
            note = u['last_note']
            # Strip prefixo de data se vier "[DD/MM] texto"
            note = re.sub(r'^\[\d{1,2}/\d{1,2}\]\s*', '', note or '').strip()
            note = _clip(note, 180)
            status_emoji = {'concluido': '✅', 'em_andamento': '🔄', 'pendente': '⏳', 'atrasado': '⚠️'}.get(u['new_status'], '')
            lines.append(f"• {status_emoji} _{acao}_ — *{resp}*")
            lines.append(f"   {note}")
        lines.append("")

    n = 0  # contador continuo pra resposta tipo "3 concluido"

    if report.get('urgentes'):
        lines.append(f"🚨 *Urgentes — atrasadas e sem update há +1 semana ({len(report['urgentes'])}):*")
        for item in report['urgentes']:
            n += 1
            resp = _short_name(item['responsavel'])
            lines.append(f"{n}. {_clip(item['acao'])} — *{resp}* (prazo: {item['prazo']})")
        lines.append("")

    if report.get('atrasadas_mov'):
        lines.append(f"⚠️ *Atrasadas — preciso de update ({len(report['atrasadas_mov'])}):*")
        for item in report['atrasadas_mov']:
            n += 1
            resp = _short_name(item['responsavel'])
            lines.append(f"{n}. {_clip(item['acao'])} — *{resp}* (prazo: {item['prazo']})")
        lines.append("")

    if report.get('no_prazo'):
        lines.append(f"🔄 *No prazo ({len(report['no_prazo'])}):*")
        for item in report['no_prazo']:
            n += 1
            resp = _short_name(item['responsavel'])
            lines.append(f"{n}. {_clip(item['acao'])} — *{resp}* ({item['prazo']})")
        lines.append("")

    if report.get('concluidas'):
        lines.append(f"✅ *Concluídas ({len(report['concluidas'])}):*")
        for item in report['concluidas']:
            n += 1
            resp = _short_name(item['responsavel'])
            lines.append(f"{n}. {_clip(item['acao'])} — *{resp}* ✓")
        lines.append("")

    if interactive:
        lines.append(f"_Total: {report['total']} | Responda com o nº + status (ex: \"3 concluído\")_")
    else:
        lines.append(f"_Total: {report['total']}_")

    return "\n".join(lines)


def _short_name(name: str) -> str:
    """Shorten 'Renato de Faria e Almeida Prado' to 'Renato A.'"""
    parts = name.strip().split()
    if len(parts) <= 2:
        return name
    # First name + last initial
    return f"{parts[0]} {parts[-1][0]}."


def _iso_week() -> str:
    """Semana ISO em BRT ('2026-W30') — granularidade do dedup do preview.

    O preview e SEMANAL (cron sabado 18h). Duas runs na mesma semana sao a
    mesma coisa; o dedup por semana evita que um re-disparo manual interrompa
    de novo. Em BRT porque a semana do Renato e a de Brasilia — sabado 18h BRT
    e domingo 21h UTC, e o dedup por semana UTC ja teria virado.
    """
    y, w, _ = to_brt(now_utc()).isocalendar()
    return f"{y}-W{w:02d}"


async def _send_preview(title: str, body: str, dedup: str, topic: Optional[str] = None) -> bool:
    """Despacha um preview RACI pelo notification_router. True se ENTREGOU.

    Antes ia por send_intel_notification direto, fora do channel_decisions —
    logo fora do teto diario de interrupcao. Nao da pra usar o helper notify()
    aqui porque ele devolve `action == 'sent'`, e um preview REBAIXADO pra push
    devolveria False; o caller conta isso como erro. O que importa pro contador
    e se foi ENTREGUE por algum canal, entao lemos a action: so 'skipped' e
    falha de verdade ('duplicate' tambem nao e erro — e o dedup funcionando).
    """
    from services.notification_router import route_to_renato

    r = await route_to_renato(
        source="raci_weekly_preview",
        payload={"title": title, "body": body},
        msg_type="raci_weekly_preview",
        urgency_score=8,
        dedup_key=dedup,
        message_text=body,
        topic_key=topic,
    )
    return r.get("action") != "skipped"


def _preview_conselhos_enabled() -> bool:
    """Preview semanal dos CONSELHOS por WhatsApp — desligado em 29/07/2026.

    Decisão do Renato: "agora que temos o RACI dentro do projeto, com botão de
    disparo, não faz sentido continuar mandando o preview. Simplesmente deve ser
    lembrado toda 2ª feira de mandar o RACI quando abrir a sessão CoS."

    O preview existia porque não havia superfície: a única forma de ver o RACI
    era ele chegar pronto no WhatsApp. Desde 28-29/07 a página
    `/projetos/{id}/raci` mostra a matriz das duas fontes, deixa editar na fonte
    e tem botão "Enviar no grupo" com preview editável — o preview semanal virou
    a segunda cópia do mesmo texto, e a pior das duas: chegava **truncado**
    (5.243 / 5.866 / 4.669 chars contra o corte silencioso de 4.096 da
    Evolution, em 13, 20 e 27/07). O lembrete agora mora na abertura da `/cos`
    de segunda.

    Kill-switch: `RACI_WEEKLY_PREVIEW_CONSELHOS=on` volta o comportamento antigo
    sem deploy. Não vale pro Jabô — ver `send_raci_to_groups`.
    """
    return (os.getenv("RACI_WEEKLY_PREVIEW_CONSELHOS") or "").strip().lower() in (
        "1", "on", "true", "yes")


async def send_raci_to_groups() -> Dict:
    """Preview semanal do RACI. Hoje sobra só a Governança Jabô.

    Os conselhos (ConselhoOS) saíram em 29/07 — ver `_preview_conselhos_enabled`.
    O **Jabô continua** porque não tem substituto: o RACI dele são as *tasks* do
    projeto #28, e a página `/projetos/28/raci` lê `raci_itens` (INTEL +
    ConselhoOS), onde ele tem **zero** linhas. Desligar os dois deixaria a
    governança da fazenda sem superfície nenhuma.

    Mantem o nome da funcao pra nao quebrar callers (cron_raci_weekly_report).
    """
    from database import get_db

    results = {"previews_sent": 0, "skipped": 0, "errors": 0, "empresas": [],
               "conselhos_preview": "on" if _preview_conselhos_enabled() else "off"}

    if not CONSELHOOS_DATABASE_URL:
        return {"error": "CONSELHOOS_DATABASE_URL not configured"}

    empresas = []
    if _preview_conselhos_enabled():
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

    # Pra cada empresa, valida que tem grupo WA linkado e gera preview
    with get_db() as conn:
        cursor = conn.cursor()

        for empresa in empresas:
            # Find INTEL project for this empresa (so pra validacao — mensagem
            # vai ser entregue ao Renato, nao ao grupo, mas se nao houver grupo
            # linkado o report nao tem destino final).
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
            if not report:
                results["skipped"] += 1
                continue

            empty = not (report.get('urgentes') or report.get('atrasadas_mov')
                         or report.get('no_prazo') or report.get('concluidas'))
            if empty:
                results["skipped"] += 1
                continue

            # Wrap com header de preview
            message = format_raci_whatsapp(report)
            preview = (
                f"📝 *PREVIEW RACI — {empresa['nome']}*\n"
                f"_Destino: {group['group_name']}_\n"
                f"_Revise, edite se quiser, e cole no grupo._\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{message}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"_Fim do preview. Acima esta o texto pronto pra copiar._"
            )

            try:
                ok = await _send_preview(
                    f"PREVIEW RACI — {empresa['nome']}",
                    preview,
                    f"raci_preview:{empresa['id']}:{_iso_week()}",
                    topic=empresa["nome"],
                )
                if ok:
                    results["previews_sent"] += 1
                    results["empresas"].append(empresa['nome'])
                    logger.info(f"RACI preview sent to Renato for {empresa['nome']}")
                else:
                    results["errors"] += 1
            except Exception as e:
                logger.error(f"Error sending RACI preview: {e}")
                results["errors"] += 1

    # --- Governança Jabô (nativo INTEL, fora do ConselhoOS) ---
    # A governanca da fazenda nao e empresa do ConselhoOS; o RACI de facto sao
    # as tasks do projeto #28. Gera o mesmo preview a partir delas. build_jabo_
    # preview abre a propria conexao (fora do with acima).
    try:
        jabo_preview = build_jabo_preview()
        if jabo_preview:
            ok = await _send_preview(
                "PREVIEW RACI — Governança Jabô",
                jabo_preview,
                f"raci_preview:jabo:{_iso_week()}",
                topic="Governança Jabô",
            )
            if ok:
                results["previews_sent"] += 1
                results["empresas"].append("Governança Jabô")
                logger.info("RACI preview sent to Renato for Governança Jabô")
            else:
                results["errors"] += 1
        else:
            results["skipped"] += 1
    except Exception as e:
        logger.error(f"Error sending Jabô RACI preview: {e}")
        results["errors"] += 1

    return results


def mark_concluidos_as_reported(empresa_id: str) -> int:
    """Marca todos os concluidos atualmente nao-relatados como ja relatados.
    Chamar depois que o Renato confirma envio do report ao grupo, pra que
    no proximo report eles sumam da secao Concluidas. Retorna count.

    Pattern: 1 vez na lista (no report seguinte ao informe de conclusao),
    depois desaparece.
    """
    import psycopg2
    if not CONSELHOOS_DATABASE_URL:
        return 0
    try:
        conn = psycopg2.connect(CONSELHOOS_DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            UPDATE raci_itens SET concluido_relatado_em = NOW()
            WHERE empresa_id = %s
              AND status = 'concluido'
              AND concluido_relatado_em IS NULL
        """, (empresa_id,))
        n = cur.rowcount
        conn.commit()
        conn.close()
        return n
    except Exception as e:
        logger.error(f"mark_concluidos_as_reported error: {e}")
        return 0


JABO_PROJECT_ID = 28


def _infer_task_responsavel(titulo: str) -> str:
    """Infere o responsavel do prefixo do titulo da task Jabô.

    "[Jabô/Andressa] Enviar..." -> "Andressa"
    "[Jabô] Classificar..."      -> "—" (tag de projeto, sem pessoa)
    "Investigar Fiama..."        -> "—"
    """
    m = re.match(r'^\s*\[([^\]]+)\]', titulo or '')
    if m and '/' in m.group(1):
        resp = m.group(1).split('/', 1)[1].strip()
        if resp:
            return resp
    return '—'


def _strip_task_prefix(titulo: str) -> str:
    """Remove o prefixo [..] do titulo pra nao duplicar com a coluna responsavel."""
    stripped = re.sub(r'^\s*\[[^\]]+\]\s*', '', titulo or '').strip()
    return stripped or (titulo or '')


def generate_jabo_report(cursor) -> Optional[Dict]:
    """RACI semanal da Governança Jabô a partir das tasks do projeto #28 (INTEL).

    A governanca da fazenda NAO vive no ConselhoOS — as tasks do #28 sao o
    "RACI" de facto (mantidas no fluxo normal do Renato/CoS). Espelha os buckets
    de generate_raci_report (urgente / atrasada-com-movimento / no-prazo /
    concluida) usando os campos de task. Statuses INTEL:
    pending/completed/cancelled/on_hold (nao ha 'em_andamento'). on_hold e
    cancelled ficam de fora (fora do radar). Responsavel inferido do titulo.
    Retorna o mesmo shape de dict que format_raci_whatsapp consome.

    `cursor` = RealDictCursor do INTEL (get_db()).
    """
    cursor.execute("""
        SELECT id, titulo, status, prioridade, data_vencimento,
               data_conclusao, atualizado_em
        FROM tasks
        WHERE project_id = %s
          AND status NOT IN ('cancelled', 'on_hold')
    """, (JABO_PROJECT_ID,))
    rows = cursor.fetchall()
    if not rows:
        return None

    hoje = date.today()
    now = datetime.now()
    update_cooldown_hours = 72     # espelha generate_raci_report
    completed_window_days = 7      # so mostra concluidas recentes

    urgentes, atrasadas_mov, no_prazo, concluidas = [], [], [], []

    for t in rows:
        prazo_raw = t['data_vencimento']
        if isinstance(prazo_raw, datetime):
            prazo_date = prazo_raw.date()
        elif isinstance(prazo_raw, date):
            prazo_date = prazo_raw
        else:
            prazo_date = None
        updated_at = t.get('atualizado_em')

        entry = {
            'id': t['id'],
            'area': '',
            'acao': _strip_task_prefix(t['titulo']),
            'prazo': prazo_date.strftime('%d/%m') if prazo_date else '—',
            'prazo_date': prazo_date,
            'responsavel': _infer_task_responsavel(t['titulo']),
            'status': t['status'],
            'updated_at': updated_at,
            'notas': '',
        }

        if t['status'] == 'completed':
            done_at = t.get('data_conclusao') or updated_at
            if done_at and (now - done_at).total_seconds() / 3600 <= completed_window_days * 24:
                concluidas.append(entry)
            continue

        # pending
        is_vencido = bool(prazo_date and prazo_date < hoje)
        has_recent_update = bool(
            updated_at and (now - updated_at).total_seconds() / 3600 <= update_cooldown_hours
        )
        if is_vencido:
            (atrasadas_mov if has_recent_update else urgentes).append(entry)
        else:
            no_prazo.append(entry)

    for bucket in (urgentes, atrasadas_mov, no_prazo):
        bucket.sort(key=lambda e: e['prazo_date'] or date.max)
    concluidas.sort(key=lambda e: e.get('updated_at') or now, reverse=True)
    concluidas = concluidas[:5]

    total = len(urgentes) + len(atrasadas_mov) + len(no_prazo) + len(concluidas)
    if total == 0:
        return None

    return {
        'empresa_nome': 'Governança Jabô',
        'empresa_id': None,
        'urgentes': urgentes,
        'atrasadas_mov': atrasadas_mov,
        'no_prazo': no_prazo,
        'concluidas': concluidas,
        'recent_updates': [],   # tasks nao tem o formato de notas [DD/MM]; header off
        'atrasados': urgentes + atrasadas_mov,
        'pendentes': list(no_prazo),
        'em_andamento': [],
        'concluidos': concluidas,
        'total': total,
    }


def build_jabo_preview() -> Optional[str]:
    """Monta o preview do RACI Jabô pronto pro Renato revisar e postar no grupo.

    Espelha o wrapper de preview do send_raci_to_groups (ConselhoOS), mas a
    fonte e o INTEL (tasks #28) e nao ha loop de resposta (interactive=False).
    Abre a propria conexao — chamado DEPOIS do bloco with do ConselhoOS.
    """
    from database import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        report = generate_jabo_report(cursor)
        if not report:
            return None
        cursor.execute("""
            SELECT group_name FROM project_whatsapp_groups
            WHERE project_id = %s AND ativo = TRUE
            LIMIT 1
        """, (JABO_PROJECT_ID,))
        g = cursor.fetchone()
        destino = (g['group_name'] if g else None) or 'Governança Jabô'

    message = format_raci_whatsapp(report, interactive=False)
    return (
        f"📝 *PREVIEW RACI — Governança Jabô*\n"
        f"_Destino: {destino}_\n"
        f"_Revise, edite se quiser, e cole no grupo._\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{message}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Fim do preview. Acima esta o texto pronto pra copiar._"
    )


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
                # `cancelado` NAO existe no enum `raci_status` do ConselhoOS
                # (pendente | em_andamento | concluido | atrasado). O UPDATE
                # estouraria com InvalidTextRepresentation — nunca estourou
                # porque este regex jamais casou em prod (0 registros de
                # `parse_raci_update` em agent_actions, medido 29/07). Cancelar
                # item de ata nao tem representacao aqui: devolve o motivo em
                # vez de gravar um valor que o banco recusa.
                logger.info("RACI: 'cancelado' pedido por WA — sem equivalente no enum")
                return {"blocked": "status_inexistente", "pedido": "cancelado",
                        "motivo": "cancelar item nao existe no RACI — "
                                  "edite no ConselhoOS ou marque concluido"}
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

                # Mesma trava do apply da IA (29/07): resposta no grupo nao
                # anda pra tras. Aqui pesa ainda mais que la, porque a
                # numeracao vem de uma lista RE-GERADA no momento da resposta —
                # se algo mudou desde o envio do relatorio, o "5" do Renato
                # pode estar apontando pra outro item. Bloquear o retrocesso
                # limita o estrago desse desalinhamento a um no-op.
                from services.raci_smart_updates import is_downgrade
                if is_downgrade(target['status'], new_status):
                    logger.warning(
                        "RACI regex: bloqueado %s -> %s no item #%s ('%s')",
                        target['status'], new_status, item_num,
                        (target['acao'] or '')[:50])
                    return {"blocked": "downgrade", "item_id": str(target['id']),
                            "acao": target['acao'], "old_status": target['status'],
                            "new_status": new_status,
                            "motivo": f"item ja esta '{target['status']}' — "
                                      f"reabrir so pelo INTEL ou ConselhoOS"}

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
