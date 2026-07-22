"""
Testes do INSERT em tempo real de mensagens de grupo no webhook Evolution (#999651).

Prova que persist_group_message_realtime:
  (a) grava msg de grupo RASTREADO (sync_enabled) e eh idempotente por message_id
      — 2a entrega do mesmo id NAO duplica (ON CONFLICT DO NOTHING);
  (b) NAO grava msg de grupo nao-rastreado (sync_enabled=FALSE / ausente);
  (c) se o INSERT falha, a funcao NAO levanta excecao (webhook/RACI seguem).

Rodar: .venv/bin/python -m pytest tests/test_group_message_realtime.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
import database  # noqa: E402
from integrations import evolution_api  # noqa: E402


# ---------------------------------------------------------------- fakes DB ----

class _FakeCursor:
    """Emula o subset de psycopg2/RealDictCursor que a funcao usa.

    - store: dict compartilhado {message_id: row} entre conexoes (simula ON
      CONFLICT (message_id) DO NOTHING persistindo entre entregas do webhook).
    - tracked: set de group_jids com sync_enabled=TRUE.
    - fail_insert: se True, o INSERT levanta -> testa erro-gracioso.
    """

    def __init__(self, store, tracked, fail_insert=False):
        self._store = store
        self._tracked = tracked
        self._fail_insert = fail_insert
        self._last = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if "FROM social_groups_cache" in s:
            jid = params[0]
            self._last = ("row",) if jid in self._tracked else None
        elif "FROM contacts" in s:  # _find_contact_by_phone
            self._last = None
        elif s.startswith("INSERT INTO group_messages"):
            if self._fail_insert:
                raise RuntimeError("boom: simulated DB failure on INSERT")
            message_id = params[1]
            if message_id in self._store:
                self.rowcount = 0            # ON CONFLICT DO NOTHING
            else:
                self._store[message_id] = params
                self.rowcount = 1
            self._last = None
        else:
            self._last = None

    def fetchone(self):
        return self._last


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_get_db(monkeypatch, store, tracked, fail_insert=False):
    def _factory():
        return _FakeConn(_FakeCursor(store, tracked, fail_insert))
    # A funcao faz `from database import get_db` em runtime -> patch no modulo.
    monkeypatch.setattr(database, "get_db", _factory)


def _payload(message_id, group_jid, text="Bom dia, avancamos no filito?"):
    return {
        "message": {"conversation": text},
        "key": {"id": message_id, "remoteJid": group_jid,
                "participant": "5511999998888@s.whatsapp.net", "fromMe": False},
        "messageTimestamp": 1753000000,
        "pushName": "Fulano",
    }


TRACKED_JID = "120363000000000000@g.us"
UNTRACKED_JID = "120363111111111111@g.us"


# --------------------------------------------------------------- (a) idempotente

def test_tracked_group_inserts_then_idempotent(monkeypatch):
    store, tracked = {}, {TRACKED_JID}
    _patch_get_db(monkeypatch, store, tracked)
    d = _payload("MSG_A", TRACKED_JID)

    first = evolution_api.persist_group_message_realtime(
        d, d["key"], TRACKED_JID, "MSG_A", False)
    second = evolution_api.persist_group_message_realtime(
        d, d["key"], TRACKED_JID, "MSG_A", False)

    assert first is True, "1a entrega deve inserir"
    assert second is False, "2a entrega do mesmo message_id NAO duplica"
    assert list(store.keys()) == ["MSG_A"], "so 1 linha gravada"
    # confere que grupo e conteudo foram para as colunas certas
    row = store["MSG_A"]
    assert row[0] == TRACKED_JID and row[1] == "MSG_A"
    assert row[5] == "Bom dia, avancamos no filito?"


# --------------------------------------------------------------- (b) nao-rastreado

def test_untracked_group_not_inserted(monkeypatch):
    store, tracked = {}, {TRACKED_JID}   # UNTRACKED_JID nao esta no set
    _patch_get_db(monkeypatch, store, tracked)
    d = _payload("MSG_B", UNTRACKED_JID)

    res = evolution_api.persist_group_message_realtime(
        d, d["key"], UNTRACKED_JID, "MSG_B", False)

    assert res is False
    assert store == {}, "grupo nao-rastreado nao grava nada"


# --------------------------------------------------------------- (c) erro-gracioso

def test_insert_failure_does_not_raise(monkeypatch):
    store, tracked = {}, {TRACKED_JID}
    _patch_get_db(monkeypatch, store, tracked, fail_insert=True)
    d = _payload("MSG_C", TRACKED_JID)

    # NAO pode levantar — o webhook (RACI + ACK) tem que seguir.
    res = evolution_api.persist_group_message_realtime(
        d, d["key"], TRACKED_JID, "MSG_C", False)

    assert res is False
    assert store == {}


# --------------------------------------------------------------- extras de robustez

def test_missing_message_id_returns_false(monkeypatch):
    store, tracked = {}, {TRACKED_JID}
    _patch_get_db(monkeypatch, store, tracked)
    d = _payload("", TRACKED_JID)
    assert evolution_api.persist_group_message_realtime(
        d, d["key"], TRACKED_JID, "", False) is False
    assert store == {}


def test_empty_content_skipped(monkeypatch):
    store, tracked = {}, {TRACKED_JID}
    _patch_get_db(monkeypatch, store, tracked)
    d = {"message": {"reactionMessage": {"text": "x"}},
         "key": {"id": "MSG_D", "remoteJid": TRACKED_JID, "fromMe": False},
         "messageTimestamp": 1753000000}
    assert evolution_api.persist_group_message_realtime(
        d, d["key"], TRACKED_JID, "MSG_D", False) is False
    assert store == {}


def test_media_document_logged_with_placeholder(monkeypatch):
    store, tracked = {}, {TRACKED_JID}
    _patch_get_db(monkeypatch, store, tracked)
    d = {"message": {"documentMessage": {"fileName": "contrato.pdf"}},
         "key": {"id": "MSG_E", "remoteJid": TRACKED_JID,
                 "participant": "5511999998888@s.whatsapp.net", "fromMe": False},
         "messageTimestamp": 1753000000}
    res = evolution_api.persist_group_message_realtime(
        d, d["key"], TRACKED_JID, "MSG_E", False)
    assert res is True
    row = store["MSG_E"]
    assert row[6] == "document"             # message_type
    assert row[5] == "[Documento: contrato.pdf]"
