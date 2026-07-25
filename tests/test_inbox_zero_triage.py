"""
Testes da frente INBOX-ZERO por marcadores (handoff CoS 24/07).

Cobre a lógica determinística (sem tocar a caixa real do Gmail nem o Neon):
  - kill-switches (INBOX_ZERO_ENABLED / EMAIL_TRIAGE_CALIBRATION_ENABLED)
  - regras calibradas Renato 24/07 (RC1-RC5) via classify_calibrated
  - roteamento por AÇÃO-REQUERIDA puro (resolve_inbox_action) legado vs inbox-zero
  - route_archive_bucket com os novos rule_hits calibrados
  - apply_triage_to_inbox em DRY-RUN com Gmail/DB falsos (corredor + WA preview)

Rodar: .venv/bin/python -m pytest tests/test_inbox_zero_triage.py -v
"""
import os
import sys
import asyncio

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import email_triage as et  # noqa: E402


# ---------------------------------------------------------------------------
# Kill-switches
# ---------------------------------------------------------------------------
class TestKillSwitches:
    def test_inbox_zero_default_off(self, monkeypatch):
        monkeypatch.delenv("INBOX_ZERO_ENABLED", raising=False)
        assert et.is_inbox_zero_enabled() is False

    def test_inbox_zero_on_variants(self, monkeypatch):
        for v in ("1", "true", "on", "YES"):
            monkeypatch.setenv("INBOX_ZERO_ENABLED", v)
            assert et.is_inbox_zero_enabled() is True

    def test_inbox_zero_whitespace(self, monkeypatch):
        monkeypatch.setenv("INBOX_ZERO_ENABLED", " 1\n")
        assert et.is_inbox_zero_enabled() is True

    def test_calibration_default_off(self, monkeypatch):
        monkeypatch.delenv("EMAIL_TRIAGE_CALIBRATION_ENABLED", raising=False)
        assert et.is_calibration_enabled() is False


# ---------------------------------------------------------------------------
# Regras calibradas Renato 24/07 (RC1-RC5) — roteamento por AÇÃO-REQUERIDA
# ---------------------------------------------------------------------------
def _calib(subject="", body="", from_email="x@x.com", from_name="X",
           domain="x.com", account_type="professional"):
    return et.classify_calibrated(
        subject=subject, body_text=body, from_email=from_email,
        from_name=from_name, sender_domain=domain, account_type=account_type,
    )


