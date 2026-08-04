"""Testes do heartbeat da ingestao WA (incidente de 03-04/08/2026).

O webhook ficou 3h calado e nada avisou -- 93 mensagens fora, entre elas o
endereco de uma reuniao presencial. O heartbeat existe pra isso nao repetir.

O risco do remedio e ser pior que a doenca: "alertar se nao chega mensagem ha
20min" grita toda madrugada, e alerta que grita a toa e desligado -- ai a
proxima queda passa de novo. Por isso o veredito e COMPARATIVO. Estes testes
cobrem os dois erros possiveis:

  (a) FALSO NEGATIVO -- cano parado e ninguem avisa (o incidente);
  (b) FALSO POSITIVO -- silencio legitimo virando alarme (o que mata o alerta).

Rodar: .venv/bin/python -m pytest tests/test_wa_ingest_heartbeat.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import wa_ingest_heartbeat as hb  # noqa: E402


@pytest.fixture
def relogio(monkeypatch):
    agora = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(hb, "now_utc", lambda: agora)
    return agora


def _arma(monkeypatch, ultimo_banco, epoch_evolution):
    monkeypatch.setattr(hb, "_ultimo_evento_no_banco", lambda: ultimo_banco)
    monkeypatch.setattr(hb, "_ultima_mensagem_na_evolution", lambda: epoch_evolution)


# ------------------------------------------------------- o incidente de 03/08 --

def test_alerta_quando_evolution_tem_o_que_o_intel_nao_tem(relogio, monkeypatch):
    """O caso real: banco parado as 17:30, Evolution recebendo ate agora."""
    ultimo = relogio - timedelta(hours=3)
    _arma(monkeypatch, ultimo, int((relogio - timedelta(minutes=2)).timestamp()))

    v = hb.checar_ingestao()

    assert v["alerta"] is True
    assert v["status"] == "parado"
    assert v["atraso_min"] > 170


def test_janela_perdida_cobre_o_buraco(relogio, monkeypatch):
    """O catchup tem que pedir horas suficientes pra alcancar o inicio do gap."""
    ultimo = relogio - timedelta(hours=3)
    _arma(monkeypatch, ultimo, int(relogio.timestamp()))

    janela = hb.janela_perdida(hb.checar_ingestao())

    assert janela["horas"] >= 3, "pediria menos horas que o tamanho do buraco"


# ------------------------------------------ o alerta que se mataria sozinho --

def test_madrugada_silenciosa_nao_alerta(relogio, monkeypatch):
    """ZERO evento por horas e NORMAL de madrugada -- 06h UTC teve zero no dia
    medido e estava tudo certo. Se isto alertar, o Renato desliga o aviso e a
    proxima queda de verdade passa batido."""
    ultimo = relogio - timedelta(hours=4)
    # A Evolution tambem nao tem nada novo: o mundo esta quieto, nao o cano.
    _arma(monkeypatch, ultimo, int(ultimo.timestamp()))

    v = hb.checar_ingestao()

    assert v["alerta"] is False
    assert v["status"] == "mundo_quieto"


def test_mensagem_em_transito_nao_alerta(relogio, monkeypatch):
    """A mensagem que chegou na Evolution neste segundo ainda nao teve tempo de
    virar linha aqui. Sem tolerancia, todo pico viraria alerta."""
    ultimo = relogio - timedelta(minutes=25)
    _arma(monkeypatch, ultimo, int((ultimo + timedelta(seconds=30)).timestamp()))

    assert hb.checar_ingestao()["alerta"] is False


def test_fluxo_normal_nem_pergunta_pra_evolution(relogio, monkeypatch):
    """Custo: no caminho feliz nao ha chamada externa nenhuma."""
    monkeypatch.setattr(hb, "_ultimo_evento_no_banco", lambda: relogio - timedelta(minutes=3))

    def _explode():
        raise AssertionError("chamou a Evolution com o banco em dia")

    monkeypatch.setattr(hb, "_ultima_mensagem_na_evolution", _explode)

    assert hb.checar_ingestao()["status"] == "ok"


def test_evolution_muda_nao_vira_alerta(relogio, monkeypatch):
    """Se a Evolution nao responde, nao sabemos -- e nao-saber nao e alerta.
    Senao a instabilidade DELA viraria alarme de ingestao parada, e o aviso
    perderia o sentido."""
    _arma(monkeypatch, relogio - timedelta(hours=2), 0)

    v = hb.checar_ingestao()

    assert v["alerta"] is False
    assert v["status"] == "indeterminado"


def test_banco_vazio_nao_alerta(relogio, monkeypatch):
    _arma(monkeypatch, None, 0)

    assert hb.checar_ingestao()["alerta"] is False


# --------------------------------------------------------------- integracao --

def test_job_esta_agendado_no_worker():
    """Servico + endpoint sem agendador seria um monitor que nunca roda --
    exatamente o tipo de 'wirado mas nao vivo' que ja mordeu antes."""
    caminho = os.path.join(_ROOT, "workers", "audio-transcriber", "main.py")
    with open(caminho, encoding="utf-8") as fh:
        fonte = fh.read()

    assert "/api/cron/wa-ingest-heartbeat" in fonte


def test_endpoint_existe_e_exige_cron_auth():
    caminho = os.path.join(_ROOT, "app", "main.py")
    with open(caminho, encoding="utf-8") as fh:
        fonte = fh.read()

    i = fonte.find('@app.post("/api/cron/wa-ingest-heartbeat")')
    assert i > 0, "endpoint nao existe"
    assert "verify_cron_auth" in fonte[i:i + 2000]
