"""
Testes do apply do RACI human-in-loop (fix 22/07).

Contexto do bug: a metade "apply" do human-in-loop estava aparentemente MORTA — a
CoS recebia sempre "item nao encontrado no RACI" ao aplicar proposta shadow. A
investigacao provou que o item_id RESOLVE (todas as 72 propostas em prod casam com
raci_itens). O defeito real: apply_proposal colapsava QUATRO desfechos (env ausente,
item inexistente, no-op, excecao de DB) num unico `return None`, e apply_group_proposal
traduzia esse None SEMPRE como "item nao encontrado" — diagnostico falso que mandava
quem revisa cacar um bug de mapeamento inexistente.

Estes testes provam o novo contrato:
  - strict=True distingue os 4 casos via erros tipados;
  - strict=False (auto-apply do webhook/batch) mantem o contrato historico (None).

Rodar: python -m pytest tests/test_raci_apply_strict.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import psycopg2  # noqa: E402

from services import raci_smart_updates as rsu  # noqa: E402
from services.raci_smart_updates import (  # noqa: E402
    apply_proposal,
    RaciConfigError,
    RaciItemNotFound,
    RaciNoChange,
    RaciApplyError,
)

ITEM_ID = "6e37a6ef-78f7-4f3a-a092-9a77ab698847"


class _FakeCursor:
    def __init__(self, select_row):
        self._select_row = select_row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._select_row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, select_row):
        self.cur = _FakeCursor(select_row)
        self.committed = False
        self.closed = False

    def cursor(self, **kw):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _patch_connect(monkeypatch, select_row):
    """Faz psycopg2.connect devolver uma conexao fake com o SELECT controlado."""
    conn = _FakeConn(select_row)
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: conn)
    return conn


def _prop(**over):
    base = {"item_id": ITEM_ID, "action": "update_status", "new_status": "em_andamento",
            "new_prazo": None, "notes": None, "evidencia": "ev", "confianca": "alta"}
    base.update(over)
    return base


# ───────────────────────── strict=True (human-in-loop) ─────────────────────────

def test_strict_config_error_quando_env_ausente(monkeypatch):
    """Sem CONSELHOOS_DATABASE_URL -> RaciConfigError (era None -> 'item nao encontrado')."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "")
    with pytest.raises(RaciConfigError):
        apply_proposal(_prop(), "emp-1", strict=True)


def test_strict_config_error_ignora_whitespace(monkeypatch):
    """Valor so com espaco/newline (gotcha Vercel) conta como ausente."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "  \n ")
    with pytest.raises(RaciConfigError):
        apply_proposal(_prop(), "emp-1", strict=True)


def test_strict_item_not_found(monkeypatch):
    """SELECT nao acha o item -> RaciItemNotFound (mapeamento realmente quebrado)."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    _patch_connect(monkeypatch, select_row=None)
    with pytest.raises(RaciItemNotFound) as ei:
        apply_proposal(_prop(), "emp-1", strict=True)
    assert ITEM_ID in str(ei.value)


def test_strict_no_change(monkeypatch):
    """Item existe mas ja esta no estado alvo e sem notes/prazo -> RaciNoChange (no-op)."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    _patch_connect(monkeypatch, select_row={"id": ITEM_ID, "acao": "Tabela Camila", "status": "em_andamento"})
    with pytest.raises(RaciNoChange):
        apply_proposal(_prop(new_status="em_andamento", notes=None, new_prazo=None), "emp-1", strict=True)


def test_strict_success_muda_status(monkeypatch):
    """Item resolve + mudanca real -> UPDATE roda, commit, retorna dict (o caso #16)."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    conn = _patch_connect(monkeypatch, select_row={"id": ITEM_ID, "acao": "Tabela Camila", "status": "pendente"})
    # audit log nao deve tocar DB real no teste
    import services.agent_actions as aa
    monkeypatch.setattr(aa, "log_action", lambda *a, **k: None)

    out = apply_proposal(_prop(new_status="em_andamento", notes="repasse aceito"), "emp-1", strict=True)
    assert out is not None
    assert out["old_status"] == "pendente"
    assert out["new_status"] == "em_andamento"
    assert conn.committed is True
    # o UPDATE de fato incluiu o SET status
    update_sqls = [sql for (sql, _p) in conn.cur.executed if sql.strip().upper().startswith("UPDATE")]
    assert update_sqls and "status = %s" in update_sqls[0]


def test_strict_db_exception_vira_apply_error(monkeypatch):
    """Excecao inesperada de DB -> RaciApplyError (nao mais 'item nao encontrado')."""
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")

    def boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(psycopg2, "connect", boom)
    with pytest.raises(RaciApplyError):
        apply_proposal(_prop(), "emp-1", strict=True)


# ───────────────────────── strict=False (auto-apply backcompat) ─────────────────────────

def test_nonstrict_env_ausente_retorna_none(monkeypatch):
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "")
    assert apply_proposal(_prop(), "emp-1") is None


def test_nonstrict_item_not_found_retorna_none(monkeypatch):
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    _patch_connect(monkeypatch, select_row=None)
    assert apply_proposal(_prop(), "emp-1") is None


def test_nonstrict_no_change_retorna_none(monkeypatch):
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    _patch_connect(monkeypatch, select_row={"id": ITEM_ID, "acao": "x", "status": "em_andamento"})
    assert apply_proposal(_prop(new_status="em_andamento", notes=None), "emp-1") is None


def test_nonstrict_db_exception_retorna_none(monkeypatch):
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")

    def boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(psycopg2, "connect", boom)
    assert apply_proposal(_prop(), "emp-1") is None
