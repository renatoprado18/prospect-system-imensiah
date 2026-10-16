"""O ciclo de grupo tratava FALHA como "passada vazia" e fechava a conta errada.

O incidente (16/10/2026). Depois de religar o WhatsApp, o `wa_repareia.sh`
rodava o polling de grupo num `for` em bash cujo parser lia os campos por
`.get()`: quando a resposta não tinha o shape esperado, saía
`offset None -> None | grupos None | salvas None` e o loop seguia. **3 das 7
passadas saíram assim.** Como a condição de parada era
`next_offset == 0 && i > 1` — e passada falha não avança o cursor — o ciclo
"fechou" deixando grupos sem varrer. O script relatou **19** mensagens
recuperadas; uma rodada manual depois achou **mais 16**. Contra uma média
noturna de 30,8, os 19 passavam perfeitamente por "noite fraca".

Duas lições, e cada teste aqui fixa uma:

  (a) falha NÃO é passada vazia — não pode contar como "secou", porque os
      grupos daquela página seguem sem varrer;
  (b) o critério de parada é SECAR (duas passadas seguidas sem nada novo),
      não "o cursor voltou a zero".

Rodar: .venv/bin/python -m pytest tests/test_grupo_ciclo_falha_calada.py -v
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_spec = importlib.util.spec_from_file_location(
    "_grupo_ciclo", os.path.join(_ROOT, "scripts", "_grupo_ciclo.py")
)
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)


class _Resposta:
    """Dublê de resposta HTTP para o `urlopen` usado por `_uma_passada`."""

    def __init__(self, corpo: str, status: int = 200):
        self._corpo = corpo.encode()
        self.status = status

    def read(self):
        return self._corpo

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _responde(monkeypatch, corpo, status=200):
    monkeypatch.setattr(
        gc.urllib.request, "urlopen", lambda *a, **k: _Resposta(corpo, status)
    )


# --------------------------------------- (a) falha não é passada vazia --

def test_shape_inesperado_e_FALHA_nao_vazia(monkeypatch):
    """O caso exato de 16/10: JSON válido, sem `result.messages_saved`.

    Antes isto imprimia `salvas None` e passava por passada vazia.
    """
    _responde(monkeypatch, '{"status": "ok", "job": "run-group-messages"}')

    ok, desc, salvas = gc._uma_passada("segredo")

    assert ok is False, "shape inesperado voltou a contar como passada boa"
    assert "shape inesperado" in desc
    assert salvas == 0


def test_resposta_nao_json_e_falha(monkeypatch):
    _responde(monkeypatch, "<html>502 Bad Gateway</html>")

    ok, desc, _ = gc._uma_passada("segredo")

    assert ok is False
    assert "não-JSON" in desc


def test_http_500_e_falha(monkeypatch):
    _responde(monkeypatch, '{"detail": "boom"}', status=500)

    ok, desc, _ = gc._uma_passada("segredo")

    assert ok is False
    assert "HTTP 500" in desc


def test_excecao_de_rede_e_falha(monkeypatch):
    def _explode(*a, **k):
        raise TimeoutError("estourou")

    monkeypatch.setattr(gc.urllib.request, "urlopen", _explode)

    ok, desc, _ = gc._uma_passada("segredo")

    assert ok is False
    assert "TimeoutError" in desc


def test_passada_boa_e_lida(monkeypatch):
    _responde(monkeypatch, """
    {"offset_used": 13, "next_offset": 22,
     "result": {"groups_synced": 9, "messages_saved": 8}}
    """)

    ok, desc, salvas = gc._uma_passada("segredo")

    assert ok is True
    assert salvas == 8
    assert "13" in desc and "22" in desc


def test_zero_salvas_e_passada_VAZIA_legitima(monkeypatch):
    """Zero com shape certo é vazia de verdade — tem de contar para secar."""
    _responde(monkeypatch, '{"offset_used": 0, "next_offset": 13,'
                           ' "result": {"groups_synced": 13, "messages_saved": 0}}')

    ok, _, salvas = gc._uma_passada("segredo")

    assert ok is True and salvas == 0


# ------------------------------------------- (b) o critério é SECAR --

@pytest.fixture
def _segredo(monkeypatch):
    monkeypatch.setattr(gc, "_segredo", lambda: "segredo-de-teste")


def _encadeia(monkeypatch, passadas):
    """Faz `_uma_passada` devolver, em ordem, cada tupla da lista."""
    fila = list(passadas)

    def _proxima(_segredo):
        return fila.pop(0) if fila else (True, "extra", 0)

    monkeypatch.setattr(gc, "_uma_passada", _proxima)


def test_para_depois_de_duas_secas(monkeypatch, _segredo, capsys):
    _encadeia(monkeypatch, [
        (True, "p1", 12),
        (True, "p2", 0),
        (True, "p3", 0),
    ])

    assert gc.main() == 0
    saida = capsys.readouterr().out
    assert "SECOU" in saida
    assert "Total recuperado: 12" in saida


def test_falha_no_meio_NAO_conta_como_seca_e_sai_diferente_de_zero(
    monkeypatch, _segredo, capsys
):
    """O caso de 16/10: uma passada vazia, uma FALHA, outra vazia.

    Se a falha contasse como seca, isto pararia com "secou" e daria a janela
    por coberta — foi assim que 16 mensagens ficaram para trás.
    """
    _encadeia(monkeypatch, [
        (True, "p1", 5),
        (True, "p2", 0),
        (False, "shape inesperado: {...}", 0),
        (True, "p4", 0),
        (True, "p5", 0),
    ])

    codigo = gc.main()
    saida = capsys.readouterr().out

    assert codigo == 2, "fechou com exit 0 apesar de ter página sem varrer"
    assert "FALHARAM" in saida
    assert "não é 'nada novo'" in saida
    assert "Rode o ciclo outra vez" in saida


def test_teto_sem_secar_avisa_e_nao_sai_zero(monkeypatch, _segredo, capsys):
    """Sempre trazendo coisa nova: não secou, e dizer que terminou seria falso."""
    _encadeia(monkeypatch, [(True, f"p{i}", 3) for i in range(1, 40)])

    codigo = gc.main()
    saida = capsys.readouterr().out

    assert codigo == 3
    assert "TETO" in saida
    assert "rode de novo" in saida.lower()


def test_sem_segredo_nao_roda(monkeypatch, capsys):
    monkeypatch.setattr(gc, "_segredo", lambda: "")

    assert gc.main() == 1


def test_uma_seca_sozinha_nao_para(monkeypatch, _segredo, capsys):
    """Uma passada vazia pode ser só a página de um grupo quieto.

    Parar na primeira encerraria o ciclo cedo — é por isso que o limiar é 2.
    """
    _encadeia(monkeypatch, [
        (True, "p1", 0),
        (True, "p2", 7),
        (True, "p3", 0),
        (True, "p4", 0),
    ])

    assert gc.main() == 0
    assert "Total recuperado: 7" in capsys.readouterr().out
