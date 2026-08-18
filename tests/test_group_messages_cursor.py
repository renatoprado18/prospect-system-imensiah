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


def test_step_saiu_do_daily_sync():
    """Regressão do alarme: o step não pode voltar pro daily-sync sem que alguém
    reabra esta discussão. Enquanto ele estiver lá com 90s, o job sai vermelho."""
    main_py = open(os.path.join(_ROOT, "app", "main.py")).read()
    assert 'run_step("group_messages_sync"' not in main_py, \
        "group_messages_sync voltou pro daily-sync — o job volta a sair `error` todo dia"
    assert "/api/cron/run-group-messages" in main_py, "o cron isolado sumiu"
