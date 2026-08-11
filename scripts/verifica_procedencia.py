#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Verificador de procedência — roda ANTES de entregar peça que o Renato assina.

POR QUE EXISTE. Em 10/08/26 ele corrigiu a sessão CoS **quatro vezes** na mesma
conversa, sempre o mesmo defeito: afirmação sobre ele escrita a partir do que
soava bem, com a evidência contrária disponível — três das quatro a menos de 90
segundos na mesma fonte. Nenhuma saiu porque ele leu; a carga de conferir ficou
com ele. A regra saiu daí ([[feedback_procedencia_em_peca_assinada]]): toda peça
assinada vem com a fonte ao lado de cada afirmação factual, e o que não tem fonte
fica marcado como inferido — nunca some na prosa.

POR QUE UM SCRIPT, e não a regra sozinha. O memo já registra que memória degrada
ao longo da sessão, que auto-revisão é o método mais fraco (o mesmo processo que
escreveu avalia, e tende a confirmar), e que pedir rigor já foi tentado. O que
funciona nesta casa é o padrão de `verifica_boards.py`, `rotas_sem_auth.py` e
`memory_inventory.py`: **regra que depende de lembrar falha; regra que vira
instrumento morde**. Este script não pede boa vontade — devolve uma lista e
reprova acima de zero.

O QUE ELE NÃO É. Não é detector de mentira: não sabe se "115%" é verdade. Ele
responde uma pergunta mais modesta e verificável — *esta afirmação tem um
ponteiro para onde conferir?* Um número certo sem fonte reprova igual, e é o
comportamento desejado: a fonte é o que permite ao Renato checar em 10 segundos
em vez de reler a peça inteira desconfiando de tudo.

⚠️ RODE NO RASCUNHO ANOTADO, NÃO NA PEÇA FINAL. A fonte é para o RENATO
conferir, não para o destinatário ler — `messages#27742` numa página que vai ao
Gui seria absurdo. O memo já define o fluxo: a peça sai limpa e o bloco de
procedência vai JUNTO na devolutiva, para ele ver a base ao lado do texto. O
artefato que este script mede é o de trabalho; a versão que sai não leva marca
nenhuma. Rodar na peça final acusa 100% e não quer dizer nada — foi o que
aconteceu no primeiro teste com `posicionamento_ap.html`.

COMO PASSAR NO VERIFICADOR (as duas saídas são legítimas):
  1. citar a fonte  — `messages#27742`, `task#999789`, `(CV, linha 12)`,
     `(áudio 14:32)`, `[fonte: RACI Vallen]`
  2. marcar como não-verificado — `[inferido]`, `[sem base]`, `[hipótese]`,
     `[a confirmar]`
A segunda existe porque o memo pede exatamente isso: afirmação sem fonte não
some, fica MARCADA. O que o script proíbe é a terceira via — a afirmação
factual escorregando na prosa como se fosse sabida.

FALSO POSITIVO É BARATO, FALSO NEGATIVO NÃO. A heurística marca de mais: "1"
numa lista numerada pode virar achado. Custa um olhar. Deixar passar "cresceu
115%" sem fonte custa o que custou em 10/08 — e a assimetria é o motivo de o
corte ser generoso.

Uso:  ./verifica_procedencia.py rascunho.md
      ./verifica_procedencia.py --texto "Fundei a Natique em 1996."
      cat peca.txt | ./verifica_procedencia.py -
      ./verifica_procedencia.py peca.html --quiet   # só o veredito
"""
import argparse
import html
import pathlib
import re
import sys

VERDE, AMARELO, VERMELHO = "🟢", "🟡", "🔴"

# ---------------------------------------------------------------------------
# O QUE CONTA COMO PONTEIRO DE FONTE.
#
# Deliberadamente amplo: o objetivo é que citar seja FÁCIL. Um verificador que
# só aceita um formato empurra o autor a brigar com a ferramenta em vez de citar
# — e aí ele para de rodar o verificador, que é o pior desfecho possível.
# ---------------------------------------------------------------------------
FONTE = re.compile(r"""
      \w+\#\d+                        # messages#27742, task#999789, bh#15
    | \#\d{3,}                        # #999789
    | \[fonte:[^\]]+\]                # [fonte: RACI Vallen]
    | \(fonte:[^)]+\)
    | \((?:CV|áudio|audio|ata|e-?mail|WA|WhatsApp|Fathom|LinkedIn)[^)]*\)
    | https?://\S+
    | \b\d{1,2}:\d{2}\b               # minuto do áudio: 14:32
