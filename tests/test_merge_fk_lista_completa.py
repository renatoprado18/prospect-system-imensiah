"""A lista de FK do merge tem que acompanhar o banco — vigiada por teste, não por lembrança.

POR QUE ESTE ARQUIVO EXISTE. `_FK_TABLES_TO_CONTACTS` é escrita à mão e diz ao
merge quais tabelas repontar antes de apagar a ficha secundária. Ela foi
auditada em 25/07/26 e **envelheceu em três semanas**: em 23/08 havia 4 tabelas
fora dela, duas em `ON DELETE CASCADE`. Não era volume — eram
`google_nao_fundir` e `contato_empresa_mantida`, o registro das decisões do
Renato ("não funda esta ficha com o Google"), gravado pelas migrations 077/078
justamente pra parar de ser reperguntado. O merge as apagaria e a pergunta
voltaria, sem erro nenhum: o merge termina 200.

O DEFEITO DE FUNDO não era a lista desatualizada — era ela depender de alguém
lembrar de reauditar. `scripts/gate_mutirao_merge.py` já sabia detectar isso,
mas só roda quando alguém o chama, e ninguém chama um gate que não falha
sozinho ([[feedback_consumidor_morto_wiring]]). Estes testes rodam na suíte:
tabela nova com `contact_id` derruba o build no dia em que nasce, não três
semanas depois na véspera de um mutirão de 1.885 fichas.

⚠️ A suíte encosta no Postgres LOCAL (ver `conftest._travar_no_banco_local`).
Se o local estiver defasado do Neon, este teste passa e o prod segue exposto —
por isso o gate continua existindo, e continua sendo o que se roda antes de
soltar merge em massa contra produção.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from services.contact_dedup import (  # noqa: E402
    _COMPOSITE_UNIQUE_PARTNERS,
    _FK_TABLES_TO_CONTACTS,
)

# `projects` aponta por `owner_contact_id` e é tratada à parte, fora do laço.
_FORA_DO_PADRAO = {"projects"}


@pytest.fixture(scope="module")
def cur():
    """Conexão pelo helper do projeto, NUNCA por `psycopg2.connect(DATABASE_URL)`.

    A primeira versão conectava na mão e passava sozinha, mas quebrava na suíte
    inteira com `role "neondb_owner" does not exist`: o `.env` define `PGUSER` e
    `PGPASSWORD` do Neon, algum teste anterior roda `load_dotenv`, e o DSN local
    `postgresql://localhost:5432/intel` não traz usuário — o libpq então usa o
    `PGUSER` do ambiente contra o Postgres local. Verde isolado e vermelho junto
    é a assinatura de teste que depende de ordem de execução.
    """
    from database import get_connection
    conn = get_connection()
    c = conn.cursor()
    yield c
    c.close()
    conn.close()


def _tabelas_com_coluna_de_contato(cur):
    """Toda TABELA (não view — view herda a coluna e não se reponta) com uma
    coluna de contato no schema public."""
    cur.execute("""
        SELECT c.table_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         WHERE c.table_schema = 'public'
           AND c.column_name IN ('contact_id', 'owner_contact_id')
           AND t.table_type = 'BASE TABLE'
           AND c.table_name <> 'contacts'
    """)
    return {r["table_name"] for r in cur.fetchall()}


def test_nenhuma_tabela_com_contact_id_fica_fora_da_lista(cur):
    """O teste que teria pego as 4 de 23/08 no dia em que a migration rodou."""
    faltando = _tabelas_com_coluna_de_contato(cur) - set(_FK_TABLES_TO_CONTACTS) - _FORA_DO_PADRAO
    assert not faltando, (
        f"tabela(s) com coluna de contato fora de _FK_TABLES_TO_CONTACTS: {sorted(faltando)}. "
        "O merge não as reponta: em CASCADE ele APAGA as linhas, em SET NULL perde o vínculo, "
        "sem FK deixa órfã apontando pra id inexistente — e termina 200 nos três casos. "
        "Acrescente à lista (e veja se a tabela tem unique com contact_id, que exige "
        "_COMPOSITE_UNIQUE_PARTNERS também)."
    )


def test_lista_nao_tem_tabela_que_nao_existe_mais(cur):
    """O simétrico: nome morto na lista faz o merge rodar `UPDATE` numa tabela
    inexistente e explodir no meio, com parte das FKs já repontadas."""
    cur.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    existem = {r["table_name"] for r in cur.fetchall()}
    fantasmas = set(_FK_TABLES_TO_CONTACTS) - existem
    assert not fantasmas, (
        f"_FK_TABLES_TO_CONTACTS cita tabela(s) que não existem mais: {sorted(fantasmas)}"
    )


def test_toda_unique_com_contact_id_esta_declarada(cur):
    """A armadilha que trocaria um defeito por outro.

    Acrescentar uma tabela à lista sem declarar sua unique composta faz o UPDATE
    violar a constraint no MEIO do merge — pior que o defeito original, porque
    interrompe com metade das FKs já movidas. `google_nao_fundir` (contact_id,
    google_rid) e `contato_empresa_mantida` (contact_id, empresa_ignorada) são
    exatamente esse caso.
    """
    cur.execute("""
        SELECT tc.table_name,
               string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) AS cols
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_schema = kcu.table_schema
         WHERE tc.table_schema = 'public'
           AND tc.constraint_type IN ('UNIQUE', 'PRIMARY KEY')
         GROUP BY tc.table_name, tc.constraint_name
    """)
    faltando = {}
    for r in cur.fetchall():
        tabela, colunas = r["table_name"], r["cols"].split(",")
        if "contact_id" not in colunas or tabela not in _FK_TABLES_TO_CONTACTS:
            continue
        parceiras = [c for c in colunas if c != "contact_id"]
        if not parceiras:
            continue  # unique só em contact_id: o merge não tem o que desempatar
        declaradas = _COMPOSITE_UNIQUE_PARTNERS.get(tabela)
        if declaradas is None or set(declaradas) != set(parceiras):
            faltando[tabela] = {"no banco": parceiras, "declarado": declaradas}
    assert not faltando, (
        f"unique composta com contact_id não declarada em _COMPOSITE_UNIQUE_PARTNERS: {faltando}. "
        "Sem isso o UPDATE do merge estoura no meio, com parte das FKs já repontadas."
    )


def test_o_veto_do_renato_esta_protegido(cur):
    """Controle positivo, nomeado.

    Os testes acima são genéricos e continuariam verdes se alguém removesse
    justamente estas duas linhas — elas são o caso que motivou o arquivo, e o
    único cujo dano é apagar uma decisão que o Renato já tomou. Uma guarda que
    não nomeia o caso que a originou certifica a categoria e perde o exemplar
    ([[feedback_controle_positivo_pega_o_furo_real]]).
    """
    for tabela, parceira in (("google_nao_fundir", "google_rid"),
                             ("contato_empresa_mantida", "empresa_ignorada")):
        assert tabela in _FK_TABLES_TO_CONTACTS, (
            f"{tabela} saiu da lista — está em ON DELETE CASCADE, então o merge volta a "
            "APAGAR o registro do veto do Renato e a decisão volta a ser reperguntada."
        )
        assert _COMPOSITE_UNIQUE_PARTNERS.get(tabela) == [parceira], (
            f"{tabela} precisa de _COMPOSITE_UNIQUE_PARTNERS['{tabela}'] == ['{parceira}']"
        )
