"""
RACI Group Shadow — fecha o fantasma do updater de RACI (fix 10/07).

Problema: `process_group_message` (que aplica RACI) so roda no webhook, mas as
mensagens de grupo chegam pelo `group_message_sync` (lote), que so grava. Logo o
updater NUNCA corria nos reportes reais dos conselheiros.

Este modulo e o elo que faltava: um cron varre `group_messages` nao-processados,
resolve a empresa (project_whatsapp_groups -> projeto -> ConselhoOS), roda
`propose_updates_from_text` (que NAO aplica) e guarda cada proposta em
`raci_group_proposals` com status 'pending_review', notificando o Renato.

SHADOW-FIRST: nada e auto-aplicado no RACI do cliente. Toda proposta — inclusive
alta confianca — espera aprovacao do Renato (`apply_group_proposal`). Governanca
de cliente nao muda sem gate humano (mesma licao da CoPiloto que se recusou a
inventar).
"""
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import psycopg2

from database import get_db
from services.raci_smart_updates import (
    propose_updates_from_text,
    apply_proposal,
    MIN_TEXT_LEN_FOR_AI,
)

log = logging.getLogger("raci_group_shadow")


class TransientInfraError(Exception):
    """Falha de infra (DB/CONSELHOOS indisponivel). Aborta o run atual pra retry
    no proximo tick — SEM marcar/queimar mensagens (nao perde o backlog)."""


def _cos_conn():
    cos = (os.getenv("CONSELHOOS_DATABASE_URL") or "").strip()
    if not cos:
        return None
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(cos)


def _resolve_empresa(group_jid: str) -> Optional[Tuple[str, str]]:
    """group_jid -> (empresa_id, empresa_nome) no ConselhoOS. None se nao mapeia."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT p.nome AS nome FROM project_whatsapp_groups pwg
               JOIN projects p ON p.id = pwg.project_id
               WHERE pwg.group_jid = %s AND pwg.ativo = TRUE LIMIT 1""",
            (group_jid,),
        )
        row = cur.fetchone()
    if not row:
        return None
    proj_nome = row["nome"] if isinstance(row, dict) else row[0]
    conn2 = _cos_conn()
    if not conn2:
        return None
    try:
        cur2 = conn2.cursor()
        cur2.execute(
            "SELECT id, nome FROM empresas WHERE LOWER(nome) LIKE LOWER(%s) LIMIT 1",
            (f"%{proj_nome}%",),
        )
        emp = cur2.fetchone()
    finally:
        conn2.close()
    return (str(emp[0]), emp[1]) if emp else None


def _resolve_empresa_safe(group_jid: str) -> Optional[Tuple[str, str]]:
    """_resolve_empresa, mas classifica a falha:
      - conexao caida (OperationalError/InterfaceError) -> TransientInfraError
        (o loop aborta o run e tenta de novo no proximo tick, sem queimar msgs);
      - qualquer outro erro -> propaga como 'poison' daquele jid (o loop marca as
        msgs desse jid como sem-empresa e AVANCA, pra nao re-travar a janela)."""
    try:
        return _resolve_empresa(group_jid)
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        raise TransientInfraError(f"resolve_empresa({group_jid}): {type(e).__name__}: {e}") from e


def _fetch_item_acoes(item_ids: List[str]) -> Dict[str, str]:
    """item_id -> acao (snapshot pro review). ConselhoOS."""
    if not item_ids:
        return {}
    conn2 = _cos_conn()
    if not conn2:
        return {}
    try:
        cur2 = conn2.cursor()
        cur2.execute("SELECT id, acao FROM raci_itens WHERE id = ANY(%s::uuid[])", (item_ids,))
        return {str(r[0]): (r[1] or "") for r in cur2.fetchall()}
    except Exception:
        return {}
    finally:
        conn2.close()


