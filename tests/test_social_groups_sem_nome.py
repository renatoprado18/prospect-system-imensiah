"""GRUPO SEM NOME NÃO SE DESCARTA EM SILÊNCIO — 10/11/2026.

MEDIDO contra a Evolution ao vivo em 10/11: o `findChats` devolve **136 grupos,
60 deles com `pushName: null` (44%)** — e os dois caminhos de inventário
filtravam por nome (`and c.get('pushName')` no findChats, `and name` no
fetchAllGroups), então o que não tinha nome SUMIA sem log, sem contagem e sem
erro. O refresh respondia `refreshed: 75`, que se lê como "deu tudo certo". Foi
assim que a "Fazenda Jaboticabeiras" não existia no cache no dia do dossiê.

⚠️ O QUE A MEDIÇÃO MOSTROU, e que é o motivo destes testes existirem na forma em
que estão: dos 60 sem nome, **54 já estavam no cache com nome real** e **34 dão
404 no `findGroupInfos`** (número saiu do grupo, JID de comunidade). Hoje isto
recupera ZERO grupo. O que se conserta é (a) o MECANISMO — grupo novo sem nome
passa a aparecer — e (b) a MEDIÇÃO, que era o número errado sem ninguém notar.

Por isso o teste que mais importa aqui não é "resolveu o nome", é **"o que não
resolveu aparece CONTADO"**. Um conserto que trocasse um descarte silencioso por
outro descarte silencioso passaria em qualquer teste de caminho feliz.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_social_groups_sem_nome.py -q
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import social_groups as sg  # noqa: E402

# A função VERDADEIRA, guardada antes de qualquer fixture a substituir — é com ela
# que se testa o `except` do banco fora do ar.
_CACHE_REAL = sg._jids_nomeados_no_cache


# ------------------------------------------------------------------ dublês

class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class _ClienteFake:
    """Dublê do httpx.AsyncClient. Conta as chamadas a findGroupInfos — é o que
    permite provar o atalho pelo cache (a perf), e não só o resultado."""

    def __init__(self, chats, infos):
        self.chats, self.infos = chats, infos
        self.info_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        return _Resp(self.chats)

    async def get(self, url, headers=None, timeout=None):
        jid = url.split("groupJid=")[-1]
        self.info_calls.append(jid)
        subject = self.infos.get(jid)
        if subject is None:
            return _Resp({"error": "not found"}, status=404)
        return _Resp({"subject": subject})


@pytest.fixture
def evolution(monkeypatch):
    monkeypatch.setenv("EVOLUTION_API_URL", "https://wa.exemplo")
    monkeypatch.setenv("EVOLUTION_API_KEY", "k")
    monkeypatch.setenv("EVOLUTION_INSTANCE", "rap")

    def _instalar(chats, infos=None, cache=None):
        cli = _ClienteFake(chats, infos or {})
        monkeypatch.setattr("services.social_groups.httpx.AsyncClient",
                            lambda *a, **k: cli)
        monkeypatch.setattr(sg, "_jids_nomeados_no_cache", lambda: dict(cache or {}))
        return cli
    return _instalar


def _chat(jid, push=None):
    return {"remoteJid": jid, "pushName": push}


G1, G2, G3 = "1111@g.us", "2222@g.us", "3333@g.us"


# ---------------------------------------------- o descarte silencioso acabou

def test_grupo_sem_pushname_nao_desaparece_mais(evolution):
    """O defeito, reproduzido: o grupo sem nome não pode sumir da lista."""
    cli = evolution([_chat(G1, "Com nome"), _chat(G2, None)],
                    infos={G2: "Fazenda Jaboticabeiras"})

    gs = asyncio.run(sg._fetch_groups_from_api())

    assert {g["jid"] for g in gs} == {G1, G2}, "os DOIS têm de voltar"
    por_jid = {g["jid"]: g for g in gs}
    assert por_jid[G2]["name"] == "Fazenda Jaboticabeiras"
    assert por_jid[G2]["resolvido_por_info"] is True
    assert por_jid[G1]["resolvido_por_info"] is False, "quem tinha pushName não gasta chamada"


def test_o_que_NAO_resolve_volta_com_name_None_e_nao_e_engolido(evolution):
    """A que mais importa. 404 no findGroupInfos é o caso REAL (34 dos 60 em
    10/11) — e ele tem de voltar na lista, para quem chama poder CONTAR."""
    evolution([_chat(G1, "Com nome"), _chat(G2, None)], infos={})   # G2 → 404

    gs = asyncio.run(sg._fetch_groups_from_api())

    por_jid = {g["jid"]: g for g in gs}
    assert set(por_jid) == {G1, G2}, "404 não pode virar desaparecimento"
    assert por_jid[G2]["name"] is None
    assert por_jid[G2]["resolvido_por_info"] is False


def test_nao_chama_findgroupinfos_para_quem_o_cache_ja_nomeia(evolution):
    """A 1ª versão chamou findGroupInfos para os 60 e levou 13,4s, com 34 das
    chamadas batendo em 404 a cada refresh, para sempre. O cache no meio é o que
    mantém a promessa de 2-4s — e sem este teste a regressão volta calada."""
    cli = evolution([_chat(G1, None), _chat(G2, None)],
                    infos={G1: "resolvido ao vivo", G2: "NAO DEVERIA SER CHAMADO"},
                    cache={G2: "Nome que o cache já tem"})

    gs = asyncio.run(sg._fetch_groups_from_api())

    assert cli.info_calls == [G1], f"só o desconhecido vai ao vivo; foi {cli.info_calls}"
    por_jid = {g["jid"]: g for g in gs}
    assert por_jid[G2]["name"] == "Nome que o cache já tem"
    assert por_jid[G2]["resolvido_por_info"] is False


def test_cache_fora_do_ar_degrada_para_lento_e_correto(evolution, monkeypatch):
    """Neon fora: todos os sem-nome vão ao vivo. Degradar para correto-e-lento é
    o certo; degradar para "descarta" era o defeito.

    Usa o helper VERDADEIRO com o `get_db` quebrado — stubar o helper por um
    `lambda: {}` testaria o dublê, não o `except` que precisa estar lá."""
    cli = evolution([_chat(G1, None), _chat(G2, None)], infos={G1: "A", G2: "B"})

    def _db_morto():
        raise RuntimeError("Neon fora do ar")

    monkeypatch.setattr(sg, "get_db", _db_morto)
    monkeypatch.setattr(sg, "_jids_nomeados_no_cache", _CACHE_REAL)

    assert _CACHE_REAL() == {}, "banco fora devolve vazio, não levanta"

    gs = asyncio.run(sg._fetch_groups_from_api())

    assert sorted(cli.info_calls) == sorted([G1, G2]), (
        "sem cache, TODOS os sem-nome vão ao findGroupInfos")
    assert all(g["name"] for g in gs)


def test_fetchallgroups_tambem_para_de_descartar(evolution):
    """Mesmo defeito pela outra porta: `and name` no fetchAllGroups."""
    class _Cli(_ClienteFake):
        async def get(self, url, headers=None, timeout=None):
            return _Resp([{"id": G1, "subject": "Com nome"},
                          {"id": G2, "subject": ""},
                          {"id": G3, "subject": None}])

    cli = _Cli([], {})
    import services.social_groups as mod
    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **k: cli
    try:
        gs = asyncio.run(sg._fetch_all_groups_evolution())
    finally:
        mod.httpx.AsyncClient = orig

    assert {g["jid"] for g in gs} == {G1, G2, G3}, "sem subject não é sem existir"
    por_jid = {g["jid"]: g for g in gs}
    assert por_jid[G1]["name"] == "Com nome"
    assert por_jid[G2]["name"] is None and por_jid[G3]["name"] is None


# ------------------------------------- a resposta para de dizer o número errado

def test_refresh_relata_o_DENOMINADOR_e_os_sem_nome(evolution, monkeypatch):
    """`refreshed: 75` era o número errado e não havia como notar: faltava o
    denominador. Agora `vistos` + `sem_nome` + os jids saem na resposta."""
    evolution([_chat(G1, "Um"), _chat(G2, None), _chat(G3, None)],
              infos={G2: "Dois"})            # G3 → 404
    gravados = []
    monkeypatch.setattr(sg, "get_db", None, raising=False)

    async def _corre():
        # `_persist` roda em thread e usa get_db; substituo o persist inteiro.
        import services.social_groups as mod
        real = mod.refresh_groups_basic
        return await real(full=False)

    # Intercepta a escrita sem tocar no banco.
    import services.social_groups as mod

    class _Cur:
        def execute(self_, sql, params=None):
            if params:
                gravados.append(params)

        def close(self_):
            pass

    class _Conn:
        def cursor(self_):
            return _Cur()

        def commit(self_):
            pass

        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

    monkeypatch.setattr(mod, "get_db", lambda: _Conn())

    out = asyncio.run(_corre())

    assert out["vistos"] == 3, "o DENOMINADOR tem de estar na resposta"
    assert out["refreshed"] == 2, "só os dois com nome entram no cache"
    assert out["sem_nome"] == 1
    assert out["sem_nome_jids"] == [G3], "o jid vai junto: contagem sem lista não é acionável"
    assert "nota_sem_nome" in out
    assert out["resolvidos_por_info"] == 1
    assert {p[0] for p in gravados} == {G1, G2}
    assert G3 not in {p[0] for p in gravados}, (
        "grupo sem nome NÃO é gravado — o jid como nome recria o placeholder "
        "que esconde o grupo da lista")
