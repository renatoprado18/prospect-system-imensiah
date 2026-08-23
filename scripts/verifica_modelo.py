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
    #
    # O `\*` no nome da entidade nao e' decorativo. Ate 14/08/26 o padrao aceitava
    # so letras, e a linha `| **Lugar** | **2** | locais · locais_contatos |` — a
    # 11a entidade, criada pela 074 no dia anterior — simplesmente nao casava. O
    # verificador seguia imprimindo VERDE dizendo "6 entidades no baseline" contra
    # as 7 do memo, sem nunca ter olhado `locais`/`locais_contatos`. Guarda que se
    # abstem em silencio certifica o que nao checou ([[feedback_guarda_abstencao_vira_fabrica]]):
    # por isso o total de entidades agora e' conferido contra o memo, logo abaixo.
    #
    # ⚠️ 23/08/26 — ESSA CONFERENCIA NAO EXISTIA. O comentario acima a prometia
    # desde 14/08 e o codigo logo abaixo so conferia o total de TABELAS (168). O
    # efeito: o memo declara 11 entidades, a tabela de baseline lista 7, e o
    # verificador imprimia `🟢 ... 7 entidades no baseline` todo dia — sem que
    # nada dissesse que as outras 4 nunca foram olhadas. Entre elas **Frente**,
    # que o proprio memo chama de hub ("quase toda travessia passa por ela").
    # O conserto de 14/08 mexeu no regex e parou ali; a guarda estrutural que o
    # comentario anunciava ficou por escrever. Comentario nao e' codigo — o
    # verificador nao le o que ele promete ([[feedback_prompt_nao_le_comentario]]).
    entidades = {}
    for m in re.finditer(r"^\|\s*\**\s*([A-Za-zÀ-ÿ ]+?)\s*\**\s*\|\s*\*\*(\d+)\*\*\s*\|\s*(.+?)\s*\|$",
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

    # A guarda prometida acima, agora escrita. O memo declara o numero de
    # entidades no cabecalho ("## As 11 entidades, em 3 grupos"); se o baseline
    # nao tem uma linha para cada uma, o verificador NAO pode dizer "conforme" —
    # ele so olhou parte do modelo. Divergir aqui e' o estado esperado enquanto o
    # baseline estiver incompleto, e e' exatamente esse o aviso que faltava.
    declarado = re.search(r"##\s*As\s+(\d+)\s+entidades", texto)
    if not declarado:
        sys.exit("[verifica-modelo] o memo nao declara mais quantas entidades existem "
                 "('## As N entidades'). Sem esse numero nao da pra saber se o "
                 "baseline cobre o modelo inteiro — corrija o memo ou o parse.")

    return {
        "entidades": entidades,
        "entidades_declaradas": int(declarado.group(1)),
        "total": int(total.group(1)),
        "mortas": {k: int(v.replace(".", "")) for k, v in mortas.items()},
    }


def verificar(contrato: dict, quiet: bool = False) -> int:
    achados = []
    with _conn() as c, c.cursor() as cur:
        # --- o baseline cobre o modelo inteiro? ------------------------------
        # Primeiro de tudo, porque condiciona todo o resto: um baseline parcial
        # nao pode produzir a frase "banco conforme o contrato".
        no_baseline, declaradas = len(contrato["entidades"]), contrato["entidades_declaradas"]
        if no_baseline < declaradas:
            faltam = declaradas - no_baseline
            achados.append((AMARELO,
                            f"BASELINE PARCIAL: {no_baseline} de {declaradas} entidades tem linha "
                            f"no baseline — {faltam} nunca foram medidas. O verde abaixo, quando "
                            f"vier, vale so para as {no_baseline} verificadas."))
        elif no_baseline > declaradas:
            achados.append((VERMELHO,
                            f"o baseline tem {no_baseline} entidades e o memo declara {declaradas} "
                            f"— um dos dois esta errado; sem isso batendo nao ha contrato"))

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

        # --- cobertura: quantas tabelas nenhuma entidade reivindica ----------
        # A regua honesta do alcance deste verificador. "7 de 11 entidades" ja
        # diz pouco; o que importa e quantas TABELAS ficam fora de qualquer
        # entidade, porque e nelas que estrutura nova entra sem ser notada. Sai
        # como numero, nao como cor: cobertura parcial e' estado conhecido do
        # modelo, nao defeito do dia ([[feedback_regua_cobertura_parcial]]).
        reivindicadas = {t.split(".")[-1] for spec in contrato["entidades"].values()
                         for t in spec["tabelas"]}
        cur.execute("""SELECT table_name FROM information_schema.tables
                       WHERE table_schema='public' AND table_type='BASE TABLE'""")
        do_banco = {r[0] for r in cur.fetchall()}
        fora = do_banco - reivindicadas
        if fora:
            achados.append((AMARELO,
                            f"COBERTURA: {len(do_banco) - len(fora)} de {len(do_banco)} tabelas "
                            f"pertencem a alguma entidade ({100*(len(do_banco)-len(fora))//len(do_banco)}%) "
                            f"— {len(fora)} fora de qualquer entidade, onde estrutura nova entra sem alarme"))

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
