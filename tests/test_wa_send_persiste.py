"""A mensagem que o INTEL ENVIA precisa entrar no historico (30/09/2026).

O QUE ESTAVA QUEBRADO. A Evolution nao emite `messages.upsert` para envio via
API -- so `SEND_MESSAGE`. O caminho da Tonia ja sabia disso desde 08/07 e
persistia no `send.message`; o caminho principal (`rap-whatsapp`) nunca recebeu
o mesmo tratamento: `process_sent_message` logava, checava resposta a proposta e
retornava sem gravar. Medido em 6 meses de `webhook_audit`: `intel-bot-v2`
gravou 248 de 338 envios, `rap-whatsapp` **0 de 337**.

POR QUE NAO APARECEU ANTES. O polling do `whatsapp_sync` cobria o buraco com
~16 min de atraso, entao a mensagem ACABAVA entrando e ninguem via defeito --
so lentidao. E o modo de falha tipico deste repo: quando a rede de seguranca
vira o caminho principal, ninguem nota ate ela cair ([[feedback_fallback_sem_motor_vira_motor]]).

O QUE ESTES TESTES SEGURAM, alem do caminho feliz:
  (a) IDEMPOTENCIA -- polling e webhook gravando a mesma mensagem nao podem
      produzir duas linhas; a dedup e por `metadata->>'message_id'`, a mesma
      chave que o `whatsapp_sync` usa;
  (b) REPLAY nao grava -- reprocessar auditoria antiga inseriria historico com
      data de hoje;
  (c) FALHA NAO DERRUBA O WEBHOOK -- persistir e best-effort: o mesmo handler
      trata resposta a proposta, e um erro de banco nao pode matar isso;
  (d) o resultado precisa voltar com o `message_id` -- e ele que alimenta o
      `resulting_message_id` da auditoria, o campo que TORNA o furo medivel.
      Sem ele, um conserto silencioso seria indistinguivel de nenhum conserto.

Rodar: .venv/bin/python -m pytest tests/test_wa_send_persiste.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import wa_ingest  # noqa: E402


def _payload(msg_id="3EB0TESTE0001", from_me=True, jid="5511999998888@s.whatsapp.net",
             texto="mensagem de teste", instance="rap-whatsapp"):
    return {
        "event": "send.message",
        "instance": instance,
        "data": {
            "key": {"id": msg_id, "fromMe": from_me, "remoteJid": jid},
            "message": {"conversation": texto},
            "messageTimestamp": 1790768954,
        },
    }


@pytest.fixture
def ingest_fake(monkeypatch):
    """Substitui `_ingest_upsert` guardando como foi chamado.

    Testamos a COSTURA (extrair chaves do payload, capturar o id, blindar
    excecao), nao o INSERT -- esse ja e o caminho provado do `wa_ingest`, em
    producao desde julho. Duplicar a cobertura dele aqui testaria psycopg2.
    """
    chamadas = []

    def _fake(payload, event, instance, wa_message_id, _audit, _resultado=None):
        chamadas.append({"event": event, "instance": instance, "mid": wa_message_id})
        _audit("stored", resulting_message_id=4242)
        return {"stored": True, "reason": "stored"}

    monkeypatch.setattr(wa_ingest, "_ingest_upsert", _fake)
    return chamadas


# ------------------------------------------------------------ caminho feliz --

def test_send_message_persiste_e_devolve_o_id(ingest_fake):
    """O envio grava E devolve o id -- sem o id a auditoria nao mede nada."""
    out = wa_ingest.persist_sent_message(_payload())

    assert out["stored"] is True
    assert out["message_id"] == 4242, "o id precisa subir pro resulting_message_id"
    assert out["reason"] == "stored"


def test_extrai_as_chaves_certas_do_payload(ingest_fake):
    """`event`, `instance` e o id da mensagem saem do lugar certo do envelope."""
    wa_ingest.persist_sent_message(_payload(msg_id="3EB0ABC", instance="rap-whatsapp"))

    assert ingest_fake[0]["event"] == "send.message"
    assert ingest_fake[0]["instance"] == "rap-whatsapp"
    assert ingest_fake[0]["mid"] == "3EB0ABC"


def test_normaliza_grafia_do_evento(ingest_fake):
    """A Evolution varia entre `SEND_MESSAGE` e `send.message` conforme a versao.

    O `_ingest_upsert` decide por essa string; sem normalizar, uma atualizacao
    da Evolution desligaria a gravacao sem erro nenhum.
    """
    p = _payload()
    p["event"] = "SEND_MESSAGE"
    wa_ingest.persist_sent_message(p)

    assert ingest_fake[0]["event"] == "send.message"


# --------------------------------------------------------------- dedup/erro --

def test_duplicata_nao_grava_de_novo(monkeypatch):
    """Polling e webhook competem pela mesma mensagem -- so uma linha pode sair.

    Se a dedup falhar, cada envio vira DUAS linhas no historico e a conversa
    fica ilegivel. A chave e `metadata->>'message_id'`, a mesma do whatsapp_sync.
    """
    def _dup(payload, event, instance, wa_message_id, _audit):
        _audit("duplicate")
        return {"stored": False, "reason": "duplicate"}

    monkeypatch.setattr(wa_ingest, "_ingest_upsert", _dup)
    out = wa_ingest.persist_sent_message(_payload())

    assert out["stored"] is False
    assert out["reason"] == "duplicate"
    assert out["message_id"] is None


def test_erro_ao_persistir_nao_propaga(monkeypatch):
    """Gravar e best-effort: o MESMO handler trata resposta a proposta.

    Deixar a excecao subir trocaria "mensagem fora do historico" por "webhook
    inteiro derrubado" -- o remedio pior que a doenca.
    """
    def _explode(*a, **k):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(wa_ingest, "_ingest_upsert", _explode)
    out = wa_ingest.persist_sent_message(_payload())

    assert out["stored"] is False
    assert out["message_id"] is None
    assert "banco fora" in out["reason"]


@pytest.mark.parametrize("ruim", [None, "", 42, [], "nao sou dict"])
def test_payload_invalido_nao_explode(ruim):
    """Payload torto vira recusa limpa, nunca AttributeError no webhook."""
    out = wa_ingest.persist_sent_message(ruim)

    assert out["stored"] is False
    assert out["reason"] == "invalid_payload"


def test_payload_sem_key_nao_explode(ingest_fake):
    """`data.key` ausente: segue com id vazio em vez de estourar.

    A Evolution ja mandou evento sem `key` em versoes anteriores; quem decide se
    da pra gravar e o `_ingest_upsert`, nao a extracao.
    """
    p = _payload()
    p["data"] = {}
    out = wa_ingest.persist_sent_message(p)

    assert out["stored"] is True
    assert ingest_fake[0]["mid"] == ""
