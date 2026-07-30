"""
Ciclo de vida do `on_hold` — 30/07/2026. Duas metades do mesmo defeito.

METADE 1 — O SYNC DESFAZIA O PARQUEIO. As 4 tasks que a sessao Dev da madrugada
moveu pra `on_hold` (#999636 mandato Filito, #999637 evaluation do geologo,
#999652 retorno Baeta, #999655 retorno Alfredo Villela) estavam de volta em
`pending` as 05:00:23 — o daily-sync rodou 05:00:02. O Google Tasks so tem
needsAction|completed, e o pull traduzia needsAction como 'pending', rebaixando
qualquer estado que existe so no INTEL. A guarda de conflito
(atualizado_em > last_synced_at) so ADIA: quando um sync toca last_synced_at sem
que atualizado_em mude, o Google volta a vencer na run seguinte.

METADE 2 — `on_hold` ERA UM BECO. A convencao de 29/07
(feedback_aguardar_terceiro_on_hold) exige que a task parqueada RESSURJA por dois
gatilhos — resposta do terceiro e janela de 7 dias esgotada. Nenhum existia: a
string "on_hold" aparecia em 5 lugares em app/ inteiro e nenhum reabria. Task
parqueada nao voltava nunca.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_on_hold_lifecycle.py -q
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import task_reconciler, tasks_sync  # noqa: E402


# ---------------------------------------------------------------------------
# Fake DB: roteia por trecho do SQL e grava o que foi escrito
# ---------------------------------------------------------------------------
class _FakeCur:
    def __init__(self, state):
        self.state = state
        self._rows = []
        self._one = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.state["sql"].append((s, params))
        low = s.lower()

        if low.startswith("update"):
            self.state["updates"].append((s, params))
            self._rows, self._one = [], None
            return

        if "from tasks where google_task_id" in low:
            self._one = self.state.get("local_task")
        elif "from tasks where status = 'on_hold'" in low:
            self._rows = self.state.get("on_hold_rows", [])
        elif "from messages" in low:
            self._rows = self.state.get("messages", [])
        elif "analyzer_settings" in low:
            self._one = self.state.get("setting")
        else:
            self._rows, self._one = [], None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCur(self.state)

    def commit(self):
        self.state["commits"] += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, module, state):
    state.setdefault("sql", [])
    state.setdefault("updates", [])
    state.setdefault("commits", 0)
    monkeypatch.setattr(module, "get_db", lambda: _FakeConn(state))
    return state


# ===========================================================================
# METADE 1 — o pull do Google nao rebaixa estado local
# ===========================================================================
def _pull(monkeypatch, local_status, google_status):
    """Roda _sync_google_task_to_local e devolve o status gravado no UPDATE."""
    state = _install(monkeypatch, tasks_sync, {
        "local_task": {
            "id": 999636, "status": local_status,
            "atualizado_em": datetime(2026, 7, 30, 4, 15),
            "last_synced_at": datetime(2026, 7, 30, 5, 0),  # guarda INATIVA
        },
    })
    svc = tasks_sync.TasksSyncService.__new__(tasks_sync.TasksSyncService)
    asyncio.run(svc._sync_google_task_to_local(
        {"id": "g1", "title": "t", "status": google_status, "etag": "e"}, "@default"
    ))
    upd = [u for u in state["updates"] if "set titulo" in u[0].lower()]
    assert upd, "esperava o UPDATE de sincronizacao"
    # ordem dos params: titulo, descricao, due, status, status(CASE), etag, gid
    return upd[0][1][3]


def test_pull_nao_rebaixa_on_hold(monkeypatch):
    """O caso real de 30/07 05:00:23: parqueada volta needsAction do Google
    (que nao conhece o conceito) e era rebaixada pra pending."""
    assert _pull(monkeypatch, "on_hold", "needsAction") == "on_hold"


@pytest.mark.parametrize("status", ["in_progress", "cancelled", "delegated"])
def test_pull_nao_rebaixa_outros_estados_locais(monkeypatch, status):
    """Mesma classe. As 72 `cancelled` de prod so escapavam por terem sido
    apagadas no Google — acidente, nao desenho."""
    assert _pull(monkeypatch, status, "needsAction") == status


def test_pull_mantem_pending_como_pending(monkeypatch):
    assert _pull(monkeypatch, "pending", "needsAction") == "pending"


def test_google_ainda_pode_concluir_task_parqueada(monkeypatch):
    """Marcar feito no Google continua concluindo aqui — a guarda e contra
    REBAIXAMENTO, nao contra o Google ter voz."""
    assert _pull(monkeypatch, "on_hold", "completed") == "completed"


def test_google_ainda_pode_reabrir_concluida(monkeypatch):
    """`completed` fica FORA do conjunto protegido de proposito: desmarcar no
    Google tem que reabrir aqui. Regressao de nao ter congelado demais."""
    assert _pull(monkeypatch, "completed", "needsAction") == "pending"


# ===========================================================================
# METADE 2 — o sweep de saida do on_hold
# ===========================================================================
def _row(**kw):
    base = {
        "id": 999655, "titulo": "[Renda fixa] Aguardar retorno Alfredo Villela",
        "contact_id": None, "project_id": None,
        "data_vencimento": datetime(2026, 7, 20),
        "atualizado_em": datetime(2026, 7, 30),
        "on_hold_since": datetime(2026, 7, 20),
        "on_hold_reason": task_reconciler.REASON_ESPERA,
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    """log_action grava em agent_actions; nos testes so contamos as chamadas."""
    calls = []
    import services.agent_actions as aa
    monkeypatch.setattr(aa, "log_action", lambda **kw: calls.append(kw))
    return calls


def _sweep(monkeypatch, rows, messages=None, now=None, dry_run=False):
    state = _install(monkeypatch, task_reconciler, {
        "on_hold_rows": rows, "messages": messages or [], "setting": None,
    })
    if now:
        monkeypatch.setattr(task_reconciler, "_now_naive_utc", lambda: now)
    return task_reconciler.sweep_on_hold(dry_run=dry_run), state


def test_parqueio_sem_carimbo_ganha_carencia(monkeypatch):
    """Parqueio por SQL manual/UI/snooze da Tonia nao declara intencao. O sweep
    carimba e NAO avalia na mesma run — a janela precisa existir antes de poder
    estar esgotada, senao parquear nao teria efeito nenhum."""
    out, state = _sweep(monkeypatch, [_row(on_hold_since=None, on_hold_reason=None)])
    assert out["classified"] == 1
    assert out["reopened"] == 0
    assert any("on_hold_since = COALESCE" in u[0] for u in state["updates"])


def test_parqueio_indefinido_nao_sofre_aging(monkeypatch):
    """As 7 tasks que o board registra como 'absorvidas do CoS, nao perdidas, so
    parqueadas' (#398, #468, #502, #999513, #999516, #999518, #999584). Aplicar
    7 dias a elas devolveria ao portao o que foi silenciado por decisao."""
    out, _ = _sweep(
        monkeypatch,
        [_row(id=398, on_hold_reason=task_reconciler.REASON_INDEFINIDO,
              data_vencimento=datetime(2026, 5, 1), on_hold_since=datetime(2026, 5, 1))],
        now=datetime(2026, 7, 30),
    )
    assert out["reopened"] == 0
    assert out["still_held"] == 1


def test_dentro_da_janela_fica_parqueada(monkeypatch):
    """3 dias de espera: e a janela normal, nao vale nagging."""
    out, _ = _sweep(monkeypatch, [_row(data_vencimento=datetime(2026, 7, 27),
                                       on_hold_since=datetime(2026, 7, 27))],
                    now=datetime(2026, 7, 30))
    assert out["reopened"] == 0


def test_janela_esgotada_reabre(monkeypatch):
    """>7 dias sem resposta: o terceiro sumiu, volta pro radar pro Renato
    decidir se cobra."""
    out, state = _sweep(monkeypatch, [_row(data_vencimento=datetime(2026, 7, 18),
                                           on_hold_since=datetime(2026, 7, 18))],
                        now=datetime(2026, 7, 30))
    assert out["reopened"] == 1
    assert out["items"][0]["trigger"] == "aging"
    assert any("status = 'pending'" in u[0] for u in state["updates"])


def test_resposta_substantiva_reabre(monkeypatch):
    """Gatilho (a): a bola voltou pro Renato."""
    out, _ = _sweep(
        monkeypatch,
        [_row(contact_id=4376)],
        messages=[{"conteudo": "Renato, segue o contrato assinado pra vocês",
                   "ts": datetime(2026, 7, 25)}],
        now=datetime(2026, 7, 22),  # AINDA dentro da janela: quem reabre e a resposta
    )
    assert out["reopened"] == 1
    assert out["items"][0]["trigger"] == "reply"


def test_cortesia_pura_nao_reabre(monkeypatch):
    """O falso-positivo que custou um dia no check G da /cos: uma cortesia as
    16:11 ressuscitou item respondido as 16:10. Reusa o filtro ja calibrado do
    raci_smart_updates."""
    out, _ = _sweep(
        monkeypatch,
        [_row(contact_id=4376)],
        messages=[{"conteudo": "Obrigado Renato, um abraço e bom domingo!",
                   "ts": datetime(2026, 7, 25)}],
        now=datetime(2026, 7, 22),
    )
    assert out["reopened"] == 0


def test_reabertura_limpa_o_carimbo(monkeypatch):
    """Anti-loop (feedback_livelock_reprocessamento): sem zerar on_hold_since, a
    mensagem que causou a reabertura seguiria sendo 'posterior ao parqueio' pra
    sempre e a task reabriria toda run mesmo depois de re-parqueada."""
    _, state = _sweep(monkeypatch, [_row(data_vencimento=datetime(2026, 7, 1),
                                         on_hold_since=datetime(2026, 7, 1))],
                      now=datetime(2026, 7, 30))
    upd = [u[0] for u in state["updates"] if "status = 'pending'" in u[0]]
    assert upd, "esperava o UPDATE de reabertura"
    assert "on_hold_since = NULL" in upd[0]
    assert "on_hold_reason = NULL" in upd[0]


def test_reparqueio_concede_janela_nova(monkeypatch):
    """A janela conta de GREATEST(vencimento, parqueio). Task vencida em 01/06 e
    re-parqueada em 29/07 NAO reabre no dia seguinte — senao o gesto de parquear
    seria inutil justamente no caso que mais precisa dele."""
    task = _row(data_vencimento=datetime(2026, 6, 1), on_hold_since=datetime(2026, 7, 29))
    assert task_reconciler._wait_deadline(task) == datetime(2026, 8, 5)
    out, _ = _sweep(monkeypatch, [task], now=datetime(2026, 7, 30))
    assert out["reopened"] == 0


def test_dry_run_nao_escreve(monkeypatch):
    out, state = _sweep(monkeypatch, [_row(data_vencimento=datetime(2026, 7, 1),
                                           on_hold_since=datetime(2026, 7, 1))],
                        now=datetime(2026, 7, 30), dry_run=True)
    assert out["reopened"] == 1
    assert state["updates"] == []


def test_kill_switch_desliga_a_fase_2(monkeypatch):
    state = _install(monkeypatch, task_reconciler, {
        "on_hold_rows": [_row()], "setting": {"setting_value": "off"},
    })
    out = task_reconciler.sweep_on_hold()
    assert out == {"disabled": True}
    assert state["updates"] == []


def test_sem_vencimento_usa_o_parqueio_como_base(monkeypatch):
    """Task sem data_vencimento nao pode explodir no GREATEST."""
    task = _row(data_vencimento=None, on_hold_since=datetime(2026, 7, 1))
    assert task_reconciler._wait_deadline(task) == datetime(2026, 7, 8)
