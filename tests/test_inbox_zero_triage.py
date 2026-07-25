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
# Recalibracao 25/07 — RC6 recibo / RC7 device-alert / RC8 marketing
# ---------------------------------------------------------------------------
class TestRecalibration2507:
    def test_rc6_receipt_vira_financeiro(self):
        # "Your receipt from Anthropic" batia R3_frente->Renato. Agora recibo
        # = execucao contabil -> Financeiro (funde em Andressa no inbox-zero).
        d = _calib(subject="Your receipt from Anthropic, PBC",
                   from_email="receipts@anthropic.com", domain="anthropic.com")
        assert d["classification"] == "must_read"
        assert "financeiro" in d["suggested_tags"]
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]

    def test_rc6_recibo_pt(self):
        d = _calib(subject="Recibo de pagamento -  Poli Angels",
                   from_email="mailer@vindi.com.br", domain="vindi.com.br")
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]
        assert "financeiro" in d["suggested_tags"]

    def test_rc6_boleto_real_nao_vira_marketing(self):
        # "Boleto disponível" (Serasa) tem RC6 (boleto) ANTES do RC8 marketing:
        # boleto real nunca eh arquivado como marketing.
        d = _calib(subject="Boleto disponível! 🚨",
                   from_email="carteiradigital@comunicados.serasa.com.br",
                   domain="comunicados.serasa.com.br")
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]

    def test_rc7_device_notify_vira_arquivar(self):
        # "new trusted device added" = notificacao (nada a fazer) -> arquivar.
        d = _calib(subject="Anthropic: Security alert: new trusted device added",
                   from_email="noreply@anthropic.com", domain="anthropic.com")
        assert d["classification"] == "archive_proposed"
        assert d["rule_hits"] == ["RC7_device_notify"]

    def test_rc7_govbr_novo_dispositivo(self):
        d = _calib(subject="gov.br: Alerta de segurança: acesso em novo dispositivo",
                   from_email="naoresponda@acesso.gov.br", domain="acesso.gov.br")
        assert d["rule_hits"] == ["RC7_device_notify"]

    def test_rc7_cede_pra_rc3_quando_exige_acao(self):
        # "security alert" + "reset your password" = AÇÃO -> RC3 (Andressa),
        # nao RC7 (a frase-de-acao vence a notificacao de dispositivo).
        d = _calib(subject="Security alert: new sign-in — reset your password now",
                   from_email="noreply@somebank.com", domain="somebank.com")
        assert d["rule_hits"] == ["RC3_seguranca_andressa"]
        assert "!Andressa" in d["suggested_tags"]

    def test_rc8_marketing_subject_vira_arquivar(self):
        # IBGC "Últimas vagas | Masterclass" batia R3_frente->Renato. Agora RC8.
        d = _calib(subject="Últimas vagas | Masterclass – Governança Climática",
                   from_email="notifications@instructure.com",
                   domain="instructure.com")
        assert d["classification"] == "archive_proposed"
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    def test_rc8_bulk_subdomain_vira_arquivar(self):
        # Railway news (news.railway.app) sem frase de marketing — subdominio
        # de disparo em massa (news.) -> arquivar.
        d = _calib(subject="DNS logs, query the Railway API from the CLI",
                   from_email="hello@news.railway.app", domain="news.railway.app")
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    def test_rc8_cora_novidade_prefixo(self):
        # "Novidade:" (com ':') = copy de marketing -> arquivar.
        d = _calib(subject="Novidade: cartão virtual temporário para sua empresa",
                   from_email="nao-responda@cora.com.br", domain="cora.com.br")
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    def test_rc8_novidades_sem_dois_pontos_nao_dispara(self):
        # Guard do teste RC4: "Novidades da semana" (sem ':') nao vira marketing.
        d = _calib(subject="Novidades da semana",
                   from_email="contato@empresa.com.br", domain="empresa.com.br")
        assert d is None

    def test_rc8_route_archive_bucket_arquivar(self):
        assert et.route_archive_bucket(["RC8_vendor_marketing"], 0.85) == "arquivar"
        assert et.route_archive_bucket(["RC7_device_notify"], 0.85) == "arquivar"

    def test_rc7_rc8_inbox_zero_arquivam_nao_renato(self):
        # No inbox-zero, RC7/RC8 (archive_proposed) NAO caem na fila do Renato.
        for hit in ("RC7_device_notify", "RC8_vendor_marketing"):
            a = et.resolve_inbox_action(
                _dec("archive_proposed", hits=[hit], conf=0.85), ACC, True)
            assert a["bucket"] == "arquivar", hit
            assert a["notify_renato"] is False, hit


