"""
Camada de inteligência — loop de revisão por frente (CoS, FASE 0: só observa).

Origem: o Renato pediu (24/07) "uma camada rodando o tempo todo, verificando
email/WA, sabendo como dar andamento em cada frente, e só me acionando quando
necessário, segundo as políticas aprovadas". A fase 0 (ratificada por ele) é
READ-ONLY: a camada LÊ e DIZ — não toca em nada — pra ele julgar a qualidade do
julgamento antes de ganhar qualquer poder de agir.

O que faz: pra cada frente ativa com grupo/canal (project_whatsapp_groups ativo),
reúne o estado real (projeto + tasks + membros + DMs + mensagens de grupo + notas)
e produz um DEBRIEFING por frente:
  estado · movimento recente · trava · precisa_de_voce (só portão genuíno) ·
  vigílias · nota honesta (drift / task alarmista / spam a ignorar).
O placar ("de N frentes, X precisam de você") é derivado DETERMINÍSTICO das
frentes (sem 2ª chamada LLM) — mais barato e confiável.

Espelha o gather de `project_smart_update.analyze_project_updates` (mesmas queries,
read-only) + o padrão de custo `llm_usage.record_response`. NÃO executa ação
(sem create_task/draft/send) — a promoção pra Auto/Propor é decisão futura do
Renato, sob [[feedback_cos_autonomy_policy]].
"""
import os
import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import httpx

from database import get_db
from services import llm, llm_usage
from services.group_message_sync import get_group_messages
from services.tz import now_utc

logger = logging.getLogger(__name__)

# Janelas de leitura
_GROUP_DAYS = 12
_DM_DAYS = 21
_NOTES_LIMIT = 3

_SYSTEM = """Você é a camada de inteligência do Renato — o Chief of Staff digital dele, rodando sozinho.

Sua tarefa NESTA fase é SÓ LER E DIZER. Você NÃO age (não cria task, não rascunha, não envia). Você lê o estado real de UMA frente e devolve um debriefing curto e afiado, na altitude de um chief of staff — não um relatório, um julgamento.

O usuário do sistema é RENATO. Em mensagem de grupo/DM, "Renato"/outgoing = ele.

Para a frente, decida:
- **estado**: onde a frente está, em 1-2 frases. O essencial, não a história.
- **movimento**: o que andou de concreto nos últimos dias (quem fez o quê). Vazio se nada andou.
- **trava**: o que está segurando o avanço agora (1 frase). Vazio se nada trava.
- **precisa_de_voce**: SÓ marque sim se for um PORTÃO GENUÍNO — uma decisão/ação que SÓ o Renato pode fazer (autoridade/relação/gosto/info que só ele tem) E que é irreversível ou sensível ao tempo agora. Logística, status, "aguardar terceiro", próximo-passo-óbvio NÃO são portão. Na dúvida, é NÃO. Se sim, diga em 1 frase O QUE ele precisa fazer e por quê.
- **vigilias**: o que VOCÊ ficaria de olho (retorno de terceiro esperado, prazo se aproximando, execução a confirmar) — coisas que não pedem decisão dele agora, mas você acompanharia. Máx 2.
- **nota**: honestidade — se uma task está alarmista/desatualizada, se há spam/ruído a ignorar, se algo está driftando. Vazio se nada.

Regras duras:
- Prioridade no INTEL: número MAIOR = mais importante (8-10 gate estratégico; 1-3 baixa).
- Cite evidência ao afirmar (ID de task, quem disse no grupo). Nunca invente.
- DATAS E VALORES: copie EXATO da fonte, nunca parafraseie nem aproxime (se a nota diz "17/08", escreva 17/08 — não "início de agosto" nem "01/08").
- Ignore spam/ruído (ex: "aumente seu limite de crédito", robô) — e diga na nota que ignorou.
- Português correto, com acento. Tom: informal-com-gravidade, direto, sem preâmbulo, sem emoji.

Retorne APENAS JSON válido (sem markdown):
{
  "estado": "...",
  "movimento": "...",
  "trava": "...",
  "precisa_de_voce": {"sim": false, "o_que": ""},
  "vigilias": ["..."],
  "nota": "..."
}"""


def _target_frentes(cursor) -> List[Dict[str, Any]]:
    """TODOS os projetos ativos (o Renato ampliou 24/07 de 'grupo/canal' pra todos).
    Frentes sem nenhum sinal (task/membro/grupo) sao puladas em run_daily_review."""
    cursor.execute("""
        SELECT p.id, p.nome, p.descricao, p.prioridade
        FROM projects p
        WHERE p.status = 'ativo'
        ORDER BY p.prioridade DESC NULLS LAST, p.id
    """)
    return [dict(r) for r in cursor.fetchall()]


