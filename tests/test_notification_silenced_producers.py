"""
Testes do SILENCIO POR PRODUTOR no notification_router (decisao do Renato 29/07/2026).

Contexto medido em prod (channel_decisions, 14 dias): todo produtor automatico
declara urgency 8 e por isso ganha WhatsApp — email_triage 15x, inbox_smart 9x,
platform_costs 8x, inbox_triage 3x, raci_smart_update 2x, group_digest 2x, TODOS
com decision_rule='score_ge_8'. Baseline do canal: 127 mensagens -> 5 respostas
(~1,6% de acao). O teto diario e o dedup graduam por EVENTO; este gate gradua por
IDENTIDADE: maquina nao interrompe por WhatsApp.

O invariante que estes testes protegem: o item silenciado NAO se perde. Ele entra
em `pending_notifications` (ledger lido pela tela de silenciadas, que desde 27/07
mostra nao-entregues por ate 30 dias) e recebe um toque de push por cima. E, ao
contrario do item rebaixado pelo teto, ele NAO volta pro WhatsApp quando o push
falha — era essa escalacao que anulava o teto no evento de 28/07 (o push falhou 1
de 4 vezes na janela medida; as 2 inscricoes sao de 15/07 e nao renovam).

Rodar:
  .venv/bin/python -m pytest tests/test_notification_silenced_producers.py -q
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import notification_router as nr  # noqa: E402

# Reusa o fake de DB do arquivo do teto: mesma tabela, mesma semantica de LIKE.
from tests.test_notification_budget_dedup import FakeDB, _Conn  # noqa: E402


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(nr, "get_db", lambda: _Conn(fake))
    return fake


@pytest.fixture
def wired(monkeypatch, db):
    """Multicanal ligado, silencio no default de producao, rede mockada."""
    monkeypatch.setenv("NOTIFICATION_MULTICHANNEL", "on")
    monkeypatch.delenv("NOTIFICATION_DIGEST_MODE", raising=False)
    monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
    monkeypatch.delenv("WA_BUDGET_PIERCE_RULES", raising=False)
    monkeypatch.setenv("SUNDAY_SILENCE_OFF", "1")
    # Teto fora do caminho: quero medir o silencio, nao o teto.
    monkeypatch.setenv("WA_DAILY_BUDGET_MODE", "off")
    monkeypatch.setenv("NOTIFICATION_CROSS_DEDUP", "off")
    state = {"wa": [], "push": [], "push_ok": True, "db": db}

    async def _fake_send_now(text):
        state["wa"].append(text)
        return True

    def _fake_send_push(title, body, urgent=False, tag=None, data=None):
        state["push"].append({"title": title, "urgent": urgent, "tag": tag})
        return state["push_ok"]

    monkeypatch.setattr(nr, "_send_now", _fake_send_now)
    monkeypatch.setattr(nr, "_send_push", _fake_send_push)
    return state


def _route(**kw):
    kw.setdefault("payload", {"title": "t", "body": "b"})
    kw.setdefault("urgency_score", 8)
    return asyncio.run(nr.route_to_renato(**kw))


# ---------------------------------------------------------------------------
# A lista e o kill-switch
# ---------------------------------------------------------------------------
class TestListaEKillSwitch:
    def test_default_tem_os_produtores_medidos(self, monkeypatch):
        monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
        lista = nr.get_silenced_producers()
        # os 6 que apareceram mandando WhatsApp na janela de 14 dias
        for esperado in ("email_triage", "inbox_smart", "platform_costs",
                         "inbox_triage", "raci_smart_update", "group_digest"):
            assert esperado in lista, esperado

    def test_none_desliga_o_gate(self, monkeypatch):
        monkeypatch.setenv("WA_SILENCED_PRODUCERS", "none")
        assert nr.get_silenced_producers() == ()
        assert not nr.is_silenced_producer("email_triage")

    def test_csv_troca_a_lista(self, monkeypatch):
        monkeypatch.setenv("WA_SILENCED_PRODUCERS", "foo, bar")
        assert nr.get_silenced_producers() == ("foo", "bar")
        assert nr.is_silenced_producer("foo")
        assert not nr.is_silenced_producer("email_triage")

    def test_source_vazio_nao_e_silenciado(self):
        assert not nr.is_silenced_producer(None)
        assert not nr.is_silenced_producer("")


# ---------------------------------------------------------------------------
# decide_channel
# ---------------------------------------------------------------------------
class TestDecideChannel:
    def test_produtor_silenciado_desce_pra_push(self, monkeypatch):
        monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
        ch, rule = nr.decide_channel({"title": "t"}, 8, "platform_costs", None)
        assert ch == "push"
        assert rule == "producer_silenced:score_ge_8"

    def test_produtor_nao_listado_segue_indo_pro_whatsapp(self, monkeypatch):
        monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
        ch, rule = nr.decide_channel({"title": "t"}, 8, "agent_intent", None)
        assert (ch, rule) == ("whatsapp", "score_ge_8")

    def test_score_baixo_nao_e_afetado(self, monkeypatch):
        """O gate so age sobre quem SERIA WhatsApp; nao promove nem rebaixa o resto."""
        monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
        assert nr.decide_channel({"title": "t"}, 6, "email_triage", None) == ("push", "score_5_7")
        assert nr.decide_channel({"title": "t"}, 2, "email_triage", None) == ("pill", "score_lt_5")

    def test_env_none_religa_o_whatsapp(self, monkeypatch):
        monkeypatch.setenv("WA_SILENCED_PRODUCERS", "none")
        assert nr.decide_channel({"title": "t"}, 8, "email_triage", None) == ("whatsapp", "score_ge_8")


# ---------------------------------------------------------------------------
# O que AINDA fura o silencio
# ---------------------------------------------------------------------------
class TestFuroDeterministico:
    def test_cron_critico_fura_apesar_do_produtor_silenciado(self, monkeypatch):
        """cron_health esta na lista, mas severity critical e FATO, nao auto-declaracao.

        Guarda a armadilha que me pegou escrevendo isto: is_urgent() curto-circuita
        em 'score_ge_8' antes do loop de URGENCY_RULES, entao comparar a etiqueta
        nunca acharia 'cron_error_prod'. Quem decide o furo e _budget_pierce_rule.
        """
        monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
        ch, rule = nr.decide_channel(
            {"title": "x", "body": "b", "severity": "critical"}, 8, "cron_health", None,
        )
        assert ch == "whatsapp"
        assert rule == "cron_error_prod"

    def test_reuniao_em_menos_de_30min_fura(self, monkeypatch):
        monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
        ch, rule = nr.decide_channel(
            {"title": "x", "minutes_until": 10, "confirmed": False}, 8, "email_triage", None,
        )
        assert ch == "whatsapp"
        assert rule == "meeting_soon_unconfirmed"

    def test_force_immediate_auto_declarado_NAO_fura(self, monkeypatch):
        """Mesma classe do score>=8: e o produtor afirmando, nao um fato verificado."""
        monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
        ch, rule = nr.decide_channel(
            {"title": "x", "force_immediate": True}, 8, "cost_tracker", None,
        )
        assert ch == "push"
        assert rule.startswith("producer_silenced:")

    def test_pierce_rules_off_silencia_ate_o_cron_critico(self, monkeypatch):
        monkeypatch.delenv("WA_SILENCED_PRODUCERS", raising=False)
        monkeypatch.setenv("WA_BUDGET_PIERCE_RULES", "none")
        ch, _ = nr.decide_channel(
            {"title": "x", "severity": "critical"}, 8, "cron_health", None,
        )
        assert ch == "push"


# ---------------------------------------------------------------------------
# Ponta a ponta: nada se perde, e nada volta pro WhatsApp
# ---------------------------------------------------------------------------
class TestDispatch:
    def test_silenciado_nao_manda_whatsapp_e_fica_no_ledger(self, wired, db):
        r = _route(source="inbox_smart", dedup_key="inbox_smart:20260729")
        assert wired["wa"] == [], "produtor silenciado nao pode mandar WhatsApp"
        assert len(wired["push"]) == 1, "mas recebe o toque de push"
        assert r["channel"] == "push"
        assert r["pending_id"] is not None, "e fica no ledger da tela de silenciadas"
        assert len(db.pending_rows) == 1

    def test_push_falho_cai_em_pill_e_NAO_volta_pro_whatsapp(self, wired, db):
        """O ponto central da frente.

        Um item rebaixado pelo TETO escala de volta pro WhatsApp quando o push
        falha ('entregar demais > perder'). O silenciado nao: o objetivo dele e o
        silencio, e o ledger ja garante que ele aparece na tela.
        """
        wired["push_ok"] = False
        r = _route(source="platform_costs", dedup_key="cost:20260729")
        assert wired["wa"] == [], "push falho NAO pode reabrir o WhatsApp"
        assert r["channel"] == "pill"
        assert r["decision_rule"] == "push_fallback_pill"
        assert r["pending_id"] is not None
        assert len(db.pending_rows) == 1

    def test_rebaixamento_fica_gravado_em_channel_decisions(self, wired, db):
        _route(source="group_digest", dedup_key="digest:20260729")
        rows = [r for r in db.channel_rows if r.get("source") == "group_digest"]
        assert rows, "toda decisao de canal deixa rastro"
        assert rows[-1]["decided_channel"] == "push"
        assert rows[-1]["decision_rule"].startswith("producer_silenced:")

    def test_nao_silenciado_continua_chegando_no_whatsapp(self, wired):
        r = _route(source="agent_intent")
        assert len(wired["wa"]) == 1
        assert r["channel"] == "whatsapp"

    def test_cron_critico_chega_no_whatsapp_ponta_a_ponta(self, wired):
        r = _route(source="cron_health",
                   payload={"title": "x", "body": "b", "severity": "error"})
        assert len(wired["wa"]) == 1, "o furo deterministico atravessa o dispatch"
        assert r["channel"] == "whatsapp"

    def test_dedup_do_ledger_nao_repete_o_toque(self, wired, db):
        """Dois runs do mesmo produtor com a mesma dedup_key: 1 push, 1 pending."""
        _route(source="inbox_smart", dedup_key="inbox_smart:20260729")
        r2 = _route(source="inbox_smart", dedup_key="inbox_smart:20260729")
        assert r2["action"] == "duplicate"
        assert len(wired["push"]) == 1
        assert len(db.pending_rows) == 1
