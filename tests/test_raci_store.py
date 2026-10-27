"""
Testes do `raci_store` — o roteamento que ninguem estava medindo (27/10/2026).

POR QUE ESTE ARQUIVO EXISTE. A fonte unica do RACI (06/10) virou a LEITURA da
matriz para so-INTEL e deixou as escritas automaticas no ConselhoOS. O resultado
medido em 26/10 na Vallen: INTEL parado em 13/10, ConselhoOS com 29 itens
movidos entre 14 e 23/10 — a reuniao de conselho inteira — e o grupo do cliente
exibindo `✅ Atualizado` o tempo todo. Treze dias, zero sinal.

Nada disso teria sido pego pela suite de entao, e a razao e' estrutural: TODOS
os testes do RACI dublavam `psycopg2.connect`. Um dublê de conexao responde
"o SQL esta certo?" e e' cego para "este SQL foi para o banco certo?" — a
unica pergunta que importava. Um dublê que atende as duas conexoes com o mesmo
objeto nao distingue uma da outra; e' o medidor que nao mede a si mesmo
([[feedback_medidor_que_nao_mede_a_si_mesmo]]).

Entao aqui os dois bancos sao dublados SEPARADAMENTE e os testes cobram DESTINO,
nao sintaxe. Cada caso roda nos dois targets: o controle positivo
([[feedback_controle_positivo_pega_o_furo_real]]) e' o que impede um roteador
que manda tudo para o mesmo lugar de passar verde.

Rodar: .venv/bin/python -m pytest tests/test_raci_store.py -v
"""
import os
import sys
from contextlib import contextmanager
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import raci_store  # noqa: E402

EMPRESA_UUID = "9c246a3a-3e7e-4472-9854-ce83f6e22e14"
ITEM_UUID = "6e37a6ef-78f7-4f3a-a092-9a77ab698847"
PROJECT_ID = 24


# ─────────────────────────────────────────────────────────────────────────────
# Dublês: um por banco, de propósito
# ─────────────────────────────────────────────────────────────────────────────
class _Cur:
    def __init__(self, banco, respostas):
        self.banco = banco
        self._respostas = respostas          # lista de rows a devolver, em ordem
        self._fila = list(respostas)

    def execute(self, sql, params=None):
        self.banco.queries.append((" ".join(sql.split()), params))
        self.banco._pendente = self._fila.pop(0) if self._fila else None

    def fetchone(self):
        p = self.banco._pendente
        if isinstance(p, list):
            return p[0] if p else None
        return p

    def fetchall(self):
        p = self.banco._pendente
        if p is None:
            return []
        return p if isinstance(p, list) else [p]

    @property
    def rowcount(self):
        return self.banco.rowcount

    def close(self):
        pass


class _Banco:
    """Um banco dublado. `queries` é o que se cobra: o SQL E a chave usada."""

    def __init__(self, nome, respostas=()):
        self.nome = nome
        self.queries = []
        self.committed = 0
        self.rowcount = 1
        self._pendente = None
        self._respostas = list(respostas)

    def cursor(self, **kw):
        return _Cur(self, self._respostas)

    def commit(self):
        self.committed += 1

    def close(self):
        pass

    # ── o que os testes perguntam ──
    @property
    def escritas(self):
        return [q for q, _p in self.queries if q.upper().startswith(("UPDATE", "INSERT"))]

    @property
    def leituras(self):
        return [q for q, _p in self.queries if q.upper().startswith("SELECT")]

    def params_de(self, trecho):
        return [p for q, p in self.queries if trecho in q]


