"""
Testes do mapa LID -> telefone (frente de 17/10/26).

O backlog registrava LID como IRRECUPERAVEL, e a conclusao vinha de uma medida
certa sobre o caminho errado: `findMessages` nao devolve `remoteJidAlt` em
replay ⇒ LID nao volta pela MENSAGEM. Mas volta pelo GRUPO: `findGroupInfos`
da o par {id: "<lid>@lid", phoneNumber: "<e164>@s.whatsapp.net"}.

Prova:
  (a) o par e extraido do formato real da Evolution;
  (b) participante SEM `phoneNumber` nao gera par (nao se inventa casamento);
  (c) participante que ja vem como telefone nao e par de mapa;
  (d) CONFLITO preserva o par antigo e e CONTADO — sobrescrever calado faria a
      identidade de uma msg depender de qual grupo foi colhido por ultimo;
  (e) a resolucao nao infere: LID fora do mapa devolve None;
  (f) `resolver_remetente` distingue "LID sem mapa" de "LID resolvido sem
      ficha" — causas diferentes, baldes diferentes;
  (g) a guarda do FIXO continua valendo para LID resolvido;
  (h) grupo que nao responde != grupo sem LID (medido pelo script).

Rodar: .venv/bin/python -m pytest tests/test_wa_lid_map.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import group_msg_attribution as gma  # noqa: E402
from services import wa_lid_map as wlm  # noqa: E402


# ------------------------------------------------------- (a)(b)(c) extracao ----

def test_extrai_par_do_formato_real():
    """Formato colhido de /group/findGroupInfos em 17/10 (grupo da fazenda)."""
    p = {"id": "53322706874561@lid",
         "phoneNumber": "553598868837@s.whatsapp.net", "admin": None}
    assert wlm._extrair_par(p) == ("53322706874561", "553598868837")


def test_participante_sem_telefone_nao_gera_par():
    """A Evolution nem sempre conhece o numero. Inventar casamento ali e pior
    que nao ter o par."""
    assert wlm._extrair_par({"id": "53322706874561@lid"}) is None
    assert wlm._extrair_par({"id": "53322706874561@lid", "phoneNumber": ""}) is None
    assert wlm._extrair_par({"id": "53322706874561@lid", "phoneNumber": None}) is None


def test_participante_que_ja_e_telefone_nao_e_par_de_mapa():
    p = {"id": "5511982229988@s.whatsapp.net",
         "phoneNumber": "5511982229988@s.whatsapp.net"}
    assert wlm._extrair_par(p) is None


@pytest.mark.parametrize("fone", ["123", "12345678", "1234567890123456789"])
def test_telefone_de_tamanho_impossivel_nao_gera_par(fone):
    """O CHECK da 087 recusaria no banco; recusar antes evita a exception."""
    assert wlm._extrair_par({"id": "999@lid", "phoneNumber": fone}) is None


# ------------------------------------------------------------ fake cursor -----

class _FakeConn:
    """`psycopg2.extras.execute_values` lê `cursor.connection.encoding`."""
    encoding = "UTF8"


class _FakeCursor:
    """
    Emula wa_lid_map + a busca de ficha por telefone.

    Emula tambem o suficiente de psycopg2 para `execute_values` rodar DE
    VERDADE (`connection.encoding` + `mogrify`), em vez de monkeypatchar a
    gravacao em lote: era justamente a gravacao em lote que tinha de ser
    provada, e um fake que a contorna provaria a versao antiga.
    """

    def __init__(self, mapa=None, contatos=None):
        self.mapa = dict(mapa or {})
        self.contatos = list(contatos or [])
        self._res = []
        self.rowcount = 0
        self.connection = _FakeConn()

    def mogrify(self, template, args=()):
        vals = ", ".join(
            "NULL" if a is None else f"'{a}'" for a in (args or ()))
        return f"({vals})".encode()

    def execute(self, sql, params=()):
        if isinstance(sql, bytes):
            sql = sql.decode()
        s = " ".join(sql.split()).lower()

        if s.startswith("select lid, phone from wa_lid_map"):
            pedidos = list(params[0])
            self._res = [{"lid": k, "phone": v}
                         for k, v in self.mapa.items() if k in pedidos]
            return

        if s.startswith("insert into wa_lid_map"):
            # execute_values ja interpolou as tuplas no proprio SQL
            import re as _re
            n = 0
            for tup in _re.findall(r"\(([^()]*)\)", sql[sql.lower().index("values"):]):
                campos = [c.strip().strip("'") for c in tup.split(",")]
                if len(campos) < 2:
                    continue
                lid, phone = campos[0], campos[1]
                if lid not in self.mapa:
                    self.mapa[lid] = phone
                    n += 1
            self.rowcount = n
            self._res = []
            return

        if "from contacts" in s:
            key, full = str(params[0]), str(params[1])
            hits = [c for c in self.contatos
                    if c["digitos"].endswith(key) or c["digitos"] == full]
            self._res = [{"id": h["id"], "nome": h["nome"]} for h in hits[:1]]
            return

        if s.startswith("select phone from wa_lid_map"):
            lid = params[0]
            self._res = [{"phone": self.mapa[lid]}] if lid in self.mapa else []
            return

        self._res = []

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)


# ------------------------------------------------------------- (d) conflito ----

def test_conflito_preserva_o_par_antigo_e_e_contado():
    cur = _FakeCursor(mapa={"999": "5511111111111"})
    r = wlm.gravar_pares(cur, [("999", "5522222222222")], origem_jid="g1@g.us")
    assert r["conflitos"] == 1
    assert r["novos"] == 0
    assert cur.mapa["999"] == "5511111111111", "o par antigo foi sobrescrito"


def test_par_igual_nao_conta_como_novo_nem_conflito():
    cur = _FakeCursor(mapa={"999": "5511111111111"})
    r = wlm.gravar_pares(cur, [("999", "5511111111111")], origem_jid="g1@g.us")
    assert r == {"novos": 0, "conflitos": 0, "iguais": 1}


def test_par_novo_entra():
    cur = _FakeCursor()
    r = wlm.gravar_pares(cur, [("888", "5533333333333")], origem_jid="g1@g.us")
    assert r["novos"] == 1
    assert cur.mapa["888"] == "5533333333333"


# ----------------------------------------------------------- (e) resolucao ----

def test_lid_fora_do_mapa_devolve_none_sem_inferir():
    cur = _FakeCursor(mapa={"111": "5511999999999"})
    assert wlm.telefone_de_lid(cur, "222") is None
    assert wlm.telefone_de_lid(cur, "") is None
    assert wlm.telefone_de_lid(cur, None) is None


def test_resolve_lid_com_sufixo_arroba():
    cur = _FakeCursor(mapa={"111": "5511999999999"})
    assert wlm.telefone_de_lid(cur, "111@lid") == "5511999999999"


# ------------------------------------------------- (f)(g) na atribuicao -------

CONTATOS = [
    {"id": 18707, "nome": "Eduardo Marson Ferreira", "digitos": "5511989071248"},
    {"id": 18219, "nome": "Sergio Teixeira de Andrade Filho", "digitos": "551138970085"},
]


def test_lid_sem_mapa_sai_declarado_como_sem_mapa():
    """Nao pode cair em `sem_ficha`: sao causas diferentes, e somá-las
    esconderia uma estrutural dentro de uma de cadastro."""
    cur = _FakeCursor(mapa={}, contatos=CONTATOS)
    cid, motivo = gma.resolver_remetente(cur, "101073566040118", "alguem")
    assert cid is None
    assert motivo == gma.PULADA_LID


def test_lid_resolvido_que_casa_vira_atribuida_via_lid():
    """O balde proprio e o que permite medir o ganho DESTA frente."""
    cur = _FakeCursor(mapa={"101073566040118": "5511989071248"}, contatos=CONTATOS)
    cid, motivo = gma.resolver_remetente(cur, "101073566040118",
                                         "Eduardo Marson Ferreira")
    assert cid == 18707
    assert motivo == gma.ATRIBUIDA_VIA_LID


def test_lid_resolvido_sem_ficha_tem_balde_proprio():
    """Ja se sabe QUAL e o numero; falta cadastro. Diferente de LID opaco."""
    cur = _FakeCursor(mapa={"999999999999999": "5599999999999"}, contatos=CONTATOS)
    cid, motivo = gma.resolver_remetente(cur, "999999999999999", "ninguem")
    assert cid is None
    assert motivo == gma.PULADA_LID_SEM_FICHA


def test_guarda_do_fixo_vale_igual_para_lid_resolvido():
    """O mapa diz de que NUMERO a msg veio, nao quem digitou — e fixo continua
    compartilhado (Douglas x Orestes)."""
    cur = _FakeCursor(mapa={"777777777777777": "551138970085"}, contatos=CONTATOS)
    cid, motivo = gma.resolver_remetente(cur, "777777777777777", "Secretaria Lacaz")
    assert cid is None
    assert motivo == gma.PULADA_FIXO_NOME_DIVERGE


def test_lid_resolvido_para_fixo_com_nome_que_bate_passa():
    cur = _FakeCursor(mapa={"777777777777777": "551138970085"}, contatos=CONTATOS)
    cid, motivo = gma.resolver_remetente(cur, "777777777777777",
                                         "Sergio Teixeira de Andrade Filho")
    assert cid == 18219
    assert motivo == gma.ATRIBUIDA_VIA_LID


def test_telefone_normal_nao_passa_pelo_mapa():
    """Regressao: numero de 13 digitos continua resolvendo direto."""
    cur = _FakeCursor(mapa={}, contatos=CONTATOS)
    cid, motivo = gma.resolver_remetente(cur, "5511989071248",
                                         "Eduardo Marson Ferreira")
    assert cid == 18707
    assert motivo == gma.ATRIBUIDA


def test_lid_resolvido_com_nome_divergente_ainda_atribui():
    """pushName de grupo e apelido/emoji; no celular o numero manda."""
    cur = _FakeCursor(mapa={"101073566040118": "5511989071248"}, contatos=CONTATOS)
    cid, motivo = gma.resolver_remetente(cur, "101073566040118", "🐱Dudu🐱")
    assert cid == 18707
    assert motivo == gma.ATRIBUIDA_VIA_LID


# ------------------------------------------------- gravacao EM LOTE -----------

def test_lote_dedup_intra_grupo():
    """O mesmo LID pode vir duas vezes do mesmo grupo — conta uma."""
    cur = _FakeCursor()
    r = wlm.gravar_pares(cur, [("555", "5511999999999"), ("555", "5511999999999")],
                         origem_jid="g@g.us")
    assert r["novos"] == 1
    assert cur.mapa == {"555": "5511999999999"}


def test_lote_separa_novos_de_iguais_e_conflitos_numa_passada():
    """Um lote com os tres casos tem de contar os tres certo."""
    cur = _FakeCursor(mapa={"aa": "5511111111111", "bb": "5522222222222"})
    r = wlm.gravar_pares(cur, [
        ("aa", "5511111111111"),   # igual
        ("bb", "5599999999999"),   # conflito
        ("cc", "5533333333333"),   # novo
    ], origem_jid="g@g.us")
    assert r == {"novos": 1, "conflitos": 1, "iguais": 1}
    assert cur.mapa["bb"] == "5522222222222", "conflito sobrescreveu"
    assert cur.mapa["cc"] == "5533333333333"


def test_lote_vazio_nao_faz_query():
    cur = _FakeCursor()
    assert wlm.gravar_pares(cur, [], origem_jid="g@g.us") == {
        "novos": 0, "conflitos": 0, "iguais": 0}


# ------------------------------------------- budget da colheita no cron -------

@pytest.mark.asyncio
async def test_colheita_para_no_budget_e_declara_parcial(monkeypatch):
    """A colheita roda FORA do `wait_for` do cron: sem relógio próprio, 5 grupos
    numa instância lenta somariam 150s por cima dos 200s do sync — e só
    estouraria em produção."""
    import itertools
    relogio = itertools.count(0, 30)       # cada consulta avança 30s
    monkeypatch.setattr(wlm, "grupos_com_lid_descoberto",
                        lambda cur, limite=3: ["a@g.us", "b@g.us", "c@g.us"])

    async def _colhe_lento(jid, timeout=10.0):
        return [("1" * 15, "5511999999999")]
    monkeypatch.setattr(wlm, "colher_grupo", _colhe_lento)

    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: next(relogio))

    r = await wlm.colher_pendentes(_FakeCursor(), budget_s=45.0)
    assert r["parcial"] is True
    assert r["grupos_tentados"] < r["grupos_alvo"]


@pytest.mark.asyncio
async def test_grupo_sem_resposta_nao_conta_como_coberto(monkeypatch):
    """Instância muda != grupo sem LID. Somar os dois faria a cobertura parecer
    completa."""
    monkeypatch.setattr(wlm, "grupos_com_lid_descoberto",
                        lambda cur, limite=3: ["a@g.us", "b@g.us"])

    async def _vazio(jid, timeout=10.0):
        return []
    monkeypatch.setattr(wlm, "colher_grupo", _vazio)

    r = await wlm.colher_pendentes(_FakeCursor(), budget_s=999.0)
    assert r["sem_resposta"] == 2
    assert r["novos"] == 0
    assert r["parcial"] is False
