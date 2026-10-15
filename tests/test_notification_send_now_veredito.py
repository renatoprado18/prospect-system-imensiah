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

def test_send_now_examina_o_retorno():
    """Guarda do defeito estrutural: `return True` logo após o `await`.

    Procura a FORMA, não a palavra (prosa sobre o código vive no mesmo
    arquivo — ver `feedback_guarda_de_classe_por_ast_nao_substring`): o corpo
    tem de ligar o retorno do `send_text` a uma variável e consultá-la.
    """
    import ast

    caminho = os.path.join(_ROOT, "app", "services", "notification_router.py")
    with open(caminho, encoding="utf-8") as fh:
        arvore = ast.parse(fh.read())

    fn = next(
        (n for n in ast.walk(arvore)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_now"),
        None,
    )
    assert fn is not None, "_send_now desapareceu"

    # O await do send_text tem de estar ATRIBUÍDO (não descartado).
    atribui_envio = any(
        isinstance(no, ast.Assign)
        and isinstance(no.value, ast.Await)
        for no in ast.walk(fn)
    )
    assert atribui_envio, "o retorno do send_text está sendo descartado outra vez"

    # E tem de existir mais de um `return False` alcançável: o da exceção e o
    # do erro por retorno. Com um só, provavelmente o veredito voltou a ser
    # "não explodiu".
    falsos = [
        no for no in ast.walk(fn)
        if isinstance(no, ast.Return)
        and isinstance(no.value, ast.Constant)
        and no.value.value is False
    ]
    assert len(falsos) >= 3, (
        f"só {len(falsos)} `return False` — sem telefone, exceção e recusa "
        "da Evolution são três caminhos distintos de falha"
    )