# ---------------------------------------------------------------------------
# Ordem: calibracao roda ANTES do R3 (frente keyword) — raiz do FP 25/07
# ---------------------------------------------------------------------------
class TestCalibrationBeforeFrente:
    def _svc_classify(self, monkeypatch, subject, from_email, domain,
                      enable_calibration):
        # Forca is_frente_keyword a "casar" QUALQUER texto (simula 'Anthropic'/
        # 'IBGC'/'Poli Angels' serem frente keywords no DB de prod).
        import services.cos_keywords as ck
        monkeypatch.setattr(ck, "is_frente_keyword", lambda text: 2)
        svc = et.EmailTriageService()
        headers = {"from": f"X <{from_email}>", "subject": subject}
        return svc.classify_email_cos(
            headers=headers, body_text="", gmail_label_ids=[],
            account_email=ACC, account_type="professional",
            contact_id=None, enable_calibration=enable_calibration,
        )

    def test_receipt_com_frente_kw_vai_pra_financeiro_nao_r3(self, monkeypatch):
        # Recibo que MENCIONA frente: calibracao ON -> RC6 (nao R3_frente).
        d = self._svc_classify(
            monkeypatch, "Your receipt from Anthropic",
            "receipts@anthropic.com", "anthropic.com", enable_calibration=True)
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]

    def test_marketing_com_frente_kw_vai_pra_rc8_nao_r3(self, monkeypatch):
        d = self._svc_classify(
            monkeypatch, "Últimas vagas | Masterclass IBGC",
            "notifications@instructure.com", "instructure.com",
            enable_calibration=True)
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    def test_legado_calibracao_off_ainda_bate_r3_frente(self, monkeypatch):
        # Kill-switch OFF: comportamento LEGADO — R3_frente vence (byte-a-byte).
        d = self._svc_classify(
            monkeypatch, "Your receipt from Anthropic",
            "receipts@anthropic.com", "anthropic.com", enable_calibration=False)
        assert d["rule_hits"] == ["R3_frente"]
        assert "!!Renato" in d["suggested_tags"]


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