def _gather_frente(cursor, project_id: int) -> Dict[str, Any]:
    """Reúne o estado real de uma frente (read-only). Espelha o smart_update."""
    cursor.execute("SELECT id, nome, descricao FROM projects WHERE id = %s", (project_id,))
    project = cursor.fetchone()
    if not project:
        return {}
    project = dict(project)

    cursor.execute("""
        SELECT t.id, t.titulo, t.descricao, t.status, t.data_vencimento, t.prioridade,
               c.nome AS responsavel
        FROM tasks t
        LEFT JOIN contacts c ON c.id = t.contact_id
        WHERE t.project_id = %s AND t.status NOT IN ('completed', 'cancelled')
        ORDER BY t.data_vencimento ASC NULLS LAST
    """, (project_id,))
    tasks = [dict(r) for r in cursor.fetchall()]

    cursor.execute("""
        SELECT pm.contact_id, c.nome, pm.papel
        FROM project_members pm JOIN contacts c ON c.id = pm.contact_id
        WHERE pm.project_id = %s
    """, (project_id,))
    members = [dict(r) for r in cursor.fetchall()]

    # DMs recentes dos membros (fonte de sinal das frentes SEM grupo). Espelha
    # o smart_update: agrupa por contato, janela _DM_DAYS, corta mensagem curta.
    dms: List[Dict[str, Any]] = []
    member_ids = [m["contact_id"] for m in members if m.get("contact_id")]
    if member_ids:
        cursor.execute("""
            SELECT m.conteudo, m.direcao, COALESCE(m.enviado_em, m.recebido_em) AS ts,
                   c.nome AS contact_nome
            FROM messages m
            JOIN conversations cv ON cv.id = m.conversation_id
            JOIN contacts c ON c.id = cv.contact_id
            WHERE cv.contact_id = ANY(%s)
              AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - (%s || ' days')::interval
              AND m.conteudo IS NOT NULL AND LENGTH(m.conteudo) > 10
            ORDER BY cv.contact_id, COALESCE(m.enviado_em, m.recebido_em) ASC
            LIMIT 40
        """, (member_ids, _DM_DAYS))
        dms = [dict(r) for r in cursor.fetchall()]

    # Mensagens ROTEADAS por conteúdo (roteador B) — de QUALQUER um, membro ou não.
    # Fecha o furo do gather-por-membership. Exclui as já cobertas pelo DM dos membros.
    dm_ids = {m.get("id") for m in dms if m.get("id")}
    routed: List[Dict[str, Any]] = []
    try:
        cursor.execute("""
            SELECT m.id, cv.canal, m.direcao, COALESCE(m.enviado_em, m.recebido_em) AS ts,
                   c.nome AS sender, LEFT(m.conteudo, 400) AS conteudo
            FROM message_project_links l
            JOIN messages m ON m.id = l.message_id
            JOIN conversations cv ON cv.id = m.conversation_id
            JOIN contacts c ON c.id = cv.contact_id
            WHERE l.project_id = %s
              AND COALESCE(m.enviado_em, m.recebido_em) > NOW() - (%s || ' days')::interval
            ORDER BY ts DESC LIMIT 25
        """, (project_id, _DM_DAYS))
        routed = [dict(r) for r in cursor.fetchall() if dict(r)["id"] not in dm_ids]
    except Exception as e:
        # tabela do roteador (053) pode não existir ainda — degrada gracioso
        logger.warning("frente_review: routed skip (%s)", e)
        cursor.connection.rollback()

    # Notas recentes (memória curta da frente)
    cursor.execute("""
        SELECT titulo, conteudo, criado_em FROM project_notes
        WHERE project_id = %s ORDER BY criado_em DESC LIMIT %s
    """, (project_id, _NOTES_LIMIT))
    notes = [dict(r) for r in cursor.fetchall()]

    # Grupos vinculados
    cursor.execute("""
        SELECT group_jid, group_name FROM project_whatsapp_groups
        WHERE project_id = %s AND ativo = TRUE
    """, (project_id,))
    groups = [dict(r) for r in cursor.fetchall()]

    return {"project": project, "tasks": tasks, "members": members, "dms": dms,
            "routed": routed, "notes": notes, "groups": groups}


