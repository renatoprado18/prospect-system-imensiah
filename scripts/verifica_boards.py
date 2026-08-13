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

TETO POR SEÇÃO + VALIDADE (12/08/26). O teto global dizia QUANTO cortar e nunca
QUAL — e quem fecha a sessão com pressa corta o que estiver mais à mão. Medido
naquele dia: os boards foram partidos em 30/07 e em 13 dias o `session_locks`
estourou pelo menos quatro vezes (03/08, 08/08 a 107%, 12/08 a 106%, 12/08 noite
a 126%), sempre voltando por consolidação manual. Eles não crescem sem limite —
**oscilam colados no teto**, o que é o retrato de um dreno que só roda quando
alguém se lembra.

A composição mostrou onde: o `session_locks` é **67% Log**, contra 26% e 18% de
"Fechado" nos outros dois. Não é acaso — é o único board em que os DOIS papéis
escrevem, e a regra dele ("recado atendido sai") depende da outra sessão
confirmar o atendimento. Ninguém desce o recado alheio sem saber se foi lido,
então ele acumula por padrão.

Daí as duas regras aqui: cada seção que acumula tem orçamento próprio, e cada
entrada datada tem VALIDADE. Recado que ninguém atendeu em 7 dias não vai ser
atendido — virou histórico sem ninguém declarar. Isso troca julgamento por data:
o script passa a dizer QUAIS linhas descer, não só quantos bytes sobram.

Uso:  ./verifica_boards.py            # relatório
      ./verifica_boards.py --quiet    # só o que estourou (abertura de sessão)
      ./verifica_boards.py --vencidos # lista as entradas fora da validade
