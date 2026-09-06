"""O signal nunca virava `resolved` — e sem numerador o valor nao se mede.

O CASO QUE ORIGINOU (06/09/2026, task #999981). De 206 signals de
`frente_review.alertar_portoes` em 45 dias, ZERO resolvidos: 186 expirados por
ausencia na run seguinte e 20 abertos. O ultimo `resolved` de qualquer origem
era de 27/06, feito pelo `tonha_brain`, que morreu. `capability_registry` divide
resolved/(resolved+ignorado) — com o numerador estruturalmente zero, o medidor
so podia mentir (0.0, corrigido em 31/08) ou abster (NULL, o estado atual).

A evidencia de ato JA ESTAVA no banco: o texto do signal cita as tasks que o
portao carrega, e o Renato as conclui no Google Tasks. O que faltava era ler.

As propriedades que estes testes preservam, cada uma por um erro real:

  1. `messages#28762` NAO e a task 28762. O texto mistura tabelas na mesma
     frase ("messages#28762 + PDF #28763"); sem esta regra, um ID de mensagem
     fecharia portao que ninguem cumpriu.
  2. Conclusao ANTES do signal nao e ato — o portao seria dado por cumprido por
     algo que ja estava feito quando ele nasceu.
  3. Fora da janela nao e ato: qualquer conclusao futura acabaria colhida como
     se fosse resposta ao alerta.
  4. `dry_run` e o padrao, e nao escreve nada.
  5. Anti-eco: conclusao feita pela propria camada nao conta. Hoje nenhuma
     operacao dela conclui task; o teste existe para o dia em que uma concluir.

Rodar: PYTHONPATH=app python -m pytest tests/test_signal_ato.py -v
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import pytest  # noqa: E402

from services.signal_ato import (  # noqa: E402
    extrair_tasks_citadas, fechar_signals_por_ato, JANELA_ATO_HORAS, MOTIVO,
)

AGORA = datetime(2026, 9, 1, 10, 0, 0)


# --------------------------------------------------------------------------
# 1) o extrator: o que e citacao de task e o que e citacao de outra tabela
# --------------------------------------------------------------------------
@pytest.mark.parametrize("texto,esperado", [
    # as formas reais colhidas dos signals em producao
    ("Task #999919, vence amanha 24/08.", {999919}),
    ("a task #999931 ja dispara segunda", {999931}),
    ("sair da mesa das 11h com nome e prazo do parecer (#999872).", {999872}),
    ("a decisao contratual da Jessica (#999858) e a fatura (#999884)", {999858, 999884}),
    ("tasks #999826 e #999960, ambas pending", {999826, 999960}),
    # e o que NAO pode virar task
    ("o criterio foi pedido em messages#30322", set()),
    ("entregue ao Israel (messages#28762 + PDF #28763)", {28763}),
    ("proxima janela: calendar_events#6287", set()),
    ("registro em project_notes#1533", set()),
    ("sem nenhum numero aqui", set()),
    ("", set()),
    (None, set()),
])
def test_extrator_separa_task_de_outras_tabelas(texto, esperado):
    assert extrair_tasks_citadas(texto) == esperado


def test_pdf_colado_em_messages_ainda_e_ambiguo_mas_nao_vaza_o_id_da_mensagem():
    """`messages#28762 + PDF #28763`: o primeiro e mensagem, o segundo e' solto.

    O extrator rejeita o que esta colado a nome de tabela e aceita o solto. Isso
    e' proposital: `#N` solto e' a forma como o texto cita task. O que impede o
    estrago nao e' o extrator sozinho — e' a exigencia, la no fechador, de que o
    ID exista em `tasks` E tenha sido concluido na janela.
    """
    assert extrair_tasks_citadas("messages#28762 + PDF #28763") == {28763}


# --------------------------------------------------------------------------
# um cursor/conn falso, so com o que o fechador usa
# --------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, signals, tasks, agent_writes):
        self._signals, self._tasks, self._writes = signals, tasks, agent_writes
        self._rows = []
        self.updates = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM signals" in s and s.startswith("SELECT"):
            self._rows = list(self._signals)
        elif "FROM tasks" in s:
            ids = set(params[0])
            self._rows = [t for t in self._tasks
                          if t["id"] in ids and t["status"] == "completed"
                          and t["data_conclusao"] is not None]
        elif "FROM agent_writes" in s:
            ids = set(params[0])
            self._rows = [{"registro_id": r} for r in self._writes if r in ids]
        elif s.startswith("UPDATE signals"):
            self.updates.append(params)
            self._rows = []
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, signals, tasks, agent_writes=()):
        self._cur = _FakeCursor(signals, tasks, agent_writes)

    def cursor(self):
        return self._cur


def _signal(sid, texto, criado=AGORA, status="open"):
    return {"id": sid, "detector": "frente_review.alertar_portoes", "status": status,
            "resolved_by": None, "criado_em": criado,
            "contexto": {"o_que": texto, "project_id": 40}}


def _task(tid, concluida_em, status="completed", titulo="uma task"):
    return {"id": tid, "status": status, "data_conclusao": concluida_em, "titulo": titulo}


# --------------------------------------------------------------------------
# 2) o fechador
# --------------------------------------------------------------------------
def test_task_concluida_depois_do_signal_fecha_o_portao():
    conn = _FakeConn(
        [_signal(1, "a task #999931 vence segunda")],
        [_task(999931, AGORA + timedelta(hours=13))],
    )
    r = fechar_signals_por_ato(conn, dry_run=True)
    assert r["fechados"] == 1
    assert r["evidencias"][0]["task_id"] == 999931
    assert r["evidencias"][0]["horas"] == 13.0


def test_conclusao_anterior_ao_signal_nao_e_ato():
    """O portao nasceu depois de a task ja estar feita — nada foi cumprido por ele."""
    conn = _FakeConn(
        [_signal(1, "a task #999931 vence segunda")],
        [_task(999931, AGORA - timedelta(hours=5))],
    )
    assert fechar_signals_por_ato(conn, dry_run=True)["fechados"] == 0


def test_conclusao_fora_da_janela_nao_e_ato():
    conn = _FakeConn(
        [_signal(1, "a task #999931 vence segunda")],
        [_task(999931, AGORA + timedelta(hours=JANELA_ATO_HORAS + 1))],
    )
    assert fechar_signals_por_ato(conn, dry_run=True)["fechados"] == 0


def test_id_de_mensagem_nao_fecha_portao():
    """Mesmo que exista uma task com o numero da mensagem, a citacao era de `messages`."""
    conn = _FakeConn(
        [_signal(1, "o criterio foi pedido em messages#30322")],
        [_task(30322, AGORA + timedelta(hours=2))],
    )
    assert fechar_signals_por_ato(conn, dry_run=True)["fechados"] == 0


def test_task_nao_concluida_nao_fecha():
    conn = _FakeConn(
        [_signal(1, "a task #999931 vence segunda")],
        [_task(999931, None, status="pending")],
    )
    assert fechar_signals_por_ato(conn, dry_run=True)["fechados"] == 0


def test_dry_run_e_o_padrao_e_nao_escreve():
    conn = _FakeConn(
        [_signal(1, "a task #999931 vence segunda")],
        [_task(999931, AGORA + timedelta(hours=2))],
    )
    r = fechar_signals_por_ato(conn)          # sem passar dry_run
    assert r["dry_run"] is True
    assert r["fechados"] == 1
    assert conn._cur.updates == []           # nenhuma escrita


def test_sem_dry_run_escreve_com_procedencia():
    conn = _FakeConn(
        [_signal(1, "a task #999931 vence segunda")],
        [_task(999931, AGORA + timedelta(hours=2))],
    )
    r = fechar_signals_por_ato(conn, dry_run=False)
    assert r["fechados"] == 1
    assert len(conn._cur.updates) == 1
    params = conn._cur.updates[0]
    assert params[1] == MOTIVO
    assert "ato_evidencia" in params[2]       # a evidencia vai pro contexto
    assert "999931" in params[2]


def test_conclusao_feita_pela_camada_nao_conta_como_ato():
    """Anti-eco: se a propria camada concluir a task, o detector nao pode se
    declarar util por isso. Hoje nenhuma operacao dela conclui — a guarda existe
    para o dia em que uma concluir."""
    conn = _FakeConn(
        [_signal(1, "a task #999931 vence segunda")],
        [_task(999931, AGORA + timedelta(hours=2))],
        agent_writes=[999931],
    )
    r = fechar_signals_por_ato(conn, dry_run=True)
    assert r["fechados"] == 0
    assert r["descartados_por_agente"] == 1


def test_um_signal_fecha_uma_vez_mesmo_citando_varias_tasks():
    conn = _FakeConn(
        [_signal(1, "as tasks #999931 e #999858 estao no portao")],
        [_task(999931, AGORA + timedelta(hours=2)),
         _task(999858, AGORA + timedelta(hours=3))],
    )
    r = fechar_signals_por_ato(conn, dry_run=True)
    assert r["fechados"] == 1


def test_signal_sem_citacao_nao_quebra():
    conn = _FakeConn([_signal(1, "portao sem numero de task nenhum")], [])
    r = fechar_signals_por_ato(conn, dry_run=True)
    assert r["fechados"] == 0
    assert r["citacoes"] == 0


# --------------------------------------------------------------------------
# 3) o ratio nao pode mentir pelo avesso
# --------------------------------------------------------------------------
def test_cobertura_acompanha_o_ratio():
    """Dar numerador ao medidor criou um 1.0 tao enganoso quanto o 0.0 de 31/08.

    Assim que os 18 atos entraram, `alertar_portoes` ficou com 18 acted / 0
    ignored -> ratio 1.0, com 168 sinais inconclusivos fora da conta. Lido
    sozinho, 1.0 diz "detector perfeito" para uma medicao que alcanca 9% dos
    casos. `value_coverage` e o que impede essa leitura.
    """
    from services.capability_registry import _detectors

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [{
                "detector": "frente_review.alertar_portoes",
                "invocations": 206, "resolved": 18, "dismissed": 0,
                "still_open": 20, "expired_ttl": 0, "expired_stale": 168,
                "expired_disabled": 0, "expired_outro": 0, "avg_urgencia": 8.0,
            }]

    d = _detectors(_Cur(), 45)[0]
    assert d["value_ratio"] == 1.0
    assert d["value_acted"] == 18 and d["value_ignored"] == 0
    # 18 de 206 -> a medicao alcanca menos de 10% dos sinais
    assert d["extra"]["value_coverage"] == round(18 / 206, 4)
    assert "nao sobre o total" in d["extra"]["value_coverage_note"]