# ---------------------------------------------------------------------------
# B1 (25/07) — guard de vendor de massa telecom/utility.
#
# Bug real (run 13:25 BRT, cron_runs #46449): dois e-mails da Vivo foram pro
# balde ANDRESSA via R3_5_financial_gov ("Whitelist financeiro (vivo.com.br)
# (sem keyword — priority menor)"), e a camada esperta ainda os ENCAMINHOU.
# Causa-raiz: o DOMÍNIO estava valendo como prova de cobrança.
#
# Correção genérica (nenhuma regra cita "Vivo" como marca): categoria de
# fornecedor (telecom/utilities) + exigência de evidência POSITIVA de cobrança.
# ---------------------------------------------------------------------------
class TestVendorBulkGuard2507:
    # --- os dois e-mails REAIS da run 13:25 --------------------------------
    def test_vivo_contrato_eletronico_vira_arquivar(self):
        d = _calib(subject="Veja seu contrato dos serviços Vivo",
                   from_email="contratoeletronico@vivo.com.br",
                   from_name="Vivo - Contrato do Cliente",
                   domain="vivo.com.br", account_type="personal")
        assert d["classification"] == "archive_proposed"
        assert d["rule_hits"] == ["RC8_vendor_marketing"]
        assert "!Andressa" not in d["suggested_tags"]
        assert "!!Renato" not in d["suggested_tags"]

    def test_vivo_campanha_app_vira_arquivar(self):
        d = _calib(subject="Vai de App Vivo! Benefícios.",
                   from_email="vivo@vivo.com.br", from_name="",
                   domain="vivo.com.br", account_type="personal")
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    # --- direção oposta: cobrança REAL não pode regredir -------------------
    def test_vivo_fatura_real_continua_financeiro(self):
        d = _calib(subject="Sua fatura Vivo já está disponível",
                   from_email="faturadigital@vivo.com.br", domain="vivo.com.br")
        assert d["classification"] == "must_read"
        assert "financeiro" in d["suggested_tags"]
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]

    def test_vivo_boleto_segunda_via_continua_financeiro(self):
        d = _calib(subject="Segunda via do boleto — vencimento em 30/07",
                   from_email="cobranca@vivo.com.br", domain="vivo.com.br")
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]
        assert "financeiro" in d["suggested_tags"]

    def test_comgas_fatura_continua_financeiro(self):
        d = _calib(subject="Comgás - Sua fatura de gás venceu",
                   from_email="naoresponda@comgas.com.br", domain="comgas.com.br")
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]

    def test_cobranca_no_assunto_vence_copy_promocional(self):
        # Assunto misto (promo + cobrança): cobrança no ASSUNTO sempre ganha.
        d = _calib(subject="Aproveite o desconto e pague sua fatura hoje",
                   from_email="mkt@claro.com.br", domain="claro.com.br")
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]

    def test_rodape_com_fatura_em_peca_promocional_vira_arquivar(self):
        # Campanha cujo ÚNICO sinal financeiro está no rodapé + assunto promo.
        d = _calib(subject="Oferta exclusiva: dobro de internet",
                   body="Aproveite. Consulte sua fatura no app Meu Vivo.",
                   from_email="ofertas@vivo.com.br", domain="vivo.com.br")
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    def test_rodape_com_fatura_sem_assunto_promo_nao_arquiva(self):
        # Sem copy promocional no assunto e sem List-Unsubscribe: conservador,
        # o corpo com "fatura" mantém a trilha financeira (RC6).
        d = _calib(subject="Comunicado Vivo",
                   body="Segue o valor a pagar da sua fatura deste mês.",
                   from_email="atendimento@vivo.com.br", domain="vivo.com.br")
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]

    def test_list_unsubscribe_conta_como_marcador_de_massa(self):
        d = et.classify_calibrated(
            subject="Comunicado Vivo", body_text="Consulte sua fatura no app.",
            from_email="news@vivo.com.br", from_name="Vivo",
            sender_domain="vivo.com.br", has_unsubscribe=True,
        )
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    # --- generalidade: não é hardcode de marca -----------------------------
    def test_guard_vale_pra_outros_vendors_da_categoria(self):
        for dom in ("claro.com.br", "tim.com.br", "enel.com.br", "cpfl.com.br",
                    "sabesp.com.br", "email.comgas.com.br"):
            d = _calib(subject="Conheça as vantagens do nosso clube",
                       from_email=f"mkt@{dom}", domain=dom)
            assert d is not None and d["rule_hits"] == ["RC8_vendor_marketing"], dom

    def test_nao_vaza_pra_fornecedor_fora_da_categoria(self):
        # Domínio comum sem copy de marketing continua caindo fora do RC8.
        assert _calib(subject="Veja seu contrato dos serviços",
                      from_email="contato@empresa.com.br",
                      domain="empresa.com.br") is None

    def test_conta_comgas_caixa_de_cobranca_nao_arquiva(self):
        # Achado da auditoria do dia 1 (run 11:02): "Comgas <suafatura@comgas.
        # com.br> — 'Conta Comgas'" é fatura de gás REAL, mas nem o assunto nem
        # o corpo trazem keyword financeira. O local-part é quem salva.
        assert _calib(subject="Conta Comgas", body="",
                      from_email="suafatura@comgas.com.br",
                      domain="comgas.com.br") is None

    def test_caixa_de_cobranca_vence_ate_com_assunto_promocional(self):
        d = _calib(subject="Conheça as vantagens", body="",
                   from_email="boleto@claro.com.br", domain="claro.com.br")
        assert d is None  # não vira RC8; segue pro whitelist financeiro

    def test_helper_billing_sender(self):
        assert et._is_billing_sender("suafatura@comgas.com.br") is True
        assert et._is_billing_sender("cobranca@vivo.com.br") is True
        assert et._is_billing_sender("financeiro@x.com") is True
        assert et._is_billing_sender("contratoeletronico@vivo.com.br") is False
        assert et._is_billing_sender("mkt@vivo.com.br") is False
        assert et._is_billing_sender("") is False

    def test_helper_dominio_e_subdominio(self):
        assert et._is_vendor_bulk_domain("vivo.com.br") is True
        assert et._is_vendor_bulk_domain("email.vivo.com.br") is True
        assert et._is_vendor_bulk_domain("") is False
        assert et._is_vendor_bulk_domain("naovivo.com.br") is False

    def test_bucket_final_e_arquivar_no_inbox_zero(self):
        d = _calib(subject="Veja seu contrato dos serviços Vivo",
                   from_email="contratoeletronico@vivo.com.br",
                   domain="vivo.com.br", account_type="personal")
        a = et.resolve_inbox_action(d, ACC, True)
        assert a["bucket"] == "arquivar"
        assert a["notify_renato"] is False
        assert a["archive"] is True


