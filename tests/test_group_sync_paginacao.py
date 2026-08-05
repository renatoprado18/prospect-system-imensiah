"""O sync de grupo tem que paginar até alcançar o que já tem.

O BUG (medido em 05/08/2026): `_sync_single_group` pedia as 50 mensagens mais
recentes UMA VEZ, e quem chama é o `daily-sync` — 1×/dia. Grupo que passasse de
50 naquele dia perdia o excedente **para sempre**: a rodada seguinte também só
olhava as 50 últimas, e não existe catch-up.

Números que motivaram:
  · CHARUTO CAP estourou as 50 em 52 de 66 dias (79%), média 73/dia, pico 154
  · o INTEL tinha 3.741 de 19.508 mensagens do grupo
  · somando os 8 grupos rastreados: 20.646 no INTEL × 56.037 na Evolution (37%)

E era invisível: pedia 50, recebia 50, gravava, reportava sucesso. Ninguém
perguntava "havia mais?".

Rodar: .venv/bin/python -m pytest tests/test_group_sync_paginacao.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402


def _msg(mid, ts=1785000000):
    return {"key": {"id": mid, "participant": "5511999@s.whatsapp.net", "fromMe": False},
            "message": {"conversation": f"texto {mid}"},
            "messageTimestamp": ts, "pushName": "Fulano", "messageType": "conversation"}


class _RespFake:
    def __init__(self, records, total=None):
        self.status_code = 200
        self._r = records
        self._t = total

    def json(self):
        return {"messages": {"records": self._r, "total": self._t}}


class _ClientFake:
    """Devolve páginas em sequência e conta quantas foram pedidas."""

    def __init__(self, paginas, total=None):
        self.paginas = paginas
        self.total = total
        self.pedidas = []

    async def post(self, url, headers=None, json=None):
        p = json.get("page", 1)
        self.pedidas.append(p)
        recs = self.paginas[p - 1] if p - 1 < len(self.paginas) else []
        return _RespFake(recs, self.total)


@pytest.fixture
def sem_banco(monkeypatch):
    """Isola do Postgres: nada conhecido, e o INSERT não faz nada."""
    from services import group_message_sync as gms

    class _Cur:
        rowcount = 1
        def execute(self, *a, **k): pass
        def fetchall(self): return []
        def fetchone(self): return None

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(gms, "get_db", lambda: _Conn())
    monkeypatch.setattr(gms, "_find_contact_by_phone", lambda c, p: None)
    return gms


@pytest.mark.asyncio
async def test_pagina_alem_da_primeira(sem_banco):
    """O caso do CHARUTO: 154 mensagens num dia não cabem em 50."""
    gms = sem_banco
    cli = _ClientFake([[_msg(f"a{i}") for i in range(50)],
                       [_msg(f"b{i}") for i in range(50)],
                       [_msg(f"c{i}") for i in range(4)],
                       []])

    await gms._sync_single_group(cli, "http://x", "k", "inst", "jid@g.us", "Grupo", 50)

    assert len(cli.pedidas) >= 3, (
        f"parou cedo — pediu só as páginas {cli.pedidas}; o excedente do dia se perderia"
    )


@pytest.mark.asyncio
async def test_para_quando_alcanca_o_conhecido(sem_banco, monkeypatch):
    """Não varre o histórico inteiro toda rodada: página já conhecida = chegou."""
    gms = sem_banco

    class _CurConhecido:
        rowcount = 1
        def execute(self, *a, **k): pass
        def fetchall(self): return [{"message_id": f"velha{i}"} for i in range(50)]
        def fetchone(self): return None

    class _ConnConhecido:
        def cursor(self): return _CurConhecido()
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(gms, "get_db", lambda: _ConnConhecido())

    cli = _ClientFake([[_msg("nova1")],                                # página 1: nova
                       [_msg(f"velha{i}") for i in range(50)],          # página 2: toda conhecida
                       [_msg("naodeviabuscar")]])

    await gms._sync_single_group(cli, "http://x", "k", "inst", "jid@g.us", "Grupo", 50)

    assert 3 not in cli.pedidas, (
        f"continuou depois de alcançar o passado (páginas {cli.pedidas}) — "
        "varreria o histórico inteiro toda rodada"
    )


@pytest.mark.asyncio
async def test_avisa_quando_o_buraco_e_maior_que_a_rodada(sem_banco, caplog):
    """A guarda comparativa: sem ela a próxima regressão volta a ser silenciosa."""
    import logging
    gms = sem_banco
    cli = _ClientFake([[_msg("x1")], []], total=99999)

    with caplog.at_level(logging.WARNING):
        await gms._sync_single_group(cli, "http://x", "k", "inst", "jid@g.us", "Grupo", 50)

    assert any("faltam" in r.message for r in caplog.records), (
        "buraco enorme entre Evolution e INTEL passou sem aviso"
    )


@pytest.mark.asyncio
async def test_nao_e_mais_teto_fixo(sem_banco):
    """Guarda contra a regressão exata: `limit` como teto de uma chamada só."""
    import inspect
    from services import group_message_sync as gms

    src = inspect.getsource(gms._sync_single_group)
    assert '"limit": limit' not in src, "voltou a pedir teto fixo numa chamada única"
    assert '"page"' in src and '"offset"' in src, "paginação sumiu"
