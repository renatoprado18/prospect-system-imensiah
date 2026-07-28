"""
Testes da NORMALIZACAO DE TELEFONE no match WhatsApp -> ficha (28/07).

O DEFEITO QUE ISTO TRANCA
-------------------------
O WhatsApp entrega o numero CRU (`5511992526344`); o Google entrega FORMATADO
(`+55 (11) 99252-6344`). Oito call-sites comparavam os dois com

    WHERE telefones::text LIKE '%<ultimos 8 digitos crus>%'

e o hifen do formato do Google cai EXATAMENTE no meio desses 8 digitos
(celular BR e `9XXXX-XXXX`). Resultado medido em prod: 3.659 dos 9.149 numeros
da base (40%) eram invisiveis ao match, e o webhook criava ficha fantasma
"Desconhecido +55..." pra quem ja tinha ficha (18 das 44 fantasmas da base).

Os testes de SQL rodam contra o Postgres LOCAL numa tabela temporaria propria
— nao dependem do conteudo da base, e sao pulados se o Postgres nao estiver
no ar. Sem eles a expressao `regexp_replace(...)` ficaria so na intencao: um
fake cursor nao prova semantica de SQL.

Rodar: .venv/bin/python -m pytest tests/test_phone_match_normalization.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.contact_identity import (  # noqa: E402
    PHONE_MATCH_DIGITS,
    canonical_br_phone,
    find_contact_by_phone,
    phone_kind,
    phone_lookup_key,
    phone_match_order_sql,
    phone_match_sql,
)


# O par exato que a CoS reproduziu em 28/07: mesmo numero, duas grafias.
WA_CRU = "5511992526344"
GOOGLE_FORMATADO = "+55 (11) 99252-6344"


# ==================== phone_lookup_key (puro) ====================

def test_formatos_diferentes_do_mesmo_numero_dao_a_mesma_chave():
    """O ponto da frente inteira: cru e formatado tem que colidir."""
    assert phone_lookup_key(WA_CRU) == phone_lookup_key(GOOGLE_FORMATADO)


@pytest.mark.parametrize("grafia", [
    "+55 (11) 99252-6344",   # Google, celular SP
    "+5511992526344",        # cru com DDI
    "5511992526344",         # cru sem +
    "55 11 99252 6344",      # espacos
    "(11) 99252-6344",       # sem DDI
    "11992526344",           # so DDD + numero
    "99252-6344",            # so o assinante
])
def test_todas_as_grafias_de_um_mesmo_numero_convergem(grafia):
    assert phone_lookup_key(grafia) == "92526344"


def test_numero_curto_demais_nao_vira_chave():
    """Menos que PHONE_MATCH_DIGITS nao identifica ninguem: o chamador
    precisa saber disso pra pular a busca em vez de casar com qualquer um."""
    assert phone_lookup_key("12345") is None
    assert phone_lookup_key("") is None
    assert phone_lookup_key(None) is None


def test_chave_tem_o_tamanho_declarado():
    key = phone_lookup_key(WA_CRU)
    assert len(key) == PHONE_MATCH_DIGITS
    assert key.isdigit()


def test_o_9o_digito_brasileiro_nao_separa_a_mesma_pessoa():
    """Numero antigo (8 digitos de assinante) e novo (9) do mesmo par
    DDD+linha convergem — e a razao de o corte ser 8 e nao 9."""
    assert phone_lookup_key("551192526344") == phone_lookup_key("5511992526344")


# ==================== o 9o digito (migracao Anatel) ====================
#
# Ate 2013-2016 o celular BR tinha 8 digitos de assinante; a Anatel prefixou um
# 9. `35 9985-1122` e `35 99985-1122` sao A MESMA LINHA. A base tem 226 numeros
# no formato antigo e 13 pessoas com ficha duplicada só por causa disto.

def test_celular_no_formato_antigo_e_no_novo_tem_a_mesma_forma_canonica():
    assert canonical_br_phone("553599851122") == canonical_br_phone("5535999851122")
    assert canonical_br_phone("553599851122") == "5535999851122"


def test_canonizacao_e_idempotente():
    uma = canonical_br_phone("553599851122")
    assert canonical_br_phone(uma) == uma


def test_fixo_nao_recebe_o_nono_digito():
    """O que separa fixo de celular antigo é o primeiro dígito do assinante:
    a Anatel reserva 2-5 pra fixo e 6-9 pra móvel. Inserir o 9 num fixo
    inventaria um número que não existe."""
    assert canonical_br_phone("551130624437") == "551130624437"   # 3 = fixo
    assert canonical_br_phone("551145678901") == "551145678901"   # 4 = fixo


def test_canoniza_tambem_sem_ddi():
    assert canonical_br_phone("3599851122") == "35999851122"


def test_numero_internacional_passa_intacto():
    """Um US de 11 dígitos não pode ganhar um 9 brasileiro no meio."""
    assert canonical_br_phone("19734744164") == "19734744164"


def test_canonizacao_nao_reescreve_formatacao():
    """Entra e sai só-dígitos — a função é pra COMPARAR, não pra gravar."""
    assert canonical_br_phone("+55 (35) 9985-1122") == "5535999851122"


def test_celular_antigo_e_classificado_como_celular_nao_como_fixo():
    """`names_match` e a guarda de linha compartilhada são mais rigorosos pra
    'landline' (por causa do fixo que Douglas e Orestes dividem). Chamar 226
    celulares de fixo aplicava a eles o critério de uma linha que várias
    pessoas usam — celular é pessoal."""
    assert phone_kind("553599851122") == "mobile"
    assert phone_kind("5535999851122") == "mobile"
    assert phone_kind("551130624437") == "landline"


def test_a_chave_de_busca_ja_absorvia_o_9_e_continua_absorvendo():
    """O corte de 8 dígitos sempre atravessou a migração — é por isso que ele
    é 8 e não 9. A canonização não muda a busca, muda o DESEMPATE."""
    assert phone_lookup_key("553599851122") == phone_lookup_key("5535999851122")


# ==================== fragmentos SQL ====================

def test_fragmentos_respeitam_o_alias_pedido():
    cond = phone_match_sql("self_c")
    assert "self_c.telefones" in cond
    assert "c.telefones" not in cond.replace("self_c.telefones", "")


def test_cada_fragmento_consome_um_placeholder():
    """Contrato com quem monta a propria query: condicao = 1 param (a chave),
    ORDER BY = 1 param (os digitos inteiros). Errar isso desalinha a tupla."""
    assert phone_match_sql("c").count("%s") == 1
    assert phone_match_order_sql("c").count("%s") == 1


def test_condicao_le_o_campo_number_e_nao_o_json_inteiro():
    """Comparar `telefones::text` deixaria `type`/`whatsapp` no caldo e
    poderia casar por acidente."""
    cond = phone_match_sql("c")
    assert "->>'number'" in cond
    assert "telefones::text" not in cond


# ==================== SQL de verdade (Postgres local) ====================

def _pg():
    """Conexao com o Postgres local, ou None (testes de SQL sao pulados)."""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        return psycopg2.connect(
            "postgresql://rap@localhost:5432/intel",
            cursor_factory=RealDictCursor,
            connect_timeout=2,
        )
    except Exception:
        return None


@pytest.fixture
def cursor_com_contatos():
    """
    Cursor sobre uma tabela `contacts` TEMPORARIA (sombreia a real dentro da
    transacao). Tudo rola em transacao com rollback: nao escreve na base.
    """
    conn = _pg()
    if conn is None:
        pytest.skip("Postgres local indisponivel")
    cur = conn.cursor()
    cur.execute("""
        CREATE TEMPORARY TABLE contacts (
            id SERIAL PRIMARY KEY, nome TEXT, telefones JSONB
        ) ON COMMIT DROP
    """)
    cur.execute("""
        INSERT INTO contacts (id, nome, telefones) VALUES
          (1, 'Formatado Google',
              '[{"type":"mobile","number":"+55 (11) 99252-6344","whatsapp":true}]'),
          (2, 'Cru',
              '[{"type":"mobile","number":"+5511975598043","whatsapp":true}]'),
          (3, 'Sem telefone', NULL),
          (4, 'Telefones nao-array', '{"number":"+5511911111111"}'),
          (5, 'Dois numeros',
              '[{"type":"home","number":"+55 11 3062-4437"},
                {"type":"mobile","number":"+55 (11) 98888-7777"}]')
    """)
    try:
        yield cur
    finally:
        conn.rollback()
        conn.close()


def test_acha_ficha_com_numero_formatado_a_partir_do_numero_cru(cursor_com_contatos):
    """O caso do tio Jose Olimpio: era exatamente isto que falhava e criava
    a ficha 'Desconhecido'."""
    achado = find_contact_by_phone(cursor_com_contatos, WA_CRU)
    assert achado is not None, "numero formatado pelo Google segue invisivel"
    assert achado["id"] == 1


def test_acha_ficha_com_numero_cru(cursor_com_contatos):
    """O caminho que ja funcionava antes nao pode ter regredido."""
    achado = find_contact_by_phone(cursor_com_contatos, "5511975598043")
    assert achado["id"] == 2


def test_numero_de_ninguem_devolve_none(cursor_com_contatos):
    """Sem isto o webhook pararia de criar fantasma pra desconhecido de
    verdade — que e comportamento desejado (politica B)."""
    assert find_contact_by_phone(cursor_com_contatos, "5511900000000") is None


def test_telefone_nulo_ou_fora_do_formato_nao_quebra(cursor_com_contatos):
    """`jsonb_typeof = 'array'` protege as linhas 3 e 4; sem a guarda,
    jsonb_array_elements levantaria erro e derrubaria o webhook."""
    assert find_contact_by_phone(cursor_com_contatos, "5511911111111") is None


def test_casa_por_qualquer_numero_da_ficha_nao_so_o_primeiro(cursor_com_contatos):
    achado = find_contact_by_phone(cursor_com_contatos, "5511988887777")
    assert achado["id"] == 5
    achado_fixo = find_contact_by_phone(cursor_com_contatos, "551130624437")
    assert achado_fixo["id"] == 5


def test_numero_curto_nao_consulta_o_banco(cursor_com_contatos):
    assert find_contact_by_phone(cursor_com_contatos, "1234") is None


def test_desempate_prefere_match_do_numero_inteiro(cursor_com_contatos):
    """
    Duas fichas compartilham os 8 digitos finais, mas so uma tem o numero
    INTEIRO igual. O LIKE antigo fazia LIMIT 1 sem ORDER BY: quem vencia
    dependia da ordem fisica da tabela. Aqui a ficha com id MAIOR tem que
    ganhar, o que so acontece pelo criterio de match forte.
    """
    cursor_com_contatos.execute("""
        INSERT INTO contacts (id, nome, telefones) VALUES
          (10, 'Colisao de sufixo (outro DDD)',
               '[{"type":"mobile","number":"+55 (21) 99252-6344"}]'),
          (11, 'Numero inteiro igual',
               '[{"type":"mobile","number":"+55 (61) 3062-4437"}]')
    """)
    achado = find_contact_by_phone(cursor_com_contatos, "556130624437")
    assert achado["id"] == 11, "match do numero inteiro tem que vencer o sufixo"


def test_colunas_extras_chegam_no_resultado(cursor_com_contatos):
    achado = find_contact_by_phone(cursor_com_contatos, WA_CRU, columns="id, nome")
    assert achado["nome"] == "Formatado Google"


def test_numero_no_formato_antigo_acha_a_ficha_gravada_no_formato_novo(cursor_com_contatos):
    """O caso Marcos Ribeiro: o WhatsApp entregou sem o 9, a ficha tem com o 9.
    Exercita a canonização no SQL (o CASE do ORDER BY), não só em Python."""
    cursor_com_contatos.execute("""
        INSERT INTO contacts (id, nome, telefones) VALUES
          (20, 'Marcos Ribeiro',
               '[{"type":"mobile","number":"+5535999851122","whatsapp":true}]')
    """)
    achado = find_contact_by_phone(cursor_com_contatos, "553599851122")
    assert achado is not None
    assert achado["id"] == 20


def test_desempate_canonico_vence_o_literal(cursor_com_contatos):
    """Duas fichas casam pelo sufixo, mas só uma é a mesma LINHA do número
    buscado — e ela está gravada na outra era da numeração. Sem canonizar o
    ORDER BY, o desempate cairia no menor id (a ficha errada)."""
    cursor_com_contatos.execute("""
        INSERT INTO contacts (id, nome, telefones) VALUES
          (30, 'Colisao de sufixo (outro DDD)',
               '[{"type":"mobile","number":"+5511999851122"}]'),
          (31, 'Mesma linha, formato novo',
               '[{"type":"mobile","number":"+5535999851122"}]')
    """)
    achado = find_contact_by_phone(cursor_com_contatos, "553599851122")
    assert achado["id"] == 31, "a canonização do ORDER BY não está valendo"
