"""O BACKFILL GASTAVA VISÃO PAGA SEM APARECER EM MEDIDOR (05/10/2026).

`scripts/wa_media_backfill.py` chama a visão da Anthropic direto pelo SDK, fora do
worker — e por isso saltava o único ponto de gravação do lado-INTEL
(`services.llm_usage.record_response`). O pipeline vivo não tem esse furo: o
`/analyze-image` do worker registra `worker.image_analyze`, e são 1.060 chamadas /
US$ 2,79 atribuídos nos últimos 30 dias. O backfill caía no balde anônimo
`llm:anthropic-platform` do `capability_registry`.

Por que isso não é contabilidade: o registry deriva custo-POR-FUNÇÃO lendo o
`endpoint` de `tonia_llm_usage`, e é esse número que falta como DENOMINADOR do
`value_ratio` (P1 #1000266, NULL nas 23 funções LLM). Consumo que não aparece não
pode ser dividido por valor nenhum.

⚠️ POR QUE ESTE TESTE USA STUB E NÃO CHAMADA REAL: em 04/10/2026 às ~08h a conta
Anthropic ficou SEM SALDO (canary `no_credits`, alerta enviado ao Renato 08:33, 0
chamadas LLM em prod desde então). Validar por chamada real era impossível no dia
do conserto. O stub cobre o que de fato pode quebrar aqui — o SHAPE do que se
passa pro medidor e o nome do endpoint —, e o `record_response` já é exercido em
produção por 9 outros call-sites.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_backfill_registra_custo_visao.py -q
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))

_SCRIPT = _ROOT / "scripts" / "wa_media_backfill.py"


def _carrega_backfill():
    spec = importlib.util.spec_from_file_location("wa_media_backfill", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Bloco:
    def __init__(self, tipo, texto=None, pensamento=None):
        self.type = tipo
        if texto is not None:
            self.text = texto
        if pensamento is not None:
            self.thinking = pensamento


class _RespostaFake:
    """Imita o objeto do SDK: `.content` com blocos e `.model_dump()` com `usage`.

    O bloco 0 é `thinking` DE PROPÓSITO — é a forma real da geração 5, e o furo
    do `content[0]` (#1000265) é o motivo de `extrai_imagem` varrer por tipo em
    vez de indexar.
    """

    def __init__(self, usage=None, texto="CONTEUDO EXTRAIDO"):
        self.content = [
            _Bloco("thinking", pensamento="analisando a imagem..."),
            _Bloco("text", texto=texto),
        ]
        self._usage = usage if usage is not None else {
            "input_tokens": 1500,
            "output_tokens": 420,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    def model_dump(self):
        return {
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "thinking"}, {"type": "text", "text": "x"}],
            "usage": self._usage,
        }


@pytest.fixture
def backfill_com_stub(monkeypatch, tmp_path):
    """Carrega o script e troca o SDK da Anthropic por um fake. Captura o que
    chega em `llm_usage.record_response` em vez de escrever no banco."""
    mod = _carrega_backfill()
    registrado = {}

    class _Messages:
        def create(self, **kwargs):
            registrado["kwargs_api"] = kwargs
            return _RespostaFake()

    class _ClienteFake:
        def __init__(self, **_):
            self.messages = _Messages()

    import types
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = _ClienteFake
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    from services import llm_usage

    def _fake_record_response(function, model, response_json, **kw):
        registrado["function"] = function
        registrado["model"] = model
        registrado["response_json"] = response_json
        registrado["metadata"] = kw.get("metadata")
        return llm_usage.compute_cost(
            model,
            **{k: v for k, v in llm_usage._extract_usage(response_json).items()}
        )

    monkeypatch.setattr(llm_usage, "record_response", _fake_record_response)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "chave-de-teste")

    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return mod, registrado, str(img)


def test_registra_no_endpoint_proprio_do_backfill(backfill_com_stub):
    """Endpoint separado do `worker.image_analyze` de propósito: somar lote
    excepcional com regime esconderia que são duas funções diferentes."""
    mod, reg, img = backfill_com_stub
    mod.extrai_imagem(img, "image/png", "", "MSGID123")
    assert reg["function"] == "backfill.wa_image_vision"
    assert reg["function"] != "worker.image_analyze"


def test_passa_o_usage_real_e_nao_um_dict_vazio(backfill_com_stub):
    """O que mais podia quebrar calado: mandar pro medidor um payload sem `usage`.

    `record_response` é best-effort e engole erro — então um shape errado NÃO
    levanta: gravaria uma linha de custo ZERO, que no medidor se lê como "de
    graça". Custo ausente é lacuna conhecida; custo zero é mentira.
    """
    mod, reg, img = backfill_com_stub
    mod.extrai_imagem(img, "image/png", "", "MSGID123")
    usage = (reg["response_json"] or {}).get("usage") or {}
    assert usage.get("input_tokens") == 1500
    assert usage.get("output_tokens") == 420

    from services import llm_usage
    custo = llm_usage.compute_cost("claude-haiku-4-5-20251001", 1500, 420)
    assert custo > 0, "haiku a 1.00/5.00 por milhão não pode custar zero"


def test_metadata_leva_o_message_id_pra_rastrear_o_anexo(backfill_com_stub):
    """A coluna `wa_attachments.extraction_cost_usd` NÃO tem leitor (medido em
    05/10: 1 escritor, 0 consumidores), então a rastreabilidade por anexo tem de
    viver na tabela que TEM leitor — aqui, no metadata da linha de usage."""
    mod, reg, img = backfill_com_stub
    mod.extrai_imagem(img, "image/png", "", "MSGID123")
    assert reg["metadata"]["message_id"] == "MSGID123"
    assert reg["metadata"]["origem"] == "wa_media_backfill"


def test_le_o_texto_mesmo_com_thinking_no_bloco_zero(backfill_com_stub):
    """Guarda do #1000265 neste call-site: indexar `content[0]` devolveria o
    bloco de raciocínio (ou levantaria), e a extração sairia vazia — gravando
    'extracao vazia (provavel escaneado)' sobre uma imagem que tinha texto."""
    mod, reg, img = backfill_com_stub
    texto = mod.extrai_imagem(img, "image/png", "", "MSGID123")
    assert texto == "CONTEUDO EXTRAIDO"


def test_falha_do_medidor_nao_derruba_a_extracao(backfill_com_stub, monkeypatch):
    """Telemetria nunca quebra o lote: um backfill de centenas de imagens não
    pode morrer no meio porque a tabela de usage sumiu no alvo."""
    mod, reg, img = backfill_com_stub
    from services import llm_usage

    def _explode(*a, **kw):
        raise RuntimeError("tonia_llm_usage indisponivel")

    monkeypatch.setattr(llm_usage, "record_response", _explode)
    texto = mod.extrai_imagem(img, "image/png", "", "MSGID123")
    assert texto == "CONTEUDO EXTRAIDO"


def test_a_legenda_entra_no_pedido_quando_existe(backfill_com_stub):
    """Controle positivo do stub: se o fake não estivesse interceptando a chamada
    de verdade, nada aqui falharia e os testes acima passariam vazios."""
    mod, reg, img = backfill_com_stub
    mod.extrai_imagem(img, "image/png", "Planilha de custos do talhao 3", "M1")
    blocos = reg["kwargs_api"]["messages"][0]["content"]
    texto_pedido = [b for b in blocos if b.get("type") == "text"][0]["text"]
    assert "Planilha de custos do talhao 3" in texto_pedido
    assert reg["kwargs_api"]["model"] == mod.MODELO_VISAO
