"""O RACI VIVE EM DOIS BANCOS E O GRUPO DO CLIENTE RECEBIA LINHA REPETIDA.

Decisão do Renato em 26/09/26: fonte única. Medido em prod em 05/10, projeto 24
(Vallen Clinic): `get_matrix` devolve **39 registros abertos para 23 itens reais**
(+70%), porque INTEL e ConselhoOS guardam as mesmas ações de reunião. A Alba
(projeto 26) tem inflação maior.

O risco agudo não é de código, é de governança: `POST /raci/send-to-group` publica
a matriz no grupo de WhatsApp do CLIENTE. Até 05/10 a mitigação era VERBAL — a CoS
disse ao Renato para não apertar o botão. Instrução verbal não é guarda.

A ASSIMETRIA QUE DECIDE O DESENHO, e é o que estes testes protegem:

    detectar para BLOQUEAR  → falso positivo estorva um envio (recuperável, 1 clique)
    detectar para FUNDIR    → falso positivo APAGA responsabilidade de cliente do
                              painel, e ninguém descobre

Por isso aqui não se funde nada: todos os itens continuam na lista, marcados. E a
detecção usa só texto NORMALIZADO (caixa/acento/pontuação), nunca similaridade.
Medido no próprio conjunto em 05/10: "Regra de repasse da **Dra. Daniela**" ×
"Acordo de repasse da **Dra. Sayonê**" dá 0.58 no SequenceMatcher — duas médicas
diferentes, contratos diferentes. Qualquer corte que pegasse os pares de vocabulário
divergente ("a Gestora" × "Jéssica") passaria por cima desse 0.58 e fundiria
contrato de médica.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_raci_duas_fontes.py -q
"""
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))

from services import raci_matrix as rm  # noqa: E402


def _item(fonte, ident, acao, status="pendente", prazo=None, task_id=None):
    return rm._normalize(
        {"id": ident, "acao": acao, "status": status, "prazo": prazo,
         "task_id": task_id, "area": None, "responsavel_r": None,
         "responsavel_a": None, "responsavel_c": None, "responsavel_i": None,
         "notas": None, "concluido_em": None, "concluido_em_fonte": None},
        fonte,
    )


# ===========================================================================
# 1. A chave de dedup: mesma frase sim, frase parecida NUNCA
# ===========================================================================

def test_chave_ignora_caixa_acento_e_pontuacao():
    a = rm._chave_dedup("Contrato da Dra. Camila — redigir na base do MODELO 3")
    b = rm._chave_dedup("contrato da dra camila  redigir na base do modelo 3")
    assert a == b and a


def test_chave_NAO_aproxima_medicas_diferentes():
    """O falso positivo concreto que mataria a abordagem por similaridade.

    Estas duas frases dão 0.58 de SequenceMatcher — alto o bastante para
    qualquer corte frouxo. São contratos de DUAS médicas. A chave normalizada as
    separa por construção, porque normalizar não aproxima: só desfaz grafia.
    """
    d = rm._chave_dedup("Regra de repasse da Dra. Daniela por escrito — 12% do faturamento")
    s = rm._chave_dedup("Acordo de repasse da Dra. Sayonê (vascular) por escrito")
    assert d != s

    from difflib import SequenceMatcher
    assert SequenceMatcher(None, d, s).ratio() > 0.4, (
        "se a similaridade entre as duas caísse, este teste perderia o sentido — "
        "ele existe porque elas SÃO parecidas e ainda assim não podem casar"
    )


def test_chave_vazia_nunca_pareia():
    """Ação vazia nos dois lados geraria par de tudo-com-tudo. É o modo de falha
    em que a guarda bloquearia todo envio por um item sem texto."""
    assert rm._chave_dedup("") == ""
    assert rm._chave_dedup("   —  ") == ""
    pares = rm._detectar_duplicatas([
        _item(rm.FONTE_INTEL, 1, ""),
        _item(rm.FONTE_CONSELHOOS, "u1", "  "),
    ])
    assert pares == []


# ===========================================================================
# 2. Detecta, e NÃO funde
# ===========================================================================