class TestVendorBulkGuardServiceLevel:
    """Replay pelo classify_email_cos — o caminho que rodou em produção."""

    def _svc(self, subject, from_email, domain, calibration, body="",
             account_type="personal", headers_extra=None):
        svc = et.EmailTriageService()
        headers = {"from": f"X <{from_email}>", "subject": subject}
        headers.update(headers_extra or {})
        return svc.classify_email_cos(
            headers=headers, body_text=body, gmail_label_ids=[],
            account_email="renato.almeida.prado@gmail.com",
            account_type=account_type, contact_id=None,
            enable_calibration=calibration,
        )

    def test_regressao_vivo_calibracao_on(self):
        d = self._svc("Veja seu contrato dos serviços Vivo",
                      "contratoeletronico@vivo.com.br", "vivo.com.br", True)
        assert d["rule_hits"] == ["RC8_vendor_marketing"]
        assert et.resolve_inbox_action(d, ACC, True)["bucket"] == "arquivar"

    def test_regressao_vivo_calibracao_off_sai_do_whitelist(self):
        # Com calibração OFF o RC não roda: o guard do R3.5 é quem impede que
        # o domínio Vivo, sozinho, vire "financeiro" -> Andressa.
        d = self._svc("Veja seu contrato dos serviços Vivo",
                      "contratoeletronico@vivo.com.br", "vivo.com.br", False)
        assert "R3_5_financial_gov" not in d["rule_hits"]
        assert "financeiro" not in [str(t).lower() for t in d["suggested_tags"]]

    def test_fatura_real_calibracao_off_continua_financeiro(self):
        d = self._svc("Sua fatura Vivo já está disponível",
                      "faturadigital@vivo.com.br", "vivo.com.br", False)
        assert d["rule_hits"] == ["R3_5_financial_gov"]
        assert d["classification"] == "must_read"

    def test_conta_comgas_continua_financeiro_calibracao_off(self):
        # Regressão do dia 1 replicada pelo caminho real (R3.5): o local-part
        # suafatura@ mantém a conta de gás no whitelist -> Andressa/Financeiro.
        d = self._svc("Conta Comgas", "suafatura@comgas.com.br",
                      "comgas.com.br", False)
        assert d["rule_hits"] == ["R3_5_financial_gov"]
        assert d["classification"] == "must_read"

    def test_banco_fora_da_categoria_nao_muda(self):
        # Guard é só telecom/utility: banco continua no whitelist financeiro.
        d = self._svc("Extrato disponível", "noreply@itau.com.br",
                      "itau.com.br", False)
        assert d["rule_hits"] == ["R3_5_financial_gov"]


