"""
Testes da guarda de REPLAY do webhook Evolution (incidente de 04/08/2026).

Contexto: o commit `a7654f4` fechou `/api/webhooks/whatsapp` com auth achando que
a rota nao tinha consumidor -- o consumidor dela e a Evolution, que fica FORA do
repo. Resultado: 3h sem ingestao e 93 mensagens perdidas, 8 delas `fromMe`.

Pra fazer o backfill dessas 93 foi preciso reprocessar mensagens antigas pelo
handler. So que o handler dispara `process_renato_reply` quando ve uma resposta a
proposta vinda do Renato -- isso AGE (trata a proposta e responde no WhatsApp).
Reexecutar sobre mensagem antiga e agir no mundo pela segunda vez, com o estado de
hoje. O `wa_catchup` ja mandava `_catchup: True` no payload desde sempre; ninguem
lia esse campo, e ele roda /30min em producao.

Estes testes provam que:
  (a) replay NAO dispara `process_renato_reply` -- nem em messages.upsert nem em
      send.message;
  (b) ao vivo (sem `_catchup`) o disparo CONTINUA acontecendo -- a guarda nao pode
      matar o caminho normal;
  (c) a decisao do replay fica auditavel (`proposal_response_replay`), e nao
      invisivel;
  (d) essa decisao consta em `_TERMINAL_DECISIONS` do wa_catchup -- senao a
      mensagem, que nunca vira linha em `messages`, e re-detectada como 'missing'
      toda run (o livelock medido em 25/07).

Rodar: python3 -m pytest tests/test_webhook_replay_guard.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import asyncio  # noqa: E402
import pytest  # noqa: E402
from integrations import evolution_api  # noqa: E402


# Vocabulario REAL do detector (`is_proposal_response`): digito 1-4, emoji
# numerico, "#123", ou comando ("pendentes"/"ignorar"/"lista"). Nao aceita
# "aprovo" -- foi o que este assert pegou na primeira rodada. Se o vocabulario
# mudar, o teste falha alto em vez de virar no-op silencioso
# (ver feedback_filtro_vocabulario_errado_falha_calado).
_RESPOSTA = "1"


@pytest.fixture(autouse=True)
def _guarda_o_detector():
    assert evolution_api.is_proposal_response(_RESPOSTA), (
        f"'{_RESPOSTA}' deixou de ser resposta-a-proposta; este teste vira no-op silencioso"
    )


@pytest.fixture
def disparos(monkeypatch):
    """Intercepta o efeito de saida. Lista vazia = nada agiu no mundo.

    Deixa o `asyncio.create_task` real rodar (o handler o chama de dentro da
    funcao) -- e o caminho de producao. Quem testa da o `await asyncio.sleep(0)`
    pra a task agendada executar.
    """
    capturados = []

    async def _fake_reply(content, phone):
        capturados.append((content, phone))

    monkeypatch.setattr(evolution_api, "process_renato_reply", _fake_reply)
    return capturados


def _upsert(texto):
    return {
        "key": {"remoteJid": "5511984153337@s.whatsapp.net", "fromMe": True, "id": "MSGID1"},
        "message": {"conversation": texto},
        "messageTimestamp": 1785778000,
        "pushName": "Renato",
    }


def _sent(texto):
    return {
        "key": {"remoteJid": "5511984153337@s.whatsapp.net", "fromMe": True, "id": "MSGID2"},
        "message": {"conversation": texto},
    }


# ------------------------------------------------------------ messages.upsert --

@pytest.mark.asyncio
async def test_replay_nao_dispara_acao_sobre_proposta(disparos, monkeypatch):
    monkeypatch.setattr(evolution_api, "_record_webhook_audit", lambda **k: None)

    r = await evolution_api.process_incoming_message(_upsert(_RESPOSTA), replay=True)
    await asyncio.sleep(0)

    assert disparos == [], "replay disparou process_renato_reply -- agiu no mundo 2x"
    assert r["reason"] == "proposal_response_replay"
    assert r["processed"] is False


@pytest.mark.asyncio
async def test_ao_vivo_continua_disparando(disparos, monkeypatch):
    """A guarda nao pode matar o caminho normal."""
    monkeypatch.setattr(evolution_api, "_record_webhook_audit", lambda **k: None)

    r = await evolution_api.process_incoming_message(_upsert(_RESPOSTA), replay=False)
    await asyncio.sleep(0)

    assert disparos, "ao vivo parou de tratar resposta a proposta -- a guarda vazou"
    assert r["reason"] == "proposal_response"


@pytest.mark.asyncio
async def test_replay_fica_auditavel(monkeypatch):
    """Suprimir em silencio esconderia o replay de quem for investigar depois."""
    vistos = []
    monkeypatch.setattr(
        evolution_api, "_record_webhook_audit",
        lambda **k: vistos.append((k.get("decision"), k.get("decision_reason"))),
    )
    monkeypatch.setattr(evolution_api, "process_renato_reply", lambda c, p: None)

    await evolution_api.process_incoming_message(
        _upsert(_RESPOSTA), audit_ctx={"source": "evolution_webhook"}, replay=True
    )

    assert ("skipped", "proposal_response_replay") in vistos


# --------------------------------------------------------------- send.message --

@pytest.mark.asyncio
async def test_send_message_replay_nao_dispara(disparos):
    r = await evolution_api.process_sent_message(_sent(_RESPOSTA), replay=True)
    await asyncio.sleep(0)

    assert disparos == [], "replay de send.message agiu sobre a proposta"
    assert r["reason"] == "proposal_response_replay"


@pytest.mark.asyncio
async def test_send_message_ao_vivo_dispara(disparos):
    r = await evolution_api.process_sent_message(_sent(_RESPOSTA), replay=False)
    await asyncio.sleep(0)

    assert disparos, "send.message ao vivo parou de tratar proposta"
    assert r["reason"] == "proposal_response"


# ------------------------------------------------------- livelock do catchup --

def test_decisao_de_replay_e_terminal_pro_catchup():
    """Sem isto o catchup re-detecta a msg como 'missing' toda run, pra sempre."""
    from services.wa_catchup import _TERMINAL_DECISIONS

    assert "proposal_response_replay" in _TERMINAL_DECISIONS


def test_catchup_marca_payload_como_replay():
    """A ponta que produz o replay tem que casar com a ponta que le.

    O `_catchup: True` existia e ninguem lia -- e por isso que a guarda nao
    existia. Se alguem renomear a chave de um lado so, isto falha.
    """
    import inspect
    from services import wa_catchup

    src = inspect.getsource(wa_catchup._replay_through_webhook)
    assert '"_catchup": True' in src

    handler = inspect.getsource(evolution_api.handle_evolution_webhook)
    assert 'payload.get("_catchup")' in handler


# ------------------------------------------- hora da mensagem x hora da gravacao --

def test_dm_grava_hora_da_mensagem_nao_hora_da_gravacao():
    """O INSERT de DM tem que datar pela mensagem, nao por `now()`.

    Era `timestamp = datetime.now()`. Ao vivo a diferenca e de segundos e passa
    despercebida; em replay a mensagem entra datada de HOJE. No backfill de
    04/08 o fio do Marcelo Domenico (ontem 17:11 BRT) foi gravado como 12:56 de
    hoje -- quem le por recencia ve conversa velha como quente.

    Le o fonte porque o INSERT exige DB; o que importa aqui e que a linha
    `datetime.now()` nao volte num refactor.
    """
    import inspect
    from integrations import evolution_api as ev

    src = inspect.getsource(ev.process_incoming_message)
    assert "timestamp = datetime.now()" not in src, (
        "DM voltou a datar pela hora da gravacao -- replay/catchup vao mentir a data"
    )
    assert 'data.get("messageTimestamp")' in src


def test_grupo_ja_datava_pela_mensagem():
    """Guarda o caminho que ja estava certo, pra simetria nao se perder.

    `persist_group_message_realtime` sempre datou pelo `messageTimestamp` -- foi
    a comparacao com ele que revelou que a DM nao fazia o mesmo.
    """
    import inspect
    from integrations import evolution_api as ev

    src = inspect.getsource(ev.persist_group_message_realtime)
    assert "messageTimestamp" in src, "o caminho de grupo perdeu o messageTimestamp"
    assert "datetime.now()" not in src
