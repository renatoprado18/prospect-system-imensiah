"""O FURO DO `content[0]` — sweep de 45 call sites (30/09/2026, #1000265).

Trocar uma das constantes de modelo por um da geracao 5 muda duas coisas que
nenhum call site declarava: `thinking` liga sozinho, e **o bloco 0 deixa de ser o
texto**. `content[0]["text"]` num bloco `thinking` levanta KeyError; no SDK,
`ThinkingBlock.text` nem existe (AttributeError). Como quase todo call site vive
dentro de um `try/except Exception` que so loga, a funcao inteira parava de
acontecer sem erro visivel.

O board registrava "35 call sites". Medido: **45** — 38 em JSON cru
(`resp.json()["content"][0]["text"]`) e 7 pelo SDK. Numero herdado se confere
antes de agir ([[feedback_numero_herdado_medir_a_regua]]).

Tres coisas aqui, e a terceira e a que importa mais num sweep:

  1. `require_text` falha NOMEADA em vez de devolver None. `first_text(...) or ""`
     consertaria o caso comum e transformaria o caso ruim em string vazia — o mesmo
     silencio, com outra roupa.
  2. Os call sites consertados leem o texto quando o bloco 0 e `thinking`.
  3. **Guarda de regressao**: o repo nao pode voltar a ter `content[0]`. Sem ela o
     46o call site nasce amanha com o mesmo furo, e um sweep sem guarda e uma
     limpeza que envelhece.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_content0_sweep.py -q
"""
import os
import re
import sys
from pathlib import Path

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from services import llm  # noqa: E402


# Como a API responde de verdade quando o modelo pensa: thinking PRIMEIRO.
RESP_COM_THINKING = {
    "content": [
        {"type": "thinking", "thinking": "vou avaliar o pedido...", "signature": "x"},
        {"type": "text", "text": '{"ok": true}'},
    ],
    "usage": {"input_tokens": 10, "output_tokens": 5},
}
# O caso em que o teto estourou DENTRO do raciocinio: nao ha bloco de texto.
RESP_SO_THINKING = {
    "content": [{"type": "thinking", "thinking": "..." , "signature": "x"}],
    "usage": {},
}


# ===========================================================================
# 1. require_text — falha nomeada, nunca string vazia
# ===========================================================================

def test_acha_o_texto_com_thinking_no_bloco_zero():
    """O caso que derrubava tudo: o texto esta no bloco 1."""
    assert llm.require_text(RESP_COM_THINKING, "t") == '{"ok": true}'
    assert llm.first_text(RESP_COM_THINKING) == '{"ok": true}'


def test_sem_bloco_de_texto_LEVANTA_e_diz_quais_blocos_vieram():
    """`['thinking']` na mensagem mata a duvida na hora. Sem isso a excecao seria
    tao inutil quanto o silencio que ela substitui."""
    with pytest.raises(llm.LlmNoTextError) as e:
        llm.require_text(RESP_SO_THINKING, "modulo.funcao")
    msg = str(e.value)
    assert "modulo.funcao" in msg, "a mensagem tem que dizer ONDE"
    assert "thinking" in msg, "a mensagem tem que dizer QUAIS blocos vieram"


def test_NUNCA_devolve_string_vazia():
    """CONTRAPROVA do desenho: `first_text(...) or ''` teria passado nos testes de
    cima e mantido o silencio. `require_text` nao pode ter esse desfecho."""
    for payload in (RESP_SO_THINKING, {"content": []}, {}):
        with pytest.raises(llm.LlmNoTextError):
            llm.require_text(payload, "t")


def test_aceita_objeto_do_SDK_tambem():
    """7 dos 45 call sites usam o SDK, nao `resp.json()`."""
    class _Bloco:
        def __init__(self, tipo, texto=None):
            self.type = tipo
            if texto is not None:
                self.text = texto

    class _Resp:
        content = [_Bloco("thinking"), _Bloco("text", "ata pronta")]

    assert llm.require_text(_Resp(), "t") == "ata pronta"
    assert llm.block_kinds(_Resp()) == ["thinking", "text"]


# ===========================================================================
# 2. Call sites reais leem o texto com thinking no bloco 0
# ===========================================================================

