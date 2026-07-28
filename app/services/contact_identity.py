"""
Contact Identity Resolution — cascata de identidade usada pelo sync do Google.

PROBLEMA QUE ISTO RESOLVE (diagnostico 25/07/26)
------------------------------------------------
`contacts.google_contact_id` e POR CONTA. A mesma pessoa na conta pessoal
(renato.almeida.prado@gmail.com) e na profissional (renato@almeida-prado.com)
tem `resourceName` diferente — entao o sync nunca casava as duas e INSERIA
uma ficha nova toda vez. Medido em prod: 2.011 grupos duplicados por telefone,
1.785 (89%) com uma ficha de cada conta.

O `sync_contacts_incremental` (o que o cron daily-sync das 5h chama) so
reconhecia por `google_contact_id`; o `sync_contacts_from_google` (importacao
completa) tinha um fallback por e-mail primario. 69% da base nao tem e-mail —
mas 7.112 contatos tem telefone, que nunca era consultado.

A CASCATA
---------
Antes de qualquer INSERT, resolve identidade nesta ordem:

  a) mapa multi-conta `{conta: resourceName}` guardado no contato
     (ver GOOGLE_IDS_COLUMN/GOOGLE_IDS_KEY) + a coluna escalar legada;
  b) e-mail normalizado (lowercase/trim), casando contra QUALQUER e-mail
     do contato — nao so o primario;
  c) telefone normalizado + nome similar (o que faltava).

Resolveu por (b) ou (c) -> ATUALIZA o contato existente e registra o
google_contact_id daquela conta no mapa, em vez de inserir.

GUARDA: TELEFONE NAO E CHAVE SOZINHO
------------------------------------
Caso real da base: 551135761505 tem "Douglas Bassi" e "Orestes Alves de
Almeida Prado" — e um FIXO COMPARTILHADO, pessoas distintas. Merge cego por
telefone teria corrompido dado. Por isso o tier (c) so casa com similaridade
de nome, e o criterio e mais rigoroso pra fixo que pra celular (ver
`names_match`).

Este modulo reusa os primitivos de `contact_dedup` (normalize_phone,
normalize_name_for_dedup). A separacao existe porque `contact_dedup` e
analise/merge em lote, offline, sobre a base inteira; aqui e resolucao
online, por registro, no caminho do sync.

KILL-SWITCH
-----------
  CONTACT_SYNC_IDENTITY_CASCADE=0  -> desliga a cascata inteira; os dois syncs
                                      voltam ao comportamento antigo.
  CONTACT_SYNC_PHONE_MATCH=0       -> mantem (a) e (b), desliga so o tier (c).
Default dos dois: ligado.
"""
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from services.contact_dedup import normalize_phone, normalize_name_for_dedup


# ============== Onde mora o mapa multi-conta ==============
#
# Sem DDL (decisao 25/07/26): o mapa {conta: resourceName} vai numa chave
# namespaced dentro de um JSONB que ja existe em `contacts`.
#
# Escolhido `empresa_dados` porque, entre os JSONB de objeto disponiveis:
#   - e objeto ('{}'), nao array — os demais candidatos (tags, categorias,
#     enderecos, enrichment_sources, datas_importantes...) sao arrays;
#   - esta praticamente vazio: 1 linha populada em 11.807 em prod;
#   - NAO tem nenhum consumidor em template (grep em app/templates = 0 hits);
#   - os consumidores Python leem apenas chaves de negocio especificas
#     (meeting_suggestion.py: address/endereco/location/localizacao),
#     entao uma chave com prefixo `_` nunca colide.
#
# `insights_ai` foi descartado apesar de tambem ser objeto: o front faz
# `Object.keys(insights).length === 0` pra decidir "Sem insights"
# (rap_contact_detail.html:4773) — escrever ali em ~11k contatos trocaria
# "Sem insights" por um painel vazio. Regressao visivel.
#
# O lugar certo e uma tabela/coluna propria; isso e backlog de DDL.
GOOGLE_IDS_COLUMN = "empresa_dados"
GOOGLE_IDS_KEY = "_google_contact_ids"


# ============== Kill-switches ==============

def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "off", "no")


def cascade_enabled() -> bool:
    """Cascata de identidade ligada? (CONTACT_SYNC_IDENTITY_CASCADE)"""
    return _env_flag("CONTACT_SYNC_IDENTITY_CASCADE", True)


def phone_match_enabled() -> bool:
    """Tier (c) telefone+nome ligado? (CONTACT_SYNC_PHONE_MATCH)"""
    return _env_flag("CONTACT_SYNC_PHONE_MATCH", True)


# ============== Limiares (documentados) ==============

# Uma linha com mais de N fichas distintas na base nao e identidade de
# ninguem: e ramal de empresa / e-mail de setor. Acima disso o tier abstem.
# 8 e folgado de proposito: a propria base tem duplicatas (ate 4-5 fichas da
# mesma pessoa), entao um corte apertado perderia casos legitimos.
SHARED_LINE_MAX_CONTACTS = 8

# Telefone so identifica se tiver DDD. Menos que isso e ambiguo no pais todo.
MIN_PHONE_DIGITS = 10

# Locais de e-mail que sao caixa de setor, nunca pessoa fisica.
ROLE_EMAIL_LOCALS = {
    "contato", "contact", "contatos", "info", "sac", "atendimento",
    "comercial", "vendas", "financeiro", "adm", "administrativo",
    "faleconosco", "suporte", "support", "rh", "noreply", "no-reply",
    "naoresponda", "cobranca", "juridico", "marketing", "compras",
}

# Particulas que nao carregam identidade em nome PT-BR.
_NAME_PARTICLES = {
    "de", "da", "do", "das", "dos", "e", "del", "di", "du",
    "la", "le", "van", "von", "y", "dell",
}

