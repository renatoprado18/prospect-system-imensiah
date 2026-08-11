"""O sync de calendário tem que paginar — e o cancelamento tem que chegar.

O BUG (medido em 11/08/2026, a partir de um recado da sessão CoS). A Vallen
presencial de 12/08 foi cancelada no Google e seguia `confirmed` no INTEL, com
`last_synced_at` congelado em 08/08 enquanto os vizinhos da mesma série eram
carimbados no mesmo dia. A CoS afirmou ao Renato que "o convite segue confirmado
no Google" lendo aquela linha, e ele teve de corrigir com print da agenda.

A CAUSA ERA MAIS FUNDA QUE O CANCELAMENTO, e os números fecham a cadeia:

  · a janela do full sync (-30/+90 dias) tinha 135 eventos; `maxResults` = 100
  · o Google devolve `nextSyncToken` SÓ na última página — havendo
    `nextPageToken`, o sync token não vem
  · o código ignorava `nextPageToken`: 35 eventos nunca sincronizados
  · sem `nextSyncToken`, `calendar_sync_state` ficava VAZIA (verificado: zero
    linhas em produção)
  · sem token, `incremental_sync` caía em `full_sync` toda vez — e o incremental
    era o ÚNICO caminho que tratava cancelamento
  · e sem `showDeleted`, o Google nem devolve a ocorrência cancelada: ela é
    OMITIDA, então "não mudou" e "foi cancelado" viram a mesma coisa

Efeito medido: dos 135 eventos da janela, 108 tocados no dia; 19 com
`last_synced_at` de julho ou antes.

Rodar: .venv/bin/python -m pytest tests/test_calendar_sync_paginacao.py -v
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from integrations.google_calendar import GoogleCalendarIntegration  # noqa: E402


def _ev(eid, status="confirmed"):
    return {"id": eid, "status": status,
            "summary": f"evento {eid}",
            "start": {"dateTime": "2026-08-12T14:00:00-03:00"},
            "end": {"dateTime": "2026-08-12T15:00:00-03:00"}}


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


class _ClientFake:
    """Cliente que reproduz a paginação REAL do Google: `nextSyncToken` só na
    última página, e `pageToken` obrigatório para avançar."""

    def __init__(self, paginas):
        self.paginas = paginas
        self.chamadas = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        self.chamadas.append(dict(params or {}))
        token = (params or {}).get("pageToken")
        idx = 0 if token is None else int(token)
        pagina = self.paginas[idx]
        payload = {"items": pagina}
        if idx + 1 < len(self.paginas):
            payload["nextPageToken"] = str(idx + 1)
        else:
            payload["nextSyncToken"] = "TOKEN_FINAL"
        return _Resp(payload)


@pytest.fixture
def integra(monkeypatch):
    inst = GoogleCalendarIntegration()

    def _instalar(paginas):
        cliente = _ClientFake(paginas)
        monkeypatch.setattr("integrations.google_calendar.httpx.AsyncClient",
                            lambda *a, **k: cliente)
        return cliente
    return inst, _instalar


def test_pagina_ate_o_fim_e_devolve_tudo(integra):
    """135 eventos em 2 páginas: o sync tem que trazer os 135."""
    inst, instalar = integra
    paginas = [[_ev(f"a{i}") for i in range(100)],
               [_ev(f"b{i}") for i in range(35)]]
    cliente = instalar(paginas)

    r = asyncio.run(inst.list_events_incremental(access_token="x"))

    assert len(r["events"]) == 135, "parou na primeira página — 35 eventos perdidos"
    assert len(cliente.chamadas) == 2
    assert cliente.chamadas[1].get("pageToken") == "1", "não avançou de página"


def test_sync_token_so_existe_apos_a_ultima_pagina(integra):
    """A cadeia inteira do bug depende disto.

    O Google só devolve `nextSyncToken` na última página. Parando na primeira, o
    token vem vazio, `calendar_sync_state` nunca é gravada e o incremental — o
    único caminho que tratava cancelamento — nunca entra em ação.
    """
    inst, instalar = integra
    instalar([[_ev(f"a{i}") for i in range(100)], [_ev("b0")]])

    r = asyncio.run(inst.list_events_incremental(access_token="x"))
    assert r["nextSyncToken"] == "TOKEN_FINAL", (
        "sem token de sync o incremental nunca roda e o sync fica preso em full")
    assert r.get("nextPageToken") is None, "o page token não pode vazar já consumido"


def test_pede_cancelados_ao_google(integra):
    """Sem `showDeleted`, a ocorrência cancelada não é atualizada — é OMITIDA."""
    inst, instalar = integra
    cliente = instalar([[_ev("a0")]])
    asyncio.run(inst.list_events_incremental(access_token="x"))
    assert cliente.chamadas[0].get("showDeleted") == "true"


def test_full_sync_nao_manda_orderby_com_synctoken(integra):
    """`orderBy` junto de `syncToken` é 400 do Google. Um erro aqui zeraria o
    sync incremental inteiro, silenciosamente, a cada rodada."""
    inst, instalar = integra
    cliente = instalar([[_ev("a0")]])
    asyncio.run(inst.list_events_incremental(access_token="x", sync_token="TK"))
    p = cliente.chamadas[0]
    assert p.get("syncToken") == "TK"
    assert "orderBy" not in p and "timeMin" not in p


def test_teto_de_paginas_descarta_o_token(integra):
    """Varredura incompleta não pode gravar token.

    Gravar o token de uma varredura que não terminou faz o incremental seguinte
    partir de um estado que nunca existiu — e o buraco fica permanente, porque o
    delta daquele intervalo não volta.
    """
    inst, instalar = integra
    # 25 páginas: acima do teto de 20.
    instalar([[_ev(f"p{n}_{i}") for i in range(2)] for n in range(25)])

    r = asyncio.run(inst.list_events_incremental(access_token="x"))
    assert r.get("paginas_estouradas") is True
    assert r["nextSyncToken"] is None, "gravaria token de varredura incompleta"
    assert r["events"], "mesmo estourando, devolve o que juntou"


def test_full_sync_apaga_o_cancelado():
    """O full sync recebia cancelado e fazia upsert dele como se fosse real.

    Só o incremental tratava — e o incremental não rodava, porque o token nunca
    era gravado. Apagar (em vez de marcar `status='cancelled'`) é deliberado:
    nenhuma das 32 consultas a `calendar_events` filtra por status, e o item
    marcado continuaria aparecendo em toda tela. Foi o que aconteceu com o #4035.
    """
    fonte = open(os.path.join(_ROOT, "app", "services", "calendar_sync.py"),
                 encoding="utf-8").read()
    i = fonte.index("async def full_sync")
    j = fonte.index("async def _upsert_event")
    corpo = fonte[i:j]
    assert 'event.get("status") == "cancelled"' in corpo, (
        "full_sync voltou a ignorar cancelamento")
    assert "DELETE FROM calendar_events" in corpo
    assert "cursor.rowcount" in corpo, (
        "contar +=1 infla a estatística com cancelamento de evento que nem "
        "estava na base — e ela é o número que diz se o sync funciona")
