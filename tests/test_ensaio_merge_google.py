"""
Testes do ESPIÃO do ensaio de merge (`scripts/ensaio_merge_google.py`).

POR QUE O ENSAIO PRECISA DE TESTE. O espião é o que separa "ensaio" de "mutirão":
ele delega ao módulo real toda LEITURA e intercepta toda ESCRITA. Se um dos três
métodos de escrita falhasse em sobrescrever o do módulo real, `__getattr__`
delegaria — e o ensaio criaria fichas de verdade no Google enquanto reportava
placar zerado. Um medidor que não se mede certifica o que nunca checou.
[[feedback_medidor_que_nao_mede_a_si_mesmo]]

Rodar: .venv/bin/python -m pytest tests/test_ensaio_merge_google.py -v
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)


def _carregar_ensaio():
    """Importa o script pelo caminho — `scripts/` não é pacote."""
    caminho = os.path.join(_ROOT, "scripts", "ensaio_merge_google.py")
    spec = importlib.util.spec_from_file_location("ensaio_merge_google", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ensaio = _carregar_ensaio()


class _RealExplosivo:
    """Módulo do Google onde QUALQUER escrita é um erro de teste."""

    def __init__(self):
        self.leituras = []

    async def create_google_contact(self, *a, **kw):
        pytest.fail("create chegou ao Google real — o ensaio viraria mutirão")

    async def update_google_contact(self, *a, **kw):
        pytest.fail("update chegou ao Google real")

    async def delete_google_contact(self, *a, **kw):
        pytest.fail("delete chegou ao Google real")

    async def buscar_ficha_existente(self, token, nome, telefones):
        self.leituras.append((nome, tuple(telefones or ())))
        return "people/cACHADA"

    def alguma_constante(self):
        return "vinda do real"


@pytest.fixture
def espiao():
    real = _RealExplosivo()
    return ensaio.EspiaoGoogle(real), real


# ==================== as três escritas não passam ====================

@pytest.mark.asyncio
async def test_create_nao_chega_no_google_real(espiao):
    esp, _ = espiao
    rid = await esp.create_google_contact("tok", {"nome": "Bettina Berman"})
    assert "ENSAIO" in rid, "o id devolvido tem que denunciar que é de ensaio"
    assert esp.placar["create"] == 1
    assert esp.registro == [("CREATE", "Bettina Berman", "")]


@pytest.mark.asyncio
async def test_delete_nao_chega_no_google_real(espiao):
    esp, _ = espiao
    assert await esp.delete_google_contact("tok", "people/cXYZ") is True
    assert esp.placar["delete"] == 1


@pytest.mark.asyncio
async def test_update_nao_chega_no_google_real(espiao, monkeypatch):
    esp, _ = espiao
    monkeypatch.setattr(esp, "_ficha_responde",
                        lambda *a, **kw: _coro(True))
    assert await esp.update_google_contact("tok", "people/cXYZ", {"nome": "Bel"}) is True
    assert esp.placar["update"] == 1


async def _coro(v):
    return v


# ==================== as leituras PASSAM (controle positivo) ====================
#
# Sem isto, um espião que bloqueasse tudo passaria nos testes acima — e o ensaio
# mediria um caminho em que `buscar_ficha_existente` nunca roda, ou seja,
# exatamente a função sob julgamento ficaria de fora.

@pytest.mark.asyncio
async def test_busca_chega_ao_google_real(espiao):
    esp, real = espiao
    achado = await esp.buscar_ficha_existente("tok", "Bettina Berman", ["11999998888"])
    assert achado == "people/cACHADA"
    assert real.leituras == [("Bettina Berman", ("11999998888",))]


def test_atributo_desconhecido_delega_ao_real(espiao):
    esp, _ = espiao
    assert esp.alguma_constante() == "vinda do real"


# ==================== fidelidade do update ====================

@pytest.mark.asyncio
async def test_update_em_ficha_inexistente_conta_como_falharia(espiao, monkeypatch):
    """O caminho real cai no create quando o update falha. Devolver True de graça
    esconderia justamente os creates que o ensaio veio contar."""
    esp, _ = espiao
    monkeypatch.setattr(esp, "_ficha_responde", lambda *a, **kw: _coro(False))
    assert await esp.update_google_contact("tok", "people/cSUMIU", {"nome": "X"}) is False
    assert esp.placar["update_falharia"] == 1
    assert esp.placar["update"] == 0


# ==================== a guarda de alvo ====================

def test_guarda_recusa_prod(monkeypatch):
    monkeypatch.setenv("DB_TARGET", "prod")
    with pytest.raises(SystemExit) as e:
        ensaio._exigir_banco_local()
    assert "local" in str(e.value).lower()


def test_guarda_recusa_alvo_ausente(monkeypatch):
    """DB_TARGET vazio não pode ser lido como 'deve ser local' — alvo é
    declarado, nunca deduzido."""
    monkeypatch.delenv("DB_TARGET", raising=False)
    with pytest.raises(SystemExit):
        ensaio._exigir_banco_local()


def test_guarda_deixa_passar_local(monkeypatch):
    monkeypatch.setenv("DB_TARGET", "local")
    ensaio._exigir_banco_local()   # não levanta