# Titulos/pronomes de tratamento. NAO identificam ninguem e, pior, ocupavam a
# posicao de PRIMEIRO token — que o `names_match` exige que bata. Medido em
# 26/07: "Dra. Vanelise" tinha primeiro token 'dra', entao a comparacao contra
# "Wanelise B Carvalho" morria antes de olhar o nome. Descartar e seguro: um
# titulo nunca e a evidencia que distingue duas pessoas na mesma linha.
_NAME_TITLES = {
    "dr", "dra", "drs", "sr", "sra", "srta", "prof", "profa", "professor",
    "professora", "eng", "enga", "adv", "advogado", "advogada", "cel",
    "gen", "pe", "padre", "irma", "dom", "exmo", "ilmo", "mestre", "med",
}


# Nomes que NAO identificam ninguem: o cartao do proprio dono da agenda, que o
# Google exporta como "Eu"/"Me"/"Meu perfil". 27/07 — o cartao da agenda veio
# como `Me Eu` #26634 carregando o telefone da Manuela #4067; `names_match`
# corretamente disse "nao casa" (os nomes SAO diferentes) e a cascata inseriu,
# partindo o historico dela (904 msgs) e desfazendo o merge do dia anterior.
# O defeito nao esta na comparacao — esta em tratar um rotulo vazio como nome.
_PLACEHOLDER_NAMES = {
    "me", "eu", "myself", "voce", "self", "meu perfil", "my profile",
    "eu mesmo", "me eu", "eu me", "owner", "dono", "perfil", "profile",
    "sem nome", "no name", "unnamed", "desconhecido", "unknown", "contato",
    "contact", "novo contato", "new contact",
}


# ============== Nome ==============

def is_placeholder_name(name: str) -> bool:
    """O nome e um rotulo vazio em vez de identificar alguem?

    Conservador de proposito: casa o nome INTEIRO normalizado contra a lista,
    nunca um token isolado. "Eduardo Melo" nao vira placeholder por conter
    "me"; "Maria Eunice" nao vira por conter "eu". So o rotulo puro passa.
    """
    base = normalize_name_for_dedup(name or "")
    base = re.sub(r"[^a-z0-9 ]+", " ", base)
    base = " ".join(base.split())
    if not base:
        return True
    if base in _PLACEHOLDER_NAMES:
        return True
    # "Me"/"Eu" sozinho tambem chega com token unico apos normalizacao.
    toks = base.split()
    return len(toks) == 1 and toks[0] in _PLACEHOLDER_NAMES


def significant_tokens(name: str) -> List[str]:
    """
    Tokens de um nome que efetivamente identificam alguem.

    Reusa `normalize_name_for_dedup` (tira acento, minusculiza, remove
    sufixos Jr/Filho/Neto/II...) e ainda descarta pontuacao, particulas
    (de/da/do/e...) e iniciais de 1 letra ("Renato A. Prado" -> renato, prado).
    """
    base = normalize_name_for_dedup(name or "")
    base = re.sub(r"[^a-z0-9 ]+", " ", base)
    toks = [
        t for t in base.split()
        if len(t) > 1 and t not in _NAME_PARTICLES and t not in _NAME_TITLES
    ]
    # Se sobrou NADA (ficha chamada so "Dr." ou "Sra."), devolve os tokens sem
    # o filtro de titulo — melhor comparar algo do que virar lista vazia, que
    # `names_match` trata como "nao casa" e viraria insercao silenciosa.
    if not toks:
        toks = [t for t in base.split() if len(t) > 1 and t not in _NAME_PARTICLES]
    return toks


# ============== O 9o digito (migracao Anatel) ==============
#
# Ate 2013-2016 o celular brasileiro tinha 8 digitos de assinante; a Anatel
# prefixou um 9 e ele passou a ter 9. `11 8415-3337` e `11 98415-3337` sao
# A MESMA LINHA — nao sao dois numeros parecidos.
#
# O que isso custava aqui (medido em prod 28/07/26): a base tem 226 numeros no
# formato antigo, e como `normalize_phone` nao equipara as duas eras, o indice
# `ContactIndex.by_phone` guardava a mesma linha sob DUAS chaves. Consequencia:
# o tier (c) da cascata nunca via os dois lados juntos, e o sync criava ficha
# nova. Sao **13 pessoas com ficha duplicada por este motivo e mais nenhum**
# (Carla Werkhaizer, Rodrigo Maia, Fredy Schaible, Rafael Prado, Paloma
# Pinheiro, Isaac, Daniel Kras, Sidnei Madeira, Maria Guevara, Fernanda,
# Pousada Tatuapara, Marcos Ribeiro).
#
# COMO DISTINGUIR CELULAR ANTIGO DE FIXO: pelo primeiro digito do assinante.
# A Anatel reserva 2-5 pra fixo e 6-9 pra movel. Confere com a base: dos 869
# numeros BR de 8 digitos de assinante, 643 comecam com 2-5 (fixo) e 226 com
# 7/8/9 (celular antigo) — nenhum com 6.
#
# RISCO ASSUMIDO: fixos MUITO antigos de cidades pequenas chegaram a comecar
# com 6 ou 7 antes da padronizacao. Sao 5 numeros com prefixo 7 na base. Se um
# deles for fixo, a forma canonica dele vira um celular que nao existe — e um
# numero que nao casa com nada, nao um match errado. Pra virar dano precisaria
# existir um celular real com aquele numero E o nome bater (o tier (c) exige
# `names_match`). Nao reescrevemos o dado gravado: a canonizacao existe so pra
# COMPARAR.
_BR_MOBILE_PREFIXES = ("6", "7", "8", "9")


