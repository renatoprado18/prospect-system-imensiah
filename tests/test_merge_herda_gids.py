"""O sobrevivente do merge herda os resourceName de TODAS as fichas do grupo.

O DEFEITO (medido pelo ensaio em 22/08/2026, `scripts/ensaio_merge_google.py`).
As duplicatas desta base são tipicamente TRÊS fichas da mesma pessoa: uma da
conta pessoal, uma da profissional e uma legada sem vínculo. `merge_contacts`
levava só o `google_contact_id` escalar do `base`, então o sobrevivente saía
conhecendo UMA conta.

Na outra conta, `propagate_contact_to_google` não achava alvo e caía em
`_achar_ou_criar` — e como a ficha real daquela conta pertence a um contato
ABSORVIDO, ela estava em `condenados` e a busca era PROIBIDA de encontrá-la.
Resultado: apagava a ficha boa e criava outra na mesma conta, com resourceName
novo. É o "2 apagadas, 4 criadas" da Bettina Berman.

Placar do ensaio em 10 grupos, antes: 12 CREATE, 8 UPDATE, 0 DELETE.

Rodar: .venv/bin/python -m pytest tests/test_merge_herda_gids.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.contact_dedup import (  # noqa: E402
    merge_contacts, propagate_merge_to_google,
)
from services.contact_identity import (  # noqa: E402
    GOOGLE_IDS_COLUMN, GOOGLE_IDS_KEY, google_ids_map,
)

PESSOAL = "renato.almeida.prado@gmail.com"
PROFISSIONAL = "renato@almeida-prado.com"


def _ficha(cid, nome, conta=None, gid=None, origem=None, tel="11999998888"):
    c = {
        "id": cid, "nome": nome, "empresa": None, "cargo": None,
        "emails": [], "telefones": [{"type": "mobile", "number": tel}],
        "foto_url": None, "linkedin": None, "contexto": "",
        "google_contact_id": gid if origem else None,
        "origem": origem,
    }
    if conta and gid:
        c[GOOGLE_IDS_COLUMN] = {GOOGLE_IDS_KEY: {conta: [gid]}}
    return c


# O trio real: pessoal + profissional + legada sem vínculo.
def _trio():
    return [
        _ficha(23552, "Suemi Marui", PESSOAL, "cPESSOAL"),
        _ficha(22893, "Suemi Marui", PROFISSIONAL, "cPROFISSIONAL"),
        _ficha(5571, "Suemi Marui"),
    ]


# ==================== merge_contacts une os mapas ====================

def test_sobrevivente_conhece_as_DUAS_contas():
    merged = merge_contacts(_trio())
    mapa = google_ids_map(merged)
    assert set(mapa) == {PESSOAL, PROFISSIONAL}, (
        f"o sobrevivente saiu conhecendo {list(mapa)} — na conta que falta ele "
        f"criaria ficha nova")
    assert mapa[PESSOAL] == ["cPESSOAL"]
    assert mapa[PROFISSIONAL] == ["cPROFISSIONAL"]


def test_duas_fichas_na_MESMA_conta_viram_lista():
    """A mesma agenda pode ter 2 fichas da pessoa: uma é adotada, a outra é o
    passivo que o mutirão vem limpar. As duas têm que aparecer."""
    grupo = [_ficha(1, "Bel Coelho", PESSOAL, "cPRIMEIRA"),
             _ficha(2, "Bel Coelho", PESSOAL, "cSEGUNDA")]
    mapa = google_ids_map(merge_contacts(grupo))
    assert mapa[PESSOAL] == ["cPRIMEIRA", "cSEGUNDA"], "ordem estável: base primeiro"


def test_grupo_sem_nenhum_vinculo_nao_inventa_mapa():
    """Contato que nunca teve ficha no Google não pode sair com mapa vazio
    gravado por cima do que havia."""
    grupo = [_ficha(1, "Fulano"), _ficha(2, "Fulano")]
    merged = merge_contacts(grupo)
    assert not merged.get(GOOGLE_IDS_COLUMN)


def test_origem_viaja_junto():
    """`origem` é o fallback de conta do contato pré-cascata, no update e na
    deleção. Sem ela o escalar legado fica sem dono."""
    grupo = [_ficha(1, "Fulano", origem=f"google_{PESSOAL}", gid="cLEGADA"),
             _ficha(2, "Fulano")]
    assert merge_contacts(grupo)["origem"] == f"google_{PESSOAL}"


# ==================== a ficha adotada não é apagada ====================

class _GcEspiao:
    def __init__(self):
        self.apagados = []
        self.atualizados = []
        self.criados = 0

    async def refresh_access_token(self, rt):
        return {"access_token": "tok"}

    async def buscar_ficha_existente(self, token, nome, telefones):
        return None

    async def update_google_contact(self, token, rid, dados, *a, **kw):
        self.atualizados.append(rid)
        return True

    async def create_google_contact(self, token, dados, *a, **kw):
        self.criados += 1
        return "cNOVA"

    async def delete_google_contact(self, token, rid, *a, **kw):
        self.apagados.append(rid)
        return True


class _Cur:
    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _Conn:
    def cursor(self, **kw):
        return _Cur()

    def commit(self):
        pass


@pytest.fixture
def contas(monkeypatch):
    async def _fake(_conn):
        return [{"email": PESSOAL, "access_token": "tok", "refresh_token": "r",
                 "tipo": "personal"},
                {"email": PROFISSIONAL, "access_token": "tok", "refresh_token": "r",
                 "tipo": "professional"}]
    import services.contact_dedup as cd
    monkeypatch.setattr(cd, "get_google_accounts", _fake)


@pytest.mark.asyncio
async def test_nao_apaga_a_ficha_que_acabou_de_atualizar(contas):
    """O ciclo completo. O sobrevivente adota `cPROFISSIONAL`, que veio de uma
    ficha ABSORVIDA — sem a guarda, o merge atualiza e apaga o mesmo
    resourceName, e a pessoa some da agenda profissional."""
    grupo = _trio()
    merged = merge_contacts(grupo)
    absorvidas = [c for c in grupo if c["id"] != merged["id"]]

    gc = _GcEspiao()
    r = await propagate_merge_to_google(merged, absorvidas, _Conn(), gc)

    assert gc.criados == 0, "criou ficha nova conhecendo as duas contas"
    assert set(gc.atualizados) == {"cPESSOAL", "cPROFISSIONAL"}
    assert "cPROFISSIONAL" not in gc.apagados, (
        "apagou a ficha que acabou de atualizar — a pessoa sumiria da agenda")
    assert "cPESSOAL" not in gc.apagados
    pulados = [v for v in r["deletions"].values()
               if v.get("skipped") == "adotada_pelo_sobrevivente"]
    assert pulados, "o pulo tem que ficar registrado, não ser silencioso"


@pytest.mark.asyncio
async def test_a_ficha_SOBRANDO_na_mesma_conta_continua_condenada(contas):
    """Controle positivo: a guarda não pode virar 'não apaga nada'. A segunda
    ficha da MESMA conta é exatamente o passivo que o mutirão veio limpar."""
    grupo = [_ficha(1, "Bel Coelho", PESSOAL, "cADOTADA"),
             _ficha(2, "Bel Coelho", PESSOAL, "cSOBRA")]
    merged = merge_contacts(grupo)
    absorvidas = [c for c in grupo if c["id"] != merged["id"]]

    gc = _GcEspiao()
    await propagate_merge_to_google(merged, absorvidas, _Conn(), gc)

    assert "cSOBRA" in gc.apagados, "a duplicata da mesma agenda tem que sair"
    assert "cADOTADA" not in gc.apagados


# ==================== merge não inventa presença ====================

@pytest.mark.asyncio
async def test_nao_cria_ficha_em_conta_onde_a_pessoa_nunca_esteve(contas):
    """3 dos 120 grupos do ensaio eram só isto: gente que existe apenas na conta
    pessoal ganhando ficha NOVA na profissional, porque o fallback de
    `propagate_contact_to_google` é "não sei o contexto → manda pra todas".
    Espalhar contato entre agendas é trabalho do sync, não de quem desduplica."""
    grupo = [_ficha(5338, "Rosalia Pipponzi", PESSOAL, "cPESSOAL"),
             _ficha(26714, "Rosalia Pipponzi")]
    merged = merge_contacts(grupo)
    absorvidas = [c for c in grupo if c["id"] != merged["id"]]

    gc = _GcEspiao()
    await propagate_merge_to_google(merged, absorvidas, _Conn(), gc)

    assert gc.criados == 0, "criou ficha na agenda profissional sem a pessoa estar lá"
    assert gc.atualizados == ["cPESSOAL"], "atualiza só onde a pessoa existe"


@pytest.mark.asyncio
async def test_grupo_sem_vinculo_nenhum_ainda_propaga(contas):
    """Controle positivo: a restrição não pode virar 'merge nunca propaga'. Sem
    vínculo conhecido não há o que restringir — o comportamento antigo vale, e a
    mudança não sai calada."""
    grupo = [_ficha(1, "Fulano de Tal"), _ficha(2, "Fulano de Tal")]
    merged = merge_contacts(grupo)
    absorvidas = [c for c in grupo if c["id"] != merged["id"]]

    gc = _GcEspiao()
    await propagate_merge_to_google(merged, absorvidas, _Conn(), gc)

    assert gc.criados == 2, "sem vínculo, propaga pras duas contas como antes"
