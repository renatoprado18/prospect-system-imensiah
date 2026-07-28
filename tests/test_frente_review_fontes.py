"""
Testes da HIERARQUIA DE FONTES da camada de inteligência (28/07, task #999697).

O DEFEITO (medido em prod, não suposto)
----------------------------------------
A camada monta o prompt de cada frente com duas fontes que podem discordar:
`project_notes` (rascunho do dia) e `system_memories` (o espelho dos memos .md,
registro durável revisado pelo Renato). Três coisas estavam erradas:

  1. A hierarquia existia num COMENTÁRIO Python ("autoridade; trate como
     verdade registrada") — que o modelo nunca lê. No prompt os dois blocos
     eram neutros e a NOTA vinha primeiro, então na contradição vencia a
     saliência: a fonte mais frágil.
  2. Cada memória entrava truncada em 600 chars. Medido: 227 dos 229 memos são
     maiores que isso, e a entrega média era ~20% do conteúdo (2% nos maiores).
  3. Os documentos de PROCESSO (session_locks 167k, board CoS 100k, dev backlog
     154k) casavam com qualquer termo de busca — são o log de tudo — e ocupavam
     slot de memória em TODA frente, servindo 0,4% de si mesmos. Nas frentes
     #47, #24 e #28 isso queimava 1-2 dos 5 slots.

Rodar: .venv/bin/python -m pytest tests/test_frente_review_fontes.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import frente_review as fr  # noqa: E402


def gather(memories=None, notes=None):
    """Gather mínimo — só o que `_fmt_gather` precisa pra montar o prompt."""
    return {
        "project": {"id": 47, "nome": "Reorg 7 empresas", "descricao": ""},
        "tasks": [], "members": [], "dms": [], "routed": [], "groups": [],
        "renato_outbound": [],
        "memories": memories or [],
        "notes": notes or [],
    }


# ==================== hierarquia declarada NO PROMPT ====================

def test_a_regra_de_hierarquia_esta_no_system_e_nao_so_num_comentario():
    """O modelo lê o system prompt; não lê comentário Python. A regra tem que
    estar onde ele enxerga."""
    s = fr._SYSTEM.lower()
    assert "hierarquia das fontes" in s
    assert "memória vence" in s


def test_o_system_manda_registrar_a_divergencia_e_nao_so_escolher():
    """Escolher a fonte certa em silêncio conserta a resposta e esconde o
    problema — a nota errada continua lá pro próximo consumidor."""
    assert "nota" in fr._SYSTEM.lower()
    assert "divergência" in fr._SYSTEM.lower()


def test_o_bloco_de_memoria_se_declara_autoridade_no_proprio_rotulo():
    txt = fr._fmt_gather(gather(
        memories=[{"titulo": "memo", "conteudo": "fato", "criado_em": "2026-07-01"}]))
    assert "MAIOR AUTORIDADE" in txt


def test_o_bloco_de_notas_se_declara_perecivel():
    txt = fr._fmt_gather(gather(
        notes=[{"titulo": "nota", "conteudo": "rascunho", "criado_em": "2026-07-28"}]))
    assert "perde pra MEMÓRIA" in txt


def test_memoria_aparece_antes_das_notas_no_prompt():
    """Ordem é saliência. Com a nota primeiro, a fonte frágil ganhava o
    desempate por recência mesmo com a regra escrita."""
    txt = fr._fmt_gather(gather(
        memories=[{"titulo": "memo", "conteudo": "o sócio é o Gustavo",
                   "criado_em": "2026-07-01"}],
        notes=[{"titulo": "nota", "conteudo": "o sócio é o Orestes",
                "criado_em": "2026-07-28"}]))
    assert txt.index("MEMÓRIA / DECISÕES") < txt.index("NOTAS RECENTES")


# ==================== truncagem ====================

def test_memo_inteiro_passa_quando_cabe():
    corpo = "x" * 800
    txt = fr._fmt_gather(gather(
        memories=[{"titulo": "m", "conteudo": corpo, "criado_em": "2026-07-01"}]))
    assert corpo in txt
    assert "[…truncado]" not in txt


def test_memo_grande_entrega_bem_mais_que_os_600_de_antes():
    """O corte antigo entregava ~20% de um memo típico de 2-10k. O fato
    relevante podia estar depois dele e não existir pra camada."""
    corpo = "y" * 5000
    txt = fr._fmt_gather(gather(
        memories=[{"titulo": "m", "conteudo": corpo, "criado_em": "2026-07-01"}]))
    assert txt.count("y") == fr._MEM_CHARS == 2500


def test_conteudo_cortado_avisa_que_foi_cortado():
    """Sem a marca, o modelo lê um texto que termina no meio de uma frase como
    se fosse o documento inteiro."""
    txt = fr._fmt_gather(gather(
        memories=[{"titulo": "m", "conteudo": "z" * 5000, "criado_em": "2026-07-01"}]))
    assert "[…truncado]" in txt


def test_nota_tambem_avisa_o_corte():
    txt = fr._fmt_gather(gather(
        notes=[{"titulo": "n", "conteudo": "w" * 2000, "criado_em": "2026-07-28"}]))
    assert "[…truncado]" in txt
    assert txt.count("w") == fr._NOTE_CHARS == 500


# ==================== documentos de processo ====================

@pytest.mark.parametrize("titulo", [
    "session-locks-quadro-vivo-cos-dev",
    "CoS status board (executivo)",
    "Dev backlog (roadmap CoS v2)",
    "SESSION-LOCKS-quadro-vivo",          # case-insensitive
])
def test_board_e_lock_sao_documento_de_processo(titulo):
    assert fr._is_process_doc(titulo) is True


@pytest.mark.parametrize("titulo", [
    "project_reorg_7_empresas",
    "Vallen Clinic — Equipe e papéis",
    "feedback_verify_before_relay",
    "Business Landscape - Renato",
])
def test_memo_tematico_nao_e_documento_de_processo(titulo):
    """O denylist não pode virar peneira: é exatamente destes memos que a
    camada precisa."""
    assert fr._is_process_doc(titulo) is False


def test_titulo_ausente_nao_quebra():
    assert fr._is_process_doc(None) is False
    assert fr._is_process_doc("") is False


def test_documento_de_processo_nao_ocupa_slot_de_memoria(monkeypatch):
    """O caso real: o `session_locks` casava com qualquer termo (é o log de
    tudo) e queimava 1 dos 5 slots em TODA frente, entregando 0,36% de si
    mesmo. Aqui ele é o 1º hit e mesmo assim não pode entrar."""
    hits = [
        {"id": 674, "titulo": "session-locks-quadro-vivo-cos-dev", "conteudo": "log"},
        {"id": 875, "titulo": "project_reorg_7_empresas", "conteudo": "sócio = Gustavo"},
    ]
    monkeypatch.setattr(fr, "search_memories", lambda *a, **k: hits, raising=False)
    import services.system_memory as sm
    monkeypatch.setattr(sm, "search_memories", lambda *a, **k: hits)

    out = fr._gather_memories("Reorg 7 empresas", None, [])
    titulos = [m["titulo"] for m in out]
    assert "session-locks-quadro-vivo-cos-dev" not in titulos
    assert "project_reorg_7_empresas" in titulos


def test_max_tokens_cabe_o_debriefing_com_a_divergencia():
    """900 cortava o JSON no meio quando o modelo registrava a divergência, e
    a frente inteira saía sem debriefing (erro de parse). Regressão barata de
    trancar: é uma constante no corpo da chamada."""
    import inspect
    src = inspect.getsource(fr.review_frente)
    assert '"max_tokens": 1400' in src