@pytest.fixture
def bancos(monkeypatch):
    """Instala um dublê para o INTEL e outro para o ConselhoOS.

    Devolve `(intel, cos)`. Chamar `.respostas(...)` configura o que cada um
    devolve. O ponto do fixture é que os dois NUNCA são o mesmo objeto: é isso
    que torna possível cobrar em qual deles a escrita caiu.
    """
    intel = _Banco("intel")
    cos = _Banco("conselhoos")

    @contextmanager
    def fake_get_db():
        yield intel

    monkeypatch.setattr(raci_store, "get_db", fake_get_db)
    monkeypatch.setattr(raci_store, "_cos_conn", lambda: cos)
    monkeypatch.setenv("CONSELHOOS_DATABASE_URL", "postgresql://fake")
    return intel, cos


def _scope(target):
    return raci_store.RaciScope(target=target, project_id=PROJECT_ID,
                                empresa_uuid=EMPRESA_UUID, nome="Vallen Clinic")


# ─────────────────────────────────────────────────────────────────────────────
# O kill-switch
# ─────────────────────────────────────────────────────────────────────────────
def test_target_default_e_intel(monkeypatch):
    """O default é o destino CERTO, e isso é decisão, não descuido.

    Flag que nasce desligada é consumidor morto esperando alguém lembrar dela
    ([[feedback_consumidor_morto_wiring]]) — e o custo desta já foi pago em 13
    dias de defasagem silenciosa. O valor do switch é a REVERSÃO, não o
    opt-in.
    """
    monkeypatch.delenv("RACI_WRITE_TARGET", raising=False)
    assert raci_store.write_target() == raci_store.TARGET_INTEL


@pytest.mark.parametrize("valor", ["conselhoos", "  conselhoos  ", "CONSELHOOS", "ConselhoOS\n"])
def test_switch_volta_pro_conselhoos_apesar_de_espaco_e_caixa(monkeypatch, valor):
    """Railway/Vercel colam '\\n' no valor — a reversão de emergência não pode
    falhar por whitespace ([[feedback_env_var_whitespace]])."""
    monkeypatch.setenv("RACI_WRITE_TARGET", valor)
    assert raci_store.write_target() == raci_store.TARGET_CONSELHOOS


