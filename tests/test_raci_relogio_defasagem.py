"""O RELÓGIO DA MATRIZ RACI — 09/11/2026.

A pergunta que abriu isto é literal, do Renato, antes de disparar o RACI de
segunda: **"como saber a última atualização? Corremos o risco de mandar
desatualizado?"** — e a resposta era que não havia como saber. Dois defeitos
somados:

1. **A régua media a COLUNA ERRADA.** `max(atualizado_em)` não é o relógio da
   matriz, porque CONCLUIR um item carimba `concluido_em` e não toca
   `atualizado_em`. Medido em prod em 09/11, projeto 24 (Vallen):
   `max(atualizado_em)` = **27/10** e `greatest(...)` = **29/10** — a data da
   própria mesa, onde 6 itens foram fechados. A régua antiga diria "parado desde
   27/10" com a matriz mexida na véspera.
2. **Nada travava o envio.** O Alba tinha relógio de 06/10 contra uma mesa de
   26/10, e o primeiro item que o conselho leria era "Remarcar a Reunião de
   Conselho — cancelar 13/10 e confirmar 27/10", vencido, de uma reunião que já
   tinha acontecido.

E o limite MEDIDO que decide a forma da trava: **a data real da mesa não tem casa
no INTEL.** Conferido em 09/11 — `conselhoos_meetings` vazia (0 linhas),
`project_events` vazia (0 linhas), `calendar_events` sem vínculo com projeto.
Sobra `project_notes` tipo='reuniao', cujo `criado_em` é quando a ATA FOI
DIGITADA: a ata da mesa Vallen de 29/10 entrou em 31/10. Por isso a trava tem
dois níveis, e o teste que mais importa aqui é o da **contraprova**: ela não pode
barrar o Vallen por 2 dias de atraso de digitação, nem barrar para sempre os
projetos que não têm nota de reunião nenhuma — guarda que barra o caso são acaba
desligada junto com a que importa.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_raci_relogio_defasagem.py -q
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from database import get_db  # noqa: E402
from services import raci_matrix as rm  # noqa: E402


# ------------------------------------------------- o relógio, contra o Postgres

@pytest.fixture
def projeto():
    """Projeto de teste com rollback — nada fica no banco.

    SQL de verdade de propósito: `GREATEST(MAX(a), MAX(b), MAX(c))` com NULLs é
    exatamente o tipo de coisa que um dublê de cursor responderia do jeito que o
    teste espera, e o Postgres do jeito que ele é.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO projects (nome, tipo, status)
               VALUES ('[TESTE] relógio RACI', 'conselho', 'ativo') RETURNING id""")
        pid = cur.fetchone()["id"]
        yield pid, cur
        conn.rollback()


def _item(cur, pid, *, criado, atualizado, concluido=None, status="pendente"):
    cur.execute(
        """INSERT INTO raci_itens (project_id, acao, status, criado_em,
                                   atualizado_em, concluido_em)
           VALUES (%s, 'acao de teste', %s, %s, %s, %s)""",
        (pid, status, criado, atualizado, concluido))


def _nota_reuniao(cur, pid, criado, titulo="Reunião de Conselho (teste)"):
    cur.execute(
        """INSERT INTO project_notes (project_id, tipo, titulo, conteudo, criado_em)
           VALUES (%s, 'reuniao', %s, 'ata de teste', %s)""",
        (pid, titulo, criado))


def test_relogio_pega_concluido_em_quando_atualizado_em_esta_atras(projeto):
    """O DEFEITO, reproduzido: fechar item carimba `concluido_em` e não toca
    `atualizado_em`. É o caso Vallen — régua antiga 27/10, relógio real 29/10."""
    pid, cur = projeto
    _item(cur, pid, criado=datetime(2026, 10, 1, 9, 0),
          atualizado=datetime(2026, 10, 27, 10, 0))
    _item(cur, pid, criado=datetime(2026, 10, 1, 9, 0),
          atualizado=datetime(2026, 10, 26, 8, 0),
          concluido=datetime(2026, 10, 29, 14, 52), status="concluido")

    rel = rm._relogio(cur, pid)

    assert rel["relogio"].startswith("2026-10-29"), "o relógio é o GREATEST dos três"
    assert rel["so_atualizado_em"].startswith("2026-10-27"), (
        "a régua antiga fica exposta ao lado — é a prova de que media errado")
    assert rel["itens"] == 2


def test_relogio_pega_criado_em_de_item_novo_sem_update(projeto):
    """Item inserido e nunca editado: `criado_em` é o movimento mais novo."""
    pid, cur = projeto
    _item(cur, pid, criado=datetime(2026, 10, 1, 9, 0),
          atualizado=datetime(2026, 10, 1, 9, 0))
    cur.execute(
        """INSERT INTO raci_itens (project_id, acao, status, criado_em, atualizado_em)
           VALUES (%s, 'item novo da mesa', 'pendente', %s, %s)""",
        (pid, datetime(2026, 11, 5, 16, 0), datetime(2026, 11, 5, 16, 0)))

    rel = rm._relogio(cur, pid)
    assert rel["relogio"].startswith("2026-11-05")


def test_matriz_vazia_nao_mente_uma_data(projeto):
    pid, cur = projeto
    rel = rm._relogio(cur, pid)
    assert rel["relogio"] is None
    assert rel["itens"] == 0
    assert rel["nivel"] == "sem_comparacao"


# ------------------------------------------------------------- os três níveis

def test_defasado_quando_a_mesa_e_bem_depois_do_relogio(projeto):
    """O caso Alba: relógio de 06/10 contra mesa de 26/10."""
    pid, cur = projeto
    _item(cur, pid, criado=datetime(2026, 10, 1), atualizado=datetime(2026, 10, 6))
    _nota_reuniao(cur, pid, datetime(2026, 10, 26))

    rel = rm._relogio(cur, pid)
    assert rel["gap_dias"] == 20
    assert rel["nivel"] == "defasado"
    assert rel["defasado"] is True


def test_atencao_quando_o_gap_cabe_no_atraso_de_digitar_a_ata(projeto):
    """O caso Vallen: mesa em 29/10, matriz mexida em 29/10, ata digitada em 31/10.

    Barrar isto seria barrar o caso são — a matriz REFLETE a mesa; o que chegou
    depois foi o texto da ata."""
    pid, cur = projeto
    _item(cur, pid, criado=datetime(2026, 10, 1),
          atualizado=datetime(2026, 10, 26),
          concluido=datetime(2026, 10, 29, 14, 52), status="concluido")
    _nota_reuniao(cur, pid, datetime(2026, 10, 31, 11, 0))

    rel = rm._relogio(cur, pid)
    assert rel["gap_dias"] == 2
    assert rel["nivel"] == "atencao"
    assert rel["defasado"] is False, "ambíguo não é defasado"


def test_ok_quando_o_relogio_e_mais_novo_que_a_mesa(projeto):
    pid, cur = projeto
    _item(cur, pid, criado=datetime(2026, 11, 1), atualizado=datetime(2026, 11, 9))
    _nota_reuniao(cur, pid, datetime(2026, 10, 26))

    rel = rm._relogio(cur, pid)
    assert rel["gap_dias"] == -14
    assert rel["nivel"] == "ok"


def test_sem_nota_de_reuniao_o_veredito_e_a_TERCEIRA_resposta(projeto):
    """`defasado=None`, não `False`. Dizer False certificaria frescor que ninguém
    conferiu — o mesmo erro de `duplicatas_checadas`, pelo lado oposto."""
    pid, cur = projeto
    _item(cur, pid, criado=datetime(2026, 11, 1), atualizado=datetime(2026, 11, 9))

    rel = rm._relogio(cur, pid)
    assert rel["gap_dias"] is None
    assert rel["defasado"] is None
    assert rel["nivel"] == "sem_comparacao"


def test_tolerancia_e_ajustavel_por_env(projeto, monkeypatch):
    """Apertar a régua não pode exigir deploy."""
    pid, cur = projeto
    _item(cur, pid, criado=datetime(2026, 10, 1), atualizado=datetime(2026, 10, 25))
    _nota_reuniao(cur, pid, datetime(2026, 10, 28))

    assert rm._relogio(cur, pid)["nivel"] == "atencao"   # gap 3, tolerância 7
    monkeypatch.setenv("RACI_ATA_LAG_DIAS", "1")
    assert rm._relogio(cur, pid)["nivel"] == "defasado"  # gap 3, tolerância 1


# --------------------------------------------- a trava do envio ao grupo

def _rel(nivel, **kw):
    base = {"nivel": nivel, "relogio_label": "06/10/2026 09:00", "gap_dias": 20,
            "reuniao_titulo": "Reunião de Conselho (Alba)"}
    base.update(kw)
    return base


def test_trava_barra_o_defasado_e_diz_os_numeros():
    motivo = rm.bloqueio_por_defasagem(_rel("defasado"))
    assert motivo and motivo.startswith("BLOQUEADO")
    # O motivo tem de carregar a prova: veredito sem número vira superstição.
    assert "06/10/2026 09:00" in motivo and "20 dias" in motivo
    assert "confirmar_defasagem=true" in motivo


def test_trava_NAO_barra_atencao_nem_ok_nem_sem_comparacao():
    """A contraprova que impede a guarda de ser desligada."""
    for nivel in ("atencao", "ok", "sem_comparacao"):
        assert rm.bloqueio_por_defasagem(_rel(nivel)) is None, nivel


def test_trava_NAO_barra_matriz_sem_relogio():
    """`get_matrix` indisponível (ex.: override de duplicata com detecção falha)
    não pode virar bloqueio por um nome sem ligação."""
    assert rm.bloqueio_por_defasagem(None) is None
    assert rm.bloqueio_por_defasagem({}) is None


def test_override_declarado_libera():
    assert rm.bloqueio_por_defasagem(_rel("defasado"), confirmado=True) is None