def canonical_br_phone(digits: Any) -> str:
    """
    Forma canonica de um telefone pra fins de COMPARACAO: celular brasileiro
    no formato antigo recebe o 9 que a Anatel prefixou.

        553599851122   ->  5535999851122
        5535999851122  ->  5535999851122   (ja canonico)
        551130624437   ->  551130624437    (fixo, intocado)

    Nao normaliza DDI nem formata: entra e sai so-digitos.
    """
    d = re.sub(r"\D", "", str(digits or ""))
    # 55 + DDD(2) + assinante(8), assinante comecando por prefixo movel
    if len(d) == 12 and d.startswith("55") and d[4] in _BR_MOBILE_PREFIXES:
        return d[:4] + "9" + d[4:]
    # DDD(2) + assinante(8), sem DDI
    if len(d) == 10 and d[2] in _BR_MOBILE_PREFIXES:
        return d[:2] + "9" + d[2:]
    return d


def phone_kind(normalized: str) -> str:
    """
    Classifica um telefone JA normalizado por `normalize_phone`.

    'mobile'   -> celular (assinante de 9 digitos, ou de 8 no formato
                  pre-Anatel, que se reconhece pelo prefixo movel)
    'landline' -> fixo (assinante de 8 digitos comecando em 2-5)
    'unknown'  -> internacional / formato que nao da pra afirmar

    O ramo do celular ANTIGO importa alem da etiqueta: `names_match` e mais
    rigoroso pra 'landline' (por causa do fixo compartilhado Douglas x
    Orestes) e a guarda de linha compartilhada tambem. Chamar 226 celulares de
    fixo era aplicar a eles o criterio de uma linha que varias pessoas dividem
    — celular e pessoal.
    """
    d = normalized or ""
    if not d.isdigit():
        return "unknown"
    if d.startswith("55"):
        if len(d) == 13:
            return "mobile"
        if len(d) == 12:
            return "mobile" if d[4] in _BR_MOBILE_PREFIXES else "landline"
        return "unknown"
    if len(d) == 11 and d[2] == "9":
        return "mobile"
    if len(d) == 10:
        return "mobile" if d[2] in _BR_MOBILE_PREFIXES else "landline"
    return "unknown"


