"""Janela de avaliacao proporcional do `monitor-cron-health`.

Contexto (27/07/26): 3a ocorrencia da mesma classe de falso-positivo. Nos 14
dias anteriores o Renato recebeu 15 mensagens "[INTEL] Cron health alert", a
maioria delas:

    - platform-costs-snapshot: nunca disparou (esperado 44640min)

44640min = 31 dias — job MENSAL, que rodou no slot certo em 02/07 e estava
saudavel. O fix anterior (047d30b) somou `cron_runs` como 2a fonte de
evidencia, mas manteve a regra "sem evidencia agora => alerta agora": janela
efetiva ZERO, identica pra um job de 5min e pra um job mensal.

A regra coberta aqui: ausencia de evidencia so vira veredito depois de
decorrido tempo PROPORCIONAL ao intervalo declarado no cron_registry.
"""
from datetime import datetime, timedelta, timezone

from main import _cron_job_verdict, _cron_never_fired_window_min

UTC = timezone.utc
NOW = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)

MENSAL = 44640      # platform-costs-snapshot
DIARIO = 1440       # cos-daily-review
CINCO_MIN = 5       # process-scheduled-actions


def ago(**kw):
    return NOW - timedelta(**kw)


class TestJanelaProporcional:
    def test_janela_cresce_com_o_intervalo(self):
        assert _cron_never_fired_window_min(CINCO_MIN) < _cron_never_fired_window_min(DIARIO)
        assert _cron_never_fired_window_min(DIARIO) < _cron_never_fired_window_min(MENSAL)

    def test_janela_mensal_passa_de_31_dias(self):
        """O ponto do bug: 31 dias tem que caber DENTRO da janela, com folga."""
        assert _cron_never_fired_window_min(MENSAL) > MENSAL
        assert 35 < _cron_never_fired_window_min(MENSAL) / 1440 < 45

    def test_janela_curta_tem_piso_de_uma_hora(self):
        """Job de 5min nao pode alertar em 6min — o monitor roda 1x/h."""
        assert _cron_never_fired_window_min(CINCO_MIN) >= 60

    def test_intervalo_invalido_nao_explode(self):
        assert _cron_never_fired_window_min(0) >= 60
        assert _cron_never_fired_window_min(None) >= 60


class TestJobMensal:
    def test_mensal_saudavel_nao_alerta(self):
        """O caso real: rodou 02/07, hoje 27/07 (gap 25d). Job mensal saudavel."""
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=MENSAL,
            last_seen=datetime(2026, 7, 2, 12, 58, tzinfo=UTC),
        )
        assert v["status"] == "ok"

    def test_mensal_sem_evidencia_recem_observado_nao_alerta(self):
        """platform-costs-snapshot antes de 047d30b: sem heartbeat porque o
        worker so assumiu os crons em 13/07. 14 dias de observacao nao provam
        nada sobre um job de 31 dias."""
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=MENSAL,
            last_seen=None, observed_since=ago(days=14),
        )
        assert v["status"] == "pending_first_run"

    def test_mensal_de_fato_morto_alerta(self):
        """Falso-negativo oposto: parou de rodar de verdade. Tem que alertar."""
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=MENSAL, last_seen=ago(days=70),
        )
        assert v["status"] == "stale"

    def test_mensal_nunca_disparou_depois_da_janela_alerta(self):
        """Sem NENHUMA evidencia depois de 60 dias observando um job de 31
        dias: nao ha leitura benigna. Alerta."""
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=MENSAL,
            last_seen=None, observed_since=ago(days=60),
        )
        assert v["status"] == "never_fired"


class TestJobsCurtos:
    def test_5min_atrasado_alerta(self):
        v = _cron_job_verdict(now=NOW, expected_interval_min=CINCO_MIN, last_seen=ago(minutes=30))
        assert v["status"] == "stale"
        assert v["gap_min"] == 30.0

    def test_5min_no_horario_nao_alerta(self):
        v = _cron_job_verdict(now=NOW, expected_interval_min=CINCO_MIN, last_seen=ago(minutes=4))
        assert v["status"] == "ok"

    def test_5min_sem_evidencia_alerta_em_horas_nao_em_dias(self):
        """A janela proporcional nao pode virar mordaca pros jobs rapidos."""
        assert _cron_job_verdict(
            now=NOW, expected_interval_min=CINCO_MIN,
            last_seen=None, observed_since=ago(minutes=30),
        )["status"] == "pending_first_run"
        assert _cron_job_verdict(
            now=NOW, expected_interval_min=CINCO_MIN,
            last_seen=None, observed_since=ago(hours=3),
        )["status"] == "never_fired"

    def test_diario_atrasado_alerta(self):
        v = _cron_job_verdict(now=NOW, expected_interval_min=DIARIO, last_seen=ago(days=3))
        assert v["status"] == "stale"


class TestJobRecemRegistrado:
    """24/07/26: cos-daily-review e cos-signal-router foram alertados como
    'nunca disparou' na PRIMEIRA passada do monitor — antes do 1o slot deles."""

    def test_recem_registrado_nao_alerta(self):
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=DIARIO,
            last_seen=None, observed_since=ago(minutes=10),
        )
        assert v["status"] == "pending_first_run"
        assert v["waiting_min"] == 10.0

    def test_primeiro_avistamento_nao_alerta(self):
        """observed_since=None = o monitor esta vendo o job agora, pela 1a vez."""
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=DIARIO, last_seen=None, observed_since=None,
        )
        assert v["status"] == "pending_first_run"

    def test_recem_registrado_alerta_quando_a_janela_vence(self):
        """Nascer novo compra prazo, nao imunidade."""
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=DIARIO,
            last_seen=None, observed_since=ago(days=3),
        )
        assert v["status"] == "never_fired"


class TestKillSwitchETz:
    def test_kill_switch_restaura_comportamento_antigo(self):
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=MENSAL,
            last_seen=None, observed_since=ago(minutes=10), proportional=False,
        )
        assert v["status"] == "never_fired"

    def test_kill_switch_nao_afeta_quem_tem_evidencia(self):
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=MENSAL,
            last_seen=ago(days=25), proportional=False,
        )
        assert v["status"] == "ok"

    def test_timestamp_naive_tratado_como_utc(self):
        """cron_runs.started_at e TIMESTAMP sem tz — UTC por convencao."""
        naive = datetime(2026, 7, 27, 20, 30)   # 30min antes de NOW, em UTC
        v = _cron_job_verdict(now=NOW, expected_interval_min=60, last_seen=naive)
        assert v["gap_min"] == 30.0
        assert v["status"] == "ok"

    def test_observed_since_no_futuro_nao_vira_negativo(self):
        v = _cron_job_verdict(
            now=NOW, expected_interval_min=DIARIO,
            last_seen=None, observed_since=NOW + timedelta(hours=2),
        )
        assert v["waiting_min"] == 0.0
        assert v["status"] == "pending_first_run"
