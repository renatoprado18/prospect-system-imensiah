"""Cancelar um evento APAGA — a base não guarda `status='cancelled'`.

O BUG (medido em 12/08/2026). Duas partes do sistema mandavam o oposto na mesma
coluna:

  · `calendar_sync.py` decidiu em 11/08 que cancelado é DELETE, e escreveu o
    porquê no código: "NENHUMA das 32 consultas a `calendar_events` filtra por
    status, então o evento cancelado continuaria aparecendo em toda tela"
  · `actuators.update_calendar_event` documentava o contrário na docstring —
    "para 'não vou', use delete_calendar_event ou status='cancelled'" — e
    gravava a marcação, que é o caminho que NÃO tira de tela nenhuma

Medido no dia: 47 consultas a `calendar_events`, só 5 filtram status. Em prod,
404 eventos, ZERO cancelados — o sync nunca cria a linha; quem criava era a
marcação manual. Foi assim que o #4035, marcado na mão pela sessão CoS, seguiu
visível em todas as telas.

O caminho HTTP era o furo maior: `PUT /api/calendar/events/{id}` passa o corpo
CRU pro service, então consertar só o actuator deixava o endpoint gravando
cancelado calado. Por isso a regra mora no service — é por onde os dois passam.

Rodar: .venv/bin/python -m pytest tests/test_calendar_cancelled_apaga.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services.calendar_events import CalendarEventsService  # noqa: E402


class _Cursor:
    """Cursor que só registra o SQL que passou por ele."""

    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append(" ".join(sql.split()))

    def fetchone(self):
        return {"google_event_id": None, "local_only": True}


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self):
        return _Cursor(self.sink)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def svc(monkeypatch):
    """Service com o banco trocado por um espião de SQL."""
    s = CalendarEventsService()
    sql = []
    monkeypatch.setattr("services.calendar_events.get_db", lambda: _Conn(sql))
    monkeypatch.setattr(s, "get_event", lambda eid: {"id": eid, "status": "confirmed"})
    s._sql = sql
    return s


@pytest.mark.asyncio
async def test_cancelled_apaga_em_vez_de_gravar(svc):
    """O caso do #4035: marcar 'cancelled' tem que virar DELETE."""
    apagados = []

    async def _fake_delete(event_id, delete_from_google=True, scope="single"):
        apagados.append(event_id)
        return True

    svc.delete_event = _fake_delete

    out = await svc.update_event(4035, {"status": "cancelled"})

    assert apagados == [4035], "cancelled tem que chamar delete_event"
    assert out["deleted"] is True
    # O ponto do bug: nenhum UPDATE pode ter gravado a marcação.
    assert not any("UPDATE calendar_events" in s for s in svc._sql), \
        f"gravou em vez de apagar: {svc._sql}"


@pytest.mark.asyncio
async def test_cancelled_aceita_maiuscula_e_espaco(svc):
    """'Cancelled ' vindo do LLM é o mesmo caso — normalizar antes de decidir."""
    apagados = []

    async def _fake_delete(event_id, delete_from_google=True, scope="single"):
        apagados.append(event_id)
        return True

    svc.delete_event = _fake_delete
    await svc.update_event(99, {"status": "  CANCELLED "})
    assert apagados == [99]


@pytest.mark.asyncio
async def test_tentative_ainda_grava(svc):
    """CONTROLE NEGATIVO. Uma trava que apaga qualquer status não é uma trava:
    'tentative' e 'confirmed' seguem sendo gravados normalmente."""

    async def _nunca(*a, **k):
        raise AssertionError("delete_event não podia ser chamado para 'tentative'")

    svc.delete_event = _nunca
    await svc.update_event(7, {"status": "tentative"})
    assert any("UPDATE calendar_events" in s for s in svc._sql), \
        "status normal tem que continuar gravando"


@pytest.mark.asyncio
async def test_update_sem_status_intacto(svc):
    """CONTROLE NEGATIVO. Renomear não pode ter virado apagar."""

    async def _nunca(*a, **k):
        raise AssertionError("delete_event não podia ser chamado num rename")

    svc.delete_event = _nunca
    await svc.update_event(7, {"summary": "novo nome"})
    assert any("UPDATE calendar_events" in s for s in svc._sql)
