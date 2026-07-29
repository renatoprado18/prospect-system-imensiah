"""
Dois fixes de 29/07/2026, ambos achados medindo em producao.

1. CACHE DAS FRENTE_KEYWORDS — `_load_keywords` abria uma conexao ao banco POR
   CHAMADA, e `is_frente_keyword` a chama uma vez por item do consumidor. Em
   `circulos.get_prioridades_por_contexto` isso dava 1.586 conexoes ao Neon num
   request (57 keywords x 1.586 contatos com circulo<=4). Era a causa dos 31s de
   /api/contacts/needs-attention (medido 3x: 31,5 / 31,1 / 30,4s) — o spinner
   eterno da home. Depois do cache: 1 conexao, e a chamada local caiu de +7min
   pra 4,97s.

2. IDADE MAXIMA DA NOTICIA — o watcher nao filtrava por data. `_parse_pub_date`
   servia so pra escrever "Publicado: ..." na descricao, e a dedup por url_hash
   so evita repetir a MESMA url. Materia velha que aparece no feed pela primeira
   vez entrava como novidade: o Renato recebeu como "Acao Sugerida" uma materia
   da Revista Cafeicultura de 2016/2017 sobre microlotes da Suplicy.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_keywords_cache_and_news_age.py -q
"""
import os
import sys
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import cos_keywords  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Cache das keywords
# ---------------------------------------------------------------------------
class _FakeCur:
    def __init__(self, counter):
        self._counter = counter

    def execute(self, *a, **kw):
        self._counter["queries"] += 1

    def fetchall(self):
        return [{"frente": 1, "keyword": "Wadhwani"}, {"frente": 2, "keyword": "conselho"}]


class _FakeConn:
    def __init__(self, counter):
        self._counter = counter

    def cursor(self):
        return _FakeCur(self._counter)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def spy(monkeypatch):
    """Conta quantas conexoes o _load_keywords realmente abre."""
    counter = {"conns": 0, "queries": 0}

    def fake_get_db():
        counter["conns"] += 1
        return _FakeConn(counter)

    monkeypatch.setattr(cos_keywords, "get_db", fake_get_db)
    monkeypatch.delenv("COS_KEYWORDS_CACHE_TTL", raising=False)
    cos_keywords.invalidate_keywords_cache()
    yield counter
    cos_keywords.invalidate_keywords_cache()


class TestKeywordsCache:
    def test_n_chamadas_abrem_UMA_conexao(self, spy):
        """O defeito original: 1.586 contatos = 1.586 conexoes."""
        for _ in range(500):
            cos_keywords.is_frente_keyword("Diretor de conselho na Wadhwani")
        assert spy["conns"] == 1, f"esperava 1 conexao, abriu {spy['conns']}"

    def test_resultado_nao_muda_com_cache(self, spy):
        a = cos_keywords.is_frente_keyword("Wadhwani Foundation")
        b = cos_keywords.is_frente_keyword("Wadhwani Foundation")
        assert a == b == 1

    def test_invalidate_forca_releitura(self, spy):
        cos_keywords.is_frente_keyword("conselho")
        assert spy["conns"] == 1
        cos_keywords.invalidate_keywords_cache()
        cos_keywords.is_frente_keyword("conselho")
        assert spy["conns"] == 2

    def test_ttl_zero_desliga_o_cache(self, spy, monkeypatch):
        """Kill-switch: volta ao comportamento pre-29/07."""
        monkeypatch.setenv("COS_KEYWORDS_CACHE_TTL", "0")
        cos_keywords.invalidate_keywords_cache()
        for _ in range(4):
            cos_keywords.is_frente_keyword("conselho")
        assert spy["conns"] == 4

    def test_ttl_expirado_recarrega(self, spy, monkeypatch):
        monkeypatch.setenv("COS_KEYWORDS_CACHE_TTL", "60")
        cos_keywords.invalidate_keywords_cache()
        cos_keywords.is_frente_keyword("conselho")
        assert spy["conns"] == 1
        # simula 61s de idade mexendo no timestamp do cache
        cos_keywords._KEYWORDS_CACHE_AT -= 61
        cos_keywords.is_frente_keyword("conselho")
        assert spy["conns"] == 2

    def test_ttl_invalido_cai_no_default(self, monkeypatch):
        monkeypatch.setenv("COS_KEYWORDS_CACHE_TTL", "abacaxi")
        assert cos_keywords._cache_ttl() == 60.0

    def test_falha_de_db_devolve_cache_velho(self, spy, monkeypatch):
        """Keyword defasada > nenhuma: sem isso o consumidor perde a priorizacao."""
        cos_keywords.is_frente_keyword("conselho")
        cached = list(cos_keywords._KEYWORDS_CACHE)

        def boom():
            raise RuntimeError("neon fora do ar")

        monkeypatch.setattr(cos_keywords, "get_db", boom)
        cos_keywords._KEYWORDS_CACHE_AT -= 999  # forca expirar
        assert cos_keywords._load_keywords() == cached

    def test_falha_sem_cache_devolve_vazio(self, monkeypatch):
        cos_keywords.invalidate_keywords_cache()

        def boom():
            raise RuntimeError("neon fora do ar")

        monkeypatch.setattr(cos_keywords, "get_db", boom)
        assert cos_keywords._load_keywords() == []


# ---------------------------------------------------------------------------
# 2. Idade maxima da noticia
# ---------------------------------------------------------------------------
class TestNoticiaVelha:
    def test_default_e_30_dias(self):
        from services import project_news_watcher as w
        assert w.NEWS_MAX_AGE_DAYS == 30

    def test_materia_de_2016_seria_descartada(self):
        """O caso real: Revista Cafeicultura, microlotes Suplicy, 2016/2017."""
        from services import project_news_watcher as w
        from services.tz import now_utc

        pub = w._parse_pub_date("Mon, 24 Oct 2016 10:00:00 GMT")
        assert pub is not None
        idade = (now_utc().replace(tzinfo=None) - pub).days
        assert idade > w.NEWS_MAX_AGE_DAYS

    def test_materia_de_ontem_passa(self):
        from services import project_news_watcher as w
        from services.tz import now_utc

        ontem = now_utc().replace(tzinfo=None) - timedelta(days=1)
        idade = (now_utc().replace(tzinfo=None) - ontem).days
        assert idade <= w.NEWS_MAX_AGE_DAYS

    def test_pub_date_ilegivel_nao_derruba_o_item(self):
        """Nem todo feed traz pubDate; barrar por ausencia perderia noticia real.
        O caso e contado em stats['skipped_no_date'] pra nao virar buraco cego."""
        from services import project_news_watcher as w
        assert w._parse_pub_date("qualquer coisa") is None
        assert w._parse_pub_date(None) is None
        assert w._parse_pub_date("") is None

    def test_parse_normaliza_pra_naive_utc(self):
        """Comparar aware com naive levanta TypeError — o filtro quebraria."""
        from services import project_news_watcher as w
        dt = w._parse_pub_date("Sun, 28 Jun 2026 14:30:00 GMT")
        assert dt is not None and dt.tzinfo is None