# ---------------------------------------------------------------------------
# Extensão 25/07 — CAMPANHA de massa em domínio FINANCEIRO não-telecom
# ---------------------------------------------------------------------------
class TestFinancialBulkCampaign2507:
    """Pesquisa/NPS/indicação de domínio financeiro -> arquivar.

    Casos reais medidos hoje (todos R3_5_financial_gov conf 0.78 -> !!Renato+
    financeiro -> balde Andressa -> a camada esperta ainda encaminhava).
    """

    # --- os e-mails REAIS ---------------------------------------------------
    def test_quintoandar_nps_pesquisa_vira_arquivar(self):
        d = _calib(subject="Ainda dá tempo de responder a pesquisa!",
                   from_email="nao-responda@quintoandar.com.br",
                   from_name="QuintoAndar", domain="quintoandar.com.br",
                   account_type="personal")
        assert d["classification"] == "archive_proposed"
        assert d["rule_hits"] == ["RC8_vendor_marketing"]
        assert "!Andressa" not in d["suggested_tags"]
        assert "!!Renato" not in d["suggested_tags"]

    def test_quintoandar_melhorar_experiencia_vira_arquivar(self):
        d = _calib(subject="Queremos melhorar sua experiência",
                   from_email="nao-responda@quintoandar.com.br",
                   domain="quintoandar.com.br", account_type="personal")
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    def test_cora_campanha_indicacao_vira_arquivar(self):
        d = _calib(subject="Lembrete: Sua indicação vale até R$2.000 em limite",
                   from_email="nao-responda@cora.com.br", domain="cora.com.br")
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    def test_quintoandar_consorcio_vira_arquivar(self):
        d = _calib(subject="Conheça o Consórcio QuintoAndar e pague 50% da parcela",
                   from_email="noreply@email.quintoandar.com.br",
                   domain="email.quintoandar.com.br", account_type="personal")
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    # --- direção proibida: cobrança/aviso real NÃO pode regredir ------------
    def test_boleto_aluguel_quintoandar_continua_financeiro(self):
        d = _calib(subject="Boleto de aluguel disponível — vence em 05/08",
                   from_email="nao-responda@quintoandar.com.br",
                   domain="quintoandar.com.br")
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]
        assert "financeiro" in d["suggested_tags"]

    def test_pesquisa_com_boleto_no_assunto_nao_arquiva(self):
        # Assunto misto: evidência de cobrança sempre ganha da copy de campanha.
        d = _calib(subject="Responda a pesquisa e veja seu boleto",
                   from_email="nao-responda@quintoandar.com.br",
                   domain="quintoandar.com.br")
        assert d["rule_hits"] == ["RC6_recibo_financeiro"]

    def test_pesquisa_com_cobranca_no_corpo_nao_arquiva(self):
        # Mais restrito que o guard de telecom (que tolera fatura só no rodapé):
        # em domínio financeiro, cobrança no CORPO já desliga o guard.
        assert et._financial_bulk_campaign(
            "avalie sua experiencia", "sua fatura vence amanha",
            "cora.com.br", from_email="nao-responda@cora.com.br") is False

    def test_cobranca_cora_continua_na_trilha(self):
        d = _calib(subject="Aviso de cobrança", from_email="nao-responda@cora.com.br",
                   domain="cora.com.br")
        # Nem RC8: cai fora das regras calibradas e segue pro whitelist (R3.5).
        assert d is None or d["rule_hits"] != ["RC8_vendor_marketing"]

    def test_mudanca_contratual_cora_continua_na_trilha(self):
        # Caso real 24/07, MESMA caixa (nao-responda@) das campanhas: não tem
        # keyword financeira nenhuma e mesmo assim tem que ir pro Renato.
        d = _calib(subject="Alteração dos Termos e Condições do Cartão de Crédito Cora",
                   from_email="nao-responda@cora.com.br", domain="cora.com.br")
        assert d is None

    def test_seguranca_de_conta_bancaria_continua_na_trilha(self):
        d = _calib(subject="Detectamos atividade suspeita na sua conta",
                   from_email="nao-responda@cora.com.br", domain="cora.com.br")
        assert d["rule_hits"] == ["RC3_seguranca_andressa"]

    def test_extrato_nao_e_campanha(self):
        assert et._financial_bulk_campaign(
            "extrato mensal disponivel", "", "itau.com.br") is False

    def test_fatura_comgas_nao_entra_neste_guard(self):
        # Comgás é telecom/utility: guard próprio, este aqui nem olha.
        assert et._is_financial_bulk_domain("comgas.com.br") is False
        assert _calib(subject="Conta Comgas", from_email="suafatura@comgas.com.br",
                      domain="comgas.com.br") is None

    def test_gov_receita_intocado(self):
        assert et._is_financial_bulk_domain("receita.fazenda.gov.br") is False
        assert et._is_financial_bulk_domain("tjmg.jus.br") is False
        assert et._financial_bulk_campaign(
            "responda a pesquisa de satisfacao", "",
            "receita.fazenda.gov.br") is False

    # --- helpers ------------------------------------------------------------
    def test_helper_dominio_derivado_do_whitelist(self):
        assert et._is_financial_bulk_domain("quintoandar.com.br") is True
        assert et._is_financial_bulk_domain("email.quintoandar.com.br") is True
        assert et._is_financial_bulk_domain("cora.com.br") is True
        assert et._is_financial_bulk_domain("itau.com.br") is True
        assert et._is_financial_bulk_domain("vivo.com.br") is False   # telecom
        assert et._is_financial_bulk_domain("empresa.com.br") is False
        assert et._is_financial_bulk_domain("") is False

    def test_caixa_de_cobranca_desliga_o_guard(self):
        assert et._financial_bulk_campaign(
            "avalie sua experiencia com o atendimento", "",
            "cora.com.br", from_email="boleto@cora.com.br") is False

    def test_nps_de_caixa_de_cobranca_ainda_cai_no_rc8_generico(self):
        # Divergência CONSCIENTE: o sinal (a) do RC8 é domain-agnóstico. Um
        # assunto que diz explicitamente "avalie sua experiência" é pesquisa,
        # não cobrança — o local-part não muda o que o e-mail PEDE (nada).
        d = _calib(subject="Avalie sua experiência com o atendimento",
                   from_email="boleto@cora.com.br", domain="cora.com.br")
        assert d["rule_hits"] == ["RC8_vendor_marketing"]

    def test_bucket_final_e_arquivar_no_inbox_zero(self):
        d = _calib(subject="Ainda dá tempo de responder a pesquisa!",
                   from_email="nao-responda@quintoandar.com.br",
                   domain="quintoandar.com.br", account_type="personal")
        a = et.resolve_inbox_action(d, ACC, True)
        assert a["bucket"] == "arquivar"
        assert a["notify_renato"] is False
        assert a["archive"] is True


