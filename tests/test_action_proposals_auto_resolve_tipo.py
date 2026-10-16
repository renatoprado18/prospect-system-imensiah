"""O auto-resolve-on-reply matava proposta de AÇÃO antes de executar (16/10/2026).

O caso, achado pela sessão CoS: a proposta **#1235**
(`update_contact_email`, "Email detectado: vanessa.schrot@allergan.com",
contato 26857) nasceu em 14/10 19h16 e foi marcada `resolved` às **19h20 — 4
minutos depois**, com `executed_at` NULO. O Renato só respondeu ao contato no
WhatsApp. O e-mail nunca entrou no cadastro e o contato seguiu "Desconhecido
+55…" por dois dias, corrigido à mão.

A causa era o contraste entre comentário e código. Ficava escrito ali, antes
do SQL: *"Dismissa apenas tipos relacionados a aguardar resposta / Exclui
tipos que requerem acao especifica (reschedule, cancel, etc)"* — e o `UPDATE`
**não tinha filtro de tipo nenhum**, resolvendo toda proposta `pending`
daquele contato. O filtro que o comentário prometia nunca foi escrito, e o
comentário garantiu que ninguém fosse conferir.

Denominador: de **499** `resolved`, **487** com `executed_at` NULO.
`update_contact_email` tinha **30 propostas e ZERO execuções** em toda a
história da tabela.

Estes testes exercitam o SQL de verdade contra o Postgres local, não um mock
de cursor — um mock aqui confirmaria a lista e não veria o `WHERE` (foi mock
de cursor que deixou passar os 4 defeitos da frente RACI em 07/10).

Rodar: .venv/bin/python -m pytest tests/test_action_proposals_auto_resolve_tipo.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from database import get_db  # noqa: E402
from services.action_proposals import (  # noqa: E402
    AUTO_RESOLVE_ON_REPLY,
    ActionProposalsService,
)


@pytest.fixture
def contato():
    """Contato descartável — e a limpeza remove as propostas antes dele."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO contacts (nome) VALUES (%s) RETURNING id",
            ("ZZ Teste auto-resolve",),
        )
        cid = cur.fetchone()["id"]
        conn.commit()
    yield cid
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM action_proposals WHERE contact_id = %s", (cid,))
        cur.execute("DELETE FROM contacts WHERE id = %s", (cid,))
        conn.commit()


def _propoe(contact_id, action_type):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO action_proposals
                (contact_id, action_type, status, title, criado_em)
            VALUES (%s, %s, 'pending', %s, NOW() - interval '10 minutes')
            RETURNING id
            """,
            (contact_id, action_type, f"teste {action_type}"),
        )
        pid = cur.fetchone()["id"]
        conn.commit()
        return pid


def _status(pid):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, executed_at FROM action_proposals WHERE id = %s", (pid,)
        )
        r = cur.fetchone()
        return r["status"], r["executed_at"]


# ------------------------------------------------- o caso real da #1235 --

def test_update_contact_email_SOBREVIVE_a_resposta(contato):
    """O defeito exato: responder ao contato não pode matar a ação.

    Fica VERMELHO sem o filtro de tipo — era assim que a #1235 morria em 4
    minutos sem o e-mail nunca entrar no cadastro.
    """
    pid = _propoe(contato, "update_contact_email")

    ActionProposalsService().dismiss_stale_on_reply(contato)

    status, executed = _status(pid)
    assert status == "pending", (
        "a proposta de atualizar e-mail foi resolvida por uma resposta no "
        "WhatsApp — o cadastro nunca recebe o dado"
    )
    assert executed is None


@pytest.mark.parametrize("tipo", [
    "update_contact_email",
    "update_contact_phone",
    "create_meeting",
    "meeting_request",
    "cancel_event",
    "reschedule_event",
    "camada_cadastro",
    "introduction_request",
])
def test_tipos_de_ACAO_sobrevivem(contato, tipo):
    """Todos os tipos que morreram em produção, um por um.

    Medido em 16/10: 53 `create_meeting`, 24 `meeting_request`, 24
    `update_contact_email`, 10 `introduction_request`, 7 `camada_cadastro`,
    7 `cancel_event`, 5 `reschedule_event` e 4 `update_contact_phone`
    estavam `resolved` com `executed_at` NULO.
    """
    pid = _propoe(contato, tipo)

    ActionProposalsService().dismiss_stale_on_reply(contato)

    assert _status(pid)[0] == "pending", f"{tipo} ainda morre por resposta"


# ----------------------------- e o que DEVE continuar sendo resolvido --
#
# O risco do remédio é o oposto: se nada mais se resolve por resposta, a fila
# de "aguardando resposta" vira depósito e o Renato volta a ver proposta de
# conversa que ele já teve.


@pytest.mark.parametrize("tipo", list(AUTO_RESOLVE_ON_REPLY))
def test_tipos_de_ESPERA_continuam_resolvendo(contato, tipo):
    pid = _propoe(contato, tipo)

    n = ActionProposalsService().dismiss_stale_on_reply(contato)

    assert n >= 1
    assert _status(pid)[0] == "resolved", (
        f"{tipo} significa 'espero que ele responda' — a resposta É o desfecho"
    )


def test_resolve_espera_e_preserva_acao_no_mesmo_contato(contato):
    """O caso misto, que é o real: o contato tem os dois tipos abertos.

    Antes, a resposta levava os dois. Agora tem de levar só a espera.
    """
    espera = _propoe(contato, "pending_response")
    acao = _propoe(contato, "update_contact_email")

    ActionProposalsService().dismiss_stale_on_reply(contato)

    assert _status(espera)[0] == "resolved"
    assert _status(acao)[0] == "pending"


def test_allowlist_nao_contem_tipo_que_executa():
    """Guarda de desenho: nada que tenha execução própria entra na allowlist.

    Se um tipo executa, quem o encerra é a execução — não uma resposta no
    WhatsApp. Esta lista protege contra o próximo tipo ser acrescentado aqui
    por conveniência.
    """
    com_execucao = {
        "update_contact_email", "update_contact_phone", "create_meeting",
        "meeting_request", "cancel_event", "reschedule_event",
        "camada_cadastro", "introduction_request", "weekly_editorial",
    }
    invasores = com_execucao & set(AUTO_RESOLVE_ON_REPLY)
    assert not invasores, f"tipos de ação na allowlist de auto-resolve: {invasores}"


def test_sql_filtra_por_action_type():
    """Guarda do defeito estrutural, por AST — não por substring.

    O comentário antigo descrevia o filtro que o SQL não tinha. Uma guarda por
    substring casaria com a própria prosa que explica isso (aconteceu 3× em
    15/10, ver `feedback_guarda_de_classe_por_ast_nao_substring`), então aqui
    se olha o literal SQL REAL passado ao `execute`.
    """
    import ast

    caminho = os.path.join(_ROOT, "app", "services", "action_proposals.py")
    with open(caminho, encoding="utf-8") as fh:
        arvore = ast.parse(fh.read())

    fn = next(
        n for n in ast.walk(arvore)
        if isinstance(n, ast.FunctionDef) and n.name == "dismiss_stale_on_reply"
    )
    literais = [
        no.value for no in ast.walk(fn)
        if isinstance(no, ast.Constant) and isinstance(no.value, str)
    ]
    sql = next((s for s in literais if "UPDATE action_proposals" in s), None)
    assert sql, "o UPDATE desapareceu"
    assert "action_type" in sql, (
        "o UPDATE voltou a não filtrar tipo — é o defeito da #1235 de volta"
    )