""", re.X | re.I)

# Marcação explícita de "não tenho fonte" — a saída legítima da regra.
INFERIDO = re.compile(
    r"\[(?:inferido|sem base|hipótese|hipotese|a confirmar|não verificado|nao verificado)\]",
    re.I)

# ---------------------------------------------------------------------------
# O QUE CONTA COMO AFIRMAÇÃO FACTUAL.
#
# Sinais que, na prática, foram o que deu errado: número, percentual, dinheiro,
# data, período ("desde abril", "de 2013 a 2017") e verbo de feito concluído
# ("fundei", "vendi", "cresceu"). São os que soam bem e por isso escapam.
# ---------------------------------------------------------------------------
SINAIS = [
    (re.compile(r"\d+\s*%|\bpor cento\b", re.I),                       "percentual"),
    (re.compile(r"R\$\s?[\d.,]+|\bUS\$\s?[\d.,]+|\bmilhõ|\bmilhão|\bmil\b", re.I), "valor"),
    (re.compile(r"\b(19|20)\d{2}\b"),                                  "ano"),
    (re.compile(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b"),                    "data"),
    (re.compile(r"\bdesde\b|\bde \d+ a \d+\b|\bhá \d+\s+(anos|meses|dias)", re.I), "período"),
    (re.compile(r"\b\d+\s+(itens|reuniões|conselhos|anos|meses|empresas|"
                r"clientes|funcionários|transações|assentos|dias)\b", re.I),       "contagem"),
    (re.compile(r"\b(fundei|fundou|vendi|vendeu|cresceu|dobrou|triplicou|liderou|"
                r"liderei|coordenei|coordenou|implantei|implantou|entreguei|"
                r"entregou|conquistou|conquistei|faturou|fatura)\b", re.I),        "feito"),
    (re.compile(r"\b(sempre|nunca|todos|nenhum|único|primeira vez|maior|melhor)\b",
                re.I),                                                 "absoluto"),
]

# Linha que é estrutura, não afirmação: título markdown curto, item de menu,
# separador. Marcar isto seria ruído puro.
ESTRUTURA = re.compile(r"^\s*(#{1,6}\s|\|[-: |]+\||[-*_]{3,}\s*$)")


def _limpar(texto: str) -> str:
    """HTML vira texto. As peças do Renato saem em `~/cockpit/*.html`."""
    if "<" in texto and ">" in texto:
        texto = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", texto, flags=re.S | re.I)
        texto = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", texto, flags=re.I)
        texto = re.sub(r"<[^>]+>", " ", texto)
        texto = html.unescape(texto)
    return texto


def _sentencas(texto: str):
    """Quebra em sentenças, guardando a linha de origem.

    Por linha primeiro, e só depois por pontuação: numa peça real muita coisa é
    bullet sem ponto final, e quebrar só por `.` juntaria três afirmações num
    achado só — o autor não saberia qual delas citar.
    """
    for n, linha in enumerate(texto.split("\n"), 1):
        limpa = linha.strip()
        if not limpa or ESTRUTURA.match(limpa):
            continue
        for parte in re.split(r"(?<=[.!?;])\s+", limpa):
            parte = parte.strip(" -*•\t")
            if len(parte) >= 12:
                yield n, parte


def analisar(texto: str):
    """Devolve (achados, total_factuais). Achado = factual e SEM ponteiro."""
    achados, factuais = [], 0
    for linha, frase in _sentencas(_limpar(texto)):
        marcas = [nome for rx, nome in SINAIS if rx.search(frase)]
        if not marcas:
            continue
        factuais += 1
        if FONTE.search(frase) or INFERIDO.search(frase):
            continue
        achados.append({"linha": linha, "frase": frase, "sinais": marcas})
    return achados, factuais


def main() -> int:
    ap = argparse.ArgumentParser(description="Afirmação factual sem ponteiro de fonte.")
    ap.add_argument("alvo", nargs="?", help="arquivo (.md/.txt/.html) ou '-' para stdin")
    ap.add_argument("--texto", help="texto direto, em vez de arquivo")
    ap.add_argument("--quiet", action="store_true", help="só o veredito")
    a = ap.parse_args()

    if a.texto:
        conteudo, origem = a.texto, "--texto"
    elif a.alvo == "-":
        conteudo, origem = sys.stdin.read(), "stdin"
    elif a.alvo:
        p = pathlib.Path(a.alvo).expanduser()
        if not p.exists():
            print(f"{VERMELHO} arquivo não encontrado: {p}")
            return 2
        conteudo, origem = p.read_text(encoding="utf-8"), str(p)
    else:
        ap.print_help()
        return 2

    achados, factuais = analisar(conteudo)

    print(f"\n╔═ PROCEDÊNCIA · {origem} ═╗")
    if not factuais:
        # Zero factuais não é aprovação: é o verificador dizendo que não achou
        # nada para medir. Tratar como verde esconderia um arquivo vazio, um
        # HTML que não foi limpo, ou um seletor errado.
        print(f"  {AMARELO} nenhuma afirmação factual reconhecida — nada foi verificado.")
        print("     Se a peça TEM fatos, o texto não chegou aqui como esperado.")
        return 1

    if not achados:
        print(f"  {VERDE} {factuais} afirmação(ões) factual(is), todas com fonte ou marcadas.")
        return 0

    print(f"  {VERMELHO} {len(achados)} de {factuais} afirmações factuais SEM ponteiro de fonte:\n")
    for x in achados:
        frase = x["frase"] if len(x["frase"]) <= 150 else x["frase"][:147] + "..."
        print(f"  linha {x['linha']:>4} · [{', '.join(x['sinais'])}]")
        print(f"    {frase}\n")

    if not a.quiet:
        print("  Para cada uma: cite a fonte (messages#123, task#999, (CV, linha 12),")
        print("  (áudio 14:32)) OU marque como [inferido] / [sem base] / [a confirmar].")
        print("  A regra não é ter certeza — é o Renato poder conferir em 10 segundos")
        print("  em vez de reler a peça inteira desconfiando de tudo.")
        print("\n  ⚠️ RASCUNHO ANOTADO, não a peça final: a fonte é pra ELE conferir,")
        print("  não pro destinatário ler. A peça sai limpa e a procedência vai junto")
        print("  na devolutiva. Rodar no arquivo que já foi entregue acusa 100% à toa.")
        print("  ⚠️ Falso positivo é barato; falso negativo custou 4 correções numa")
        print("  sessão só em 10/08. O corte é generoso de propósito.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
