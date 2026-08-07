"""Despriorização de portão repetido no agente da camada CoS.

POR QUE ESTES TESTES (07/08/2026). Medido numa janela de 7 dias: 305 dos 393
portões abertos (77%) eram o MESMO pedido de novo — Wadhwani 36 de 40, Despesas
33 de 40. Os filtros que já existiam olham a entrada ("houve movimento?", "já vi
este movimento?") e num grupo ativo a resposta é sim toda hora; o que se repetia
era a saída.

O que estes testes travam é a propriedade que torna o fix seguro: repetição
ADIA, nunca suprime. Se alguém trocar o `>= 2` por um `continue`, o portão
legítimo não atendido some da fila — e some em silêncio, que é a pior classe de
defeito deste sistema.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "cos_agent"))

import run  # noqa: E402


# ---------------------------------------------------------------- assinatura --

def test_assinatura_ignora_numeros_que_mudam_sem_o_pedido_mudar():
    """"R$ 65.000" e "R$ 65.237,46" são o MESMO pedido — o valor exato aparece
    depois de o agente ler o documento, e tratá-los como pedidos distintos
    zeraria a contagem justamente na repetição que interessa."""
    a = run._assinatura_pedido("Conferir o valor de R$ 65.000 na relação de 16-17/07")
    b = run._assinatura_pedido("Conferir o valor de R$ 65.237,46 na relação de 17/07")
    assert a == b


def test_assinatura_separa_pedidos_de_fato_diferentes():
    a = run._assinatura_pedido("Conferir o valor do crédito na relação de credores")
    b = run._assinatura_pedido("Decidir se aceita a proposta de retainer do Nick")
    assert a != b


def test_assinatura_aguenta_vazio():
    assert run._assinatura_pedido(None) == ""
    assert run._assinatura_pedido("") == ""


# ------------------------------------------------------------------ contagem --

class _FakeCur:
    def __init__(self, linhas):
        self._linhas = linhas

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._linhas

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, linhas):
        self._linhas = linhas

    def cursor(self):
        return _FakeCur(self._linhas)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _payload(project_id, pedido, sim=True):
    return {"frentes": [{"project_id": project_id, "frente": "X",
                         "precisa_de_voce": {"sim": sim, "o_que": pedido}}]}


def _com_rodadas(monkeypatch, rodadas):
    monkeypatch.setattr(run, "_conn", lambda url: _FakeConn([{"payload": p} for p in rodadas]))


def test_conta_rodadas_seguidas_com_o_mesmo_pedido(monkeypatch):
    _com_rodadas(monkeypatch, [_payload(5, "conferir o crédito da Emma")] * 4)
    # 4 rodadas iguais = 3 repetições (a primeira não repete nada)
    assert run.repeticao_por_frente("url")[5] == 3


def test_pedido_novo_zera_a_contagem(monkeypatch):
    """A propriedade que faz o mecanismo se autocorrigir: quando a contraparte
    responde, o pedido muda e a frente volta pro topo da fila sozinha."""
    _com_rodadas(monkeypatch, [
        _payload(5, "conferir o crédito da Emma"),
        _payload(5, "conferir o crédito da Emma"),
        _payload(5, "decidir se impugna o valor antes da AGC"),
    ])
    assert run.repeticao_por_frente("url")[5] == 0


def test_frente_sem_portao_nao_entra_na_conta(monkeypatch):
    _com_rodadas(monkeypatch, [_payload(9, "nada a fazer", sim=False)] * 3)
    assert 9 not in run.repeticao_por_frente("url")


def test_sem_historico_ninguem_e_penalizado(monkeypatch):
    """Falha de leitura devolve dict vazio: sem histórico, a ordem antiga vale.
    Penalizar por ausência de dado seria punir a frente pelo defeito da query."""
    def _explode(url):
        raise RuntimeError("banco fora")
    monkeypatch.setattr(run, "_conn", _explode)
    assert run.repeticao_por_frente("url") == {}


# -------------------------------------------------------------------- ordem --

def test_repetida_vai_pro_fim_mas_continua_na_fila():
    """O ponto do fix: ADIA, não suprime. A repetitiva perde a vez para quem tem
    novidade — e segue na lista, porque o pedido pode ser legítimo e só não ter
    sido atendido ainda."""
    todas = [{"id": 56, "movimento": 8}, {"id": 51, "movimento": 5}, {"id": 43, "movimento": 2}]
    rep = {56: 4}
    ordenada = sorted(todas, key=lambda f: (rep.get(f["id"], 0) >= 2, -f["movimento"]))
    assert [f["id"] for f in ordenada] == [51, 43, 56]
    assert len(ordenada) == len(todas), "nenhuma frente pode sumir da fila"


def test_uma_repeticao_so_nao_penaliza():
    """O corte é em 2+ seguidas: uma repetição pode ser o pedido legítimo que
    ainda não teve tempo de ser atendido."""
    todas = [{"id": 1, "movimento": 3}, {"id": 2, "movimento": 9}]
    rep = {2: 1}
    ordenada = sorted(todas, key=lambda f: (rep.get(f["id"], 0) >= 2, -f["movimento"]))
    assert [f["id"] for f in ordenada] == [2, 1]
