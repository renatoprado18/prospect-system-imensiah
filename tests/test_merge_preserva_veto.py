"""O veto do Renato sobrevive ao merge — provado no banco, não na lista.

`test_merge_fk_lista_completa.py` prova que a lista está CONSISTENTE com o
schema. Isto aqui prova o COMPORTAMENTO: cria duas fichas de verdade no Postgres
local, dá um veto à secundária, roda a migração de referências do merge e
confere que o veto continua lá, apontando pra primária.

A distinção não é acadêmica. A lista podia estar completa e o merge continuar
errando — foi assim que o incidente de 16/06/26 (Eduardo Marson, ~17 mensagens e
anexos perdidos) passou: o código "conhecia" as tabelas e a ordem das operações
é que apagava. Guarda que confere a declaração e nunca executa o caminho
certifica a intenção, não o efeito.

Tudo roda dentro de uma transação com ROLLBACK no fim: nenhuma linha sobrevive
ao teste, nem quando ele falha no meio.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from services.contact_dedup import _migrate_contact_references  # noqa: E402


@pytest.fixture
def cur():
    """Cursor em transação, sempre desfeita. Nunca faz commit, de propósito.

    Conecta pelo helper do projeto, não por `psycopg2.connect(DATABASE_URL)`: o
    `.env` exporta `PGUSER=neondb_owner`, e um DSN local sem usuário faz o libpq
    herdá-lo do ambiente assim que qualquer teste anterior rode `load_dotenv`.
    """
    from database import get_connection
    conn = get_connection()
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        c.close()
        conn.close()


def _ficha(cur, nome):
    cur.execute("INSERT INTO contacts (nome) VALUES (%s) RETURNING id", (nome,))
    return cur.fetchone()["id"]


def test_veto_do_google_migra_para_a_ficha_primaria(cur):
    """O caso grave: `google_nao_fundir` está em ON DELETE CASCADE.

    Sem a tabela na lista, o DELETE da ficha secundária levaria o veto junto e o
    INTEL voltaria a propor a fusão que o Renato já recusou — sem erro nenhum.
    """
    primaria, secundaria = _ficha(cur, "Fulano [teste]"), _ficha(cur, "Fulano [teste dup]")
    cur.execute(
        "INSERT INTO google_nao_fundir (contact_id, google_rid, motivo) VALUES (%s, %s, %s)",
        (secundaria, "people/TESTE_MERGE", "não é a mesma pessoa"),
    )

    _migrate_contact_references(cur, primaria, [secundaria])
    cur.execute("DELETE FROM contacts WHERE id = %s", (secundaria,))

    cur.execute("SELECT contact_id, motivo FROM google_nao_fundir WHERE google_rid = %s",
                ("people/TESTE_MERGE",))
    sobreviventes = [(r["contact_id"], r["motivo"]) for r in cur.fetchall()]
    assert sobreviventes == [(primaria, "não é a mesma pessoa")], (
        "o veto do Renato não sobreviveu ao merge — era exatamente o que a "
        "migration 077 existe pra impedir"
    )


def test_veto_de_empresa_migra_para_a_ficha_primaria(cur):
    """Mesmo caso, `contato_empresa_mantida` (078) — também CASCADE."""
    primaria, secundaria = _ficha(cur, "Beltrano [teste]"), _ficha(cur, "Beltrano [teste dup]")
    cur.execute(
        """INSERT INTO contato_empresa_mantida
           (contact_id, empresa_intel, empresa_ignorada, motivo)
           VALUES (%s, %s, %s, %s)""",
        (secundaria, "Alba", "ACME [teste]", "saiu da ACME em 2024"),
    )

    _migrate_contact_references(cur, primaria, [secundaria])
    cur.execute("DELETE FROM contacts WHERE id = %s", (secundaria,))

    cur.execute("SELECT contact_id FROM contato_empresa_mantida WHERE empresa_ignorada = %s",
                ("ACME [teste]",))
    assert [r["contact_id"] for r in cur.fetchall()] == [primaria]


def test_veto_repetido_nas_duas_fichas_nao_estoura_o_merge(cur):
    """A armadilha da unique composta, no caminho real.

    Se as DUAS fichas vetam o mesmo `google_rid`, o UPDATE viola
    `google_nao_fundir_par` — e estouraria no MEIO da migração, com parte das
    FKs já repontadas. A política "primário vence" resolve: a linha redundante
    da secundária é apagada antes, e o veto continua existindo pela primária.
    """
    primaria, secundaria = _ficha(cur, "Cicrano [teste]"), _ficha(cur, "Cicrano [teste dup]")
    for cid in (primaria, secundaria):
        cur.execute(
            "INSERT INTO google_nao_fundir (contact_id, google_rid, motivo) VALUES (%s, %s, %s)",
            (cid, "people/TESTE_COLISAO", f"veto da ficha {cid}"),
        )

    _migrate_contact_references(cur, primaria, [secundaria])   # não pode estourar
    cur.execute("DELETE FROM contacts WHERE id = %s", (secundaria,))

    cur.execute("SELECT contact_id FROM google_nao_fundir WHERE google_rid = %s",
                ("people/TESTE_COLISAO",))
    assert [r["contact_id"] for r in cur.fetchall()] == [primaria], \
        "o veto tem que restar exatamente uma vez, na primária"


def test_ledger_do_check_g_nao_vira_orfao(cur):
    """`check_g_ledger` não tem FK declarada — o DELETE não falha e ninguém vê.

    São 1.393 linhas em prod. Órfã é pior que erro: o placar do check-G continua
    somando sobre linhas que apontam pra uma ficha que não existe mais.
    """
    primaria, secundaria = _ficha(cur, "Sicrano [teste]"), _ficha(cur, "Sicrano [teste dup]")
    # `message_id`, `msg_em` e `decisao` são NOT NULL sem default — a primeira
    # versão deste teste as ignorava e caía num `pytest.skip`, que passa verde e
    # não protege nada. Preenchidas com valores de teste; a transação é desfeita.
    cur.execute("""INSERT INTO check_g_ledger (contact_id, message_id, msg_em, decisao)
                   VALUES (%s, %s, now(), %s) RETURNING id""",
                (secundaria, -1, "suprimido"))   # `decisao` tem CHECK: mostrado|suprimido
    linha = cur.fetchone()["id"]

    _migrate_contact_references(cur, primaria, [secundaria])
    cur.execute("DELETE FROM contacts WHERE id = %s", (secundaria,))

    cur.execute("SELECT contact_id FROM check_g_ledger WHERE id = %s", (linha,))
    assert cur.fetchone()["contact_id"] == primaria, "a linha do ledger ficou apontando pra ficha apagada"
