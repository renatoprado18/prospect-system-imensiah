"""O cron da camada virou rede de segurança — e rede que não pega é decoração.

Contexto (10/08/2026): dois motores produziam o mesmo debriefing. O agente
local roda 14×/dia no Max (custo zero) desde 31/07; o cron da API refazia o
trabalho 1×/dia por US$ 16,83/mês. Desligar o cron seco resolveria o custo e
criaria um risco pior: Mac dormindo = nenhum portão, sem aviso, e "não apareceu
nada" é indistinguível de "não havia nada".

A saída foi condicionar: a API só roda quando o agente NÃO escreveu hoje. O
custo passa a ser proporcional à falha em vez de fixo.

Estes testes guardam as três formas de o mecanismo mentir:
  1. não pular quando devia  -> a economia não acontece
  2. pular quando não devia   -> o dia fica sem portão
  3. contar a própria rodada  -> o fallback se auto-satisfaz e nunca mais roda

Rodar: PYTHONPATH=app .venv/bin/python -m pytest tests/test_cos_daily_review_fallback.py -v
"""
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))


class _Cur:
    def __init__(self, resultado):
        self._resultado = resultado
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())

    def fetchone(self):
        return self._resultado


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _com_resultado(monkeypatch, resultado):
    from services import frente_review
    cur = _Cur(resultado)
    monkeypatch.setattr(frente_review, "get_db", lambda: _Conn(cur))
    return frente_review, cur


def test_pula_quando_o_agente_ja_escreveu(monkeypatch):
    """O caso comum: Mac acordou, rodou, gravou. A API não gasta nada."""
    fr, _ = _com_resultado(monkeypatch, {"ult": "2026-08-10 14:19:11"})
    assert fr.agente_local_ja_rodou_hoje() is True


def test_roda_quando_o_agente_nao_escreveu(monkeypatch):
    """Mac dormindo / viagem / launchd quebrado: a rede tem que pegar."""
    fr, _ = _com_resultado(monkeypatch, {"ult": None})
    assert fr.agente_local_ja_rodou_hoje() is False


def test_roda_quando_nao_ha_linha_nenhuma(monkeypatch):
    """Banco sem review nenhum hoje — fetchone devolvendo None não pode virar
    'já rodou' por acidente."""
    fr, _ = _com_resultado(monkeypatch, None)
    assert fr.agente_local_ja_rodou_hoje() is False


def test_so_conta_o_agente_local_nao_a_propria_api(monkeypatch):
    """A trava contra o fallback se auto-satisfazer.

    A rodada da PRÓPRIA API também grava em cos_daily_review. Se a consulta não
    filtrasse por motor, bastaria um dia de fallback para que no dia seguinte
    ela encontrasse "review de hoje" e pulasse para sempre — a máquina ouvindo
    o próprio eco. O filtro tem que estar na query, não na intenção.
    """
    fr, cur = _com_resultado(monkeypatch, {"ult": None})
    fr.agente_local_ja_rodou_hoje()
    assert "payload->>'motor' = 'agente_local'" in cur.sql, (
        f"a query não filtra por motor — o fallback vai se auto-satisfazer: {cur.sql}"
    )
    assert "CURRENT_DATE" in cur.sql, "sem recorte de hoje, ontem conta como hoje"


def test_erro_na_checagem_nao_vira_pulo():
    """Se a checagem explode, a rota RODA. Abster no ambíguo deixaria o dia sem
    portão justamente quando a verificação falhou — a guarda que se abstém
    fabrica o defeito que devia impedir."""
    fonte = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    i = fonte.index("cos-daily-review: checagem do agente local falhou")
    trecho = fonte[i - 400:i + 300]
    assert "_ja_rodou = False" in trecho, (
        "no except, o padrão tem que ser RODAR (False), não pular"
    )


def test_o_cron_roda_depois_da_primeira_rodada_do_agente():
    """Horário importa: o agente começa 07:12 BRT (10:12 UTC). Um cron às 10:00
    UTC checaria antes de o agente ter chance de rodar, dispararia o fallback
    todo dia, gastaria igual e provaria nada."""
    fonte = (_ROOT / "workers" / "audio-transcriber" / "main.py").read_text(encoding="utf-8")
    i = fonte.index('("cos-daily-review"')
    linha = fonte[i:fonte.index("\n", i)]
    assert "hour=12" in linha, f"cron cedo demais para o fallback significar algo: {linha}"