class TestCalibratedRules:
    def test_rc1_convocacao_titulo_vira_renato(self):
        # Ex ratificado 24/07: Club Athletico "convocação p/ compra de título"
        # PARECE administrativo mas exige a DECISÃO do Renato.
        d = _calib(subject="Convocação para compra de título patrimonial",
                   domain="clubeathletico.com.br")
        assert d["classification"] == "must_read"
        assert "!!Renato" in d["suggested_tags"]
        assert d["rule_hits"] == ["RC1_decisao_renato"]

    def test_rc1_assembleia_body(self):
        d = _calib(subject="Comunicado", body="Fica convocada a assembleia geral ordinária")
        assert d and d["rule_hits"] == ["RC1_decisao_renato"]

    def test_rc2_tabeliao_vira_andressa(self):
        d = _calib(subject="Cancelamento de protesto - instrumento",
                   from_name="3º Tabelião de Protesto", domain="tabelionato.com.br")
        assert d["classification"] == "must_read"
        assert "!Andressa" in d["suggested_tags"]
        assert d["rule_hits"] == ["RC2_tabeliao_andressa"]

    def test_rc3_lastpass_vira_andressa_com_phishing_flag(self):
        d = _calib(subject="Your LastPass security alert",
                   from_email="noreply@lastpass.com", domain="lastpass.com")
        assert "!Andressa" in d["suggested_tags"]
        assert "possivel-phishing" in d["suggested_tags"]
        assert d["rule_hits"] == ["RC3_seguranca_andressa"]

    def test_rc4_mercadolivre_transacional_vira_arquivar(self):
        d = _calib(subject="Seu pacote chegou!",
                   from_email="noreply@mercadolivre.com.br",
                   domain="mercadolivre.com.br")
        assert d["classification"] == "archive_proposed"
        assert d["rule_hits"] == ["RC4_ml_transacional"]

    def test_rc4_ml_sem_txn_nao_dispara(self):
        # ML sem frase transacional NÃO deve capturar (deixa fluir p/ R4+).
        d = _calib(subject="Novidades da semana",
                   from_email="ofertas@mercadolivre.com.br",
                   domain="mercadolivre.com.br")
        assert d is None

    def test_rc5_juridico_com_frente_ativa_vira_renato(self, monkeypatch):
        monkeypatch.setattr(
            et, "_email_juridico_matches_active_front",
            lambda text: {"project_id": 5, "project_name": "Emma"},
        )
        d = _calib(subject="Nova publicação sobre Fictor",
                   from_email="alerta@jusbrasil.com.br", domain="jusbrasil.com.br")
        assert d["classification"] == "must_read"
        assert "!!Renato" in d["suggested_tags"]
        assert d["rule_hits"] == ["RC5_juridico_frente"]

    def test_rc5_juridico_sem_frente_vira_arquivar(self, monkeypatch):
        monkeypatch.setattr(
            et, "_email_juridico_matches_active_front", lambda text: None,
        )
        d = _calib(subject="Nova publicação diário oficial",
                   from_email="alerta@jusbrasil.com.br", domain="jusbrasil.com.br")
        assert d["classification"] == "archive_proposed"
        assert d["rule_hits"] == ["RC5_juridico_sem_frente"]

    def test_no_match_returns_none(self):
        assert _calib(subject="Oi tudo bem", body="vamos conversar") is None


# ---------------------------------------------------------------------------
# route_archive_bucket com os novos rule_hits
# ---------------------------------------------------------------------------
class TestRouteArchiveBucket:
    def test_rc4_ml_arquivar(self):
        assert et.route_archive_bucket(["RC4_ml_transacional"], 0.88) == "arquivar"

    def test_rc5_sem_frente_arquivar(self):
        assert et.route_archive_bucket(["RC5_juridico_sem_frente"], 0.85) == "arquivar"

    def test_legacy_noreply_arquivar(self):
        assert et.route_archive_bucket(["R4_noreply"], 0.95) == "arquivar"

    def test_unsub_deletar(self):
        assert et.route_archive_bucket(["R5_unsub"], 0.92) == "deletar"

    def test_low_conf_noop(self):
        assert et.route_archive_bucket(["R4_noreply"], 0.5) == "noop"

    def test_personal_default_stays_noop_in_legacy(self):
        # Não muda o prod sweep legado: personal-default fica noop (fica no inbox).
        assert et.route_archive_bucket(["R7_personal_default"], 0.78) == "noop"


# ---------------------------------------------------------------------------
# resolve_inbox_action — puro, testável sem Gmail
# ---------------------------------------------------------------------------
def _dec(classification, tags=None, hits=None, conf=0.9):
    return {
        "classification": classification,
        "suggested_tags": tags or [],
        "rule_hits": hits or [],
        "ai_confidence": conf,
    }


ACC = "renato@almeida-prado.com"


