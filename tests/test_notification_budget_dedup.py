"""
Testes do teto diario de interrupcao + dedup cross-produtor do notification_router.

Contexto (dados reais de 14 dias de producao): 127 WhatsApps do sistema, 5
respostas. channel_decisions mostrava 35 linhas com decision_rule='score_ge_8'
(urgencia AUTO-DECLARADA pelo produtor) e quase nenhuma regra deterministica —
ou seja, a graduacao de canal do F-B nunca acontecia. Alem disso o mesmo e-mail
do pai do Renato ("Re: Regularizacao contabil das minhas 7 empresas") chegava
2x: uma por email_triage, outra dentro do resumo do inbox_smart.

Cobre:
  - teto diario respeitado (candidato a WA desce pra push)
  - rebaixamento REGISTRADO em channel_decisions e item NAO perdido
    (push indisponivel -> pill/pending; a rule do downgrade sobrevive)
  - regra deterministica furando o teto (e auto-declarado NAO furando)
  - dedup cross-produtor pegando o caso Orestes (email_triage -> inbox_smart)
  - virada de dia BRT (o teto conta o dia de Brasilia, nao o dia UTC)
  - kill-switches (defaults + reversibilidade por env)

Rodar:
  PYTHONPATH=app .venv/bin/python -m pytest tests/test_notification_budget_dedup.py -q
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import notification_router as nr  # noqa: E402
from services.tz import now_utc  # noqa: E402


# ---------------------------------------------------------------------------
# Fake DB — channel_decisions + pending_notifications em memoria
# ---------------------------------------------------------------------------
def _like(value: str, pattern: str) -> bool:
    """LIKE do Postgres (com escape por barra invertida) -> regex."""
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.match("^" + "".join(out) + "$", value or "", re.DOTALL) is not None


class FakeDB:
    def __init__(self, now=None):
        self.channel_rows = []
        self.pending_rows = []
        self.now = now or now_utc().replace(tzinfo=None)
        self._seq = 0

    # --- helpers de seed -------------------------------------------------
    def seed_whatsapp(self, source, created_at, dedup_key=None, sent_ok=True,
                      decision_rule="score_ge_8"):
        self._seq += 1
        self.channel_rows.append({
            "id": self._seq, "source": source, "msg_type": None,
            "urgency_score": 8, "decided_channel": "whatsapp",
            "decision_rule": decision_rule, "sent_ok": sent_ok,
            "multichannel_mode": "on", "dedup_key": dedup_key,
            "payload_title": None, "created_at": created_at,
        })

    def rules(self):
        return [r["decision_rule"] for r in self.channel_rows]

    def last_channel_row(self):
        return self.channel_rows[-1] if self.channel_rows else None

    # --- protocolo psycopg2 ----------------------------------------------
    def execute(self, sql, params):
        s = " ".join(sql.split())

        if s.startswith("INSERT INTO channel_decisions"):
            (source, msg_type, score, channel, rule, sent_ok,
             mode, dedup_key, title) = params
            self._seq += 1
            self.channel_rows.append({
                "id": self._seq, "source": source, "msg_type": msg_type,
                "urgency_score": score, "decided_channel": channel,
                "decision_rule": rule, "sent_ok": sent_ok,
                "multichannel_mode": mode, "dedup_key": dedup_key,
                "payload_title": title, "created_at": self.now,
            })
            return []

        if "SELECT COUNT(*) AS c FROM channel_decisions" in s:
            start, end = params
            c = sum(
                1 for r in self.channel_rows
                if r["decided_channel"] == "whatsapp"
                and r["sent_ok"] is True
                and start <= r["created_at"] < end
            )
            return [{"c": c}]

        if s.startswith("SELECT id, source, msg_type, created_at, payload_title"):
            since, source, topic_patterns, _same_source, own_patterns = params
            for r in sorted(self.channel_rows, key=lambda x: x["created_at"], reverse=True):
                if r["decided_channel"] != "whatsapp" or r["sent_ok"] is not True:
                    continue
                if r["created_at"] < since:
                    continue
                dk = r.get("dedup_key") or ""
                if r["source"] != source:
                    if any(_like(dk, p) for p in topic_patterns):
                        return [r]
                elif any(_like(dk, p) for p in own_patterns):
                    return [r]
            return []

        if s.startswith("INSERT INTO pending_notifications"):
            source, msg_type, payload, score, target = params[0], params[1], params[2], params[3], params[4]
            dedup_key = params[5] if len(params) > 5 else None
            if dedup_key and any(
                p["source"] == source and p["dedup_key"] == dedup_key and p["sent_at"] is None
                for p in self.pending_rows
            ):
                return []  # ON CONFLICT DO NOTHING
            self._seq += 1
            self.pending_rows.append({
                "id": self._seq, "source": source, "msg_type": msg_type,
                "payload": payload, "urgency_score": score,
                "digest_target": target, "dedup_key": dedup_key, "sent_at": None,
            })
            return [{"id": self._seq}]

        return []


class _Cursor:
    def __init__(self, db):
        self.db = db
        self._rows = []

    def execute(self, sql, params=None):
        self._rows = self.db.execute(sql, params or ())

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cursor(self.db)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(nr, "get_db", lambda: _Conn(fake))
    return fake


@pytest.fixture
def wired(monkeypatch, db):
    """Multicanal ligado + WA/push instrumentados (nao tocam rede)."""
    monkeypatch.setenv("NOTIFICATION_MULTICHANNEL", "on")
    monkeypatch.delenv("NOTIFICATION_DIGEST_MODE", raising=False)
    monkeypatch.delenv("WA_BUDGET_PIERCE_RULES", raising=False)
    monkeypatch.setenv("SUNDAY_SILENCE_OFF", "1")  # domingo nao interfere
    state = {"wa": [], "push": [], "push_ok": True}

    async def _fake_send_now(text):
        state["wa"].append(text)
        return True

    def _fake_send_push(title, body, urgent=False, tag=None, data=None):
        state["push"].append({"title": title, "urgent": urgent, "tag": tag})
        return state["push_ok"]

    monkeypatch.setattr(nr, "_send_now", _fake_send_now)
    monkeypatch.setattr(nr, "_send_push", _fake_send_push)
    state["db"] = db
    return state


def _route(**kw):
    kw.setdefault("source", "email_triage")
    kw.setdefault("payload", {"title": "t", "body": "b"})
    kw.setdefault("urgency_score", 8)
    return asyncio.run(nr.route_to_renato(**kw))


def _fill_budget(db, n, source="email_triage"):
    """Gasta n WhatsApps dentro do dia BRT corrente."""
    start, _ = nr._brt_day_window_utc()
    for i in range(n):
        db.seed_whatsapp(source, start + timedelta(seconds=i))


# ---------------------------------------------------------------------------
# Kill-switches
# ---------------------------------------------------------------------------
class TestKillSwitches:
    def test_budget_default_shadow(self, monkeypatch):
        monkeypatch.delenv("WA_DAILY_BUDGET_MODE", raising=False)
        assert nr.get_budget_mode() == nr.GATE_SHADOW

    def test_budget_mode_env(self, monkeypatch):
        for raw, expected in ((" on\n", "on"), ("OFF", "off"), ("1", "on"), ("0", "off")):
            monkeypatch.setenv("WA_DAILY_BUDGET_MODE", raw)
            assert nr.get_budget_mode() == expected

    def test_budget_mode_invalido_cai_no_default(self, monkeypatch):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "talvez")
        assert nr.get_budget_mode() == nr.GATE_SHADOW

    def test_budget_default_3(self, monkeypatch):
        monkeypatch.delenv("WA_DAILY_BUDGET", raising=False)
        assert nr.get_daily_budget() == 3

    def test_budget_valor_env(self, monkeypatch):
        monkeypatch.setenv("WA_DAILY_BUDGET", " 5 \n")
        assert nr.get_daily_budget() == 5
        monkeypatch.setenv("WA_DAILY_BUDGET", "zero")
        assert nr.get_daily_budget() == 3
        monkeypatch.setenv("WA_DAILY_BUDGET", "-1")
        assert nr.get_daily_budget() == 3
        monkeypatch.setenv("WA_DAILY_BUDGET", "0")
        assert nr.get_daily_budget() == 0

    def test_cross_dedup_default_on(self, monkeypatch):
        monkeypatch.delenv("NOTIFICATION_CROSS_DEDUP", raising=False)
        assert nr.get_cross_dedup_mode() == nr.GATE_ON

    def test_pierce_rules_default_e_override(self, monkeypatch):
        monkeypatch.delenv("WA_BUDGET_PIERCE_RULES", raising=False)
        assert nr.get_budget_piercing_rules() == nr.BUDGET_PIERCING_RULES_DEFAULT
        monkeypatch.setenv("WA_BUDGET_PIERCE_RULES", "none")
        assert nr.get_budget_piercing_rules() == ()
        monkeypatch.setenv("WA_BUDGET_PIERCE_RULES", "financial_alert, press_detection")
        assert nr.get_budget_piercing_rules() == ("financial_alert", "press_detection")


# ---------------------------------------------------------------------------
# Normalizacao da chave de topico
# ---------------------------------------------------------------------------
class TestTopicKey:
    def test_re_e_acento_colapsam_na_mesma_chave(self):
        a = nr.normalize_topic_key("Re: Regularização contábil das minhas 7 empresas")
        b = nr.normalize_topic_key("Regularização contábil das minhas 7 empresas")
        assert a == b == "regularizacao-contabil-das-minhas-7-empresas"

    def test_prefixos_encadeados(self):
        assert nr.normalize_topic_key("Enc: Re: Fwd: Contrato Aptus revisado") == \
            nr.normalize_topic_key("Contrato Aptus revisado")

    def test_re_com_contador(self):
        assert nr.normalize_topic_key("Re[2]: Contrato Aptus revisado") == \
            nr.normalize_topic_key("Contrato Aptus revisado")

    def test_sobrevive_a_truncagem_diferente_entre_produtores(self):
        """email_triage:2406 corta o assunto em 70; :4559 corta em 60. A chave
        precisa ser um prefixo que caiba nos dois cortes."""
        longo = ("Regularização contábil e societária das minhas 7 empresas "
                 "inativas com o escritório novo")
        assert nr.normalize_topic_key(longo[:70]) == nr.normalize_topic_key(longo[:60])
        assert nr.normalize_topic_key(longo[:60]) == nr.normalize_topic_key(longo)

    def test_assunto_curto_vira_none(self):
        assert nr.normalize_topic_key("ok") is None
        assert nr.normalize_topic_key("   ") is None
        assert nr.normalize_topic_key(None) is None

    def test_collect_de_payload_e_de_arg(self):
        keys = nr.collect_topic_keys(
            {"subject": "Re: Regularização contábil das minhas 7 empresas",
             "thread_id": "18f0aabbcc"},
            topic_key="Regularizacao contabil das minhas 7 empresas",
        )
        assert keys[0] == "regularizacao-contabil-das-minhas-7-empresas"
        assert "thread-18f0aabbcc" in keys
        assert len(keys) == 2  # dedup interno

    def test_collect_vazio_quando_nao_ha_chave(self):
        assert nr.collect_topic_keys({"title": "x", "body": "y"}) == []

    def test_encode_preserva_dedup_do_produtor(self):
        enc = nr.encode_dedup_key("inbox_smart:2026072712", ["abcdefghij"])
        assert enc.startswith("inbox_smart:2026072712 ")
        assert "#topic:abcdefghij#" in enc
        assert nr.encode_dedup_key("x", []) == "x"


# ---------------------------------------------------------------------------
# (1) Teto diario
# ---------------------------------------------------------------------------
class TestOrcamentoDiario:
    def test_dentro_do_teto_manda_whatsapp(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "3")
        _fill_budget(db, 2)
        r = _route()
        assert r["channel"] == "whatsapp"
        assert len(wired["wa"]) == 1

    def test_teto_estourado_desce_pra_push(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "3")
        _fill_budget(db, 3)
        r = _route()
        assert r["channel"] == "push"
        assert r["decision_rule"] == nr.RULE_BUDGET_DOWNGRADED
        assert r["downgraded_from"] == "whatsapp"
        assert wired["wa"] == []                    # nao interrompeu
        assert len(wired["push"]) == 1
        assert wired["push"][0]["urgent"] is True   # desceu de canal, nao de importancia

    def test_rebaixamento_fica_registrado_em_channel_decisions(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "1")
        _fill_budget(db, 1)
        _route()
        row = db.last_channel_row()
        assert row["decision_rule"] == nr.RULE_BUDGET_DOWNGRADED
        assert row["decided_channel"] == "push"

    def test_push_indisponivel_escala_de_volta_pro_whatsapp(self, monkeypatch, wired, db):
        """A escada do rebaixado NAO termina em pill: pending e dead-letter
        (unico dreno, consume_pending_for_digest, so roda nos crons de briefing
        — comentados em workers/audio-transcriber/main.py:176-177). Sem push,
        entregar demais > perder."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "1")
        _fill_budget(db, 1)
        wired["push_ok"] = False
        r = _route(dedup_key="orestes-1")
        assert r["channel"] == "whatsapp"
        assert r["decision_rule"] == nr.RULE_DOWNGRADE_WA_ESCALATION
        assert len(wired["wa"]) == 1
        assert db.pending_rows == []                 # nao foi pra dead-letter
        # o rebaixamento continua registrado, mesmo tendo sido revertido
        assert nr.RULE_BUDGET_DOWNGRADED in db.rules()

    def test_ultimo_recurso_quando_push_e_wa_falham(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "1")
        _fill_budget(db, 1)
        wired["push_ok"] = False

        async def _wa_down(text):
            return False

        monkeypatch.setattr(nr, "_send_now", _wa_down)
        r = _route(dedup_key="orestes-1")
        assert r["channel"] == "pill"
        assert r["decision_rule"] == nr.RULE_DOWNGRADE_ALL_FAILED
        assert r["pending_id"] is not None
        assert len(db.pending_rows) == 1
        # os 3 degraus da escada ficam no ledger
        assert nr.RULE_BUDGET_DOWNGRADED in db.rules()
        assert nr.RULE_DOWNGRADE_WA_ESCALATION in db.rules()
        assert nr.RULE_DOWNGRADE_ALL_FAILED in db.rules()

    def test_shadow_manda_whatsapp_mas_deixa_rastro(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "shadow")
        monkeypatch.setenv("WA_DAILY_BUDGET", "1")
        _fill_budget(db, 1)
        r = _route()
        assert r["channel"] == "whatsapp"
        assert len(wired["wa"]) == 1
        assert r["decision_rule"] == nr.RULE_BUDGET_SHADOW

    def test_off_e_byte_a_byte_o_legado(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        monkeypatch.setenv("WA_DAILY_BUDGET", "0")
        _fill_budget(db, 9)
        r = _route()
        assert r["channel"] == "whatsapp"
        assert r["decision_rule"] == "score_ge_8"

    def test_falha_de_db_e_fail_open(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "0")

        def _boom(now=None):
            raise RuntimeError("neon caiu")

        monkeypatch.setattr(nr, "wa_sent_today", _boom)
        r = _route()
        assert r["channel"] == "whatsapp"  # na duvida, interrompe (nunca engole)

    def test_wa_falho_nao_consome_orcamento(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "2")
        start, _ = nr._brt_day_window_utc()
        db.seed_whatsapp("email_triage", start, sent_ok=True)
        db.seed_whatsapp("email_triage", start, sent_ok=False)  # nao interrompeu
        r = _route()
        assert r["channel"] == "whatsapp"

    def test_pill_e_push_normais_nao_passam_pelo_teto(self, monkeypatch, wired, db):
        """Score baixo continua indo pra pill com a rule de sempre."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "0")
        r = _route(source="task_reconciler", urgency_score=3)
        assert r["channel"] == "pill"
        assert r["decision_rule"] == "score_lt_5"


# ---------------------------------------------------------------------------
# Escape hatch — quem fura o teto
# ---------------------------------------------------------------------------
class TestEscapeHatch:
    def _exhaust(self, monkeypatch, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "0")

    def test_reuniao_em_20min_fura(self, monkeypatch, wired, db):
        self._exhaust(monkeypatch, db)
        r = _route(source="calendar", urgency_score=8,
                   payload={"title": "Reuniao", "body": "b", "minutes_until": 20})
        assert r["channel"] == "whatsapp"
        assert len(wired["wa"]) == 1

    def test_imprensa_fura(self, monkeypatch, wired, db):
        self._exhaust(monkeypatch, db)
        r = _route(source="email_triage", urgency_score=8,
                   payload={"title": "x", "body": "b", "email_from": "j@folha.uol.com.br"})
        assert r["channel"] == "whatsapp"

    def test_cron_error_prod_fura(self, monkeypatch, wired, db):
        self._exhaust(monkeypatch, db)
        r = _route(source="cron_health", urgency_score=8,
                   payload={"title": "x", "body": "b", "severity": "critical"})
        assert r["channel"] == "whatsapp"

    def test_score_ge_8_auto_declarado_NAO_fura(self, monkeypatch, wired, db):
        """O caso dos 17 email_triage + 9 inbox_smart + 3 inbox_triage."""
        self._exhaust(monkeypatch, db)
        for src in ("email_triage", "inbox_smart", "inbox_triage"):
            r = _route(source=src, urgency_score=8)
            assert r["channel"] == "push", src
            assert r["decision_rule"] == nr.RULE_BUDGET_DOWNGRADED

    def test_alerta_de_custo_NAO_fura(self, monkeypatch, wired, db):
        """US$3,88 nao vale a mesma interrupcao que um e-mail do pai.
        financial_alert casa em source=='cost_tracker' (identidade de produtor),
        e force_immediate e auto-declarado — nenhum dos dois fura por default."""
        self._exhaust(monkeypatch, db)
        r = _route(source="cost_tracker", msg_type="budget_threshold_hit",
                   urgency_score=10,
                   payload={"title": "Budget", "body": "b", "force_immediate": True})
        assert r["channel"] == "push"
        assert r["decision_rule"] == nr.RULE_BUDGET_DOWNGRADED

    def test_env_reabre_o_furo_sem_deploy(self, monkeypatch, wired, db):
        self._exhaust(monkeypatch, db)
        monkeypatch.setenv("WA_BUDGET_PIERCE_RULES", "financial_alert,caller_force_immediate")
        r = _route(source="cost_tracker", msg_type="budget_threshold_hit",
                   urgency_score=10, payload={"title": "Budget", "body": "b"})
        assert r["channel"] == "whatsapp"

    def test_pierce_reavalia_regra_mesmo_com_score_8(self, monkeypatch, wired, db):
        """is_urgent() curto-circuita em score>=8 e devolve 'score_ge_8'; o
        escape hatch precisa olhar o FATO, nao a etiqueta que sobrou."""
        assert nr.is_urgent({"minutes_until": 10}, 8, "calendar", None)[1] == "score_ge_8"
        assert nr._budget_pierce_rule("calendar", None, {"minutes_until": 10}, 8) == \
            "meeting_soon_unconfirmed"


# ---------------------------------------------------------------------------
# (2) Dedup cross-produtor — caso Orestes
# ---------------------------------------------------------------------------
ORESTES = "Re: Regularização contábil das minhas 7 empresas"
ORESTES_SEM_RE = "Regularização contábil das minhas 7 empresas"


class TestDedupCrossProdutor:
    def _seed_email_triage_wa(self, db, minutos_atras=40, source="email_triage"):
        key = nr.encode_dedup_key(
            f"{source}:x", nr.collect_topic_keys(None, topic_key=ORESTES)
        )
        db.seed_whatsapp(
            source,
            now_utc().replace(tzinfo=None) - timedelta(minutes=minutos_atras),
            dedup_key=key,
        )

    def test_segundo_produtor_desce_de_canal(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        monkeypatch.setenv("NOTIFICATION_CROSS_DEDUP", "on")
        self._seed_email_triage_wa(db)
        # inbox_smart manda o resumo com N assuntos, um deles o do Orestes
        r = _route(
            source="inbox_smart", urgency_score=8,
            dedup_key="inbox_smart:2026072712",
            payload={"title": "Triagem inteligente", "body": "..."},
            topic_keys=["Extrato de julho", ORESTES_SEM_RE],
        )
        assert r["channel"] == "push"
        assert r["decision_rule"] == nr.RULE_CROSS_DEDUP_DOWNGRADED
        assert wired["wa"] == []

    def test_mesmo_produtor_sem_dedup_key_passa(self, monkeypatch, wired, db):
        """O casamento por TOPICO e cross-produtor: o mesmo produtor repetindo
        o assunto, sem chave, nao e barrado por essa metade da regra."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        self._seed_email_triage_wa(db)
        r = _route(source="email_triage", urgency_score=8, topic_key=ORESTES)
        assert r["channel"] == "whatsapp"

    def test_mesma_dedup_key_no_branch_whatsapp_desce(self, monkeypatch, wired, db):
        """Buraco fechado: ate agora o branch whatsapp nao consultava dedup_key
        NENHUMA (so push e pill consultavam, via ON CONFLICT do enqueue)."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        db.seed_whatsapp("email_triage", now_utc().replace(tzinfo=None),
                         dedup_key="email_urgent:18f0aa")
        r = _route(source="email_triage", urgency_score=8,
                   dedup_key="email_urgent:18f0aa")
        assert r["channel"] == "push"
        assert r["decision_rule"] == nr.RULE_SAME_KEY_DOWNGRADED

    def test_mesma_dedup_key_casa_com_marcadores_de_topico(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        self._seed_email_triage_wa(db)  # dedup_key = "email_triage:x #topic:...#"
        r = _route(source="email_triage", urgency_score=8, dedup_key="email_triage:x")
        assert r["decision_rule"] == nr.RULE_SAME_KEY_DOWNGRADED

    def test_dedup_key_diferente_passa(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        db.seed_whatsapp("email_triage", now_utc().replace(tzinfo=None),
                         dedup_key="email_urgent:18f0aa")
        r = _route(source="email_triage", urgency_score=8,
                   dedup_key="email_urgent:18f0bb")
        assert r["channel"] == "whatsapp"

    def test_underscore_da_chave_nao_vira_curinga(self, monkeypatch, wired, db):
        """'_' e curinga em LIKE — a chave crua tem que ser escapada."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        db.seed_whatsapp("email_triage", now_utc().replace(tzinfo=None),
                         dedup_key="email_urgent:abc")
        r = _route(source="email_triage", urgency_score=8,
                   dedup_key="emailXurgent:abc")
        assert r["channel"] == "whatsapp"

    def test_fora_da_janela_nao_dedupa(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        monkeypatch.setenv("NOTIFICATION_CROSS_DEDUP_WINDOW_H", "6")
        self._seed_email_triage_wa(db, minutos_atras=7 * 60)
        r = _route(source="inbox_smart", urgency_score=8, topic_key=ORESTES_SEM_RE)
        assert r["channel"] == "whatsapp"

    def test_wa_anterior_falho_nao_dedupa(self, monkeypatch, wired, db):
        """Se o primeiro WhatsApp falhou, o Renato nao viu — o segundo passa."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        key = nr.encode_dedup_key("email_triage:x",
                                  nr.collect_topic_keys(None, topic_key=ORESTES))
        db.seed_whatsapp("email_triage", now_utc().replace(tzinfo=None),
                         dedup_key=key, sent_ok=False)
        r = _route(source="inbox_smart", urgency_score=8, topic_key=ORESTES_SEM_RE)
        assert r["channel"] == "whatsapp"

    def test_assunto_diferente_nao_dedupa(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        self._seed_email_triage_wa(db)
        r = _route(source="inbox_smart", urgency_score=8,
                   topic_key="Contrato Aptus revisado")
        assert r["channel"] == "whatsapp"

    def test_sem_topic_key_fica_inerte(self, monkeypatch, wired, db):
        """Retrocompatibilidade: produtor que nao passa chave nao muda de vida."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        self._seed_email_triage_wa(db)
        r = _route(source="inbox_smart", urgency_score=8)
        assert r["channel"] == "whatsapp"

    def test_kill_switch_off(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        monkeypatch.setenv("NOTIFICATION_CROSS_DEDUP", "off")
        self._seed_email_triage_wa(db)
        r = _route(source="inbox_smart", urgency_score=8, topic_key=ORESTES_SEM_RE)
        assert r["channel"] == "whatsapp"

    def test_shadow_deixa_rastro_sem_mudar_canal(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        monkeypatch.setenv("NOTIFICATION_CROSS_DEDUP", "shadow")
        self._seed_email_triage_wa(db)
        r = _route(source="inbox_smart", urgency_score=8, topic_key=ORESTES_SEM_RE)
        assert r["channel"] == "whatsapp"
        assert r["decision_rule"] == nr.RULE_CROSS_DEDUP_SHADOW

    def test_dedup_vale_ate_pra_regra_deterministica(self, monkeypatch, wired, db):
        """Se a MESMA coisa ja foi entregue por WA na janela, repetir nao
        acrescenta informacao — nem pra imprensa."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        self._seed_email_triage_wa(db)
        r = _route(source="inbox_smart", urgency_score=8, topic_key=ORESTES_SEM_RE,
                   payload={"title": "x", "body": "b", "email_from": "j@folha.uol.com.br"})
        assert r["channel"] == "push"
        assert r["decision_rule"] == nr.RULE_CROSS_DEDUP_DOWNGRADED

    def test_topico_prefixo_nao_casa_por_engano(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        key = nr.encode_dedup_key(None, nr.collect_topic_keys(None, topic_key="Contrato Aptus"))
        db.seed_whatsapp("email_triage", now_utc().replace(tzinfo=None), dedup_key=key)
        r = _route(source="inbox_smart", urgency_score=8,
                   topic_key="Contrato Aptus revisado hoje")
        assert r["channel"] == "whatsapp"

    def test_marcadores_ficam_no_log_pro_proximo_produtor(self, monkeypatch, wired, db):
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
        _route(source="email_triage", urgency_score=8,
               dedup_key="email_triage:abc", topic_key=ORESTES)
        row = db.last_channel_row()
        assert row["dedup_key"].startswith("email_triage:abc ")
        assert "#topic:regularizacao-contabil-das-minhas-7-empresas#" in row["dedup_key"]


# ---------------------------------------------------------------------------
# Virada de dia BRT
# ---------------------------------------------------------------------------
class TestViradaDiaBRT:
    def test_janela_e_do_dia_brt(self):
        # 27/07 02:30 UTC == 26/07 23:30 BRT -> dia BRT ainda e 26
        start, end = nr._brt_day_window_utc(datetime(2026, 7, 27, 2, 30))
        assert start == datetime(2026, 7, 26, 3, 0)
        assert end == datetime(2026, 7, 27, 3, 0)

        # 27/07 03:30 UTC == 27/07 00:30 BRT -> virou
        start, end = nr._brt_day_window_utc(datetime(2026, 7, 27, 3, 30))
        assert start == datetime(2026, 7, 27, 3, 0)
        assert end == datetime(2026, 7, 28, 3, 0)

    def test_contagem_zera_na_virada_brt(self, db):
        # 3 WhatsApps as 23h BRT do dia 26 (= 02:00 UTC do dia 27)
        for _ in range(3):
            db.seed_whatsapp("email_triage", datetime(2026, 7, 27, 2, 0))
        # ainda 26/07 em BRT: o orcamento esta gasto
        assert nr.wa_sent_today(datetime(2026, 7, 27, 2, 30)) == 3
        # 00:30 BRT do dia 27: dia novo, orcamento zerado
        assert nr.wa_sent_today(datetime(2026, 7, 27, 3, 30)) == 0

    def test_dia_utc_daria_a_resposta_errada(self, db):
        """Guarda anti-regressao: contar por dia UTC deixaria o Renato sem
        orcamento das 21h as 24h BRT (as msgs das 21h-24h do dia anterior
        ainda estariam no mesmo dia UTC)."""
        db.seed_whatsapp("email_triage", datetime(2026, 7, 27, 1, 0))  # 22h BRT do dia 26
        assert nr.wa_sent_today(datetime(2026, 7, 27, 5, 0)) == 0      # 02h BRT do dia 27

    def test_teto_por_dia_brt_no_dispatch(self, monkeypatch, wired, db):
        """Fim de tarde: teto gasto. Passada a meia-noite BRT, libera."""
        monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "on")
        monkeypatch.setenv("WA_DAILY_BUDGET", "1")
        ontem_brt = datetime(2026, 7, 27, 2, 0)   # 23h BRT do dia 26
        db.seed_whatsapp("email_triage", ontem_brt)

        real_window = nr._brt_day_window_utc
        monkeypatch.setattr(
            nr, "_brt_day_window_utc",
            lambda now=None: real_window(datetime(2026, 7, 27, 2, 30)),
        )
        assert _route()["decision_rule"] == nr.RULE_BUDGET_DOWNGRADED

        monkeypatch.setattr(
            nr, "_brt_day_window_utc",
            lambda now=None: real_window(datetime(2026, 7, 27, 3, 30)),
        )
        assert _route()["channel"] == "whatsapp"
