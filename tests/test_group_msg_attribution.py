"""
Testes da atribuicao de contato em mensagens de grupo (frente de 17/10/26).

O defeito: `group_message_sync` resolve `contact_id` so no INSERT e grava com
`ON CONFLICT DO NOTHING` ⇒ mensagem ja gravada nunca e reavaliada, e contato
criado depois dela fica orfao para sempre (77% das msgs de grupo em prod).

Prova:
  (a) LID disfarcado de telefone sai declarado como LID, nao como "sem ficha"
      — somar os dois esconderia uma causa estrutural dentro de outra;
  (b) celular casa pelo NUMERO mesmo com pushName de apelido/emoji, porque
      exigir nome ali devolveria a mensagem pro anonimato que e o defeito;
  (c) FIXO com nome divergente NAO casa (linha compartilhada, Douglas x
      Orestes) — a guarda e assimetrica de proposito;
  (d) ficha placeholder ("Desconhecido +55...") recebe a atribuicao;
  (e) `from_me` fica fora da fila (nao vira eco na ficha do Renato);
  (f) o UPDATE nunca sobrescreve `contact_id` ja preenchido;
  (g) os baldes FECHAM com a fila — ramo que sai calado e o modo de falha
      que esta frente inteira veio corrigir.

Rodar: .venv/bin/python -m pytest tests/test_group_msg_attribution.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import group_msg_attribution as gma  # noqa: E402


# ---------------------------------------------------------------- fakes DB ----

class _FakeCursor:
    """
    Emula o subset usado: a query de agregacao da fila, o SELECT de
    `find_contact_by_phone` e o UPDATE.

    `contacts` e uma lista de dicts {id, nome, digitos}. `msgs` e a lista de
    mensagens {sender_phone, sender_name, contact_id, from_me}.
    """

    def __init__(self, contacts, msgs):
        self.contacts = contacts
        self.msgs = msgs
        self._result = []
        self.rowcount = 0
        self.updates = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).lower()

        if s.startswith("update group_messages"):
            contact_id, fone = params
            n = 0
            for m in self.msgs:
                if (m["sender_phone"] == fone
                        and m.get("contact_id") is None
                        and not m.get("from_me")):
                    m["contact_id"] = contact_id
                    n += 1
            self.rowcount = n
            self.updates.append((contact_id, fone, n))
            self._result = []
            return

        if "group by sender_phone" in s:
            agg = {}
            for m in self.msgs:
                if m.get("contact_id") is not None or m.get("from_me"):
                    continue
                k = m["sender_phone"]
                cur = agg.setdefault(k, {"sender_phone": k,
                                         "sender_name": m.get("sender_name"),
                                         "n": 0})
                cur["n"] += 1
            rows = sorted(agg.values(), key=lambda r: -r["n"])
            if params and "limit" in s:
                rows = rows[: params[0]]
            self._result = rows
            return

        if "from contacts" in s:
            # params = (key, full) — ambos so-digitos. Casa por sufixo, que e o
            # que `phone_match_sql` faz na pratica.
            key, full = params[0], params[1]
            hits = [c for c in self.contacts
                    if c["digitos"].endswith(str(key))
                    or c["digitos"] == str(full)]
            self._result = [{"id": h["id"], "nome": h["nome"]} for h in hits[:1]]
            return

        if "group by group_jid" in s or "count(contact_id)" in s:
            self._result = [{"total": len(self.msgs),
                             "com_contato": sum(1 for m in self.msgs
                                                if m.get("contact_id")),
                             "orfas": sum(1 for m in self.msgs
                                          if not m.get("contact_id")
                                          and not m.get("from_me")),
                             "grupos_zerados": 0}]
            return

        self._result = []

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


CONTATOS = [
    {"id": 18707, "nome": "Eduardo Marson Ferreira", "digitos": "5511989071248"},
    {"id": 359, "nome": "Antonio Carlos Piccino Filho", "digitos": "5514981281000"},
    # fixo compartilhado do escritorio
    {"id": 18219, "nome": "Sergio Teixeira de Andrade Filho", "digitos": "551138970085"},
    # ficha sem nome, dona legitima do numero
    {"id": 26770, "nome": "Desconhecido +5511986236954", "digitos": "5511986236954"},
]


def _cur(msgs):
    return _FakeCursor([dict(c) for c in CONTATOS], msgs)


# ------------------------------------------------------------------ (a) LID ----

@pytest.mark.parametrize("lid", ["101073566040118", "56887345193178",
                                 "20796248481865", "245049392673008"])
def test_lid_sai_declarado_e_nao_como_sem_ficha(lid):
    """O `.replace('@lid','')` da ingestao deixa 15 digitos no campo telefone.

    Isso tem de sair como LID: cair em `pulada_sem_ficha` misturaria 18.196
    msgs de causa estrutural com as 23.821 que de fato sao contato novo.
    """
    cid, motivo = gma.resolver_remetente(_cur([]), lid, "101073566040118")
    assert cid is None
    assert motivo == gma.PULADA_LID


def test_telefone_br_de_13_digitos_nao_e_confundido_com_lid():
    """A fronteira importa: 13 digitos e celular BR com DDI, 14+ e LID."""
    cid, motivo = gma.resolver_remetente(
        _cur([]), "5511989071248", "Eduardo Marson Ferreira")
    assert cid == 18707
    assert motivo == gma.ATRIBUIDA


# -------------------------------------------------------------- (b) celular ----

@pytest.mark.parametrize("push", ["👀", "🐱Janaira Franca🐱", "Dudu", ""])
def test_celular_casa_por_numero_mesmo_com_pushname_de_apelido(push):
    """pushName e escrito pelo remetente — nao pode ser requisito no celular.

    Exigir nome aqui devolveria a mensagem pro anonimato, que e o defeito.
    """
    cid, motivo = gma.resolver_remetente(_cur([]), "5511989071248", push)
    assert cid == 18707
    assert motivo in (gma.ATRIBUIDA, gma.ATRIBUIDA_NOME_DIVERGE)


def test_celular_com_nome_divergente_atribui_mas_declara():
    """Atribui (numero manda) e DECLARA, pra politica poder ser revista."""
    cid, motivo = gma.resolver_remetente(
        _cur([]), "5514981281000", "Antonio Fernando Alves")
    assert cid == 359
    assert motivo == gma.ATRIBUIDA_NOME_DIVERGE


# ----------------------------------------------------------------- (c) fixo ----

def test_fixo_com_nome_divergente_NAO_casa():
    """Fixo e compartilhado: o numero nao identifica pessoa."""
    cid, motivo = gma.resolver_remetente(
        _cur([]), "551138970085", "Secretaria Lacaz")
    assert cid is None
    assert motivo == gma.PULADA_FIXO_NOME_DIVERGE


def test_fixo_com_nome_que_bate_casa():
    cid, motivo = gma.resolver_remetente(
        _cur([]), "551138970085", "Sergio Teixeira de Andrade Filho")
    assert cid == 18219
    assert motivo == gma.ATRIBUIDA


# ---------------------------------------------------------- (d) placeholder ----

def test_ficha_placeholder_recebe_atribuicao():
    """"Desconhecido +55..." e dona do numero; atribuir e o que deixa a ficha
    ganhar nome depois pelo uso."""
    cid, motivo = gma.resolver_remetente(
        _cur([]), "5511986236954", "Ana Carolina Conceicao")
    assert cid == 26770
    assert motivo == gma.ATRIBUIDA_FICHA_PLACEHOLDER


def test_sem_ficha_nenhuma_nao_inventa_contato():
    cid, motivo = gma.resolver_remetente(_cur([]), "5513981644267", "Rafael")
    assert cid is None
    assert motivo == gma.PULADA_SEM_FICHA


# ------------------------------------------------------------- (e)(f)(g) ------

def test_from_me_fica_fora_da_fila():
    """631 msgs minhas: a flag ja diz o autor. Aponta-las pra ficha do Renato
    (#23419) alimentaria as logicas que casam "o terceiro" por ficha."""
    msgs = [
        {"sender_phone": "5511989071248", "sender_name": "Renato",
         "contact_id": None, "from_me": True},
        {"sender_phone": "5511989071248", "sender_name": "Eduardo Marson Ferreira",
         "contact_id": None, "from_me": False},
    ]
    cur = _cur(msgs)
    res = gma.atribuir_orfas(cur)
    assert res["msgs_atribuidas"] == 1
    assert msgs[0]["contact_id"] is None, "msg from_me nao devia ser atribuida"
    assert msgs[1]["contact_id"] == 18707


def test_update_nao_sobrescreve_atribuicao_existente():
    """Entre a leitura da fila e a escrita o webhook pode ter gravado msg nova.
    `contact_id IS NULL` no UPDATE protege o que a ingestao ja acertou."""
    msgs = [
        {"sender_phone": "5511989071248", "sender_name": "Eduardo Marson Ferreira",
         "contact_id": 999, "from_me": False},
        {"sender_phone": "5511989071248", "sender_name": "Eduardo Marson Ferreira",
         "contact_id": None, "from_me": False},
    ]
    cur = _cur(msgs)
    gma.atribuir_orfas(cur)
    assert msgs[0]["contact_id"] == 999, "atribuicao existente foi sobrescrita"
    assert msgs[1]["contact_id"] == 18707


def test_baldes_fecham_com_a_fila():
    """Todo remetente examinado cai em exatamente um motivo. Balde que nao
    fecha e balde que esconde — o modo de falha que originou esta frente."""
    msgs = [
        {"sender_phone": "5511989071248", "sender_name": "Eduardo Marson Ferreira",
         "contact_id": None, "from_me": False},
        {"sender_phone": "101073566040118", "sender_name": "101073566040118",
         "contact_id": None, "from_me": False},
        {"sender_phone": "551138970085", "sender_name": "Secretaria",
         "contact_id": None, "from_me": False},
        {"sender_phone": "5513981644267", "sender_name": "Rafael",
         "contact_id": None, "from_me": False},
        {"sender_phone": "5511986236954", "sender_name": "Ana Carolina",
         "contact_id": None, "from_me": False},
    ]
    res = gma.atribuir_orfas(_cur(msgs))
    assert res["remetentes_examinados"] == 5
    assert sum(res["por_motivo"].values()) == 5
    assert sum(res["msgs_por_motivo"].values()) == 5
    assert res["por_motivo"] == {
        gma.ATRIBUIDA: 1,
        gma.PULADA_LID: 1,
        gma.PULADA_FIXO_NOME_DIVERGE: 1,
        gma.PULADA_SEM_FICHA: 1,
        gma.ATRIBUIDA_FICHA_PLACEHOLDER: 1,
    }


def test_commit_por_remetente_e_chamado():
    """O Neon derruba conexao em lote longo sem erro; commit por item e o que
    impede a rodada de morrer pendurada no socket."""
    msgs = [
        {"sender_phone": "5511989071248", "sender_name": "Eduardo Marson Ferreira",
         "contact_id": None, "from_me": False},
        {"sender_phone": "5511986236954", "sender_name": "Ana",
         "contact_id": None, "from_me": False},
    ]
    chamadas = []
    gma.atribuir_orfas(_cur(msgs), commit_cb=lambda: chamadas.append(1))
    assert len(chamadas) == 2


def test_limite_pega_os_de_maior_volume_primeiro():
    """O cron leva um pedaco por rodada; tem de ser o pedaco que mais rende."""
    msgs = ([{"sender_phone": "5511986236954", "sender_name": "Ana",
              "contact_id": None, "from_me": False}] * 1
            + [{"sender_phone": "5511989071248", "sender_name": "Eduardo Marson Ferreira",
                "contact_id": None, "from_me": False} for _ in range(5)])
    res = gma.atribuir_orfas(_cur([dict(m) for m in msgs]), limite_remetentes=1)
    assert res["remetentes_examinados"] == 1
    assert res["msgs_atribuidas"] == 5


# ----------------------------------- placeholder "rotulo + numero" ------------

@pytest.mark.parametrize("nome", [
    "Desconhecido +5511986236954",   # padrao real: 69 fichas em prod
    "Desconhecido",
    "+5511986236954",
    "5511986236954",
    "",
])
def test_ficha_sem_nome_identificador(nome):
    """`is_placeholder_name` casa o nome inteiro e por isso nao pega
    "Desconhecido +55..." — o padrao que a base realmente tem."""
    assert gma.ficha_sem_nome_identificador(nome) is True


@pytest.mark.parametrize("nome", [
    "Eduardo Marson Ferreira",
    "Antonio Carlos Piccino Filho",
    "Dorita Moraes",
])
def test_nome_real_nao_e_confundido_com_placeholder(nome):
    assert gma.ficha_sem_nome_identificador(nome) is False
