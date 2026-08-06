"""As páginas de ~/cockpit têm que ter JavaScript VÁLIDO.

Em 04/08/2026 o Renato marcou os vereditos na fila de fatos e nada foi
registrado. O HTML estava perfeito — cards, `data-uid`, botões, os quatro
elementos que o script procura, tudo no lugar. O que estava quebrado era o JS:

    const txt=L.join("
                     ^  SyntaxError: Invalid or unexpected token

O `\\n` do `join` foi escrito certo no arquivo, mas a string do template era
`JS = \"\"\"...\"\"\"` — não-raw. Ao importar o módulo, Python resolveu o escape e
transformou em quebra de linha REAL, partindo a string ao meio. Um SyntaxError
mata o script INTEIRO: nenhum `addEventListener` chega a registrar, então clicar
não fazia absolutamente nada — sem erro visível, sem pista.

Inspecionar o HTML não pega isso. Só parsear o JS pega.

Rodar: .venv/bin/python -m pytest tests/test_paginas_cockpit.py -v
"""
import os
import re
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "scripts", "cos_agent"))

import pytest  # noqa: E402


def _modulos():
    import fatos_html
    import placar_html
    return {"fatos_html": fatos_html, "placar_html": placar_html}


@pytest.mark.parametrize("nome", ["fatos_html", "placar_html"])
def test_escape_do_javascript_sobrevive_ao_import(nome):
    """A causa raiz, verificada direto: se `JS` não for raw string, o `\\n` do
    `join` vira newline e a string do JavaScript parte."""
    mod = _modulos()[nome]
    depois_do_join = mod.JS.split('L.join(')[1][:8]
    assert "\n" not in depois_do_join, (
        f"{nome}.JS não é raw string — o \\n do join virou quebra de linha real"
    )


@pytest.mark.parametrize("nome", ["fatos_html", "placar_html"])
def test_javascript_gerado_e_sintaticamente_valido(nome):
    """O teste que teria pego o bug: parseia o JS da página REAL gerada.

    Um SyntaxError derruba todos os listeners de uma vez — o sintoma é
    'clico e não acontece nada', sem erro visível pra quem usa.
    """
    if not shutil.which("node"):
        pytest.skip("node ausente")

    mod = _modulos()[nome]
    itens = [{
        "id": 1, "contato": "Fulano <teste> & cia", "categoria": "professional",
        "fato": 'fato com "aspas", <tag> e & ampersand', "confianca": 0.45,
        "evidencia": [{"tipo": "ficha", "termo": "X", "texto": "#1 Y"}],
        # placar_html usa outras chaves; ambos toleram o extra
        "dia": "2026-08-04", "frente": "Frente & <teste>", "portao": "texto do portão",
        "latencia": None,
    }]
    html = mod.render(itens)
    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)

    r = subprocess.run(["node", "--check", "-"], input=js,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"JS inválido em {nome}:\n{r.stderr[:500]}"


@pytest.mark.parametrize("nome", ["fatos_html", "placar_html"])
def test_pagina_tem_o_que_o_script_procura(nome):
    """Script válido que aponta pra elemento inexistente também não registra."""
    mod = _modulos()[nome]
    itens = [{
        "id": 1, "contato": "Fulano", "categoria": "professional",
        "fato": "um fato", "confianca": 0.45, "evidencia": [],
        "dia": "2026-08-04", "frente": "Uma frente", "portao": "um portão",
        "latencia": 0,
    }]
    html = mod.render(itens)
    for ident in ("feitos", "limpar", "copiar", "saida"):
        assert f'id="{ident}"' in html, f"{nome}: falta #{ident}"
    assert "data-uid=" in html
    assert 'data-v="' in html


# ------------------------------------------------- o terceiro estado --

def test_fila_de_fatos_tem_corrige_com_campo():
    """Nasceu com Confirma/Descarta e o Renato achou o caso que nenhum cobria na
    primeira sentada: "'Rico' (possivelmente filho) que treina judô" — confirmar
    gravaria mentira (não é filho), descartar perderia a verdade (Rico existe e
    treina judô). A resposta era corrigir: "apelido de Ricardo Lindenbojm".
    """
    import fatos_html

    html = fatos_html.render([{
        "id": 1, "contato": "X", "categoria": "personal", "fato": "f",
        "confianca": 0.45, "evidencia": [],
    }])
    assert 'data-v="corrige"' in html
    assert 'class="fix"' in html, "sem campo de texto, CORRIGE não tem o que gravar"


def test_parser_aceita_correcao_com_texto():
    import importlib.util
    import pathlib

    p = pathlib.Path(_ROOT) / "scripts" / "cos_agent" / "fatos.py"
    fonte = p.read_text()
    # o regex tem que capturar o texto depois de ':' — sem isso a correção
    # entraria como veredito vazio e o fato ficaria como estava
    assert r'([A-ZÀ-Ú]+)\s*(?::\s*(.+))?$' in fonte
    assert "CORRIGE sem texto" in fonte, "correção vazia tem que ser recusada, não gravada"


# ------------------------------------- feedback na página do sistema --------

def test_pagina_do_sistema_tem_js_valido():
    """Mesma armadilha do placar: JS dentro de f-string com chaves colidindo, ou
    `\\n` não-raw partindo a string. Nos dois casos o script morre inteiro e
    NENHUM listener registra — o sintoma é 'clico e não acontece nada'."""
    import pathlib
    import subprocess

    if not shutil.which("node"):
        pytest.skip("node ausente")

    src = pathlib.Path(_ROOT).joinpath("scripts", "sistema.py").read_text()
    assert "JS_FEEDBACK = r\"\"\"" in src, "o JS voltou pra dentro da f-string"

    html = pathlib.Path(os.path.expanduser("~/cockpit/sistema.html"))
    if not html.exists():
        pytest.skip("página ainda não gerada")
    js = re.search(r"<script>(.*?)</script>", html.read_text(), re.S)
    assert js, "página sem script"
    r = subprocess.run(["node", "--check", "-"], input=js.group(1),
                       capture_output=True, text=True)
    assert r.returncode == 0, f"JS inválido:\n{r.stderr[:400]}"


def test_gravador_aceita_nome_com_em_dash():
    """O separador do formato é ' — ' e VÁRIAS frentes têm em-dash no nome
    ("Luminosità — Vinho Itália→Japão"). Um regex guloso partiria o nome e
    gravaria o veredito na frente errada."""
    import re as _re

    padrao = r"^(\d{4}-\d{2}-\d{2})\s+#(\d+)\s+(.+?)\s+—\s+([A-ZÀ-Ú ]+?)(?:\s*\|\s*RASO:(.*))?$"
    linha = "2026-08-06 #48 Luminosità — Vinho Itália→Japão — CERTA | RASO: julgou sem olhar tudo"
    m = _re.match(padrao, linha)

    assert m, "não parseou"
    assert m.group(3) == "Luminosità — Vinho Itália→Japão", f"nome partido: {m.group(3)!r}"
    assert m.group(4).strip() == "CERTA"
    assert m.group(5) is not None, "perdeu o marcador RASO"
