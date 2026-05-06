"""Telemetria do funil LinkedIn (Fase 0 do roadmap LinkedIn-as-acquisition).

Responsabilidades:
- track_linkdapi_call(endpoint, status_code): registra uso e debita 1 credito
- record_refill(amount, notes): registra compra de creditos
- get_balance(): saldo atual = SUM(credits_delta)
- get_funnel_state(): contagens atuais de cada estagio (topicos, engajadores, etc)
- snapshot_funnel(): persiste estado em linkedin_funnel_metrics e devolve dict
- check_low_balance_alert(threshold=500): WhatsApp pra Renato se saldo cair

Tabelas linkedin_topics e linkedin_engagers ainda nao existem (Fases 1 e 2);
funcoes que dependem delas defaultam pra 0 e logam aviso.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from database import get_db

logger = logging.getLogger(__name__)

LOW_BALANCE_THRESHOLD = int((os.getenv("LINKDAPI_LOW_BALANCE_THRESHOLD") or "500").strip() or 500)
REFILL_HINT_USD = 10
REFILL_HINT_CREDITS = 1200


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table_name,),
    )
    return cursor.fetchone() is not None


def track_linkdapi_call(endpoint: str, status_code: Optional[int] = None, notes: Optional[str] = None) -> None:
    """Registra uma chamada LinkdAPI debitando 1 credito.

    Idempotencia: cada call vira uma row separada — chamadas repetidas debitam
    multiplas vezes propositalmente (cada call cobra 1 credit do provedor).
    Falha em INSERT NAO propaga — telemetria nao deve quebrar o fluxo principal.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO linkdapi_usage (endpoint, status_code, credits_delta, notes)
                VALUES (%s, %s, -1, %s)
                """,
                (endpoint, status_code, notes),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"track_linkdapi_call falhou ({endpoint}): {e}")


def record_refill(amount: int, notes: Optional[str] = None) -> Dict:
    """Registra refill de creditos (positivo). Usado por /api/admin/linkdapi/refill."""
    if amount <= 0:
        raise ValueError("amount precisa ser > 0")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO linkdapi_usage (endpoint, status_code, credits_delta, notes)
            VALUES ('refill', NULL, %s, %s)
            RETURNING id, called_at
            """,
            (amount, notes or f"manual refill +{amount}"),
        )
        row = cursor.fetchone()
        conn.commit()
    return {"id": row["id"], "called_at": row["called_at"].isoformat(), "amount": amount}


def get_balance() -> int:
    """Saldo atual = SUM(credits_delta). Sem rows = 0."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(SUM(credits_delta), 0) AS saldo FROM linkdapi_usage")
        row = cursor.fetchone()
    return int(row["saldo"] or 0)


def get_consumption_last_7d() -> int:
    """Retorna creditos consumidos (valor absoluto) nos ultimos 7 dias.
    Refills (positivos) nao contam — so calls (negativos)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(SUM(ABS(credits_delta)), 0) AS consumo
            FROM linkdapi_usage
            WHERE credits_delta < 0 AND called_at > NOW() - INTERVAL '7 days'
            """
        )
        row = cursor.fetchone()
    return int(row["consumo"] or 0)


def get_funnel_state() -> Dict[str, int]:
    """Estado atual das tabelas do funil. Tabelas das fases futuras
    (linkedin_topics, linkedin_engagers) defaultam pra 0 ate serem criadas."""
    state = {
        "keywords_ativas": 0,
        "topicos_descobertos_30d": 0,
        "engajadores_unicos_30d": 0,
        "contatos_enriquecidos": 0,
        "conversas_iniciadas_30d": 0,
        "demos_originadas_30d": 0,
    }
    with get_db() as conn:
        cursor = conn.cursor()

        if _table_exists(cursor, "linkedin_topics"):
            cursor.execute("SELECT COUNT(DISTINCT keyword) AS k FROM linkedin_topics")
            state["keywords_ativas"] = int((cursor.fetchone() or {}).get("k") or 0)
            cursor.execute(
                """
                SELECT COUNT(*) AS n FROM linkedin_topics
                WHERE descoberto_em > NOW() - INTERVAL '30 days'
                """
            )
            state["topicos_descobertos_30d"] = int((cursor.fetchone() or {}).get("n") or 0)

        if _table_exists(cursor, "linkedin_engagers"):
            cursor.execute(
                """
                SELECT COUNT(DISTINCT urn_pessoa) AS n FROM linkedin_engagers
                WHERE engajou_em > NOW() - INTERVAL '30 days'
                """
            )
            state["engajadores_unicos_30d"] = int((cursor.fetchone() or {}).get("n") or 0)

        # contatos_enriquecidos: lemos da coluna linkedin_dossie (Fase 3 cria a coluna).
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'linkedin_dossie'
            ) AS exists
            """
        )
        if (cursor.fetchone() or {}).get("exists"):
            cursor.execute(
                "SELECT COUNT(*) AS n FROM contacts WHERE linkedin_dossie IS NOT NULL AND linkedin_dossie != ''"
            )
            state["contatos_enriquecidos"] = int((cursor.fetchone() or {}).get("n") or 0)

    return state


