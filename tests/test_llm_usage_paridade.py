"""Os dois medidores de custo têm que concordar sobre preço.

Há duas cópias do registro de custo LLM, e a duplicação é legítima: o worker é
um serviço Railway de build isolado (`Procfile` próprio, `requirements.txt` sem
as deps do INTEL, `app/` fora do container) e usa psycopg3, enquanto o INTEL usa
psycopg2. Dois INSERTs, dois drivers — tudo bem.

O que NÃO pode divergir é a tabela de preços. Dois preços que discordam produzem
dois relatórios que discordam, e nenhum dos dois avisa: some no total, aparece
como "o worker é mais barato". Este teste é o que impede o bump de preço de
entrar num arquivo só.

Contexto (10/08/2026): antes disso o worker não registrava NADA — 8 chamadas ao
Claude (bot por texto, transcrição, imagem, PDF, ata) invisíveis para o cost
tracker, que reportava um número sem sinalizar o que excluía.

Rodar: .venv/bin/python -m pytest tests/test_llm_usage_paridade.py -v
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_INTEL = _ROOT / "app" / "services" / "llm_usage.py"
_WORKER = _ROOT / "workers" / "audio-transcriber" / "llm_usage.py"


def _carrega(nome, caminho):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nome] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def modulos():
    return _carrega("_llm_intel", _INTEL), _carrega("_llm_worker", _WORKER)


def test_tabela_de_precos_identica(modulos):
    """Preço divergente = dois relatórios que discordam, nenhum deles avisando."""
    intel, worker = modulos
    assert worker._PRICING == intel._PRICING, (
        "as tabelas de preço divergiram — ao bumpar modelo, mude os DOIS arquivos:\n"
        f"  INTEL : {intel._PRICING}\n"
        f"  worker: {worker._PRICING}"
    )


def test_mesmo_fallback_conservador(modulos):
    """Modelo desconhecido nunca pode zerar custo — some da conta sem sinal."""
    intel, worker = modulos
    assert worker._DEFAULT_TIER == intel._DEFAULT_TIER
    assert worker.compute_cost("modelo-que-nao-existe", input_tokens=1_000_000) > 0


@pytest.mark.parametrize("model,tin,tout", [
    ("claude-haiku-4-5-20251001", 10_000, 500),
    ("claude-sonnet-4-20250514", 250_000, 3_000),
    ("claude-sonnet-4-6", 8_000, 1_200),
    ("claude-opus-4-7", 1_000, 100),
])
def test_mesmo_custo_para_a_mesma_chamada(modulos, model, tin, tout):
    """A mesma chamada tem que custar o mesmo nos dois lados."""
    intel, worker = modulos
    assert worker.compute_cost(model, tin, tout) == intel.compute_cost(model, tin, tout)


def test_extrai_o_usage_do_mesmo_jeito(modulos):
    """A API devolve cache_read_input_tokens; a tabela guarda cache_read_tokens.
    Se um lado esquecer o de-para, o cache vira zero e o custo sai subestimado."""
    intel, worker = modulos
    resposta = {"usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}}
    assert worker._extract_usage(resposta) == intel._extract_usage(resposta)
    assert worker._extract_usage(resposta)["cache_read_tokens"] == 7


def test_worker_nao_grava_linha_vazia(modulos):
    """Resposta sem usage não vira linha de custo zero: a média mentiria pra
    baixo e a chamada pareceria de graça."""
    _, worker = modulos
    assert worker.record_response("teste.vazio", "claude-sonnet-4-6", {"usage": {}}) is None
    assert worker.record_response("teste.vazio", "claude-sonnet-4-6", {}) is None


def test_telemetria_nao_quebra_o_caller(modulos, monkeypatch):
    """Se o banco estiver fora, a chamada de LLM segue viva — o custo é
    best-effort, nunca um ponto de falha no caminho do bot."""
    _, worker = modulos
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalido:1/x")
    resposta = {"usage": {"input_tokens": 10, "output_tokens": 5}, "model": "claude-sonnet-4-6"}
    assert worker.record_response("teste.erro", "claude-sonnet-4-6", resposta) is None
