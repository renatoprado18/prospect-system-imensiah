"""
Smoke test pra Fase 6 do Life Coaching: vector search em system_memories.

O que testa:
1. embeddings.embed_sync() retorna vetor de dim correta quando API key configurada
2. save_system_memory grava embedding na coluna nova
3. search_memories(mode='semantic') acha memorias por sinonimo (drenado/cansado/exausto)
4. search_memories(mode='hybrid') deduplica resultados keyword + semantic
5. mode='keyword' continua funcionando como antes

Skip automatico se VOYAGE_API_KEY nao estiver no env (evita falsos negativos
em CI sem credencial) E se a Voyage recusar por 429/5xx.

⚠️ POR QUE OS 5 TESTES SEMANTICOS PULAM COM FREQUENCIA (medido 21/08/26). A chave
do `.env` e `pa-`, presa no free tier de **3 RPM** — ver
[[reference_voyage_rate_limits]], que ja registra: *"em uso real (~1 query/msg)
nunca bate limite; so em testes batch"*. O fixture cria 3 memorias, e 3 chamadas
em rajada estouram a cota; o rate limit e da NOSSA conta, nao um defeito do
codigo.

Antes disso os dois testes ficavam VERMELHOS por indisponibilidade externa, e
vermelho cronico ensina a ignorar vermelho — o mesmo mal do `daily-sync` que saia
`error` 13 dias seguidos sem perder dado. `embed_sync.indisponivel` distingue "a
API recusou" de "o vetor veio errado": no primeiro caso pula dizendo por que, no
segundo continua falhando, porque ai o defeito e nosso.

Os testes que NAO dependem da API (literal pgvector, extensao instalada, coluna
`embedding` presente) rodam sempre — o arquivo nao vira skip integral.

Pra roda-los de verdade: espacar as chamadas ou destravar o tier (a memoria
explica como; o gargalo e RPM, nao custo).

Rodar:
    python -m pytest tests/test_f6_vector_search.py -v
"""
import os
import sys

import pytest

# Bootstrap path pro mesmo padrao do test_circulos
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

# Carrega .env se nao foi carregado ainda (rodar pytest sem ./dev.sh)
try:
    from dotenv import load_dotenv
    here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(os.path.dirname(here), ".env"))
    load_dotenv(os.path.join(os.path.dirname(here), ".env.local"), override=False)
except ImportError:
    pass

# O alvo do banco é decidido em tests/conftest.py (`_travar_no_banco_local`), que
# roda ANTES deste módulo. A guarda que existia aqui vinha depois do `load_dotenv`
# acima e por isso nunca disparava: dizia "Force banco local" e a suíte inteira
# rodava contra o Neon de PRODUÇÃO — inclusive o `save_system_memory` e o DELETE
# do fixture abaixo. Não reintroduzir a checagem aqui: `load_dotenv` sem
# `override=True` já não desfaz o que o conftest deixou no ambiente.


requires_voyage = pytest.mark.skipif(
    not os.getenv("VOYAGE_API_KEY", "").strip(),
    reason="VOYAGE_API_KEY not set — skipping semantic test",
)


@pytest.fixture(scope="module")
def memory_ids():
    """Cria 3 memorias de teste e remove no teardown.

    ESCOPO `module` DE PROPOSITO (21/08/26). Como `function`, as 3 memorias eram
    recriadas pra CADA um dos 5 testes que pedem o fixture — 15 chamadas ao
    Voyage numa rajada, o que rendia `429 Too Many Requests` e dois vermelhos
    cronicos na suite. Nao eram defeito nosso, mas vermelho cronico ensina a
    ignorar vermelho: e o mesmo mal do `daily-sync` que saia `error` 13 dias
    seguidos sem perder dado. Com `module`, sao 3 chamadas por rodada.

    Os testes que usam o fixture so LEEM as memorias, entao compartilhar e
    seguro; quem escrever nelas tem que voltar pra `function`.

    Memorias com mesma ideia central (cansaco/exaustao) mas palavras diferentes —
    teste valida que semantic acha as 3 mesmo quando query usa palavra ausente
    em todas elas (ex: 'fatigado').
    """
    from services.system_memory import save_system_memory
    from database import get_db

    ids = []
    fixtures = [
        ("Saí drenado da call com fornecedor", "Reunião pesada, terminei sem energia. Tema recorrente."),
        ("Domingo de novo cansado de mais", "Notei que após semana de muita reunião, o corpo cobra. Padrão claro."),
        ("Acordei exausto antes da viagem", "Mesmo após 8h de sono, peso de exaustão. Possível ansiedade antecipatória."),
    ]
    from services.embeddings import embed_sync

    for titulo, conteudo in fixtures:
        mid = save_system_memory(
            titulo=titulo,
            conteudo=conteudo,
            tipo="reflexao",
            tags=["test_f6"],
            fonte="test",
        )
        assert mid is not None, "save_system_memory returned None — embedding/insert failed"
        ids.append(mid)

    # A verificação vem DEPOIS de gravar, não antes: o 429 da Voyage é
    # intermitente sob rajada — uma sonda prévia passava e as chamadas seguintes
    # estouravam, deixando as memórias sem embedding e o teste vermelho por
    # indisponibilidade externa. `indisponivel` (ver embed_sync) diz se a API
    # recusou; nesse caso não há medição possível, e acusar defeito nosso é o
    # vermelho crônico que ensina a ignorar vermelho.
    if embed_sync.indisponivel:
        _apagar(ids)
        pytest.skip("Voyage recusou (429/5xx) durante o setup — sem medição possível")

    yield ids

    # Teardown
    _apagar(ids)


