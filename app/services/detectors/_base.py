"""
Detectores deterministas — utilitarios base.

Cada detector implementa run(conn) -> DetectorRun, recebe conexao Postgres,
calcula signals, e retorna stats. Helper emit_signal faz INSERT ... ON CONFLICT
DO UPDATE pra garantir idempotencia.

Schema de signals em scripts/migrations/026_signals.sql.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from database import get_db
from services.tz import now_utc

logger = logging.getLogger(__name__)


@dataclass
class DetectorRun:
    """Stats de uma execucao de detector."""
    detector: str
    emitted: int = 0
    updated: int = 0
    skipped: int = 0
    expired: int = 0
    duration_ms: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detector": self.detector,
            "emitted": self.emitted,
            "updated": self.updated,
            "skipped": self.skipped,
            "expired": self.expired,
            "duration_ms": self.duration_ms,
            "errors": self.errors[:5],
        }


@contextmanager
def savepoint(conn, label: str):
    """Rollback fino — falha de um bloco nao polui txn dos outros.

    Uso:
        try:
            with savepoint(conn, "duplicados"):
                cur.execute(...)
                ...
        except Exception as e:
            res.errors.append(...)
    """
    safe = "".join(c if c.isalnum() else "_" for c in label)[:40]
    cur = conn.cursor()
    cur.execute(f"SAVEPOINT sp_{safe}")
    try:
        yield
        cur.execute(f"RELEASE SAVEPOINT sp_{safe}")
    except Exception:
        cur.execute(f"ROLLBACK TO SAVEPOINT sp_{safe}")
        raise


def make_signal_hash(tipo: str, *key_parts: Any) -> str:
    """Gera hash determinístico pra dedup.

    Ex: make_signal_hash('raci_vencido', empresa_id, raci_id) gera mesma
    hash sempre pra mesmo (tipo, empresa, raci). INSERT ON CONFLICT vira UPDATE.
    """
    raw = f"{tipo}::" + "::".join(str(p) for p in key_parts if p is not None)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def emit_signal(
    conn,
    *,
    tipo: str,
    signal_hash: str,
    urgencia: int,
    contexto: Dict[str, Any],
    detector: str,
) -> str:
    """Insere ou atualiza signal. Retorna 'emitted' (novo) ou 'updated' (existente).

    Idempotente via signal_hash UNIQUE. Se ja existe e ainda 'open', atualiza
    contexto + urgencia (caso urgencia mudou). Se ja 'resolved'/'dismissed',
    NAO reabre (evita ruido).
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO signals (signal_hash, tipo, urgencia, contexto, detector, status)
        VALUES (%s, %s, %s, %s::jsonb, %s, 'open')
        ON CONFLICT (signal_hash) DO UPDATE
        SET
            urgencia = EXCLUDED.urgencia,
            contexto = EXCLUDED.contexto,
            atualizado_em = NOW()
        WHERE signals.status = 'open'
        RETURNING (xmax = 0) AS inserted
        """,
        (signal_hash, tipo, urgencia, json.dumps(contexto, default=str), detector),
    )
    row = cur.fetchone()
    if row is None:
        # Conflito em row resolved/dismissed — nao reabre
        return "skipped"
    return "emitted" if row["inserted"] else "updated"


def expire_stale_signals(conn, *, detector: str, current_hashes: List[str], reason: str = "no_longer_present") -> int:
    """Marca como 'expired' signals deste detector que NAO apareceram nesta run.

    Ex: detector_editorial roda e gera signals pra 5 hot takes velhos. Se na
    proxima run so 3 ainda existem (2 foram aproveitados ou dismissed), os
    outros 2 viram expired automatico.
    """
    if not current_hashes:
        return 0
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE signals
        SET status = 'expired',
            resolved_at = NOW(),
            resolved_by = 'detector_expired'
        WHERE detector = %s
          AND status = 'open'
          AND signal_hash != ALL(%s)
        RETURNING id
        """,
        (detector, current_hashes),
    )
    return cur.rowcount