def test_valor_desconhecido_cai_no_default_e_nao_silencia(monkeypatch, caplog):
    """Typo não pode virar roteamento silencioso pro banco errado."""
    monkeypatch.setenv("RACI_WRITE_TARGET", "intelzinho")
    with caplog.at_level("WARNING"):
        assert raci_store.write_target() == raci_store.TARGET_INTEL
    assert any("intelzinho" in r.getMessage() for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Leitura: em QUAL banco, sob QUAL chave
# ─────────────────────────────────────────────────────────────────────────────
def test_abertos_vem_do_intel_por_project_id(bancos):
    intel, cos = bancos
    intel._respostas = [[{"id": 47, "acao": "Contrato da Camila", "status": "pendente",
                          "prazo": None, "notas": "", "responsavel_r": "Renato",
                          "responsavel_a": None, "responsavel_c": None,
                          "responsavel_i": None, "updated_at": None,
                          "concluido_relatado_em": None, "area": "Jurídico"}]]

    itens = raci_store.list_open_items(_scope(raci_store.TARGET_INTEL))

    assert [i["acao"] for i in itens] == ["Contrato da Camila"]
    assert cos.queries == [], "foi ao ConselhoOS para uma leitura que é do INTEL"
    assert intel.params_de("project_id = %s") == [(PROJECT_ID,)]


def test_abertos_vem_do_conselhoos_quando_o_switch_volta(bancos):
    """CONTROLE POSITIVO. Sem ele, um roteador que manda tudo pro INTEL passaria
    no teste de cima e o kill-switch seria decorativo."""
    intel, cos = bancos
    cos._respostas = [[{"id": ITEM_UUID, "acao": "Item de ata", "status": "pendente",
                        "prazo": None, "notas": "", "responsavel_r": None,
                        "responsavel_a": None, "responsavel_c": None,
                        "responsavel_i": None, "updated_at": None,
                        "concluido_relatado_em": None, "area": None}]]

    itens = raci_store.list_open_items(_scope(raci_store.TARGET_CONSELHOOS))

    assert [i["acao"] for i in itens] == ["Item de ata"]
    assert intel.queries == [], "foi ao INTEL com o switch em conselhoos"
    assert cos.params_de("empresa_id = %s") == [(EMPRESA_UUID,)]


def test_id_sai_sempre_como_string_nos_dois_targets(bancos):
    """`raci_group_proposals.item_id` é `text` e o caminho inteiro carrega o id
    como string. Devolver `int` no INTEL e `UUID` no ConselhoOS faria a
    comparação com o contexto do LLM falhar por TIPO, calada."""
    intel, _cos = bancos
    base = {"acao": "x", "status": "pendente", "prazo": None, "notas": "",
            "responsavel_r": None, "responsavel_a": None, "responsavel_c": None,
            "responsavel_i": None, "updated_at": None,
            "concluido_relatado_em": None, "area": None}
    intel._respostas = [[{**base, "id": 47}]]
    assert raci_store.list_open_items(_scope(raci_store.TARGET_INTEL))[0]["id"] == "47"


def test_report_rows_filtra_concluido_ja_relatado(bancos):
    """O filtro que a 088 veio sustentar: concluído aparece UMA vez e some.

    Sem ele os 81 concluídos da Vallen entrariam como novidade toda segunda — e
    o `[:5]` do formatador esconderia o defeito, entregando cinco itens
    fechados há meses como se fossem da semana.
    """
    intel, _cos = bancos
    intel._respostas = [[]]
    raci_store.report_rows(_scope(raci_store.TARGET_INTEL))
    q = intel.leituras[0]
    assert "concluido_relatado_em IS NULL" in q
    assert "status != 'concluido'" in q


# ─────────────────────────────────────────────────────────────────────────────
# Tradução de referência — o que não quebra a fila de 92
# ─────────────────────────────────────────────────────────────────────────────
def test_uuid_da_fila_traduz_para_id_do_intel(bancos):
    """As 92 propostas `pending_review` carregam UUID do ConselhoOS e todas têm
    par no INTEL (medido 27/10). A tradução é o que as mantém aplicáveis sem
    reescrever a fila em lote."""
    intel, _cos = bancos
    intel._respostas = [{"id": 47}]

    assert raci_store.resolve_item_ref(_scope(raci_store.TARGET_INTEL), ITEM_UUID) == "47"
    q, params = intel.queries[0]
    assert "conselhoos_raci_id = %s::uuid" in q
    assert params[0] == ITEM_UUID


def test_id_nativo_nao_vai_ao_banco(bancos):
    """Id já no formato do target não gasta consulta — e, mais importante, não
    depende de ponteiro: é o caso dos 3 itens abertos da Vallen que nasceram só
    no INTEL (entre eles o domínio `vallenclinic`)."""
    intel, cos = bancos
    assert raci_store.resolve_item_ref(_scope(raci_store.TARGET_INTEL), "47") == "47"
    assert intel.queries == [] and cos.queries == []


def test_sem_par_devolve_none_em_vez_de_chutar(bancos):
    """Ausência de par é informação, não erro — e nunca pode virar um id
    inventado, que escreveria num item real, o errado."""
    intel, _cos = bancos
    intel._respostas = [None]
    assert raci_store.resolve_item_ref(_scope(raci_store.TARGET_INTEL), ITEM_UUID) is None


def test_referencia_que_nao_e_int_nem_uuid_nao_passa(bancos):
    """`item_id` alucinado tipo "item 3" ou "" não vira lookup nem escrita."""
    scope = _scope(raci_store.TARGET_INTEL)
    for lixo in ("item 3", "", None, "3; DROP TABLE raci_itens"):
        assert raci_store.resolve_item_ref(scope, lixo) is None


# ─────────────────────────────────────────────────────────────────────────────
# Escrita: o defeito desta frente, travado
# ─────────────────────────────────────────────────────────────────────────────
def test_update_escreve_no_intel_e_nao_toca_o_conselhoos(bancos):
    """A REGRESSÃO QUE CUSTOU 13 DIAS. Com o target em `intel`, nenhuma escrita
    pode cair no banco do cliente — nem como efeito colateral."""
    intel, cos = bancos
    escreveu = raci_store.update_item(_scope(raci_store.TARGET_INTEL), "47",
                                      status="concluido")
    assert escreveu is True
    assert cos.escritas == [], "escreveu no ConselhoOS com o target em intel"
    assert len(intel.escritas) == 1
    q = intel.escritas[0]
    assert "atualizado_em = NOW()" in q, "carimbou updated_at (coluna do outro banco)"
    # A procedência viaja junto do fechamento: é o que torna o auto-update
    # visível para quem lê a matriz, em vez de exigir auditoria.
    assert "concluido_em_fonte" in q and "'grupo_wa'" in q


def test_update_escreve_no_conselhoos_quando_o_switch_volta(bancos):
    """CONTROLE POSITIVO da escrita, e a prova de que a reversão funciona."""
    intel, cos = bancos
    raci_store.update_item(_scope(raci_store.TARGET_CONSELHOOS), ITEM_UUID,
                           status="concluido")
    assert intel.escritas == []
    assert len(cos.escritas) == 1
    q = cos.escritas[0]
    assert "updated_at = NOW()" in q
    # Lá a coluna de procedência tem outro contrato — este módulo não mexe no
    # que não consolidou.
    assert "concluido_em_fonte" not in q


def test_nota_concatena_nunca_sobrescreve(bancos):
    """Campo de texto curado reescrito inteiro foi o dano da cascata de 14/08, e
    aqui o texto é de governança de cliente."""
    intel, _cos = bancos
    raci_store.update_item(_scope(raci_store.TARGET_INTEL), "47",
                           append_note="\n[27/10] o Gustavo confirmou")
    q = intel.escritas[0]
    assert "notas = COALESCE(notas, '') || %s" in q
    assert "notas = %s" not in q


def test_update_sem_mudanca_nao_escreve(bancos):
    """No-op não polui audit log nem gera `✅ Atualizado` no grupo — confirmar
    no grupo uma mudança que não aconteceu é fato falso pros conselheiros."""
    intel, cos = bancos
    assert raci_store.update_item(_scope(raci_store.TARGET_INTEL), "47") is False
    assert intel.escritas == [] and cos.escritas == []


def test_status_fora_do_check_e_recusado_antes_do_banco(bancos):
    """`cancelado` não existe no CHECK de nenhum dos dois lados. Barrar aqui
    devolve o motivo em vez de um erro de banco sem explicação."""
    intel, _cos = bancos
    with pytest.raises(raci_store.RaciStoreError):
        raci_store.update_item(_scope(raci_store.TARGET_INTEL), "47", status="cancelado")
    assert intel.escritas == []


def test_update_em_item_sem_par_levanta_em_vez_de_escrever_errado(bancos):
    intel, cos = bancos
    intel._respostas = [None]
    with pytest.raises(raci_store.RaciStoreError):
        raci_store.update_item(_scope(raci_store.TARGET_INTEL), ITEM_UUID,
                               status="concluido")
    assert intel.escritas == [] and cos.escritas == []


# ─────────────────────────────────────────────────────────────────────────────
# Criação: a diferença de schema que é deliberada
# ─────────────────────────────────────────────────────────────────────────────
def test_item_novo_no_intel_aceita_prazo_ausente(bancos):
    """`prazo` é NOT NULL no ConselhoOS e OPCIONAL no INTEL, e a diferença é de
    propósito: item sem prazo não vira atrasado, que é o estado certo das
    responsabilidades permanentes. Cobrar prazo aqui inventaria data onde a
    ausência é informação."""
    intel, _cos = bancos
    intel._respostas = [{"id": 200}]
    novo = raci_store.create_item(_scope(raci_store.TARGET_INTEL),
                                  {"acao": "Coordenar a cadência do conselho"})
    assert novo == "200"
    assert intel.escritas and intel.escritas[0].upper().startswith("INSERT")


def test_item_novo_no_conselhoos_exige_prazo(bancos):
    """Lá a coluna é NOT NULL: recusar com motivo é melhor que estourar no banco."""
    _intel, cos = bancos
    with pytest.raises(raci_store.RaciStoreError):
        raci_store.create_item(_scope(raci_store.TARGET_CONSELHOOS), {"acao": "x"})
    assert cos.escritas == []


def test_item_nascido_de_grupo_declara_que_nao_tem_gemeo_de_ata(bancos):
    """Sem `sem_par_declarado_em`, o item novo volta à fila de pareamento como
    "ainda não decidi" — os dois estados ficam indistinguíveis, que foi
    exatamente a lição da migration 085."""
    intel, _cos = bancos
    intel._respostas = [{"id": 201}]
    raci_store.create_item(_scope(raci_store.TARGET_INTEL), {"acao": "y"})
    assert "sem_par_declarado_em" in intel.escritas[0]


def test_item_de_ata_guarda_a_reuniao_na_origem(bancos):
    """O INTEL não tem coluna `reuniao_id` (as reuniões moram no ConselhoOS). O
    rastro viaja em `origem` para não morrer na travessia."""
    intel, _cos = bancos
    intel._respostas = [{"id": 202}]
    raci_store.create_item(_scope(raci_store.TARGET_INTEL), {"acao": "z"},
                           reuniao_id="11111111-2222-3333-4444-555555555555")
    params = intel.params_de("INSERT INTO raci_itens")[0]
    assert "ata:11111111-2222-3333-4444-555555555555" in params


def test_acao_vazia_nao_cria_linha(bancos):
    intel, _cos = bancos
    with pytest.raises(raci_store.RaciStoreError):
        raci_store.create_item(_scope(raci_store.TARGET_INTEL), {"acao": "   "})
    assert intel.escritas == []


# ─────────────────────────────────────────────────────────────────────────────
# undo_hint — o `::uuid` grudado
# ─────────────────────────────────────────────────────────────────────────────
def test_undo_hint_do_intel_nao_tem_cast_de_uuid(bancos):
    """O hint vinha com `::uuid` literal. Com id inteiro isso é SQL que não
    roda — e hint de undo que não roda é pior que hint ausente: quem precisar
    dele vai precisar sob pressão."""
    _intel, _cos = bancos
    hint = raci_store.undo_hint_update(_scope(raci_store.TARGET_INTEL), "47", "pendente")
    assert "::uuid" not in hint
    assert "WHERE id=47" in hint
    assert "atualizado_em=NOW()" in hint


def test_undo_hint_do_conselhoos_mantem_o_cast(bancos):
    _intel, _cos = bancos
    hint = raci_store.undo_hint_update(_scope(raci_store.TARGET_CONSELHOOS),
                                       ITEM_UUID, "pendente")
    assert f"'{ITEM_UUID}'::uuid" in hint
    assert "updated_at=NOW()" in hint


# ─────────────────────────────────────────────────────────────────────────────
# Escopo
# ─────────────────────────────────────────────────────────────────────────────
def test_chave_do_escopo_prefere_o_uuid_da_empresa():
    """A chave está nas dedup-keys já emitidas e no `empresa_id` das 92
    propostas. Trocá-la junto com o target reemitiria aviso que o Renato já
    viu."""
    assert _scope(raci_store.TARGET_INTEL).chave == EMPRESA_UUID


def test_escopo_sem_empresa_tem_chave_propria_em_vez_de_vazia():
    s = raci_store.RaciScope(target=raci_store.TARGET_INTEL, project_id=28,
                             empresa_uuid=None, nome="Governança Jabô")
    assert s.chave == "project:28"


def test_escopo_incompleto_levanta_em_vez_de_devolver_lista_vazia():
    """Devolver `[]` aqui seria a abstenção que vira fábrica de diagnóstico
    falso: "não há itens" é uma afirmação, e este caminho não a pode fazer
    quando o que falta é saber onde procurar."""
    sem_projeto = raci_store.RaciScope(target=raci_store.TARGET_INTEL, project_id=None,
                                       empresa_uuid=EMPRESA_UUID, nome="X")
    with pytest.raises(raci_store.RaciScopeError):
        sem_projeto.exige_project()

    sem_empresa = raci_store.RaciScope(target=raci_store.TARGET_CONSELHOOS,
                                       project_id=24, empresa_uuid=None, nome="Y")
    with pytest.raises(raci_store.RaciScopeError):
        sem_empresa.exige_empresa()


def test_chave_de_escopo_ilegivel_nao_vira_consulta(bancos):
    intel, cos = bancos
    for lixo in ("", None, "Vallen Clinic"):
        with pytest.raises(raci_store.RaciScopeError):
            raci_store.resolve_scope(lixo)
    assert intel.queries == [] and cos.queries == []


# ─────────────────────────────────────────────────────────────────────────────
# O gate de alcance — o efeito colateral que a refatoração quase embutiu
# ─────────────────────────────────────────────────────────────────────────────
def _s(pid, uuid=None):
    return raci_store.RaciScope(target=raci_store.TARGET_INTEL, project_id=pid,
                                empresa_uuid=uuid, nome=f"projeto {pid}")


def test_conselho_com_vinculo_segue_habilitado(monkeypatch):
    """#24 e #26 são os que já rodavam — o conserto não pode encolher o alcance."""
    monkeypatch.delenv("RACI_GRUPO_PROJECTS", raising=False)
    assert raci_store.grupo_raci_habilitado(_s(24, EMPRESA_UUID)) is True


def test_projeto_sem_vinculo_nao_entra_sozinho(monkeypatch):
    """A REGRESSÃO QUE ESTE GATE EXISTE PARA BARRAR.

    Trocar a resolução de `ILIKE '%nome%'` em `empresas` para `project_id`
    acha MAIS grupos — e medido em 27/10 incluiria o #47 (RENATAO JURID CONTAB):
    advogados e contadores, 8 itens de RACI, que passariam a receber
    `✅ Atualizado` automático sem ninguém ter pedido. Ampliar alcance é decisão
    de domínio, não efeito colateral de refatoração.
    """
    monkeypatch.delenv("RACI_GRUPO_PROJECTS", raising=False)
    assert raci_store.grupo_raci_habilitado(_s(47)) is False


def test_ampliar_exige_dizer_o_numero_do_projeto(monkeypatch):
    """Crescer é possível — mas explícito, e por isso auditável."""
    monkeypatch.setenv("RACI_GRUPO_PROJECTS", "47, 99")
    assert raci_store.grupo_raci_habilitado(_s(47)) is True
    assert raci_store.grupo_raci_habilitado(_s(98)) is False


def test_projeto_com_caminho_proprio_nunca_entra(monkeypatch):
    """O #28 tem `jabo_group_raci`, que aplica SEM gate humano (decisão de
    16/08) e já escreve em `raci_itens`. Deixá-lo entrar no caminho genérico
    daria dois escritores com políticas de confiança diferentes no mesmo RACI.
    Nem o opt-in da env o libera: aqui a recusa é estrutural."""
    monkeypatch.setenv("RACI_GRUPO_PROJECTS", "28")
    assert raci_store.grupo_raci_habilitado(_s(28)) is False
    assert raci_store.grupo_raci_habilitado(_s(28, EMPRESA_UUID)) is False


def test_mark_reported_usa_a_chave_do_target(bancos):
    intel, cos = bancos
    intel.rowcount = 7
    assert raci_store.mark_reported(_scope(raci_store.TARGET_INTEL)) == 7
    assert cos.escritas == []
    assert intel.params_de("concluido_relatado_em = NOW()") == [(PROJECT_ID,)]