def _edit_distance_le1(a: str, b: str) -> bool:
    """Levenshtein <= 1 (uma troca, insercao ou remocao). Curto-circuita cedo."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:  # uma substituicao
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    # uma insercao/remocao: caminha em paralelo com um deslocamento
    short, long_ = (a, b) if la < lb else (b, a)
    i = j = 0
    folga = True
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
            j += 1
        elif folga:
            folga = False
            j += 1
        else:
            return False
    return True


# Primeiro nome so aceita variante ortografica a partir deste tamanho. Curto
# (Ana/Ane, Rita/Rito, Luis/Luiz... e tambem Ana/Ada) tem distancia 1 entre
# nomes de pessoas DIFERENTES com frequencia alta; a partir de 6 letras a
# colisao vira rara e o ganho real aparece (Vanelise/Wanelise, Katia/Catia nao
# entra, Cristina/Christina entra).
_FIRST_NAME_VARIANT_MIN_LEN = 6


def _first_names_match(a: str, b: str) -> bool:
    """Primeiro nome igual, ou variante ortografica de 1 caractere.

    Why: a mesma pessoa aparece com grafia diferente entre as agendas —
    "Dra. Vanelise" (conta pessoal) x "Wanelise B Carvalho" (profissional),
    medido 26/07 na advogada da penhora. Exigir igualdade exata recriava a
    duplicata em todo sync completo.
    """
    if a == b:
        return True
    if len(a) < _FIRST_NAME_VARIANT_MIN_LEN or len(b) < _FIRST_NAME_VARIANT_MIN_LEN:
        return False
    return _edit_distance_le1(a, b)


def _is_initials_of(token: str, others: List[str]) -> bool:
    """`token` e a sigla das iniciais de `others`? ("dap" <- dansieri almeida prado)

    Why: "Manuela DAP" x "Manuela Dansieri de Almeida Prado" (a filha do Renato)
    nao tinha NENHUM token em comum alem do primeiro nome, entao o tier celular
    recusava. Sigla e evidencia forte e barata: exige que cada letra case, em
    ordem, com a inicial de um sobrenome distinto.
    """
    if not (2 <= len(token) <= 4) or not others:
        return False
    iniciais = [t[0] for t in others]
    idx = 0
    for ch in token:
        while idx < len(iniciais) and iniciais[idx] != ch:
            idx += 1
        if idx >= len(iniciais):
            return False
        idx += 1
    return True


def names_match(name_a: str, name_b: str, kind: str = "unknown") -> bool:
    """
    Os dois nomes podem ser a MESMA pessoa, dado que compartilham um telefone?

    Primeiro nome tem que bater sempre — e a parte do nome que menos varia
    entre agendas (o sobrenome e que aparece abreviado, faltando ou a mais).

    CELULAR ('mobile') — criterio frouxo:
        primeiro nome igual + (pelo menos um outro token em comum
        OU um dos lados ser mononimo).
        Por que frouxo: no Brasil celular e 1:1 com pessoa (portabilidade,
        aparelho pessoal). A chance de duas pessoas DIFERENTES dividirem o
        mesmo celular E terem o mesmo primeiro nome E um sobrenome em comum
        e desprezivel. O mononimo ("Andressa" x "Andressa Souza") passa
        so aqui, apoiado no celular como evidencia forte.

    FIXO ('landline') e DESCONHECIDO — criterio rigoroso:
        o conjunto de tokens do nome mais curto tem que estar CONTIDO no do
        mais longo, e o mais curto precisa de >= 2 tokens (mononimo nao passa).
        Por que rigoroso: fixo e linha de casa/empresa. E exatamente o modo de
        falha observado na base — 551135761505 com "Douglas Bassi" e
        "Orestes Alves de Almeida Prado". Contencao total captura "mesma
        pessoa escrita mais curta" (Orestes Alves de Almeida Prado x Orestes
        Almeida Prado) e rejeita "duas pessoas no mesmo endereco"
        (Ana Silva x Ana Costa), que e o que um limiar por percentual deixaria
        passar. 'unknown' herda o rigoroso porque nao da pra afirmar que a
        linha e pessoal.
    """
    ta = significant_tokens(name_a)
    tb = significant_tokens(name_b)
    if not ta or not tb:
        return False

    # Nome identico token-a-token casa em qualquer tipo de linha, inclusive
    # mononimo. Duas pessoas DISTINTAS com o nome inteiro igual dividindo a
    # mesma linha e caso de laboratorio; ja duas fichas "Carol" no mesmo fixo
    # sao, na base real, a mesma Carol importada duas vezes. Sem esta saida o
    # criterio rigoroso rejeitava ate ficha identica ("Voitel" x "Voitel").
    # O teto SHARED_LINE_MAX_CONTACTS limita o estrago se for um ramal.
    if ta == tb:
        return True

    if not _first_names_match(ta[0], tb[0]):
        return False

    sa, sb = set(ta), set(tb)
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)

    if kind == "mobile":
        if len(shorter) == 1:
            return True
        if (sa & sb) - {ta[0], tb[0]}:
            return True
        # Sigla dos sobrenomes conta como token em comum ("Manuela DAP").
        resto_a, resto_b = ta[1:], tb[1:]
        return any(
            _is_initials_of(t, resto_b) for t in resto_a
        ) or any(
            _is_initials_of(t, resto_a) for t in resto_b
        )

    if len(shorter) < 2:
        return False
    return shorter.issubset(longer)


# ============== Extracao dos campos do contato ==============

def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


def contact_emails(contact: Dict) -> List[str]:
    """E-mails normalizados (lowercase/trim), sem os de setor."""
    out = []
    for item in _as_list(contact.get("emails")):
        raw = item.get("email", "") if isinstance(item, dict) else str(item)
        email = (raw or "").strip().lower()
        if not email or "@" not in email:
            continue
        local = email.split("@", 1)[0]
        if local in ROLE_EMAIL_LOCALS:
            continue
        if email not in out:
            out.append(email)
    return out


def contact_phones(contact: Dict) -> List[str]:
    """
    Telefones normalizados com DDD, deduplicados, na forma CANONICA.

    Canonico (`canonical_br_phone`) e o que faz a mesma linha nas duas eras da
    numeracao brasileira cair na MESMA chave do indice `by_phone`. Sem isso,
    `11 8415-3337` e `11 98415-3337` viravam duas chaves e a cascata nunca
    enxergava que sao a mesma pessoa — 13 duplicatas da base nasceram assim.
    """
    out = []
    for item in _as_list(contact.get("telefones")):
        raw = item.get("number", "") if isinstance(item, dict) else str(item)
        norm = normalize_phone(raw or "")
        if len(norm) < MIN_PHONE_DIGITS:
            continue
        norm = canonical_br_phone(norm)
        if norm not in out:
            out.append(norm)
    return out


def google_ids_map(contact: Dict) -> Dict[str, List[str]]:
    """Le o mapa {conta: [resourceNames]} de um row de contacts.

    27/07 — ERA `{conta: gid}` ESCALAR, E ISSO CRIAVA DUPLICATA. A mesma pessoa
    pode ter DUAS fichas na MESMA agenda Google (a `Francine Harumi Kaga` tinha,
    e o Renato tinha 3 do filho na profissional). Com um slot escalar por conta,
    o 2o resourceName nunca era reconhecido: o tier de gid falhava, a cascata
    caia pros tiers seguintes e, quando o telefone ainda nao estava propagado,
    INSERIA ficha nova. Foi assim que nasceu a #26520 (criada 12:11 na 1a run;
    a #2107 so recebeu o telefone as 15:32, na 2a). Alcance medido em prod:
    28 fichas com gid escalar fora do proprio mapa.

    LEITURA RETROCOMPATIVEL: aceita o formato legado (`{conta: "gid"}`) e o novo
    (`{conta: ["gid1","gid2"]}`), sempre devolvendo lista. Por isso a migracao
    dos ~9,5k registros ja gravados NAO e pre-requisito — os dois formatos
    convivem, e cada `link_google_id` reescreve no formato novo naturalmente.
    """
    blob = _as_dict(contact.get(GOOGLE_IDS_COLUMN))
    raw = blob.get(GOOGLE_IDS_KEY)
    out: Dict[str, List[str]] = {}
    for account, gids in _as_dict(raw).items():
        if not account:
            continue
        # str = formato legado; list = formato novo. Qualquer outra coisa (int,
        # dict, None) e dado corrompido e e ignorada em vez de virar "None".
        valores = [gids] if isinstance(gids, str) else (gids if isinstance(gids, list) else [])
        limpos: List[str] = []
        for gid in valores:
            if not gid or not isinstance(gid, (str, int)):
                continue
            s = str(gid)
            if s not in limpos:
                limpos.append(s)
        if limpos:
            out[str(account)] = limpos
    return out


def google_ids_all(contact: Dict) -> List[str]:
    """Todos os resourceNames da ficha, de todas as contas, sem repetir.

    Pro indice, que so precisa saber "este gid aponta pra este contato" — nao
    de que conta ele veio.
    """
    todos: List[str] = []
    for gids in google_ids_map(contact).values():
        for gid in gids:
            if gid not in todos:
                todos.append(gid)
    return todos


def google_ids_for_account(contact: Dict, account_email: str) -> List[str]:
    """resourceNames desta ficha NAQUELA conta (lista; pode ter 2+)."""
    return list(google_ids_map(contact).get(account_email) or [])


def merge_json_lists(existing: Any, incoming: Any, key: str,
                     normalizer=None) -> List[Dict]:
    """
    Uniao de listas de e-mail/telefone preservando o que ja existia.

    Necessario porque agora uma MESMA linha de `contacts` e alimentada pelas
    DUAS contas do Google. O sync antigo substituia `emails`/`telefones` pelo
    que veio da conta que rodou — com a linha compartilhada isso viraria
    ping-pong diario, cada sync apagando o que o outro escreveu.
    Preserva a ordem: o que ja estava primeiro, o novo depois.
    """
    out: List[Dict] = []
    seen = set()

    for source in (_as_list(existing), _as_list(incoming)):
        for item in source:
            if isinstance(item, dict):
                raw = item.get(key, "")
                obj = item
            else:
                raw = str(item)
                obj = {key: raw}
            raw = (raw or "").strip()
            if not raw:
                continue
            token = normalizer(raw) if normalizer else raw.lower()
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(obj)

    return out


def merge_contexto(existing: Optional[str], incoming: Optional[str]) -> str:
    """
    Uniao dos contextos ('personal' + 'professional' -> 'personal,professional').

    Sem isso a linha compartilhada oscilaria de contexto a cada sync. O formato
    com virgula ja e o que `merge_contacts` produz e o que
    `propagate_contact_to_google` espera ('personal' in contexto).
    Deduplica os tokens — a versao de `merge_contacts` acumulava repeticoes
    ('personal,professional,professional' existe em 48 linhas da base).
    """
    tokens = []
    for blob in (existing or "", incoming or ""):
        for tok in str(blob).split(","):
            tok = tok.strip()
            if tok and tok not in tokens:
                tokens.append(tok)
    return ",".join(sorted(tokens))


# ============== Indice de identidade ==============

class ContactIndex:
    """
    Snapshot em memoria da base de contatos, indexado por google id / e-mail /
    telefone. Construido UMA vez por execucao de sync.

    Por que em memoria e nao uma query por contato: a busca por telefone exige
    normalizar `telefones` (JSONB, sem indice) — medido em ~60ms por contato
    numa base de 11.8k. Num full resync de 5k contatos isso e 300s+, acima do
    teto da Vercel. Carregar 11.8k linhas uma vez custa uma query.

    O indice e mutavel: `register()` mantem ele em dia conforme o sync insere
    ou atualiza, pra que dois registros do MESMO lote (ex.: a mesma pessoa nas
    duas contas, num full resync) casem entre si.
    """

    def __init__(self):
        self.by_gid: Dict[str, int] = {}
        self.by_email: Dict[str, List[int]] = defaultdict(list)
        self.by_phone: Dict[str, List[int]] = defaultdict(list)
        self.names: Dict[int, str] = {}
        self.loaded = False

    # ---- construcao ----

    def add_contact(self, row: Dict) -> None:
        contact_id = row.get("id")
        if contact_id is None:
            return

        self.names[contact_id] = row.get("nome") or ""

        gid = row.get("google_contact_id")
        if gid:
            self.by_gid.setdefault(str(gid), contact_id)
        # TODOS os gids da ficha, nao um por conta: e exatamente o 2o gid da
        # mesma agenda que o formato escalar perdia, fazendo o tier de gid
        # falhar e a cascata inserir ficha nova (ver google_ids_map).
        for mapped in google_ids_all(row):
            self.by_gid.setdefault(mapped, contact_id)

        for email in contact_emails(row):
            if contact_id not in self.by_email[email]:
                self.by_email[email].append(contact_id)

        for phone in contact_phones(row):
            if contact_id not in self.by_phone[phone]:
                self.by_phone[phone].append(contact_id)

    def load(self, cursor) -> "ContactIndex":
        """
        Carrega a base. SELECT puro — nao escreve nada.

        Custo medido (25/07): ~9s contra o Neon a partir da maquina local,
        11.8k linhas. So roda quando algum registro NAO casou pelo
        google_contact_id — num dia normal (34 novos em 30d) nem chega a ser
        construido. Fica dentro do orcamento de 90s do step_contacts.

        Contatos sem NENHUM identificador (sem gid, sem e-mail, sem telefone)
        ficam de fora: nao ha tier que possa resolve-los. Sao ~880 de 11.8k.
        """
        cursor.execute(f"""
            SELECT id, nome, emails, telefones, google_contact_id,
                   {GOOGLE_IDS_COLUMN}
            FROM contacts
            WHERE google_contact_id IS NOT NULL
               OR (jsonb_typeof(emails) = 'array'
                   AND jsonb_array_length(emails) > 0)
               OR (jsonb_typeof(telefones) = 'array'
                   AND jsonb_array_length(telefones) > 0)
               OR jsonb_exists({GOOGLE_IDS_COLUMN}, %s)
        """, (GOOGLE_IDS_KEY,))
        for row in cursor.fetchall():
            self.add_contact(dict(row))
        self.loaded = True
        return self

    def ensure_loaded(self, cursor) -> None:
        if not self.loaded:
            self.load(cursor)

    def register(self, contact_id: int, contact: Dict,
                 account_email: str = None, gid: str = None) -> None:
        """Reflete no indice um contato recem inserido/atualizado."""
        row = dict(contact)
        row["id"] = contact_id
        if gid:
            row["google_contact_id"] = gid
        self.names[contact_id] = row.get("nome") or self.names.get(contact_id, "")
        self.add_contact(row)

    # ---- resolucao ----

    def _resolve_by_gid(self, contact: Dict, account_email: str) -> Optional[Dict]:
        gid = contact.get("google_contact_id")
        if not gid:
            return None
        found = self.by_gid.get(str(gid))
        if found is None:
            return None
        return {"contact_id": found, "matched_by": "google_id_map", "detail": str(gid)}

    def _resolve_by_email(self, contact: Dict) -> Optional[Dict]:
        for email in contact_emails(contact):
            candidates = self.by_email.get(email) or []
            if not candidates:
                continue
            if len(candidates) > SHARED_LINE_MAX_CONTACTS:
                # e-mail que aparece em dezenas de fichas nao identifica ninguem
                continue
            # base ja pode ter duplicatas do mesmo e-mail; o menor id vence
            return {
                "contact_id": min(candidates),
                "matched_by": "email",
                "detail": email,
            }
        return None

    def _resolve_by_phone(self, contact: Dict) -> Optional[Dict]:
        nome = contact.get("nome") or ""
        placeholder = is_placeholder_name(nome)
        for phone in contact_phones(contact):
            candidates = self.by_phone.get(phone) or []
            if not candidates or len(candidates) > SHARED_LINE_MAX_CONTACTS:
                continue

            kind = phone_kind(phone)

            # Nome-placeholder ("Me", "Eu", "Meu perfil"): o cartao do PROPRIO
            # dono da agenda, que o Google exporta com nome inutil. Comparar por
            # nome aqui e garantia de nao casar — foi assim que `Me Eu` #26634
            # nasceu com o telefone da Manuela #4067 e partiu de novo o historico
            # dela (904 msgs), desfazendo na pratica o merge do dia anterior.
            # Com UM candidato so, o telefone e evidencia suficiente e o nome de
            # origem nao contradiz nada (ele nao diz nada). Com 2+, abstem: nao
            # ha como escolher, e inserir seria repetir o defeito.
            if placeholder:
                if len(candidates) == 1:
                    return {
                        "contact_id": candidates[0],
                        "matched_by": "phone_placeholder_name",
                        "detail": f"{phone}/{kind}/nome_lixo:{nome[:20]}",
                    }
                continue

            passed = [
                cid for cid in candidates
                if names_match(nome, self.names.get(cid, ""), kind)
            ]

            if len(passed) == 1:
                return {
                    "contact_id": passed[0],
                    "matched_by": "phone_name",
                    "detail": f"{phone}/{kind}",
                }
            if len(passed) > 1:
                # Varios candidatos passaram contra o nome que CHEGOU. Duas
                # situacoes muito diferentes se escondem aqui:
                #
                #  (1) eles sao equivalentes ENTRE SI ("Diana Berezin" x
                #      "Diana Berezin" x "Diana Berezin", mesmo telefone) —
                #      isso nao e ambiguidade, e uma duplicata que a base JA
                #      tem. Abster-se aqui insere mais uma ficha e agrava o
                #      grupo a cada sync: medido em 26/07, 2.218 telefones
                #      nessa condicao (5.877 fichas) = 2.218 gatilhos armados.
                #      Casa com o menor id, exatamente como `_resolve_by_email`
                #      ja resolve o mesmo empate.
                #
                #  (2) eles NAO sao equivalentes entre si ("Ana Silva" x
                #      "Ana Costa" no fixo de casa) — ambiguidade real, e
                #      abster continua sendo mais barato que errar.
                #
                # Nota: o caso que motivou a guarda (fixo 551135761505,
                # Douglas Bassi x Orestes) nem chega aqui — `names_match` ja
                # devolve um so candidato. Quem protege ali e o criterio de
                # nome, nao esta regra.
                if self._all_equivalent(passed, kind):
                    return {
                        "contact_id": min(passed),
                        "matched_by": "phone_name_dup",
                        "detail": f"{phone}/{kind}/dup:{len(passed)}",
                    }
                return {
                    "contact_id": None,
                    "matched_by": None,
                    "detail": f"ambiguo:{phone}",
                    "ambiguous": [self.names.get(c, "") for c in passed],
                }
        return None

    def _all_equivalent(self, contact_ids: List[int], kind: str) -> bool:
        """Todos estes contatos sao a mesma pessoa entre si?

        Exige equivalencia par-a-par, nao so contra o nome de entrada:
        `names_match` nao e transitivo (um mononimo casa com dois sobrenomes
        diferentes num celular), e aceitar por transitividade fundiria pessoas
        distintas. Com o teto de SHARED_LINE_MAX_CONTACTS=8 sao no maximo 28
        comparacoes de string.
        """
        for i, a in enumerate(contact_ids):
            for b in contact_ids[i + 1:]:
                if not names_match(self.names.get(a, ""), self.names.get(b, ""), kind):
                    return False
        return True

    def resolve(self, contact: Dict, account_email: str) -> Dict:
        """
        Roda a cascata a/b/c. Devolve sempre um dict; `contact_id` None
        significa "nao reconheci, pode inserir".
        """
        empty = {"contact_id": None, "matched_by": None, "detail": None}

        hit = self._resolve_by_gid(contact, account_email)
        if hit:
            return hit

        hit = self._resolve_by_email(contact)
        if hit:
            return hit

        if phone_match_enabled():
            hit = self._resolve_by_phone(contact)
            if hit:
                return hit

        return empty


# ============== Escrita do mapa multi-conta ==============

def google_ids_blob(account_email: str, gid: str) -> str:
    """JSON pronto pro INSERT de um contato novo. Grava LISTA (ver
    google_ids_map): a mesma conta pode acumular um 2o resourceName depois."""
    if not account_email or not gid:
        return json.dumps({})
    return json.dumps({GOOGLE_IDS_KEY: {account_email: [gid]}})


def link_google_id(cursor, contact_id: int, account_email: str, gid: str) -> None:
    """
    Registra `{conta: resourceName}` no mapa do contato, sem apagar as outras
    contas, e mantem a coluna escalar `google_contact_id` preenchida pros
    consumidores legados.

    A escalar so e preenchida se estiver NULL: sobrescrever faria as duas
    contas brigarem pela coluna a cada sync, e ela tem UNIQUE constraint.
    """
    if not contact_id or not account_email or not gid:
        return

    # ACUMULA na lista daquela conta em vez de sobrescrever o slot. O UPDATE e
    # atomico (read-modify-write em Python abriria corrida entre o sync e o
    # webhook). O CASE normaliza o formato legado no caminho: string vira array
    # de 1, ausente vira array vazio — por isso nao ha migracao a rodar.
    # DISTINCT + ORDER BY mantem a lista sem repetido e deterministica.
    cursor.execute(f"""
        UPDATE contacts
        SET {GOOGLE_IDS_COLUMN} = jsonb_set(
                COALESCE({GOOGLE_IDS_COLUMN}, '{{}}'::jsonb),
                %s,
                COALESCE({GOOGLE_IDS_COLUMN} -> %s, '{{}}'::jsonb)
                    || jsonb_build_object(%s::text, (
                        SELECT jsonb_agg(DISTINCT v ORDER BY v)
                        FROM jsonb_array_elements(
                            CASE jsonb_typeof({GOOGLE_IDS_COLUMN} -> %s -> %s)
                                WHEN 'array'  THEN {GOOGLE_IDS_COLUMN} -> %s -> %s
                                WHEN 'string' THEN jsonb_build_array({GOOGLE_IDS_COLUMN} -> %s -> %s)
                                ELSE '[]'::jsonb
                            END || jsonb_build_array(%s::text)
                        ) AS v
                    )),
                true
            ),
            google_contact_id = COALESCE(google_contact_id, %s)
        WHERE id = %s
    """, (
        "{" + GOOGLE_IDS_KEY + "}",
        GOOGLE_IDS_KEY,
        account_email,
        GOOGLE_IDS_KEY, account_email,
        GOOGLE_IDS_KEY, account_email,
        GOOGLE_IDS_KEY, account_email,
        gid,
        gid,
        contact_id,
    ))


def unlink_google_id(cursor, account_email: str, gid: str) -> Optional[str]:
    """
    O contato foi APAGADO numa das contas do Google. Desfaz o vinculo daquela
    conta e so remove a ficha se nao sobrou nenhuma outra conta apontando pra
    ela.

    Sem isto, agora que uma mesma linha e alimentada pelas duas contas, apagar
    o contato na conta pessoal levaria junto a ficha inteira (e o historico
    pendurado nela) que a conta profissional ainda usa. O sync antigo fazia
    `DELETE FROM contacts WHERE google_contact_id = %s` sem essa checagem —
    era inofensivo so porque cada linha pertencia a uma conta so.

    Devolve 'deleted', 'unlinked' ou None (nao achou).
    """
    if not gid:
        return None

    # O `->> %s = %s` so casava quando o valor era STRING; com lista ele devolve
    # NULL e a ficha nao seria achada. O containment (`@>`) cobre os dois
    # formatos: array que contem o gid, e string igual ao gid.
    cursor.execute(f"""
        SELECT id, google_contact_id, {GOOGLE_IDS_COLUMN}
        FROM contacts
        WHERE google_contact_id = %s
           OR {GOOGLE_IDS_COLUMN} -> %s -> %s @> to_jsonb(%s::text)
           OR {GOOGLE_IDS_COLUMN} -> %s ->> %s = %s
        LIMIT 1
    """, (gid,
          GOOGLE_IDS_KEY, account_email, gid,
          GOOGLE_IDS_KEY, account_email, gid))
    row = cursor.fetchone()
    if not row:
        return None

    row = dict(row)
    contact_id = row["id"]

    # Tira APENAS este gid — nao a conta inteira. A mesma conta pode ter um 2o
    # resourceName pra esta pessoa (e a razao desta frente): apagar uma das duas
    # fichas no Google nao pode desvincular a que sobrou, nem levar junto a
    # ficha do INTEL com todo o historico pendurado nela.
    remaining: Dict[str, List[str]] = {}
    for acc, gids in google_ids_map(row).items():
        sobra = [g for g in gids if not (acc == account_email and g == gid)]
        if sobra:
            remaining[acc] = sobra

    if not remaining:
        cursor.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
        return "deleted" if cursor.rowcount > 0 else None

    # Ainda ha gid vivo (outra conta, OU a mesma conta com um 2o resourceName):
    # tira so este e, se a coluna escalar apontava pro id apagado, repassa pra
    # um gid vivo.
    fallback_gid = next(iter(remaining.values()))[0]
    cursor.execute(f"""
        UPDATE contacts
        SET {GOOGLE_IDS_COLUMN} = jsonb_set(
                COALESCE({GOOGLE_IDS_COLUMN}, '{{}}'::jsonb),
                %s, %s::jsonb, true
            ),
            google_contact_id = CASE
                WHEN google_contact_id = %s THEN %s ELSE google_contact_id
            END,
            atualizado_em = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (
        "{" + GOOGLE_IDS_KEY + "}",
        json.dumps(remaining),
        gid,
        fallback_gid,
        contact_id,
    ))
    return "unlinked"


