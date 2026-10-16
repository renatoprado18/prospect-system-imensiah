"""`_send_now` reportava sucesso quando a Evolution RECUSAVA o envio (15/10/2026).

O incidente, ponta a ponta: em 14/10 18h59 a instância `rap-whatsapp` caiu
(`close`/403 — o Renato re-registrou o número no WhatsApp comum) e o INTEL
ficou 16,3h sem ingestão. Em 15/10 11h06 o heartbeat recém-consertado detectou
e disparou "WhatsApp DESCONECTADO do INTEL", urgência 9. O router mandou para
`canal=whatsapp` e gravou `sent_ok=True` — pela instância que estava caída. O
Renato confirmou que **não recebeu**.

A causa não era o canal: era o veredito. `_send_now` fazia
`await client.send_text(...)` e `return True` incondicional, protegido por um
`except Exception` que **nunca podia disparar**, porque o `_request` do
`evolution_api` não levanta em erro HTTP — devolve
`{"error":..., "status_code":...}`. Guarda que não pode disparar é decorativa.

Detectar sem alcançar é pior que não detectar: deixa a tela verde do lado de
quem deveria agir. E o conserto desbloqueia o resto sozinho — o chamador já
rebaixa `False` para pill (ledger durável + badge + digest), escada que existia
e era anulada pelo falso sucesso.

Rodar: .venv/bin/python -m pytest tests/test_notification_send_now_veredito.py -v
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import notification_router as nr  # noqa: E402


class _ClienteFake:
    """Dublê do EvolutionAPIClient que devolve o que lhe mandarem devolver.

    Devolve — não levanta. É esse o ponto: o cliente real foi desenhado para
    sinalizar erro pelo RETORNO, e um dublê que levantasse exceção testaria um
    mundo que não existe, dando verde ao bug ([[feedback_fake_frouxo_da_verde_a_bug_real]]).
    """

    def __init__(self, resposta):
        self.resposta = resposta
        self.chamadas = []

    async def send_text(self, phone, message, instance_name=None):
        self.chamadas.append((phone, message, instance_name))
        if isinstance(self.resposta, Exception):
            raise self.resposta
        return self.resposta


@pytest.fixture(autouse=True)
def _telefone(monkeypatch):
    monkeypatch.setenv(nr.RENATO_PHONE_ENV, "5511999999999")


def _arma(monkeypatch, resposta):
    cliente = _ClienteFake(resposta)
    import integrations.evolution_api as ev
    monkeypatch.setattr(ev, "get_evolution_client", lambda: cliente)
    return cliente


def _envia(monkeypatch, resposta):
    cliente = _arma(monkeypatch, resposta)
    ok = asyncio.run(nr._send_now("mensagem de teste"))
    return ok, cliente


# ------------------------------------------- o caso real de 14-15/10/2026 --

def test_instancia_caida_nao_conta_como_enviado(monkeypatch):
    """O caso que custou o aviso.

    Com a instância em `close`, a Evolution recusa o sendText. O `_request`
    devolve o erro em vez de levantar, e o `return True` antigo o convertia em
    sucesso — `sent_ok=True` sobre um aviso que nunca chegou.
    """
    ok, cliente = _envia(monkeypatch, {
        "error": 'Bad Request: "Connection Closed"',
        "status_code": 400,
    })

    assert ok is False, "recusa da Evolution voltou a contar como envio"
    assert cliente.chamadas, "nem tentou enviar"


def test_erro_sem_status_code_tambem_reprova(monkeypatch):
    """`_request` devolve `{"error": ...}` sem `status_code` quando a API não
    está configurada. Não pode virar sucesso."""
    ok, _ = _envia(monkeypatch, {"error": "Evolution API não configurada"})

    assert ok is False


def test_status_500_reprova(monkeypatch):
    ok, _ = _envia(monkeypatch, {"error": "boom", "status_code": 500})

    assert ok is False


def test_excecao_continua_reprovando(monkeypatch):
    """O caminho antigo (rede caindo de fato) não pode ter regredido."""
    ok, _ = _envia(monkeypatch, RuntimeError("conexão morreu"))

    assert ok is False


# --------------------------------------- e o falso NEGATIVO, que é o risco --
#
# Reprovar demais trocaria um alarme mudo por um alarme atrasado: todo envio
# bom cairia em pill. Por isso o veredito só reprova com SINAL EXPLÍCITO de
# erro, e estes testes fixam isso.


def test_sucesso_normal_da_evolution_passa(monkeypatch):
    ok, _ = _envia(monkeypatch, {
        "key": {"id": "3EB0ABC", "remoteJid": "5511999999999@s.whatsapp.net"},
        "status": "PENDING",
    })

    assert ok is True


def test_resposta_irreconhecivel_nao_reprova(monkeypatch):
    """Shape que não entendo não é erro — tratá-lo como falha rebaixaria
    envio bom. O veredito exige sinal de erro, não ausência de confirmação."""
    for resposta in ({}, None, [], "ok", {"status": "PENDING"}):
        ok, _ = _envia(monkeypatch, resposta)
        assert ok is True, f"reprovou resposta legítima: {resposta!r}"


def test_status_2xx_explicito_passa(monkeypatch):
    ok, _ = _envia(monkeypatch, {"status_code": 201, "key": {"id": "X"}})

    assert ok is True


def test_sem_telefone_nao_tenta(monkeypatch):
    monkeypatch.delenv(nr.RENATO_PHONE_ENV, raising=False)
    monkeypatch.setattr(nr, "RENATO_PHONE_FALLBACK", "")
    cliente = _arma(monkeypatch, {"key": {"id": "X"}})

    assert asyncio.run(nr._send_now("x")) is False
    assert not cliente.chamadas, "tentou enviar sem destinatário"


# --------------------------------------------------------- guarda de classe --

def _funcao(nome):
    import ast

    caminho = os.path.join(_ROOT, "app", "services", "notification_router.py")
    with open(caminho, encoding="utf-8") as fh:
        arvore = ast.parse(fh.read())
    fn = next(
        (n for n in ast.walk(arvore)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == nome),
        None,
    )
    assert fn is not None, f"{nome} desapareceu"
    return ast, fn


def test_quem_envia_examina_o_retorno():
    """Guarda do defeito estrutural: `return True` logo após o `await`.

    Procura a FORMA, não a palavra (prosa sobre o código vive no mesmo
    arquivo — ver `feedback_guarda_de_classe_por_ast_nao_substring`): o corpo
    tem de ligar o retorno do `send_text` a uma variável e consultá-la.

    ⚠️ Esta guarda já reprovou uma refatoração legítima — quando o envio saiu
    de `_send_now` para `_tenta_envio` — e isso é ela funcionando: avisou que a
    estrutura mudou. Guarda de classe amarrada a um NOME é frágil por desenho;
    o que importa é que *quem faz o envio* leia o retorno.
    """
    ast, fn = _funcao("_tenta_envio")

    atribui_envio = any(
        isinstance(no, ast.Assign) and isinstance(no.value, ast.Await)
        for no in ast.walk(fn)
    )
    assert atribui_envio, "o retorno do send_text está sendo descartado outra vez"

    falsos = [
        no for no in ast.walk(fn)
        if isinstance(no, ast.Return)
        and isinstance(no.value, ast.Constant)
        and no.value.value is False
    ]
    assert len(falsos) >= 2, (
        f"só {len(falsos)} `return False` — exceção e recusa por retorno são "
        "dois caminhos distintos de falha"
    )


def test_send_now_tem_a_escada_de_duas_instancias():
    """O alarme não pode depender só do canal que ele monitora.

    `_send_now` tem de tentar a instância padrão e, falhando, a do bot — que
    foi a única de WhatsApp a sobreviver às duas quedas.
    """
    ast, fn = _funcao("_send_now")

    tentativas = [
        no for no in ast.walk(fn)
        if isinstance(no, ast.Call)
        and isinstance(no.func, ast.Name)
        and no.func.id == "_tenta_envio"
    ]
    assert len(tentativas) >= 2, (
        f"só {len(tentativas)} tentativa(s) — sem a 2ª instância, a queda do "
        "número do Renato leva o aviso embora junto"
    )

    nomes = {
        n.id for no in ast.walk(fn) for n in ast.walk(no) if isinstance(n, ast.Name)
    }
    assert "INTEL_BOT_INSTANCE" in nomes, "a 2ª instância deixou de ser a do bot"


# ------------------------- a 2ª instância: o canal que sobreviveu às quedas --
#
# O alarme mais importante do sistema ("o WhatsApp parou de entrar") saía pela
# instância `rap-whatsapp`, que é o número do próprio Renato — o alvo do
# alarme. Nas quedas de 14/10 e 15/10 ele confirmou não ter recebido nada. O
# `intel-bot-v2` é outro número e ficou `open` nas DUAS.


class _ClientePorInstancia:
    """Devolve resposta diferente por instância, e registra a ordem."""

    def __init__(self, por_instancia, padrao):
        self.por_instancia = por_instancia
        self.padrao = padrao
        self.ordem = []

    def _resp(self, instancia):
        return self.por_instancia.get(instancia, self.padrao)

    async def send_text(self, phone, message, instance_name=None):
        self.ordem.append(instance_name)
        r = self._resp(instance_name)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def _instancias(monkeypatch):
    import integrations.evolution_api as ev
    from services import intel_bot

    estado = {}

    def _arma(por_instancia, padrao):
        c = _ClientePorInstancia(por_instancia, padrao)
        estado["cliente"] = c
        monkeypatch.setattr(ev, "get_evolution_client", lambda: c)
        monkeypatch.setattr(ev, "EvolutionAPIClient", lambda instance_name=None: c)
        monkeypatch.setattr(intel_bot, "INTEL_BOT_INSTANCE", "intel-bot-v2")
        return c

    return _arma


def test_principal_falha_e_o_BOT_entrega(_instancias):
    """O caso das duas quedas: principal em `close`, bot de pé."""
    cliente = _instancias(
        {"intel-bot-v2": {"key": {"id": "OK"}, "status": "PENDING"}},
        {"error": 'Bad Request: "Connection Closed"', "status_code": 400},
    )

    assert asyncio.run(nr._send_now("alarme")) is True
    assert cliente.ordem == [None, "intel-bot-v2"], (
        "tem de tentar a padrão ANTES do bot, e só uma vez cada"
    )


def test_principal_de_pe_NAO_usa_o_bot(_instancias):
    """Sem isto, todo urgente sairia duplicado — dois remetentes."""
    cliente = _instancias({}, {"key": {"id": "OK"}, "status": "PENDING"})

    assert asyncio.run(nr._send_now("rotina")) is True
    assert cliente.ordem == [None], "mandou pelo bot com a principal funcionando"


def test_as_duas_falham_rebaixa_para_pill(_instancias):
    """Com os dois canais fora, `False` manda o chamador enfileirar o pill."""
    cliente = _instancias({}, {"error": "boom", "status_code": 500})

    assert asyncio.run(nr._send_now("alarme")) is False
    assert cliente.ordem == [None, "intel-bot-v2"]


def test_excecao_na_principal_ainda_tenta_o_bot(_instancias):
    cliente = _instancias(
        {"intel-bot-v2": {"key": {"id": "OK"}}},
        RuntimeError("socket morreu"),
    )

    assert asyncio.run(nr._send_now("alarme")) is True
    assert "intel-bot-v2" in cliente.ordem
