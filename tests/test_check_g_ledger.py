"""Testes do ledger do check G (06/08/2026).

O ledger existe pra medir o que a triagem descarta. Um medidor errado é PIOR
que nenhum — tem a cara de quem sabe. Estes testes travam os dois vieses que
quase entraram em produção na primeira versão:

  (a) TAUTOLOGIA — a v1 contava "o Renato agiu depois" como erro em TODOS os
      gates e cuspiu "59,5% de falso negativo". Mas o gate `bola_com_terceiro`
      suprime PORQUE ele já respondeu; "agiu depois" aparecia em 125 de 127.
      O critério de erro tem que mudar por gate. Número correto: 5,6%.

  (b) ZERO QUE SIGNIFICA "NÃO OLHEI" — mensagem sem ficha não tem `contact_id`
      pra cruzar. Contá-la como "sem erro" produz verde por ausência de
      medição, que é a família de defeito que originou esta tabela (37,7% do
      suprimido, na janela de 06/08). Tem que sair como `nao_medido`, com o
      percentual à vista.

Rodar: .venv/bin/python -m pytest tests/test_check_g_ledger.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

os.environ.setdefault("DB_TARGET", "local")

import pytest  # noqa: E402
from services import check_g_ledger as led  # noqa: E402


def _medir_com(linhas, monkeypatch):
    """Roda medir() sobre linhas forjadas, sem tocar no banco."""
    class _Cur:
        def execute(self, *a, **kw):
            pass

        def fetchall(self):
            return linhas

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(led, "get_db", lambda: _Conn())
    return led.medir(dias=14)


def _linha(gate, n, virou_task=0, agiu=0, nao_medido=0, virou_ficha=0, decisao="suprimido"):
    return {"gate": gate, "decisao": decisao, "n": n, "virou_task": virou_task,
            "agiu": agiu, "nao_medido": nao_medido, "virou_ficha": virou_ficha}


# --------------------------------------------------------------------------
# (a) o critério de erro muda por gate
# --------------------------------------------------------------------------

def test_agiu_depois_nao_e_erro_no_gate_de_quem_ja_respondeu(monkeypatch):
    """O caso real de 06/08: 127 suprimidas, 117 com ação depois, 8 viraram task.

    Se "agiu" contasse, daria 98% de falso negativo — e seria tautologia pura,
    porque o gate suprime exatamente quem ele já respondeu.
    """
    r = _medir_com([_linha(led.GATE_BOLA_DELE, 127, virou_task=8, agiu=117)], monkeypatch)
    assert r["erros_do_gate"] == 8, "só 'virou_task' acusa neste gate"
    assert r["falso_negativo_pct"] == pytest.approx(6.3, abs=0.1)


def test_ruido_comercial_acusa_com_qualquer_dos_dois(monkeypatch):
    """Promoção não deveria gerar ação NEM tarefa — aqui os dois acusam."""
    r = _medir_com([_linha(led.GATE_RUIDO, 4, virou_task=1, agiu=2)], monkeypatch)
    assert r["erros_do_gate"] == 3


def test_o_que_foi_mostrado_nunca_conta_como_erro(monkeypatch):
    """Erro do check é o que ele ESCONDEU. O exibido foi julgado por um humano."""
    r = _medir_com([
        _linha("mostrado", 71, agiu=42, decisao="mostrado"),
        _linha(led.GATE_RUIDO, 2, virou_task=1),
    ], monkeypatch)
    assert r["suprimidas"] == 2
    assert r["erros_do_gate"] == 1


# --------------------------------------------------------------------------
# (b) o não-medido não vira zero
# --------------------------------------------------------------------------

def test_sem_ficha_sai_como_nao_medido_e_nao_como_acerto(monkeypatch):
    """97 de 257 na medição real. Se virassem 'sem erro', o painel ficaria verde
    por não ter olhado — exatamente o defeito que a tabela existe pra caçar."""
    r = _medir_com([
        _linha(led.GATE_SEM_FICHA, 97, nao_medido=97),
        _linha(led.GATE_BOLA_DELE, 127, virou_task=8, agiu=117),
        _linha(led.GATE_JA_RESOLVIDA, 32, agiu=27),
        _linha(led.GATE_RUIDO, 1, agiu=1),
    ], monkeypatch)
    assert r["nao_medido"] == 97
    assert r["nao_medido_pct"] == pytest.approx(37.7, abs=0.2)
    # 9 erros sobre 160 medidas (257 - 97), não sobre 257: o denominador é o que
    # deu pra verificar.
    assert r["erros_do_gate"] == 9
    assert r["falso_negativo_pct"] == pytest.approx(5.6, abs=0.1)


def test_tudo_nao_medido_nao_vira_zero_por_cento(monkeypatch):
    """Nenhuma medida ⇒ a taxa é None (desconhecida), nunca 0% (perfeita)."""
    r = _medir_com([_linha(led.GATE_SEM_FICHA, 40, nao_medido=40)], monkeypatch)
    assert r["falso_negativo_pct"] is None
    assert r["nao_medido_pct"] == 100.0


def test_todo_gate_declara_o_criterio_que_usa(monkeypatch):
    """Critério implícito é critério que ninguém audita."""
    r = _medir_com([
        _linha(led.GATE_BOLA_DELE, 1), _linha(led.GATE_SEM_FICHA, 1, nao_medido=1),
        _linha(led.GATE_RUIDO, 1), _linha(led.GATE_JA_RESOLVIDA, 1),
    ], monkeypatch)
    for g in r["por_gate"]:
        assert g["criterio_de_erro"], f"gate {g['gate']} sem critério declarado"
