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
import re
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
    _split_uid,
    _update_conselhoos,
    _update_intel,
    delete_item,
    get_matrix,
    update_item,
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

def test_concluido_no_prazo_exige_AS_DUAS_datas():
    """`None` não é `False`.

    Item concluído sem data de fechamento, ou sem prazo, não é item atrasado —
    é item NÃO MEDIDO. Colapsar os dois em `False` transformaria os 42 itens do
    Vallen sem data recuperável em 42 fechamentos fora do prazo, que é a versão
    silenciosa de inventar o número (073).
    """
    from datetime import datetime
    sem_nada = _normalize(linha(status="concluido"), FONTE_INTEL)
    assert sem_nada["concluido_no_prazo"] is None
    assert sem_nada["concluido_em"] is None

    so_prazo = _normalize(linha(status="concluido", prazo=ONTEM), FONTE_INTEL)
    assert so_prazo["concluido_no_prazo"] is None, "sem data de conclusão não se avalia"

    so_data = _normalize(linha(status="concluido",
                               concluido_em=datetime(2026, 8, 1, 10, 0)), FONTE_INTEL)
    assert so_data["concluido_no_prazo"] is None, "sem prazo não há do que atrasar"


def test_no_prazo_compara_dia_e_nao_instante():
    """Fechar às 18h do dia do prazo é fechar no prazo."""
    from datetime import datetime
    hoje = date.today()
    it = _normalize(linha(status="concluido", prazo=hoje,
                          concluido_em=datetime(hoje.year, hoje.month, hoje.day, 18, 30)),
                    FONTE_INTEL)
    assert it["concluido_no_prazo"] is True
    tarde = _normalize(linha(status="concluido", prazo=ONTEM,
                             concluido_em=datetime(hoje.year, hoje.month, hoje.day, 9, 0)),
                       FONTE_INTEL)
    assert tarde["concluido_no_prazo"] is False


def test_acumulado_declara_a_cobertura_junto_com_o_numero():
    """A régua tem que dizer sobre quantos ela fala.

    O caso real: o Vallen tem 52 concluídos e só 10 com data recuperável.
    Publicar "5 no prazo" sem o denominador faria parecer 5 de 52.
    """
    from datetime import datetime
    from services.raci_matrix import _acumulado
    hoje = date.today()
    itens = [
        _normalize(linha(id=1, status="concluido", prazo=hoje,
                         concluido_em=datetime(hoje.year, hoje.month, hoje.day, 9, 0),
                         concluido_em_fonte="gatilho"), FONTE_INTEL),
        _normalize(linha(id=2, status="concluido", prazo=ONTEM,
                         concluido_em=datetime(hoje.year, hoje.month, hoje.day, 9, 0),
                         concluido_em_fonte="relato"), FONTE_INTEL),
        _normalize(linha(id=3, status="concluido"), FONTE_INTEL),          # sem data
        _normalize(linha(id=4, status="pendente", prazo=AMANHA), FONTE_INTEL),
    ]
    a = _acumulado(itens)
    assert a["concluidos"] == 3 and a["total"] == 4
    assert a["rotulo"] == "3/4"
    assert a["medidos"] == 2, "só os que têm as duas datas entram na conta"
    assert a["no_prazo"] == 1 and a["fora_prazo"] == 1
    assert a["sem_data"] == 1, "o não medido tem que aparecer, não sumir"
    assert a["cobertura_pct"] == 67
    assert a["por_relato"] == 1, "backfill é limite superior — precisa ser separável"
    assert "de 2 medidos" in a["rotulo_pontualidade"]


def test_acumulado_de_frente_sem_conclusao_nao_inventa_numero():
    from services.raci_matrix import _acumulado
    a = _acumulado([_normalize(linha(status="pendente"), FONTE_INTEL)])
    assert a["concluidos"] == 0
    assert a["rotulo"] == "0/1"
    assert a["cobertura_pct"] is None and a["rotulo_pontualidade"] is None, (
        "sem item concluído, percentual seria divisão por zero disfarçada de zero")


def test_as_duas_fontes_viram_o_mesmo_formato():
    a = _normalize(linha(), FONTE_INTEL)
    b = _normalize(linha(), FONTE_CONSELHOOS)
    assert set(a) == set(b)
    assert a["r"] == b["r"] == "Piccino"


