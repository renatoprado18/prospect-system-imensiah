#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Quantas horas de WhatsApp faltam recuperar. Auxiliar do `wa_repareia.sh`.

POR QUE É UM ARQUIVO, e não SQL inline no shell (16/10/2026). No
`wa_repareia.sh` esta consulta vivia como `python3 -c "..."` dentro de
`$(...)`, com o SQL em tripla-aspa. O literal `messages.upsert` entre
apóstrofos encostava na tripla-aspa de fechamento, os dois se fundiam, o
Python cortava a string ali e o Postgres recebia aspa não terminada
(`unterminated quoted string`). Com `2>/dev/null` e `|| HORAS=20` em volta, o
cálculo NUNCA rodou e ninguém soube: anunciou 3h numa queda de 16,5h e 20h
numa de 9,7h, as duas vezes imprimindo "medida ANTES de religar". Quatro
níveis de quoting (bash → $() → python -c → SQL) é uma armadilha, não um
estilo: em arquivo, não há nada para escapar.

O QUE MEDE, e por que estes dois filtros. `max(received_at)` dos eventos
**desta instância** (`WHERE instance`) e **só de MENSAGEM**
(`event_type='messages.upsert'`):

- sem o filtro de instância, o `MAX` pega o evento mais novo de qualquer uma —
  o `intel-bot-v2` vivo encurtou um silêncio de 15,8h para 3,8h em 15/10 e
  zerou o alarme ([[feedback_medidor_agregado_mascara_fonte_morta]]);
- sem o filtro de event_type, o próprio RELIGAMENTO fecha o buraco na medida:
  parear grava `connection.update` e `qrcode.updated`, então a janela some no
  instante em que o cano volta, sem que uma única mensagem tenha sido
  recuperada. O medidor não pode se mover junto com o que ele mede.

Imprime `JANELA_HORAS=<n>` — marcador, porque o módulo `database` escreve
`[DB] neondb @ ...` em stdout e uma captura crua misturaria as duas coisas.
Não imprime nada se não houver o que medir: o chamador trata a ausência como
"não medi" e diz isso em voz alta, em vez de inventar um default silencioso.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import psycopg2  # noqa: E402

FOLGA_HORAS = 2

SQL = (
    "SELECT ceil(extract(epoch from (now() - max(received_at))) / 3600.0) + %s "
    "FROM webhook_audit "
    "WHERE instance = %s AND event_type = %s"
)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL ausente", file=sys.stderr)
        return 1

    instancia = (os.environ.get("EVOLUTION_INSTANCE") or "rap-whatsapp").strip()
    conn = psycopg2.connect(url, keepalives=1)
    try:
        cur = conn.cursor()
        cur.execute(SQL, (FOLGA_HORAS, instancia, "messages.upsert"))
        linha = cur.fetchone()
    finally:
        conn.close()

    valor = linha[0] if linha else None
    if valor is None:
        print(
            f"nenhum messages.upsert para a instancia {instancia!r} — "
            "nada a medir",
            file=sys.stderr,
        )
        return 2

    print(f"JANELA_HORAS={int(valor)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
