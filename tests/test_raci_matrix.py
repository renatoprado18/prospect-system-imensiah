"""
Testes da MATRIZ RACI GENERICA (28/07, task #999703).

O que esta coberto (sem tocar Neon nem o ConselhoOS):
  - normalizacao: uma linha de qualquer fonte vira o MESMO formato
  - `status_efetivo` DERIVADO na leitura (pendente + prazo vencido = atrasado)
    e a excecao que importa: item sem prazo NUNCA vira atrasado
  - ordenacao: atrasado primeiro, concluido por ultimo; sem prazo no fim do
    proprio bucket
  - a uniao das fontes e a marcacao de quem e editavel (so o lado INTEL)
  - o resumo e do conjunto COMPLETO mesmo com filtro aplicado
  - fonte ConselhoOS indisponivel degrada pra [] + erro reportado, nunca
    derruba a matriz nem finge que o RACI e menor

Rodar: .venv/bin/python -m pytest tests/test_raci_matrix.py -v
"""
import os
import sys
from datetime import date, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import raci_matrix  # noqa: E402
from services.raci_matrix import (  # noqa: E402
    FONTE_CONSELHOOS,
    FONTE_INTEL,
    _normalize,
    _sort_key,
    get_matrix,
)


ONTEM = date.today() - timedelta(days=1)
AMANHA = date.today() + timedelta(days=1)


def linha(**kw):
    base = {
        "id": 1, "area": "Tributário", "acao": "Decidir prescrever × transacionar",
        "responsavel_r": "Piccino", "responsavel_a": "Renato",
        "responsavel_c": None, "responsavel_i": "Gustavo",
        "prazo": None, "status": "pendente", "notas": None,
    }
    base.update(kw)
    return base


# ==================== normalização ====================

def test_as_duas_fontes_viram_o_mesmo_formato():
    a = _normalize(linha(), FONTE_INTEL)
    b = _normalize(linha(), FONTE_CONSELHOOS)
    assert set(a) == set(b)
    assert a["r"] == b["r"] == "Piccino"


def test_so_o_lado_intel_e_editavel():
    """O ConselhoOS é lido cross-DB, read-only. Uma tela que oferecesse editar
    o que ela não escreve prometeria o que não cumpre."""
    assert _normalize(linha(), FONTE_INTEL)["editavel"] is True
    assert _normalize(linha(), FONTE_CONSELHOOS)["editavel"] is False


def test_uid_separa_ids_iguais_de_fontes_diferentes():
    """id 1 do INTEL e id 1 do ConselhoOS são linhas distintas."""
    a = _normalize(linha(id=1), FONTE_INTEL)
    b = _normalize(linha(id=1), FONTE_CONSELHOOS)
    assert a["uid"] != b["uid"]


def test_campos_em_branco_viram_none_e_nao_string_vazia():
    it = _normalize(linha(area="   ", responsavel_c=""), FONTE_INTEL)
    assert it["area"] is None
    assert it["c"] is None


# ==================== status derivado ====================

def test_pendente_com_prazo_vencido_e_lido_como_atrasado():
    """Derivado na leitura, não por cron: a matriz não pode depender de um job
    ter rodado pra dizer a verdade sobre o que venceu."""
    it = _normalize(linha(status="pendente", prazo=ONTEM), FONTE_INTEL)
    assert it["status"] == "pendente"
    assert it["status_efetivo"] == "atrasado"


def test_item_sem_prazo_nunca_vira_atrasado():
    """A responsabilidade permanente ('coordenação da cadência') não vence.
    Pintá-la de vermelho é falso-atrasado — o que corroeu a credibilidade do
    RACI do Vallen em 13/07."""
    it = _normalize(linha(status="pendente", prazo=None), FONTE_INTEL)
    assert it["status_efetivo"] == "pendente"


def test_concluido_vencido_continua_concluido():
    it = _normalize(linha(status="concluido", prazo=ONTEM), FONTE_INTEL)
    assert it["status_efetivo"] == "concluido"


def test_prazo_futuro_preserva_o_status():
    it = _normalize(linha(status="em_andamento", prazo=AMANHA), FONTE_INTEL)
    assert it["status_efetivo"] == "em_andamento"


def test_dias_para_prazo_tem_sinal():
    assert _normalize(linha(prazo=ONTEM), FONTE_INTEL)["dias_para_prazo"] == -1
    assert _normalize(linha(prazo=AMANHA), FONTE_INTEL)["dias_para_prazo"] == 1
    assert _normalize(linha(prazo=None), FONTE_INTEL)["dias_para_prazo"] is None


def test_prazo_sai_em_iso_e_em_br():
    it = _normalize(linha(prazo=date(2026, 7, 30)), FONTE_INTEL)
    assert it["prazo"] == "2026-07-30"
    assert it["prazo_br"] == "30/07/2026"


# ==================== ordenação ====================

def test_atrasado_vem_antes_e_concluido_por_ultimo():
    itens = [
        _normalize(linha(id=1, status="concluido", acao="c"), FONTE_INTEL),
        _normalize(linha(id=2, status="pendente", prazo=ONTEM, acao="a"), FONTE_INTEL),
        _normalize(linha(id=3, status="pendente", prazo=AMANHA, acao="b"), FONTE_INTEL),
    ]
    itens.sort(key=_sort_key)
    assert [i["id"] for i in itens] == [2, 3, 1]