def test_detecta_o_par_entre_as_duas_fontes():
    itens = [
        _item(rm.FONTE_INTEL, 50, "Auditoria de processos — 30/09"),
        _item(rm.FONTE_CONSELHOOS, "abc", "auditoria de processos 30 09"),
    ]
    pares = rm._detectar_duplicatas(itens)
    assert len(pares) == 1
    assert pares[0]["intel_uid"] == "intel:50"
    assert pares[0]["conselhoos_uid"] == "conselhoos:abc"
    assert pares[0]["motivo"] == "texto_identico"


def test_nao_pareia_dentro_da_MESMA_fonte():
    """Duas linhas iguais no mesmo banco são outro problema (digitação dupla), e
    fundi-las aqui esconderia o erro em vez de mostrá-lo. O par só existe
    atravessando as fontes."""
    itens = [
        _item(rm.FONTE_INTEL, 1, "mesma acao"),
        _item(rm.FONTE_INTEL, 2, "mesma acao"),
    ]
    assert rm._detectar_duplicatas(itens) == []


def test_marca_divergencia_de_status_entre_as_copias():
    """O sinal que a CoS obteve à mão em 05/10: quando as duas cópias discordam,
    alguém atualizou só um lado — e esse é o item que precisa de atenção."""
    itens = [
        _item(rm.FONTE_INTEL, 1, "acao x", status="em_andamento"),
        _item(rm.FONTE_CONSELHOOS, "u", "acao x", status="pendente"),
    ]
    assert rm._detectar_duplicatas(itens)[0]["status_divergente"] is True

    iguais = [
        _item(rm.FONTE_INTEL, 1, "acao y", status="pendente"),
        _item(rm.FONTE_CONSELHOOS, "u", "acao y", status="pendente"),
    ]
    assert iguais and rm._detectar_duplicatas(iguais)[0]["status_divergente"] is False


# ===========================================================================
# 3. O resumo voltou a mostrar movimento
# ===========================================================================

def test_status_efetivo_engole_em_andamento_quando_o_prazo_venceu():
    """A causa do defeito, fixada: NÃO é bug do `status_efetivo` — é correto que
    item vencido apareça como atrasado. O bug era o resumo ter só essa dimensão.
    """
    from datetime import date, timedelta
    venceu = date.today() - timedelta(days=10)
    it = _item(rm.FONTE_INTEL, 1, "acao", status="em_andamento", prazo=venceu)
    assert it["status"] == "em_andamento"
    assert it["status_efetivo"] == "atrasado"


def test_item_sem_prazo_nunca_vira_atrasado():
    """Responsabilidade permanente ("coordenação da cadência") não vence. Marcá-la
    de vermelho corroeu a credibilidade do RACI do Vallen em 13/07 — a propriedade
    é antiga e não pode regredir junto com a mudança do resumo."""
    it = _item(rm.FONTE_INTEL, 1, "coordenacao da cadencia", status="pendente", prazo=None)
    assert it["status_efetivo"] == "pendente"


def test_rotulo_de_movimento_diz_as_duas_dimensoes():
    """"30 atrasados, dos quais 17 com movimento" — a frase que o painel e o grupo
    usam sem recalcular (e sem divergir entre si)."""
    from datetime import date, timedelta
    venceu = date.today() - timedelta(days=5)
    itens = [
        _item(rm.FONTE_INTEL, 1, "a", status="em_andamento", prazo=venceu),
        _item(rm.FONTE_INTEL, 2, "b", status="em_andamento", prazo=venceu),
        _item(rm.FONTE_INTEL, 3, "c", status="pendente", prazo=venceu),
    ]
    atrasados = sum(1 for i in itens if i["status_efetivo"] == "atrasado")
    com_mov = sum(1 for i in itens
                  if i["status_efetivo"] == "atrasado" and i["status"] == "em_andamento")
    assert (atrasados, com_mov) == (3, 2), (
        "os três vencem, e DOIS têm movimento declarado — se o resumo só "
        "contasse status_efetivo, o painel mostraria 'em andamento: 0'"
    )


# ===========================================================================
# 4. A cobertura é dita em voz alta, não assumida
# ===========================================================================

