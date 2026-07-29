"""
Testes do digest de grupos reescrito (29/07).

O digest velho mandava 6.450 caracteres com 15 grupos, ordenados por VOLUME de
mensagens — sete dos quais diziam textualmente "Menção a Renato: Nenhuma". O
Renato: "está gerando zero de valor".

O que estes testes seguram, na ordem em que os defeitos apareceram no ensaio
contra os dados reais:

  1. Nada relevante => NÃO manda digest (silêncio é resposta).
  2. Ordenação por acionabilidade, nunca por volume — foi o volume que jogou o
     Conselho Vallen (6 msgs) para depois de 5.000 caracteres de fofoca.
  3. `acao_sua` só sobrevive com uma PESSOA nomeada esperando: sem isso o
     modelo inventa tarefa ("Avaliar implicações legais da ordem judicial") e
     cada invenção vira uma interrupção no WhatsApp.
  4. Verbo de espera e direção invertida não são ação.
  5. Teto de tamanho, com o que sobrou declarado.

Rodar: python -m pytest tests/test_group_digest.py -v
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import group_digest as gd  # noqa: E402


# ── _parse_avaliacao ─────────────────────────────────────────────────────────
def test_parse_relevante_completo():
    raw = json.dumps({
        "relevante": True, "motivo": "frente",
        "linha": "Thalita pediu a lista de VIPs; Lara tem os dados e analisa hoje.",
        "quem_espera": "Thalita", "acao_sua": "Aprovar o corte da lista de VIPs",
    })
    d = gd._parse_avaliacao(raw)
    assert d["relevante"] is True
    assert d["acao_sua"] == "Aprovar o corte da lista de VIPs"


def test_parse_irrelevante():
    d = gd._parse_avaliacao(json.dumps({"relevante": False, "motivo": "nada"}))
    assert d["relevante"] is False


def test_parse_resposta_sem_json_vira_irrelevante():
    """Na dúvida, silêncio — nunca uma linha 'resumo indisponível' ocupando espaço."""
    assert gd._parse_avaliacao("não consegui")["relevante"] is False


def test_parse_relevante_sem_linha_e_descartado():
    """'Relevante' sem fato é o mesmo que irrelevante."""
    raw = json.dumps({"relevante": True, "linha": "   ", "motivo": "frente"})
    assert gd._parse_avaliacao(raw)["relevante"] is False


def test_parse_corta_linha_na_palavra():
    longa = "palavra " * 40
    d = gd._parse_avaliacao(json.dumps({"relevante": True, "linha": longa,
                                        "quem_espera": None, "acao_sua": None}))
    assert len(d["linha"]) <= gd.MAX_LINHA_CHARS + 1
    assert d["linha"].endswith("…")
    assert not d["linha"].endswith("palavr…")  # não corta no meio da palavra


# ── _limpa_acao ──────────────────────────────────────────────────────────────
def test_acao_exige_pessoa_nomeada():
    """A classe de defeito mais cara do ensaio: o modelo inventando tarefa.

    'Avaliar implicações legais da ordem judicial' não foi pedido por ninguém —
    e, como `acao_sua` decide entre interromper no WhatsApp e mandar push,
    deixar passar reconstrói o problema que a reescrita veio resolver.
    """
    assert gd._limpa_acao("Avaliar implicações legais da ordem judicial", None) is None
    assert gd._limpa_acao("Analisar o impacto da decisão", "o grupo") is None
    assert gd._limpa_acao("Responder a proposta", "Thalita") == "Responder a proposta"


def test_acao_rejeita_coletivo_como_quem_espera():
    for coletivo in ("o grupo", "o conselho", "a equipe", "todos", "N/A"):
        assert gd._limpa_acao("Fazer alguma coisa", coletivo) is None


def test_verbo_de_espera_nao_e_acao():
    assert gd._limpa_acao("Aguardar respostas do grupo", "Nuno") is None
    assert gd._limpa_acao("Acompanhar o andamento", "Priscila") is None
    assert gd._limpa_acao("Monitorar a evolução", "Lara") is None


def test_direcao_invertida_nao_e_acao():
    """'Renato perguntou X — aguarda resposta dela' põe o Renato na ponta que
    espera. Quem espera não tem tarefa."""
    assert gd._limpa_acao(
        "Renato perguntou se há algo travando — aguarda resposta dela", "Priscila") is None


def test_acao_vazia():
    assert gd._limpa_acao(None, "Thalita") is None
    assert gd._limpa_acao("   ", "Thalita") is None


# ── _format_digest ───────────────────────────────────────────────────────────
def _item(group, linha="fato", projeto=None, acao=None, msgs=5):
    return {"group": group, "projeto": projeto, "linha": linha,
            "acao_sua": acao, "messages": msgs, "relevante": True}


def test_ordena_por_acionabilidade_nao_por_volume():
    """O defeito estrutural do digest velho, num teste.

    O grupo tagarela (200 msgs, nada esperado) não pode vir antes do item que
    pede algo do Renato (3 msgs).
    """
    itens = [
        _item("Tagarela", msgs=200),
        _item("Vallen", projeto="Vallen Clinic", acao="Aprovar a lista", msgs=3),
        _item("Frente sem acao", projeto="Jabô", msgs=50),
    ]
    texto, _ = gd._format_digest(itens, itens)
    pos = {n: texto.find(n) for n in ("Vallen", "Frente sem acao", "Tagarela")}
    assert pos["Vallen"] < pos["Frente sem acao"] < pos["Tagarela"]


def test_cabecalho_declara_denominador():
    """Dizer '5 de 18' prova que os outros 13 foram olhados e descartados —
    sem isso, o digest parece ter perdido coisa."""
    relevantes = [_item("A"), _item("B")]
    avaliados = relevantes + [{"group": f"g{i}", "relevante": False} for i in range(11)]
    texto, _ = gd._format_digest(relevantes, avaliados)
    assert "2 de 13" in texto


def test_teto_de_tamanho_e_o_que_sobrou_e_declarado():
    """Truncar em silêncio lê como 'era só isso' — o mesmo erro de régua que já
    mordeu o projeto antes."""
    itens = [_item(f"Grupo {i}", linha="x" * 170) for i in range(30)]
    texto, cortados = gd._format_digest(itens, itens)
    assert len(texto) <= gd.MAX_DIGEST_CHARS + 40  # + a linha do aviso
    assert cortados > 0
    assert f"+{cortados} não coube" in texto


def test_acao_aparece_marcada():
    texto, _ = gd._format_digest([_item("Vallen", acao="Aprovar a lista")],
                                 [_item("Vallen", acao="Aprovar a lista")])
    assert "↳ você: Aprovar a lista" in texto


def test_nome_de_grupo_longo_e_encurtado():
    nome = "ACFICTOR - Canal Interativo: Perguntas e Respostas"
    texto, _ = gd._format_digest([_item(nome)], [_item(nome)])
    assert nome not in texto
    assert "ACFICTOR" in texto


def test_corta_na_palavra():
    assert gd._corta("um dois tres quatro", 12) == "um dois…"
    assert gd._corta("curto", 50) == "curto"