def test_dentro_do_bucket_o_prazo_mais_proximo_vem_antes():
    d1 = date.today() + timedelta(days=2)
    d2 = date.today() + timedelta(days=9)
    itens = [
        _normalize(linha(id=1, prazo=d2), FONTE_INTEL),
        _normalize(linha(id=2, prazo=d1), FONTE_INTEL),
    ]
    itens.sort(key=_sort_key)
    assert [i["id"] for i in itens] == [2, 1]


def test_item_sem_prazo_vai_pro_fim_do_bucket_mas_nao_some():
    itens = [
        _normalize(linha(id=1, prazo=None), FONTE_INTEL),
        _normalize(linha(id=2, prazo=AMANHA), FONTE_INTEL),
    ]
    itens.sort(key=_sort_key)
    assert [i["id"] for i in itens] == [2, 1]


# ==================== get_matrix (fontes mockadas) ====================

class FakeCursor:
    def __init__(self, projeto, itens_intel):
        self._projeto = projeto
        self._itens = itens_intel
        self._ultimo = None

    def execute(self, sql, params=None):
        self._ultimo = "projeto" if "FROM projects" in sql else "itens"

    def fetchone(self):
        return self._projeto if self._ultimo == "projeto" else None

    def fetchall(self):
        return self._itens


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def matriz(monkeypatch):
    """Monta get_matrix com fontes controladas."""
    def _montar(projeto, itens_intel, conselhoos=None, erro_conselhoos=None):
        cursor = FakeCursor(projeto, itens_intel)
        monkeypatch.setattr(raci_matrix, "get_db", lambda: FakeConn(cursor))
        monkeypatch.setattr(
            raci_matrix, "_fetch_conselhoos_status",
            lambda uuid: (
                [_normalize(r, FONTE_CONSELHOOS) for r in (conselhoos or [])],
                erro_conselhoos,
            ),
        )
    return _montar


PROJ_CONSELHO = {
    "id": 24, "nome": "Vallen Clinic", "tipo": "conselho", "status": "ativo",
    "empresa_id": 2, "empresa_nome": "Vallen Clinic",
    "conselhoos_empresa_id": "9c246a3a-3e7e-4472-9854-ce83f6e22e14",
}
PROJ_SIMPLES = {
    "id": 47, "nome": "Regularização das 7 empresas", "tipo": "negocio",
    "status": "ativo", "empresa_id": None, "empresa_nome": None,
    "conselhoos_empresa_id": None,
}


def test_projeto_de_conselho_une_as_duas_fontes(matriz):
    matriz(PROJ_CONSELHO, [linha(id=1, acao="do intel")],
           conselhoos=[linha(id=99, acao="do conselhoos")])
    r = get_matrix(24)
    assert r["total"] == 2
    assert {i["fonte"] for i in r["itens"]} == {FONTE_INTEL, FONTE_CONSELHOOS}
    assert [f["fonte"] for f in r["fontes"]] == [FONTE_INTEL, FONTE_CONSELHOOS]


def test_projeto_sem_empresa_nao_consulta_conselhoos(matriz):
    """A maioria dos projetos é assim. Ausência de fonte-conselho não é erro
    nem estado degradado — não pode aparecer como fonte quebrada."""
    matriz(PROJ_SIMPLES, [linha(id=1)])
    r = get_matrix(47)
    assert [f["fonte"] for f in r["fontes"]] == [FONTE_INTEL]
    assert r["total"] == 1


def test_conselhoos_fora_do_ar_nao_derruba_a_matriz(matriz):
    """Degrada pro lado INTEL e REPORTA o erro. Uma matriz incompleta que se
    apresenta como completa é pior que erro na cara — a tela usa isto pra
    avisar em vez de mostrar um RACI menor em silêncio."""
    matriz(PROJ_CONSELHO, [linha(id=1)],
           conselhoos=[], erro_conselhoos="connection refused")
    r = get_matrix(24)
    assert r["total"] == 1
    conselho = [f for f in r["fontes"] if f["fonte"] == FONTE_CONSELHOOS][0]
    assert conselho["erro"] == "connection refused"


def test_filtro_encolhe_os_itens_mas_nao_o_resumo(matriz):
    """Se o resumo também encolhesse, olhar 'concluídos' esconderia quantos
    atrasados existem — justamente o número que se quer vigiar."""
    matriz(PROJ_SIMPLES, [
        linha(id=1, status="pendente", prazo=ONTEM),
        linha(id=2, status="concluido"),
        linha(id=3, status="pendente"),
    ])
    r = get_matrix(47, status="concluido")
    assert r["total"] == 1
    assert r["resumo"] == {"atrasado": 1, "pendente": 1,
                           "em_andamento": 0, "concluido": 1}
    assert r["filtro_status"] == "concluido"


def test_projeto_inexistente_devolve_erro_e_nao_estoura(matriz):
    matriz(None, [])
    r = get_matrix(999999)
    assert r["error"] == "projeto não encontrado"