def test_as_duas_fontes_sao_editaveis_mas_so_o_intel_e_removivel():
    """Write-through (29/07): editar vale nos dois lados, gravando NA FONTE.
    Remover, não — apagar linha de RACI de conselho é destruir registro de ata
    de uma empresa por um caminho que não é o dela."""
    intel = _normalize(linha(), FONTE_INTEL)
    conselho = _normalize(linha(), FONTE_CONSELHOOS)
    assert intel["editavel"] is True and conselho["editavel"] is True
    assert intel["removivel"] is True
    assert conselho["removivel"] is False


def test_so_o_conselhoos_exige_prazo():
    """`prazo` é NOT NULL lá e opcional aqui. A tela usa isto pra avisar antes,
    em vez de deixar limpar o campo e falhar na gravação."""
    assert _normalize(linha(), FONTE_INTEL)["prazo_obrigatorio"] is False
    assert _normalize(linha(), FONTE_CONSELHOOS)["prazo_obrigatorio"] is True


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


# ==================== escrita: roteamento por fonte ====================
#
# O que estes testes protegem: um `uid` mal lido escreveria no BANCO ERRADO —
# `12` do INTEL e um uuid do ConselhoOS chegam pelo mesmo endpoint.

UUID = "9c246a3a-3e7e-4472-9854-ce83f6e22e14"


def test_uid_separa_as_duas_fontes():
    assert _split_uid("intel:12") == (FONTE_INTEL, "12")
    assert _split_uid(f"conselhoos:{UUID}") == (FONTE_CONSELHOOS, UUID)


def test_id_nu_continua_valendo_como_intel():
    """Formato da 1ª versão da tela; segue chegando de aba aberta ou link
    salvo. Quebrar isso daria 400 num caminho que funcionava."""
    assert _split_uid("12") == (FONTE_INTEL, "12")
    assert _split_uid(12) == (FONTE_INTEL, "12")


@pytest.mark.parametrize("ruim", ["", "  ", "outro:9", "intel:", "intel:abc", ":12"])
def test_uid_indecifravel_nao_vira_palpite(ruim):
    """Adivinhar a fonte aqui grava no banco errado — melhor recusar."""
    assert _split_uid(ruim) == (None, None)


def test_update_com_uid_invalido_nao_toca_banco_nenhum(monkeypatch):
    monkeypatch.setattr(raci_matrix, "_update_intel",
                        lambda *a: pytest.fail("não podia chegar no INTEL"))
    monkeypatch.setattr(raci_matrix, "_update_conselhoos",
                        lambda *a: pytest.fail("não podia chegar no ConselhoOS"))
    assert "inválido" in update_item("outro:9", {"status": "concluido"})["error"]


def test_update_roteia_pra_fonte_certa(monkeypatch):
    chamou = {}
    monkeypatch.setattr(raci_matrix, "_update_intel",
                        lambda i, d: chamou.setdefault("intel", i) or {"ok": True})
    monkeypatch.setattr(raci_matrix, "_update_conselhoos",
                        lambda i, d: chamou.setdefault("cos", i) or {"ok": True})

    update_item("intel:12", {"status": "concluido"})
    update_item(f"conselhoos:{UUID}", {"status": "concluido"})

    assert chamou["intel"] == 12          # int, não string
    assert chamou["cos"] == UUID


def test_delete_de_item_de_conselho_e_recusado(monkeypatch):
    """DELETE segue INTEL-only mesmo com a escrita aberta."""
    monkeypatch.setattr(raci_matrix, "get_db",
                        lambda: pytest.fail("não podia abrir o banco"))
    r = delete_item(f"conselhoos:{UUID}")
    assert "não se remove pelo INTEL" in r["error"]


# ==================== escrita: guardas do ConselhoOS ====================

@pytest.fixture
def sem_conexao(monkeypatch):
    """Faz qualquer tentativa de conectar no ConselhoOS falhar o teste: o que
    se quer provar é que a validação barra ANTES de abrir conexão."""
    monkeypatch.setattr(raci_matrix, "_conselhoos_url",
                        lambda: pytest.fail("validação devia ter barrado antes"))