def _has_signal(g: Dict[str, Any]) -> bool:
    """Frente tem algo pra raciocinar? (evita gastar LLM em casca vazia)."""
    return bool(g.get("tasks") or g.get("dms") or g.get("routed") or g.get("groups") or g.get("notes"))


def _fmt_gather(g: Dict[str, Any]) -> str:
    """Serializa o gather num texto compacto pro prompt."""
    p = g["project"]
    today = date.today().isoformat()
    parts = [f"FRENTE: {p['nome']}", f"DESCRIÇÃO: {(p.get('descricao') or '')[:400]}", f"HOJE: {today}"]

    if g["members"]:
        parts.append("PARTICIPANTES: " + ", ".join(f"{m['nome']} ({m.get('papel') or 'membro'})" for m in g["members"]))

    if g["tasks"]:
        parts.append("\nTAREFAS ABERTAS:")
        for t in g["tasks"]:
            venc = str(t["data_vencimento"])[:10] if t.get("data_vencimento") else "sem prazo"
            atrasada = " [ATRASADA]" if t.get("data_vencimento") and str(t["data_vencimento"])[:10] < today else ""
            resp = f" resp:{t['responsavel']}" if t.get("responsavel") else ""
            desc = f" — {t['descricao'][:220]}" if t.get("descricao") else ""
            parts.append(f"- [#{t['id']} prio{t.get('prioridade')}]{atrasada} {t['titulo']} (vence {venc}{resp}){desc}")

    if g["notes"]:
        parts.append("\nNOTAS RECENTES DA FRENTE:")
        for n in reversed(g["notes"]):
            dt = str(n.get("criado_em") or "?")[:10]
            parts.append(f"--- {dt} · {n.get('titulo') or ''} ---\n{(n.get('conteudo') or '')[:500]}")

    # DMs dos membros (agrupadas por contato) — sinal das frentes sem grupo
    if g.get("dms"):
        from collections import defaultdict
        convos = defaultdict(list)
        for m in g["dms"]:
            convos[m["contact_nome"]].append(m)
        for nome, msgs in convos.items():
            parts.append(f"\n--- DM com {nome} (recentes) ---")
            for m in msgs[-12:]:
                dt = str(m.get("ts") or "?")[:16]
                sender = "RENATO" if m.get("direcao") == "outgoing" else nome
                parts.append(f"[{dt}] {sender}: {(m.get('conteudo') or '')[:400]}")

    # Mensagens ROTEADAS por conteúdo (roteador B) — inclui NÃO-membros
    if g.get("routed"):
        parts.append("\n--- MENSAGENS LIGADAS À FRENTE POR CONTEÚDO (podem ser de não-membros) ---")
        for m in g["routed"]:
            dt = str(m.get("ts") or "?")[:16]
            who = "RENATO" if m.get("direcao") == "outgoing" else (m.get("sender") or "?")
            parts.append(f"[{dt} · {m.get('canal')}] {who}: {(m.get('conteudo') or '')[:400]}")

    # Mensagens de grupo (fonte principal do 'o que anda')
    for grp in g["groups"]:
        msgs = []
        try:
            msgs = get_group_messages(grp["group_jid"], limit=30) or []
        except Exception as e:
            logger.warning("frente_review: get_group_messages %s: %s", grp["group_jid"], e)
        if not msgs:
            continue
        parts.append(f"\n--- GRUPO WHATSAPP: {grp['group_name']} (recentes) ---")
        for m in msgs[-25:]:
            dt = str(m.get("timestamp") or m.get("created_at") or "?")[:16]
            sender = "RENATO" if m.get("from_me") else (m.get("sender_name") or "?")
            content = (m.get("content") or "")[:400]
            parts.append(f"[{dt}] {sender}: {content}")

    return "\n".join(parts)


