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
    algum UPDATE chegou a sair.

    `n_itens` existe por causa de um furo que o controle negativo pegou em
    23/08: com a lista de 1 item, os testes do rodape ("✅ 6 concluídos.")
    passavam mesmo SEM o fix — o 6 caia no guard de indice fora da lista, nao na
    trava que estava sendo testada. Teste que passa pela razao errada certifica
    conformidade que nunca checou. Pra exercitar a trava, o item 6 tem que
    EXISTIR. [[feedback_controle_positivo_pega_o_furo_real]]
    """
    conn = _Conn()

    def _montar(status_do_item_1, n_itens: int = 1):
        itens = [{"id": ITEM_ID, "acao": "Revisar contrato Aptus",
                  "status": status_do_item_1}]
        itens += [{"id": f"{i}d5ac71c-c16d-4c87-84e6-f875ee8f6978",
                   "acao": f"Item de enchimento {i}", "status": "em_andamento"}
                  for i in range(2, n_itens + 1)]
        monkeypatch.setattr(rwr, "CONSELHOOS_DATABASE_URL", "postgresql://fake")
        monkeypatch.setattr(rwr, "generate_raci_report", lambda e: {
            "urgentes": itens,
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


# ==================== relatorio nao e comando (fix 23/08) ====================
#
# Defeito de 21/08: a Kelly mandou o RACI completo da Alba no grupo e o rodape
# "✅ 6 concluídos." casou o regex. O bot marcou o 6o item da lista posicional —
# "Zerar o passivo da Alba" — como concluido e confirmou no grupo 7s depois,
# sendo que a propria mensagem o classificava como em andamento, previsao 30/10.
#
# A classe do defeito importa mais que o item: TODO relatorio de RACI termina com
# um resumo numerico, entao todo relatorio era um comando em potencial.

# Trecho real da msg 824445 (group_messages, prod) — reduzido, mas com o rodape
# intacto e acima do teto de caracteres.
MSG_KELLY = """Boa tarde a todos! Segue a RACI atualizada

📋 RACI — Alba Consultoria
21/08/2026

✅ Realizados (6):
* Corrigir posicionamento institucional de "pró-bono" para "pró-business" — Sandra (29/07/2026) Realizada.
* Enviar o questionário aos futuros sócios e consolidar respostas — Sandra (08/08/2026) Realizado.

🔄 Em andamento (7):
* Zerar o passivo da Alba via aporte da família da Rosa — Família Rosa (17/08/2026) *Em andamento, previsão 30/10*.
* Opção pelo Simples em setembro (reversível até novembro) — Larisse (30/09/2026) *Em andamento*.

✅ 6 concluídos.

13 itens no total."""


def test_relatorio_da_kelly_nao_vira_comando(banco, monkeypatch):
    """Regressao do 21/08: a mensagem real que fechou o item errado.

    13 itens porque foi esse o tamanho da lista real — e porque com uma lista
    curta o "6" morreria no guard de indice, nao na trava."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("em_andamento", n_itens=13)
    assert rwr.parse_raci_update(MSG_KELLY, EMPRESA) is None
    assert conn.committed is False
    assert not any("UPDATE" in sql.upper() for sql, _ in conn.cur.executed)


@pytest.mark.parametrize("msg", [
    "✅ 6 concluídos.",                            # o rodape exato
    "_✅ 6 concluídos._",                          # com formatacao WA
    "*6 concluidos*",
    "Total: 12 itens, 6 concluidos e 4 em andamento",
    "13 itens, 5 feitos",
    "resumo do mes: 8 prontos",
])
def test_resumo_numerico_nao_e_comando(banco, msg, monkeypatch):
    """Contador de itens no plural conta, nao manda fechar. Cada um destes
    casava antes do fix e escreveria no item daquela posicao — por isso a lista
    tem 13 itens: os numeros citados (5, 6, 8, 12) precisam EXISTIR pra que a
    recusa venha da trava e nao do guard de indice."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("em_andamento", n_itens=13)
    assert rwr.parse_raci_update(msg, EMPRESA) is None
    assert conn.committed is False


def test_mensagem_longa_nao_passa_pelo_regex(banco, monkeypatch):
    """Teto de tamanho: mesmo com um comando VALIDO no meio, texto longo nao
    aplica direto — vai pro fallback da IA, que propoe em vez de escrever."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("em_andamento")
    longa = "1 concluido\n" + ("contexto irrelevante " * 40)
    assert len(longa) > rwr.RACI_REGEX_MAX_CHARS
    assert rwr.parse_raci_update(longa, EMPRESA) is None
    assert conn.committed is False


# ==================== controle positivo ====================
# Sem isto, um regex que nunca casa passaria em todos os testes acima. As travas
# so valem se o uso legitimo continuar escrevendo.

@pytest.mark.parametrize("msg,esperado", [
    ("1 concluido", "concluido"),
    ("1 concluído", "concluido"),
    ("item 1 concluido", "concluido"),
    ("#1 feito", "concluido"),
    ("  1 pronto  ", "concluido"),
])
def test_comando_curto_continua_aplicando(banco, msg, esperado, monkeypatch):
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("em_andamento")
    r = rwr.parse_raci_update(msg, EMPRESA)
    assert r is not None and r.get("blocked") is None, f"{msg!r} deixou de casar"
    assert r["new_status"] == esperado
    assert conn.committed is True


def test_comando_com_notas_continua_aplicando(banco, monkeypatch):
    """O formato com detalhe (`5 em andamento: ...`) e' o que mais se aproxima
    do teto de caracteres — precisa continuar passando."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = banco("pendente")
    r = rwr.parse_raci_update(
        "1 em andamento: o Gustavo confirmou a procuracao pra sexta", EMPRESA)
    assert r.get("blocked") is None
    assert r["new_status"] == "em_andamento"
    assert "gustavo" in (r["notes"] or "").lower()
    assert conn.committed is True
