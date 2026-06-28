"""
Nível A — Análise passiva da operação semanal.

Gera relatório markdown com 6 seções:
  1. Comunicação (WA inbound, response time, grupos, email triage)
  2. Compromissos (tasks, RACI ConselhoOS)
  3. Calendário (eventos, distribuição por tipo)
  4. Tonha / Patrol Agent (action_proposals)
  5. Lacunas detectáveis (contatos phantom, threads sem resposta, follow-ups prometidos)
  6. Resumo executivo

Rodar standalone:
    python scripts/weekly_ops_report.py [--days 7] [--out /tmp/report.md]

Convenção TZ: INTEL armazena UTC naive — converter pra BRT no SQL.
Exceção: calendar_events já em BRT (memory feedback_calendar_events_tz.md).
"""

import os
import sys
import argparse
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv("/Users/rap/prospect-system/.env")

DATABASE_URL = os.getenv("DATABASE_URL")
CONSELHOOS_DATABASE_URL = os.getenv("CONSELHOOS_DATABASE_URL")


def section(title):
    return f"\n## {title}\n"


def safe(label, fn, *args, **kwargs):
    """Roda um bloco com try/except, marca ⚠️ se falhar."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return f"⚠️ {label}: erro — {type(e).__name__}: {e}"


def fmt_hours(seconds):
    if seconds is None:
        return "—"
    if seconds < 3600:
        return f"{int(seconds/60)}min"
    h = seconds / 3600
    return f"{h:.1f}h"


def fmt_pct(n, total):
    if not total:
        return "—"
    return f"{100*n/total:.0f}%"


# ──────────────────────────────────────────────────────────────────────
# 1. COMUNICAÇÃO
# ──────────────────────────────────────────────────────────────────────

def section_comunicacao(cur, days):
    md = section("1. Comunicação")

    # 1a. WA inbound DMs — volume + tempo de resposta
    # Filtra contatos com silence_default=true (broadcasts, automação, marketing)
    cur.execute(f"""
        WITH inbound AS (
          SELECT m.id, m.contact_id, m.criado_em
          FROM messages m
          LEFT JOIN contacts c ON c.id = m.contact_id
          WHERE m.direcao = 'incoming'
            AND m.criado_em >= NOW() - INTERVAL '{days} days'
            AND m.criado_em < NOW()
            AND COALESCE(c.silence_default, false) = false
        ),
        with_response AS (
          SELECT
            i.id,
            i.contact_id,
            i.criado_em AS inbound_at,
            (SELECT MIN(m2.criado_em)
             FROM messages m2
             WHERE m2.contact_id = i.contact_id
               AND m2.direcao = 'outgoing'
               AND m2.criado_em > i.criado_em) AS first_response_at
          FROM inbound i
        )
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE first_response_at IS NOT NULL) AS respondido,
          COUNT(*) FILTER (WHERE first_response_at IS NULL) AS sem_resposta,
          COUNT(*) FILTER (WHERE first_response_at IS NULL
                            AND inbound_at < NOW() - INTERVAL '24 hours') AS evaporado_24h,
          AVG(EXTRACT(EPOCH FROM (first_response_at - inbound_at))) AS avg_resp_sec,
          PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (first_response_at - inbound_at))
          ) AS p50_resp_sec
        FROM with_response;
    """)
    r = cur.fetchone()
    md += f"""
### 1.1  WhatsApp inbound (DMs)

- **Recebidas**: {r['total']}
- **Respondidas**: {r['respondido']} ({fmt_pct(r['respondido'], r['total'])})
- **Sem resposta**: {r['sem_resposta']} ({fmt_pct(r['sem_resposta'], r['total'])})
  - Destas, **{r['evaporado_24h']}** estão há +24h sem resposta
