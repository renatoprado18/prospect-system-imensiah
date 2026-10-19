"""
Extrai o TEOR das decisões de um alerta processual.

⚠️ SO O ASTREA TRAZ TEOR. Medido nos 30 alertas de 30 dias (19/10/26), e a
conta fecha em tres baldes:

- **7 com teor extraido** — Astrea, nos dois formatos (ver `_TEOR`).
- **6 sem teor de verdade** — tambem Astrea, mas sao "andamento automatico"
  ("Processo encaminhado para o Magistrado"): nao houve decisao, logo nao ha
  teor. Ausencia real, nao furo de parser.
- **15 do Jusbrasil** (`nao-responda@jusbrasil.com.br`): corpo de 533 a 854
  chars e **nenhum teor** — diz *"Acesse para conferir mais detalhes"* e manda
  link. Nao e extraivel por nenhuma engenhosidade: a informacao nao esta ali.

Isso sai DECLARADO (`tem_teor=False` + `motivo_sem_teor`), porque um extrator
que devolve vazio sem dizer por que vira "o processo nao teve decisao" na
leitura de quem consome.

⚠️ O board afirmava *"o teor vem no corpo"* sem distinguir os produtores. Vale
para metade dos alertas — e foi preciso medir para saber de qual metade.
"""

import re
from typing import Dict, List, Optional

# Campos de identificacao do cabecalho do Astrea
_CAMPOS = {
    "numero": re.compile(r"N[úu]mero:\s*([0-9.\-]{15,30})"),
    "acao": re.compile(r"Nome da a[çc][ãa]o:\s*(.+?)(?:\n|N[úu]mero:)", re.S),
    "juizo": re.compile(r"Ju[íi]zo:\s*(.+?)(?:\n|Tradu[çc][ãa]o)", re.S),
    "instancia": re.compile(r"Inst[âa]ncia:\s*(.+?)(?:\n|Nome da)", re.S),
}

# ⚠️ O ASTREA TEM DOIS FORMATOS, e a 1a versao deste extrator conhecia um só —
# achou teor em **2 de 13** e eu quase relatei "os outros 11 nao trazem teor".
# Eram 11 corpos de ate 5.090 chars que o parser nao lia:
#
#   formato "movimentacoes/andamentos" -> rotulo `Teor do ato:`
#   formato "publicacoes"              -> rotulo `Conteudo:`  ← este faltava
#
# O de publicacao e o MAIS rico: traz `Parte: X Polo: ATIVO|PASSIVO`, os
# advogados com OAB, `Classe:`, `Orgao:` e o link do `Inteiro teor:` no DJE.
# Licao: cobertura baixa de parser e hipotese sobre o PARSER antes de ser
# conclusao sobre a FONTE.
_TEOR = re.compile(
    r"(?:Teor do ato|Conte[úu]do):\s*(.+?)"
    r"(?=\s*(?:Tradu[çc][ãa]o do Juridiqu[êe]s|Movimenta[çc][ãa]o oficial"
    r"|Rela[çc][ãa]o:|Este e-mail|Entendi|Publica[çc][ãa]o\s*\n|$))",
    re.S | re.I,
)

# `Parte: NOME Polo: ATIVO|PASSIVO` — identificacao oficial, melhor que inferir
# nomes do texto corrido do ato.
_PARTE_POLO = re.compile(
    r"Parte:\s*(.+?)\s*Polo:\s*(ATIVO|PASSIVO)", re.I)

_META_PUB = {
    "orgao": re.compile(r"[ÓO]rg[ãa]o:\s*(.+?)(?:\n|Data de)", re.S),
    "tipo_comunicacao": re.compile(r"Tipo de comunica[çc][ãa]o:\s*(.+?)(?:\n|Meio:)", re.S),
    "classe": re.compile(r"Classe:\s*(.+?)(?:\n|Conte[úu]do:)", re.S),
    "inteiro_teor_url": re.compile(r"Inteiro teor:\s*(https?://\S+)"),
    "disponibilizacao": re.compile(r"Data de disponibiliza[çc][ãa]o:\s*([0-9/]{8,10})"),
    "processo_principal": re.compile(r"processo principal\s*([0-9.\-]{15,30})"),
}

# A tradução por IA do Astrea: util, mas e INTERPRETACAO, nao o ato. Fica em
# campo separado pra nunca ser citada como se fosse o texto da decisao.
_TRADUCAO = re.compile(
    r"Tradu[çc][ãa]o do Juridiqu[êe]s\s*(?:Gerada por Intelig[êe]ncia "
    r"Artificial\*?)?\s*(.+?)"
    r"(?=\s*(?:Entendi|N[ãa]o entendi|Movimenta[çc][ãa]o oficial|$))",
    re.S | re.I,
)

# Tipo da movimentacao, logo depois de "Disponibilizada pelo tribunal"
_TIPO_MOV = re.compile(
    r"Disponibilizada pelo tribunal\s*(.+?)(?=\s*(?:Rela[çc][ãa]o:|Teor do ato:|$))",
    re.S | re.I,
)

# Sinais de ato que exige reacao — nao e classificacao juridica, e TRIAGEM:
# serve pra ordenar o que o Renato le primeiro, e por isso erra pro lado de
# sinalizar demais.
_SINAIS_CRITICOS = (
    ("bloqueio de ativos", "bloqueio de ativos financeiros"),
    ("sisbajud", "ordem via SISBAJUD"),
    ("indisponibilidade", "indisponibilidade de ativos"),
    ("penhora", "penhora"),
    ("teimosinha", "SISBAJUD teimosinha (repete por dias)"),
    ("arresto", "arresto"),
    ("despejo", "despejo"),
    ("multa", "multa"),
    ("intime-se", "intimacao com prazo"),
    ("prazo de", "prazo em curso"),
)


