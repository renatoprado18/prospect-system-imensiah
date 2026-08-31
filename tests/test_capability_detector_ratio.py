"""
O medidor contava SUCESSO como ruido (31/08/26).

`capability_registry._detectors` tinha um balde unico de `expired` e somava tres
coisas incompativeis nele. A pior: `detector_expired`, que e o carimbo de
`expire_stale_signals` quando o hash SUMIU da run seguinte. Pro
`frente_review.alertar_portoes` isso e o mais perto de sucesso que existe — o
portao saiu do debriefing porque foi cumprido — e era contado como ignorado.

Medido em prod antes do conserto: `detector:frente_review.alertar_portoes` com
value_ratio **0.0000**, 99 `expired` (todos `detector_expired`), zero
`ttl_expired`, zero `resolved`; e **119 de 119** signals do detector constando
como entregues em `tonia_seen_signals`. Entrega 100%, valor medido zero. Cada
rodada de `persist_snapshot` gravava mais uma linha dessa tendencia falsa, e a
decisao de aposentar detector se apoiaria nela.

A propriedade que importa: sinal SEM DESFECHO LEGIVEL nao entra em nenhum lado.
Abster (None) e a resposta honesta; 0.0 era invencao.

Rodar: PYTHONPATH=app python -m pytest tests/test_capability_detector_ratio.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.capability_registry import _detectors, _ratio  # noqa: E402


def _row(detector="d", invocations=0, resolved=0, dismissed=0, still_open=0,
         expired_ttl=0, expired_stale=0, expired_disabled=0, expired_outro=0,
         avg_urgencia=8.0):
    return {
        "detector": detector,
        "invocations": invocations or (resolved + dismissed + still_open
                                       + expired_ttl + expired_stale
                                       + expired_disabled + expired_outro),
        "resolved": resolved,
        "dismissed": dismissed,
        "still_open": still_open,
        "expired_ttl": expired_ttl,
        "expired_stale": expired_stale,
        "expired_disabled": expired_disabled,
        "expired_outro": expired_outro,
        "avg_urgencia": avg_urgencia,
    }


class _Cur:
    """Cursor fake no formato RealDictCursor (fetchall -> list[dict])."""

    def __init__(self, rows):
        self._rows = rows
        self.sql = None

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self._rows


def _one(row):
    return _detectors(_Cur([row]), 14)[0]


class TestPortaoNaoEIgnorado:
    def test_o_caso_real_de_prod_abstem_em_vez_de_gravar_zero(self):
        """99 expired por ausencia na run, nenhum resolved: o antigo dava 0.0."""
        cap = _one(_row("frente_review.alertar_portoes",
                        expired_stale=99, still_open=20))
        assert cap["value_ratio"] is None, "0.0 aqui e a mentira que gerou a task"
        assert cap["value_acted"] == 0
        assert cap["value_ignored"] == 0, "expired por ausencia nao e ignorado"
        assert cap["extra"]["inconclusive"] == 99

    def test_a_nota_explica_a_abstencao_e_aponta_a_causa_raiz(self):
        cap = _one(_row("frente_review.alertar_portoes", expired_stale=5))
        nota = cap["extra"]["value_note"]
        assert "#999981" in nota, "sem apontar o fechador ausente, a abstencao vira mudez"
        assert "nao e ignorancia" in nota.replace("NAO", "nao")

    def test_detector_desligado_tambem_nao_conta(self):
        """Pararam de perguntar != o detector nao valia nada."""
        cap = _one(_row("detector_relacionamento", expired_disabled=29))
        assert cap["value_ratio"] is None
        assert cap["value_ignored"] == 0
        assert cap["extra"]["inconclusive"] == 29


class TestOQueContinuaSendoIgnorado:
    def test_ttl_expired_e_ignorado_de_verdade(self):
        """Ficou disponivel o prazo inteiro e nada veio — isso e ignorar."""
        cap = _one(_row("pre_meeting_briefing", resolved=1, expired_ttl=5))
        assert cap["value_ignored"] == 5
        assert cap["value_ratio"] == _ratio(1, 5)

    def test_dismissed_e_ignorado(self):
        cap = _one(_row("d", resolved=3, dismissed=1))
        assert cap["value_ignored"] == 1
        assert cap["value_ratio"] == 0.75

    def test_rotulo_desconhecido_conta_como_ignorado_e_fica_visivel(self):
        """Conservador: nao inflar valor. Mas se crescer, apareceu escritor novo."""
        cap = _one(_row("d", resolved=1, expired_outro=4))
        assert cap["value_ignored"] == 4
        assert cap["extra"]["expired_outro"] == 4


class TestContagemNaoPerdeSinal:
    def test_expired_total_continua_somando_os_quatro_baldes(self):
        cap = _one(_row("d", expired_ttl=1, expired_stale=2,
                        expired_disabled=3, expired_outro=4))
        assert cap["extra"]["expired"] == 10

    def test_ratio_convive_com_inconclusivo_e_declara_a_base(self):
        cap = _one(_row("d", resolved=3, expired_ttl=1, expired_stale=10))
        assert cap["value_ratio"] == 0.75
        assert cap["extra"]["inconclusive"] == 10
        assert "10" in cap["extra"]["value_note"], "quem le precisa saber o que ficou de fora"

    def test_sem_nada_na_janela_mantem_a_nota_antiga(self):
        cap = _one(_row("d", still_open=7))
        assert cap["value_ratio"] is None
        assert "todos open" in cap["extra"]["value_note"]
