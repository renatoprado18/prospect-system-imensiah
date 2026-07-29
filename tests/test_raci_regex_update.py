"""
Testes da trava do caminho REGEX do RACI (`parse_raci_update`, fix 29/07).

Este e o path que le a resposta do Renato no grupo ("3 concluido") e escreve
direto no ConselhoOS. Dois defeitos latentes fechados aqui:

  1. RETROCESSO — nao havia comparacao de direcao, so de diferenca. "5 em
     andamento" sobre um item ja `concluido` reabria o item. Pesa mais que no
     path da IA porque a numeracao vem de uma lista RE-GERADA no momento da
     resposta: se algo mudou desde o envio do relatorio, o "5" do Renato pode
     estar apontando pra outro item. Travar o retrocesso limita o estrago desse
     desalinhamento a um no-op.

  2. `cancelado` NAO EXISTE no enum `raci_status` do ConselhoOS (os valores sao
     pendente | em_andamento | concluido | atrasado). O UPDATE estouraria com
     InvalidTextRepresentation. Nunca estourou porque este regex jamais casou
     em prod — 0 registros de `parse_raci_update` em `agent_actions` (medido no
     Neon 29/07; as 17 aplicacoes historicas vieram todas do path da IA).

Rodar: .venv/bin/python -m pytest tests/test_raci_regex_update.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import raci_weekly_report as rwr  # noqa: E402

EMPRESA = "9c246a3a-3e7e-4472-9854-ce83f6e22e14"
ITEM_ID = "4d5ac71c-c16d-4c87-84e6-f875ee8f6978"


class _Cur:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def close(self):
        pass


class _Conn:
    def __init__(self):
        self.cur = _Cur()
        self.committed = False

    def cursor(self, **kw):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        pass


@pytest.fixture
def banco(monkeypatch):
    """Report controlado + conexao fake. Devolve a conexao pra inspecionar se
    algum UPDATE chegou a sair."""
    conn = _Conn()

    def _montar(status_do_item_1):
        monkeypatch.setattr(rwr, "CONSELHOOS_DATABASE_URL", "postgresql://fake")
        monkeypatch.setattr(rwr, "generate_raci_report", lambda e: {
            "urgentes": [{"id": ITEM_ID, "acao": "Revisar contrato Aptus",
                          "status": status_do_item_1}],
            "atrasadas_mov": [], "no_prazo": [], "concluidas": [],
        })
        import psycopg2
        monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: conn)
        return conn
    return _montar


# ==================== retrocesso ====================

@pytest.mark.parametrize("msg,esperado", [
    ("1 em andamento", "em_andamento"),
    ("item 1 iniciado", "em_andamento"),
])
def test_resposta_no_grupo_nao_reabre_item_concluido(banco, msg, esperado, monkeypatch):
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("concluido")
    r = rwr.parse_raci_update(msg, EMPRESA)
    assert r["blocked"] == "downgrade"
    assert r["old_status"] == "concluido" and r["new_status"] == esperado
    assert conn.committed is False
    assert not any("UPDATE" in sql.upper() for sql, _ in conn.cur.executed)


def test_bloqueio_explica_por_onde_reabrir(banco, monkeypatch):
    """A recusa precisa dizer o caminho legitimo — senao vira parede muda."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    banco("concluido")
    r = rwr.parse_raci_update("1 em andamento", EMPRESA)
    assert "INTEL" in r["motivo"] or "ConselhoOS" in r["motivo"]


def test_progresso_continua_passando(banco, monkeypatch):
    """A trava nao pode custar o uso normal: fechar item pelo grupo."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("em_andamento")
    r = rwr.parse_raci_update("1 concluido", EMPRESA)
    assert r.get("blocked") is None
    assert r["new_status"] == "concluido"
    assert conn.committed is True


# ==================== status fora do enum ====================

@pytest.mark.parametrize("msg", ["1 cancelado", "item 1 removido"])
def test_cancelado_nao_vira_update_que_o_banco_recusa(banco, msg, monkeypatch):
    """`cancelado` nao esta no enum: gravar estouraria. Devolve o motivo."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("pendente")
    r = rwr.parse_raci_update(msg, EMPRESA)
    assert r["blocked"] == "status_inexistente"
    assert conn.committed is False
    assert not any("UPDATE" in sql.upper() for sql, _ in conn.cur.executed)


def test_cancelado_barra_antes_de_abrir_conexao(banco, monkeypatch):
    """Recusa e semantica, nao de banco: nao precisa nem consultar o item."""
    monkeypatch.setattr(rwr, "CONSELHOOS_DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(rwr, "generate_raci_report",
                        lambda e: pytest.fail("nao devia consultar o RACI"))
    assert rwr.parse_raci_update("1 cancelado", EMPRESA)["blocked"] == "status_inexistente"


# ==================== o que nao e update ====================

def test_mensagem_sem_padrao_nao_casa(banco, monkeypatch):
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    banco("pendente")
    assert rwr.parse_raci_update("bom dia a todos", EMPRESA) is None


def test_indice_fora_da_lista_nao_estoura(banco, monkeypatch):
    """Numeracao desalinhada e o risco conhecido deste path — nao pode virar
    IndexError nem escrever no item errado por acidente."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("pendente")
    assert rwr.parse_raci_update("9 concluido", EMPRESA) is None
    assert conn.committed is False