# ============== Busca online por telefone (WhatsApp -> ficha) ==============
#
# PROBLEMA (reproduzido pela CoS em 28/07/26, medido em prod no mesmo dia)
# ----------------------------------------------------------------------
# O WhatsApp entrega o numero CRU (`5511992526344`). O Google entrega
# FORMATADO (`+55 (11) 99252-6344`). Oito call-sites comparavam os dois com
#
#     WHERE telefones::text LIKE '%<ultimos 8 digitos do numero cru>%'
#
# e o hifen do formato do Google cai EXATAMENTE no meio desses 8 digitos
# (celular BR e `9XXXX-XXXX`: os ultimos 8 sao `XXXX-XXXX`). Ou seja: nao era
# um caso de borda, era 100% dos numeros formatados — 3.659 dos 9.149 numeros
# da base (40%).
#
# Efeito por consumidor: o webhook de WA inbound criava ficha fantasma
# "Desconhecido +55..." pra quem JA tinha ficha (18 das 44 fantasmas da base
# tinham ficha real — Gustavo Glasser, Piccino, Francine, Raimundo...); o
# sync de grupo deixava `group_messages.contact_id` NULL; a lista de
# participantes de grupo escondia gente conhecida.
#
# A correcao e comparar SO DIGITOS dos dois lados, por campo `number` do
# JSONB — nao pelo texto do JSON inteiro, que carrega `type`/`whatsapp` e
# poderia casar por acidente.

