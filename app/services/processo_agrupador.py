"""
Identidade de PROCESSO a partir de um alerta de e-mail.

POR QUE EXISTE
--------------
Os alertas processuais chegam de dois produtores que identificam o processo de
formas diferentes, e nenhum deles e o numero em todos os casos:

- **Astrea** (`astrea@aurum.com.br`) manda o **numero CNJ** no texto.
- **Jusbrasil** (`nao-responda@jusbrasil.com.br`) **nao manda numero**: manda as
  PARTES — *"Alexandro Bereska x NK Parking S/A — TJSP · Sao Paulo"*.

Medido nos 30 alertas dos ultimos 30 dias (19/10/26): só **13 tinham CNJ**. Uma
chave que exija numero deixaria 17 de fora — mais da metade — e o agrupamento
pareceria funcionar porque os 13 agrupam bem.

⚠️ E A NORMALIZACAO E O PONTO, nao o regex. Sem ela o MESMO processo quebra em
grupos distintos, porque o corpo do Jusbrasil prefixa a dupla com o rotulo da
secao: *"**Decisoes 1 processo** Alexandro Bereska x NK Parking S/A"* (7 ocorr.)
e *"Alexandro Bereska x NK Parking S/A"* (5) sao o mesmo caso, e um
agrupador ingenuo relataria 7 processos onde ha 5. Fragmentar e pior que nao
agrupar: dois cartoes do mesmo processo leem-se como dois processos.
"""

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

# NNNNNNN-DD.AAAA.J.TR.OOOO, com ou sem os separadores
_CNJ = re.compile(r"(\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4})")

# "Parte A x Parte B", ancorado no tribunal ou fim de sentenca. O ` x ` e a
# convencao do Jusbrasil e nao aparece por acidente em nome de empresa.
_PARTES = re.compile(
    r"([A-ZÁÉÍÓÚÂÊÔÃÕÇ][^.;\n]{2,58}?)\s+x\s+"
    r"([A-ZÁÉÍÓÚÂÊÔÃÕÇ][^.;\n]{2,58}?)"
    r"(?=\s+(?:TJ|TRT|TRF|STJ|STF|JEF|TST)|\.|$)"
)

# Rotulos de secao que o Jusbrasil poe ANTES da dupla de partes. Tudo ate o
# ultimo deles e contexto do e-mail, nao nome de parte.
_PREFIXOS_DE_SECAO = (
    "decisoes", "decisao", "novos processos encontrados", "novo processo",
    "processos encontrados", "movimentacoes", "publicacoes", "audiencias",
    "processo", "processos",
)

_NUM_SOLTO = re.compile(r"^\d+\s+")


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFD", str(texto or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _limpa_parte(nome: str) -> str:
    """
    Tira do nome da parte o rotulo de secao que o corpo do e-mail prefixou.

    "Decisoes 1 processo Alexandro Bereska" -> "Alexandro Bereska"
    "Novos processos encontrados 1 processo Jose Re" -> "Jose Re"

    Varre da esquerda removendo prefixo conhecido + eventual contador, e repete:
    o Jusbrasil empilha ("Decisoes" + "1 processo") e uma passada so deixaria
    metade do rotulo dentro do nome.
    """
    s = " ".join(str(nome or "").split())
    mudou = True
    while mudou and s:
        mudou = False
        n = _norm(s)
        for pref in _PREFIXOS_DE_SECAO:
            if n.startswith(pref):
                s = s[len(pref):].strip()
                s = _NUM_SOLTO.sub("", s).strip()
                mudou = True
                break
        # contador solto ("1 processo Alexandro") entre rotulos
        if not mudou and _NUM_SOLTO.match(s):
            s = _NUM_SOLTO.sub("", s).strip()
            mudou = True
    return s


def chave_processo(subject: str, content: str = "") -> Optional[Dict]:
    """
    Identidade do processo que o alerta trata, ou None se nao der para dizer.

    Devolve `{"tipo": "cnj"|"partes", "chave": <str canonica>, "rotulo": <str>}`.
    `chave` e para agrupar; `rotulo` e para exibir.

    None NAO e falha: os e-mails de marketing do mesmo remetente nao tratam de
    processo nenhum e devem sair da fila — contar um grupo "sem chave" junto com
    os processos reais inflaria a conta.
    """
    # CNJ pode vir no assunto (Astrea) ou no corpo — a concatenacao e inofensiva
    # porque o numero nao absorve texto vizinho.
    m = _CNJ.search(f"{subject or ''} {content or ''}")
    if m:
        digitos = re.sub(r"[^0-9]", "", m.group(1))
        if len(digitos) == 20:
            return {
                "tipo": "cnj",
                "chave": digitos,
                "rotulo": (f"{digitos[:7]}-{digitos[7:9]}.{digitos[9:13]}."
                           f"{digitos[13]}.{digitos[14:16]}.{digitos[16:20]}"),
            }

    # ⚠️ PARTES NUNCA NA CONCATENACAO, e este foi um defeito real desta versao:
    # buscando em `subject + content`, a EMENDA entre os dois virava nome de
    # parte. "Audiencia no processo" (assunto) + "ALEXANDRO BERESKA x ..."
    # (corpo) dava a parte "Audiencia no processo ALEXANDRO BERESKA", e o mesmo
    # processo fragmentava por assunto — exatamente o que o agrupamento existe
    # pra evitar. O corpo e onde o Jusbrasil poe as partes; o assunto dele e
    # generico de proposito. Tentar o assunto SOZINHO como fallback mantem o
    # caso raro coberto sem reabrir a emenda.
    for alvo in (content, subject):
        if not alvo:
            continue
        p = _PARTES.search(alvo)
        if not p:
            continue
        a = _limpa_parte(p.group(1))
        b = _limpa_parte(p.group(2))
        if a and b:
            # chave canonica: minuscula sem acento, pra "NK Parking S/A" e
            # "NK PARKING S/A" nao virarem dois processos
            return {
                "tipo": "partes",
                "chave": f"{_norm(a)} x {_norm(b)}",
                "rotulo": f"{a} x {b}",
            }

    return None


def agrupar(alertas: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Agrupa alertas por processo. Devolve `(grupos, sem_chave)`.

    Cada grupo: `{"tipo", "chave", "rotulo", "alertas": [...], "ultimo": ts,
    "n": int}`, ordenado pelo alerta mais RECENTE — o que importa num processo e
    a ultima movimentacao, nao o volume de avisos.

    Espera dicts com ao menos `subject`, `content` e `timestamp`.
    """
    por_chave: Dict[str, Dict] = {}
    sem_chave: List[Dict] = []

    for a in alertas:
        ident = chave_processo(a.get("subject"), a.get("content"))
        if not ident:
            sem_chave.append(a)
            continue
        g = por_chave.setdefault(ident["chave"], {
            "tipo": ident["tipo"], "chave": ident["chave"],
            "rotulo": ident["rotulo"], "alertas": [],
        })
        g["alertas"].append(a)
        # CNJ ganha do rotulo de partes quando os dois aparecem para a mesma
        # chave: o numero identifica sem ambiguidade.
        if ident["tipo"] == "cnj":
            g["tipo"], g["rotulo"] = "cnj", ident["rotulo"]

    grupos = []
    for g in por_chave.values():
        g["n"] = len(g["alertas"])
        g["ultimo"] = max(x.get("timestamp") for x in g["alertas"])
        grupos.append(g)
    grupos.sort(key=lambda g: g["ultimo"], reverse=True)
    return grupos, sem_chave