- **Tempo médio até 1ª resposta**: {fmt_hours(r['avg_resp_sec'])}
- **Mediana**: {fmt_hours(r['p50_resp_sec'])}
"""

    # 1b. Top 5 contatos que escreveram mais (exclui silence_default)
    cur.execute(f"""
        SELECT c.nome,
               COUNT(*) AS msgs,
               MAX(m.criado_em AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo') AS ultima
        FROM messages m
        LEFT JOIN contacts c ON c.id = m.contact_id
        WHERE m.direcao = 'incoming'
          AND m.criado_em >= NOW() - INTERVAL '{days} days'
          AND COALESCE(c.silence_default, false) = false
        GROUP BY c.nome
        ORDER BY msgs DESC
        LIMIT 5;
    """)
    md += "\n**Top 5 emissores (DMs):**\n\n"
    md += "| Contato | Msgs | Última |\n|---|---:|---|\n"
    for row in cur.fetchall():
        nome = row['nome'] or "(sem nome)"
        ultima = row['ultima'].strftime('%d/%m %H:%M') if row['ultima'] else "—"
        md += f"| {nome} | {row['msgs']} | {ultima} |\n"

    # 1c. Grupos WA — volume por grupo
    cur.execute(f"""
        SELECT
          group_jid,
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE from_me = false) AS recebidas,
          COUNT(*) FILTER (WHERE from_me = true) AS enviadas
        FROM group_messages
        WHERE timestamp >= NOW() - INTERVAL '{days} days'
        GROUP BY group_jid
        ORDER BY total DESC
        LIMIT 10;
    """)
    md += "\n### 1.2  Grupos WhatsApp\n\n"
    md += "| Grupo | Total | Recebidas | Suas |\n|---|---:|---:|---:|\n"
    for row in cur.fetchall():
        # Tentar mapear JID conhecidos
        jid = row['group_jid']
        label = jid[:30] + "…" if len(jid) > 30 else jid
        if jid == "120363408325592607@g.us":
            label = "**Conselho Vallen**"
        md += f"| {label} | {row['total']} | {row['recebidas']} | {row['enviadas']} |\n"

    # 1d. Email triage
    cur.execute(f"""
        SELECT
          account_type,
          classification,
          status,
          COUNT(*) AS n
        FROM email_triage
        WHERE criado_em >= NOW() - INTERVAL '{days} days'
        GROUP BY account_type, classification, status
        ORDER BY n DESC
        LIMIT 15;
    """)
    rows = cur.fetchall()
    md += "\n### 1.3  Email Triage\n\n"
    if not rows:
        md += "_Sem emails triados na janela._\n"
    else:
        # Resumo por status
        cur.execute(f"""
            SELECT
              status,
              COUNT(*) AS n
            FROM email_triage
            WHERE criado_em >= NOW() - INTERVAL '{days} days'
            GROUP BY status
            ORDER BY n DESC;
        """)
        md += "Por status:\n\n"
        for row in cur.fetchall():
            md += f"- **{row['status']}**: {row['n']}\n"

        cur.execute(f"""
            SELECT
              account_type,
              classification,
              COUNT(*) AS n
            FROM email_triage
            WHERE criado_em >= NOW() - INTERVAL '{days} days'
              AND needs_attention = true
              AND status = 'pending'
            GROUP BY account_type, classification
            ORDER BY n DESC
            LIMIT 5;
        """)
        pending = cur.fetchall()
        if pending:
            md += "\n**Pendentes que pedem atenção:**\n\n"
            md += "| Conta | Classificação | Qtd |\n|---|---|---:|\n"
            for row in pending:
                md += f"| {row['account_type'] or '—'} | {row['classification'] or '—'} | {row['n']} |\n"

    return md


# ──────────────────────────────────────────────────────────────────────
# 2. COMPROMISSOS — tasks + RACI
# ──────────────────────────────────────────────────────────────────────

def section_compromissos(cur, cur_cos, days):
    md = section("2. Compromissos")

    # 2a. Tasks
    cur.execute(f"""
        SELECT
          COUNT(*) FILTER (WHERE status = 'pending' AND data_vencimento < NOW()) AS vencidas,
          COUNT(*) FILTER (WHERE status = 'pending' AND data_vencimento BETWEEN NOW() AND NOW() + INTERVAL '7 days') AS vence_7d,
          COUNT(*) FILTER (WHERE status = 'pending' AND (data_vencimento IS NULL OR data_vencimento > NOW() + INTERVAL '7 days')) AS futuras,
          COUNT(*) FILTER (WHERE status = 'completed' AND data_conclusao >= NOW() - INTERVAL '{days} days') AS concluidas_periodo,
          COUNT(*) FILTER (WHERE data_criacao >= NOW() - INTERVAL '{days} days') AS criadas_periodo
        FROM tasks;
    """)
    r = cur.fetchone()
    md += f"""
