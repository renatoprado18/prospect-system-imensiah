"""A régua de "próximo passo" do board_hunt, na parte que erra calada.

POR QUE ESTE TESTE EXISTE. O alarme "⚠ sem próximo passo" só olhava a agenda, e
em 23/08/26 acusava 11 frentes das quais 10 tinham ato marcado em task pra
semana seguinte — acusava quem estava em dia. O conserto foi ler `tasks`; o
risco do conserto é o simétrico, e pior: alarme que EMUDECE por casar com
evidência frouxa some com a frente do radar sem ninguém notar
([[feedback_guarda_abstencao_vira_fabrica]]).

O que estes testes travam é exatamente a linha que alguém afrouxaria "pra pegar
mais casos": o casamento exige `contact_id` E `project_id`. A task #999870
("Voltar ao Monforte") aponta pro contato da Premix e mora no projeto 65 — com
só `contact_id` ela viraria o próximo passo de uma frente que não é a dela.
"""
import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import board_hunt as B


def _task(tid, cid, pid, venc=None, status="pending", hold=None):
    return {"id": tid, "titulo": f"task {tid}", "contact_id": cid, "project_id": pid,
            "data_vencimento": datetime(venc.year, venc.month, venc.day) if venc else None,
            "status": status, "on_hold_since": hold, "on_hold_reason": None}


def _frente(fid=1, cid=10, pid=20):
    return {"id": fid, "nome": "Frente", "contato_id": cid, "project_id": pid}


# ------------------------------------------------------------- o corte estrito

def test_task_do_mesmo_contato_em_outro_projeto_nao_casa():
    """O caso Premix — a razão de o corte ser por cid E pid."""
    d = {"tasks": {(10, 65): _task(999870, 10, 65, date(2026, 9, 1))}, "esperas": {}}
    assert B.proxima_task(_frente(cid=10, pid=20), d) is None


def test_task_do_mesmo_projeto_mas_outro_contato_nao_casa():
    d = {"tasks": {(99, 20): _task(1, 99, 20, date(2026, 9, 1))}, "esperas": {}}
    assert B.proxima_task(_frente(cid=10, pid=20), d) is None


def test_task_com_contato_e_projeto_iguais_casa():
    t = _task(999911, 10, 20, date(2026, 8, 24))
    d = {"tasks": {(10, 20): t}, "esperas": {}}
    assert B.proxima_task(_frente(cid=10, pid=20), d) is t


def test_frente_sem_projeto_nunca_casa():
    """Sem `project_id` não há corte estrito possível — não casar é o correto."""
    d = {"tasks": {(10, None): _task(1, 10, None, date(2026, 8, 24))}, "esperas": {}}
    assert B.proxima_task(_frente(cid=10, pid=None), d) is None


# ------------------------------------------------------------------- a espera

def test_espera_consciente_e_lida_do_balde_proprio():
    t = _task(999851, 10, 20, date(2026, 8, 18), status="on_hold", hold=datetime(2026, 8, 18))
    d = {"tasks": {}, "esperas": {(10, 20): t}}
    assert B.espera_consciente(_frente(cid=10, pid=20), d) is t
    assert B.proxima_task(_frente(cid=10, pid=20), d) is None


# ----------------------------------------------------- a janela e a ordenação

def test_coletar_ignora_task_vencida_e_sem_data(monkeypatch):
    """Task sem data não é ato marcado; task no passado é o oposto de próximo
    passo. A #999851 (hold, vencida 18/08) não pode entrar como 'próximo'."""
    hoje = date(2026, 8, 23)
    linhas = [
        _task(1, 10, 20, None),                         # sem data  → fora
        _task(2, 10, 20, hoje - timedelta(days=5)),     # vencida   → fora
        _task(3, 10, 20, hoje + timedelta(days=99)),    # além de 45d → fora
        _task(4, 10, 20, hoje + timedelta(days=2)),     # ✅ esta
    ]
    d = _coletar_tasks(linhas, [_frente(cid=10, pid=20)], hoje)
    assert d["tasks"][(10, 20)]["id"] == 4


def test_coletar_pega_a_mais_proxima_no_tempo(monkeypatch):
    """Duas tasks válidas: vale a que vence antes. É o que separou a #999897
    ('Tocar a Fernanda', 24/08) da #999843 (one-pager LGPD, 28/08) na Motiva."""
    hoje = date(2026, 8, 23)
    linhas = [_task(999897, 10, 20, hoje + timedelta(days=1)),
              _task(999843, 10, 20, hoje + timedelta(days=5))]
    d = _coletar_tasks(linhas, [_frente(cid=10, pid=20)], hoje)
    assert d["tasks"][(10, 20)]["id"] == 999897


def _coletar_tasks(linhas, frentes, hoje):
    """Roda o trecho de coleta de `coletar()` contra um cursor de mentira.

    Vai direto no `coletar` real em vez de reimplementar a régua aqui: teste que
    copia a lógica que deveria vigiar passa a concordar consigo mesmo.
    """
    class _Cur:
        def __init__(self):
            self._n = 0

        def execute(self, sql, args=None):
            self._sql = sql

        def fetchall(self):
            self._n += 1
            return linhas if "FROM tasks" in self._sql else []

        def fetchone(self):
            if "calendar_events" in self._sql and "max(" in self._sql:
                return {"agenda": None}
            if "count(" in self._sql:
                return {"total": 0, "com_contato": 0}
            return {"agenda": None}

    cur = _Cur()
    B_frentes = list(frentes)

    class _CurFrentes(_Cur):
        def fetchall(self):
            if "board_hunt_frentes" in self._sql:
                return [dict(f, subtitulo=None, fase=1, status="ativo", nota=None,
                             piso_alvo=None, projeto=None, contato=None,
                             originador_contact_id=None, originador=None,
                             originador_rotulo=None) for f in B_frentes]
            if "FROM tasks" in self._sql:
                return linhas
            return []

    return B.coletar(_CurFrentes(), hoje)


# ---------------------------------------------------- o efeito na temperatura

def test_ato_em_task_conta_como_ter_para_onde_ir():
    """Troca fresca com a bola no terceiro caía pra morno por não ter reunião —
    mesmo com toque agendado pra amanhã."""
    assert B.temperatura(4, "terceiro", False) == "morno"
    assert B.temperatura(4, "terceiro", True) == "quente"
