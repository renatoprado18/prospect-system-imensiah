"""Testes do caminho único de merge INTEL⇄Google (`duplicados.merge_par`).

Diretriz do Renato em 14/08/26: *"consistência INTEL⇄Google é por CAMINHO, não
por desenho"*. Até 16/08 a propagação morava na ROTA — quem fundia pela tela via
a ficha sumir do Google; quem fundia por script deixava a ficha viva lá, e o sync
completo a trazia de volta como duplicata nova no dia seguinte.

O que estes testes prendem:
  - o contato absorvido é lido ANTES do merge que o apaga (sem isso a propagação
    fica sem alvo, e o defeito é invisível: o merge local dá certo);
  - falha na propagação NÃO é engolida nem vira sucesso — o merge local já
    aconteceu e é irreversível;
  - `propagate=False` é explícito, nunca o default.

Rodar: .venv/bin/pytest tests/test_merge_par.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import duplicados as D  # noqa: E402


class _Cursor:
    """Devolve a ficha enquanto ela 'existe'; None depois do merge simulado."""

    def __init__(self, estado):
        self.estado = estado
        self._ultimo = None

    def execute(self, sql, params=None):
        alvo = params[0] if params else None
        self._ultimo = self.estado.get(alvo)

    def fetchone(self):
        return self._ultimo


class _Conn:
    def __init__(self, estado):
        self.estado = estado

    def cursor(self):
        return _Cursor(self.estado)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cenario(monkeypatch):
    """Duas fichas; o merge 'apaga' a absorvida, como o real faz."""
    estado = {
        1: {"id": 1, "nome": "Fica", "google_contact_id": None},
        2: {"id": 2, "nome": "Absorvida", "google_contact_id": "people/c123"},
    }
    monkeypatch.setattr(D, "get_db", lambda: _Conn(estado))

    chamadas = {"merge": 0, "propagou_com": None}

    def _merge(keep_id, merge_id, field_choices=None):
        chamadas["merge"] += 1
        estado.pop(merge_id, None)          # o real DELETA a linha
        return {"success": True, "merged_fields": ["nome"]}

    monkeypatch.setattr(D, "merge_contatos", _merge)
    return estado, chamadas


def _mock_propagacao(monkeypatch, chamadas, erro=None):
    async def _prop(merged, deleted, conn, gmod):
        chamadas["propagou_com"] = deleted
        if erro:
            raise erro
        return {"updates": {}, "deletions": {"people/c123": "ok"}}

    import services.contact_dedup as CD
    monkeypatch.setattr(CD, "propagate_merge_to_google", _prop)


@pytest.mark.asyncio
async def test_absorvido_e_lido_antes_do_merge(cenario, monkeypatch):
    """O ponto que não pode soltar: depois do merge a linha não existe, e o
    `google_contact_id` dela some do banco para sempre. Se a leitura passar para
    depois, a propagação fica sem alvo e o merge local segue parecendo perfeito."""
    estado, chamadas = cenario
    _mock_propagacao(monkeypatch, chamadas)

    r = await D.merge_par(1, 2, google_contacts_module=object())

    assert chamadas["merge"] == 1
    assert 2 not in estado, "o merge simulado apagou a ficha, como o real faz"
    assert chamadas["propagou_com"][0]["google_contact_id"] == "people/c123"
    assert r["google_propagation"]["deletions"] == {"people/c123": "ok"}


@pytest.mark.asyncio
async def test_falha_na_propagacao_nao_vira_sucesso(cenario, monkeypatch):
    """O merge local já aconteceu e é irreversível. Engolir o erro faria o
    chamador relatar 'Sucesso!' com a ficha viva no Google — que é o defeito
    original com outra roupa."""
    estado, chamadas = cenario
    _mock_propagacao(monkeypatch, chamadas, erro=RuntimeError("token expirado"))

    r = await D.merge_par(1, 2, google_contacts_module=object())

    assert r["success"] is True, "o merge local aconteceu e deve ser relatado"
    assert "token expirado" in r["google_propagation"]["error"]


@pytest.mark.asyncio
async def test_propagate_false_nao_toca_no_google(cenario, monkeypatch):
    estado, chamadas = cenario
    _mock_propagacao(monkeypatch, chamadas)

    r = await D.merge_par(1, 2, propagate=False, google_contacts_module=object())

    assert chamadas["propagou_com"] is None
    assert r["google_propagation"] is None


@pytest.mark.asyncio
async def test_erro_do_merge_aborta_antes_de_propagar(cenario, monkeypatch):
    estado, chamadas = cenario
    _mock_propagacao(monkeypatch, chamadas)
    monkeypatch.setattr(D, "merge_contatos",
                        lambda *a, **k: {"error": "conflito de unique"})

    r = await D.merge_par(1, 2, google_contacts_module=object())

    assert r == {"error": "conflito de unique"}
    assert chamadas["propagou_com"] is None, "não se propaga merge que não houve"


@pytest.mark.asyncio
async def test_guardas_de_entrada(cenario):
    assert "error" in await D.merge_par(1, 1)
    assert "não existe" in (await D.merge_par(1, 987654))["error"]
