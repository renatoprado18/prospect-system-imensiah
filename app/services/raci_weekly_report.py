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

# Teto de tamanho do caminho REGEX (fix 23/08). Comando de status e' curto
# ("3 concluido", "5 em andamento: detalhe"); relatorio de RACI tem milhares de
# caracteres. A msg da Kelly que virou comando em 21/08 tinha 1.867. Generoso de
# proposito: o pattern 2 captura `notes` livres, e apertar demais empurraria uso
# legitimo pro fallback da IA sem necessidade.
RACI_REGEX_MAX_CHARS = 400


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
    a segunda cópia do mesmo texto. O lembrete agora mora na abertura da `/cos`
    de segunda.

    ⚠️ CORREÇÃO 29/07: ao desligar isto eu afirmei que o preview "chegava
    truncado" (5.243 / 5.866 / 4.669 chars contra um suposto corte de 4.096 da
    Evolution). **Não chegava.** Medido depois contra a própria API dela, que
    devolve o entregue: o preview de 4.669 chars está inteiro, com o rodapé
    "_Fim do preview_" no fim. A Evolution não corta em 4.096 — isso era
    convenção do nosso código tratada como fato. O desligamento continua certo
    pelo motivo que basta (é cópia redundante da página, e foi decisão do
    Renato); a truncagem era argumento meu, e caiu.

    Kill-switch: `RACI_WEEKLY_PREVIEW_CONSELHOS=on` volta o comportamento antigo
    sem deploy. Não vale pro Jabô — ver `send_raci_to_groups`.
    """
    return (os.getenv("RACI_WEEKLY_PREVIEW_CONSELHOS") or "").strip().lower() in (
        "1", "on", "true", "yes")


async def send_raci_to_groups() -> Dict:
    """Preview semanal do RACI. Hoje sobra só a Governança Jabô.

    Os conselhos (ConselhoOS) saíram em 29/07 — ver `_preview_conselhos_enabled`.
    O **Jabô continua** porque é a superfície que avisa quando o RACI travou (foi
    ela que expôs o silêncio de 03 a 16/08).

    ⚠️ ESTE DOCSTRING DIZIA, ATÉ 17/08, que "o RACI do Jabô são as tasks do #28 e
    a página `/projetos/28/raci` tem zero linhas em `raci_itens`". Era verdade em
    29/07 e envelheceu calado: hoje o #28 tem **10 itens curados** em
    `raci_itens`, são eles que a página mostra, é deles que sai o texto enviado ao
    grupo (conferido nos RACIs de 29/07 e 03/08, ambos no formato do
    `raci_matrix`), e é neles que o auto-update escreve. A afirmação velha é o que
    manteve o preview lendo `tasks` por três semanas — ver `build_jabo_preview`.

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
    # A governanca da fazenda nao e empresa do ConselhoOS: o RACI dela sao os 10
    # itens de `raci_itens` do #28, e o preview sai da MESMA funcao que o botao de
    # envio usa. build_jabo_preview abre a propria conexao (fora do with acima).
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


def _infer_task_responsavel(titulo: str, contato_nome: Optional[str] = None) -> str:
    """Responsavel da task Jabô: o VÍNCULO primeiro, o prefixo do titulo depois.

    "[Jabô/Andressa] Enviar..." -> "Andressa"
    "[Jabô] Classificar..."      -> vínculo, ou "—" (tag de projeto, sem pessoa)
    "Investigar Fiama..."        -> vínculo, ou "—"

    ATÉ 16/08/2026 ISTO SÓ LIA O PREFIXO — e o prefixo existe em pouquíssimas
    tasks, então o RACI saía com "—" em quase toda linha. `tasks.contact_id`
    estava preenchido em 36 das 61 do #28 (a Andressa é dona de 3 pendentes) e
    ninguém lia: campo cheio sem consumidor, o mesmo padrão que este backlog vem
    catalogando ([[feedback_consumidor_morto_wiring]]).

    Por que isso importa mais que estética: o RACI vai para um grupo onde a
    outra pessoa procura o próprio nome. Uma matriz em que o trabalho dela
    aparece como "—" não a reconhece — e reconhecimento é a função da peça, não
    um efeito colateral dela.

    O prefixo VENCE o vínculo quando existe: ele é declaração explícita de
    quem escreveu a task, e `contact_id` às vezes marca o interlocutor do
    assunto e não o dono do trabalho.

    ⚠️ E POR ISSO O VÍNCULO SOZINHO NÃO BASTA. Medido em 16/08: "Convidar
    Rodrigo a visitar a fazenda" e "Avaliar visita a Lisboa" têm `contact_id` do
    Rodrigo — que é o ASSUNTO, não o executor; quem convida é o Renato. Atribuir
    por vínculo cru mandaria ao grupo uma matriz dando tarefa a quem não a tem.
    Só conta o vínculo de quem o PROJETO declara como executor (`papel` do
    `project_members`) — `contato_executor` já chega filtrado por isso. Numa peça
    que vai para terceiros, "—" honesto vale mais que um nome errado.
    """
    m = re.match(r'^\s*\[([^\]]+)\]', titulo or '')
    if m and '/' in m.group(1):
        resp = m.group(1).split('/', 1)[1].strip()
        if resp:
            return resp
    if contato_nome:
        return contato_nome.split()[0]     # primeiro nome, como no resto da peça
    return '—'