async def review_frente(project_id: int, gather: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Debriefing de UMA frente (read-only, 1 chamada LLM). Falha graciosa."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"project_id": project_id, "error": "ANTHROPIC_API_KEY ausente"}

    if gather is None:
        with get_db() as conn:
            gather = _gather_frente(conn.cursor(), project_id)
    if not gather:
        return {"project_id": project_id, "error": "frente nao encontrada"}

    prompt = _fmt_gather(gather)
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": llm.BALANCED,
                    "max_tokens": 900,
                    "system": _SYSTEM,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            logger.error("frente_review LLM %s: %s", resp.status_code, resp.text[:200])
            return {"project_id": project_id, "error": f"LLM {resp.status_code}"}
        result = resp.json()
        llm_usage.record_response("cos.frente_review", llm.BALANCED, result)  # F-E custo por-função
        text = result.get("content", [{}])[0].get("text", "")
        start, end = text.find("{"), text.rfind("}") + 1
        if start < 0 or end <= start:
            return {"project_id": project_id, "error": "resposta nao-JSON", "raw": text[:200]}
        deb = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        logger.error("frente_review parse %s: %s", project_id, e)
        return {"project_id": project_id, "error": "JSON invalido"}
    except Exception as e:
        logger.error("frente_review %s: %s", project_id, e)
        return {"project_id": project_id, "error": str(e)}

    # normaliza + carimba metadados
    pdv = deb.get("precisa_de_voce") or {}
    return {
        "project_id": project_id,
        "frente": gather["project"]["nome"],
        "estado": (deb.get("estado") or "").strip(),
        "movimento": (deb.get("movimento") or "").strip(),
        "trava": (deb.get("trava") or "").strip(),
        "precisa_de_voce": {"sim": bool(pdv.get("sim")), "o_que": (pdv.get("o_que") or "").strip()},
        "vigilias": [v for v in (deb.get("vigilias") or []) if v][:2],
        "nota": (deb.get("nota") or "").strip(),
    }


async def run_daily_review(limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Roda a camada sobre todas as frentes-alvo (grupo/canal), read-only.
    Retorna o payload do dia: frentes + placar derivado. NÃO persiste (o caller
    decide) — mas é o formato que a storage/cockpit consomem.
    """
    with get_db() as conn:
        cur = conn.cursor()
        frentes = _target_frentes(cur)
        if limit:
            frentes = frentes[:limit]
        gathers = {f["id"]: _gather_frente(cur, f["id"]) for f in frentes}

    # Pula frentes sem nenhum sinal (casca vazia) — não gasta LLM.
    frentes = [f for f in frentes if _has_signal(gathers.get(f["id"], {}))]

    # Paraleliza as revisões (semáforo) — sequencial estourava 280s com ~18 frentes.
    import asyncio
    sem = asyncio.Semaphore(6)

    async def _one(f):
        async with sem:
            return await review_frente(f["id"], gather=gathers.get(f["id"]))

    results = await asyncio.gather(*[_one(f) for f in frentes], return_exceptions=True)

    debriefs: List[Dict[str, Any]] = []
    for f, r in zip(frentes, results):
        if isinstance(r, Exception) or (isinstance(r, dict) and r.get("error")):
            logger.warning("frente_review pulou #%s: %s", f["id"], r)
        else:
            debriefs.append(r)

    # Placar DETERMINÍSTICO (sem 2ª chamada LLM)
    precisa = [{"frente": d["frente"], "project_id": d["project_id"], "o_que": d["precisa_de_voce"]["o_que"]}
               for d in debriefs if d["precisa_de_voce"]["sim"]]
    vigilias = [{"frente": d["frente"], "project_id": d["project_id"], "item": v}
                for d in debriefs for v in d["vigilias"]]
    cobertas = [d["frente"] for d in debriefs if not d["precisa_de_voce"]["sim"]]

    return {
        "run_at": now_utc().isoformat(),
        "n_frentes": len(debriefs),
        "frentes": debriefs,
        "placar": {"precisa_de_voce": precisa, "vigilias": vigilias, "cobertas": cobertas},
    }


def persist_review(payload: Dict[str, Any]) -> int:
    """Grava o payload do dia em cos_daily_review. Retorna o id."""
    from services.tz import to_brt
    run_date = to_brt(now_utc()).date()
    n_frentes = payload.get("n_frentes", 0)
    n_precisa = len((payload.get("placar") or {}).get("precisa_de_voce") or [])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO cos_daily_review (run_date, n_frentes, n_precisa, payload)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (run_date, n_frentes, n_precisa, json.dumps(payload, ensure_ascii=False)),
        )
        rid = cur.fetchone()[0]
        conn.commit()
    return rid


def latest_review() -> Optional[Dict[str, Any]]:
    """Último debriefing gravado (o que o cockpit lê). None se ainda não rodou."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT payload, run_at FROM cos_daily_review ORDER BY run_at DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        return None
    payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return payload


async def run_and_persist() -> Dict[str, Any]:
    """Roda o loop completo e persiste. Chamado pelo cron diário."""
    payload = await run_daily_review()
    rid = persist_review(payload)
    return {"id": rid, "n_frentes": payload["n_frentes"],
            "n_precisa": len(payload["placar"]["precisa_de_voce"])}
