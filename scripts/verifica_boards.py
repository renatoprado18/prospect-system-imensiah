#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Verificador de teto dos boards — roda na ABERTURA da sessão.

POR QUE EXISTE, e por que não bastava o hook. Já havia uma guarda de teto no
`~/.claude/hooks/sync_memory.sh`: quando alguém ESCREVE num board acima do
limite, ela avisa. Mas o dano não é da escrita — é da LEITURA. Board grande
demais é lido truncado na abertura, e a sessão decide sobre um pedaço do
conteúdo achando que viu tudo. Em 30/07 o `/dev` leu **22% do backlog** (44.080
de 198.959 caracteres) sem nenhum sinal de que faltava o resto.

Quem escreve recebe o alarme e pode ignorá-lo; quem LÊ nunca recebia nada. Este
script é o alarme do lado do leitor — e ele FALHA ALTO (exit 1), porque um
verificador que só sussurra é o mesmo que não existir: foi assim que três boards
passaram do teto e ninguém consolidou.

Medido em 07/08, quando este script nasceu: `session_locks.md` com 17,1 KB e
`project_cos_status.md` com 12,1 KB — os dois quase no dobro do teto, lidos
naquela mesma manhã sem que a sessão soubesse.

O teto é 9 KB e não 8 pelo mesmo motivo do hook: o endpoint de sync devolve o
`.md` com frontmatter enriquecido (`originSessionId` + `modified`), o que engorda
algumas centenas de bytes a cada escrita. Cobrar 8 KB exatos faria o autor cortar
prosa boa por causa de metadado que ele não escreveu.

`*_historico.md` é isento POR DESENHO: é arquivo morto, append-only, nunca lido
na abertura. O teto existe justamente para empurrar conteúdo para dentro dele.

Uso:  ./verifica_boards.py            # relatório
      ./verifica_boards.py --quiet    # só o que estourou (abertura de sessão)
"""
import argparse
import os
import sys

MEM = os.path.expanduser("~/.claude/projects/-Users-rap-prospect-system/memory")

# O que a abertura de sessão carrega inteiro, com o teto de cada um.
#
# O MEMORY.md tem teto MAIOR e por um motivo, não por indulgência: ele é ÍNDICE,
# não board. Cortar um board move prosa envelhecida pro histórico; cortar o
# índice APAGA o ponteiro pra uma memória — que é esquecer, não arrumar. O
# número aqui é o limite prático de leitura da ferramenta (~25 KB, o ponto em
# que ela recusou abrir o `cos_status` em 30/07), com folga.
#
# ⚠️ Na 1ª versão deste script (07/08) dei 12 KB a ele por analogia com o hook e
# o alarme acusou 167% — sobre um arquivo que a sessão vinha lendo INTEIRO sem
# problema. Alarme sem dano demonstrado é o defeito que este mesmo dia
# encontrou no monitor do RACI; corrigido antes de entrar no ar.
BOARDS = {
    "MEMORY.md":               (24576, "índice lido em TODA sessão — teto é o limite de leitura"),
    "session_locks.md":         (9216, "canal CoS⇄Dev"),
    "project_dev_backlog.md":   (9216, "roadmap dev"),
    "project_cos_status.md":    (9216, "board executivo"),
}
VERDE, AMARELO, VERMELHO = "🟢", "🟡", "🔴"


def medir():
    achados, ok = [], []
    for nome, (teto, papel) in BOARDS.items():
        caminho = os.path.join(MEM, nome)
        if not os.path.exists(caminho):
            achados.append((VERMELHO, f"{nome} NÃO EXISTE — a abertura conta com ele"))
            continue
        tam = os.path.getsize(caminho)
        pct = 100 * tam / teto
        if tam > teto:
            achados.append((VERMELHO if pct > 130 else AMARELO,
                            f"{nome}: {tam/1024:.1f} KB — {pct:.0f}% do teto "
                            f"({teto/1024:.0f} KB, {papel}). Excesso: {(tam-teto)/1024:.1f} KB"))
        else:
            ok.append(f"{nome} {tam/1024:.1f}/{teto/1024:.0f} KB")
    return achados, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="só o que estourou")
    a = ap.parse_args()

    achados, ok = medir()
    print("╔═ TETO DOS BOARDS (o que a abertura lê inteiro) ═╗")
    if not achados:
        print(f"  {VERDE} todos dentro do teto — {' · '.join(ok)}")
        return 0

    for icone, msg in achados:
        print(f"  {icone} {msg}")
    if ok and not a.quiet:
        print(f"  {VERDE} dentro do teto: {' · '.join(ok)}")
    print("\n  ⚠️ ISTO NÃO É COSMÉTICO. Acima do teto, a abertura lê o arquivo TRUNCADO e a")
    print("  sessão decide sobre um pedaço do conteúdo achando que viu tudo. Em 30/07 o")
    print("  `/dev` leu 22% do backlog sem nenhum sinal.")
    print("  CONSERTO: mova o que envelheceu para <board>_historico.md (append-only, nunca")
    print("  lido na abertura) e deixe no board só o ESTADO ATUAL. Recado atendido SAI.")
    # Falha alto de propósito: alarme que não interrompe vira paisagem — foi
    # exatamente o que aconteceu com o aviso do hook, ignorado até 17 KB.
    return 1


if __name__ == "__main__":
    sys.exit(main())