# Papel de `project_members` que caracteriza QUEM EXECUTA. "R:" é a notação RACI
# escrita à mão nos papéis do #28 ("Executora operacional (R: prospecção...)");
# "execut" pega "Gerente da fazenda — executa classificação". Contraparte,
# prospect, decisor e cadeia regional ficam de fora de propósito: participam da
# frente sem serem donos de linha.
_PAPEL_EXECUTOR = re.compile(r'\bR:|execut', re.IGNORECASE)


def _strip_task_prefix(titulo: str) -> str:
    """Remove o prefixo [..] do titulo pra nao duplicar com a coluna responsavel."""
    stripped = re.sub(r'^\s*\[[^\]]+\]\s*', '', titulo or '').strip()
    return stripped or (titulo or '')


def generate_jabo_report(cursor) -> Optional[Dict]:
    """As tasks do projeto #28 (INTEL) nos buckets do RACI.

    ⚠️ NÃO É A PEÇA QUE VAI AO GRUPO, e chamar isto de "o RACI do Jabô" foi o
    erro que `build_jabo_preview` conserta em 17/08: o RACI que a Andressa recebe
    sai de `raci_itens` via `raci_matrix`. Estas tasks são o backlog do Renato no
    projeto — trabalho real, mas outro conjunto (em 17/08, zero itens em comum
    com os 10 do RACI). Serve ao `_bloco_backlog_tasks`, como contexto.

    Espelha os buckets de generate_raci_report (urgente / atrasada-com-movimento
    / no-prazo / concluida) usando os campos de task. Statuses INTEL:
    pending/completed/cancelled/on_hold (nao ha 'em_andamento'). on_hold e
    cancelled ficam de fora (fora do radar). Responsavel inferido do titulo.
    Retorna o mesmo shape de dict que format_raci_whatsapp consome.

    `cursor` = RealDictCursor do INTEL (get_db()).
    """
    # `public.` explícito e JOIN no contato: as views `copilot.*` traduzem nomes
    # de coluna, e sem o schema a errada parece existir ([[feedback_copilot_view_verify_consumer]]).
    cursor.execute("""
        SELECT t.id, t.titulo, t.status, t.prioridade, t.data_vencimento,
               t.data_conclusao, t.atualizado_em,
               c.nome AS contato_nome, pm.papel AS contato_papel
        FROM public.tasks t
        LEFT JOIN public.contacts c ON c.id = t.contact_id
        LEFT JOIN public.project_members pm
               ON pm.contact_id = t.contact_id AND pm.project_id = t.project_id
        WHERE t.project_id = %s
          AND t.status NOT IN ('cancelled', 'on_hold')
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
            'responsavel': _infer_task_responsavel(
                t['titulo'],
                t.get('contato_nome') if _PAPEL_EXECUTOR.search(t.get('contato_papel') or '')
                else None),
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


def _intel_base_url() -> str:
    """Domínio canônico do INTEL, com env PRÓPRIA — de propósito não herda
    `BASE_URL`: em 16/08/2026 ela valia `prospects.almeida-prado.com`, o domínio
    antigo. Os dois respondem 200 hoje, então o link não quebraria; o problema é
    que um preview semanal ensina o destinatário para onde olhar, e apontar pro
    host velho o mantém vivo por inércia. Link quebrado num preview é pior que
    link ausente — treina a pessoa a ignorar o preview inteiro."""
    return os.getenv("INTEL_PUBLIC_URL", "https://intel.almeida-prado.com").rstrip("/")


def jabo_reportes_pendentes(cursor, group_jid: Optional[str] = None) -> Dict:
    """O que o outro lado reportou no grupo DESDE o último RACI que saiu.

    POR QUE ISTO EXISTE (16/08/2026, pedido do Renato: "a Andressa tem enviado
    diversas atualizações e não estamos fazendo o RACI semanal; não quero
    desmotivá-la"). O preview chegava a ele toda segunda e não dizia a única
    coisa que decide se ele para tudo e dispara: **quantas vezes a outra pessoa
    escreveu sem receber nada de volta**. Medido naquele dia: RACI parado desde
    03/08, e a Andressa reportando em 04, 05, 11 e 12/08 — cinco toques, zero
    retorno. O preview de 10/08 chegou e não foi disparado; nada nele informava
    esse silêncio acumulado.

    Isto NÃO entra no texto que vai ao grupo — seria devolver a ela um resumo do
    que ela mesma escreveu. É contexto para quem decide disparar.
    """
    if group_jid is None:
        cursor.execute("""
            SELECT group_jid FROM project_whatsapp_groups
            WHERE project_id = %s AND ativo = TRUE LIMIT 1
        """, (JABO_PROJECT_ID,))
        row = cursor.fetchone()
        if not row:
            return {"desde": None, "dias": None, "reportes": []}
        group_jid = row["group_jid"]

    # Âncora = último RACI QUE SAIU no grupo, não o último preview gerado. É a
    # diferença entre "a máquina produziu" e "a pessoa recebeu" — e era
    # exatamente essa confusão que fazia o placar do cron parecer saudável
    # ([[feedback_medir_o_consumidor_certo]]).
    cursor.execute("""
        SELECT MAX(timestamp) AS ts FROM group_messages
        WHERE group_jid = %s AND from_me = TRUE AND content ILIKE %s
    """, (group_jid, '%RACI%'))
    r = cursor.fetchone()
    desde = r["ts"] if r else None

    cursor.execute("""
        SELECT timestamp, sender_name, content FROM group_messages
        WHERE group_jid = %s AND from_me = FALSE
          AND (%s::timestamp IS NULL OR timestamp > %s)
        ORDER BY timestamp
    """, (group_jid, desde, desde))
    reportes = [dict(x) for x in cursor.fetchall()]

    # Idade calculada no banco: `group_messages.timestamp` é UTC e o processo
    # roda em BRT — subtrair um do outro erra em 3h e pode virar um dia inteiro
    # na fronteira ([[feedback_hora_do_banco_e_utc]]).
    dias = None
    if desde:
        cursor.execute(
            "SELECT EXTRACT(DAY FROM (now() AT TIME ZONE 'UTC') - %s)::int AS d", (desde,))
        dias = max(0, (cursor.fetchone() or {}).get("d") or 0)
    return {"desde": desde, "dias": dias, "reportes": reportes}


def _bloco_reportes(ctx: Dict) -> str:
    """Cabeçalho do preview: o silêncio acumulado, em uma olhada."""
    reportes, dias = ctx["reportes"], ctx["dias"]
    if not reportes:
        if dias is not None and dias > 10:
            return (f"⚠️ *Último RACI foi há {dias} dias* — e ninguém escreveu no "
                    f"grupo desde então.\n\n")
        return ""

    quem = {}
    for r in reportes:
        nome = (r.get("sender_name") or "alguém").split()[0]
        quem[nome] = quem.get(nome, 0) + 1
    resumo = " · ".join(f"*{n}* {c}×" for n, c in
                        sorted(quem.items(), key=lambda kv: -kv[1]))

    linhas = [f"🔔 *{len(reportes)} atualizações no grupo sem retorno* — {resumo}"]
    if dias is not None:
        linhas.append(f"_Último RACI enviado há *{dias} dias*._")
    linhas.append("")
    # As 3 mais recentes, na íntegra curta: quem decide disparar precisa ver o
    # TEOR, não só a contagem. Contagem sozinha vira número que se ignora.
    #
    # A amostra pula mensagens muito curtas ("Olá! Bom dia!"), que ocupariam a
    # vaga de uma atualização real — mas a CONTAGEM acima inclui todas: são
    # toques sem retorno do mesmo jeito. Corte por COMPRIMENTO, nunca por
    # palavra-chave: filtro de vocabulário erra calado e descarta justamente o
    # que foi escrito fora do padrão esperado ([[feedback_filtro_vocabulario_errado_falha_calado]]).
    substantivas = [r for r in reportes
                    if len(" ".join((r.get("content") or "").split())) >= 25]
    for r in (substantivas or reportes)[-3:]:
        quando = r["timestamp"].strftime("%d/%m") if r.get("timestamp") else "—"
        nome = (r.get("sender_name") or "—").split()[0]
        txt = " ".join((r.get("content") or "").split())[:150]
        linhas.append(f"• _{quando}_ *{nome}*: {txt}")
    # Duas quebras, não uma: os blocos do preview são concatenados direto, e com
    # `join` sozinho o próximo cola na última bullet. Passou despercebido enquanto
    # o vizinho era só o "👉 Enviar"; com um bloco de conteúdo embaixo, os dois
    # viram um parágrafo só e a peça deixa de ser varrível de olho.
    return "\n".join(linhas) + "\n\n"


def _bloco_backlog_tasks(report: Optional[Dict]) -> str:
    """As tasks do #28 que pedem ação — rotuladas como o que NÃO vai ao grupo.

    Até 17/08 estas tasks ERAM o corpo do preview, e é por isso que o bloco
    existe em vez de a fonte simplesmente ser trocada: elas são trabalho real do
    Renato no Jabô (14 linhas hoje), e tirá-las sem substituto removeria de um
    preview semanal informação que ele vinha recebendo — o tipo de "conserto"
    que a pessoa descobre pela ausência.

    Só o acionável entra (vencidas + com movimento recente), e contado, não
    listado inteiro: o preview já carrega veredito, silêncio acumulado e a peça
    do grupo. Repetir 14 linhas de backlog aqui competiria com o que ele precisa
    decidir em trinta segundos.
    """
    if not report:
        return ""
    urgentes = report.get('urgentes') or []
    atrasadas = report.get('atrasadas_mov') or []
    if not (urgentes or atrasadas):
        return ""

    linhas = [f"📌 *Seu backlog do #28 — {len(urgentes) + len(atrasadas)} em atraso* "
              f"_(não vai ao grupo)_"]
    for e in (urgentes + atrasadas)[:3]:
        linhas.append(f"• {_clip(e['acao'], 70)} — *{e['responsavel']}* ({e['prazo']})")
    resto = len(urgentes) + len(atrasadas) - 3
    if resto > 0:
        linhas.append(f"_+{resto} em {_intel_base_url()}/projetos/{JABO_PROJECT_ID}_")
    return "\n".join(linhas) + "\n\n"


def build_jabo_preview() -> Optional[str]:
    """Monta o preview do RACI Jabô pronto pro Renato revisar e postar no grupo.

    ⚠️ O TEXTO PRONTO PRA COPIAR VEM DA MESMA FONTE QUE O BOTÃO ENVIA, e essa é
    a razão de ser desta função hoje. Até 17/08 ele vinha de `generate_jabo_report`
    (as *tasks* do #28) enquanto o "Enviar com 1 clique" — a página
    `/projetos/28/raci` — monta o texto com `raci_matrix.get_matrix`, sobre os 10
    itens curados de `raci_itens`. Medido naquele dia: **os dois conjuntos não
    tinham UM item em comum** (14 tasks operacionais × 10 frentes de exportação).
    O Renato revisava um texto e disparava outro, e o gate humano validava uma
    peça que nunca chegava a ninguém.

    Isso vinha piorando por desenho: o auto-update do grupo (`jabo_group_raci`,
    no ar desde 16/08) escreve em `raci_itens` — quanto melhor ele funcionasse,
    mais o que a Andressa recebia se afastava do que ele tinha aprovado. O
    primeiro caso já está no banco: o item "Envio de amostras" saiu de `pendente`
    (RACI de 03/08) para `em_andamento`, e nenhum preview mostrou isso.

    É a mesma correção do merge de contato em 16/08 (`5b2a7c7`): quem prepara e
    quem executa passam pela MESMA função, senão a divergência é questão de
    tempo. As tasks continuam no preview, no `_bloco_backlog_tasks` e nomeadas
    como o que não vai ao grupo.

    Abre a propria conexao — chamado DEPOIS do bloco with do ConselhoOS.
    """
    from database import get_db
    from services.raci_matrix import format_for_whatsapp, get_matrix

    # A peça que vai ao grupo. `get_matrix` abre a própria conexão e ainda une o
    # ConselhoOS na leitura — é exatamente o que a página faz.
    matrix = get_matrix(JABO_PROJECT_ID)
    if matrix.get("error"):
        logger.warning("build_jabo_preview: matriz do #%s indisponível: %s",
                       JABO_PROJECT_ID, matrix["error"])
        return None
    message = format_for_whatsapp(matrix)
    if not (matrix.get("itens") or []):
        return None

    with get_db() as conn:
        cursor = conn.cursor()
        # As tasks agora são CONTEXTO, não a peça — e o preview não morre se elas
        # sumirem: quem manda em existir preview é o RACI, acima.
        report = generate_jabo_report(cursor)
        cursor.execute("""
            SELECT group_name FROM project_whatsapp_groups
            WHERE project_id = %s AND ativo = TRUE
            LIMIT 1
        """, (JABO_PROJECT_ID,))
        g = cursor.fetchone()
        destino = (g['group_name'] if g else None) or 'Governança Jabô'
        # Mesma conexão: o bloco de reportes é do wrapper, não do relatório.
        try:
            ctx = jabo_reportes_pendentes(cursor)
        except Exception as e:                                  # noqa: BLE001
            # Preview sem o bloco vale mais que preview nenhum — mas a falha TEM
            # que aparecer, senão o silêncio some junto com o medidor dele.
            logger.warning("build_jabo_preview: reportes pendentes falharam: %s", e)
            ctx = {"desde": None, "dias": None, "reportes": []}

    # O follow-up da medição do auto-update viaja aqui, na superfície que ele já
    # lê toda segunda — e só quando a decisão passa a ser possível. Prometer
    # "olhe o placar em 30 dias" seria passar trabalho a ele, que é justamente o
    # que a ferramenta existe para não fazer.
    try:
        from services.jabo_group_raci import bloco_veredito_para_preview
        veredito = bloco_veredito_para_preview()
    except Exception as e:                                      # noqa: BLE001
        logger.warning("build_jabo_preview: veredito indisponível: %s", e)
        veredito = ""

    disparo = f"{_intel_base_url()}/projetos/{JABO_PROJECT_ID}/raci"
    return (
        f"📝 *PREVIEW RACI — Governança Jabô*\n"
        f"_Destino: {destino}_\n\n"
        f"{veredito}"
        f"{_bloco_reportes(ctx)}"
        f"{_bloco_backlog_tasks(report)}"
        f"👉 Enviar com 1 clique: {disparo}\n"
        f"_(ou copie o texto abaixo e cole no grupo — é o mesmo texto)_\n"
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

    NAO reconhece resumo de relatorio ("✅ 6 concluídos.", "12 itens, 6
    concluidos"). Ver as tres travas abaixo — o defeito de 21/08.
    """
    import psycopg2
    import psycopg2.extras

    # ── TRAVA 1: comando e' mensagem CURTA (fix 23/08) ───────────────────────
    # Todo relatorio de RACI termina com um resumo numerico, entao todo
    # relatorio e' um comando em potencial. Em 21/08 o rodape "✅ 6 concluídos."
    # da Kelly casou e marcou o 6o item da lista posicional ("Zerar o passivo da
    # Alba") como concluido — sendo que a propria mensagem o dava como em
    # andamento, previsao 30/10. O bot confirmou no grupo 7s depois.
    # Rejeitar aqui nao perde a mensagem: ela cai no fallback da IA, que PROPOE
    # em vez de aplicar. Este caminho escreve sem revisao, entao a duvida tem que
    # cair pro lado de nao escrever.
    if len(message.strip()) > RACI_REGEX_MAX_CHARS:
        logger.info("RACI regex: %d chars > %d — nao e comando, deixa pro fallback IA",
                    len(message.strip()), RACI_REGEX_MAX_CHARS)
        return None

    # Match patterns like "3 concluído", "item 5 em andamento: details"
    #
    # ── TRAVA 2: ancorado no INICIO DA LINHA (^ com MULTILINE) ───────────────
    # O rodape tem "✅ " antes do numero, entao nao e' inicio de linha. Custo:
    # "ok, 3 concluido" deixa de casar e vira proposta da IA — degradacao segura.
    #
    # ── TRAVA 3: o PLURAL nao e' comando ─────────────────────────────────────
    # "concluídos"/"feitos"/"prontos" contam itens, nao mandam fechar um. O
    # lookahead (?![a-zà-ÿ]) exige fim de palavra de verdade — \b casaria o "s".
    patterns = [
        r'^\s*(?:item\s*)?#?(\d+)\s+(conclu[ií]do|feito|pronto|done|completo)(?![a-zà-ÿ])',
        r'^\s*(?:item\s*)?#?(\d+)\s+(em andamento|iniciado|trabalhando|in progress)(?![a-zà-ÿ])(?:\s*[:\-]\s*(.+))?',
        r'^\s*(?:item\s*)?#?(\d+)\s+(cancelado|removido|n[aã]o aplic[aá]vel)(?![a-zà-ÿ])',
    ]

    for pattern in patterns:
        m = re.search(pattern, message.lower().strip(), re.MULTILINE)
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