def test_a_deteccao_declara_que_NAO_pega_tudo():
    """Medido em prod 05/10: a chave pega 9 de ~17 pares na Vallen e 10 de ~12 na
    Alba. O resíduo é o de vocabulário divergente ("a Gestora" × "Jéssica"), que
    texto nenhum resolve — e fica VISIVELMENTE duplicado, que é o estado honesto.

    A guarda do envio é binária: 9 pares já bloqueiam. Cobertura parcial na
    detecção não é cobertura parcial na proteção
    ([[feedback_regua_cobertura_parcial]]).
    """
    doc = rm._detectar_duplicatas.__doc__ or ""
    assert "9 pares" in doc and "10" in doc, (
        "a cobertura medida tem de estar escrita onde quem lê a função a vê"
    )
    assert "NUNCA FUNDIR" in doc


def test_itens_continuam_todos_na_lista(monkeypatch):
    """A prova de que não se funde: a detecção não remove nada."""
    itens = [
        _item(rm.FONTE_INTEL, 1, "acao igual"),
        _item(rm.FONTE_CONSELHOOS, "u", "acao igual"),
        _item(rm.FONTE_INTEL, 2, "acao solitaria"),
    ]
    antes = len(itens)
    rm._detectar_duplicatas(itens)
    assert len(itens) == antes == 3


# ===========================================================================
# 5. A guarda na ORIGEM — dedup na leitura é band-aid enquanto o produtor vive
# ===========================================================================

def test_create_recusa_item_que_ja_existe_no_conselhoos(monkeypatch):
    """O produtor foi medido: NÃO é cron. Os 25 itens do projeto 24 são todos
    `origem='manual'`, em 5 datas; os da Alba vêm de `raci_grupo_04set` e
    `ata_alba_07_08`. É sessão transcrevendo ata numa empresa cujas ações já
    estão no ConselhoOS — então a guarda vai onde a cópia nasce."""
    monkeypatch.setattr(rm, "_ja_existe_no_conselhoos", lambda pid, acao: "conselhoos:abc")
    out = rm.create_item(24, {"acao": "Auditoria de processos — 30/09"})
    assert "error" in out and "ConselhoOS" in out["error"]
    assert out["duplicata_de"] == "conselhoos:abc"


def test_create_com_override_nem_consulta_o_outro_banco(monkeypatch):
    """O override existe porque pode haver item legitimamente distinto com a
    mesma redação — mas tem de ser DECLARADO, não ser o default.

    ⚠️ A primeira versão deste teste chamava `create_item` com o override e
    deixava o fluxo seguir: ele INSERIU uma linha de verdade no banco local
    (`#62 acao='x'`, projeto 24), que eu tive de apagar à mão. Teste que escreve
    em banco compartilhado não é teste, é efeito colateral — e com
    `TEST_DB_TARGET=prod` teria sujado a matriz de um CLIENTE. Agora o `get_db`
    é interceptado e o INSERT nunca acontece: o que se verifica é a decisão
    (passou da guarda sem consultar), não a escrita.
    """
    consultou = []
    monkeypatch.setattr(rm, "_ja_existe_no_conselhoos",
                        lambda pid, acao: consultou.append(pid) or "conselhoos:abc")

    class _Barreira:
        def cursor(self):
            raise AssertionError("o teste não deve chegar ao banco")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(rm, "get_db", lambda: _Barreira())

    with pytest.raises(AssertionError, match="não deve chegar ao banco"):
        rm.create_item(24, {"acao": "x", "permitir_duplicata": True})

    assert consultou == [], (
        "com `permitir_duplicata=True` a checagem é pulada por completo — "
        "consultar o outro Neon para depois ignorar a resposta é custo puro"
    )


