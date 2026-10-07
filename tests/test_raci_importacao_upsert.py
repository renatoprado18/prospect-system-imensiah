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


@pytest.fixture
def cur():
    """Conexão pelo helper do projeto, NUNCA `psycopg2.connect(DSN)` na mão.

    🔴 07/10: a primeira versão conectava direto em
    `postgresql://localhost:5432/intel`. Passava SOZINHA e era PULADA na suíte
    inteira — o `.env` define `PGUSER=neondb_owner` (Neon), algum teste anterior
    roda `load_dotenv`, e um DSN local sem usuário faz o libpq usar esse PGUSER
    contra o Postgres local: `role "neondb_owner" does not exist`.

    O pior não foi o erro — foi o DIAGNÓSTICO. Meu `except OperationalError`
    chamava isso de "Postgres local fora do ar, suba com ./dev.sh", com o banco
    de pé e aceitando conexões. Skip que mente sobre a causa é skip que ninguém
    investiga: os 5 testes desta frente estavam fora da suíte e eu quase relatei
    "1697 passed" sem notar que não haviam rodado.

    O repo já conhecia a armadilha (`test_merge_fk_lista_completa`,
    `test_merge_preserva_veto`) e já tinha a saída: o helper do projeto.
    """
    from database import get_connection
    conn = get_connection()
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


def test_conclusao_RETROATIVA_mantem_a_data_do_fato(cur):
    """Migration 086. Conclusão quase nunca é registrada no instante em que
    acontece — chega por relato no grupo, por ata lida depois, por sessão que
    varre a semana.

    O trigger datava pela ESCRITA no caminho de UPDATE (respeitava data explícita
    só no INSERT). Em 07/10 isso carimbou 07/10 num item que o Renato concluiu em
    05/10, com registro no grupo do cliente. É a mesma classe do check G da /cos
    (ordenar conversa pela INGESTÃO em vez do ENVIO) — e aqui contamina
    `concluido_no_prazo`: item entregue no prazo e registrado uma semana depois
    aparece como fora do prazo, contra o cliente.
    """
    pid = _projeto_temporario(cur)
    cur.execute("""
        INSERT INTO raci_itens (project_id, acao, status, origem, prazo)
        VALUES (%s, 'entregue no prazo, registrado tarde', 'pendente', 'manual', DATE '2026-09-10')
     RETURNING id
    """, (pid,))
    item = cur.fetchone()["id"]

    cur.execute("""
        UPDATE raci_itens
           SET status='concluido',
               concluido_em = TIMESTAMP '2026-09-05 15:02',
               concluido_em_fonte = 'whatsapp_grupo'
         WHERE id = %s
     RETURNING concluido_em, concluido_em_fonte
    """, (item,))
    r = cur.fetchone()
    assert r["concluido_em"].date().isoformat() == "2026-09-05", (
        "a data informada é a do FATO e tem de sobreviver — quem informa, manda"
    )
    assert r["concluido_em_fonte"] == "whatsapp_grupo"


def test_conclusao_SEM_data_informada_segue_carimbando_agora(cur):
    """A outra metade da 086: o caso comum não mudou. Sem data informada, o
    trigger carimba `now()` e marca 'gatilho' — e é isso que permite distinguir
    depois o que foi carimbado no ato do que foi reconstruído por relato."""
    pid = _projeto_temporario(cur)
    cur.execute("""
        INSERT INTO raci_itens (project_id, acao, status, origem)
        VALUES (%s, 'concluído agora', 'pendente', 'manual') RETURNING id
    """, (pid,))
    item = cur.fetchone()["id"]

    cur.execute(
        "UPDATE raci_itens SET status='concluido' WHERE id=%s "
        "RETURNING concluido_em, concluido_em_fonte", (item,))
    r = cur.fetchone()
    assert r["concluido_em"] is not None
    assert r["concluido_em_fonte"] == "gatilho"


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
