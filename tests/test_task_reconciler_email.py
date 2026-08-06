"""
A resposta que volta por E-MAIL — 03-06/08/2026.

CASO REAL: task #999695 "[Reorg 7] FUP Piccino", espera de `joao@piccino.com.br`.
O e-mail JA estava em `messages` (1.769 linhas com `conversations.canal='email'`,
desde jul/2025) e mesmo assim nenhuma reconciliacao a enxergava. Tres furos
somados, todos medidos em prod em 06/08:

  (a) o cruzamento era `m.contact_id = t.contact_id` e **81% dos e-mails tem
      `messages.contact_id` NULL** (1.436 de 1.769);
  (b) FICHA IRMA — a task aponta pra #2869 e a thread inteira esta gravada na
      #2858, as duas com `joao@piccino.com.br` (56 enderecos em mais de uma
      ficha, 133 fichas envolvidas);
  (c) task SEM `contact_id` (73 das 119 abertas) sumia de qualquer gate.

Este arquivo prova as tres pernas do conserto. Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_task_reconciler_email.py -q

O bloco final (classe TestProd) fala com o Neon de PRODUCAO em MODO LEITURA e so
roda com `RECONCILER_PROD_CHECK=1` no ambiente — e a prova contra o dado de
verdade, nao um teste de CI:
  RECONCILER_PROD_CHECK=1 PYTHONPATH=app .venv/bin/pytest tests/test_task_reconciler_email.py -q
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services.contact_identity import (  # noqa: E402
    contact_ids_by_emails,
    extract_emails,
    message_emails_sql,
)
from services import task_reconciler as tr  # noqa: E402


# ===========================================================================
# 1. Extracao de endereco — o cano da task SEM contact_id
# ===========================================================================

def test_extrai_endereco_da_descricao_da_task():
    """A descricao da #999695 registra pra QUEM o e-mail foi. Enquanto a task
    nao tinha `contact_id`, esse texto era a unica identidade disponivel."""
    desc = ("E-mail ENVIADO ao Piccino em 27/07/2026 19:19 BRT "
            "(joao@piccino.com.br); thread sem resposta ate 30/07.")
    assert extract_emails(desc) == ["joao@piccino.com.br"]


def test_extrai_dedup_e_normaliza():
    assert extract_emails("A <Nick@Luminosita.IT> e nick@luminosita.it.") == \
        ["nick@luminosita.it"]


def test_ponto_final_da_frase_nao_entra_no_endereco():
    """"...(nick@luminosita.it)." — o `.` de fim de frase colado no dominio
    faria o endereco nunca casar com o `from` da mensagem."""
    assert extract_emails("mandei pro nick@luminosita.it.") == ["nick@luminosita.it"]


def test_texto_sem_endereco_nao_inventa_identidade():
    assert extract_emails("Aguardar retorno do Nick sobre o modelo") == []
    assert extract_emails(None) == []


# ===========================================================================
# 2. O fragmento SQL — contrato com quem monta a query
# ===========================================================================

def test_fragmento_consome_exatamente_dois_placeholders():
    """`_scope_where` empurra o MESMO array duas vezes; errar isso desalinha a
    tupla inteira de parametros e a query cai no runtime, em producao."""
    assert message_emails_sql("m").count("%s") == 2


def test_fragmento_e_direcional():
    """incoming casa pelo `from`, outgoing pelo `to`. Sem isso, um e-mail de
    outra pessoa com o contato em copia contaria como resposta do contato — e
    "o terceiro respondeu" e justamente o gatilho que fecha task de espera."""
    frag = message_emails_sql("m")
    assert "direcao = 'incoming'" in frag
    assert "'from'" in frag and "'to'" in frag


def test_fragmento_nao_carrega_porcento_solto():
    """Um `%` fora de `%s` faz o psycopg2 estourar ("unsupported format
    character") em query com parametros. A regex nasceu sem `%` de proposito
    pra o MESMO fragmento rodar colado no psql e via driver."""
    assert message_emails_sql("m").replace("%s", "").count("%") == 0


def test_scope_where_sem_identidade_nao_casa_nada():
    """Escopo vazio tem que virar FALSE explicito. Um WHERE vazio devolveria a
    base inteira e o reconciliador fecharia task por mensagem de estranho."""
    cond, params = tr._scope_where({"contact_ids": [], "emails": []})
    assert cond == "FALSE" and params == []


def test_scope_where_junta_as_duas_pernas():
    cond, params = tr._scope_where({"contact_ids": [2869, 2858],
                                    "emails": ["joao@piccino.com.br"]})
    assert "contact_id = ANY(%s)" in cond and "cv.canal = 'email'" in cond
    assert params[0] == [2869, 2858]
    assert params[1] == params[2] == ["joao@piccino.com.br"]  # o array vai 2×


# ===========================================================================
# 3. SQL de verdade (Postgres local, tabelas TEMPORARIAS, rollback)
#    Reproduz o caso Piccino: task numa ficha, thread na ficha irma, resposta
#    chegando com contact_id NULL.
# ===========================================================================

def _pg():
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        return psycopg2.connect(
            os.getenv("LOCAL_TEST_DATABASE_URL", "postgresql://rap@localhost:5432/intel"),
            cursor_factory=RealDictCursor, connect_timeout=2,
        )
    except Exception:
        return None


@pytest.fixture
def cursor_caso_piccino():
    """Cursor sobre copias TEMPORARIAS de contacts/conversations/messages, que
    sombreiam as reais dentro da transacao. Tudo em rollback: nao escreve."""
    conn = _pg()
    if conn is None:
        pytest.skip("Postgres local indisponivel")
    cur = conn.cursor()
    cur.execute("""
        CREATE TEMPORARY TABLE contacts (id INT PRIMARY KEY, nome TEXT, emails JSONB) ON COMMIT DROP;
        CREATE TEMPORARY TABLE conversations (id INT PRIMARY KEY, canal TEXT) ON COMMIT DROP;
        CREATE TEMPORARY TABLE messages (
            id INT PRIMARY KEY, conversation_id INT, contact_id INT, direcao TEXT,
            conteudo TEXT, metadata JSONB, enviado_em TIMESTAMP, recebido_em TIMESTAMP,
            criado_em TIMESTAMP
        ) ON COMMIT DROP;
    """)
    cur.execute("""
        INSERT INTO contacts (id, nome, emails) VALUES
          (2869, 'João Carlos de Almeida Prado e Piccino',
                 '[{"tipo":"trabalho","email":"joao@piccino.com.br","primary":true}]'),
          (2858, 'Joao',
                 '[{"type":"home","email":"joao@piccino.com.br","primary":true}]'),
          (9999, 'Terceiro Qualquer',
                 '[{"type":"work","email":"outro@exemplo.com"}]');
        INSERT INTO conversations (id, canal) VALUES (10,'email'), (20,'whatsapp');
        INSERT INTO messages (id, conversation_id, contact_id, direcao, conteudo, metadata, criado_em) VALUES
          -- (A) o FUP que o Renato mandou: gravado na ficha IRMA (#2858)
          (100, 10, 2858, 'outgoing', 'Dr. João, seguem as 3 duvidas...',
           '{"to":["joao@piccino.com.br"],"account":"renato@almeida-prado.com"}', '2026-07-27 22:19:37'),
          -- (B) A RESPOSTA. contact_id NULL (81% dos e-mails sao assim) e o
          --     remetente no formato "Nome <endereco>" (13 linhas em prod).
          (101, 10, NULL, 'incoming', 'Renato, respondendo aos 3 pontos: a Empreseira pode...',
           '{"from":"João Piccino <joao@piccino.com.br>","from_name":"João Piccino"}', '2026-08-06 12:00:00'),
          -- (C) ARMADILHA: e-mail de OUTRA pessoa com o Piccino em copia.
          --     Nao pode contar como "o Piccino respondeu".
          (102, 10, NULL, 'incoming', 'Segue a minuta para conhecimento',
           '{"from":"outro@exemplo.com","to":["joao@piccino.com.br"]}', '2026-08-06 13:00:00'),
          -- (D) ARMADILHA: endereco que CONTEM o do alvo. ILIKE casaria.
          (103, 10, NULL, 'incoming', 'spam', '{"from":"naoejoao@piccino.com.br.br"}', '2026-08-06 14:00:00');
    """)
    yield cur
    conn.rollback()
    conn.close()


def _casadas(cur, emails, contact_ids=()):
    cond, params = tr._scope_where({"contact_ids": list(contact_ids), "emails": emails})
    cur.execute(f"""
        SELECT m.id FROM messages m
        LEFT JOIN conversations cv ON cv.id = m.conversation_id
        WHERE {cond} ORDER BY m.id
    """, tuple(params))
    return [r["id"] for r in cur.fetchall()]


def test_ficha_irma_e_encontrada_pelo_endereco(cursor_caso_piccino):
    """#2869 e #2858 sao a MESMA pessoa. Sem isto, a task da #2869 nunca ve a
    thread da #2858 — o furo (b), que sobrevive ate ao `contact_id` preenchido."""
    assert sorted(contact_ids_by_emails(cursor_caso_piccino,
                                        ["joao@piccino.com.br"])) == [2858, 2869]


def test_a_resposta_orfa_e_detectada(cursor_caso_piccino):
    """A prova do pedido: a resposta do Piccino chega com `contact_id` NULL e
    ainda assim casa com a task. Pelo cano velho (`m.contact_id=2869`) o
    resultado seria VAZIO — a task ficaria cega pra sempre."""
    casadas = _casadas(cursor_caso_piccino, ["joao@piccino.com.br"], contact_ids=[2869])
    assert 101 in casadas, "a resposta por e-mail tem que ser vista"

    cursor_caso_piccino.execute("SELECT id FROM messages WHERE contact_id = 2869")
    assert cursor_caso_piccino.fetchall() == [], "cano velho: zero evidencia (o defeito)"


def test_o_fup_na_ficha_irma_tambem_entra(cursor_caso_piccino):
    assert 100 in _casadas(cursor_caso_piccino, ["joao@piccino.com.br"], contact_ids=[2869])


def test_terceiro_com_o_contato_em_copia_nao_conta_como_resposta(cursor_caso_piccino):
    """#102 e incoming de `outro@exemplo.com` com o Piccino no `to`. Casar `to`
    em incoming reabriria/fecharia espera com mensagem de quem nao e o esperado."""
    assert 102 not in _casadas(cursor_caso_piccino, ["joao@piccino.com.br"])


def test_endereco_que_contem_o_alvo_nao_casa(cursor_caso_piccino):
    """`naoejoao@piccino.com.br.br` contem `joao@piccino.com.br`. O casamento e
    por igualdade de endereco extraido, nao por `ILIKE '%...%'` — que e como o
    matching ingenuo de e-mail->contato erra hoje."""
    assert 103 not in _casadas(cursor_caso_piccino, ["joao@piccino.com.br"])


def test_sem_identidade_nao_casa_nada(cursor_caso_piccino):
    assert _casadas(cursor_caso_piccino, []) == []


def test_conversa_no_whatsapp_nao_expulsa_o_email_da_janela(cursor_caso_piccino, monkeypatch):
    """REGRESSAO ACHADA PELO TESTE CONTRA PROD (06/08).

    A #999735 tinha 6 WhatsApps trocados com a Fran no dia 06/08 e o e-mail ao
    Nick era de 05/08: ordenando so por data, as 8 vagas do prompt eram todas de
    WhatsApp e o e-mail — a UNICA evidencia do canal que a task espera — ficava
    de fora. Cruzar e-mail sem cota por canal nao teria resolvido nada."""
    cursor_caso_piccino.execute("""
        INSERT INTO messages (id, conversation_id, contact_id, direcao, conteudo, criado_em)
        SELECT 200 + g, 20, 2869, CASE WHEN mod(g, 2) = 0 THEN 'incoming' ELSE 'outgoing' END,
               'papo de whatsapp ' || g, TIMESTAMP '2026-08-07 10:00:00' + (g || ' min')::interval
          FROM generate_series(1, 10) g
    """)

    class _Conn:
        def cursor(self_):
            return cursor_caso_piccino

        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

    monkeypatch.setattr(tr, "get_db", lambda: _Conn())
    msgs = tr._fetch_messages_since(
        {"contact_ids": [2869], "emails": ["joao@piccino.com.br"]},
        "2026-01-01 00:00:00",
    )
    canais = [m["canal"] for m in msgs]
    assert "email" in canais, f"o e-mail sumiu na enxurrada de WhatsApp: {canais}"
    assert canais.count("email") >= 2


# ===========================================================================
# 4. PROVA EM PRODUCAO — somente leitura, opt-in
# ===========================================================================

@pytest.mark.skipif(os.getenv("RECONCILER_PROD_CHECK") != "1",
                    reason="prova contra o Neon de prod; ligar com RECONCILER_PROD_CHECK=1")
class TestProd:
    """Le o Neon de producao em transacao READ ONLY (`set_session(readonly=True)`):
    o proprio driver recusa qualquer escrita. Nenhuma task e fechada ou reaberta
    — so as funcoes de LEITURA do reconciliador sao chamadas."""

    @pytest.fixture(scope="class")
    def prod(self):
        import psycopg2
        import psycopg2.extras
        from pathlib import Path

        env = {}
        arq = Path(_ROOT) / ".env"
        if not arq.exists():                      # worktree nao carrega .env
            arq = Path("/Users/rap/prospect-system/.env")
        if not arq.exists():
            pytest.skip(".env indisponivel")
        for line in arq.read_text().splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
        url = env.get("DATABASE_URL_UNPOOLED") or env.get("DATABASE_URL")
        if not url:
            pytest.skip("DATABASE_URL ausente")
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.set_session(readonly=True, autocommit=True)

        class RO:
            def cursor(self_):
                return conn.cursor()

            def commit(self_):
                raise AssertionError("read-only: ninguem devia commitar")

            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        tr.get_db = lambda: RO()
        yield conn
        conn.close()

    def _task(self, conn, tid):
        cur = conn.cursor()
        cur.execute("""SELECT id, titulo, descricao, contact_id, status, data_criacao
                       FROM tasks WHERE id = %s""", (tid,))
        row = cur.fetchone()
        if not row:
            pytest.skip(f"task #{tid} nao existe mais nesta base")
        return dict(row)

    def test_999695_alcanca_a_thread_de_email(self, prod):
        """A ficha da task (#2869) NAO tem a thread; a irma (#2858) tem. Medido
        em 06/08: 0 e-mails pelo cano velho, 1 pelo novo."""
        t = self._task(prod, 999695)
        scope = tr._task_scope(t)
        assert "joao@piccino.com.br" in scope["emails"]
        assert 2858 in scope["contact_ids"], "a ficha irma tem que entrar no escopo"

        cur = prod.cursor()
        cond, params = tr._scope_where(scope)
        cur.execute(f"""
            SELECT count(*) n FROM messages m
            LEFT JOIN conversations cv ON cv.id = m.conversation_id
            WHERE {cond} AND cv.canal = 'email'
              AND COALESCE(m.enviado_em, m.recebido_em, m.criado_em) > %s
        """, tuple(params) + (t["data_criacao"],))
        novo = cur.fetchone()["n"]

        cur.execute("""
            SELECT count(*) n FROM messages m
            JOIN conversations cv ON cv.id = m.conversation_id
            WHERE m.contact_id = %s AND cv.canal='email'
              AND COALESCE(m.enviado_em, m.recebido_em, m.criado_em) > %s
        """, (t["contact_id"], t["data_criacao"]))
        velho = cur.fetchone()["n"]

        assert velho == 0, "cano velho enxergava zero e-mail — o defeito relatado"
        assert novo > 0, "cano novo tem que enxergar a thread do Piccino"

    def test_999735_sem_ficha_e_alcancada_pelo_endereco_no_texto(self, prod):
        """Segunda ocorrencia VIVA do mesmo defeito, achada ao medir: "Aguardar
        retorno do Nick", `contact_id` NULL, `nick@luminosita.it` na descricao."""
        t = self._task(prod, 999735)
        if t["contact_id"]:
            pytest.skip("a task ja foi linkada a uma ficha — o furo (c) foi corrigido no dado")
        scope = tr._task_scope(t)
        assert scope["origem"] == "texto"
        assert "nick@luminosita.it" in scope["emails"]
        msgs = tr._fetch_messages_since(scope, t["data_criacao"])
        assert any(m["canal"] == "email" for m in msgs), \
            "o e-mail enviado ao Nick tem que aparecer como evidencia"

    def test_endereco_do_dono_nunca_vira_identidade_de_terceiro(self, prod):
        """A #999704 cita `renato@almeida-prado.com` na descricao. Sem a
        subtracao dos enderecos do dono, ela passaria a se "fechar" com o eco do
        proprio sistema ([[feedback_maquina_ouve_o_proprio_eco]])."""
        cur = prod.cursor()
        cur.execute("""SELECT id, titulo, descricao, contact_id, data_criacao FROM tasks
                       WHERE status IN ('pending','on_hold') AND contact_id IS NULL
                         AND (coalesce(titulo,'')||' '||coalesce(descricao,''))
                             ILIKE '%%renato@almeida-prado.com%%' LIMIT 1""")
        row = cur.fetchone()
        if not row:
            pytest.skip("nenhuma task aberta sem ficha citando o endereco do dono")
        scope = tr._task_scope(dict(row))
        assert "renato@almeida-prado.com" not in scope["emails"]