def test_checagem_na_criacao_ABSTEM_quando_o_outro_banco_cai(monkeypatch):
    """Assimetria oposta à da guarda do envio, e é deliberada.

    No ENVIO, não poder checar BLOQUEIA: o custo de publicar linha repetida no
    grupo do cliente é alto e o envio pode esperar. Na CRIAÇÃO, não poder checar
    LIBERA: barrar o trabalho do dia porque um banco que não é o nosso está fora
    seria pior, e a duplicata que nasce ainda encontra duas redes depois dela — a
    detecção na leitura e o bloqueio no envio.
    """
    monkeypatch.setattr(rm, "_fetch_conselhoos_status",
                        lambda uuid: ([], "ConselhoOS fora do ar"))

    class _Cur:
        def execute(self, *a): pass
        def fetchone(self): return {"conselhoos_empresa_id": "uuid-qualquer"}

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(rm, "get_db", lambda: _Conn())
    assert rm._ja_existe_no_conselhoos(24, "qualquer acao") is None


# ===========================================================================
# 6. O pareamento (passo 3) — PROPÕE, não decide
# ===========================================================================

def _fake_db(monkeypatch, proj_row, intel_rows):
    class _Cur:
        def __init__(self): self._r = None
        def execute(self, sql, *a):
            self._r = ("proj" if "FROM projects p" in sql else "intel")
        def fetchone(self): return proj_row if self._r == "proj" else None
        def fetchall(self): return intel_rows if self._r == "intel" else []

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(rm, "get_db", lambda: _Conn())


def test_pareamento_marca_o_exato_e_ORDENA_o_resto(monkeypatch):
    """O exato vem primeiro e marcado; o resto entra só ordenado por semelhança.

    A ordem é para o olho, não é palpite de resposta — por isso `exato` é um
    booleano separado do `score`, e não "score >= X".
    """
    _fake_db(monkeypatch,
             {"id": 24, "nome": "Vallen", "conselhoos_empresa_id": "uuid"},
             [{"id": 1, "acao": "Auditoria de processos — 30/09", "responsavel_r": "Amadeo",
               "prazo": None, "status": "pendente", "conselhoos_raci_id": None}])
    monkeypatch.setattr(rm, "_fetch_conselhoos_status", lambda u: ([
        _item(rm.FONTE_CONSELHOOS, "c1", "auditoria de processos 30 09"),
        _item(rm.FONTE_CONSELHOOS, "c2", "Auditoria de processos da Dra. Camila"),
        _item(rm.FONTE_CONSELHOOS, "c3", "Publicar o site"),
    ], None))

    out = rm.propor_pareamento(24)
    assert out["total_pendentes"] == 1 and out["com_sugestao_exata"] == 1
    cands = out["pendentes"][0]["candidatos"]
    assert cands[0]["exato"] is True and cands[0]["id"] == "c1"
    assert all(c["exato"] is False for c in cands[1:])
    assert [c["id"] for c in cands] == sorted(
        [c["id"] for c in cands], key=lambda i: -next(x["score"] for x in cands if x["id"] == i)
    ), "os não-exatos têm de vir em ordem decrescente de semelhança"


def test_pareamento_nao_oferece_linha_de_ata_ja_usada(monkeypatch):
    """Duas execuções não podem apontar para a mesma deliberação. O índice único
    da 084 barra no banco; aqui a lista nem oferece, pra não convidar ao erro."""
    _fake_db(monkeypatch,
             {"id": 24, "nome": "Vallen", "conselhoos_empresa_id": "uuid"},
             [{"id": 1, "acao": "acao x", "responsavel_r": None, "prazo": None,
               "status": "pendente", "conselhoos_raci_id": None},
              {"id": 2, "acao": "acao x", "responsavel_r": None, "prazo": None,
               "status": "pendente", "conselhoos_raci_id": "c1"}])
    monkeypatch.setattr(rm, "_fetch_conselhoos_status", lambda u: (
        [_item(rm.FONTE_CONSELHOOS, "c1", "acao x")], None))

    out = rm.propor_pareamento(24)
    assert out["ja_pareados"] == 1
    assert out["conselhoos_livres"] == 0
    assert out["pendentes"][0]["candidatos"] == [], (
        "c1 já está apontada pelo item 2 — oferecê-la de novo convidaria ao erro"
    )


