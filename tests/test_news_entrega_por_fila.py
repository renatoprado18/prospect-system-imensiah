"""Notícia capturada na hora errada não pode evaporar.

O CASO (22/08/2026). O Renato perguntou por que não foi avisado de um artigo do
Gui capturado em 10/08. Medido, a cadeia inteira:

  1. os 6 watchers estavam em `delivery_mode='silent'` (o default) — sem push e
     sem score: 0 de 162 hits tinham `ai_relevance_score`;
  2. o digest do Modo D morreu em duas etapas — o A3 (porta-voz único, 12/07)
     desligou o envio self-chat com early-return, e em 17/07 o tick foi
     desregistrado por ter virado no-op;
  3. sobrou o briefing da Tônia, que lê `copilot.news_hits` numa janela de
     2 DIAS — e roda em 22 de 45 dias (49%).

O hit do Gui era o ÚNICO na janela, não competia com nada, e ainda assim se
perdeu: o briefing não rodou em 11/08 nem 12/08, voltou em 13/08 e a essa altura
o hit tinha saído da janela.

A RAIZ É A JANELA DE TEMPO, NÃO O CANAL. Entrega por "últimas 48h" aposta que o
consumidor rode dentro delas; quando ele falha, o item não atrasa — evapora, sem
deixar registro. O corte passa a ser de ESTADO (`pushed_at IS NULL`), que é o
mesmo conserto do cursor do sync de grupos.

Rodar: .venv/bin/python -m pytest tests/test_news_entrega_por_fila.py -v
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402


class _Cur:
    """Cursor falso: guarda o SQL e devolve os hits declarados no teste."""

    def __init__(self, hits):
        self._hits = hits
        self.sqls = []
        self.updates = []

    def execute(self, sql, params=None):
        limpo = " ".join(sql.split())
        self.sqls.append((limpo, params))
        if limpo.startswith("UPDATE"):
            self.updates.append(params)

    def fetchall(self):
        return self._hits


class _Conn:
    def __init__(self, hits):
        self.cur = _Cur(hits)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hit(hid, titulo, dias_atras=0, projeto="Máquina de Originação"):
    return {"id": hid, "title": titulo, "url": f"http://x/{hid}", "source": "Google News",
            "hit_at": datetime.now() - timedelta(days=dias_atras),
            "watcher_id": 6, "query": "Carlos Guilherme Nose FESA", "projeto": projeto}


@pytest.fixture
def cenario(monkeypatch):
    from services import project_news_watcher as m

    estado = {"signals": []}

    def _emit(conn, **kw):
        estado["signals"].append(kw)
        return "emitted"

    monkeypatch.setattr("services.detectors._base.emit_signal", _emit)
    monkeypatch.setattr("services.detectors._base.make_signal_hash",
                        lambda tipo, *p: f"{tipo}:{'-'.join(str(x) for x in p)}")
    estado["mod"] = m
    # `alertar_hits_nao_entregues` importa `get_db` DENTRO da função, então o
    # patch tem que ser na origem — patchear o atributo do módulo não alcança.
    estado["patch_db"] = lambda conn: monkeypatch.setattr("database.get_db", lambda: conn)
    return estado


@pytest.mark.asyncio
async def test_hit_antigo_nao_entregue_ainda_e_alertado(cenario, monkeypatch):
    """O CASO DO GUI: capturado há 12 dias, nunca entregue.

    Com janela de 2 dias ele já teria sumido. Como a fila é por estado, ele
    continua sendo notícia que o Renato não viu."""
    conn = _Conn([_hit(147, "Gente & Gestão: recrutamento olho no olho", dias_atras=12)])
    cenario["patch_db"](conn)

    r = await cenario["mod"].alertar_hits_nao_entregues()
    assert r["hits"] == 1, "hit antigo sumiu — é a janela de tempo de novo"
    assert cenario["signals"], "não emitiu signal"
    ctx = cenario["signals"][0]["contexto"]
    assert "Gente & Gestão" in str(ctx)


@pytest.mark.asyncio
async def test_a_consulta_nao_filtra_por_data(cenario, monkeypatch):
    """Guarda de regressão do defeito de origem: se alguém reintroduzir um
    `hit_at > NOW() - interval`, a perda silenciosa volta."""
    conn = _Conn([_hit(1, "x")])
    cenario["patch_db"](conn)
    await cenario["mod"].alertar_hits_nao_entregues()

    select = next(s for s, _p in conn.cur.sqls if s.startswith("SELECT"))
    assert "pushed_at IS NULL" in select, "o corte por estado sumiu"
    assert "NOW() - " not in select, (
        "voltou a filtrar por janela de tempo — foi assim que o hit do Gui evaporou")


@pytest.mark.asyncio
async def test_marca_entregue_so_DEPOIS_de_emitir(cenario, monkeypatch):
    """Marcar antes de emitir perderia a notícia se o emit falhasse — trocaria um
    buraco silencioso por outro."""
    conn = _Conn([_hit(1, "x")])
    cenario["patch_db"](conn)
    await cenario["mod"].alertar_hits_nao_entregues()

    ordem = [s for s, _p in conn.cur.sqls]
    i_update = next(i for i, s in enumerate(ordem) if s.startswith("UPDATE"))
    i_select = next(i for i, s in enumerate(ordem) if s.startswith("SELECT"))
    assert i_select < i_update
    assert conn.commits == 1


@pytest.mark.asyncio
async def test_sem_pendencia_nao_emite_nada(cenario, monkeypatch):
    """Silêncio quando não há notícia — alerta vazio ensina a ignorar alerta."""
    conn = _Conn([])
    cenario["patch_db"](conn)
    r = await cenario["mod"].alertar_hits_nao_entregues()
    assert r["hits"] == 0
    assert r["skipped_reason"] == "sem_hits_pendentes"
    assert cenario["signals"] == []


@pytest.mark.asyncio
async def test_agrupa_por_projeto(cenario, monkeypatch):
    """Um signal com tudo, agrupado — não um signal por notícia."""
    conn = _Conn([_hit(1, "a", projeto="Jabô"), _hit(2, "b", projeto="Jabô"),
                  _hit(3, "c", projeto="Vallen")])
    cenario["patch_db"](conn)
    r = await cenario["mod"].alertar_hits_nao_entregues()
    assert r["projetos"] == 2
    assert len(cenario["signals"]) == 1, "emitiu um signal por notícia — vira ruído"


@pytest.mark.asyncio
async def test_urgencia_8_para_a_tonia_surfacar(cenario, monkeypatch):
    """A urgent da Tônia usa min_urgencia=8. Abaixo disso o alerta não chega —
    seria repetir o silêncio por outro caminho."""
    conn = _Conn([_hit(1, "x")])
    cenario["patch_db"](conn)
    await cenario["mod"].alertar_hits_nao_entregues()
    assert cenario["signals"][0]["urgencia"] == 8
