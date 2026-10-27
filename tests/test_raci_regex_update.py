"""
Testes da trava do caminho REGEX do RACI (`parse_raci_update`, fix 29/07).

Este e o path que le a resposta do Renato no grupo ("3 concluido") e escreve no
RACI. Dois defeitos latentes fechados aqui:

  1. RETROCESSO — nao havia comparacao de direcao, so de diferenca. "5 em
     andamento" sobre um item ja `concluido` reabria o item. Pesa mais que no
     path da IA porque a numeracao vem de uma lista RE-GERADA no momento da
     resposta: se algo mudou desde o envio do relatorio, o "5" do Renato pode
     estar apontando pra outro item. Travar o retrocesso limita o estrago desse
     desalinhamento a um no-op.

  2. `cancelado` NAO EXISTE no CHECK de `status` (os valores sao pendente |
     em_andamento | concluido | atrasado — e isso vale nos DOIS bancos, medido em
     27/10). O UPDATE estouraria. Nunca estourou porque este regex jamais casou
     em prod — 0 registros de `parse_raci_update` em `agent_actions` (medido no
     Neon 29/07; as 17 aplicacoes historicas vieram todas do path da IA).

⚠️ 27/10/26 — ESTES TESTES DEIXARAM DE DUBLAR `psycopg2`, e vale dizer por que.
Antes a fixture fingia uma conexao e inspecionava o SQL que saia dela, o que
embutia a premissa de que esta funcao sabe em que banco escreve. Era ai que o
sistema estava errado: ela escrevia no ConselhoOS enquanto a matriz lia o INTEL.
Pior, a numeracao do "3 concluido" sai de `generate_raci_report` — se so a
escrita tivesse sido corrigida, o numero indexaria a lista de um banco e
escreveria no item de mesma posicao do outro. Hoje as duas pontas passam pelo
`raci_store` e a fixture dubla ELE; o roteamento tem teste proprio em
`test_raci_store.py`.

Rodar: .venv/bin/python -m pytest tests/test_raci_regex_update.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import raci_store  # noqa: E402
from services import raci_weekly_report as rwr  # noqa: E402

EMPRESA = "9c246a3a-3e7e-4472-9854-ce83f6e22e14"
ITEM_ID = "4d5ac71c-c16d-4c87-84e6-f875ee8f6978"

SCOPE = raci_store.RaciScope(
    target=raci_store.TARGET_INTEL, project_id=24,
    empresa_uuid=EMPRESA, nome="Vallen Clinic",
)


class _StoreSpy:
    """Registra as escritas tentadas. `committed` mantem o nome antigo dos
    asserts: aqui ele significa "alguma escrita de fato saiu"."""

    def __init__(self):
        self.updates = []           # [(ref, status, nota)]

    @property
    def committed(self) -> bool:
        return bool(self.updates)


@pytest.fixture
def banco(monkeypatch):
    """Report controlado + store dublado. Devolve o spy pra inspecionar se
    algum UPDATE chegou a sair.

    `n_itens` existe por causa de um furo que o controle negativo pegou em
    22/08: com a lista de 1 item, os testes do rodape ("✅ 6 concluídos.")
    passavam mesmo SEM o fix — o 6 caia no guard de indice fora da lista, nao na
    trava que estava sendo testada. Teste que passa pela razao errada certifica
    conformidade que nunca checou. Pra exercitar a trava, o item 6 tem que
    EXISTIR. [[feedback_controle_positivo_pega_o_furo_real]]
    """
    spy = _StoreSpy()

    def _montar(status_do_item_1, n_itens: int = 1):
        itens = [{"id": ITEM_ID, "acao": "Revisar contrato Aptus",
                  "status": status_do_item_1}]
        itens += [{"id": f"{i}d5ac71c-c16d-4c87-84e6-f875ee8f6978",
                   "acao": f"Item de enchimento {i}", "status": "em_andamento"}
                  for i in range(2, n_itens + 1)]
        monkeypatch.setattr(rwr, "generate_raci_report", lambda e: {
            "urgentes": itens,
            "atrasadas_mov": [], "no_prazo": [], "concluidas": [],
        })
        monkeypatch.setattr(raci_store, "resolve_scope", lambda _c: SCOPE)

        def update_item(_scope, ref, *, status=None, prazo=None, append_note=None):
            spy.updates.append((str(ref), status, append_note))
            return True

        monkeypatch.setattr(raci_store, "update_item", update_item)
        monkeypatch.setattr(raci_store, "undo_hint_update",
                            lambda s, r, old: f"-- undo {r} -> {old}")
        import services.agent_actions as aa
        monkeypatch.setattr(aa, "log_action", lambda *a, **k: None)
        return spy
    return _montar


# ==================== retrocesso ====================

@pytest.mark.parametrize("msg,esperado", [
    ("1 em andamento", "em_andamento"),
    ("item 1 iniciado", "em_andamento"),
])
def test_resposta_no_grupo_nao_reabre_item_concluido(banco, msg, esperado):
    spy = banco("concluido")
    r = rwr.parse_raci_update(msg, EMPRESA)
    assert r["blocked"] == "downgrade"
    assert r["old_status"] == "concluido" and r["new_status"] == esperado
    assert spy.committed is False


def test_bloqueio_explica_por_onde_reabrir(banco):
    """A recusa precisa dizer o caminho legitimo — senao vira parede muda."""
    banco("concluido")
    r = rwr.parse_raci_update("1 em andamento", EMPRESA)
    assert "matriz do projeto" in r["motivo"]


def test_progresso_continua_passando(banco):
    """A trava nao pode custar o uso normal: fechar item pelo grupo."""
    spy = banco("em_andamento")
    r = rwr.parse_raci_update("1 concluido", EMPRESA)
    assert r.get("blocked") is None
    assert r["new_status"] == "concluido"
    assert spy.committed is True


def test_escreve_no_item_apontado_pela_numeracao_do_report(banco):
    """A ESCRITA VAI NO ITEM QUE A LISTA NUMEROU — a regressao que a fonte
    unica tornou possivel.

    Com a leitura e a escrita em bancos diferentes, "1 concluido" resolveria a
    posicao 1 numa fonte e escreveria no id de mesma posicao da outra. Este
    teste trava o elo: o `ref` entregue ao store e' o `id` que o report devolveu
    naquela posicao, nao o indice nem outro id.
    """
    spy = banco("em_andamento", n_itens=13)
    rwr.parse_raci_update("3 concluido", EMPRESA)
    assert spy.updates, "o comando valido deixou de escrever"
    ref_escrito = spy.updates[0][0]
    assert ref_escrito == "3d5ac71c-c16d-4c87-84e6-f875ee8f6978"
    assert ref_escrito != "3", "escreveu a POSICAO em vez do id do item"


# ==================== status fora do enum ====================

@pytest.mark.parametrize("msg", ["1 cancelado", "item 1 removido"])
def test_cancelado_nao_vira_update_que_o_banco_recusa(banco, msg):
    """`cancelado` nao esta no CHECK: gravar estouraria. Devolve o motivo."""
    spy = banco("pendente")
    r = rwr.parse_raci_update(msg, EMPRESA)
    assert r["blocked"] == "status_inexistente"
    assert spy.committed is False


def test_cancelado_barra_antes_de_consultar_o_raci(banco, monkeypatch):
    """Recusa e semantica, nao de banco: nao precisa nem consultar o item."""
    monkeypatch.setattr(rwr, "generate_raci_report",
                        lambda e: pytest.fail("nao devia consultar o RACI"))
    assert rwr.parse_raci_update("1 cancelado", EMPRESA)["blocked"] == "status_inexistente"


# ==================== o que nao e update ====================

def test_mensagem_sem_padrao_nao_casa(banco):
    banco("pendente")
    assert rwr.parse_raci_update("bom dia a todos", EMPRESA) is None


def test_indice_fora_da_lista_nao_estoura(banco):
    """Numeracao desalinhada e o risco conhecido deste path — nao pode virar
    IndexError nem escrever no item errado por acidente."""
    spy = banco("pendente")
    assert rwr.parse_raci_update("9 concluido", EMPRESA) is None
    assert spy.committed is False


# ==================== relatorio nao e comando (fix 22/08) ====================
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


def test_relatorio_da_kelly_nao_vira_comando(banco):
    """Regressao do 21/08: a mensagem real que fechou o item errado.

    13 itens porque foi esse o tamanho da lista real — e porque com uma lista
    curta o "6" morreria no guard de indice, nao na trava."""
    spy = banco("em_andamento", n_itens=13)
    assert rwr.parse_raci_update(MSG_KELLY, EMPRESA) is None
    assert spy.committed is False


@pytest.mark.parametrize("msg", [
    "✅ 6 concluídos.",                            # o rodape exato
    "_✅ 6 concluídos._",                          # com formatacao WA
    "*6 concluidos*",
    "Total: 12 itens, 6 concluidos e 4 em andamento",
    "13 itens, 5 feitos",
    "resumo do mes: 8 prontos",
])
def test_resumo_numerico_nao_e_comando(banco, msg):
    """Contador de itens no plural conta, nao manda fechar. Cada um destes
    casava antes do fix e escreveria no item daquela posicao — por isso a lista
    tem 13 itens: os numeros citados (5, 6, 8, 12) precisam EXISTIR pra que a
    recusa venha da trava e nao do guard de indice."""
    spy = banco("em_andamento", n_itens=13)
    assert rwr.parse_raci_update(msg, EMPRESA) is None
    assert spy.committed is False


def test_mensagem_longa_nao_passa_pelo_regex(banco):
    """Teto de tamanho: mesmo com um comando VALIDO no meio, texto longo nao
    aplica direto — vai pro fallback da IA, que propoe em vez de escrever."""
    spy = banco("em_andamento")
    longa = "1 concluido\n" + ("contexto irrelevante " * 40)
    assert len(longa) > rwr.RACI_REGEX_MAX_CHARS
    assert rwr.parse_raci_update(longa, EMPRESA) is None
    assert spy.committed is False


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
def test_comando_curto_continua_aplicando(banco, msg, esperado):
    spy = banco("em_andamento")
    r = rwr.parse_raci_update(msg, EMPRESA)
    assert r is not None and r.get("blocked") is None, f"{msg!r} deixou de casar"
    assert r["new_status"] == esperado
    assert spy.committed is True


def test_comando_com_notas_continua_aplicando(banco):
    """O formato com detalhe (`5 em andamento: ...`) e' o que mais se aproxima
    do teto de caracteres — precisa continuar passando."""
    spy = banco("pendente")
    r = rwr.parse_raci_update(
        "1 em andamento: o Gustavo confirmou a procuracao pra sexta", EMPRESA)
    assert r.get("blocked") is None
    assert r["new_status"] == "em_andamento"
    assert "gustavo" in (r["notes"] or "").lower()
    assert spy.committed is True
    # A nota chega ao store concatenada e com carimbo de dia.
    assert "gustavo" in (spy.updates[0][2] or "").lower()