@pytest.mark.parametrize("campo", ["area", "acao", "prazo"])
def test_conselhoos_recusa_apagar_campo_not_null(campo, sem_conexao):
    """Mandar None nestes viraria IntegrityError genérico, e a tela mostraria
    'erro ao salvar' sem dizer que o campo nunca foi opcional."""
    r = _update_conselhoos(UUID, {campo: None})
    assert "ConselhoOS não aceita" in r["error"]
    assert campo in r["error"]      # a mensagem diz QUAL campo


def test_status_nulo_cai_na_validacao_de_enum(sem_conexao):
    """`status` também é NOT NULL, mas a mensagem útil aqui é a do enum."""
    assert "status inválido" in _update_conselhoos(UUID, {"status": None})["error"]


def test_conselhoos_recusa_string_so_de_espaco(sem_conexao):
    assert "error" in _update_conselhoos(UUID, {"area": "   "})


@pytest.mark.parametrize("valor", ["concluído", "done", "CONCLUIDO", ""])
def test_status_invalido_e_barrado_nas_duas_fontes(valor, sem_conexao, monkeypatch):
    """ENUM lá, CHECK aqui: os dois estouram feio. 400 legível > 500."""
    monkeypatch.setattr(raci_matrix, "get_db",
                        lambda: pytest.fail("não podia abrir o banco"))
    assert "status inválido" in _update_intel(12, {"status": valor})["error"]
    assert "error" in _update_conselhoos(UUID, {"status": valor})


def test_patch_vazio_nao_gera_update(sem_conexao, monkeypatch):
    """Um UPDATE sem SET só carimbaria `updated_at` — ruído de auditoria."""
    monkeypatch.setattr(raci_matrix, "get_db",
                        lambda: pytest.fail("não podia abrir o banco"))
    assert _update_intel(12, {})["error"] == "nada a atualizar"
    assert _update_conselhoos(UUID, {})["error"] == "nada a atualizar"


def test_task_id_nao_atravessa_pro_conselhoos(sem_conexao):
    """Lá a coluna é `intel_task_id` e quem a governa é o `conselhoos_raci_sync`.
    Aceitar `task_id` aqui criaria um segundo dono do mesmo elo."""
    assert _update_conselhoos(UUID, {"task_id": 999})["error"] == "nada a atualizar"


def test_concluir_pelo_intel_nao_marca_como_ja_relatado(monkeypatch):
    """`concluido_relatado_em` é do `raci_weekly_report`: item concluído com o
    campo preenchido nunca é anunciado no grupo. A edição não pode tocá-lo."""
    sql = {}

    class Cur:
        rowcount = 1
        def execute(self, q, v=None): sql["q"] = q
        def close(self): pass

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(raci_matrix, "_conselhoos_url", lambda: "postgres://x")
    monkeypatch.setitem(sys.modules, "psycopg2",
                        type(sys)("psycopg2"))
    sys.modules["psycopg2"].connect = lambda *a, **k: Conn()
    sys.modules["psycopg2"].extras = type(sys)("extras")

    r = _update_conselhoos(UUID, {"status": "concluido"})
    assert r["ok"] is True
    assert "concluido_relatado_em" not in sql["q"]
    assert "::raci_status" in sql["q"]     # sem o cast, o enum recusa o texto
    assert "updated_at" in sql["q"]


# ==================== texto pro grupo (29/07) ====================
#
# A decisão que estes testes protegem: o texto sai SEM numeração. O
# `parse_raci_update` escuta os grupos e interpreta "3 concluído" pela ordem do
# `generate_raci_report` — que não é esta ordem. Numerar aqui convidaria uma
# resposta que acertaria o item errado.

from services.raci_matrix import (  # noqa: E402
    WHATSAPP_MAX_CHARS,
    _primeiro_nome,
    format_for_whatsapp,
)


def _matriz(itens, nome="Vallen Clinic"):
    normalizados = [_normalize(i, i.pop("_fonte", FONTE_INTEL)) for i in itens]
    resumo = {s: 0 for s in ("atrasado", "pendente", "em_andamento", "concluido")}
    for it in normalizados:
        resumo[it["status_efetivo"]] += 1
    return {"project": {"id": 24, "nome": nome}, "itens": normalizados,
            "resumo": resumo, "total": len(normalizados)}


def test_texto_nao_numera_os_itens():
    """O guard-rail principal: numeração convidaria resposta desalinhada."""
    txt = format_for_whatsapp(_matriz([
        linha(id=1, acao="Revisar contrato", prazo=ONTEM),
        linha(id=2, acao="Fechar julho", prazo=AMANHA),
    ]))
    assert "• " in txt
    assert not re.search(r"^\s*\d+\.\s", txt, re.M)


