"""Testes dos guards de dismiss/reopen de raci_group_proposals (fix 22/07).

Fecha o debito: antes so havia apply — dismiss/reopen eram psql manual. Exercita
as transicoes de status e os guards contra transicao perigosa (nunca mexer em
'applied'). Usa um fake get_db (cursor em memoria) — sem DB real.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.pop("VERCEL_ENV", None)

import importlib

import services.raci_group_shadow as gs


class _FakeCursor:
    """Cursor minimo sobre um dict {id: {'status': ...}}. Suporta o SELECT status
    e os UPDATEs de dismiss/reopen (parse simples pelo prefixo do SQL)."""

    def __init__(self, store):
        self.store = store
        self._row = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        pid = params[-1] if params else None
        if s.startswith("SELECT status FROM raci_group_proposals"):
            rec = self.store.get(pid)
            self._row = {"status": rec["status"]} if rec else None
        elif "status='dismissed'" in s:
            self.store[pid]["status"] = "dismissed"
            self.store[pid]["apply_result"] = params[0]
        elif "status='pending_review'" in s:
            self.store[pid]["status"] = "pending_review"
            self.store[pid]["apply_result"] = None
        else:
            raise AssertionError(f"SQL nao previsto no fake: {s}")

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_db(monkeypatch, store):
    monkeypatch.setattr(gs, "get_db", lambda: _FakeConn(store))


# ── dismiss ──────────────────────────────────────────────────────────────────

def test_dismiss_pending_ok(monkeypatch):
    store = {5: {"status": "pending_review"}}
    _patch_db(monkeypatch, store)
    r = gs.dismiss_group_proposal(5, reason="velha demais")
    assert r["ok"] and r["status"] == "dismissed" and r["prev"] == "pending_review"
    assert store[5]["status"] == "dismissed"
    assert "velha demais" in store[5]["apply_result"]


def test_dismiss_applied_bloqueado(monkeypatch):
    store = {7: {"status": "applied"}}
    _patch_db(monkeypatch, store)
    r = gs.dismiss_group_proposal(7)
    assert "error" in r and "aplicada" in r["error"]
    assert store[7]["status"] == "applied"  # intocado


def test_dismiss_ja_dismissed_noop(monkeypatch):
    store = {8: {"status": "dismissed"}}
    _patch_db(monkeypatch, store)
    r = gs.dismiss_group_proposal(8)
    assert r["ok"] and r.get("noop") is True


def test_dismiss_inexistente(monkeypatch):
    _patch_db(monkeypatch, {})
    r = gs.dismiss_group_proposal(999)
    assert "error" in r and "nao encontrada" in r["error"]


def test_dismiss_apply_error_ok(monkeypatch):
    store = {9: {"status": "apply_error"}}
    _patch_db(monkeypatch, store)
    r = gs.dismiss_group_proposal(9)
    assert r["ok"] and store[9]["status"] == "dismissed"


# ── reopen ───────────────────────────────────────────────────────────────────

def test_reopen_dismissed_ok(monkeypatch):
    store = {5: {"status": "dismissed", "apply_result": "dismissed manual"}}
    _patch_db(monkeypatch, store)
    r = gs.reopen_group_proposal(5)
    assert r["ok"] and r["status"] == "pending_review" and r["prev"] == "dismissed"
    assert store[5]["status"] == "pending_review"
    assert store[5]["apply_result"] is None


def test_reopen_apply_error_ok(monkeypatch):
    store = {6: {"status": "apply_error"}}
    _patch_db(monkeypatch, store)
    r = gs.reopen_group_proposal(6)
    assert r["ok"] and store[6]["status"] == "pending_review"


def test_reopen_applied_bloqueado(monkeypatch):
    store = {7: {"status": "applied"}}
    _patch_db(monkeypatch, store)
    r = gs.reopen_group_proposal(7)
    assert "error" in r and "re-aplicar" in r["error"]
    assert store[7]["status"] == "applied"  # intocado


def test_reopen_ja_pending_noop(monkeypatch):
    store = {8: {"status": "pending_review"}}
    _patch_db(monkeypatch, store)
    r = gs.reopen_group_proposal(8)
    assert r["ok"] and r.get("noop") is True


def test_reopen_inexistente(monkeypatch):
    _patch_db(monkeypatch, {})
    r = gs.reopen_group_proposal(999)
    assert "error" in r and "nao encontrada" in r["error"]
