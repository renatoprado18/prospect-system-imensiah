"""
Testes da CAMADA ESPERTA do inbox (handoff 25/07).

Cobre a lógica determinística (sem tocar Gmail real, Neon nem a rede LLM):
  - kill-switch INBOX_SMART_LAYER (default OFF = legado byte-a-byte)
  - PARTE A: forward Andressa — idempotência (label), dry-run, envio
  - PARTE B: julgamento resolvido-vs-ambíguo (_smart_triage_renato) com barra 0.85
  - PARTE C: resumo consolidado (_send_smart_summary) + comando undo (run_smart_undo)
  - apply_triage_to_inbox com smart=True em DRY-RUN (needs-you + resolved) e
    smart=False garantindo dict de retorno idêntico ao legado (sem chaves smart_*)

Rodar: .venv/bin/python -m pytest tests/test_inbox_smart_layer.py -v
"""
import os
import sys
import asyncio

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import email_triage as et  # noqa: E402

ACC = "renato@almeida-prado.com"


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------
class TestKillSwitch:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("INBOX_SMART_LAYER", raising=False)
        assert et.is_smart_layer_enabled() is False

    def test_on_variants(self, monkeypatch):
        for v in ("1", "true", "on", "YES"):
            monkeypatch.setenv("INBOX_SMART_LAYER", v)
            assert et.is_smart_layer_enabled() is True

    def test_whitespace(self, monkeypatch):
        monkeypatch.setenv("INBOX_SMART_LAYER", " 1\n")
        assert et.is_smart_layer_enabled() is True


# ---------------------------------------------------------------------------
# PARTE A — forward Andressa
# ---------------------------------------------------------------------------
class _FwdGmail:
    def __init__(self):
        self.sent = []

    async def send_message(self, access_token, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"id": "sent1"}


_HEADERS = {"subject": "Extrato de julho", "from": "Contab <c@x.com>", "date": "Sat, 25 Jul"}


class TestForwardAndressa:
    def test_dry_run_reports_only(self):
        g = _FwdGmail()
        r = asyncio.run(et._smart_forward_to_andressa(
            g, "tok", "m1", _HEADERS, {"text": "corpo"}, ACC, ["INBOX"], {}, dry_run=True))
        assert r["would"] is True and r["status"] == "would_forward"
        assert g.sent == []  # dry-run nunca envia

    def test_sends_and_labels(self, monkeypatch):
        g = _FwdGmail()

        async def _noop_label(*a, **k):
            return {"applied": True}
        monkeypatch.setattr(et, "_apply_generic_label", _noop_label)

        r = asyncio.run(et._smart_forward_to_andressa(
            g, "tok", "m1", _HEADERS, {"text": "corpo do email"}, ACC, ["INBOX"], {}, dry_run=False))
        assert r["forwarded"] is True
        assert len(g.sent) == 1
        assert g.sent[0]["to"] == et.ANDRESSA_FORWARD_TO
        assert g.sent[0]["subject"] == "Fwd: Extrato de julho"
        assert "Delegado por Renato via triagem automática" in g.sent[0]["body"]
        assert "corpo do email" in g.sent[0]["body"]

    def test_idempotent_when_already_labeled(self):
        g = _FwdGmail()
        # Label do forward já presente em label_ids -> NÃO reenvia (cron 2h seguro).
        name_to_id = {et.ANDRESSA_FORWARD_LABEL: "LFWD"}
        r = asyncio.run(et._smart_forward_to_andressa(
            g, "tok", "m1", _HEADERS, {"text": "b"}, ACC, ["INBOX", "LFWD"], name_to_id, dry_run=False))
        assert r["forwarded"] is False and r["status"] == "already_forwarded"
        assert g.sent == []

    def test_send_error_reported(self):
        class _ErrGmail:
            async def send_message(self, **k):
                return {"error": "token_expired"}
        r = asyncio.run(et._smart_forward_to_andressa(
            _ErrGmail(), "tok", "m1", _HEADERS, {"text": "b"}, ACC, ["INBOX"], {}, dry_run=False))
        assert r["forwarded"] is False and r["status"].startswith("send_error")


