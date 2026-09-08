"""RACI COMPLETA publicada no grupo — detectar o que ela traz e o banco nao tem.

O buraco que este modulo torna VISIVEL (08/09/26): em 04/09 a Kelly Souza
publicou no grupo da Alba uma RACI com 16 itens; `raci_itens` do projeto #26
tinha 6. Dez viviam so como mensagem de WhatsApp, e um deles constava
`em_andamento` no banco havendo sido marcado concluido na publicacao — a CoS
chegou a cobrar do Renato algo que ja estava feito.

POR QUE O SHADOW NAO PEGOU. A mensagem FOI processada (`raci_processed_at`
carimbado em 05/09 00h34) e gerou ZERO propostas. O vocabulario de acoes do
`raci_group_shadow` e `add_note` / `update_status` / `update_prazo` /
`complete`: ele so sabe MEXER em item que ja existe. Item que o banco nunca viu
nao tem como virar proposta — entao sumiu calado, com a mensagem marcada como
tratada. [[feedback_guarda_abstencao_vira_fabrica]]

O QUE ESTE MODULO FAZ — E O QUE NAO FAZ. Ele so CONTA e NOMEIA. Nao cria item,
nao propoe, nao aplica: governanca de cliente nao muda sem gate humano, e a
fila de review ja tem 33 propostas paradas (a mais antiga de 22/07). Produzir
mais fila sem consumo seria trocar um buraco silencioso por outro.
[[feedback_medir_o_consumidor_certo]]

FILTRO DE AUTOR — nao por `from_me`, e sim pela ORIGEM do texto. Das 19 RACIs
publicadas em 2 meses, 17 sao do proprio INTEL: auditar essas seria a maquina
ouvindo o proprio eco ([[feedback_maquina_ouve_o_proprio_eco]]), porque elas
FORAM GERADAS a partir do banco. Mas tambem nao se pode filtrar so por autor:
na Alba quem publicou a versao atualizada foi a **Kelly Souza**, nao o Renato.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

# Cabecalho da peca. Aceita o travessao com ou sem espaco e as duas grafias que
# aparecem em prod (`*RACI — Empresa*` do sistema, `*RACI — Empresa*` colado
# num "Bom dia! Segue RACI atualizada" da Kelly).
_RE_CABECALHO = re.compile(r"\*\s*RACI\s*[—\-–]\s*.+?\*", re.IGNORECASE)

# Cabecalho de bucket: `🔄 *Em andamento (8):*`, `✅ *Concluídos (4):*`,
# `🚨 *Atrasados (8):*`. O `(N)` e opcional de proposito — o texto humano nem
# sempre conta.
_RE_BUCKET = re.compile(
    r"\*\s*(Em andamento|Pendentes?|Conclu[ií]dos?|Atrasados?|A fazer)\s*"
    r"(?:\(\d+\))?\s*:?\s*\*",
    re.IGNORECASE,
)

# Bullet. `* ` (Kelly), `• ` (sistema) ou `- `. O ESPACO depois e obrigatorio:
# sem ele, `*RACI — Alba*` (negrito do WhatsApp) entraria como item.
_RE_BULLET = re.compile(r"^\s*[*•\-]\s+(.*\S)\s*$")

_STATUS_POR_BUCKET = {
    "em andamento": "em_andamento",
    "pendente": "pendente",
    "pendentes": "pendente",
    "concluido": "concluido",
    "concluidos": "concluido",
    "atrasado": "atrasado",
    "atrasados": "atrasado",
    "a fazer": "pendente",
}


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def parece_raci_publicada(texto: Optional[str]) -> bool:
    """True se o texto e uma RACI COMPLETA, nao um reporte solto.

    Exige cabecalho E pelo menos dois buckets: uma mensagem que so cita "RACI"
    de passagem ("segue a RACI amanha") nao pode entrar."""
    if not texto or len(texto) < 200:
        return False
    if not _RE_CABECALHO.search(texto):
        return False
    return len(_RE_BUCKET.findall(texto)) >= 2


def parse_itens_publicados(texto: str) -> List[Dict[str, Any]]:
    """Extrai os itens da peca: acao, responsavel, prazo e status pelo bucket.

    O responsavel vem depois do ULTIMO travessao da linha — a acao costuma
    conter travessoes proprios ("consultoria ~15,5% / treinamento ~6%" vem
    precedido de um). Partir pelo primeiro jogaria metade da acao no nome.
    """
    itens: List[Dict[str, Any]] = []
    bucket_atual: Optional[str] = None

    for linha in (texto or "").splitlines():
        m_bucket = _RE_BUCKET.search(linha)
        if m_bucket:
            chave = _sem_acento(m_bucket.group(1).strip().lower())
            bucket_atual = _STATUS_POR_BUCKET.get(chave)
            continue

        m_item = _RE_BULLET.match(linha)
        if not m_item or bucket_atual is None:
            continue

        corpo = m_item.group(1).strip()
        acao, responsavel, prazo = corpo, None, None

        if "—" in corpo or "–" in corpo:
            sep = "—" if "—" in corpo else "–"
            cabeca, _, cauda = corpo.rpartition(sep)
            if cabeca.strip():
                acao = cabeca.strip()
                responsavel = cauda.strip()

        if responsavel:
            m_prazo = re.search(r"\(([^()]*\d[^()]*)\)\s*$", responsavel)
            if m_prazo:
                prazo = m_prazo.group(1).strip()
                responsavel = responsavel[: m_prazo.start()].strip()

        itens.append({
            "acao": acao,
            "responsavel": responsavel or None,
            "prazo": prazo,
            "status": bucket_atual,
        })

    return itens


def _chave(acao: str) -> str:
    """Normaliza a acao pra casar com o banco: sem acento, sem pontuacao, minuscula.

    O texto publicado passa por edicao humana entre uma rodada e outra — o que
    sobrevive e o miolo. Truncagem em `…` (o preview corta acao longa) tambem
    e tratada, senao um item truncado no grupo pareceria sempre inedito.
    """
    s = _sem_acento((acao or "").lower())
    s = s.split("…")[0].split("...")[0]
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def _prefixo_casa(a: str, b: str, minimo: int = 25) -> bool:
    """Casa por prefixo quando um dos lados foi truncado na publicacao."""
    if not a or not b:
        return False
    curto, longo = (a, b) if len(a) <= len(b) else (b, a)
    return len(curto) >= minimo and longo.startswith(curto)


# Corte de similaridade. Calibrado contra o caso real: o item #36 da Alba esta
# no banco como "salario fixo × dividendos" e foi publicado como "salario-fixo
# vs. dividendos" — mesmo item, reescrito por quem editou a peca. Sem isto ele
# entrava como "inedito" e o aviso comecava com um falso positivo em dois.
_CORTE_SIMILARIDADE = 0.82


def _similar(a: str, b: str) -> bool:
    """Similaridade textual alta. Conservador: na duvida, considera o mesmo item."""
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _CORTE_SIMILARIDADE


def itens_nao_absorvidos(
    publicados: List[Dict[str, Any]],
    acoes_no_banco: List[str],
) -> List[Dict[str, Any]]:
    """Os itens da peca que o banco nao conhece.

    Casamento conservador de proposito: na duvida, considera ABSORVIDO. Um
    falso "inedito" vira ruido num aviso que o Renato le; um falso "ja tenho"
    so mantem o estado de hoje. O erro barato e o que subconta.
    """
    chaves = {_chave(a) for a in acoes_no_banco if a}
    chaves.discard("")
    fora: List[Dict[str, Any]] = []

    for item in publicados:
        k = _chave(item.get("acao", ""))
        if not k:
            continue
        if k in chaves:
            continue
        if any(_prefixo_casa(k, c) or _similar(k, c) for c in chaves):
            continue
        fora.append(item)

    return fora


def auditar_texto(
    texto: str,
    acoes_no_banco: List[str],
) -> Optional[Dict[str, Any]]:
    """Conveniencia: devolve o resumo da auditoria, ou None se nao e uma RACI."""
    if not parece_raci_publicada(texto):
        return None
    publicados = parse_itens_publicados(texto)
    fora = itens_nao_absorvidos(publicados, acoes_no_banco)
    return {
        "itens_publicados": len(publicados),
        "itens_no_banco": len(acoes_no_banco),
        "nao_absorvidos": fora,
        "total_nao_absorvidos": len(fora),
    }
