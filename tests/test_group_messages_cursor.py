"""O sync de grupo saiu do daily-sync e virou cron paginado — o ciclo tem que fechar.

O BUG (medido em 18/08/2026): o step `group_messages_sync` vivia dentro do
daily-sync com timeout de 90s. O `2da93e3` (05/08) fez cada grupo paginar até
alcançar o histórico — conserto certo, que encareceu a varredura e deixou a
janela de 90s onde estava. Do dia 06/08 em diante o step estourou TODO DIA, e
como `_detect_error` pinta o job inteiro quando um step falha, o daily-sync saiu
`error` 13 dias seguidos **sem perder uma única mensagem** (o webhook grava em
tempo real pelo mesmo critério; a janela das 5h UTC somou 10 msgs em 13 dias).

O dano era o alarme: job sempre-vermelho ensina a ignorar o status dos outros 12
passos. O sync virou `/api/cron/run-group-messages`, paginado por cursor.

O QUE ESTE TESTE PROTEGE: se a paginação pular um grupo ou travar no fim da
lista, a rede de segurança some calada — que é como o defeito original viveu.

Rodar: .venv/bin/python -m pytest tests/test_group_messages_cursor.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402


@pytest.fixture
def sync_fake(monkeypatch):
    """49 grupos, Evolution stubada — só a lógica de fatiamento sob teste."""
    from services import group_message_sync as gms
    from services import social_groups as sg

    grupos = [{"group_jid": f"{i}@g.us", "group_name": f"Grupo {i:02d}"} for i in range(49)]
    monkeypatch.setattr(sg, "get_sync_enabled_groups", lambda: grupos)
    monkeypatch.setenv("EVOLUTION_API_URL", "http://stub.local")

    visitados = []

    async def _um(client, base_url, api_key, instance, jid, name, limit):
        visitados.append(jid)
        return 1

    monkeypatch.setattr(gms, "_sync_single_group", _um)
    gms._visitados = visitados
    return gms


@pytest.fixture
def relogio_falso(monkeypatch):
    """Relógio controlado: cada grupo sincronizado "gasta" N segundos."""
    from services import group_message_sync as gms

    estado = {"agora": 0.0, "custo": 0.0}
    monkeypatch.setattr(gms.time, "monotonic", lambda: estado["agora"])

    async def _um(client, base_url, api_key, instance, jid, name, limit):
        estado["agora"] += estado["custo"]
        return 1

    monkeypatch.setattr(gms, "_sync_single_group", _um)
    return estado


@pytest.mark.asyncio
async def test_ciclo_cobre_todos_os_grupos(sync_fake):
    """Rodadas sucessivas visitam os 49 — nenhum grupo fica fora do ciclo."""
    vistos, offset = set(), 0
    for _ in range(5):
        sync_fake._visitados.clear()
        r = await sync_fake.sync_group_messages(limit_per_group=50, limit=12, offset=offset)
        vistos.update(sync_fake._visitados)
        offset = r["next_offset"]
    assert len(vistos) == 49, f"ficaram de fora: {49 - len(vistos)} grupos"


@pytest.mark.asyncio
async def test_cursor_volta_ao_topo_no_fim(sync_fake):
    """A fila é circular: alcançar o fim reseta pra 0, senão o cron para calado."""
    r = await sync_fake.sync_group_messages(limit_per_group=50, limit=12, offset=48)
    assert r["groups_synced"] == 1
    assert r["next_offset"] == 0
    assert r["more_pages"] is False


@pytest.mark.asyncio
async def test_offset_alem_do_fim_nao_perde_rodada(sync_fake):
    """Grupo removido entre rodadas deixa o cursor além do fim. Volta ao topo em
    vez de gastar a rodada sem sincronizar nada."""
    r = await sync_fake.sync_group_messages(limit_per_group=50, limit=12, offset=200)
    assert r["groups_synced"] == 12, "rodada perdida com cursor além do fim"


@pytest.mark.asyncio
async def test_chamador_sem_limit_varre_tudo(sync_fake):
    """O group_digest chama sem `limit` e depende da varredura completa."""
    r = await sync_fake.sync_group_messages(limit_per_group=100)
    assert r["groups_synced"] == 49
    assert r["more_pages"] is False


@pytest.mark.asyncio
async def test_lista_vazia_devolve_cursor(sync_fake, monkeypatch):
    """Sem grupos habilitados o endpoint lê next_offset do retorno — faltar a
    chave seria KeyError no cron, não 'nada a fazer'."""
    from services import social_groups as sg
    monkeypatch.setattr(sg, "get_sync_enabled_groups", lambda: [])
    r = await sync_fake.sync_group_messages(limit_per_group=50, limit=12, offset=0)
    assert r["next_offset"] == 0
    assert r["more_pages"] is False


@pytest.mark.asyncio
async def test_rodada_parcial_avanca_o_cursor(sync_fake, relogio_falso):
    """O DEFEITO DE 20/08: com corte só por `limit`, um lote caro estourava o
    timeout, o cursor NÃO avançava e a fila travava ali — os grupos seguintes
    nunca chegavam a vez e o job voltava a ser sempre-vermelho. Agora quem corta
    é o relógio: a rodada para sozinha e devolve o cursor pelo que cobriu."""
    relogio_falso["custo"] = 30.0  # cada grupo custa 30s
    r = await sync_fake.sync_group_messages(limit_per_group=50, limit=25,
                                            offset=0, budget_s=200)
    assert r["parcial"] is True, "não sinalizou rodada parcial"
    assert r["groups_synced"] == 7, f"cobriu {r['groups_synced']}, esperado 7 (200s / 30s)"
    assert r["next_offset"] == 7, "cursor não andou pelo que foi coberto"
    assert r["more_pages"] is True


@pytest.mark.asyncio
async def test_lote_caro_nao_trava_a_fila(sync_fake, relogio_falso):
    """Rodadas sucessivas sobre um lote sempre caro ainda percorrem os 49 —
    é o teste que falha se o cursor voltar a ficar preso."""
    relogio_falso["custo"] = 30.0
    offset, coberto, rodadas = 0, 0, 0
    while rodadas < 12:
        r = await sync_fake.sync_group_messages(limit_per_group=50, limit=25,
                                                offset=offset, budget_s=200)
        coberto += r["groups_synced"]
        offset = r["next_offset"]
        rodadas += 1
        if offset == 0:
            break
    assert coberto >= 49, f"ciclo não fechou: {coberto} de 49 em {rodadas} rodadas"
    assert offset == 0, "não voltou ao topo"


@pytest.mark.asyncio
async def test_grupo_patologico_conta_como_erro_dele(sync_fake, monkeypatch):
    """Um grupo que sozinho consumiria a janela não pode levar a rodada junto."""
    import asyncio as _aio
    from services import group_message_sync as gms

    async def _um(client, base_url, api_key, instance, jid, name, limit):
        if jid == "3@g.us":
            await _aio.sleep(9999)
        return 1

    monkeypatch.setattr(gms, "_sync_single_group", _um)
    r = await gms.sync_group_messages(limit_per_group=50, limit=5, offset=0,
                                      timeout_por_grupo=0.01)
    assert r["errors"] == 1
    assert r["groups_synced"] == 4, "os outros do lote deviam ter passado"
    assert r["next_offset"] == 5, "o grupo travado não pode parar o cursor"


@pytest.mark.asyncio
async def test_sem_budget_processa_o_lote_inteiro(sync_fake):
    """O `group_digest` chama sem budget e depende do lote completo."""
    r = await sync_fake.sync_group_messages(limit_per_group=50, limit=25, offset=0)
    assert r["groups_synced"] == 25
    assert r["parcial"] is False


def test_step_saiu_do_daily_sync():
    """Regressão do alarme: o step não pode voltar pro daily-sync sem que alguém
    reabra esta discussão. Enquanto ele estiver lá com 90s, o job sai vermelho."""
    main_py = open(os.path.join(_ROOT, "app", "main.py")).read()
    assert 'run_step("group_messages_sync"' not in main_py, \
        "group_messages_sync voltou pro daily-sync — o job volta a sair `error` todo dia"
    assert "/api/cron/run-group-messages" in main_py, "o cron isolado sumiu"