# ---------------------------------------------------------------------------
# PARTE B — julgamento resolvido-vs-ambíguo (barra 0.85)
# ---------------------------------------------------------------------------
def _tasks(*ids):
    return [{
        "id": i, "titulo": f"tarefa {i}", "descricao": "", "project_id": 5,
        "contact_id": 9, "data_vencimento": None, "data_criacao": None,
    } for i in ids]


def _triage(monkeypatch, tasks, verdict):
    monkeypatch.setattr(et, "_smart_candidate_tasks", lambda p, c: tasks)

    async def _judge(*a, **k):
        return verdict
    monkeypatch.setattr(et, "_smart_judge_resolution", _judge)
    return asyncio.run(et._smart_triage_renato(
        gmail_id="m", headers={"subject": "s", "from": "f@x.com"},
        from_email="f@x.com", contact_id=9, body_text="b",
        project_index=[], api_key="k"))


class TestTriageRenato:
    def test_no_candidate_tasks_escalates(self, monkeypatch):
        monkeypatch.setattr(et, "_smart_candidate_tasks", lambda p, c: [])
        r = asyncio.run(et._smart_triage_renato(
            gmail_id="m", headers={"subject": "s", "from": "f"},
            from_email="f", contact_id=9, body_text="b", project_index=[], api_key="k"))
        assert r["resolved"] is False and "sem task" in r["record"]["reason"]

    def test_resolved_close_high_conf(self, monkeypatch):
        r = _triage(monkeypatch, _tasks(101), {
            "resolved": True, "task_id": 101, "action": "close",
            "new_due_date": None, "confidence": 0.9, "reason": "loja confirmou entrega"})
        assert r["resolved"] is True
        rec = r["record"]
        assert rec["task_id"] == 101 and rec["action"] == "close" and rec["action_label"] == "fechei"

    def test_resolved_reschedule_with_date(self, monkeypatch):
        r = _triage(monkeypatch, _tasks(101), {
            "resolved": True, "task_id": 101, "action": "reschedule",
            "new_due_date": "2026-08-04", "confidence": 0.9, "reason": "remarcada p/ 3a"})
        assert r["resolved"] is True
        assert r["record"]["action_label"] == "remarquei" and r["record"]["new_due_date"] == "2026-08-04"

    def test_low_confidence_escalates(self, monkeypatch):
        r = _triage(monkeypatch, _tasks(101), {
            "resolved": True, "task_id": 101, "action": "close",
            "new_due_date": None, "confidence": 0.5, "reason": "talvez"})
        assert r["resolved"] is False  # barra 0.85 não afrouxa

    def test_reschedule_without_date_escalates(self, monkeypatch):
        r = _triage(monkeypatch, _tasks(101), {
            "resolved": True, "task_id": 101, "action": "reschedule",
            "new_due_date": None, "confidence": 0.95, "reason": "remarcou mas sem data"})
        assert r["resolved"] is False

    def test_task_id_out_of_set_escalates(self, monkeypatch):
        r = _triage(monkeypatch, _tasks(101), {
            "resolved": True, "task_id": 999, "action": "close",
            "new_due_date": None, "confidence": 0.95, "reason": "inventou id"})
        assert r["resolved"] is False  # nunca age em task fora da lista candidata

    def test_not_resolved_escalates(self, monkeypatch):
        r = _triage(monkeypatch, _tasks(101), {
            "resolved": False, "task_id": None, "action": "none",
            "new_due_date": None, "confidence": 0.9, "reason": "é uma cobrança"})
        assert r["resolved"] is False and "cobrança" in r["record"]["reason"]


# ---------------------------------------------------------------------------
# PARTE C — resumo consolidado + undo
# ---------------------------------------------------------------------------
class TestSummary:
    def test_dry_run_preview(self):
        stats = {}
        asyncio.run(et._send_smart_summary(
            stats, forwarded=2,
            resolved_items=[{"task_id": 101, "action_label": "fechei", "reason": "loja confirmou"}],
            needs_you=[{"subject": "Proposta X", "from": "a@b.com", "reason": "decidir"}],
            dry_run=True))
        p = stats["smart_summary_preview"]
        assert "Encaminharia p/ Andressa: 2" in p
        assert "fechei #101" in p
        assert 'desfaz 101' in p
        assert "Precisa de você (1)" in p
        assert "smart_summary_sent" not in stats  # dry-run não envia


