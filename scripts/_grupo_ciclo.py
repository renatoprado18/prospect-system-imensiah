#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Roda o polling de grupo até SECAR, e grita quando uma passada falha.

POR QUE EXISTE (16/10/2026). No `wa_repareia.sh` este ciclo era um `for` em
bash com o parser dentro de `python3 -c '...'` aninhado em `$(...)`, e tinha
dois defeitos que se somavam:

1. **Falha calada.** Nenhuma checagem de HTTP; o parser lia os campos com
   `.get()` e, quando a resposta não tinha o shape esperado, imprimia
   `offset None -> None | grupos None | salvas None` e o loop seguia. Na
   execução de 16/10, **3 das 7 passadas saíram assim** e ninguém soube que
   eram falhas: pareciam passadas vazias.
2. **Parada cedo.** A condição era `next_offset == 0 && i > 1`. Como as
   passadas falhas não avançam o cursor, o ciclo "fechou" deixando grupos sem
   varrer: o script relatou 19 mensagens recuperadas e uma rodada manual
   posterior achou **mais 16** nos grupos que ficaram de fora — 19 contra uma
   média noturna de 30,8 parecia só uma noite fraca.

O critério certo é SECAR, não "o cursor voltou a zero": o endpoint é paginado
e cobre ~9-13 dos 49 grupos por passada, então o ciclo só terminou quando duas
passadas consecutivas trouxerem zero mensagem nova — e desde que nenhuma
passada tenha falhado no caminho ([[feedback_guarda_abstencao_vira_fabrica]]).

E o ciclo veio pra Python inteiro em vez de só o parser: quatro níveis de
quoting (bash → $() → python -c → JSON) foi o que escondeu o erro de aspas do
cálculo da janela, no mesmo arquivo e no mesmo dia. Aqui não há nada a escapar.

Saída: uma linha por passada, e um veredito final que distingue
`SECOU` de `TETO` de `FALHAS`. Exit != 0 quando algo ficou sem varrer, pra que
quem chama não leia "terminou" como "cobriu".
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = "https://intel.almeida-prado.com/api/cron/run-group-messages"
PASSADAS_MAX = 12
SECAS_PARA_PARAR = 2


def _segredo() -> str:
    s = (os.environ.get("CRON_SECRET") or "").strip()
    if s:
        return s
    caminho = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    try:
        with open(caminho, encoding="utf-8") as fh:
            for linha in fh:
                if linha.startswith("CRON_SECRET="):
                    return linha.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _uma_passada(segredo: str):
    """Devolve (ok, descricao, salvas). `ok=False` é FALHA, não passada vazia."""
    req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {segredo}"})
    try:
        with urllib.request.urlopen(req, timeout=580) as resp:
            codigo = resp.status
            corpo = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:140].decode('utf-8', 'replace')}", 0
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:140]}", 0

    if codigo != 200:
        return False, f"HTTP {codigo}: {corpo[:140]}", 0

    try:
        d = json.loads(corpo)
    except ValueError:
        return False, f"resposta não-JSON: {corpo[:140]}", 0

    r = d.get("result")
    if not isinstance(r, dict) or "messages_saved" not in r:
        # ⚠️ ESTE é o caso que antes virava `None` e passava por vazio.
        return False, f"shape inesperado: {corpo[:140]}", 0

    salvas = r.get("messages_saved") or 0
    desc = (
        f"offset {d.get('offset_used')}→{d.get('next_offset')} · "
        f"grupos {r.get('groups_synced')} · salvas {salvas}"
    )
    return True, desc, int(salvas)


def main() -> int:
    segredo = _segredo()
    if not segredo:
        print("🔴 CRON_SECRET ausente — não rodei nada", file=sys.stderr)
        return 1

    total = 0
    secas = 0
    falhas = []

    for i in range(1, PASSADAS_MAX + 1):
        ok, desc, salvas = _uma_passada(segredo)
        if not ok:
            falhas.append((i, desc))
            # Falha NÃO conta como seca: o cursor não avançou e aqueles grupos
            # seguem sem varrer. Tratá-la como vazia é o defeito de origem.
            secas = 0
            print(f"   [{i}] 🔴 FALHOU — {desc}")
            continue

        total += salvas
        print(f"   [{i}] {desc} · acumulado {total}")
        secas = secas + 1 if salvas == 0 else 0
        if secas >= SECAS_PARA_PARAR:
            print(f"\n   ✅ SECOU — {SECAS_PARA_PARAR} passadas seguidas sem nada novo."
                  f" Total recuperado: {total}")
            break
    else:
        print(f"\n   ⚠️ TETO de {PASSADAS_MAX} passadas sem secar. Total: {total}.")
        print("      Pode haver mais a recuperar — rode de novo.")
        if not falhas:
            return 3

    if falhas:
        print(f"\n   🔴 {len(falhas)} passada(s) FALHARAM — os grupos daquelas páginas")
        print("      NÃO foram varridos, e isto não é 'nada novo':")
        for i, d in falhas:
            print(f"        [{i}] {d}")
        print("      ⏭️ Rode o ciclo outra vez antes de dar a janela por coberta.")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