"""
import argparse
import os
import re
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

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

# Seções que ACUMULAM — as que recebem uma entrada nova a cada sessão e não têm
# freio próprio. O resto do board (estado atual, backlog aberto, monitoramento)
# é reescrito no lugar e não precisa de orçamento.
#
# Os números saíram da medição de 12/08, com folga sobre o tamanho real daquele
# dia: Log 5,6 KB → 6; Fechado dev 1,5 → 2,5; Fechado cos 2,1 → 3. Teto que já
# nasce estourado é alarme sem conserto disponível — o autor não tem o que
# descer e aprende a ignorar o vermelho, que é como o aviso do hook virou
# paisagem até 17 KB.
#
# VALIDADE em dias: entrada mais velha que isso desce pro histórico. O Log usa
# 7 (recado não atendido em uma semana não será atendido); os "Fechado" usam 14,
# que é o que o próprio título deles promete ("últimos 14 dias").
SECOES = {
    "session_locks.md": [
        ("## 📓 Log", 6144, 7, "recados entre CoS e Dev"),
    ],
    "project_dev_backlog.md": [
        ("## ✅ Fechado", 2560, 14, "frentes fechadas"),
    ],
    "project_cos_status.md": [
        ("## ✅ Fechado", 3072, 14, "compromissos atendidos"),
    ],
}

# Entrada de board: "- **12/08 noite (Dev→CoS) — ..." ou "- **11/08 — ...".
# Exige o `**` porque o board usa `- ` também para sub-itens sem data, que não
# são entradas e não vencem.
_ENTRADA = re.compile(r"^-\s+\*\*(\d{1,2})/(\d{1,2})")
BRT = ZoneInfo("America/Sao_Paulo")


def _hoje():
    # Data BRT, não UTC: as entradas são datadas à mão por quem escreve, no
    # fuso em que ele vive. Depois das 21h BRT já é o dia seguinte em UTC, e a
    # entrada escrita "hoje" apareceria com um dia de idade.
    return datetime.now(BRT).date()


def _parse_data(dia, mes, hoje):
    """DD/MM sem ano → date. O ano fica implícito no board; assume o corrente e
    recua um quando a data cairia no futuro (dezembro lido em janeiro)."""
    for ano in (hoje.year, hoje.year - 1):
        try:
            d = date(ano, mes, dia)
        except ValueError:
            return None          # 31/02 e afins: entrada malformada, não vence
        if d <= hoje:
            return d
    return None


def _secoes_do(texto):
    """Corta o corpo em (titulo, conteudo) por cabeçalho `## `."""
    partes = re.split(r"\n(?=## )", texto)
    return [(p.split("\n")[0].strip(), p) for p in partes]


def medir_secoes(nome, caminho, hoje):
    """Devolve (estouros, vencidos) da seção que acumula deste board."""
    regras = SECOES.get(nome)
    if not regras:
        return [], []
    texto = open(caminho, encoding="utf-8").read()
    estouros, vencidos = [], []
    for marcador, teto, validade, papel in regras:
        for titulo, conteudo in _secoes_do(texto):
            if not titulo.startswith(marcador):
                continue
            tam = len(conteudo.encode("utf-8"))
            if tam > teto:
                estouros.append(
                    f"{nome} § {marcador.lstrip('# ')}: {tam/1024:.1f} KB — "
                    f"{100*tam/teto:.0f}% do teto de seção ({teto/1024:.0f} KB, {papel})")
            for linha in conteudo.split("\n"):
                m = _ENTRADA.match(linha)
                if not m:
                    continue
                d = _parse_data(int(m.group(1)), int(m.group(2)), hoje)
                if d is None:
                    continue
                idade = (hoje - d).days
                if idade > validade:
                    vencidos.append((idade, nome, linha.strip()[:96]))
    return estouros, vencidos


def medir():
    achados, ok, vencidos = [], [], []
    hoje = _hoje()
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

        estouros_sec, venc = medir_secoes(nome, caminho, hoje)
        achados.extend((AMARELO, e) for e in estouros_sec)
        vencidos.extend(venc)
    vencidos.sort(reverse=True)          # mais velho primeiro: é o que desce antes
    return achados, ok, vencidos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="só o que estourou")
    ap.add_argument("--vencidos", action="store_true",
                    help="lista as entradas fora da validade (o que descer primeiro)")
    a = ap.parse_args()

    achados, ok, vencidos = medir()

    if a.vencidos:
        print("╔═ ENTRADAS FORA DA VALIDADE (descer pro histórico) ═╗")
        if not vencidos:
            print(f"  {VERDE} nenhuma — toda entrada datada está dentro do prazo da seção")
            return 0
        for idade, nome, linha in vencidos:
            print(f"  {AMARELO} {idade:3d}d  {nome:26} {linha}")
        print(f"\n  {len(vencidos)} entrada(s). Recado não atendido em uma semana não será")
        print("  atendido — virou histórico sem ninguém declarar.")
        return 0

    print("╔═ TETO DOS BOARDS (o que a abertura lê inteiro) ═╗")
    if not achados:
        linha = f"  {VERDE} todos dentro do teto — {' · '.join(ok)}"
        print(linha)
        if vencidos:
            # Não falha: o board cabe numa leitura, que é o dano que este script
            # existe pra impedir. Mas avisa, porque é daqui que vem o próximo
            # estouro — e agora há uma lista do que descer, não só um número.
            print(f"  {AMARELO} {len(vencidos)} entrada(s) fora da validade "
                  f"(a mais velha: {vencidos[0][0]}d). Veja com --vencidos.")
        return 0

    for icone, msg in achados:
        print(f"  {icone} {msg}")
    if ok and not a.quiet:
        print(f"  {VERDE} dentro do teto: {' · '.join(ok)}")

    if vencidos:
        print(f"\n  ▼ COMECE POR AQUI — {len(vencidos)} entrada(s) fora da validade:")
        for idade, nome, linha in vencidos[:5]:
            print(f"     {idade:3d}d  {nome:26} {linha}")
        if len(vencidos) > 5:
            print(f"     … mais {len(vencidos)-5} (--vencidos lista todas)")

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
