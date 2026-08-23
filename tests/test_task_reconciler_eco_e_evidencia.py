"""
O ECO DO DONO E A CITACAO INVENTADA — 23/08/2026.

CASO REAL: o reconciler rodou pela 1a vez de verdade ao meio-dia de 23/08 e
fechou 2 tarefas do Renato. UMA estava errada.

  #426 "Definir microlote separavel para Portugal" — fechada com confianca 0,95
  e esta justificativa:

      "Mensagem de 14/07 documenta decisao tecnica completa: microlote = 10%
       peneira mais alta (~50 sacas), separacao na classificacao Guaxupe,
       destino container Orioli Portugal."

Medido em prod no mesmo dia, dois defeitos somados:

  (A) A ficha da task e a **#23419 — a do PROPRIO Renato**. `_task_scope`
      subtraia os ENDERECOS do dono desde 22/08, mas mantinha a FICHA dele em
      `contact_ids`; e a #23419 e onde mora o SELF-CHAT, o canal por onde o
      sistema fala com ele. As 11 mensagens que foram a julgamento eram
      briefings automaticos das 10h, e-mails que ele mesmo escreveu e A PROPRIA
      NOTIFICACAO DO RECONCILER ("Reconciliacao — fechei 2 tarefa(s)").
      **26 das 149 tasks pending** apontavam pra essa ficha.

  (B) NENHUMA das 11 mensagens citava microlote, peneira, Guaxupe ou Portugal, e
      nao havia mensagem de 14/07 no lote (as datas eram 24/06, 06/08, 18-23/08).
      A frase do veredito e, palavra por palavra, a ultima linha da DESCRICAO da
      propria tarefa — que vai no prompt. O modelo devolveu o enunciado como se
      fosse a prova.

O corte de confianca nao pega (B): 0,85 mede o quanto o modelo acredita, nao se
o que ele leu existe. Por isso a guarda e deterministica — citacao literal,
conferida em Python contra o texto exibido.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_task_reconciler_eco_e_evidencia.py -q
"""
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import task_reconciler as tr  # noqa: E402


# ===========================================================================
# 1. (A) A ficha do dono nao e "o terceiro"
# ===========================================================================

FICHA_DO_DONO = 23419


def _pg():
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        return psycopg2.connect(
            os.getenv("LOCAL_TEST_DATABASE_URL", "postgresql://rap@localhost:5432/intel"),
            cursor_factory=RealDictCursor, connect_timeout=2,
        )
    except Exception:
        return None


@pytest.fixture
def cursor_com_dono(monkeypatch):
    """Cursor sobre copia TEMPORARIA de `contacts` (rollback, nao escreve), com a
    ficha do dono e a de um terceiro. `owner_*` sao injetados: quem os resolve de
    verdade e o `contact_identity`, ja coberto pelo teste dele."""
    conn = _pg()
    if conn is None:
        pytest.skip("Postgres local indisponivel")
    cur = conn.cursor()
    cur.execute("""
        CREATE TEMPORARY TABLE contacts (id INT PRIMARY KEY, nome TEXT, emails JSONB) ON COMMIT DROP;
    """)
    cur.execute("""
        INSERT INTO contacts (id, nome, emails) VALUES
          (23419, 'Renato de Faria e Almeida Prado',
           '[{"type":"work","email":"renato@almeida-prado.com"},
             {"type":"home","email":"renato.almeida.prado@gmail.com"}]'),
          (2869, 'Joao Piccino',
           '[{"type":"work","email":"joao@piccino.com.br"}]'),
          -- ficha IRMA do dono: mesmo endereco, outro id. Se o filtro olhasse so
          -- o `contact_id` da task, ela voltaria pela porta dos fundos.
          (25407, 'Renato A Prado (duplicata)',
           '[{"type":"work","email":"renato@almeida-prado.com"}]');
    """)

    class _Conn:
        def cursor(self_):
            return cur

        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

    monkeypatch.setattr(tr, "get_db", lambda: _Conn())
    monkeypatch.setattr(
        tr, "owner_emails",
        lambda c: ["renato@almeida-prado.com", "renato.almeida.prado@gmail.com"],
    )
    monkeypatch.setattr(tr, "owner_contact_ids", lambda c: [23419, 25407])
    yield cur
    conn.rollback()
    conn.close()


