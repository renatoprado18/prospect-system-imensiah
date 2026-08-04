#!/usr/bin/env python3
"""Placar de qualidade da camada CoS — gera a página e grava o veredito.

POR QUE ESTE ARQUIVO EXISTE. Em 04/08/2026 o 1º placar foi montado à mão numa
sessão: extrair portões, cruzar movimento, escrever HTML, colar o resultado,
gravar. Deu certo e mostrou algo importante (precisão 70%→33% depois de uma
calibração decidida por volume). Mas um ritual que só acontece quando alguém o
reconstrói do zero não vira série temporal — e sem série, o PDCA volta a
otimizar só o que é fácil medir.

O CICLO:
    ./placar.py --gerar          # escreve ~/cockpit/placar.html e abre
    (o Renato marca e clica "Copiar resultado")
    ./placar.py --gravar arquivo # ou: pbpaste | ./placar.py --gravar -

Só mostra portão AINDA NÃO julgado: reabrir a página não pede de novo o que já
foi respondido, que é o que faz um ritual semanal ser tolerável.

POR QUE NÃO POSTA DIRETO NO BANCO. A página roda em file:// e o POST cross-origin
seria bloqueado; abrir CORS num endpoint de escrita pra resolver ergonomia de um
clique é troca ruim. O copia-e-cola custa 3 segundos e não abre superfície.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import _conn, DEBOUNCE_MIN, TETO_DIARIO, MAX_POR_RODADA  # noqa: E402

DESTINO = os.path.expanduser("~/cockpit/placar.html")
ROTULOS = {"CERTA": "certa", "COBROU A TOA": "errada", "DEIXOU PASSAR": "passou"}


def _rw_url() -> str:
    """URL de ESCRITA. A credencial do agente é read-only de propósito
    (project_camada_agente_local) — gravar veredito é escrita e usa a do app."""
    for linha in open("/Users/rap/prospect-system/.env"):
        if linha.startswith("DATABASE_URL="):
            return linha.split("=", 1)[1].strip().strip('"')
    sys.exit("DATABASE_URL ausente")


# ----------------------------------------------------------------- coletar --

def portoes_pendentes(desde: str, so_pendentes: bool = True) -> list[dict]:
    """Portões da janela. `so_pendentes=False` traz também os já julgados.

    A página só mostra pendente (reabrir não pede de novo o que já foi
    respondido). Mas a GRAVAÇÃO tem que enxergar todos: senão corrigir um
    veredito seria impossível — o portão já não estaria na lista, e o
    `ON CONFLICT DO UPDATE` da tabela nunca seria alcançado.
    """
    url = (os.getenv("COS_RO_URL") or "").strip() or _rw_url()
    with _conn(url) as c, c.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (run_date) run_date, payload
            FROM cos_daily_review WHERE run_date >= %s
            ORDER BY run_date, id DESC
        """, (desde,))
        dias = cur.fetchall()

        ja = set()
        if so_pendentes:
            cur.execute("SELECT run_date, frente, portao FROM cos_portao_veredito")
            ja = {(str(r["run_date"]), r["frente"], r["portao"]) for r in cur.fetchall()}

        saida = []
        for d in dias:
            p = d["payload"] if isinstance(d["payload"], dict) else json.loads(d["payload"])
            for item in (p.get("placar") or {}).get("hoje") or []:
                chave = (str(d["run_date"]), item.get("frente") or "?", item.get("o_que") or "")
                if chave in ja:
                    continue
                pid = item.get("project_id")
                lat = None
                if pid:
                    cur.execute("""
                        SELECT MIN(data_conclusao)::date - %s::date AS d FROM tasks
                        WHERE project_id = %s AND data_conclusao >= %s
                    """, (d["run_date"], pid, d["run_date"]))
                    a = cur.fetchone()["d"]
                    cur.execute("""
                        SELECT MIN(m.enviado_em)::date - %s::date AS d FROM messages m
                        WHERE m.direcao = 'outgoing' AND m.enviado_em >= %s
                          AND m.contact_id IN (
                            SELECT contact_id FROM tasks WHERE project_id = %s AND contact_id IS NOT NULL
                            UNION
                            SELECT contact_id FROM project_members WHERE project_id = %s AND contact_id IS NOT NULL)
                    """, (d["run_date"], d["run_date"], pid, pid))
                    b = cur.fetchone()["d"]
                    cands = [x for x in (a, b) if x is not None]
                    lat = min(cands) if cands else None
                saida.append({"dia": str(d["run_date"]), "frente": chave[1],
                              "portao": chave[2], "project_id": pid, "latencia": lat,
                              "motor": p.get("motor") or "api"})
        return saida