class _FakeUndoConn:
    def __init__(self, row):
        self.row = row
        self._last = ""
        self.hint_executed = None

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()).lower()
        if not self._last.startswith("select"):
            self.hint_executed = sql

    def fetchone(self):
        return self.row if self._last.startswith("select") else None

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestUndo:
    def test_undo_executes_hint_and_marks(self, monkeypatch):
        row = {"id": 7, "undo_hint": "UPDATE tasks SET status='pending' WHERE id=101",
               "action_type": "task_resolved", "title": "tarefa 101"}
        monkeypatch.setattr(et, "get_db", lambda: _FakeUndoConn(row))
        import services.agent_actions as aa
        monkeypatch.setattr(aa, "mark_undone", lambda i: True)
        r = et.run_smart_undo(101)
        assert r["undone"] is True and r["action_id"] == 7

    def test_undo_nothing_to_do(self, monkeypatch):
        monkeypatch.setattr(et, "get_db", lambda: _FakeUndoConn(None))
        r = et.run_smart_undo(101)
        assert r["undone"] is False

    def test_undo_endereca_por_task_id_nao_por_posicao_no_resumo(self, monkeypatch):
        # Invariante que sustenta a Tarefa 1: o N do "desfaz N" é o ID DA TASK
        # gravado em agent_actions.scope_ref — nunca um índice do resumo. Por
        # isso silenciar o resumo (encaminhamento) não deixa nada inalcançável:
        # o que é reversível é sempre task, e task sempre tem ID próprio.
        row = {"id": 7, "undo_hint": "UPDATE tasks SET status='pending' WHERE id=777",
               "action_type": "task_rescheduled", "title": "tarefa 777"}
        fake = _FakeUndoConn(row)
        monkeypatch.setattr(et, "get_db", lambda: fake)
        import services.agent_actions as aa
        monkeypatch.setattr(aa, "mark_undone", lambda i: True)
        r = et.run_smart_undo(777)
        assert r["undone"] is True
        assert fake.hint_executed == row["undo_hint"]

    def test_undo_rejects_non_update(self, monkeypatch):
        row = {"id": 7, "undo_hint": "DROP TABLE tasks", "action_type": "task_resolved", "title": "x"}
        monkeypatch.setattr(et, "get_db", lambda: _FakeUndoConn(row))
        r = et.run_smart_undo(101)
        assert r["undone"] is False and r["reason"] == "undo inválido"


# ---------------------------------------------------------------------------
# apply_triage_to_inbox — DRY-RUN com smart (Gmail/DB falsos, LLM monkeypatched)
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self):
        self._rows = []
        self._one = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from google_accounts" in s and "conectado" in s:
            self._rows = [{"id": 1, "email": ACC, "tipo": "professional",
                           "access_token": "tok", "refresh_token": "ref"}]
            self._one = None
        elif "count(*) as n from agent_actions" in s:
            self._one = {"n": 0}
            self._rows = []
        else:
            self._rows = []
            self._one = None

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_LABELS = [{"name": "!!Renato", "id": "L_REN"}, {"name": "!Andressa", "id": "L_AND"}]


class _FakeGmail:
    def __init__(self):
        self.msgs = {}

    async def list_labels(self, token):
        return _LABELS

    async def get_message(self, token, gid):
        return self.msgs[gid]

    def parse_message_headers(self, msg):
        return dict(msg["_headers"])

    def parse_message_body(self, msg):
        return {"text": msg.get("_body", "")}

    def extract_email_address(self, hdr):
        import re
        m = re.search(r"<([^>]+)>", hdr)
        return (m.group(1) if m else hdr).strip().lower()

    async def get_thread(self, token, tid, format="metadata"):
        return {"messages": [{"labelIds": ["INBOX"]}]}


def _msg(gid, frm, subject, body=""):
    return {
        "id": gid, "threadId": "t_" + gid, "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": "From", "value": frm},
                                {"name": "Subject", "value": subject}]},
        "_headers": {"from": frm, "subject": subject}, "_body": body,
    }