# TTL por tipo de signal EFEMERO (horas). Estes signals sao emitidos por
# emit_signal AVULSO (A6/A7 do porta-voz unico: digests, pre/post-meeting,
# escalations, linkedin) — NAO passam por nenhum detector recorrente que os
# expire via expire_stale_signals. Sem TTL ficam 'open' pra sempre: incham a
# tabela e vazam pra qualquer leitura de status='open' sem exclude_seen.
# A Tonia ja nao os re-mostra (dedup tonia_seen_signals), mas o registro fica
# sujo. Este sweep marca 'expired' por idade. Ver [[reference_porta_voz_signal_routing]].
EPHEMERAL_SIGNAL_TTL_HOURS: Dict[str, int] = {
    "pre_meeting_briefing": 24,        # dossie just-in-time; reuniao ja passou
    "post_meeting_c1": 72,             # follow-up de reuniao; 3 dias
    "agent_intent_blocked": 120,       # 5 dias travado = stale
    "weekly_digest": 168,              # 7 dias (proximo digest ja veio)
    "editorial_weekly_digest": 168,    # idem
    "editorial_monthly_digest": 720,   # 30 dias
    "linkedin_author_reply": 168,      # 7 dias
    "linkedin_engagement_quarantine": 168,
    # cruzamento_noticia_contato TEM detector recorrente (expira via
    # expire_stale_signals quando some da run), MAS expire_stale bail-a quando
    # current_hashes vem vazio (todas as manchetes suprimidas). Sem este TTL,
    # um cruzamento antigo fica 'open' pra sempre quando o detector zera. 7d.
    "cruzamento_noticia_contato": 168,
}


def expire_aged_signals(conn) -> int:
    """Marca 'expired' signals efemeros abertos que passaram do TTL por tipo.

    Complementa expire_stale_signals (que so cobre signals de detector,
    expirados por ausencia na run seguinte). Signals de emit_signal avulso nao
    tem run recorrente — expiram por IDADE aqui. Idempotente (so toca 'open').
    Retorna quantas linhas expiraram.
    """
    cur = conn.cursor()
    total = 0
    for tipo, ttl_h in EPHEMERAL_SIGNAL_TTL_HOURS.items():
        cur.execute(
            """
            UPDATE signals
            SET status = 'expired',
                resolved_at = NOW(),
                resolved_by = 'ttl_expired'
            WHERE tipo = %s
              AND status = 'open'
              AND criado_em < NOW() - make_interval(hours => %s)
            """,
            (tipo, ttl_h),
        )
        total += cur.rowcount
    return total


def run_all_detectors(only: Optional[List[str]] = None) -> Dict[str, Any]:
    """Executa todos os detectores em sequencia. Retorna stats agregadas.

    Param `only`: se passar lista de nomes, roda so esses (util pra debug/teste).
    """
    from services.detectors import (
        detector_conselhos,
        detector_editorial,
        detector_relacionamento,
        detector_operational,
        detector_financial,
        detector_governanca_pessoal,
        detector_delegacoes,
        detector_inbox,
        detector_cruzamentos,
    )

    all_detectors = [
        ("detector_conselhos", detector_conselhos.run),
        ("detector_editorial", detector_editorial.run),
        ("detector_relacionamento", detector_relacionamento.run),
        ("detector_operational", detector_operational.run),
        ("detector_financial", detector_financial.run),
        ("detector_governanca_pessoal", detector_governanca_pessoal.run),
        ("detector_delegacoes", detector_delegacoes.run),
        ("detector_inbox", detector_inbox.run),
        ("detector_cruzamentos", detector_cruzamentos.run),
    ]

    if only:
        all_detectors = [(n, f) for n, f in all_detectors if n in only]

    started = time.time()
    results: List[Dict[str, Any]] = []
    total = {"emitted": 0, "updated": 0, "skipped": 0, "expired": 0}
    aged_expired = 0

    with get_db() as conn:
        for name, fn in all_detectors:
            t0 = time.time()
            try:
                run = fn(conn)
                run.duration_ms = int((time.time() - t0) * 1000)
                conn.commit()
                results.append(run.to_dict())
                total["emitted"] += run.emitted
                total["updated"] += run.updated
                total["skipped"] += run.skipped
                total["expired"] += run.expired
            except Exception as e:
                conn.rollback()
                logger.exception(f"detector {name} crashed")
                results.append({
                    "detector": name,
                    "duration_ms": int((time.time() - t0) * 1000),
                    "errors": [str(e)[:300]],
                })

        # TTL de signals efemeros (emit_signal avulso, A6/A7) — sweep proprio,
        # falha isolada nao polui os detectores acima.
        try:
            aged_expired = expire_aged_signals(conn)
            conn.commit()
            total["expired"] += aged_expired
        except Exception as e:
            conn.rollback()
            logger.exception("expire_aged_signals crashed")

    return {
        "ok": True,
        "total_duration_ms": int((time.time() - started) * 1000),
        "detectors_run": len(results),
        "totals": total,
        "aged_expired": aged_expired,
        "details": results,
    }
