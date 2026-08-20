"""
Pytest fixtures compartilhadas para todos os testes.

⚠️ ESTE ARQUIVO TAMBÉM DECIDE EM QUE BANCO A SUÍTE ENCOSTA. Ver `_travar_no_banco_local`.
"""
import os
import re

import pytest
from datetime import datetime, timedelta


def _travar_no_banco_local() -> None:
    """Aponta a suíte pro Postgres local, ANTES de qualquer teste importar.

    O QUE ACONTECIA (medido em 20/08/26). `tests/test_f6_vector_search.py` tinha
    no topo, com todas as letras, o comentário "Force banco local":

        load_dotenv(".env")                        # <- traz DATABASE_URL do Neon
        if not os.getenv("DATABASE_URL"):          # <- nunca é verdade depois disso
            os.environ["DATABASE_URL"] = "postgresql://localhost:5432/intel"

    A guarda vinha DEPOIS do `load_dotenv`, então nunca disparava: a suíte rodava
    contra o **Neon de produção**. O fixture `memory_ids` faz três
    `save_system_memory` reais e um `DELETE` no teardown — escrita e remoção em
    prod, com o arquivo afirmando o contrário.

    E era também a causa do "a suíte pendura em ~54%" que o board carregava como
    "estado vazando entre arquivos": não vazava estado nenhum. Cada teste que
    encosta no banco pagava a viagem até o Neon (a memória
    [[feedback_dev_db_cron_runs_stale]] já registra que ele derruba conexão em
    lote longo), e 23 arquivos importam `get_db`/`get_connection`. Lentidão
    acumulada, não deadlock — por isso destravava sozinha se ninguém desistisse.

    A guarda agora roda no conftest, que o pytest carrega antes dos módulos de
    teste, e `load_dotenv` sem `override=True` não desfaz o que já está no
    ambiente. Pra rodar contra prod de propósito: `TEST_DB_TARGET=prod`.
    """
    if os.getenv("TEST_DB_TARGET") == "prod":
        print("\n⚠️  TEST_DB_TARGET=prod — a suíte vai ESCREVER no banco de produção.")
        return

    local = "postgresql://localhost:5432/intel"
    for var in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_URL_NON_POOLING"):
        atual = os.getenv(var, "")
        # localhost/127.0.0.1 passa; qualquer host de rede é remoto.
        if atual and not re.search(r"@(localhost|127\.0\.0\.1)[:/]", atual):
            os.environ[var] = local
    os.environ.setdefault("DATABASE_URL", local)
    # Some testes leem DB_TARGET pra decidir alvo; deixar coerente com o acima.
    os.environ["DB_TARGET"] = "local"
    os.environ.pop("ALLOW_PROD_FROM_LOCAL", None)


_travar_no_banco_local()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "sync_grupo_real: o teste exercita sync_group_messages de propósito "
        "(com a Evolution stubada por ele mesmo)",
    )


@pytest.fixture(autouse=True)
def _sem_varredura_de_grupo(request, monkeypatch):
    """Neutraliza `sync_group_messages` — nenhum teste varre a Evolution de verdade.

    O SEGUNDO MOTIVO DO "a suíte pendura em ~54%" (medido em 20/08/26), e é a
    MESMA causa-raiz que deixou o `daily-sync` vermelho por 13 dias.

    `group_digest.generate_daily_group_digests()` chama, fora do dry-run,
    `sync_group_messages(limit_per_group=100)` — varredura COMPLETA dos 49 grupos,
    uma chamada HTTP sequencial por grupo. Três testes de
    `test_notification_router_coverage.py` chamam essa função e stubam `get_db` e
    `_avaliar_grupo`, mas não o sync: o import dele é local, dentro da função, e
    não aparece no topo do módulo pra ninguém lembrar de mockar.

    Sempre foi assim; ficou insuportável quando o `2da93e3` (05/08) fez cada grupo
    paginar até alcançar o histórico. O mesmo commit que estourou a janela de 90s
    do daily-sync estourou a paciência de quem roda a suíte — dois sintomas
    distantes, uma causa.

    Quem testa o sync de propósito marca `@pytest.mark.sync_grupo_real`.
    """
    if request.node.get_closest_marker("sync_grupo_real"):
        return
    try:
        import services.group_message_sync as gms
    except Exception:  # pragma: no cover — app/ fora do path em teste isolado
        return

    async def _stub(*a, **k):
        return {"stub": "sync_group_messages neutralizado pelo conftest",
                "groups_synced": 0, "messages_saved": 0, "errors": 0,
                "total_groups": 0, "next_offset": 0, "more_pages": False}

    monkeypatch.setattr(gms, "sync_group_messages", _stub)


@pytest.fixture
def contact_familia():
    """Contato com tag familia - deve ser Circulo 1"""
    return {
        "id": 1,
        "nome": "Maria Silva",
        "tags": ["familia", "mae"],
        "total_interacoes": 50,
        "ultimo_contato": datetime.now().isoformat(),
        "empresa": None,
        "cargo": None,
        "contexto": "personal"
    }


@pytest.fixture
def contact_conselho():
    """Contato de conselho - deve ser Circulo 2"""
    return {
        "id": 2,
        "nome": "Joao Diretor",
        "tags": ["conselho", "board", "vallen"],
        "total_interacoes": 20,
        "ultimo_contato": (datetime.now() - timedelta(days=10)).isoformat(),
        "empresa": "Vallen Clinic",
        "cargo": "CEO",
        "contexto": "professional"
    }


@pytest.fixture
def contact_ativo():
    """Contato ativo com muitas interacoes - deve ser Circulo 2-3"""
    return {
        "id": 3,
        "nome": "Carlos Cliente",
        "tags": ["cliente", "vip"],
        "total_interacoes": 35,
        "ultimo_contato": (datetime.now() - timedelta(days=5)).isoformat(),
        "empresa": "Tech Corp",
        "cargo": "CTO",
        "linkedin": "https://linkedin.com/in/carlos",
        "contexto": "professional"
    }


@pytest.fixture
def contact_conhecido():
    """Contato ocasional - deve ser Circulo 4"""
    return {
        "id": 4,
        "nome": "Ana Networking",
        "tags": [],
        "total_interacoes": 8,
        "ultimo_contato": (datetime.now() - timedelta(days=45)).isoformat(),
        "empresa": "Startup X",
        "cargo": "Founder",
        "contexto": "professional"
    }


@pytest.fixture
def contact_arquivo():
    """Contato sem interacao - deve ser Circulo 5"""
    return {
        "id": 5,
        "nome": "Pedro Antigo",
        "tags": [],
        "total_interacoes": 0,
        "ultimo_contato": None,
        "empresa": None,
        "cargo": None,
        "contexto": None
    }


@pytest.fixture
def sample_contacts(contact_familia, contact_conselho, contact_ativo, contact_conhecido, contact_arquivo):
    """Lista de contatos de teste"""
    return [contact_familia, contact_conselho, contact_ativo, contact_conhecido, contact_arquivo]