# Quantos digitos finais comparar. 8 e o mesmo criterio que os call-sites ja
# usavam, mantido de proposito: esta frente conserta a NORMALIZACAO, nao
# afrouxa nem aperta o criterio. 8 absorve as duas divergencias comuns entre
# origens — o 9o digito de celular BR e o 0 de operadora — sem exigir DDI.
PHONE_MATCH_DIGITS = 8

# Condicao SQL reutilizavel. `{alias}` e a tabela de contatos no escopo do
# chamador; o placeholder recebe a chave de `phone_lookup_key`.
_PHONE_MATCH_COND = """jsonb_typeof({alias}.telefones) = 'array' AND EXISTS (
            SELECT 1 FROM jsonb_array_elements({alias}.telefones) _t
             WHERE right(regexp_replace(_t->>'number', '[^0-9]', '', 'g'), {n}) = %s
        )"""

# Canonizacao do 9o digito em SQL — o espelho de `canonical_br_phone`, pra
# que o desempate compare as duas eras da numeracao como iguais em vez de
# tratar `553599851122` e `5535999851122` como numeros diferentes.
_SQL_CANON = """(CASE
              WHEN length({d}) = 12 AND left({d}, 2) = '55'
                   AND substr({d}, 5, 1) IN ('6','7','8','9')
                   THEN substr({d}, 1, 4) || '9' || substr({d}, 5)
              WHEN length({d}) = 10 AND substr({d}, 3, 1) IN ('6','7','8','9')
                   THEN substr({d}, 1, 2) || '9' || substr({d}, 3)
              ELSE {d} END)"""

