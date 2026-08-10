"""O agente propõe mudança de cadastro — e o runner é quem escreve.

Diretriz do Renato (10/08/2026): a inteligência interpreta os fatos e atualiza o
conhecimento; na dúvida, aciona. O agente continua SEM credencial de escrita —
ele devolve `atualizacoes` no JSON e o runner aplica pela lista fechada de
`services/agent_write.py`.

O que estes testes guardam, em ordem de gravidade:

  1. A FRONTEIRA COME O CAMPO. O dict do debriefing é montado com chaves fixas:
     o que o agente devolve e não está listado some sem erro. Já aconteceu com
     `fatos_novos` na primeira rodada (03/08) — o prompt pedia, o persistidor
     esperava, e o meio descartava em silêncio.
  2. Confiança baixa vira PERGUNTA, não escrita nem silêncio. Silêncio aqui
     recria o caso Orbiz: o desencontro visto e ninguém avisado.
  3. Recusa é contada, nunca engolida. Proposta que some não pode ser
     distinguida de proposta que nunca existiu.
  4. Sem COS_RW_URL a camada avisa em vez de degradar calada.

Rodar: PYTHONPATH=app .venv/bin/python -m pytest tests/test_cos_agent_atualizacoes.py -v
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))


def _runner():
    spec = importlib.util.spec_from_file_location(
        "_cos_run", _ROOT / "scripts" / "cos_agent" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_cos_run"] = mod
    spec.loader.exec_module(mod)
    return mod


class _ConnFake:
    def __init__(self):
        self.sqls = []

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.sqls.append(" ".join(sql.split()))

    def fetchone(self):
        return {"id": 99}

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def runner(monkeypatch):
    mod = _runner()
    conn = _ConnFake()
    monkeypatch.setattr(mod, "_conn", lambda url: conn)
    return mod, conn


def _debrief(atualizacoes):
    return [{"project_id": 1, "frente": "Orbiz", "atualizacoes": atualizacoes}]


def test_proposta_valida_vira_escrita(runner):
    mod, conn = runner
    pl = mod.persistir_atualizacoes("postgres://fake", _debrief([{
        "operacao": "criar_frente_board_hunt",
        "dados": {"nome": "Orbiz / Rodrigo Pretola", "contato_id": 5245, "fase": 2},
        "motivo": "board registra Orbiz reativado desde 07/08 e não há linha no banco",
        "confianca": 0.9,
        "fato_origem": "messages#27573",
    }]))
    assert pl["escritas"] == 1 and pl["propostas"] == 0 and pl["recusadas"] == 0
    assert any("INSERT INTO board_hunt_frentes" in s for s in conn.sqls)
    assert any("INSERT INTO agent_writes" in s for s in conn.sqls), "escreveu sem livro-razão"


def test_confianca_baixa_vira_pergunta_com_payload(runner):
    """Não escreve e não cala: devolve o que perguntar e com que dados."""
    mod, conn = runner
    pl = mod.persistir_atualizacoes("postgres://fake", _debrief([{
        "operacao": "atualizar_fase_frente",
        "dados": {"fase": 4},
        "motivo": "talvez tenha avançado",
        "confianca": 0.4,
        "registro_id": 15,
    }]))
    assert pl["propostas"] == 1 and pl["escritas"] == 0
    det = pl["detalhe"][0]
    assert det["status"] == "proposta" and det["dados"] == {"fase": 4}
    assert not conn.sqls, "gravou apesar da confiança abaixo do piso"


def test_operacao_invalida_e_contada_nao_engolida(runner):
    mod, _ = runner
    pl = mod.persistir_atualizacoes("postgres://fake", _debrief([{
        "operacao": "apagar_contato", "dados": {"id": 1},
        "motivo": "limpeza", "confianca": 1.0,
    }]))
    assert pl["recusadas"] == 1
    assert pl["detalhe"][0]["status"] == "recusada"


def test_confianca_ausente_nao_vira_escrita(runner):
    """Campo faltando não pode virar 'confio o suficiente'."""
    mod, conn = runner
    pl = mod.persistir_atualizacoes("postgres://fake", _debrief([{
        "operacao": "registrar_nota_projeto",
        "dados": {"project_id": 1, "tipo": "camada", "conteudo": "x"},
        "motivo": "registro",
    }]))
    assert pl["escritas"] == 0 and pl["propostas"] == 1
    assert not conn.sqls


def test_teto_de_tres_por_frente(runner):
    mod, _ = runner
    uma = {
        "operacao": "registrar_nota_projeto",
        "dados": {"project_id": 1, "tipo": "camada", "conteudo": "x"},
        "motivo": "registro do fato interpretado", "confianca": 0.9,
    }
    pl = mod.persistir_atualizacoes("postgres://fake", _debrief([uma] * 6))
    assert pl["escritas"] == 3, "teto por frente não foi respeitado"


def test_sem_atualizacoes_nao_abre_conexao(monkeypatch):
    """Rodada normal não paga conexão à toa — a maioria não muda nada."""
    mod = _runner()
    def _explode(url):
        raise AssertionError("abriu conexão sem ter o que escrever")
    monkeypatch.setattr(mod, "_conn", _explode)
    assert mod.persistir_atualizacoes("postgres://fake", _debrief([]))["escritas"] == 0


def test_o_campo_sobrevive_a_fronteira():
    """A armadilha que já comeu `fatos_novos`: o dict do debriefing tem chaves
    fixas, então campo não listado some sem erro nenhum."""
    fonte = (_ROOT / "scripts" / "cos_agent" / "run.py").read_text(encoding="utf-8")
    i = fonte.index('"_meta": {"duracao_s"')
    trecho = fonte[i - 900:i]
    assert '"atualizacoes": d.get("atualizacoes")' in trecho, (
        "o dict do debriefing não carrega `atualizacoes` — o agente propõe e "
        "a fronteira descarta em silêncio"
    )


def test_sem_credencial_avisa_em_vez_de_degradar_calado():
    """Modo degradado silencioso é como se descobre em novembro que nada foi
    escrito desde agosto."""
    fonte = (_ROOT / "scripts" / "cos_agent" / "run.py").read_text(encoding="utf-8")
    i = fonte.index("COS_RW_URL ausente")
    trecho = fonte[i - 500:i + 200]
    assert "DESCARTADAS" in trecho and "stderr" in trecho


class _ConnProposta:
    """Cursor falso que simula o canal: `ja_pendente` liga o dedup."""

    def __init__(self, ja_pendente=False):
        self.ja_pendente = ja_pendente
        self.sqls = []
        self.params = []
        self.commits = 0

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.sqls.append(" ".join(sql.split()))
        self.params.append(params)

    def fetchone(self):
        return (1,) if (self.ja_pendente and "SELECT 1" in self.sqls[-1]) else None

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _item(op="atualizar_fase_frente", dados=None, rid=15):
    return {"operacao": op, "dados": dados if dados is not None else {"fase": 4},
            "motivo": "pode ter avançado", "confianca": 0.5,
            "registro_id": rid, "frente": "Orbiz"}


def test_duvida_vira_pergunta(monkeypatch):
    mod = _runner()
    conn = _ConnProposta()
    monkeypatch.setattr(mod, "_conn", lambda url: conn)
    assert mod.abrir_proposta("postgres://fake", _item()) is True
    assert any("INSERT INTO action_proposals" in s for s in conn.sqls)
    assert conn.commits == 1, "abriu a pergunta e não commitou"


def test_nao_repergunta_o_que_ja_esta_pendente(monkeypatch):
    """O agente roda 14×/dia: sem dedup, uma frente ambígua vira 14 perguntas
    idênticas por dia e afoga o canal — o caminho do ai_suggestions."""
    mod = _runner()
    conn = _ConnProposta(ja_pendente=True)
    monkeypatch.setattr(mod, "_conn", lambda url: conn)
    assert mod.abrir_proposta("postgres://fake", _item()) is False
    assert not any("INSERT INTO action_proposals" in s for s in conn.sqls)


def test_chave_de_dedup_ignora_o_motivo():
    """O motivo é prosa e muda a cada rodada; usá-lo na identidade faria toda
    pergunta parecer nova — o mesmo erro da memória identificada pelo título."""
    mod = _runner()
    a = mod._chave_dedup("atualizar_fase_frente", {"fase": 4}, 15)
    b = mod._chave_dedup("atualizar_fase_frente", {"fase": 5}, 15)
    assert a == b, "mudou a chave porque o dado mudou — vai reperguntar sem parar"
    c = mod._chave_dedup("atualizar_fase_frente", {"fase": 4}, 16)
    assert a != c, "alvos diferentes têm que ser perguntas diferentes"


def test_tipo_proprio_para_ser_medido(monkeypatch):
    """action_type próprio existe para medir SE esta pergunta é respondida, em
    vez de diluí-la no balde geral — o canal já vinha com 70% de expiração."""
    mod = _runner()
    conn = _ConnProposta()
    monkeypatch.setattr(mod, "_conn", lambda url: conn)
    mod.abrir_proposta("postgres://fake", _item())
    ins = next(s for s in conn.sqls if "INSERT INTO action_proposals" in s)
    assert "'camada_cadastro'" in ins


def test_falha_ao_perguntar_aparece(monkeypatch, capsys):
    """Dúvida que não vira pergunta e não vira erro é dúvida que nunca existiu."""
    mod = _runner()
    def _explode(url):
        raise RuntimeError("banco fora")
    monkeypatch.setattr(mod, "_conn", _explode)
    assert mod.abrir_proposta("postgres://fake", _item()) is False
    assert "NÃO virou proposta" in capsys.readouterr().err


def test_pendentes_saem_do_placar(runner):
    """O runner precisa receber a lista para abrir as perguntas — se ela não
    sair daqui, a dúvida morre no placar."""
    mod, _ = runner
    pl = mod.persistir_atualizacoes("postgres://fake", _debrief([{
        "operacao": "atualizar_fase_frente", "dados": {"fase": 4},
        "motivo": "incerto", "confianca": 0.3, "registro_id": 15,
    }]))
    assert len(pl["pendentes"]) == 1
    assert pl["pendentes"][0]["operacao"] == "atualizar_fase_frente"


def test_prompt_ensina_as_cinco_operacoes():
    """Operação que o portão aceita e o prompt não ensina é capacidade morta."""
    from services import agent_write
    prompt = (_ROOT / "scripts" / "cos_agent" / "prompt_frente.md").read_text(encoding="utf-8")
    for nome in agent_write.OPERACOES:
        assert nome in prompt, f"o prompt não ensina `{nome}` — o agente não vai propor"
    assert "0.75" in prompt, "o prompt não diz qual é o piso de confiança"
