"""
detector_operational — substitui partes de cos_portfolio + cos_sensor agendado.

Sinais:
- operational_task_vencida          — tasks.data_vencimento < hoje, status pending/in_progress
- operational_task_alta_prio_parada — tasks prioridade<=3 sem update ha +7d
- operational_task_sem_traction     — task pending prazo<=+3d c/ contact_id, outbound>24h, zero incoming desde
- operational_projeto_sem_update    — projects ativos sem atualizado_em ha +14d
- operational_milestone_vencido     — milestone data_prevista < hoje, status pendente
- operational_conflito_agenda       — 2+ calendar_events sobrepostos
"""
from __future__ import annotations

from datetime import date
from typing import List

from services.detectors._base import DetectorRun, emit_signal, expire_stale_signals, make_signal_hash, savepoint

DETECTOR_NAME = "detector_operational"

# Constante segue padrao do resto do codebase (evolution_api, editorial_*, smart_message_processor).
# Renato = dono do sistema; tasks com contact_id=ele sao pessoais, nao se "cobra" do proprio.
OWNER_CONTACT_ID = 14911


def run(conn) -> DetectorRun:
    res = DetectorRun(detector=DETECTOR_NAME)
    current_hashes: List[str] = []
    cur = conn.cursor()

    # ----- 1. Tasks vencidas -----
    try:
        with savepoint(conn, "task_vencida"):
            cur.execute("""
                SELECT id, titulo, data_vencimento, prioridade, contexto, project_id
                FROM tasks
                WHERE status IN ('pending', 'in_progress')
                  AND data_vencimento IS NOT NULL
                  AND data_vencimento < NOW()
                  AND data_vencimento > NOW() - INTERVAL '60 days'
                ORDER BY data_vencimento ASC
                LIMIT 50
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("operational_task_vencida", r["id"])
                current_hashes.append(sh)
                dias_atraso = (date.today() - r["data_vencimento"].date()).days if r["data_vencimento"] else 0
                prio = r["prioridade"] or 5
                urg = max(3, min(9, 4 + (5 - prio) + dias_atraso // 7))
                ctx = {
                    "task_id": r["id"],
                    "titulo": r["titulo"],
                    "data_vencimento": r["data_vencimento"].isoformat() if r["data_vencimento"] else None,
                    "prioridade": prio,
                    "contexto": r["contexto"],
                    "project_id": r["project_id"],
                    "dias_atraso": dias_atraso,
                }
                _bump(res, emit_signal(conn, tipo="operational_task_vencida", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"task_vencida: {str(e)[:200]}")

    # ----- 1b. Task com prazo iminente sem traction (outbound recente sem resposta) -----
    # Cobre o gap "mandei pra X, prazo amanha, X nao respondeu" que cai entre
    # operational_task_vencida (so dispara DEPOIS) e relacionamento_requer_resposta
    # (so dispara apos 3-7d sem reply). Aqui pega 0-3 dias antes do prazo.
    #
    # NAO COBRE (TODO v1):
    # - Triangulacao: task.contact_id=X mas mensagem foi pra terceiro Y
    #   (ex: "decidir Rodrigo via Lilian" — outbound foi pra Lilian, nao Rodrigo)
    # - Outbound em grupo WA (conversations.canal='whatsapp_group')
    #
    # ANTI-LOOP: filtra outbound com padroes de mensagem do bot/INTEL
    # (mesma whitelist do cos_sensor.py:1086). Nao filtra 'from_webhook'
    # porque Renato manda do celular pessoal e isso vem como from_webhook=true.
    try:
        with savepoint(conn, "task_sem_traction"):
            cur.execute("""
                WITH alvos AS (
                    SELECT
                        t.id, t.titulo, t.data_vencimento, t.prioridade,
                        t.contexto, t.project_id, t.contact_id,
                        ct.nome AS contato_nome,
                        ct.empresa AS contato_empresa,
                        EXTRACT(DAY FROM (t.data_vencimento - NOW()))::int AS dias_para_vencer
                    FROM tasks t
                    JOIN contacts ct ON ct.id = t.contact_id
                    WHERE t.status IN ('pending', 'in_progress')
                      AND t.data_vencimento IS NOT NULL
                      AND t.data_vencimento >= NOW()
                      AND t.data_vencimento <= NOW() + INTERVAL '3 days'
                      AND t.contact_id IS NOT NULL
                      AND t.contact_id != %s  -- nao cobra do proprio Renato
                ),
                ultimo_out AS (
                    SELECT DISTINCT ON (m.contact_id)
                        m.contact_id,
                        m.id AS msg_id,
                        COALESCE(m.enviado_em, m.criado_em) AS ts,
                        m.conversation_id,
                        LEFT(m.conteudo, 240) AS preview
                    FROM messages m
                    WHERE m.contact_id IN (SELECT contact_id FROM alvos)
                      AND m.direcao = 'outgoing'
                      -- anti-loop: ignora mensagens do bot/INTEL
                      AND NOT (
                        m.conteudo ILIKE 'Bom dia, Renato%%'
                        OR m.conteudo ILIKE '%%*Briefing*%%'
                        OR m.conteudo ILIKE '%%INTEL Proativo%%'
                        OR m.conteudo ILIKE '%%[Tonha]%%'
                      )
                    ORDER BY m.contact_id, COALESCE(m.enviado_em, m.criado_em) DESC
                )
                SELECT
                    a.*,
                    uo.ts AS outbound_ts,
                    uo.preview AS outbound_preview,
                    uo.conversation_id,
                    conv.canal AS conversation_canal,
                    EXTRACT(EPOCH FROM (NOW() - uo.ts))/3600 AS horas_desde_outbound
                FROM alvos a
                JOIN ultimo_out uo ON uo.contact_id = a.contact_id
                LEFT JOIN conversations conv ON conv.id = uo.conversation_id
                WHERE uo.ts < NOW() - INTERVAL '24 hours'
                  AND NOT EXISTS (
                      SELECT 1 FROM messages m3
                      WHERE m3.contact_id = a.contact_id
                        AND m3.direcao = 'incoming'
                        AND COALESCE(m3.recebido_em, m3.criado_em) > uo.ts
                  )
                ORDER BY a.data_vencimento ASC
                LIMIT 30
            """, (OWNER_CONTACT_ID,))
            for r in cur.fetchall():
                sh = make_signal_hash("operational_task_sem_traction", r["id"])
                current_hashes.append(sh)
                dias_pv = r["dias_para_vencer"] or 0
                horas_out = float(r["horas_desde_outbound"] or 0)
                prio = r["prioridade"] or 5
                # Base 4; sobe se prazo proximo + silencio longo + prio alta
                urg = 4
                if dias_pv <= 1: urg += 2
                elif dias_pv <= 2: urg += 1
                if horas_out >= 48: urg += 1
                if horas_out >= 96: urg += 1
                if prio <= 3: urg += 1
                urg = max(3, min(8, urg))
                ctx = {
                    "task_id": r["id"],
                    "titulo": r["titulo"],
                    "data_vencimento": r["data_vencimento"].isoformat() if r["data_vencimento"] else None,
                    "dias_para_vencer": dias_pv,
                    "prioridade": prio,
                    "contexto": r["contexto"],
                    "project_id": r["project_id"],
                    "contact_id": r["contact_id"],
                    "contato_nome": r["contato_nome"],
                    "contato_empresa": r["contato_empresa"],
                    "ultimo_outbound": {
                        "ts": r["outbound_ts"].isoformat() if r["outbound_ts"] else None,
                        "horas_atras": round(horas_out, 1),
                        "canal": r["conversation_canal"],
                        "conversation_id": r["conversation_id"],
                        "preview": r["outbound_preview"],
                    },
                }
                _bump(res, emit_signal(conn, tipo="operational_task_sem_traction", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"task_sem_traction: {str(e)[:200]}")

    # ----- 2. Projetos ativos sem update — prio<=3 OR dias>30 -----
    # Antes 10+ signals urg 6 viravam brain escalates. Maioria era projeto
    # baixa prioridade silenciado deliberadamente. Filtra pra so emitir quando
    # realmente vale o ping: prioridade alta (1-3) com 14d+ OU qualquer prio
    # com 30d+.
    try:
        with savepoint(conn, "projeto_sem_update"):
            cur.execute("""
                SELECT id, nome, status, prioridade, atualizado_em, owner_contact_id, tags
                FROM projects
                WHERE status = 'ativo'
                  AND atualizado_em < NOW() - INTERVAL '14 days'
                  AND (
                    (prioridade IS NOT NULL AND prioridade <= 3)
                    OR atualizado_em < NOW() - INTERVAL '30 days'
                  )
                ORDER BY atualizado_em ASC
                LIMIT 20
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("operational_projeto_sem_update", r["id"])
                current_hashes.append(sh)
                dias = (date.today() - r["atualizado_em"].date()).days if r["atualizado_em"] else 30
                prio = r["prioridade"] or 5
                # Alta prio: 4-7. Baixa prio com 30d+: 3-5
                if prio <= 3:
                    urg = max(4, min(7, 4 + dias // 14))
                else:
                    urg = max(3, min(5, 3 + dias // 30))
                ctx = {
                    "project_id": r["id"],
                    "nome": r["nome"],
                    "prioridade": prio,
                    "tags": r["tags"],
                    "atualizado_em": r["atualizado_em"].isoformat() if r["atualizado_em"] else None,
                    "dias_sem_update": dias,
                }
                _bump(res, emit_signal(conn, tipo="operational_projeto_sem_update", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"projeto_sem_update: {str(e)[:200]}")

    # ----- 3. Milestones vencidos -----
    try:
        with savepoint(conn, "milestone_vencido"):
            cur.execute("""
                SELECT m.id, m.titulo, m.data_prevista, m.project_id, p.nome AS projeto
                FROM project_milestones m
                JOIN projects p ON p.id = m.project_id
                WHERE m.status IN ('pendente', 'em_andamento')
                  AND m.data_prevista IS NOT NULL
                  AND m.data_prevista < CURRENT_DATE
                  AND m.data_prevista > CURRENT_DATE - INTERVAL '90 days'
                  AND p.status = 'ativo'
                ORDER BY m.data_prevista ASC
                LIMIT 30
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("operational_milestone_vencido", r["id"])
                current_hashes.append(sh)
                dias_atraso = (date.today() - r["data_prevista"]).days
                urg = max(4, min(8, 4 + dias_atraso // 14))
                ctx = {
                    "milestone_id": r["id"],
                    "titulo": r["titulo"],
                    "data_prevista": r["data_prevista"].isoformat(),
                    "project_id": r["project_id"],
                    "projeto": r["projeto"],
                    "dias_atraso": dias_atraso,
                }
                _bump(res, emit_signal(conn, tipo="operational_milestone_vencido", signal_hash=sh, urgencia=urg, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"milestone_vencido: {str(e)[:200]}")

    # ----- 4. Conflito de agenda (overlap em proximos 7d) -----
    try:
        with savepoint(conn, "conflito_agenda"):
            cur.execute("""
                SELECT e1.id AS e1_id, e1.summary AS e1_sum, e1.start_datetime AS e1_start,
                       e2.id AS e2_id, e2.summary AS e2_sum, e2.start_datetime AS e2_start
                FROM calendar_events e1
                JOIN calendar_events e2 ON e1.id < e2.id
                    AND e1.end_datetime > e2.start_datetime
                    AND e2.end_datetime > e1.start_datetime
                WHERE e1.status = 'confirmed' AND e2.status = 'confirmed'
                  AND e1.start_datetime BETWEEN NOW() AND NOW() + INTERVAL '7 days'
                  AND NOT e1.all_day AND NOT e2.all_day
                ORDER BY e1.start_datetime ASC
                LIMIT 20
            """)
            for r in cur.fetchall():
                sh = make_signal_hash("operational_conflito_agenda", r["e1_id"], r["e2_id"])
                current_hashes.append(sh)
                ctx = {
                    "evento_1": {"id": r["e1_id"], "summary": r["e1_sum"], "start": r["e1_start"].isoformat()},
                    "evento_2": {"id": r["e2_id"], "summary": r["e2_sum"], "start": r["e2_start"].isoformat()},
                }
                _bump(res, emit_signal(conn, tipo="operational_conflito_agenda", signal_hash=sh, urgencia=7, contexto=ctx, detector=DETECTOR_NAME))
    except Exception as e:
        res.errors.append(f"conflito_agenda: {str(e)[:200]}")

    res.expired = expire_stale_signals(conn, detector=DETECTOR_NAME, current_hashes=current_hashes)
    return res


def _bump(res: DetectorRun, result: str) -> None:
    if result == "emitted":
        res.emitted += 1
    elif result == "updated":
        res.updated += 1
    else:
        res.skipped += 1
