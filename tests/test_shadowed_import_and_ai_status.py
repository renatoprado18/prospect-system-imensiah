"""
Dois fixes de 30/07/2026.

1. UnboundLocalError por import redundante — a CLASSE, nao so as 2 instancias.
   `editorial_pdca` importa get_db no topo do modulo E repetia
   `from database import get_db` dentro de generate_weekly_briefing e
   generate_monthly_review. Um import dentro da funcao torna o nome LOCAL em todo
   o corpo dela, entao o `with get_db() as conn` que vem ANTES levanta
   UnboundLocalError.

   Efeito real, medido em cron_runs: 19/07 status=error, 26/07 status=ok com
   result_json.briefing_error="cannot access local variable 'get_db' where it is
   not associated with a value". O gerador de tasks semanais do editorial morreu:
   os pares ("Medir metricas: posts da semana" / "Responder todos os comentarios")
   nasciam toda semana desde 03/05 e pararam em 13/07 — as duas ultimas ficaram
   pending, vencendo, acumulando atraso falso.

   O teste varre o AST em vez de checar as 2 funcoes conhecidas: qualquer funcao
   que reimporte um nome ja importado no modulo falha aqui.

2. Banner "API Claude indisponivel" com detalhe vazio.
   Medido em prod: {"status":"error","detail":""} — detail vazio e assinatura de
   timeout do httpx (`str(ReadTimeout())` == ""). Com cache unico de 1h, um soluco
   de 10s pintava erro na home por uma hora sem dizer o motivo.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_shadowed_import_and_ai_status.py -q
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

APP = os.path.join(_ROOT, "app")


def _module_level_imports(tree: ast.Module) -> set:
    """Nomes trazidos por import no nivel do MODULO."""
    nomes = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                nomes.add(alias.asname or alias.name.split(".")[0])
    return nomes


def _shadowing_imports(path: str):
    """[(funcao, nome, linha)] de import dentro de funcao que sombreia o modulo."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    do_modulo = _module_level_imports(tree)
    achados = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if inner is node or not isinstance(inner, (ast.Import, ast.ImportFrom)):
                continue
            for alias in inner.names:
                nome = alias.asname or alias.name.split(".")[0]
                if nome in do_modulo:
                    achados.append((node.name, nome, inner.lineno))
    return achados


class TestImportSombreado:
    def test_editorial_pdca_limpo(self):
        """As 2 instancias que quebraram o gerador em 19/07 e 26/07."""
        achados = _shadowing_imports(os.path.join(APP, "services", "editorial_pdca.py"))
        assert achados == [], (
            "import dentro de funcao sombreando nome do modulo (UnboundLocalError "
            f"em potencial): {achados}"
        )

    def test_hot_paths_limpos(self):
        """Mesma classe nos modulos que mais rodam."""
        criticos = [
            ("services", "circulos.py"),
            ("services", "notification_router.py"),
            ("services", "cos_keywords.py"),
            ("services", "project_news_watcher.py"),
        ]
        problemas = {}
        for partes in criticos:
            caminho = os.path.join(APP, *partes)
            if not os.path.exists(caminho):
                continue
            achados = _shadowing_imports(caminho)
            if achados:
                problemas[partes[-1]] = achados
        assert not problemas, f"imports sombreados: {problemas}"

    def test_o_detector_realmente_pega_o_padrao(self):
        """Guarda contra o teste virar decoracao: prova que ele acusa o caso ruim."""
        import tempfile

        codigo = (
            "from database import get_db\n"
            "def f():\n"
            "    with get_db() as c:\n"
            "        pass\n"
            "    from database import get_db\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(codigo)
            tmp = fh.name
        try:
            achados = _shadowing_imports(tmp)
            assert achados and achados[0][1] == "get_db"
        finally:
            os.unlink(tmp)


class TestAiStatus:
    """O _degraded aplica carencia de 1 falha e nunca devolve detalhe vazio."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        import main
        main._claude_api_status = {"status": "unknown", "checked_at": None,
                                  "detail": "", "consecutive_failures": 0}
        yield

    def test_primeira_falha_nao_acusa(self):
        import main
        r = main._degraded("timeout")
        assert r["status"] == "unknown", "1a falha nao pode pintar banner"
        assert r["consecutive_failures"] == 1

    def test_segunda_consecutiva_acusa(self):
        import main
        main._claude_api_status = main._degraded("timeout")
        r = main._degraded("timeout")
        assert r["status"] == "error"
        assert r["consecutive_failures"] == 2
        assert r["detail"] == "timeout"

    def test_detalhe_nunca_vazio(self):
        """O defeito original: detail="" nao dizia nada ao Renato."""
        import main
        main._claude_api_status = main._degraded("x")
        for motivo in ("timeout", "HTTP 529", "ReadTimeout (sem mensagem)"):
            assert main._degraded(motivo)["detail"].strip()

    def test_readtimeout_vazio_virou_nome_da_classe(self):
        """str(httpx.ReadTimeout()) == '' — era a assinatura do bug."""
        import httpx
        e = httpx.ReadTimeout("")
        motivo = str(e).strip() or f"{type(e).__name__} (sem mensagem)"
        assert motivo == "ReadTimeout (sem mensagem)"

    def test_ttl_assimetrico_declarado(self):
        """Sucesso 1h, falha 5min — falha precisa ser reavaliada logo."""
        import inspect
        import main
        src = inspect.getsource(main.ai_api_status)
        assert "3600 if" in src and "300" in src