def test_a_426_sai_do_escopo_inteira(cursor_com_dono):
    """O caso que fechou errado. Sem ficha de terceiro e sem endereco de terceiro
    no texto, nao ha com quem reconciliar — e escopo vazio significa que a task
    nem chega a ser julgada."""
    scope = tr._task_scope({
        "id": 426,
        "titulo": "Definir microlote separavel para Portugal",
        "descricao": ("Decisao tecnica da fazenda: qual variedade, qual altitude, "
                      "quantos kg, pontuacao alvo (>=85 SCA?), nome do microlote."),
        "contact_id": FICHA_DO_DONO,
    })
    assert scope["contact_ids"] == []
    assert scope["emails"] == []
    assert scope["origem"] == "dono"


def test_self_chat_nao_vira_evidencia(cursor_com_dono):
    """A consequencia pratica: com escopo vazio o `_scope_where` vira FALSE, e
    nenhuma linha de `messages` casa. Era por aqui que os briefings automaticos
    e a notificacao do proprio reconciler entravam como 'conversa'."""
    scope = tr._task_scope({"id": 426, "titulo": "x", "descricao": "",
                            "contact_id": FICHA_DO_DONO})
    cond, params = tr._scope_where(scope)
    assert cond == "FALSE" and params == []
    assert tr._fetch_messages_since(scope, "2026-01-01") == []


def test_task_no_dono_com_terceiro_no_texto_continua_alcancavel(cursor_com_dono):
    """A ficha do dono some, a task NAO. Se a descricao nomeia um terceiro por
    e-mail, ela segue reconciliavel — descartar por completo trocaria um
    falso-positivo por um falso-negativo."""
    scope = tr._task_scope({
        "id": 999, "titulo": "FUP Piccino",
        "descricao": "Cobrar resposta de joao@piccino.com.br sobre a minuta.",
        "contact_id": FICHA_DO_DONO,
    })
    assert scope["origem"] == "texto"
    assert scope["emails"] == ["joao@piccino.com.br"]
    assert 2869 in scope["contact_ids"]


def test_ficha_irma_do_dono_nao_entra_pela_porta_dos_fundos(cursor_com_dono):
    """A #25407 tem o mesmo endereco do dono. Uma task que cite
    `renato@almeida-prado.com` no texto nao pode resolver pra ela."""
    scope = tr._task_scope({
        "id": 998, "titulo": "Responder a si mesmo",
        "descricao": "Mandei pra renato@almeida-prado.com pra nao esquecer.",
        "contact_id": None,
    })
    assert 25407 not in scope["contact_ids"]
    assert scope["contact_ids"] == []
    assert scope["emails"] == []


def test_ficha_de_terceiro_segue_intacta(cursor_com_dono):
    """CONTRAPROVA — o conserto nao pode encolher o alcance de quem e terceiro
    de verdade. Sem esta, 'filtrar tudo' passaria nos testes acima."""
    scope = tr._task_scope({"id": 999695, "titulo": "FUP Piccino",
                            "descricao": "", "contact_id": 2869})
    assert scope["contact_ids"] == [2869]
    assert scope["emails"] == ["joao@piccino.com.br"]
    assert scope["origem"] == "ficha"


# ===========================================================================
# 2. (B) A citacao tem que existir no que foi mostrado
# ===========================================================================

def _msgs(*textos):
    return {i: t for i, t in enumerate(textos, start=1)}


# As 11 mensagens reais que foram a julgamento na #426, resumidas. Sao TODAS do
# proprio Renato ou do proprio sistema — e nenhuma fala de cafe.
EVIDENCIA_REAL_DA_426 = _msgs(
    "Pelo que entendi, se conseguir o DIP a priori. Saudacoes/Regards, Renato A Prado",
    "Bom dia.\n\n**Ponte cruzada:** O coquetel de hoje as 18h e de boas-vindas a "
    "Antikuna Logistics no Brasil, organizado pela Camara de Comercio Brasil-Peru.",
    "Reconciliacao (conversa/WA) — fechei 2 tarefa(s) que voce resolveu direto: "
    "#999782 [Board Hunt] Gui/FESA — Alicerce",
)

# O que o modelo devolveu em producao, palavra por palavra.
CITACAO_INVENTADA_DA_426 = (
    "Microlote = os 10% de PENEIRA mais alta da safra (~50 sacas). Separar ja na "
    "classificacao na Guaxupe. Destino: container do Orioli p/ Portugal."
)


def test_a_citacao_da_426_nao_existe_em_lugar_nenhum():
    """O nucleo do caso: a frase esta na DESCRICAO da tarefa, nao nas mensagens.
    A guarda a rejeita sem precisar entender de cafe."""
    ok, motivo = tr._evidencia_confere(CITACAO_INVENTADA_DA_426, EVIDENCIA_REAL_DA_426)
    assert ok is False
    assert "nenhuma das mensagens" in motivo


def test_citacao_literal_confere():
    ok, motivo = tr._evidencia_confere(
        "o coquetel de hoje as 18h e de boas-vindas a Antikuna", EVIDENCIA_REAL_DA_426
    )
    assert ok is True and motivo == ""