class TestPrintiTrustvoxNPS:
    """Printi <coleta@info.trustvox.com.br> — 'quantas estrelas o seu pedido
    merece?'. VEREDITO: mesma CLASSE de ação-requerida (nenhuma), mecanismo
    DIFERENTE — trustvox não é domínio financeiro, então não dá pra tratar pelo
    guard financeiro. Entra pelo sinal (a) do RC8 (frase de campanha no assunto,
    válida pra qualquer remetente). R1 (imprensa) e R2 (C1/C2) rodam ANTES do
    bloco RC, então pesquisa de contato do círculo 1 continua protegida, e
    arquivar é reversível."""

    def test_printi_nps_vira_arquivar(self):
        d = _calib(subject="ALMEIDA, quantas estrelas o seu pedido merece?",
                   from_email="coleta@info.trustvox.com.br",
                   from_name="Printi", domain="info.trustvox.com.br")
        assert d["classification"] == "archive_proposed"
        assert d["rule_hits"] == ["RC8_vendor_marketing"]
        assert et.resolve_inbox_action(d, ACC, True)["bucket"] == "arquivar"

    def test_pesquisa_generica_de_qualquer_remetente(self):
        for subj in ("Pesquisa de satisfação — como fomos?",
                     "Sua opinião é muito importante pra nós",
                     "Indique e ganhe R$100"):
            d = _calib(subject=subj, from_email="x@fornecedor.com.br",
                       domain="fornecedor.com.br")
            assert d is not None and d["rule_hits"] == ["RC8_vendor_marketing"], subj

    def test_palavra_pesquisa_sozinha_nao_arquiva(self):
        # "pesquisa" solta (pesquisa de mercado/jurídica) NÃO é gatilho.
        assert _calib(subject="Resultado da pesquisa de mercado do setor",
                      from_email="x@fornecedor.com.br",
                      domain="fornecedor.com.br") is None


