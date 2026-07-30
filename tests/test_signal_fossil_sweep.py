"""
Varredura dos signals fosseis (30/07/26).

O buraco: signal de detector expira por AUSENCIA na run seguinte
(`expire_stale_signals`) — mecanismo que so roda quando o detector RODA.
Desligar os 9 detectores em 20/07 (decisao do Renato) nao expirou nada:
CONGELOU. Dez dias depois havia **110 signals 'open'**, o mais novo de 20/07,
e o `get_cockpit` (CoPiloto, skills) os servia como pendencia de hoje. A CoS
gastou a manha de 30/07 resolvendo fossil a mao — tio Antonio #89795 preso a
um contato ja mergeado, 2 RACI Vallen concluidos desde 05/07.

Dois sweeps fecham:
  A) `expire_disabled_detector_signals` — detector conhecido porem desligado.
     81 signals.
  B) `EPHEMERAL_SIGNAL_TTL_HOURS` ganhou morning/evening_briefing: digest e
     RETRATO do dia, nascer 'open' e ficar assim e defeito. 28 signals.

A propriedade que importa: (A) DERIVA da lista de detectores ligados. Religar
um detector faz seus signals pararem de ser varridos sem editar o sweep — e o
sweep nunca alcanca signal que nao vem de detector.

Rodar: PYTHONPATH=app python -m pytest tests/test_signal_fossil_sweep.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.detectors._base import (  # noqa: E402
    EPHEMERAL_SIGNAL_TTL_HOURS, KNOWN_DETECTORS,
    expire_disabled_detector_signals,
)


class _Cur:
    def __init__(self):
        self.sql = None
        self.params = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        self.rowcount = len(params[0]) if params else 0


class _Conn:
    def __init__(self):
        self._cur = _Cur()

    def cursor(self):
        return self._cur


class TestSweepDeDetectorDesligado:
    def test_com_todos_desligados_varre_os_nove(self):
        conn = _Conn()
        expire_disabled_detector_signals(conn, enabled=[])
        alvo = conn._cur.params[0]
        assert sorted(alvo) == sorted(KNOWN_DETECTORS)

    def test_detector_religado_sai_do_alvo(self):
        """A propriedade central: descomentar um detector em run_all_detectors
        basta — ninguem precisa lembrar de editar o sweep."""
        conn = _Conn()
        expire_disabled_detector_signals(conn, enabled=["detector_conselhos"])
        alvo = conn._cur.params[0]
        assert "detector_conselhos" not in alvo
        assert "detector_relacionamento" in alvo

    def test_todos_ligados_nao_varre_nada(self):
        conn = _Conn()
        n = expire_disabled_detector_signals(conn, enabled=list(KNOWN_DETECTORS))
        assert n == 0
        assert conn._cur.sql is None, "nao pode nem executar UPDATE"

    def test_so_toca_open(self):
        conn = _Conn()
        expire_disabled_detector_signals(conn, enabled=[])
        assert "status = 'open'" in conn._cur.sql

    def test_carimba_o_motivo(self):
        """`resolved_by` distingue este sweep do TTL e do resolve humano —
        sem isso nao da pra auditar quem baixou o que."""
        conn = _Conn()
        expire_disabled_detector_signals(conn, enabled=[])
        assert "resolved_by = 'detector_disabled'" in conn._cur.sql

    def test_nunca_alcanca_signal_que_nao_e_de_detector(self):
        """cos_digest, weekly_digest, pre_meeting_briefing e afins tem TTL
        proprio. O sweep filtra por KNOWN_DETECTORS justamente pra nao os
        engolir por engano."""
        conn = _Conn()
        expire_disabled_detector_signals(conn, enabled=[])
        alvo = set(conn._cur.params[0])
        for nao_detector in ("cos_digest", "weekly_digest", "pre_meeting_briefing",
                             "proactive_post_meeting", "dev_delegation_pickup"):
            assert nao_detector not in alvo


class TestTTLDosDigests:
    def test_briefings_do_cos_tem_ttl(self):
        """Os 28 fosseis de cos_digest nasciam sem TTL nenhum."""
        assert EPHEMERAL_SIGNAL_TTL_HOURS["morning_briefing"] == 48
        assert EPHEMERAL_SIGNAL_TTL_HOURS["evening_briefing"] == 48

    def test_ttl_do_digest_cobre_fim_de_semana_curto(self):
        """48h e o atraso de leitura tolerado. Menos que isso derrubaria o
        digest de sexta antes de segunda; muito mais vira passivo de novo."""
        for t in ("morning_briefing", "evening_briefing"):
            assert 24 <= EPHEMERAL_SIGNAL_TTL_HOURS[t] <= 72


class TestSincronia:
    def test_known_detectors_bate_com_os_modulos(self):
        """Detector novo tem de entrar em KNOWN_DETECTORS, senao seus signals
        seriam varridos como se ele estivesse desligado."""
        import pathlib
        d = pathlib.Path(_ROOT) / "app" / "services" / "detectors"
        modulos = {p.stem for p in d.glob("detector_*.py")}
        assert modulos == set(KNOWN_DETECTORS), modulos ^ set(KNOWN_DETECTORS)

    def test_run_all_detectors_reporta_o_sweep(self):
        """Sweep silencioso vira sweep que ninguem sabe se rodou —
        [[feedback_consumidor_morto_wiring]]."""
        import inspect
        from services.detectors import _base
        src = inspect.getsource(_base.run_all_detectors)
        assert "expire_disabled_detector_signals" in src
        assert '"disabled_expired": disabled_expired' in src
