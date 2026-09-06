#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Fecha signals cujo portao virou ATO — e o backfill do que ficou para tras.

O fechador roda sozinho dentro de `alertar_portoes` daqui pra frente. Este
script existe para dois usos que o ciclo diario nao cobre:

  1. VER antes de escrever (`--dry-run`, o padrao) — quantos, quais, com que
     evidencia.
  2. BACKFILL (`--incluir-expirados`) — os signals que ja foram carimbados
     `detector_expired` antes de este mecanismo existir. Sao 45 dias de historico
     em que o portao foi cumprido e ficou registrado como "sumiu". Reclassificar
     e' reescrever historico, entao esta atras de flag explicita e nunca sai no
     caminho automatico.

Uso:
  ./fecha_signals_por_ato.py                                   # dry-run, local
  DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 ./fecha_signals_por_ato.py
  DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 ./fecha_signals_por_ato.py --aplicar
  ... --incluir-expirados --aplicar        # o backfill do historico
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from database import get_connection  # noqa: E402
from services.signal_ato import fechar_signals_por_ato  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true",
                    help="escreve de fato (sem isto, e dry-run)")
    ap.add_argument("--incluir-expirados", action="store_true",
                    help="tambem reclassifica os ja marcados detector_expired")
    ap.add_argument("--detector", default="frente_review.alertar_portoes",
                    help="'' para todos os detectores")
    ap.add_argument("--dias", type=int, default=45)
    a = ap.parse_args()

    conn = get_connection()
    r = fechar_signals_por_ato(
        conn,
        detector=a.detector or None,
        dias=a.dias,
        dry_run=not a.aplicar,
        incluir_expirados=a.incluir_expirados,
    )

    modo = "APLICANDO" if a.aplicar else "dry-run"
    print(f"\n╔═ FECHAMENTO POR ATO ({modo}) ═╗")
    print(f"  signals examinados ....... {r['signals_examinados']:5}")
    print(f"  citacoes de task ......... {r['citacoes']:5}")
    print(f"  FECHADOS por ato ......... {r['fechados']:5}")
    if r["descartados_por_agente"]:
        print(f"  descartados (eco da camada) {r['descartados_por_agente']:5}")

    if r["evidencias"]:
        print("\n  ── a evidencia de cada um ──")
        for e in r["evidencias"]:
            print(f"   signal {e['signal_id']} [{e['status_anterior']}] "
                  f"-> task #{e['task_id']} em {e['horas']}h")
            print(f"      {e['task_titulo']}")

    if not a.aplicar and r["fechados"]:
        print("\n  Nada foi escrito. Para aplicar: --aplicar")
    if a.aplicar:
        conn.commit()
        print("\n  ✅ commit feito.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
