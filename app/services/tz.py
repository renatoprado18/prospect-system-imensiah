"""Timezone helpers — convencao do INTEL.

Regra unica: tudo armazenado e manipulado em UTC tz-aware. Conversao pra
BRT (America/Sao_Paulo) acontece apenas na borda de apresentacao
(template Jinja, JSON pra UI). Nunca usar datetime.utcnow() (deprecated
em Python 3.12, retorna naive) nem datetime.now() sem tz.

Uso tipico:
    from services.tz import now_utc, to_brt, format_brt, BRT, UTC
    ts = now_utc()                  # tz-aware UTC
    ts_brt = to_brt(ts)             # tz-aware BRT (pra display)
    label = format_brt(ts, "%H:%M") # string pronta pra UI

Em templates: `{{ ts|brt }}` ou `{{ ts|brt("%d/%m %H:%M") }}`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

UTC = timezone.utc
BRT = ZoneInfo("America/Sao_Paulo")

DEFAULT_FMT = "%d/%m/%Y %H:%M"


def now_utc() -> datetime:
    """Datetime tz-aware no UTC. Sempre use isto no lugar de datetime.utcnow()."""
    return datetime.now(UTC)


def now_brt() -> datetime:
    """Datetime tz-aware em BRT. Uso raro — prefira now_utc() e converta na borda."""
    return datetime.now(BRT)


def _ensure_aware(dt: datetime) -> datetime:
    """Garante que o datetime seja tz-aware. Naive vira UTC (assumindo
    que o storage padrao do INTEL e UTC). Necessario porque colunas
    TIMESTAMP (sem TZ) no Postgres voltam naive em psycopg2."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def to_brt(dt: Optional[datetime]) -> Optional[datetime]:
    """Converte qualquer datetime pra BRT tz-aware. Naive e tratado como UTC."""
    if dt is None:
        return None
    return _ensure_aware(dt).astimezone(BRT)


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Converte qualquer datetime pra UTC tz-aware. Naive e tratado como UTC."""
    if dt is None:
        return None
    return _ensure_aware(dt).astimezone(UTC)


def format_brt(dt: Optional[datetime], fmt: str = DEFAULT_FMT) -> str:
    """Formata datetime em BRT pra string. Retorna '' se None."""
    if dt is None:
        return ""
    return to_brt(dt).strftime(fmt)


def parse_iso(s: Union[str, None]) -> Optional[datetime]:
    """Parse ISO-8601 retornando tz-aware. String sem TZ assumida UTC.
    Aceita 'Z' como sufixo (Python <3.11 nao aceita nativo)."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return _ensure_aware(dt)


def register_jinja_filters(env) -> None:
    """Registra filtros nos templates Jinja2:
    - {{ ts|brt }}          -> '07/05/2026 21:14'
    - {{ ts|brt("%H:%M") }} -> '21:14'
    - {{ ts|iso }}          -> ISO-8601 UTC com 'Z' (pra atributos de <time> etc)
    """
    env.filters["brt"] = lambda dt, fmt=DEFAULT_FMT: format_brt(dt, fmt)
    env.filters["iso"] = lambda dt: (to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else "")