def _run_smart(monkeypatch, smart, triage_stub, msgs=None):
    import integrations.gmail as gmail_mod
    fake = _FakeGmail()
    # convite + "Oi" ambos caem em bucket renato no modo inbox-zero.
    msgs = msgs if msgs is not None else [
        _msg("m3", "Fulano <fulano@gmail.com>", "Convite: Reunião de board"),
        _msg("m4", "Alguém <random@desconhecido.example.com>", "Oi", body="tudo bem?"),
    ]
    for m in msgs:
        fake.msgs[m["id"]] = m
    monkeypatch.setattr(gmail_mod, "GmailIntegration", lambda: fake)
    monkeypatch.setattr(et, "get_db", lambda: _FakeConn())
    monkeypatch.setattr(et, "_resolve_contact_id_by_email", lambda e: None)
    monkeypatch.setattr(et, "_smart_triage_renato", triage_stub)

    async def _fake_fetch(gmail, acc, limit):
        return [{"gmail_id": m["id"], "thread_id": m["threadId"],
                 "account_email": ACC, "account_type": "professional",
                 "access_token": "tok"} for m in msgs]
    monkeypatch.setattr(et, "_fetch_inbox_messages_for_account", _fake_fetch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    return asyncio.run(et.apply_triage_to_inbox(
        account_email=ACC, limit=10, dry_run=True,
        inbox_zero=True, calibration=False, smart=smart))


class TestApplySmartDryRun:
    def test_smart_off_is_legacy_shape(self, monkeypatch):
        async def _never(**k):
            raise AssertionError("smart não deve rodar com smart=False")
        r = _run_smart(monkeypatch, smart=False, triage_stub=_never)
        # OFF: dict de retorno legado — sem chaves smart_*, ping individual mantido.
        assert "smart_mode" not in r
        assert r["wa_renato_would_push"] == 2

    def test_smart_needs_you_suppresses_individual_ping(self, monkeypatch):
        async def _needs(**k):
            return {"resolved": False, "record": {
                "from": (k["headers"].get("from") or "")[:40],
                "subject": (k["headers"].get("subject") or "")[:60],
                "reason": "precisa de decisão"}}
        r = _run_smart(monkeypatch, smart=True, triage_stub=_needs)
        assert r["smart_mode"] is True
        assert r["smart_needs_you"] == 2
        assert r["wa_renato_would_push"] == 0  # consolidado, não 1 por email
        assert "Precisa de você (2)" in r["smart_summary_preview"]

    def test_smart_resolved_dry_run_reports(self, monkeypatch):
        async def _resolve(**k):
            subj = k["headers"].get("subject") or ""
            if subj.startswith("Convite"):
                return {"resolved": True, "record": {
                    "task_id": 101, "titulo": "t", "action": "close",
                    "action_label": "fechei", "new_due_date": None,
                    "reason": "confirmado", "confidence": 0.9, "project_id": 5,
                    "contact_id": 9, "task": {"id": 101}, "from": "f", "subject": subj}}
            return {"resolved": False, "record": {"from": "x", "subject": subj, "reason": "decidir"}}
        r = _run_smart(monkeypatch, smart=True, triage_stub=_resolve)
        assert r["smart_would_resolve"] == 1
        assert r["acted"] == 0  # dry-run nunca age
        assert "fechei #101" in r["smart_summary_preview"]
        # entry do convite marcado como resolved
        resolved_entries = [e for e in r["per_email"] if e.get("smart") == "resolved"]
        assert len(resolved_entries) == 1


# ---------------------------------------------------------------------------
# GATILHO do resumo (recalibração 25/07 — decisão do Renato, opção (a))
# ---------------------------------------------------------------------------
# Medido hoje: 2 WhatsApps em 2 runs só pra anunciar 5 encaminhamentos (3
# errados), com smart_resolved=0 nas 8 runs do dia. Encaminhar pra Andressa é
# execução mecânica -> SILENCIOSO. O resumo só sai quando a camada fechou/
# remarcou task ou quando há e-mail que exige DECISÃO do Renato.
def _msg_andressa(gid="ma"):
    # agilize.com.br está em ANDRESSA_DOMAINS (R3.6) -> bucket andressa.
    return _msg(gid, "Agilize <nao-responda@agilize.com.br>", "Documentos do mês")


async def _never_triage(**k):
    raise AssertionError("parte B não roda em bucket andressa")


class TestSummaryTrigger2507:
    def test_forward_sozinho_nao_dispara_resumo(self, monkeypatch):
        r = _run_smart(monkeypatch, smart=True, triage_stub=_never_triage,
                       msgs=[_msg_andressa()])
        assert r["smart_would_forward"] == 1          # encaminhamento aconteceu
        assert "smart_summary_preview" not in r       # e ficou CALADO
        assert "smart_summary_sent" not in r
        assert r["smart_summary_skipped"] == "forward_only"   # telemetria da run

    def test_forward_aparece_no_resumo_quando_ele_sai_por_outro_motivo(self, monkeypatch):
        async def _needs(**k):
            return {"resolved": False, "record": {
                "from": "x", "subject": k["headers"].get("subject") or "",
                "reason": "precisa de decisão"}}
        r = _run_smart(monkeypatch, smart=True, triage_stub=_needs, msgs=[
            _msg_andressa(),
            _msg("m9", "Alguém <random@desconhecido.example.com>", "Oi", body="tudo bem?"),
        ])
        p = r["smart_summary_preview"]
        assert "Encaminharia p/ Andressa: 1" in p   # não vira invisível
        assert "Precisa de você (1)" in p
        assert "smart_summary_skipped" not in r

    def test_resolveu_task_e_encaminhou_menciona_os_dois(self, monkeypatch):
        async def _resolve(**k):
            subj = k["headers"].get("subject") or ""
            return {"resolved": True, "record": {
                "task_id": 101, "titulo": "t", "action": "close",
                "action_label": "fechei", "new_due_date": None,
                "reason": "confirmado", "confidence": 0.9, "project_id": 5,
                "contact_id": 9, "task": {"id": 101}, "from": "f", "subject": subj}}
        r = _run_smart(monkeypatch, smart=True, triage_stub=_resolve, msgs=[
            _msg_andressa(),
            _msg("m9", "Fulano <fulano@gmail.com>", "Convite: Reunião de board"),
        ])
        p = r["smart_summary_preview"]
        assert "Encaminharia p/ Andressa: 1" in p
        assert "fechei #101" in p
        assert 'desfaz 101' in p                     # endereça pelo ID da task


class TestForwardAudit2507:
    """Ação silenciosa tem que continuar RASTREÁVEL (label + agent_actions)."""

    def test_forward_grava_agent_action_sem_undo(self, monkeypatch):
        g = _FwdGmail()
        logged = {}

        async def _noop_label(*a, **k):
            return {"applied": True}
        monkeypatch.setattr(et, "_apply_generic_label", _noop_label)

        import services.agent_actions as aa

        def _log(**kw):
            logged.update(kw)
            return 1
        monkeypatch.setattr(aa, "log_action", _log)

        r = asyncio.run(et._smart_forward_to_andressa(
            g, "tok", "m1", _HEADERS, {"text": "corpo"}, ACC, ["INBOX"], {},
            dry_run=False))
        assert r["forwarded"] is True
        assert logged["action_type"] == "email_forwarded_andressa"
        assert logged["category"] == "email"
        assert logged["source"] == "inbox_smart_layer"
        assert logged["scope_ref"]["gmail_id"] == "m1"
        assert logged["scope_ref"]["to"] == et.ANDRESSA_FORWARD_TO
        # e-mail enviado não se desenvia: sem undo_hint (o `desfaz N` é de task).
        assert "undo_hint" not in logged

    def test_dry_run_nao_grava_audit(self, monkeypatch):
        import services.agent_actions as aa

        def _boom(**kw):
            raise AssertionError("dry-run não pode gravar agent_action")
        monkeypatch.setattr(aa, "log_action", _boom)
        r = asyncio.run(et._smart_forward_to_andressa(
            _FwdGmail(), "tok", "m1", _HEADERS, {"text": "b"}, ACC, ["INBOX"], {},
            dry_run=True))
        assert r["would"] is True