def test_conselhoos_fora_do_ar_NAO_vira_lista_vazia(monkeypatch):
    """O modo de falha que estragaria o banco de forma irreversível: sem o outro
    lado, todo item ficaria "sem candidato" e o usuário marcaria tudo como "sem
    par" — congelando o erro. Aqui devolve erro e a tela se recusa a parear."""
    _fake_db(monkeypatch,
             {"id": 24, "nome": "Vallen", "conselhoos_empresa_id": "uuid"},
             [{"id": 1, "acao": "a", "responsavel_r": None, "prazo": None,
               "status": "pendente", "conselhoos_raci_id": None}])
    monkeypatch.setattr(rm, "_fetch_conselhoos_status", lambda u: ([], "fora do ar"))
    out = rm.propor_pareamento(24)
    assert "error" in out and "indisponível" in out["error"]
    assert "pendentes" not in out


def test_item_declarado_sem_par_NAO_volta_a_ser_oferecido(monkeypatch):
    """A decisão de "nunca passou por conselho" tem de SOBREVIVER à tela.

    O defeito que a migration 085 conserta, achado em 06/10 logo depois de o
    Renato terminar o passo 3: "sem par" gravava `conselhoos_raci_id = NULL` —
    o MESMO valor de "ainda não decidi". Reabrir a tela mostrava os 12 itens
    decididos como pendentes outra vez, e ninguém conseguia responder se o
    pareamento tinha acabado, que é o gate da importação. Ausência não se
    audita; afirmação datada, sim.
    """
    _fake_db(monkeypatch,
             {"id": 24, "nome": "Vallen", "conselhoos_empresa_id": "uuid"},
             [{"id": 1, "acao": "nunca passou por conselho", "responsavel_r": None,
               "prazo": None, "status": "pendente", "conselhoos_raci_id": None,
               "sem_par_declarado_em": datetime(2026, 10, 6, 17, 20)},
              {"id": 2, "acao": "ainda não decidido", "responsavel_r": None,
               "prazo": None, "status": "pendente", "conselhoos_raci_id": None,
               "sem_par_declarado_em": None}])
    monkeypatch.setattr(rm, "_fetch_conselhoos_status", lambda u: (
        [_item(rm.FONTE_CONSELHOOS, "c9", "nunca passou por conselho")], None))

    out = rm.propor_pareamento(24)
    assert out["declarados_sem_par"] == 1
    ids = [p["intel_id"] for p in out["pendentes"]]
    assert ids == [2], (
        "o item 1 já foi decidido — reofertá-lo é pedir de novo um trabalho feito, "
        "mesmo havendo uma linha de ata de texto idêntico esperando"
    )


def test_projeto_sem_vinculo_nao_entra_no_pareamento(monkeypatch):
    _fake_db(monkeypatch, {"id": 28, "nome": "Exportação", "conselhoos_empresa_id": None}, [])
    out = rm.propor_pareamento(28)
    assert out["error"] == "projeto sem vínculo ConselhoOS"


def test_sem_par_e_resposta_valida_nao_lacuna():
    """3 itens da Vallen (esteticista, Dra. Sayonê, Dra. Camila) são execução que
    nunca passou por conselho. Uma tela que só permitisse casar empurraria o
    usuário a inventar par pra fechar a lista — por isso `definir_par(id, None)`
    é um caminho de primeira classe, e o endpoint exige `sem_par=true` explícito
    em vez de aceitar corpo vazio."""
    import inspect
    src = inspect.getsource(rm.definir_par)
    assert "None" in src and "sem par" in (rm.definir_par.__doc__ or "")


def test_projeto_sem_vinculo_conselhoos_nao_e_checado(monkeypatch):
    """A maioria dos projetos não tem fonte-conselho, e isso não é estado
    degradado — o próprio módulo diz isso no topo. Checar ali seria custo por
    nada e, pior, um erro a explicar."""
    class _Cur:
        def execute(self, *a): pass
        def fetchone(self): return {"conselhoos_empresa_id": None}

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(rm, "get_db", lambda: _Conn())
    chamou = []
    monkeypatch.setattr(rm, "_fetch_conselhoos_status",
                        lambda uuid: chamou.append(uuid) or ([], None))
    assert rm._ja_existe_no_conselhoos(99, "acao") is None
    assert chamou == [], "não devia nem consultar o outro banco"