class TestResolveLegacy:
    def test_mustread_renato_keeps_inbox(self):
        a = et.resolve_inbox_action(_dec("must_read", ["!!Renato"]), ACC, inbox_zero=False)
        assert a["bucket"] == "renato"
        assert a["archive"] is False
        assert a["notify_renato"] is False

    def test_archive_noreply_arquivar(self):
        a = et.resolve_inbox_action(_dec("archive_proposed", hits=["R4_noreply"]), ACC, False)
        assert a["bucket"] == "arquivar" and a["archive"] is True and a["remove_renato"] is True

    def test_silent_noop_stays_inbox(self):
        a = et.resolve_inbox_action(_dec("silent"), ACC, False)
        assert a["bucket"] == "noop" and a["archive"] is False

    def test_uncertain_archive_noop_stays_inbox(self):
        # personal-default (conf 0.78) -> route_archive_bucket noop -> fica no inbox.
        a = et.resolve_inbox_action(
            _dec("archive_proposed", hits=["R7_personal_default"], conf=0.78), ACC, False)
        assert a["bucket"] == "noop" and a["archive"] is False


class TestResolveInboxZero:
    def test_mustread_renato_leaves_inbox_and_notifies(self):
        a = et.resolve_inbox_action(_dec("must_read", ["!!Renato"]), ACC, inbox_zero=True)
        assert a["bucket"] == "renato"
        assert a["archive"] is True          # MUDANÇA 2: must_read sai do inbox
        assert a["notify_renato"] is True    # MUDANÇA 3: WA no novo !!Renato

    def test_financeiro_funde_em_andressa(self):
        # Tags reais do classificador p/ financeiro (R3.5/R3.5b): ["!!Renato","financeiro"].
        a = et.resolve_inbox_action(
            _dec("must_read", ["!!Renato", "financeiro"]),
            "renato.almeida.prado@gmail.com", True)
        assert a["bucket"] == "andressa"
        assert a["labels"] == ["!Andressa"]
        assert a["notify_renato"] is False   # Andressa não interrompe

    def test_andressa_bucket_no_notify(self):
        a = et.resolve_inbox_action(_dec("must_read", ["!Andressa"]), ACC, True)
        assert a["bucket"] == "andressa" and a["archive"] is True and a["notify_renato"] is False

    def test_silent_goes_to_renato_queue(self):
        # MUDANÇA 1+2: silent/incerto NÃO fica no inbox -> fila do Renato.
        a = et.resolve_inbox_action(_dec("silent"), ACC, True)
        assert a["bucket"] == "renato"
        assert a["labels"] == ["!!Renato"]
        assert a["archive"] is True and a["notify_renato"] is True

    def test_personal_default_archives_not_renato(self):
        # Ruído pessoal (R7 personal) NÃO deve inundar o Renato: arquiva direto.
        a = et.resolve_inbox_action(
            _dec("archive_proposed", hits=["R7_personal_default"], conf=0.78), ACC, True)
        assert a["bucket"] == "arquivar" and a["archive"] is True and a["notify_renato"] is False

    def test_unknown_archive_proposed_goes_to_renato(self):
        # archive_proposed com hit desconhecido -> Renato (não some silenciosamente).
        a = et.resolve_inbox_action(
            _dec("archive_proposed", hits=["R_desconhecido"], conf=0.9), ACC, True)
        assert a["bucket"] == "renato" and a["notify_renato"] is True

    def test_deletar_bucket(self):
        a = et.resolve_inbox_action(_dec("archive_proposed", hits=["R5_unsub"]), ACC, True)
        assert a["bucket"] == "deletar" and a["labels"] == ["!!Deletar"] and a["remove_renato"] is True


# ---------------------------------------------------------------------------
# apply_triage_to_inbox — DRY-RUN com Gmail/DB falsos (não toca a caixa real)
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self):
        self._rows = []
        self._one = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from google_accounts" in s and "conectado" in s:
            self._rows = [{
                "id": 1, "email": ACC, "tipo": "professional",
                "access_token": "tok", "refresh_token": "ref",
            }]
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


_LABELS = [
    {"name": "!!Renato", "id": "L_REN"},
    {"name": "!Andressa", "id": "L_AND"},
    {"name": "!!Deletar", "id": "L_DEL"},
    {"name": "7-Financeiro/Recibos", "id": "L_FIN"},
]