def test_veredito_sem_citacao_nao_fecha():
    """Omitir o campo e a saida obvia pra quem nao tem prova. Vale done=false."""
    assert tr._evidencia_confere("", EVIDENCIA_REAL_DA_426)[0] is False
    assert tr._evidencia_confere(None, EVIDENCIA_REAL_DA_426)[0] is False


def test_citacao_curta_vale_quando_e_a_mensagem_inteira():
    """CASO REAL #999921 (Carambola): a evidencia que fechou certo foi
    'Ja fiz a video' — 14 caracteres. Um piso cego mataria o fechamento correto
    junto com o inventado."""
    ok, _ = tr._evidencia_confere("Ja fiz a video", _msgs("Ja fiz a video"))
    assert ok is True


def test_citacao_curta_e_parcial_nao_passa():
    """'ok' aparece em meia base e casaria por acaso — e ai a guarda viraria
    carimbo. Trecho curto so vale se for a mensagem toda."""
    ok, motivo = tr._evidencia_confere("ok", _msgs("ok, combinado, mando amanha sem falta"))
    assert ok is False and "curta demais" in motivo


def test_aspas_curvas_nao_derrubam_citacao_verdadeira():
    """O modelo reescreve `"` como aspa curva ao copiar. Barrar por isso seria a
    guarda errando contra quem esta certo."""
    ok, _ = tr._evidencia_confere(
        '“ja fiz a video”, confirmou o cartorio',
        _msgs('"Ja fiz a video", confirmou o cartorio hoje de manha'),
    )
    assert ok is True


def test_espaco_e_quebra_de_linha_nao_contam():
    ok, _ = tr._evidencia_confere(
        "seguem   as 3\n\n duvidas do contrato", _msgs("Dr. Joao, seguem as 3 duvidas do contrato...")
    )
    assert ok is True


def test_citacao_alem_do_corte_de_exibicao_nao_passa():
    """O prompt trunca cada mensagem em MAX_MSG_CHARS. A conferencia compara
    contra o texto EXIBIDO — aceitar o que estava depois do corte deixaria passar
    justamente a invencao, ja que o modelo nunca viu aquilo."""
    inteiro = "A" * (tr.MAX_MSG_CHARS + 60) + " o segredo esta no fim"
    exibido = _msgs(tr._display_text({"conteudo": inteiro}))
    assert tr._evidencia_confere("o segredo esta no fim", exibido)[0] is False


# ===========================================================================
# 3. Controle positivo por sabotagem — o julgamento inteiro, com o LLM mockado
#    devolvendo EXATAMENTE o JSON que a producao devolveu em 23/08.
# ===========================================================================

class _FakeAnthropic:
    """Substitui o cliente da Anthropic devolvendo um JSON fixo."""

    resposta = ""

    def __init__(self, *a, **kw):
        self.messages = self

    def create(self, **kw):
        texto = _FakeAnthropic.resposta

        class _B:
            text = texto

        class _M:
            content = [_B()]

            def model_dump(self_):
                return {}

        return _M()


@pytest.fixture
def llm_mockado(monkeypatch):
    import types
    fake = types.ModuleType("anthropic")
    fake.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")
    yield


def _task_426():
    return {
        "id": 426,
        "titulo": "Definir microlote separavel para Portugal",
        "descricao": ("[14/07] Microlote = os 10% de PENEIRA mais alta da safra "
                      "(~50 sacas). Separar ja na classificacao na Guaxupe. "
                      "Destino: container do Orioli p/ Portugal."),
        "data_criacao": datetime(2026, 5, 13, 13, 42),
    }


def _msgs_426():
    return [
        {"direcao": "outgoing", "conteudo": t, "canal": "whatsapp",
         "parte": "Renato de Faria e Almeida Prado",
         "ts": datetime(2026, 8, 20 + i, 10, 0)}
        for i, t in enumerate(EVIDENCIA_REAL_DA_426.values())
    ]


def test_o_veredito_real_da_426_e_derrubado(llm_mockado):
    """SABOTAGEM: o julgamento recebe de volta o JSON de producao — done=true,
    0,95, e a justificativa que fechou a tarefa errada. Com a citacao conferida,
    ele nao passa. Sem a guarda, este teste falha (era o comportamento de 23/08)."""
    _FakeAnthropic.resposta = (
        '{"done": true, "confidence": 0.95, "evidencia_id": "M1", '
        f'"evidencia_trecho": "{CITACAO_INVENTADA_DA_426}", '
        '"reason": "Mensagem de 14/07 documenta decisao tecnica completa."}'
    )
    v = tr._judge(_task_426(), _msgs_426())
    assert v["done"] is False, "a #426 voltou a ser fechada por evidencia inexistente"
    assert v["confidence"] == 0.0
    assert v["evidencia_falha"], "a rejeicao tem que ficar registrada, nao ser silenciosa"