async def _run_sweep(
    msgs: List[Any],
    *,
    resolve_empresa: Callable[[str], Optional[Tuple[str, str]]],
    propose: Callable[[str, str], Awaitable[List[Dict[str, Any]]]],
    fetch_acoes: Callable[[List[str]], Dict[str, str]],
    store_proposal: Callable[..., Optional[Dict[str, Any]]],
    mark_processed: Callable[[int], None],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Nucleo resiliente do sweep (deps injetadas -> testavel sem DB).

    Garantia central (fix 22/07): PROGRESSO MONOTONICO. Uma mensagem individual
    problematica (sender/empresa que nao resolve, INSERT que falha, etc.) NUNCA
    derruba o batch nem re-trava a janela: pula + loga + AVANCA (marca processada).
    So uma falha de infra global (DB caido) aborta o run limpo — sem queimar msgs.

    Isso conserta o stall que segurou 5.548 msgs: antes, uma msg que levantava
    excecao nao era marcada e voltava a ocupar as vagas do LIMIT em todo tick
    (livelock), impedindo o sweep de avancar."""
    out: Dict[str, Any] = {"scanned": 0, "proposals": 0, "processed_msgs": 0, "errors": 0}
    emp_cache: Dict[str, Optional[Tuple[str, str]]] = {}
    new_proposals: List[Dict[str, Any]] = []

    for m in msgs:
        out["scanned"] += 1
        mid = m["id"]
        jid = m["group_jid"]

        # 1. Resolucao de empresa (uma vez por jid).
        if jid not in emp_cache:
            try:
                emp_cache[jid] = resolve_empresa(jid)
            except TransientInfraError as e:
                # Infra global caiu -> aborta o run, retry no proximo tick (sem burn).
                log.warning("raci_group_shadow: infra transiente (%s) — aborta run, retry depois", e)
                out["aborted_transient"] = True
                break
            except Exception:
                # jid deterministicamente ruim -> trata as msgs dele como sem-empresa
                # e AVANCA (nao trava a janela). Cacheia pra nao repetir a query.
                log.exception("raci_group_shadow: jid %s nao resolve (poison) — msgs viram sem-empresa", jid)
                emp_cache[jid] = None

        emp = emp_cache[jid]

        # 2. Processamento da mensagem — isolado: erro individual pula + AVANCA.
        try:
            if not emp:
                mark_processed(mid)  # grupo sem empresa mapeada -> nao reabre
                out["processed_msgs"] += 1
                continue

            empresa_id, empresa_nome = emp
            props = await propose(m["content"], empresa_id)
            acoes = fetch_acoes([str(p.get("item_id")) for p in props]) if props else {}

            for p in props:
                stored = store_proposal(mid, jid, empresa_id, empresa_nome, m.get("sender_name"), p, acoes)
                if stored:
                    new_proposals.append(stored)
                    out["proposals"] += 1

            mark_processed(mid)
            out["processed_msgs"] += 1
        except Exception:
            # Msg poison (sender/conteudo/INSERT). NAO aborta o sweep: loga + avanca.
            log.exception(
                "raci_group_shadow: msg poison id=%s sender=%r content=%r — skip+avanca",
                mid, m.get("sender_name"), (m.get("content") or "")[:80],
            )
            out["errors"] += 1
            try:
                mark_processed(mid)  # AVANCA pra nao re-travar a janela (root cause do stall)
                out["processed_msgs"] += 1
            except Exception:
                log.exception("raci_group_shadow: mark_processed falhou id=%s (fica pro proximo run)", mid)

    return out, new_proposals


async def process_unreviewed_groups(days: int = 7, limit: int = 40) -> Dict[str, Any]:
    """Varre group_messages nao-processados, gera propostas shadow e notifica o
    Renato. NUNCA aplica no ConselhoOS. Retorna resumo. Nunca levanta.

    Resiliente (fix 22/07): msg problematica pula + avanca; so infra caida aborta
    o run (retry). Ver _run_sweep. Pra reprocessar o backlog historico, aumente
    `days` (ex: days=3650) e `limit`."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT gm.id, gm.group_jid, gm.sender_name, gm.content
                     FROM group_messages gm
                     JOIN project_whatsapp_groups pwg
                       ON pwg.group_jid = gm.group_jid AND pwg.ativo = TRUE
                    WHERE gm.raci_processed_at IS NULL
                      AND gm.from_me = FALSE
                      AND gm.content IS NOT NULL
                      AND length(gm.content) >= %s
                      AND gm.timestamp > (now() AT TIME ZONE 'UTC') - (%s || ' days')::interval
                 ORDER BY gm.timestamp ASC
                    LIMIT %s""",
                (MIN_TEXT_LEN_FOR_AI, str(days), limit),
            )
            msgs = cur.fetchall()
    except Exception as e:
        log.exception("raci_group_shadow: falha buscando mensagens")
        return {"scanned": 0, "proposals": 0, "processed_msgs": 0, "errors": 0,
                "error": f"{type(e).__name__}: {e}"}

    out, new_proposals = await _run_sweep(
        msgs,
        resolve_empresa=_resolve_empresa_safe,
        propose=propose_updates_from_text,
        fetch_acoes=_fetch_item_acoes,
        store_proposal=_store_proposal,
        mark_processed=_mark_processed,
    )

    if new_proposals:
        try:
            await _notify_renato(new_proposals)
        except Exception:
            log.exception("raci_group_shadow: falha notificando Renato")

    log.info("raci_group_shadow: %s", out)
    return out


def unprocessed_backlog_stats(stale_days: int = 3) -> Dict[str, Any]:
    """Monitor read-only do backlog de group_messages nao-processadas (usa o
    indice parcial idx_group_messages_raci_unprocessed). Retorna quantas estao
    pendentes e quantas ja envelheceram > stale_days. Base do alerta de stall."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT count(*) AS unprocessed,
                      count(*) FILTER (
                          WHERE gm.timestamp < (now() AT TIME ZONE 'UTC') - (%s || ' days')::interval
                      ) AS stale,
                      MIN(gm.timestamp) AS oldest
                 FROM group_messages gm
                 JOIN project_whatsapp_groups pwg
                   ON pwg.group_jid = gm.group_jid AND pwg.ativo = TRUE
                WHERE gm.raci_processed_at IS NULL
                  AND gm.from_me = FALSE
                  AND gm.content IS NOT NULL
                  AND length(gm.content) >= %s""",
            (str(stale_days), MIN_TEXT_LEN_FOR_AI),
        )
        row = cur.fetchone()
    unprocessed = (row["unprocessed"] if isinstance(row, dict) else row[0]) or 0
    stale = (row["stale"] if isinstance(row, dict) else row[1]) or 0
    oldest = row["oldest"] if isinstance(row, dict) else row[2]
    return {
        "unprocessed": int(unprocessed),
        "stale": int(stale),
        "stale_days": stale_days,
        "oldest": oldest.isoformat() if oldest else None,
    }


def _mark_processed(group_message_id: int) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE group_messages SET raci_processed_at = (now() AT TIME ZONE 'UTC') WHERE id = %s",
            (group_message_id,),
        )


def _store_proposal(mid, jid, empresa_id, empresa_nome, sender, p, acoes) -> Optional[Dict[str, Any]]:
    """Grava 1 proposta shadow. Retorna a linha (pra digest) ou None."""
    item_id = str(p.get("item_id") or "")
    if not item_id:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO raci_group_proposals
                   (group_message_id, group_jid, empresa_id, empresa_nome, item_id,
                    item_acao, action, new_status, new_prazo, notes, evidencia,
                    confianca, sender_name)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (
                mid, jid, empresa_id, empresa_nome, item_id,
                acoes.get(item_id, ""), p.get("action"), p.get("new_status"),
                p.get("new_prazo"), p.get("notes"), p.get("evidencia"),
                p.get("confianca"), sender,
            ),
        )
        pid = cur.fetchone()
        pid = pid["id"] if isinstance(pid, dict) else pid[0]
    return {
        "id": pid, "empresa": empresa_nome, "sender": sender,
        "item_acao": acoes.get(item_id, item_id[:8]), "action": p.get("action"),
        "new_status": p.get("new_status"), "confianca": p.get("confianca"),
        "evidencia": p.get("evidencia"),
    }


async def _notify_renato(proposals: List[Dict[str, Any]]) -> None:
    """Digest WA pro Renato com as propostas pendentes. Nao aplica nada."""
    from services.intel_bot import send_intel_notification

    lines = [f"🔎 RACI — {len(proposals)} proposta(s) do grupo pra revisar (shadow):", ""]
    for p in proposals[:15]:
        alvo = p.get("new_status") or (p.get("action") or "nota")
        lines.append(
            f"#{p['id']} [{p.get('confianca')}] {p.get('empresa')}: "
            f"{(p.get('item_acao') or '')[:45]} → {alvo}"
        )
        if p.get("evidencia"):
            lines.append(f"   ↳ {(p['evidencia'])[:80]}")
    lines.append("")
    lines.append("Nada foi aplicado. Aprova com os #ids que eu aplico no RACI.")
    await send_intel_notification("\n".join(lines))


def apply_group_proposal(proposal_id: int) -> Dict[str, Any]:
    """Aplica UMA proposta shadow no RACI (apos aprovacao do Renato). Marca a linha."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, empresa_id, item_id, action, new_status, new_prazo, notes,
                      evidencia, confianca, status
                 FROM raci_group_proposals WHERE id = %s""",
            (proposal_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"error": f"proposta #{proposal_id} nao encontrada"}
    if row["status"] == "applied":
        return {"error": f"proposta #{proposal_id} ja aplicada"}

    proposal = {
        "item_id": row["item_id"], "action": row["action"],
        "new_status": row["new_status"], "new_prazo": row["new_prazo"],
        "notes": row["notes"], "evidencia": row["evidencia"], "confianca": row["confianca"],
    }
    try:
        result = apply_proposal(proposal, row["empresa_id"])
    except Exception as e:
        _update_proposal_status(proposal_id, "apply_error", f"{type(e).__name__}: {e}")
        return {"error": f"apply falhou: {type(e).__name__}: {e}", "id": proposal_id}

    if result is None:
        _update_proposal_status(proposal_id, "apply_error", "item nao encontrado no RACI")
        return {"error": "item nao encontrado no RACI", "id": proposal_id}

    _update_proposal_status(proposal_id, "applied", str(result))
    return {"ok": True, "id": proposal_id, "result": result}


def _update_proposal_status(proposal_id: int, status: str, apply_result: str = None) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE raci_group_proposals
                  SET status = %s, reviewed_at = (now() AT TIME ZONE 'UTC'),
                      apply_result = COALESCE(%s, apply_result)
                WHERE id = %s""",
            (status, apply_result, proposal_id),
        )


def list_pending_proposals(limit: int = 50) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, empresa_nome, sender_name, item_acao, action, new_status,
                      confianca, evidencia, criado_em
                 FROM raci_group_proposals
                WHERE status = 'pending_review'
             ORDER BY criado_em DESC LIMIT %s""",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
