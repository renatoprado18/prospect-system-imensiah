"""#1000265 — a troca de modelo não pode derrubar a classificação em silêncio.

O furo original: `_call_model` devolvia `msg.content[0].text`. Em Haiku 4.5 /
Sonnet 4.6 / Opus 4.7 o bloco 0 É o texto, porque omitir `thinking` significa não
pensar. Em Opus 5 / Sonnet 5 / Fable 5 omitir significa PENSAR: o bloco 0 vira um
`thinking`, `display` vem "omitted" (campo vazio) e `ThinkingBlock.text` não
existe — levanta AttributeError, que o `except Exception` engolia num warning.
Trocar uma constante de modelo em 1 linha derrubava as 23 funções sem erro.

Estes testes travam as três peças da blindagem. Não chamam a API: montam a
resposta que o modelo novo produz e verificam a extração.
"""
import logging

import pytest

from services import llm


# ── blocos-dublê: a forma que o SDK e o JSON cru entregam ────────────────────

class _Block:
    """Bloco do SDK. `thinking` de propósito NÃO tem `.text` — é o furo."""

    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Msg:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


THINKING_VAZIO = _Block("thinking", thinking="", signature="sig")
TEXTO = _Block("text", text='{"decisao": "urgente", "confidence": 0.9}')


def test_thinking_block_nao_tem_text():
    """Controle positivo: se um dia o SDK passar a expor `.text` vazio aqui, o
    resto da blindagem continua certo mas a premissa do memo mudou."""
    with pytest.raises(AttributeError):
        THINKING_VAZIO.text


# ── first_text: varre por tipo, nunca indexa o bloco 0 ──────────────────────

def test_first_text_acha_texto_atras_do_thinking():
    assert llm.first_text(_Msg([THINKING_VAZIO, TEXTO])) == TEXTO.text


def test_first_text_geracao_antiga_inalterada():
    """Sem thinking o bloco 0 é o texto — comportamento idêntico ao de antes."""
    assert llm.first_text(_Msg([TEXTO])) == TEXTO.text


def test_first_text_aceita_json_cru():
    """Os ~35 call sites HTTP do repo usam `resp.json()`, não o SDK."""
    payload = {"content": [{"type": "thinking", "thinking": ""},
                           {"type": "text", "text": "ok"}]}
    assert llm.first_text(payload) == "ok"


def test_first_text_sem_bloco_de_texto_e_none():
    """Teto estourado dentro do raciocínio: NÃO é resposta vazia, é falha.

    Devolver "" aqui faria a falha passar por classificação legítima.
    """
    assert llm.first_text(_Msg([THINKING_VAZIO], stop_reason="max_tokens")) is None
    assert llm.first_text(_Msg([])) is None
    assert llm.first_text(_Msg(None)) is None


# ── thinks_by_default: quem pensa sem ser mandado ───────────────────────────

@pytest.mark.parametrize("model", [
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
])
def test_geracao_5_pensa_por_default(model):
    assert llm.thinks_by_default(model) is True


@pytest.mark.parametrize("model", [
    llm.FAST, llm.BALANCED, llm.DEEP,
    "claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8",
])
def test_modelos_atuais_nao_pensam_por_default(model):
    """Inclui as 3 constantes vivas: a blindagem não muda nada hoje."""
    assert llm.thinks_by_default(model) is False


def test_thinks_by_default_tolera_lixo():
    assert llm.thinks_by_default("") is False
    assert llm.thinks_by_default(None) is False
    assert llm.thinks_by_default("  claude-opus-5  ") is True


# ── piso de max_tokens: o default de 200 não sobrevive ao thinking ──────────

def test_piso_sobe_max_tokens_no_modelo_que_pensa(monkeypatch, caplog):
    visto = {}

    def fake_create(**kw):
        visto.update(kw)
        return _Msg([THINKING_VAZIO, TEXTO])

    _instalar_cliente_falso(monkeypatch, fake_create)
    with caplog.at_level(logging.WARNING, logger="services.llm"):
        out = llm._call_model("claude-opus-5", "classifique isto")

    assert out == TEXTO.text, "classificação tem de sobreviver à troca de modelo"
    assert visto["max_tokens"] == llm.MIN_MAX_TOKENS_THINKING
    assert "max_tokens" in caplog.text, "subir o teto calado seria outro furo"


def test_piso_nao_mexe_nos_modelos_atuais(monkeypatch):
    visto = {}

    def fake_create(**kw):
        visto.update(kw)
        return _Msg([TEXTO])

    _instalar_cliente_falso(monkeypatch, fake_create)
    assert llm._call_model(llm.FAST, "p") == TEXTO.text
    assert visto["max_tokens"] == 200, "default histórico preservado"


def test_resposta_sem_texto_loga_ERROR_e_devolve_none(monkeypatch, caplog):
    """A falha tem de gritar. Um `warning` a mais não muda decisão nenhuma."""
    _instalar_cliente_falso(
        monkeypatch,
        lambda **kw: _Msg([THINKING_VAZIO], stop_reason="max_tokens"),
    )
    with caplog.at_level(logging.ERROR, logger="services.llm"):
        out = llm._call_model(llm.BALANCED, "p")

    assert out is None
    erros = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert erros, "sem bloco de texto tem de sair como ERROR, não warning"
    assert "thinking" in erros[0].getMessage()
    assert "max_tokens" in erros[0].getMessage(), "stop_reason precisa aparecer"


# ── plumbing ────────────────────────────────────────────────────────────────

def _instalar_cliente_falso(monkeypatch, fake_create):
    """Troca o SDK por um dublê. `_call_model` importa `anthropic` lá dentro."""
    import sys
    import types

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")

    class _Messages:
        def create(self, **kw):
            return fake_create(**kw)

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