def _limpa(t: Optional[str]) -> str:
    return " ".join(str(t or "").split())


def _alcance_literal(teor: str) -> Optional[str]:
    """
    O trecho "em nome de ..." COMO O JUIZ ESCREVEU, sem separar em nomes.

    🔴 DECISAO DE DESENHO, tomada depois de duas tentativas falhas: **nao se
    estrutura lista de nomes em portugues juridico com heuristica**, e as duas
    armadilhas deste corpus bastam pra provar:

      1. *"Renato de Faria **e** Almeida Prado"* — a conjuncao esta DENTRO do
         nome; split por " e " inventava duas pessoas que nao existem.
      2. *"Framboesa Engenharia**,** Arquitetura e Design Ltda."* — a virgula
         esta DENTRO do nome; split por virgula **omitia a Framboesa da lista
         de atingidos** por uma ordem de bloqueio.

    Corrigir (1) quebrou (2) e vice-versa — cada remendo movia o erro em vez de
    fecha-lo. Num ato que bloqueia conta, **inventar uma parte e omitir uma
    parte sao ambos inaceitaveis**, e nenhuma heuristica dava garantia nos dois
    sentidos. Entao devolvemos a FRASE LITERAL: quem le ve a lista exatamente
    como esta na decisao, e a estruturacao fica com o dado oficial
    (`partes`, de `Parte: X Polo: Y`), que o tribunal ja entrega separado.

    Precisao > conveniencia quando o consumidor decide sobre dinheiro bloqueado.
    """
    m = re.search(
        r"(em nome de\s+.+?)"
        r"(?=\s*(?:Quanto a|Ap[oó]s a|Ap[oó]s o|Finalizado|$))",
        teor, re.S | re.I)
    return _limpa(m.group(1)) if m else None


def menciona(teor: str, nome: str) -> bool:
    """
    O ato nomeia esta pessoa? Comparacao direta, sem tokenizar o nome.

    E a pergunta que de fato importa ("este bloqueio me alcanca?") e a unica
    que da pra responder com certeza: busca de substring normalizada, sem
    inferencia. Nome completo do Renato tem conjuncao no meio, e e exatamente
    por isso que NAO se tokeniza.
    """
    import unicodedata

    def n(t):
        t = unicodedata.normalize("NFD", str(t or "").lower())
        return " ".join("".join(c for c in t
                                if unicodedata.category(c) != "Mn").split())

    return n(nome) in n(teor)


def _partes_com_polo(corpo: str) -> List[Dict]:
    """`Parte: NOME Polo: ATIVO|PASSIVO`, a identificacao oficial da publicacao."""
    return [{"nome": _limpa(n), "polo": p.upper()}
            for n, p in _PARTE_POLO.findall(corpo or "")]


def extrair(subject: str, content: str, from_email: str = "") -> Dict:
    """
    Teor e identificacao de um alerta. Nunca levanta.

    Devolve `tem_teor`, `fonte`, os campos do cabecalho, `movimentacoes`
    (cada uma com `tipo`, `teor`, `traducao_ia`, `sinais`,
    `alcance_literal`) e
    `motivo_sem_teor` quando aplicavel — a razao e parte do resultado.
    """
    corpo = content or ""
    fonte = "astrea" if "aurum" in (from_email or "").lower() else (
        "jusbrasil" if "jusbrasil" in (from_email or "").lower() else "?")

    out: Dict = {
        "fonte": fonte, "tem_teor": False, "movimentacoes": [],
        "numero": None, "acao": None, "juizo": None, "instancia": None,
        "orgao": None, "tipo_comunicacao": None, "classe": None,
        "inteiro_teor_url": None, "disponibilizacao": None,
        "processo_principal": None, "partes": [],
        "motivo_sem_teor": None,
    }

    for campo, rx in _CAMPOS.items():
        m = rx.search(corpo)
        if m:
            out[campo] = _limpa(m.group(1))[:200]

    # formato "publicacao": metadados e as partes com polo
    for campo, rx in _META_PUB.items():
        m = rx.search(corpo)
        if m:
            out[campo] = _limpa(m.group(1))[:200]
    out["partes"] = _partes_com_polo(corpo)
    # `Processo:` aparece no formato publicacao e `Numero:` no de movimentacao
    if not out.get("numero"):
        m = re.search(r"Processo:\s*([0-9.\-]{15,30})", corpo)
        if m:
            out["numero"] = _limpa(m.group(1))

    teores = [_limpa(t) for t in _TEOR.findall(corpo)]
    if not teores:
        out["motivo_sem_teor"] = (
            "o Jusbrasil nao manda o teor no e-mail — so aviso + link"
            if fonte == "jusbrasil"
            else "nenhum 'Teor do ato' nem 'Conteudo:' no corpo")
        return out

    traducoes = [_limpa(t) for t in _TRADUCAO.findall(corpo)]
    tipos = [_limpa(t) for t in _TIPO_MOV.findall(corpo)]

    for i, teor in enumerate(teores):
        low = teor.lower()
        sinais = sorted({rot for kw, rot in _SINAIS_CRITICOS if kw in low})
        out["movimentacoes"].append({
            "tipo": tipos[i] if i < len(tipos) else None,
            "teor": teor,
            # a traducao e do Astrea e e INTERPRETACAO — campo separado de
            # proposito, pra nao ser citada como o texto do ato
            "traducao_ia": traducoes[i] if i < len(traducoes) else None,
            "sinais": sinais,
            "alcance_literal": _alcance_literal(teor),
        })

    out["tem_teor"] = True
    return out