### 2.1  Tasks (INTEL)

- **Vencidas**: {r['vencidas']} 🔴
- **Vencem em 7d**: {r['vence_7d']} 🟡
- **Concluídas na janela**: {r['concluidas_periodo']}
- **Criadas na janela**: {r['criadas_periodo']}
- **Backlog futuro**: {r['futuras']}
"""

    # 2b. Top 5 tasks vencidas
    cur.execute("""
        SELECT
          titulo,
          (data_vencimento AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::date AS prazo,
          (NOW() - data_vencimento) AS atraso
        FROM tasks
        WHERE status = 'pending' AND data_vencimento < NOW()
        ORDER BY data_vencimento ASC
        LIMIT 5;
    """)
    rows = cur.fetchall()
    if rows:
        md += "\n**5 tasks mais atrasadas:**\n\n"
        for row in rows:
            dias = row['atraso'].days
            md += f"- `{dias}d` — {row['titulo'][:80]} (prazo: {row['prazo']})\n"

    # 2c. RACI ConselhoOS por empresa
    try:
        cur_cos.execute("""
            SELECT
              e.nome AS empresa,
              COUNT(*) FILTER (WHERE r.status IN ('pendente','em_andamento') AND r.prazo < CURRENT_DATE) AS vencidos,
              COUNT(*) FILTER (WHERE r.status IN ('pendente','em_andamento') AND r.prazo BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days') AS vence_7d,
              COUNT(*) FILTER (WHERE r.status = 'concluido' AND r.updated_at >= NOW() - INTERVAL '7 days') AS concluidos_7d
            FROM raci_itens r
            JOIN empresas e ON e.id = r.empresa_id
            GROUP BY e.nome
            ORDER BY vencidos DESC;
        """)
        rows = cur_cos.fetchall()
        md += "\n### 2.2  RACI (ConselhoOS)\n\n"
        if not rows:
            md += "_Sem itens RACI._\n"
        else:
            md += "| Empresa | Vencidos | Vence 7d | Concluídos 7d |\n|---|---:|---:|---:|\n"
            for row in rows:
                md += f"| {row['empresa']} | {row['vencidos']} | {row['vence_7d']} | {row['concluidos_7d']} |\n"
    except Exception as e:
        md += f"\n⚠️ RACI ConselhoOS: {type(e).__name__}: {e}\n"

    return md


# ──────────────────────────────────────────────────────────────────────
# 3. CALENDÁRIO
# ──────────────────────────────────────────────────────────────────────

def section_calendario(cur, days):
    md = section("3. Calendário")

    # Lembrete: calendar_events já em BRT, não converter
    cur.execute(f"""
        SELECT
          COUNT(*) AS total,
          SUM(EXTRACT(EPOCH FROM (end_datetime - start_datetime))) AS total_seconds,
          COUNT(*) FILTER (WHERE all_day = false) AS com_horario,
          COUNT(*) FILTER (WHERE all_day = true) AS dia_todo
        FROM calendar_events
        WHERE start_datetime >= NOW() - INTERVAL '{days} days'
          AND start_datetime < NOW();
    """)
    r = cur.fetchone()
    md += f"""
### 3.1  Volume

- **Eventos na janela**: {r['total']} ({r['com_horario']} com horário · {r['dia_todo']} dia inteiro)
- **Horas alocadas**: {fmt_hours(r['total_seconds'])}
"""

    # Heurística de classificação por palavra-chave no summary
    cur.execute(f"""
        SELECT
          CASE
            WHEN LOWER(summary) ~ '(reuni[aã]o|meet|call|conselho|1:1|c[aá]fé com|jantar|almo[çc]o)' THEN 'Reunião/Social'
            WHEN LOWER(summary) ~ '(focus|deep work|trabalho|estudo|sprint|escrever)' THEN 'Deep Work'
            WHEN LOWER(summary) ~ '(treino|yoga|m[eé]dico|consulta|familia|fam[ií]lia|escola|filha|filho)' THEN 'Pessoal'
            WHEN LOWER(summary) ~ '(viagem|deslocamento|trajeto|voo|hotel|uber)' THEN 'Deslocamento'
            ELSE 'Outros'
          END AS tipo,
          COUNT(*) AS n,
          SUM(EXTRACT(EPOCH FROM (end_datetime - start_datetime))) AS segundos
        FROM calendar_events
        WHERE start_datetime >= NOW() - INTERVAL '{days} days'
          AND start_datetime < NOW()
          AND all_day = false
        GROUP BY tipo
        ORDER BY segundos DESC;
    """)
    rows = cur.fetchall()
    md += "\n**Distribuição por tipo (heurística por keyword):**\n\n"
    md += "| Tipo | Eventos | Horas |\n|---|---:|---:|\n"
    for row in rows:
        md += f"| {row['tipo']} | {row['n']} | {fmt_hours(row['segundos'])} |\n"

    return md


# ──────────────────────────────────────────────────────────────────────
# 4. TONHA / PATROL AGENT
# ──────────────────────────────────────────────────────────────────────

def section_tonha(cur, days):
    md = section("4. Tonha / Patrol Agent")

    cur.execute(f"""
        SELECT
          status,
          COUNT(*) AS n,
          AVG(EXTRACT(EPOCH FROM (responded_at - criado_em))) FILTER (WHERE responded_at IS NOT NULL) AS avg_response_sec
        FROM action_proposals
        WHERE criado_em >= NOW() - INTERVAL '{days} days'
        GROUP BY status
        ORDER BY n DESC;
    """)
    rows = cur.fetchall()
    total = sum(r['n'] for r in rows)
    md += f"\n**Propostas geradas na janela**: {total}\n\n"
    if rows:
        md += "| Status | Qtd | % | Tempo médio até resposta |\n|---|---:|---:|---|\n"
        for r in rows:
            md += f"| {r['status']} | {r['n']} | {fmt_pct(r['n'], total)} | {fmt_hours(r['avg_response_sec'])} |\n"

    # Tipos de ação
    cur.execute(f"""
        SELECT action_type, COUNT(*) AS n
        FROM action_proposals
        WHERE criado_em >= NOW() - INTERVAL '{days} days'
        GROUP BY action_type
        ORDER BY n DESC
        LIMIT 8;
    """)
    rows = cur.fetchall()
    if rows:
        md += "\n**Top tipos de proposta:**\n\n"
        for r in rows:
            md += f"- {r['action_type']}: {r['n']}\n"

    # Propostas que expiraram sem resposta (ignored timeout)
    cur.execute(f"""
        SELECT COUNT(*) AS n
        FROM action_proposals
        WHERE criado_em >= NOW() - INTERVAL '{days} days'
          AND status = 'pending'
          AND expires_at IS NOT NULL
          AND expires_at < NOW();
    """)
    expired = cur.fetchone()['n']
    if expired:
        md += f"\n⚠️ **{expired} propostas expiraram sem resposta** (ignoradas por timeout)\n"

    return md


# ──────────────────────────────────────────────────────────────────────
# 5. LACUNAS DETECTÁVEIS
# ──────────────────────────────────────────────────────────────────────

def section_lacunas(cur, days):
    md = section("5. Lacunas Detectáveis")

    # 5a. Contatos phantom (provavelmente WA sem nome)
    cur.execute("""
        SELECT COUNT(*) AS n
        FROM contacts
        WHERE nome ILIKE 'Desconhecido %' OR nome ILIKE 'sem nome%';
    """)
    phantom = cur.fetchone()['n']
    md += f"\n### 5.1  Contatos phantom\n\n"
    md += f"- **{phantom}** contatos sem nome resolvido (Desconhecido / sem nome)\n"
    if phantom > 0:
        md += "  - Candidates de merge se também tiverem nome em outra conta/email\n"

    # 5b. Threads WA inbound sem resposta há >24h, por contato (exclui silence_default)
    cur.execute("""
        WITH last_msg AS (
          SELECT DISTINCT ON (m.contact_id)
            m.contact_id, c.nome, c.silence_default, m.direcao, m.criado_em, LEFT(m.conteudo, 80) AS preview
          FROM messages m
          LEFT JOIN contacts c ON c.id = m.contact_id
          WHERE m.contact_id IS NOT NULL
          ORDER BY m.contact_id, m.criado_em DESC
        )
        SELECT nome, criado_em, preview
        FROM last_msg
        WHERE direcao = 'incoming'
          AND criado_em < NOW() - INTERVAL '24 hours'
          AND criado_em > NOW() - INTERVAL '14 days'
          AND COALESCE(silence_default, false) = false
        ORDER BY criado_em DESC
        LIMIT 15;
    """)
    rows = cur.fetchall()
    md += f"\n### 5.2  Threads inbound sem sua resposta (>24h, últimos 14d)\n\n"
    if not rows:
        md += "_Nenhuma._ ✓\n"
    else:
        md += f"**{len(rows)}** threads abertas mostradas (até 15):\n\n"
        for r in rows:
            dt = r['criado_em'].strftime('%d/%m %H:%M')
            nome = (r['nome'] or "(sem nome)")[:30]
            preview = (r['preview'] or "—").replace('\n', ' ')[:80]
            md += f"- `{dt}` **{nome}** — _{preview}_\n"

    # 5c. Follow-ups prometidos (heurística: outgoing que contém promessa)
    cur.execute(f"""
        SELECT
          c.nome,
          m.criado_em,
          LEFT(m.conteudo, 120) AS preview
        FROM messages m
        LEFT JOIN contacts c ON c.id = m.contact_id
        WHERE m.direcao = 'outgoing'
          AND m.criado_em BETWEEN NOW() - INTERVAL '14 days' AND NOW() - INTERVAL '24 hours'
          AND m.conteudo ~* '(te mando|te envio|te aviso|vou mandar|vou enviar|deixa eu ver|j[aá] te|na sequ[eê]ncia)'
        ORDER BY m.criado_em DESC
        LIMIT 10;
    """)
    rows = cur.fetchall()
    md += f"\n### 5.3  Follow-ups prometidos por você (últimos 14d, >24h atrás)\n\n"
    md += "_Heurística por keyword — pode ter falsos positivos. Conferir manualmente:_\n\n"
    if not rows:
        md += "_Nenhum match._\n"
    else:
        for r in rows:
            dt = r['criado_em'].strftime('%d/%m %H:%M')
            nome = (r['nome'] or "(sem nome)")[:30]
            preview = (r['preview'] or "—").replace('\n', ' ')[:120]
            md += f"- `{dt}` → **{nome}**: _{preview}_\n"

    return md


# ──────────────────────────────────────────────────────────────────────
# 6. RESUMO EXECUTIVO (no topo do report)
# ──────────────────────────────────────────────────────────────────────

def section_resumo(cur, cur_cos, days):
    """Calcula 5-7 números-chave."""
    nums = {}

    cur.execute(f"""
        WITH inbound AS (
          SELECT m.id, m.contact_id, m.criado_em
          FROM messages m
          LEFT JOIN contacts c ON c.id = m.contact_id
          WHERE m.direcao = 'incoming'
            AND m.criado_em >= NOW() - INTERVAL '{days} days'
            AND COALESCE(c.silence_default, false) = false
        )
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE NOT EXISTS (
            SELECT 1 FROM messages m2
            WHERE m2.contact_id = inbound.contact_id
              AND m2.direcao = 'outgoing'
              AND m2.criado_em > inbound.criado_em
          )) AS sem_resposta
        FROM inbound;
    """)
    r = cur.fetchone()
    nums['wa_in'] = r['total']
    nums['wa_sem_resp'] = r['sem_resposta']

    cur.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'pending' AND data_vencimento < NOW();")
    nums['tasks_vencidas'] = cur.fetchone()['n']

    try:
        cur_cos.execute("SELECT COUNT(*) AS n FROM raci_itens WHERE status IN ('pendente','em_andamento') AND prazo < CURRENT_DATE;")
        nums['raci_vencidos'] = cur_cos.fetchone()['n']
    except Exception:
        nums['raci_vencidos'] = "—"

    cur.execute(f"""
        SELECT
          COUNT(*) AS n,
          SUM(EXTRACT(EPOCH FROM (end_datetime - start_datetime))) AS seg
        FROM calendar_events
        WHERE start_datetime >= NOW() - INTERVAL '{days} days'
          AND start_datetime < NOW()
          AND all_day = false;
    """)
    r = cur.fetchone()
    nums['eventos'] = r['n']
    nums['horas_cal'] = fmt_hours(r['seg'])

    cur.execute(f"""
        SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE status = 'pending' AND expires_at < NOW()) AS expiradas
        FROM action_proposals
        WHERE criado_em >= NOW() - INTERVAL '{days} days';
    """)
    r = cur.fetchone()
    nums['propostas'] = r['n']
    nums['propostas_expiradas'] = r['expiradas']

    md = f"""
