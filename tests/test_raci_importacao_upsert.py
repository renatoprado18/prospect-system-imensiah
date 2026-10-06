"""O UPSERT da importação contra o índice PARCIAL — exercitando o INSERT de verdade.

POR QUE ESTE ARQUIVO EXISTE. Em 06/10/2026 a importação do passo 2 estourou na
PRIMEIRA linha, em produção:

    psycopg2.errors.InvalidColumnReference: there is no unique or exclusion
    constraint matching the ON CONFLICT specification

O índice da migration 084 é PARCIAL — `UNIQUE (conselhoos_raci_id) WHERE
conselhoos_raci_id IS NOT NULL`, parcial porque execução que nunca passou por
conselho precisa poder ficar NULL. E o Postgres só casa um índice parcial com um
`ON CONFLICT` que REPITA o mesmo predicado.

O que deixou isso passar é o ponto, e vale mais que o conserto: **o dry-run não
executa o INSERT**. Ele contava o que faria e devolvia um relatório perfeito —
122 lidos, 76 criados, 31 atualizados — sobre um caminho que não funcionava. Todo
o resto da frente estava coberto por teste com mock de cursor, e mock nenhum
reproduz a negociação entre `ON CONFLICT` e índice parcial: isso é o banco
decidindo, não o Python.

Por isso aqui se fala com o Postgres de verdade, e tudo roda dentro de uma
transação com ROLLBACK ao final — nada sobrevive ao teste.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402


def _conn():
    """Conexão com o banco LOCAL de desenvolvimento.

    Nunca prod: este teste ESCREVE (e desfaz). Apontar para o Neon sujaria a
    matriz de um cliente mesmo com rollback no caminho feliz.
    """
    url = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost:5432/intel")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


@pytest.fixture
def cur():
    try:
        conn = _conn()
    except psycopg2.OperationalError as e:
        pytest.skip(f"Postgres local fora do ar ({e.__class__.__name__}) — "
                    "suba com ./dev.sh; este teste precisa de banco real")
    try:
        c = conn.cursor()
        yield c
    finally:
        conn.rollback()   # nada do que este teste escreveu sobrevive
        conn.close()


def _projeto_temporario(cur):
    cur.execute("""
        INSERT INTO projects (nome, tipo, status)
        VALUES ('TESTE upsert raci', 'consultoria', 'ativo')
     RETURNING id
    """)
    return cur.fetchone()["id"]


UPSERT = """
    INSERT INTO raci_itens (project_id, acao, status, origem, conselhoos_raci_id)
    VALUES (%s, %s, 'pendente', 'conselhoos', %s)
    ON CONFLICT (conselhoos_raci_id)
        WHERE conselhoos_raci_id IS NOT NULL
    DO UPDATE SET acao = EXCLUDED.acao, atualizado_em = NOW()
    RETURNING id, (xmax = 0) AS inseriu
"""


def test_upsert_casa_com_o_indice_PARCIAL_da_084(cur):
    """O defeito exato de 06/10: sem repetir o predicado, o Postgres recusa.

    Este teste falha com `InvalidColumnReference` se alguém tirar o
    `WHERE conselhoos_raci_id IS NOT NULL` do ON CONFLICT — que é precisamente
    como a importação foi para produção e estourou na primeira linha.
    """
    pid = _projeto_temporario(cur)
    uid = "11111111-1111-1111-1111-111111111111"

    cur.execute(UPSERT, (pid, "primeira versão", uid))
    primeira = cur.fetchone()
    assert primeira["inseriu"] is True, "a primeira passada cria"

    cur.execute(UPSERT, (pid, "ata corrigida", uid))
    segunda = cur.fetchone()
    assert segunda["inseriu"] is False, "a segunda ATUALIZA — jamais duplica"
    assert segunda["id"] == primeira["id"], "é a mesma linha, não uma cópia"

    cur.execute("SELECT acao FROM raci_itens WHERE id = %s", (primeira["id"],))
    assert cur.fetchone()["acao"] == "ata corrigida"

    cur.execute(
        "SELECT COUNT(*) AS n FROM raci_itens WHERE conselhoos_raci_id = %s", (uid,))
    assert cur.fetchone()["n"] == 1, (
        "idempotência é a razão de ser do índice: rodar a importação duas vezes "
        "não pode criar a segunda cópia de cada item"
    )


def test_o_indice_e_parcial_e_por_isso_varios_NULL_convivem(cur):
    """A outra metade, e o motivo de o índice não poder ser total: os 12 itens
    que o Renato declarou "sem par" em 06/10 têm `conselhoos_raci_id` NULL. Um
    índice único não-parcial permitiria UM só deles."""
    pid = _projeto_temporario(cur)
    for i in range(3):
        cur.execute("""
            INSERT INTO raci_itens (project_id, acao, status, origem)
            VALUES (%s, %s, 'pendente', 'manual')
        """, (pid, f"execução só-INTEL {i}"))

    cur.execute("""
        SELECT COUNT(*) AS n FROM raci_itens
         WHERE project_id = %s AND conselhoos_raci_id IS NULL
    """, (pid,))
    assert cur.fetchone()["n"] == 3


def test_nao_se_pode_apontar_para_a_ata_E_declarar_que_nunca_passou(cur):
    """O CHECK da migration 085. Os dois estados juntos são contraditórios, e
    quem recusa é o banco — não a boa intenção de quem chama."""
    pid = _projeto_temporario(cur)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""
            INSERT INTO raci_itens
                (project_id, acao, status, origem, conselhoos_raci_id, sem_par_declarado_em)
            VALUES (%s, 'contraditório', 'pendente', 'manual',
                    '22222222-2222-2222-2222-222222222222', NOW())
        """, (pid,))
