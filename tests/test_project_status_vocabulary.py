"""
O vocabulario de `projects.status` estava partido entre PT e EN — 30/07/2026.

MEDIDO EM PROD antes de mexer: ativo 29 · concluido 6 · archived 5 · pausado 4 ·
paused 1 · completed 1 · encerrado 1. Sete valores para quatro estados.

O custo nao era cosmetico, eram DOIS consumidores lendo um valor que nunca
existiu:

  1. `circulos.py` filtrava `status = 'active'`. Como nao ha erro em consultar
     valor inexistente — so ausencia de linhas — o fator "Projeto ativo"
     (+60 legacy / +10 cos) NUNCA pontuou ninguem desde que foi escrito.
     Medido: 48 contatos (9 owners + 45 membros) deviam ter recebido.
  2. `contact_intelligence.py` fazia o mesmo ao montar o dossie do contato, entao
     a lista de frentes daquela pessoa vinha SEMPRE vazia — justamente o contexto
     que a funcao existe pra montar.

E `list_projects(include_completed=False)` excluia apenas 'concluido', entao 6
projetos encerrados (5 arquivados + 1 'encerrado') passavam como trabalho vivo.

A guarda de classe (test_nenhum_consumidor_le_active_em_projects) e o que impede
isso de voltar: um filtro com o vocabulario errado falha em SILENCIO.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_project_status_vocabulary.py -q
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services.projects import (  # noqa: E402
    PROJECT_STATUS, STATUS_ENCERRADOS, normalize_status,
)

_APP = os.path.join(_ROOT, "app")


# ---------------------------------------------------------------------------
# Vocabulario
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrada,esperado", [
    ("active", "ativo"),
    ("paused", "pausado"),
    ("completed", "concluido"),
    ("archived", "arquivado"),
    ("cancelled", "cancelado"),
    ("canceled", "cancelado"),
    ("encerrado", "concluido"),
])
def test_sinonimos_sao_canonizados(entrada, esperado):
    assert normalize_status(entrada) == esperado


def test_canoniza_caixa_e_espaco():
    assert normalize_status("  ARCHIVED ") == "arquivado"


def test_status_ja_canonico_nao_muda():
    for s in PROJECT_STATUS:
        assert normalize_status(s) == s


def test_desconhecido_passa_intacto():
    """A funcao existe pra impedir que o vocabulario se parta, nao pra virar
    validador silencioso que engole valor novo — quem decide o que fazer com
    desconhecido e o chamador."""
    assert normalize_status("em_negociacao") == "em_negociacao"


def test_none_e_vazio_nao_explodem():
    assert normalize_status(None) is None
    assert normalize_status("") == ""


def test_arquivado_entrou_no_vocabulario():
    """5 projetos ja estavam nesse estado como 'archived'. Nao sao concluidos nem
    cancelados — mapear pra um dos outros mentiria sobre o desfecho."""
    assert "arquivado" in PROJECT_STATUS
    assert PROJECT_STATUS["arquivado"]["label"] == "Arquivado"


def test_pausado_nao_conta_como_encerrado():
    """Parado nao e terminado: projeto pausado continua na lista."""
    assert "pausado" not in STATUS_ENCERRADOS
    assert set(STATUS_ENCERRADOS) == {"concluido", "cancelado", "arquivado"}


def test_todo_sinonimo_aponta_pra_status_valido():
    """Sinonimo que traduz pra valor fora do PROJECT_STATUS recriaria o problema
    numa nova roupagem."""
    from services.projects import _STATUS_SINONIMOS
    for origem, destino in _STATUS_SINONIMOS.items():
        assert destino in PROJECT_STATUS, f"{origem} -> {destino} nao e canonico"


# ---------------------------------------------------------------------------
# Filtro default de list_projects
# ---------------------------------------------------------------------------
def test_filtro_default_exclui_os_tres_encerrados():
    """Antes: `AND p.status != 'concluido'` — arquivado, cancelado e encerrado
    passavam como projeto em andamento (6 casos reais em prod)."""
    src = open(os.path.join(_APP, "services", "projects.py"), encoding="utf-8").read()
    assert "!= 'concluido'" not in src, (
        "o filtro default voltou a olhar so 'concluido'"
    )
    assert "STATUS_ENCERRADOS" in src


# ---------------------------------------------------------------------------
# Guarda de classe: ninguem mais pode ler 'active' de projects
# ---------------------------------------------------------------------------
def _suspeito(sql: str) -> bool:
    """True se este literal SQL filtra `projects` por status em ingles.

    Analisa o literal INTEIRO (uma query = um literal), nao uma janela de linhas:
    a primeira versao deste teste usava janela e acusou uma consulta a `tasks`
    que por acaso ficava perto de outra sobre projects. `tasks.status` E
    'completed' de verdade — o vocabulario partido e o de `projects`.

    Restrito a `projects` de proposito: `campaigns` e `campaign_enrollments` tem
    vocabulario proprio em ingles, onde 'active' e legitimo."""
    baixo = sql.lower()
    if not re.search(r"\b(from|join)\s+projects\b", baixo):
        return False
    # Qualificado por alias de outra tabela (t.status, e.status, pm.status) nao
    # e sobre projects; sem alias ou com o alias de projects (p.), e.
    for m in re.finditer(r"(\w+\.)?status\s*=\s*['\"](active|paused|completed|archived)['\"]",
                         baixo):
        alias = (m.group(1) or "").rstrip(".")
        if alias in ("", "p", "proj", "projects"):
            return True
    return False


def _consultas_suspeitas():
    """Varre os literais de string de app/ atras do defeito."""
    import ast as _ast
    achados = []
    for raiz, _, arquivos in os.walk(_APP):
        if "__pycache__" in raiz:
            continue
        for nome in arquivos:
            if not nome.endswith(".py"):
                continue
            caminho = os.path.join(raiz, nome)
            try:
                arvore = _ast.parse(open(caminho, encoding="utf-8", errors="ignore").read())
            except SyntaxError:
                continue
            for no in _ast.walk(arvore):
                if (isinstance(no, _ast.Constant) and isinstance(no.value, str)
                        and _suspeito(no.value)):
                    achados.append(
                        f"{os.path.relpath(caminho, _ROOT)}:{no.lineno}: "
                        f"{' '.join(no.value.split())[:90]}"
                    )
    return achados


def test_nenhum_consumidor_le_active_em_projects():
    """A guarda que importa. Os dois defeitos originais nao davam erro nenhum —
    devolviam zero linhas, e o recurso simplesmente nao acontecia."""
    achados = _consultas_suspeitas()
    assert not achados, (
        "consulta a `projects` filtrando por status em ingles (falha em silencio):\n"
        + "\n".join(achados)
    )


def test_o_detector_acusa_o_codigo_original():
    """Prova que a varredura nao passa por vacuidade: o SQL exato que estava em
    circulos.py ate hoje tem que ser acusado."""
    original = """
        SELECT owner_contact_id as contact_id, COUNT(*) as count
        FROM projects
        WHERE status = 'active' AND owner_contact_id IS NOT NULL
        GROUP BY owner_contact_id
    """
    assert _suspeito(original)

    original_join = """
        SELECT pm.contact_id, COUNT(DISTINCT pm.project_id) as count
        FROM project_members pm
        JOIN projects p ON p.id = pm.project_id
        WHERE p.status = 'active' AND pm.contact_id IS NOT NULL
    """
    assert _suspeito(original_join)


def test_o_detector_nao_acusa_vocabulario_alheio():
    """Falso-positivo custa credibilidade da guarda. `campaigns` usa 'active' de
    verdade, e `tasks.status='completed'` e correto mesmo numa query que faz JOIN
    com projects — foi exatamente esse o FP da primeira versao deste teste."""
    campanha = "SELECT id FROM campaign_enrollments e WHERE e.status = 'active'"
    assert not _suspeito(campanha)

    task_com_join = """
        SELECT p.nome, COUNT(*) FROM projects p
        LEFT JOIN tasks t ON t.project_id = p.id
        WHERE t.status = 'completed'
    """
    assert not _suspeito(task_com_join)