class TestFinancialBulkCampaignServiceLevel:
    """Replay pelo classify_email_cos — o caminho que rodou em produção."""

    def _svc(self, subject, from_email, domain, calibration, body="",
             account_type="personal"):
        svc = et.EmailTriageService()
        return svc.classify_email_cos(
            headers={"from": f"X <{from_email}>", "subject": subject},
            body_text=body, gmail_label_ids=[],
            account_email="renato.almeida.prado@gmail.com",
            account_type=account_type, contact_id=None,
            enable_calibration=calibration,
        )

    def test_regressao_quintoandar_calibracao_on(self):
        d = self._svc("Ainda dá tempo de responder a pesquisa!",
                      "nao-responda@quintoandar.com.br", "quintoandar.com.br", True)
        assert d["rule_hits"] == ["RC8_vendor_marketing"]
        assert et.resolve_inbox_action(d, ACC, True)["bucket"] == "arquivar"

    def test_regressao_cora_calibracao_on(self):
        d = self._svc("Lembrete: Sua indicação vale até R$2.000 em limite",
                      "nao-responda@cora.com.br", "cora.com.br", True)
        assert d["rule_hits"] == ["RC8_vendor_marketing"]
        assert et.resolve_inbox_action(d, ACC, True)["bucket"] == "arquivar"

    def test_calibracao_off_sai_do_whitelist_financeiro(self):
        d = self._svc("Ainda dá tempo de responder a pesquisa!",
                      "nao-responda@quintoandar.com.br", "quintoandar.com.br", False)
        assert "R3_5_financial_gov" not in d["rule_hits"]
        assert "financeiro" not in [str(t).lower() for t in d["suggested_tags"]]

    def test_calibracao_off_cora_nao_cai_no_balde_andressa_por_r3_6(self):
        # cora.com.br está em ANDRESSA_DOMAINS (R3.6): sem o guard lá também, o
        # bug só mudaria de porta (R3.5 -> R3.6) e o e-mail continuaria indo
        # pro balde da Andressa.
        d = self._svc("Lembrete: Sua indicação vale até R$2.000 em limite",
                      "nao-responda@cora.com.br", "cora.com.br", False)
        assert "R3_6_andressa" not in d["rule_hits"]
        assert "!Andressa" not in d["suggested_tags"]

    def test_cora_admin_normal_continua_indo_pra_andressa(self):
        # Sem copy de campanha, cora.com.br segue a trilha de sempre.
        d = self._svc("Documentos do mês", "nao-responda@cora.com.br",
                      "cora.com.br", False)
        assert d["rule_hits"] == ["R3_5_financial_gov"]

    def test_boleto_aluguel_calibracao_off_continua_financeiro(self):
        d = self._svc("Boleto de aluguel disponível",
                      "nao-responda@quintoandar.com.br", "quintoandar.com.br", False)
        assert d["rule_hits"] == ["R3_5_financial_gov"]
        assert d["classification"] == "must_read"

    def test_mudanca_contratual_calibracao_off_continua_financeiro(self):
        d = self._svc("Alteração dos Termos e Condições do Cartão de Crédito Cora",
                      "nao-responda@cora.com.br", "cora.com.br", False)
        assert d["rule_hits"] == ["R3_5_financial_gov"]

    def test_itau_extrato_nao_regride(self):
        for calib in (False, True):
            d = self._svc("Extrato disponível", "noreply@itau.com.br",
                          "itau.com.br", calib)
            assert d["rule_hits"] == ["R3_5_financial_gov"], calib

    def test_receita_gov_nao_regride(self):
        for calib in (False, True):
            d = self._svc("Sua declaração IRPF 2026",
                          "noreply@receita.fazenda.gov.br",
                          "receita.fazenda.gov.br", calib)
            assert d["rule_hits"] == ["R3_5_financial_gov"], calib
            assert "gov" in [str(t).lower() for t in d["suggested_tags"]]

    def test_comgas_conta_nao_regride(self):
        for calib in (False, True):
            d = self._svc("Conta Comgas", "suafatura@comgas.com.br",
                          "comgas.com.br", calib)
            assert d["rule_hits"] == ["R3_5_financial_gov"], calib