## Resumo Executivo  ·  últimos {days} dias

| Indicador | Valor |
|---|---:|
| WA inbound recebidas | {nums['wa_in']} |
| ↳ ainda sem sua resposta | **{nums['wa_sem_resp']}** |
| Tasks INTEL vencidas | **{nums['tasks_vencidas']}** |
| RACI ConselhoOS vencidos | **{nums['raci_vencidos']}** |
| Eventos calendário | {nums['eventos']} ({nums['horas_cal']}) |
| Propostas Tonha/Patrol | {nums['propostas']} (↳ {nums['propostas_expiradas']} expiraram) |
"""
    return md


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--out", default="/tmp/weekly_ops_report.md")
    p.add_argument("--print-only", action="store_true")
    args = p.parse_args()

    now_brt = (datetime.utcnow() - timedelta(hours=3))  # tz-naive BRT
    header = f"""# Relatório Operacional Semanal — Renato Prado

_Gerado em {now_brt.strftime('%d/%m/%Y %H:%M')} BRT · janela últimos {args.days} dias_

---
"""

    sections = []

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    conn_cos = psycopg2.connect(CONSELHOOS_DATABASE_URL, cursor_factory=RealDictCursor)
    cur_cos = conn_cos.cursor()

    try:
        sections.append(safe("Resumo", section_resumo, cur, cur_cos, args.days))
        sections.append(safe("Comunicação", section_comunicacao, cur, args.days))
        sections.append(safe("Compromissos", section_compromissos, cur, cur_cos, args.days))
        sections.append(safe("Calendário", section_calendario, cur, args.days))
        sections.append(safe("Tonha/Patrol", section_tonha, cur, args.days))
        sections.append(safe("Lacunas", section_lacunas, cur, args.days))
    finally:
        cur.close(); conn.close()
        cur_cos.close(); conn_cos.close()

    body = header + "\n".join(sections)

    if not args.print_only:
        with open(args.out, "w") as f:
            f.write(body)
        print(f"✓ Relatório salvo em {args.out} ({len(body)} chars)", file=sys.stderr)

    print(body)


if __name__ == "__main__":
    main()