class _FakeGmail:
    """Gmail dublê: só leitura, nenhum write. Em dry-run nem write seria chamado."""
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
        return {"messages": [{"labelIds": ["INBOX"]}]}  # 1 turno -> não resolvido


def _msg(gid, frm, subject, body="", labels=None, unsub=None):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if unsub:
        headers.append({"name": "List-Unsubscribe", "value": unsub})
    return {
        "id": gid, "threadId": "t_" + gid,
        "labelIds": labels or ["INBOX"],
        "payload": {"headers": headers},
        "_headers": {"from": frm, "subject": subject},
        "_body": body,
    }


def _run_dry(monkeypatch, inbox_zero, calibration=False):
    import integrations.gmail as gmail_mod

    fake = _FakeGmail()
    msgs = [
        _msg("m1", "News <noreply@news.example.com>", "Novidades da semana"),
        _msg("m2", "Loja <deals@shop.example.com>", "Oferta imperdível",
             unsub="<https://shop.example.com/unsub>"),
        _msg("m3", "Fulano <fulano@gmail.com>", "Convite: Reunião de board"),
        _msg("m4", "Alguém <random@desconhecido.example.com>", "Oi", body="tudo bem?"),
    ]
    for m in msgs:
        fake.msgs[m["id"]] = m

    monkeypatch.setattr(gmail_mod, "GmailIntegration", lambda: fake)
    monkeypatch.setattr(et, "get_db", lambda: _FakeConn())
    monkeypatch.setattr(et, "_resolve_contact_id_by_email", lambda e: None)

    async def _fake_fetch(gmail, acc, limit):
        return [{
            "gmail_id": m["id"], "thread_id": m["threadId"],
            "account_email": ACC, "account_type": "professional",
            "access_token": "tok",
        } for m in msgs]

    monkeypatch.setattr(et, "_fetch_inbox_messages_for_account", _fake_fetch)

    return asyncio.run(et.apply_triage_to_inbox(
        account_email=ACC, limit=10, dry_run=True,
        inbox_zero=inbox_zero, calibration=calibration,
    ))


class TestApplyDryRun:
    def test_legacy_keeps_uncertain_in_inbox(self, monkeypatch):
        r = _run_dry(monkeypatch, inbox_zero=False)
        assert r["ok"] is True
        assert r["inbox_zero_mode"] is False
        assert r["acted"] == 0  # dry-run nunca age
        by = {e["subject"][:20]: e for e in r["per_email"]}
        # noreply -> arquivar; unsub -> deletar; convite -> renato (mantém inbox);
        # "Oi" (silent) -> noop fica no inbox (legado).
        assert r["by_bucket"]["arquivar"] == 1
        assert r["by_bucket"]["deletar"] == 1
        assert r["by_bucket"]["renato"] == 1
        assert r["by_bucket"]["noop"] == 1
        # Legado NÃO notifica Renato (must_read fica no inbox, sem "novo balde").
        assert r["wa_renato_would_push"] == 0

    def test_inbox_zero_empties_inbox(self, monkeypatch):
        r = _run_dry(monkeypatch, inbox_zero=True)
        assert r["ok"] is True
        assert r["inbox_zero_mode"] is True
        assert r["acted"] == 0
        # Corredor: NADA fica em noop (inbox 100% roteado).
        assert r["by_bucket"]["noop"] == 0
        assert r["by_bucket"]["arquivar"] == 1   # noreply
        assert r["by_bucket"]["deletar"] == 1    # unsub comercial
        # convite (must_read !!Renato) + "Oi" (silent -> fila Renato) = 2 Renato
        assert r["by_bucket"]["renato"] == 2
        # Dois novos !!Renato -> preview de 2 avisos WA.
        assert r["wa_renato_would_push"] == 2
        # Toda ação de inbox-zero inclui archive (sai do inbox).
        for e in r["per_email"]:
            assert "archive" in e["action"]