def snapshot_funnel() -> Dict:
    """Persiste snapshot atual em linkedin_funnel_metrics. Idempotente por dia
    (re-run no mesmo dia substitui o snapshot mais recente). Retorna o dict salvo."""
    state = get_funnel_state()
    saldo = get_balance()
    consumo_7d = get_consumption_last_7d()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO linkedin_funnel_metrics (
                keywords_ativas, topicos_descobertos, engajadores_unicos,
                contatos_enriquecidos, conversas_iniciadas, demos_originadas,
                creditos_consumidos_periodo, creditos_saldo, details_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id, snapshot_em
            """,
            (
                state["keywords_ativas"],
                state["topicos_descobertos_30d"],
                state["engajadores_unicos_30d"],
                state["contatos_enriquecidos"],
                state["conversas_iniciadas_30d"],
                state["demos_originadas_30d"],
                consumo_7d,
                saldo,
                _serialize_details(state, saldo, consumo_7d),
            ),
        )
        row = cursor.fetchone()
        conn.commit()

    return {
        "id": row["id"],
        "snapshot_em": row["snapshot_em"].isoformat(),
        "saldo": saldo,
        "consumo_7d": consumo_7d,
        **state,
    }


def _serialize_details(state: Dict, saldo: int, consumo_7d: int) -> str:
    import json
    return json.dumps({
        "state": state,
        "saldo": saldo,
        "consumo_7d": consumo_7d,
        "low_balance_threshold": LOW_BALANCE_THRESHOLD,
    })


def get_funnel_history(limit: int = 12) -> list[Dict]:
    """Ultimos N snapshots em ordem cronologica (mais antigo primeiro pra grafico)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, snapshot_em, keywords_ativas, topicos_descobertos,
                   engajadores_unicos, contatos_enriquecidos,
                   conversas_iniciadas, demos_originadas,
                   creditos_consumidos_periodo, creditos_saldo
            FROM linkedin_funnel_metrics
            ORDER BY snapshot_em DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = [dict(r) for r in cursor.fetchall()]
    for r in rows:
        if r.get("snapshot_em"):
            r["snapshot_em"] = r["snapshot_em"].isoformat()
    return list(reversed(rows))


async def check_low_balance_alert(threshold: int = LOW_BALANCE_THRESHOLD) -> Dict:
    """Manda WhatsApp pro Renato se saldo < threshold E nao mandou nas ultimas 72h.

    Idempotente por janela: se ja mandou o alerta nas ultimas 72h, nao manda
    de novo (evita spam quando saldo fica oscilando perto do threshold).
    """
    saldo = get_balance()
    if saldo >= threshold:
        return {"alerted": False, "saldo": saldo, "reason": "above_threshold"}

    # Detecta alerta recente
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM linkdapi_usage
            WHERE endpoint = 'low_balance_alert' AND called_at > NOW() - INTERVAL '72 hours'
            ORDER BY called_at DESC LIMIT 1
            """
        )
        recent = cursor.fetchone()

    if recent:
        return {"alerted": False, "saldo": saldo, "reason": "alerted_recently"}

    # Manda WhatsApp (import lazy pra evitar ciclo + imports caros)
    try:
        from services.intel_bot import send_intel_notification
        msg = (
            f"🔋 LinkdAPI saldo baixo: {saldo} creditos restantes.\n\n"
            f"Refill ${REFILL_HINT_USD} = +{REFILL_HINT_CREDITS} creditos em "
            f"https://linkdapi.com/dashboard\n\n"
            f"(threshold {threshold} — alerta 1x/72h)"
        )
        sent = await send_intel_notification(msg)
    except Exception as e:
        logger.warning(f"check_low_balance_alert: send failed: {e}")
        sent = False

    # Marca alerta no ledger (delta=0, nao afeta saldo)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO linkdapi_usage (endpoint, status_code, credits_delta, notes)
                VALUES ('low_balance_alert', %s, 0, %s)
                """,
                (200 if sent else 500, f"saldo={saldo} threshold={threshold}"),
            )
            conn.commit()
    except Exception:
        logger.exception("check_low_balance_alert: nao conseguiu registrar alerta no ledger")

    return {"alerted": sent, "saldo": saldo, "threshold": threshold}
