"""
Testes do teto de tentativas do arquivamento de anexos WA (mata o livelock).

Contexto medido em produção 28/07/2026: `/api/cron/wa-drive-archive` roda a cada
20 min e somou **547 falhas em 215 runs de 3 dias**, sobre um punhado de itens —
e reportando `status='success'` em todas. A fila era definida só por
`drive_file_id IS NULL`, e nada marcava que uma tentativa tinha sido feita:
anexo cuja mídia expirou na Evolution nunca baixa, mas volta a ocupar slot do
LIMIT a cada run até envelhecer 3 dias, o que dá até 216 re-tentativas por item.
Mesma classe do livelock do wa-catchup (1eaf1be).

O que importa provar aqui não é "o contador incrementa" — é que a marcação
sobrevive aos caminhos por onde o livelock voltaria:
  - a falha é marcada em TODOS os modos de falha (re-download vazio, upload sem
    id, exceção no upload), não só no mais óbvio
  - o commit é por item; uma run que morre no meio não desconta o que já tentou
  - o sucesso NÃO consome tentativa (senão um anexo lento seria descartado)
  - a fila exclui quem esgotou, e o kill-switch restaura o comportamento antigo

Rodar:
  PYTHONPATH=app .venv/bin/python -m pytest tests/test_wa_drive_archive_attempts.py -q
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import wa_drive_archive as m  # noqa: E402


class FakeCursor:
    """Cursor mínimo: entende o COUNT do bloqueado, o SELECT da fila e os dois
    UPDATEs. Guarda o que foi escrito pra o teste inspecionar."""

    def __init__(self, db):
        self.db = db
        self._last = None

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self._last = None
        if s.startswith("SELECT COUNT(*)"):
            cap = params[0]
            self._last = [{"c": sum(1 for r in self.db.rows
                                    if r["drive_file_id"] is None and r["drive_attempts"] >= cap)}]
        elif s.startswith("SELECT a.id"):
            cap, _cap2, limit = params
            fila = [r for r in self.db.rows
                    if r["drive_file_id"] is None and (cap <= 0 or r["drive_attempts"] < cap)]
            self._last = fila[:limit]
        elif "drive_attempts = drive_attempts + 1" in s:
            motivo, rid = params
            row = self.db.by_id(rid)
            row["drive_attempts"] += 1
            row["drive_last_error"] = motivo
            self.db.uncommitted.append(("attempt", rid, row["drive_attempts"]))
        elif "SET drive_file_id" in s:
            fid, link, rid = params
            row = self.db.by_id(rid)
            row["drive_file_id"] = fid
            self.db.uncommitted.append(("archived", rid, fid))
        else:
            raise AssertionError(f"SQL inesperado no teste: {s[:120]}")

    def fetchall(self):
        return self._last or []

    def fetchone(self):
        return (self._last or [None])[0]


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.uncommitted = []
        self.committed = []
        self.rollbacks = 0

    def by_id(self, rid):
        return next(r for r in self.rows if r["id"] == rid)

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed.extend(self.uncommitted)
        self.uncommitted = []

    def rollback(self):
        self.rollbacks += 1
        self.uncommitted = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _row(rid, attempts=0, archived=None):
    from datetime import datetime

    return {
        "id": rid, "message_id": f"MID{rid}", "phone": "5511999999999",
        "kind": "image", "mime_type": "image/jpeg", "original_filename": None,
        "criado_em": datetime(2026, 7, 28, 12, 0), "direcao": "incoming",
        "drive_attempts": attempts, "drive_file_id": archived,
        "drive_last_error": None,
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("EVOLUTION_API_URL", "http://evo")
    monkeypatch.setenv("EVOLUTION_API_KEY", "k")
    monkeypatch.setenv("WA_DRIVE_MAX_ATTEMPTS", "3")

    async def _token(conn, acct):
        return "tok"

    async def _folder(token, name, parent=None):
        return {"id": "folder"}

    async def _list(token, parent=None):
        return []

    monkeypatch.setattr(m, "get_valid_token", _token)
    monkeypatch.setattr(m, "create_folder", _folder)
    monkeypatch.setattr(m, "list_folders", _list)


def _run(db, monkeypatch, *, redownload, upload=None):
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "_redownload", redownload)
    if upload is not None:
        monkeypatch.setattr(m, "upload_file", upload)
    return asyncio.run(m.archive_pending_attachments(limit=25))


async def _redl_vazio(*_a, **_kw):
    return None, None


async def _redl_ok(*_a, **_kw):
    return b"bytes", "image/jpeg"


def test_falha_de_redownload_marca_tentativa(env, monkeypatch):
    db = FakeDB([_row(1)])
    res = _run(db, monkeypatch, redownload=_redl_vazio)

    assert res["failed"] == 1
    assert db.by_id(1)["drive_attempts"] == 1, "sem isso o item volta pra fila igual"
    assert db.committed, "a tentativa tem que ser COMMITADA, nao ficar na transacao"
    assert "midia expirada" in db.by_id(1)["drive_last_error"]


def test_item_sai_da_fila_ao_esgotar_o_teto(env, monkeypatch):
    """O comportamento que fecha o livelock: 3 runs e o item para de aparecer."""
    db = FakeDB([_row(1)])
    for esperado in (1, 2, 3):
        res = _run(db, monkeypatch, redownload=_redl_vazio)
        assert res["processed"] == 1
        assert db.by_id(1)["drive_attempts"] == esperado

    assert res["exhausted"] == 1, "a run que estoura o teto tem que dizer que desistiu"

    # 4a run: a fila nao entrega mais o item, e ele e contado como bloqueado.
    res = _run(db, monkeypatch, redownload=_redl_vazio)
    assert res["processed"] == 0, "o item esgotado continua sendo re-tentado"
    assert res["blocked"] == 1, "o passivo tem que ficar visivel, nao sumir calado"


def test_upload_sem_id_tambem_marca(env, monkeypatch):
    """Modo de falha diferente do re-download — o livelock voltaria por aqui."""
    async def _up(*_a, **_kw):
        return {}

    db = FakeDB([_row(1)])
    res = _run(db, monkeypatch, redownload=_redl_ok, upload=_up)
    assert res["failed"] == 1
    assert db.by_id(1)["drive_attempts"] == 1
    assert "file id" in db.by_id(1)["drive_last_error"]


def test_excecao_no_upload_tambem_marca(env, monkeypatch):
    async def _up(*_a, **_kw):
        raise RuntimeError("Drive 500")

    db = FakeDB([_row(1)])
    res = _run(db, monkeypatch, redownload=_redl_ok, upload=_up)
    assert res["failed"] == 1
    assert db.by_id(1)["drive_attempts"] == 1
    assert "Drive 500" in db.by_id(1)["drive_last_error"]
    assert db.rollbacks >= 1, "a transacao suja tem que ser limpa antes de marcar"


def test_sucesso_nao_consome_tentativa(env, monkeypatch):
    """Anexo que demora mas chega nao pode ser penalizado."""
    async def _up(*_a, **_kw):
        return {"id": "FILEID"}

    db = FakeDB([_row(1, attempts=2)])
    res = _run(db, monkeypatch, redownload=_redl_ok, upload=_up)
    assert res["uploaded"] == 1
    assert db.by_id(1)["drive_attempts"] == 2, "sucesso nao incrementa"
    assert db.by_id(1)["drive_file_id"] == "FILEID"


def test_kill_switch_restaura_comportamento_antigo(env, monkeypatch):
    """WA_DRIVE_MAX_ATTEMPTS<=0 desliga o teto — a fila volta a entregar tudo."""
    monkeypatch.setenv("WA_DRIVE_MAX_ATTEMPTS", "0")
    db = FakeDB([_row(1, attempts=99)])
    res = _run(db, monkeypatch, redownload=_redl_vazio)
    assert res["processed"] == 1, "com o teto desligado o item esgotado volta"
    assert res["blocked"] == 0
    assert res["exhausted"] == 0


def test_max_attempts_le_env_com_fallback():
    assert m.max_attempts() == m._DEFAULT_MAX_ATTEMPTS
    os.environ["WA_DRIVE_MAX_ATTEMPTS"] = " 5 "   # Vercel/Railway colam espaco
    try:
        assert m.max_attempts() == 5
        os.environ["WA_DRIVE_MAX_ATTEMPTS"] = "nao-numero"
        assert m.max_attempts() == m._DEFAULT_MAX_ATTEMPTS, "env invalida nao pode explodir o cron"
    finally:
        del os.environ["WA_DRIVE_MAX_ATTEMPTS"]