def _apagar(ids):
    if not ids:
        return
    from database import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM system_memories WHERE id = ANY(%s::int[])", (ids,))
        conn.commit()


@requires_voyage
def test_embed_sync_returns_correct_dims():
    """embed_sync devolve vetor com 1024 floats (voyage-4-lite default)."""
    from services.embeddings import embed_sync, VOYAGE_DIMS

    vec = embed_sync("o renato saiu drenado da reuniao")
    if vec is None and embed_sync.indisponivel:
        pytest.skip("Voyage recusou (429/5xx) — indisponibilidade externa, não defeito nosso")
    assert vec is not None, (
        "embed_sync devolveu None com a API respondendo — resposta malformada "
        "ou chave inválida, e aí o defeito é nosso")
    assert isinstance(vec, list)
    assert len(vec) == VOYAGE_DIMS == 1024
    assert all(isinstance(x, float) for x in vec)


@requires_voyage
def test_embedding_saved_in_db(memory_ids):
    """Apos save_system_memory, coluna embedding nao eh NULL."""
    from database import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, embedding IS NOT NULL AS has_emb FROM system_memories WHERE id = ANY(%s::int[])",
            (memory_ids,),
        )
        rows = cur.fetchall()

    assert len(rows) == 3
    for r in rows:
        assert r["has_emb"] is True, f"Memoria #{r['id']} ficou sem embedding"


@requires_voyage
def test_semantic_search_finds_synonyms(memory_ids):
    """Buscar 'fatigado' (palavra que nao aparece em nenhuma das 3) deve achar
    as 3 via semantic — todas falam de cansaco/exaustao."""
    from services.system_memory import search_memories

    results = search_memories("fatigado", limit=20, mode="semantic")
    found_ids = {r["id"] for r in results}

    # As 3 fixtures devem estar no top — pode haver outras memorias antigas tb,
    # entao validamos que NOSSAS 3 estao no resultado.
    for mid in memory_ids:
        assert mid in found_ids, (
            f"Semantic search por 'fatigado' nao achou memoria #{mid}. "
            f"Found: {found_ids}"
        )


@requires_voyage
def test_keyword_search_misses_synonyms(memory_ids):
    """Sanity check: 'fatigado' por keyword NAO acha nenhuma das 3 (mostra
    o problema que motivou F6)."""
    from services.system_memory import search_memories

    results = search_memories("fatigado", limit=20, mode="keyword")
    found_ids = {r["id"] for r in results}

    for mid in memory_ids:
        assert mid not in found_ids, (
            f"Keyword search achou memoria #{mid} mesmo sem ter a palavra 'fatigado'. "
            f"Resultado inesperado — checar fixtures."
        )


@requires_voyage
def test_hybrid_search_dedup(memory_ids):
    """Hybrid combina keyword + semantic SEM duplicar ids."""
    from services.system_memory import search_memories

    # Query que casa keyword em 1 fixture (drenado) E semantic nas 3
    results = search_memories("drenado", limit=20, mode="hybrid")
    ids = [r["id"] for r in results]
    assert len(ids) == len(set(ids)), f"Hybrid retornou duplicatas: {ids}"

    # Pelo menos a fixture com 'drenado' tem que estar la
    found_ids = set(ids)
    assert memory_ids[0] in found_ids, "Hybrid perdeu match keyword obvio"


@requires_voyage
def test_hybrid_falls_back_when_no_api_key(monkeypatch, memory_ids):
    """Sem API key, hybrid degrada pra keyword (nao explode)."""
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    # Tem que recarregar a flag — embeddings.is_enabled le env toda chamada,
    # entao basta passar pelo monkeypatch.

    from services.system_memory import search_memories

    results = search_memories("drenado", limit=20, mode="hybrid")
    # Keyword puro acha so a 1a fixture
    ids = {r["id"] for r in results}
    assert memory_ids[0] in ids


# ----- Tests independentes de Voyage (rodam sempre) -----

def test_embedding_to_pg_literal_format():
    """Validacao de formato do literal pgvector (smoke nao depende de API)."""
    from services.embeddings import embedding_to_pg_literal

    s = embedding_to_pg_literal([0.1, -0.2, 1.5])
    assert s.startswith("[") and s.endswith("]")
    assert "0.100000" in s
    assert "-0.200000" in s
    assert "1.500000" in s


def test_pgvector_extension_present():
    """Sanity: pgvector instalado no DB local — sem isso, F6 nao roda."""
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
    from database import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT extname FROM pg_extension WHERE extname='vector'")
        row = cur.fetchone()
    assert row is not None, (
        "pgvector extension nao instalada. Rode migration 006 ou instale pgvector."
    )


def test_embedding_column_exists():
    """Coluna `embedding` existe em system_memories."""
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
    from database import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT column_name, udt_name
            FROM information_schema.columns
            WHERE table_name='system_memories' AND column_name='embedding'
            """
        )
        row = cur.fetchone()
    assert row is not None, "coluna embedding nao existe — rodar migration 006"
    assert row["udt_name"] == "vector", f"coluna embedding tipo errado: {row['udt_name']}"


def test_search_keyword_still_works():
    """search_memories(mode='keyword') continua igual ao antigo (regressao)."""
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
    from services.system_memory import search_memories

    # Aceita zero resultados (banco pode estar vazio); o que importa eh nao explodir
    results = search_memories("xyz_nao_existe_blabla_123", limit=5, mode="keyword")
    assert isinstance(results, list)


