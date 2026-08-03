#!/usr/bin/env python3
"""
Verificador de conformidade do modelo de dados.

POR QUE EXISTE. Em 03/08/26 fechamos um contrato de modelo — 10 entidades, 3
grupos, 7 decisoes — e a pergunta imediata foi: como isso nao vira mais um
documento que ninguem le e que a realidade contradiz em silencio? Este script e
a resposta: ele compara o BANCO REAL com o que o contrato declara, e a sessao
Dev o roda na abertura.

O QUE ELE NAO FAZ: nao conserta nada, nao escreve no banco, nao alerta por
WhatsApp. Divergencia de arquitetura nao e urgencia — e assunto de sessao. Foi
decisao explicita do Renato (03/08) que isto apareca na abertura do `/dev`, e
nao como notificacao: WhatsApp pra isso vira ruido.

A DECISAO DE DESENHO QUE IMPORTA. O baseline e lido do PROPRIO MEMO, nunca de
constante aqui. Assim, mudar o contrato e o ato que reconfigura a vigilancia —
em vez de criar duas verdades que divergem, que e exatamente a classe de defeito
que este contrato nasceu pra evitar (o `estado_cos` guardado em dois lugares
divergiu no mesmo dia).

Se o memo mudar de formato e o parse falhar, o script FALHA ALTO. Um verificador
que se cala quando nao entende e pior que nenhum: ele passa a certificar
conformidade que nunca checou.

Uso:  ./verifica_modelo.py            # relatorio
      ./verifica_modelo.py --quiet    # so divergencias (pra abertura de sessao)
"""
from __future__ import annotations

import argparse
import os
import re
import sys

MEMO = os.path.expanduser(
    "~/.claude/projects/-Users-rap-prospect-system/memory/project_modelo_rede_entidades.md"
)
VERDE, AMARELO, VERMELHO = "🟢", "🟡", "🔴"


def _conn():
    import psycopg2
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    url = None
    for linha in open(env, encoding="utf-8"):
        if linha.strip().startswith("DATABASE_URL="):
            url = linha.strip().split("=", 1)[1].strip().strip('"').strip("'")
            break
    if not url:
        sys.exit("[verifica-modelo] DATABASE_URL ausente no .env")
    return psycopg2.connect(url)


def ler_contrato() -> dict:
    """Extrai o baseline do memo. Falha alto se o formato mudou."""
    if not os.path.exists(MEMO):
        sys.exit(f"[verifica-modelo] memo nao encontrado: {MEMO}")
    texto = open(MEMO, encoding="utf-8").read()

    # Linhas da tabela de baseline: | Entidade | **N** | tab1 · tab2 · ... |
    entidades = {}
    for m in re.finditer(r"^\|\s*([A-Za-zÀ-ÿ ]+?)\s*\|\s*\*\*(\d+)\*\*\s*\|\s*(.+?)\s*\|$",
                         texto, re.MULTILINE):
        nome, n, tabs = m.group(1).strip(), int(m.group(2)), m.group(3)
        # remove anotacoes entre parenteses ("(3 vazias)") antes de separar
        tabs = re.sub(r"\(.*?\)", "", tabs)
        lista = [t.strip().strip("`") for t in tabs.split("·") if t.strip()]
        entidades[nome] = {"esperado": n, "tabelas": lista}

    total = re.search(r"Total de tabelas no banco:\s*(\d+)", texto)
    mortas = dict(re.findall(r"`([a-z_]+)`\s*\(([\d.]+)[,)]", texto[texto.find("Eras mortas"):])
                  ) if "Eras mortas" in texto else {}

    if not entidades or not total:
        sys.exit("[verifica-modelo] o memo mudou de formato e o baseline nao pôde ser lido. "
                 "Corrija o parse ou o memo — NAO ignore: sem baseline nao ha verificacao.")
    return {
        "entidades": entidades,
        "total": int(total.group(1)),
        "mortas": {k: int(v.replace(".", "")) for k, v in mortas.items()},
    }


def verificar(contrato: dict, quiet: bool = False) -> int:
    achados = []
    with _conn() as c, c.cursor() as cur:
        # --- tabelas por entidade -------------------------------------------
        for nome, spec in contrato["entidades"].items():
            vivas = []
            for t in spec["tabelas"]:
                schema, tabela = t.split(".") if "." in t else ("public", t)
                cur.execute("""SELECT 1 FROM information_schema.tables
                               WHERE table_schema=%s AND table_name=%s""", (schema, tabela))
                if cur.fetchone():
                    vivas.append(t)
            if len(vivas) > spec["esperado"]:
                achados.append((VERMELHO, f"{nome}: {len(vivas)} tabelas, contrato diz {spec['esperado']}"))
            elif len(vivas) < spec["esperado"]:
                achados.append((VERDE, f"{nome}: {len(vivas)} tabelas (era {spec['esperado']}) — consolidou"))

        # --- total de tabelas ------------------------------------------------
        cur.execute("""SELECT COUNT(*) FROM information_schema.tables
                       WHERE table_schema NOT IN ('pg_catalog','information_schema')""")
        total = cur.fetchone()[0]
        if total > contrato["total"]:
            achados.append((AMARELO, f"banco tem {total} tabelas, contrato registra {contrato['total']} "
                                     f"(+{total - contrato['total']} sem decisao registrada)"))
        elif total < contrato["total"]:
            achados.append((VERDE, f"banco tem {total} tabelas (eram {contrato['total']})"))

        # --- eras mortas recebendo escrita -----------------------------------
        # A regra e simples e dura: tabela declarada morta que CRESCEU voltou a
        # ser usada. Isso e mais grave que tabela nova — significa que alguem
        # esta alimentando um caminho que o contrato diz estar aposentado.
        for tabela, baseline in contrato["mortas"].items():
            try:
                cur.execute(f"SELECT COUNT(*) FROM {tabela}")
                agora = cur.fetchone()[0]
            except Exception:
                c.rollback()
                achados.append((VERDE, f"{tabela}: removida — era morta aposentada"))
                continue
            if agora > baseline:
                achados.append((VERMELHO, f"{tabela} RECEBEU ESCRITA: {baseline} → {agora} "
                                          f"(+{agora - baseline}). O contrato a declara morta."))

    print("╔═ CONFORMIDADE DO MODELO DE DADOS ═╗")
    if not achados:
        print(f"  {VERDE} banco conforme o contrato ({contrato['total']} tabelas, "
              f"{len(contrato['entidades'])} entidades no baseline)")
        return 0
    for icone, msg in achados:
        print(f"  {icone} {msg}")
    if not quiet:
        print(f"\n  Contrato: {os.path.basename(MEMO)}")
        print("  Divergencia nao e erro — e sinal. Se foi decisao, atualize o memo;")
        print("  se nao foi, alguem criou estrutura fora do modelo.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="so divergencias")
    a = ap.parse_args()
    return verificar(ler_contrato(), quiet=a.quiet)


if __name__ == "__main__":
    sys.exit(main())