def test_veredito_com_citacao_verdadeira_passa(llm_mockado):
    """CONTRAPROVA — a guarda nao pode simplesmente reprovar tudo. Sem esta, um
    `return done=False` incondicional passaria no teste acima."""
    _FakeAnthropic.resposta = (
        '{"done": true, "confidence": 0.95, "evidencia_id": "M2", '
        '"evidencia_trecho": "O coquetel de hoje as 18h e de boas-vindas a Antikuna", '
        '"reason": "confirmado na conversa"}'
    )
    v = tr._judge(_task_426(), _msgs_426())
    assert v["done"] is True and v["confidence"] == 0.95
    assert "evidencia_falha" not in v


def test_done_false_nao_precisa_de_citacao(llm_mockado):
    """Quem nao fecha nao tem o que provar — exigir citacao no `false` so geraria
    ruido no log."""
    _FakeAnthropic.resposta = (
        '{"done": false, "confidence": 0.2, "reason": "conversa tangencial"}'
    )
    v = tr._judge(_task_426(), _msgs_426())
    assert v["done"] is False and "evidencia_falha" not in v


# ===========================================================================
# 4. O kill-switch barra a escrita, nao a medicao
# ===========================================================================

@pytest.mark.asyncio
async def test_dry_run_roda_com_a_chave_desligada(monkeypatch):
    """Enquanto o kill-switch tambem abortava o `dry_run`, a unica forma de saber
    se o conserto funcionou era RELIGAR em producao e olhar — gate que so se
    valida ligando nao e gate. Com `off` + dry_run, ele mede e nao escreve."""
    monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: False)
    monkeypatch.setattr(tr, "sweep_on_hold", lambda dry_run=False: {"disabled": True})
    monkeypatch.setattr(tr, "_fetch_candidate_tasks", lambda: ([], 7, 3))

    def _nao_pode_fechar(*a, **kw):
        raise AssertionError("dry_run com a chave off nao pode escrever")

    monkeypatch.setattr(tr, "_close_task", _nao_pode_fechar)

    r = await tr.run_task_reconciler(dry_run=True)
    assert r["disabled"] is True, "o resumo tem que dizer que a chave segue off"
    assert r["dry_run"] is True
    assert r["skipped_owner_only"] == 3, "a medicao tem que chegar ao resumo"


@pytest.mark.asyncio
async def test_execucao_real_continua_barrada_pela_chave(monkeypatch):
    """CONTRAPROVA — afrouxar o dry_run nao pode afrouxar a execucao. Sem esta, a
    mudanca acima teria religado o reconciler sem ninguem pedir."""
    monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: False)

    def _nao_pode_varrer():
        raise AssertionError("a chave off tem que abortar ANTES de varrer")

    monkeypatch.setattr(tr, "_fetch_candidate_tasks", _nao_pode_varrer)

    r = await tr.run_task_reconciler(dry_run=False)
    assert r == {"disabled": True}


def test_o_prompt_diz_que_plano_nao_e_acao(llm_mockado):
    """A #426 pede 5 definicoes e a nota que a 'fechou' era um PLANO em 3 passos.
    A regra passou a existir no prompt; este teste a prende ali — regra que sai
    sem ninguem notar volta a fechar plano como execucao."""
    capturado = {}

    class _Captura(_FakeAnthropic):
        def create(self_, **kw):
            capturado["prompt"] = kw["messages"][0]["content"]
            return super().create(**kw)

    import types
    fake = types.ModuleType("anthropic")
    fake.Anthropic = _Captura
    sys.modules["anthropic"] = fake
    _FakeAnthropic.resposta = '{"done": false, "confidence": 0.1, "reason": "x"}'
    tr._judge(_task_426(), _msgs_426())

    import unicodedata
    p = "".join(
        c for c in unicodedata.normalize("NFD", capturado["prompt"].upper())
        if not unicodedata.combining(c)
    ).replace("Ç", "C")
    assert "PLANO REGISTRADO NAO E ACAO CUMPRIDA" in p
    assert "A DESCRICAO DA TAREFA NAO E EVIDENCIA" in p
    assert "SO FECHA COM TODAS SATISFEITAS" in p
    # As mensagens tem que chegar rotuladas — a citacao se apoia nos rotulos.
    assert "[M1 " in capturado["prompt"] and "[M2 " in capturado["prompt"]
