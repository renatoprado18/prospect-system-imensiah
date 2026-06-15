"""
Detectores deterministas — Fase 1 da rebuild Tonha (16/06/26).

Cada detector eh codigo puro Python+SQL (zero LLM). Le DB, calcula, escreve em
signals via INSERT ... ON CONFLICT (signal_hash) DO UPDATE.

Tonha (Sonnet 4.6) le signals e decide.

Ver docs/ARCHITECTURE_REBUILD.md.
"""
from services.detectors._base import (
    DetectorRun,
    emit_signal,
    expire_stale_signals,
    run_all_detectors,
)

__all__ = ["DetectorRun", "emit_signal", "expire_stale_signals", "run_all_detectors"]