# ------------------------------------------------------------------ gravar --

def gravar(texto: str) -> int:
    """Lê o texto do botão 'Copiar resultado' e persiste.

    Formato por linha:  DD/MM · Frente — VEREDITO
    Casa a frente contra os portões do dia; sem casamento exato, avisa em vez de
    gravar errado — veredito no portão errado é pior que veredito faltando.
    """
    linhas = [l.strip() for l in texto.splitlines() if "·" in l and "—" in l]
    if not linhas:
        sys.exit("nada reconhecido — esperado 'DD/MM · Frente — VEREDITO'")

    # TODOS, não só pendentes — corrigir um veredito é caso de uso legítimo.
    pend = {(p["dia"], p["frente"]): p
            for p in portoes_pendentes("2026-07-30", so_pendentes=False)}
    hoje = date.today()
    conn = __import__("psycopg2").connect(_rw_url())
    cur = conn.cursor()
    ok = falhou = 0
    for linha in linhas:
        m = re.match(r"(\d{2})/(\d{2})\s*·\s*(.+?)\s*—\s*([A-ZÀ-Ú ]+)\s*$", linha)
        if not m:
            print(f"  ? não parseou: {linha[:60]}"); falhou += 1; continue
        dd, mm, frente, rot = m.groups()
        veredito = ROTULOS.get(rot.strip())
        if not veredito:
            print(f"  ? veredito desconhecido '{rot}'"); falhou += 1; continue
        dia = f"{hoje.year}-{mm}-{dd}"
        p = pend.get((dia, frente.strip()))
        if not p:
            print(f"  ? sem portão correspondente: {dia} · {frente[:40]}"); falhou += 1; continue
        cur.execute("""
            INSERT INTO cos_portao_veredito
              (run_date, project_id, frente, portao, veredito, latencia_dias,
               debounce_min, teto_diario, max_por_rodada, motor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (run_date, frente, portao) DO UPDATE SET veredito = EXCLUDED.veredito
        """, (p["dia"], p["project_id"], p["frente"], p["portao"], veredito,
              p["latencia"], DEBOUNCE_MIN, TETO_DIARIO, MAX_POR_RODADA, p["motor"]))
        ok += 1
    conn.commit()
    print(f"\n{ok} gravados · {falhou} não reconhecidos")
    if ok:
        print("Rode `./pdca.py` — o bloco 5b já lê estes números.")
    return 0 if not falhou else 1


# ------------------------------------------------------------------- gerar --

def gerar(pend: list[dict]) -> int:
    if not pend:
        print("Nenhum portão pendente de veredito — placar em dia.")
        return 0
    from placar_html import render          # separado: HTML é template, não lógica
    open(DESTINO, "w").write(render(pend))
    print(f"{len(pend)} portões pendentes → {DESTINO}")
    subprocess.run(["open", DESTINO], check=False)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gerar", action="store_true")
    ap.add_argument("--gravar", metavar="ARQUIVO", help="'-' lê do stdin")
    ap.add_argument("--desde", default="2026-07-30")
    a = ap.parse_args()
    if a.gravar:
        texto = sys.stdin.read() if a.gravar == "-" else open(a.gravar).read()
        return gravar(texto)
    if a.gerar:
        return gerar(portoes_pendentes(a.desde))
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