def test_texto_nao_convida_resposta_por_numero():
    txt = format_for_whatsapp(_matriz([linha(id=1, prazo=AMANHA)])).lower()
    for isca in ("responda", "nº", "numero", "número"):
        assert isca not in txt


def test_agrupa_por_status_efetivo():
    txt = format_for_whatsapp(_matriz([
        linha(id=1, acao="Vencido", status="pendente", prazo=ONTEM),
        linha(id=2, acao="Tocando", status="em_andamento", prazo=AMANHA),
        linha(id=3, acao="Parado", status="pendente", prazo=AMANHA),
    ]))
    assert "Atrasados (1)" in txt and "Em andamento (1)" in txt and "Pendentes (1)" in txt
    # atrasado primeiro — é o que precisa de resposta no grupo
    assert txt.index("Atrasados") < txt.index("Pendentes")


def test_concluidos_sao_contagem_por_padrao():
    """Quem lê o RACI no grupo quer saber o que FALTA."""
    txt = format_for_whatsapp(_matriz([
        linha(id=1, acao="Ja feito", status="concluido"),
        linha(id=2, acao="Falta isso", status="pendente", prazo=AMANHA),
    ]))
    assert "1 concluído" in txt
    assert "Ja feito" not in txt


def test_concluidos_listam_quando_pedido():
    txt = format_for_whatsapp(_matriz([
        linha(id=1, acao="Ja feito", status="concluido")]), incluir_concluidos=True)
    assert "Ja feito" in txt


def test_bucket_vazio_nao_vira_cabecalho_orfao():
    txt = format_for_whatsapp(_matriz([linha(id=1, status="pendente", prazo=AMANHA)]))
    assert "Atrasados" not in txt and "Em andamento" not in txt


def test_matriz_vazia_nao_estoura():
    txt = format_for_whatsapp(_matriz([]))
    assert "Nenhum item" in txt


@pytest.mark.parametrize("bruto,esperado", [
    ("Jéssica (cobrindo Veridiana)", "Jéssica"),
    ("Thalita/Renato", "Thalita"),
    ("  Renato  ", "Renato"),
    (None, "—"),
    ("", "—"),
])
def test_responsavel_sai_curto(bruto, esperado):
    """'Jéssica (cobrindo Veridiana)' come a linha e empurra o prazo pra outra."""
    assert _primeiro_nome(bruto) == esperado


def test_acao_longa_e_cortada_com_reticencia():
    txt = format_for_whatsapp(_matriz([
        linha(id=1, acao="A" * 300, status="pendente", prazo=AMANHA)]))
    assert "…" in txt
    assert "A" * 300 not in txt


def test_teto_nao_barra_o_raci_completo_da_vallen():
    """CORRIGIDO 29/07 — este teste travava uma premissa FALSA.

    A versão anterior cravava `WHATSAPP_MAX_CHARS == 4096` porque "a Evolution
    corta em silêncio". Ela não corta: medido contra `chat/findMessages`, que
    devolve o ENTREGUE, digests de 6.463 chars chegaram inteiros e um preview de
    RACI de 4.669 chegou com o rodapé final intacto.

    O erro não era acadêmico. A Vallen com os 50 concluídos listados dá **4.774
    chars**, então o botão "Enviar no grupo" — o caminho oficial de segunda —
    recusava o RACI completo sem razão técnica. O que o teste protege agora é
    isso: o caso real tem que caber.
    """
    assert WHATSAPP_MAX_CHARS > 4774, (
        "o RACI completo da Vallen (4.774 chars) precisa caber — foi o caso que "
        "o teto de 4.096 barrava")


def test_aviso_de_legibilidade_nao_bloqueia():
    """Os dois números têm naturezas diferentes e não podem se confundir:
    `MAX` é recusa (limite de canal), `LEGIBILIDADE` é conselho (ninguém lê 5
    mil caracteres num grupo). Tratar preferência como limite de protocolo foi
    exatamente o erro de 28/07."""
    from services.raci_matrix import WHATSAPP_LEGIBILIDADE_CHARS
    assert WHATSAPP_LEGIBILIDADE_CHARS < WHATSAPP_MAX_CHARS