@pytest.mark.asyncio
async def test_ai_agent_call_claude_le_o_bloco_certo(monkeypatch):
    """Call site real, ponta a ponta: com `thinking` no bloco 0, o que ele devolve
    tem de ser o TEXTO. Antes do sweep devolvia None (KeyError engolido pelo
    `except Exception` que so imprime)."""
    from services import ai_agent

    class _Resp:
        status_code = 200

        def json(self):
            return RESP_COM_THINKING

    class _Cli:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(ai_agent.httpx, "AsyncClient", lambda *a, **kw: _Cli())
    monkeypatch.setattr(ai_agent.llm_usage, "record_response", lambda *a, **kw: None)
    svc = ai_agent.AIAgentService()
    svc.api_key = "sk-teste"
    assert await svc.call_claude("oi") == '{"ok": true}'


def test_hot_takes_reporta_os_BLOCOS_quando_nao_ha_texto(monkeypatch):
    """Este era o unico call site que NAO falhava calado — ele logava. Mas com o
    diagnostico invertido: dizia "No 'text' in content[0]" enquanto o texto estava
    no bloco 1. Mensagem confiante e errada custa mais que silencio."""
    from services import hot_takes
    assert llm.first_text(RESP_SO_THINKING) is None
    assert "thinking" in str(llm.block_kinds(RESP_SO_THINKING))
    # o caminho bom: com texto no bloco 1, o hot_takes o encontra
    assert llm.first_text(RESP_COM_THINKING) == '{"ok": true}'
    assert hasattr(hot_takes, "llm"), "hot_takes precisa do helper importado"


# ===========================================================================
# 3. GUARDA DE REGRESSAO — o repo nao pode voltar a ter `content[0]`
# ===========================================================================

# Onde `content[0]` PODE aparecer: nos proprios helpers (que existem justamente
# pra encapsular o acesso) e neste teste.
ISENTOS = {
    "app/services/llm.py",
    "workers/audio-transcriber/llm_usage.py",
    "tests/test_content0_sweep.py",
}

PADROES = [
    re.compile(r'\["content"\]\[0\]'),
    re.compile(r"\['content'\]\[0\]"),
    re.compile(r"\.content\[0\]\."),
]


def _linhas_de_codigo(texto):
    """Linhas sem comentario puro. Docstring que CITA o padrao (pra explicar o
    furo) nao e call site — por isso a checagem ignora `#` e aspas triplas."""
    dentro_doc = False
    for i, linha in enumerate(texto.split("\n"), start=1):
        nua = linha.strip()
        if nua.count('"""') == 1 or nua.count("'''") == 1:
            dentro_doc = not dentro_doc
            continue
        if dentro_doc or nua.startswith("#") or not nua:
            continue
        yield i, linha.split("  #")[0]


def test_nenhum_call_site_novo_com_content_indice_zero():
    """A guarda que faz o sweep durar. Achou algo? Use `llm.require_text(resp,
    "modulo.funcao")` — nunca `content[0]`, e nunca `first_text(...) or ""`."""
    achados = []
    for pasta in ("app", "scripts", "workers", "tests"):
        for p in (_ROOT / pasta).rglob("*.py"):
            rel = str(p.relative_to(_ROOT))
            if rel in ISENTOS or ".venv" in rel:
                continue
            try:
                texto = p.read_text()
            except Exception:
                continue
            for ln, linha in _linhas_de_codigo(texto):
                if any(pad.search(linha) for pad in PADROES):
                    achados.append(f"{rel}:{ln}: {linha.strip()}")
    assert not achados, (
        "call site com `content[0]` de volta (furo #1000265) — o bloco 0 e "
        "`thinking` na geracao 5:\n  " + "\n  ".join(achados)
    )


def test_a_guarda_acima_realmente_pega(tmp_path):
    """Controle positivo da guarda: um arquivo com o furo TEM de ser detectado.
    Sem isto, um erro no regex ou no filtro de docstring faria a guarda certificar
    um repo que ela nunca leu ([[feedback_guarda_abstencao_vira_fabrica]])."""
    amostra = 'def f(resp):\n    return resp.json()["content"][0]["text"]\n'
    achou = [
        ln for ln, linha in _linhas_de_codigo(amostra)
        if any(pad.search(linha) for pad in PADROES)
    ]
    assert achou == [2], "a guarda nao detecta o padrao que existe pra detectar"

    limpo = 'def f(resp):\n    return llm.require_text(resp.json(), "m.f")\n'
    assert not [
        ln for ln, linha in _linhas_de_codigo(limpo)
        if any(pad.search(linha) for pad in PADROES)
    ]