_SQL_DIGITS = "regexp_replace(_t->>'number', '[^0-9]', '', 'g')"

# Desempate deterministico. O LIKE antigo fazia `LIMIT 1` sem ORDER BY, entao
# com mais de um candidato a ficha escolhida dependia da ordem fisica da
# tabela. Prefere o numero que bate INTEIRO na forma canonica (match forte,
# imune a colisao de sufixo entre um numero BR e um internacional) e, no
# empate, o menor id — mesma regra que `_resolve_by_email`/`phone_name_dup`
# ja usam pra duplicata.
_PHONE_MATCH_ORDER = """(NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements({alias}.telefones) _t
             WHERE """ + _SQL_CANON.format(d=_SQL_DIGITS) + """ = %s
        )), {alias}.id"""


def phone_lookup_key(phone: Any) -> Optional[str]:
    """
    Chave de comparacao de um telefone, venha ele do WhatsApp ou do Google.

    Devolve os ultimos PHONE_MATCH_DIGITS digitos, ou None quando o numero e
    curto demais pra identificar alguem (o chamador deve pular a busca).
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < PHONE_MATCH_DIGITS:
        return None
    return digits[-PHONE_MATCH_DIGITS:]


def phone_match_sql(alias: str = "c") -> str:
    """
    Condicao pro WHERE de quem monta a propria query (JOIN, colunas extras).

    Consome UM parametro: `phone_lookup_key(phone)`. Para desempate estavel,
    use junto de `phone_match_order_sql`.
    """
    return _PHONE_MATCH_COND.format(alias=alias, n=PHONE_MATCH_DIGITS)


def phone_match_order_sql(alias: str = "c") -> str:
    """ORDER BY que acompanha `phone_match_sql`. Consome UM parametro: o
    numero INTEIRO na forma canonica (`canonical_br_phone(phone)`)."""
    return _PHONE_MATCH_ORDER.format(alias=alias)


def find_contact_by_phone(cursor, phone: Any,
                          columns: str = "id, nome") -> Optional[Dict]:
    """
    Acha a ficha dona de um numero de telefone, em qualquer formato de origem.

    Ponto unico de resolucao online telefone->contato. Devolve o dict do
    contato (com as `columns` pedidas) ou None. Nao cria, nao escreve.
    """
    key = phone_lookup_key(phone)
    if not key:
        return None
    # Canonico dos dois lados: senao um numero que chega no formato antigo
    # nunca seria "match inteiro" contra a ficha gravada no formato novo, e o
    # desempate cairia no menor id em vez de na ficha certa.
    full = canonical_br_phone(phone)
    cursor.execute(
        f"SELECT {columns} FROM contacts c "
        f"WHERE {phone_match_sql('c')} "
        f"ORDER BY {phone_match_order_sql('c')} LIMIT 1",
        (key, full),
    )
    row = cursor.fetchone()
    return dict(row) if row else None
