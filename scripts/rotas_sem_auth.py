#!/usr/bin/env python3
"""
Mapeia rotas mutantes sem auth e QUEM as consome.

POR QUE EXISTE. Em 03/08 fechei 129 rotas usando uma heuristica de "sem
consumidor" que removia `{param}` do path e procurava a string resultante no
repo. Isso gera FALSO NEGATIVO para path com parametro NO MEIO: a rota
`/api/editorial/{post_id}/publish-linkedin` virava `/api/editorial//publish-
linkedin`, que nunca casa com `fetch(`/api/editorial/${postId}/publish-
linkedin`)` do template. Resultado: 12+ rotas foram fechadas como se ninguem as
usasse, e o frontend as usa.

Elas nao quebraram (o cookie de sessao vai junto), mas o erro escondia outro
maior — foi ao investigar essas 12 que apareceu o problema de PAPEL: fechar com
`require_scaffold_auth` (admin) tirava a Andressa, que e operador.

A CORRECAO. Comparar por SEGMENTOS LITERAIS na mesma linha, ignorando os
parametros. `/api/editorial/{post_id}/publish-linkedin` -> exige que "api",
"editorial" e "publish-linkedin" aparecam juntos numa linha que mencione /api/.
Tolera `${x}`, `{x}`, `%s`, concatenacao — qualquer forma de interpolar.

Uso:  ./rotas_sem_auth.py                 # resumo por bloco
      ./rotas_sem_auth.py --bloco /api/editorial   # detalhe de um bloco
      ./rotas_sem_auth.py --sem-consumidor         # so as seguras de fechar
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tests"))

FONTES = ("app/templates", "app/static", "workers", "mcp", "app/services", "scripts")
EXTS = ("html", "js", "py", "json", "ts")


def _corpus() -> dict[str, list[str]]:
    """arquivo -> linhas. Lido UMA vez; a versao anterior fazia um grep por
    rota (193 subprocessos) e estourava o timeout."""
    out = {}
    for raiz in FONTES:
        caminho = os.path.join(_ROOT, raiz)
        for dp, _, fs in os.walk(caminho):
            for f in fs:
                if f.rsplit(".", 1)[-1] not in EXTS:
                    continue
                p = os.path.join(dp, f)
                try:
                    out[os.path.relpath(p, _ROOT)] = open(
                        p, encoding="utf-8", errors="ignore").read().split("\n")
                except Exception:
                    pass
    return out


def consumidores(path: str, corpus: dict, ignorar: str = "") -> list[tuple[str, str]]:
    """Onde a rota e chamada. Casa por segmentos literais na MESMA linha."""
    segs = [s for s in path.split("/") if s and not s.startswith("{")]
    if len(segs) < 2:
        return []                      # generico demais pra afirmar qualquer coisa
    achados = []
    for arq, linhas in corpus.items():
        if ignorar and arq.startswith(ignorar):
            continue
        for ln in linhas:
            if "/api/" not in ln:
                continue
            if all(s in ln for s in segs):
                achados.append((arq, ln.strip()[:76]))
                break
    return achados


def levantar():
    from test_endpoint_auth_exposure import _rotas_de_main
    corpus = _corpus()
    saida = []
    for _, m, p, tem_auth, fn in _rotas_de_main():
        if m not in ("POST", "PUT", "PATCH", "DELETE") or tem_auth:
            continue
        saida.append((m, p, fn, consumidores(p, corpus)))
    return saida


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bloco", help="detalhar um prefixo, ex: /api/editorial")
    ap.add_argument("--sem-consumidor", action="store_true",
                    help="listar so as que ninguem chama (seguras de fechar)")
    a = ap.parse_args()

    rotas = levantar()
    if a.sem_consumidor:
        alvo = [r for r in rotas if not r[3]]
        print(f"rotas mutantes sem auth E sem consumidor: {len(alvo)}")
        for m, p, fn, _ in alvo:
            print(f"  {m:<7}{p:<52}{fn}")
        return 0

    if a.bloco:
        alvo = [r for r in rotas if r[1].startswith(a.bloco)]
        print(f"{a.bloco}: {len(alvo)} rotas mutantes sem auth\n")
        for m, p, fn, cons in alvo:
            marca = f"{len(cons)} consumidor(es)" if cons else "SEM consumidor"
            print(f"  {m:<7}{p:<50}{marca}")
            for arq, linha in cons[:2]:
                print(f"          {os.path.basename(arq)}: {linha}")
        return 0

    print(f"ROTAS MUTANTES SEM AUTH: {len(rotas)}")
    com = sum(1 for r in rotas if r[3])
    print(f"  com consumidor: {com} · sem consumidor: {len(rotas) - com}\n")
    por_bloco = defaultdict(lambda: [0, 0])
    for m, p, fn, cons in rotas:
        b = "/".join(p.split("/")[:3])
        por_bloco[b][0 if cons else 1] += 1
    print(f"  {'bloco':<30}{'c/ consumidor':>14}{'s/ consumidor':>15}")
    for b, (c, s) in sorted(por_bloco.items(), key=lambda x: -(x[1][0] + x[1][1])):
        print(f"  {b:<30}{c:>14}{s:>15}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
