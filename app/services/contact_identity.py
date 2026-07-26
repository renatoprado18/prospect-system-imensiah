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


# ============== Nome ==============

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


def phone_kind(normalized: str) -> str:
    """
    Classifica um telefone JA normalizado por `normalize_phone`.

    'mobile'   -> celular (assinante de 9 digitos, comeca com 9)
    'landline' -> fixo (assinante de 8 digitos)
    'unknown'  -> internacional / formato que nao da pra afirmar
    """
    d = normalized or ""
    if not d.isdigit():
        return "unknown"
    if d.startswith("55"):
        if len(d) == 13:
            return "mobile"
        if len(d) == 12:
            return "landline"
        return "unknown"
    if len(d) == 11 and d[2] == "9":
        return "mobile"
    if len(d) == 10:
        return "landline"
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
    """Telefones normalizados com DDD, deduplicados."""
    out = []
    for item in _as_list(contact.get("telefones")):
        raw = item.get("number", "") if isinstance(item, dict) else str(item)
        norm = normalize_phone(raw or "")
        if len(norm) < MIN_PHONE_DIGITS:
            continue
        if norm not in out:
            out.append(norm)
    return out


def google_ids_map(contact: Dict) -> Dict[str, str]:
    """Le o mapa {conta: resourceName} de um row de contacts."""
    blob = _as_dict(contact.get(GOOGLE_IDS_COLUMN))
    raw = blob.get(GOOGLE_IDS_KEY)
    out = {}
    for account, gid in _as_dict(raw).items():
        if account and gid:
            out[str(account)] = str(gid)
    return out


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
        for account, mapped in google_ids_map(row).items():
            self.by_gid.setdefault(str(mapped), contact_id)

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
        for phone in contact_phones(contact):
            candidates = self.by_phone.get(phone) or []
            if not candidates or len(candidates) > SHARED_LINE_MAX_CONTACTS:
                continue

            kind = phone_kind(phone)
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
    """JSON pronto pro INSERT de um contato novo."""
    if not account_email or not gid:
        return json.dumps({})
    return json.dumps({GOOGLE_IDS_KEY: {account_email: gid}})


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

    cursor.execute(f"""
        UPDATE contacts
        SET {GOOGLE_IDS_COLUMN} = jsonb_set(
                COALESCE({GOOGLE_IDS_COLUMN}, '{{}}'::jsonb),
                %s,
                COALESCE({GOOGLE_IDS_COLUMN} -> %s, '{{}}'::jsonb)
                    || jsonb_build_object(%s::text, %s::text),
                true
            ),
            google_contact_id = COALESCE(google_contact_id, %s)
        WHERE id = %s
    """, (
        "{" + GOOGLE_IDS_KEY + "}",
        GOOGLE_IDS_KEY,
        account_email,
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

    cursor.execute(f"""
        SELECT id, google_contact_id, {GOOGLE_IDS_COLUMN}
        FROM contacts
        WHERE google_contact_id = %s
           OR {GOOGLE_IDS_COLUMN} -> %s ->> %s = %s
        LIMIT 1
    """, (gid, GOOGLE_IDS_KEY, account_email, gid))
    row = cursor.fetchone()
    if not row:
        return None

    row = dict(row)
    contact_id = row["id"]
    remaining = {
        acc: mapped for acc, mapped in google_ids_map(row).items()
        if acc != account_email
    }

    if not remaining:
        cursor.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
        return "deleted" if cursor.rowcount > 0 else None

    # Outra conta ainda usa esta ficha: tira so a chave desta conta e, se a
    # coluna escalar apontava pro id apagado, repassa pra uma conta viva.
    fallback_gid = next(iter(remaining.values()))
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
