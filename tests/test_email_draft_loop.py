"""
Fechamento do loop DRAFT -> ENVIADO (caso de 04/08/2026).

A camada montou um e-mail para o pai do Renato, deixou no rascunho do Gmail e
reportou "pronto no rascunho". Ele ja tinha enviado as 16h29. O sistema sabia o
que ELE criava e era cego ao que o Renato FAZIA.

Duas causas, as duas cobertas aqui:
  (1) `admin_gmail_proxy` action=create_draft devolvia o {id, message:{id,
      threadId}} do Gmail ao caller HTTP e descartava. Nao havia linha a
      reconciliar;
  (2) `gmail_outbound_sync` lia o `id` da mensagem enviada e IGNORAVA o
      `threadId` — a unica ancora estavel entre rascunho e enviado.

⚠️ O message id MUDA no envio: o rascunho tem um, o Gmail cria OUTRO ao
despachar. Casar por message id nao casa nunca e falharia CALADO (zero linhas,
zero erro — feedback_filtro_vocabulario_errado_falha_calado). Por isso ha teste
explicito de que o casamento por message id NAO acontece, e nao so de que o
casamento por thread acontece.

Rodar: .venv/bin/python -m pytest tests/test_email_draft_loop.py -v
"""
import os
import sys
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import email_draft_registry as reg  # noqa: E402
from services.tz import now_utc  # noqa: E402


# Ids reais no formato do Gmail. O do rascunho e o do enviado sao DIFERENTES de
# proposito: e esse o fato que derruba qualquer casamento por message id.
THREAD = "18f0c9a7b2e41d3a"
DRAFT_MSG_ID = "r-8812340099887766"      # message id DENTRO do rascunho
SENT_MSG_ID = "18f0c9b1d4e55c21"         # message id criado no envio
DRAFT_ID = "r8812340099887766"

CRIADO = now_utc() - timedelta(hours=2)   # rascunho criado as ~14h29
ENVIADO = now_utc() - timedelta(hours=1)  # Renato mandou as 16h29


# ---------------------------------------------------------------------------
# Fake cursor: uma tabela email_drafts em memoria, roteada por trecho do SQL.
# Mesmo padrao de tests/test_on_hold_lifecycle.py.
# ---------------------------------------------------------------------------
class _FakeCur:
    def __init__(self, drafts):
        self.drafts = drafts          # lista de dicts
        self.selects = []             # (sql, params) de cada SELECT
        self.updates = []             # (sql, params) de cada UPDATE
        self._rows = []
        self._one = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        low = s.lower()

        if low.startswith("update email_drafts"):
            self.updates.append((s, params))
            ids = set(params[3])
            for d in self.drafts:
                if d["id"] in ids:
                    d.update(status="sent", sent_at=params[0],
                             sent_message_id=params[1], reconciled_at=params[2])
            self._rows, self._one = [], None
            return

        if "from email_drafts" in low:
            self.selects.append((s, params))
            thread = params[0]
            self._rows = [d for d in self.drafts if d.get("thread_id") == thread]
            return

        if low.startswith("insert into email_drafts"):
            novo = {
                "id": len(self.drafts) + 1,
                "draft_id": params[0], "thread_id": params[1],
                "draft_message_id": params[2], "account_email": params[3],
                "to_emails": params[4], "subject": params[5],
                "contact_id": params[6], "source": params[7],
                "criado_em": params[8], "status": "pending",
            }
            self.drafts.append(novo)
            self._one = {"id": novo["id"]}
            return

        self._rows, self._one = [], None

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


def _draft(**over):
    d = {"id": 1, "thread_id": THREAD, "draft_message_id": DRAFT_MSG_ID,
         "status": "pending", "criado_em": CRIADO}
    d.update(over)
    return d


# ------------------------------------------------------- registro do rascunho --

class TestRegistro:
    def test_extrai_os_tres_ids_da_resposta_do_gmail(self):
        """Formato real do drafts.create — era isto que o proxy jogava fora."""
        ids = reg.extract_ids({"id": DRAFT_ID,
                               "message": {"id": DRAFT_MSG_ID, "threadId": THREAD}})
        assert ids == {"draft_id": DRAFT_ID, "thread_id": THREAD,
                       "draft_message_id": DRAFT_MSG_ID}

    def test_resposta_de_erro_nao_estoura(self):
        """Rascunho ja existe no Gmail; perder o registro e ruim, 500 e pior."""
        assert reg.extract_ids({"error": {"code": 403}})["draft_id"] is None
        assert reg.extract_ids(None)["draft_id"] is None

    def test_grava_thread_id_na_linha(self):
        cur = _FakeCur([])
        rid = reg.register_draft(
            cur, {"id": DRAFT_ID, "message": {"id": DRAFT_MSG_ID, "threadId": THREAD}},
            account_email="renato@almeida-prado.com", to_emails=["pai@exemplo.com"],
            subject="Assunto", source="cos_agent",
        )
        assert rid == 1
        assert cur.drafts[0]["thread_id"] == THREAD

    def test_sem_draft_id_nao_grava(self):
        cur = _FakeCur([])
        assert reg.register_draft(cur, {"error": "quota"}) is None
        assert cur.drafts == []


# ------------------------------------------------------------ reconciliacao --

class TestCasaPorThread:
    def test_rascunho_pendente_vira_enviado(self):
        """O caso do pai: rascunho as 14h29, envio as 16h29, mesma thread."""
        cur = _FakeCur([_draft()])
        ids = reg.reconcile_thread(cur, THREAD, sent_at=ENVIADO,
                                   sent_message_id=SENT_MSG_ID)

        assert ids == [1]
        assert cur.drafts[0]["status"] == "sent"
        assert cur.drafts[0]["sent_at"] == ENVIADO
        # Guarda o id do ENVIADO, nao o do rascunho — sao objetos diferentes.
        assert cur.drafts[0]["sent_message_id"] == SENT_MSG_ID

    def test_a_busca_e_por_thread_id(self):
        cur = _FakeCur([_draft()])
        reg.reconcile_thread(cur, THREAD, sent_at=ENVIADO, sent_message_id=SENT_MSG_ID)
        assert cur.selects[0][1] == (THREAD,)

    def test_data_do_envio_e_a_do_ato_nao_a_da_varredura(self):
        """Se gravasse now(), o ato das 16h29 apareceria como hora do cron.

        Mesma classe do bug de datacao do replay de WhatsApp (04/08): ao vivo a
        diferenca e de segundos e passa despercebida; no sync de 30min o e-mail
        aparece ate meia hora depois.
        """
        cur = _FakeCur([_draft()])
        reg.reconcile_thread(cur, THREAD, sent_at=ENVIADO, sent_message_id=SENT_MSG_ID)
        assert cur.drafts[0]["sent_at"] == ENVIADO
        assert cur.drafts[0]["reconciled_at"] > ENVIADO


class TestNaoCasaPorMessageId:
    def test_message_id_do_enviado_em_outra_thread_nao_fecha_nada(self):
        """O fato central: o id do rascunho != o id do enviado.

        Aqui o e-mail enviado carrega EXATAMENTE o draft_message_id do rascunho
        pendente — se alguem trocar a ancora para message id, este teste passa a
        casar coisa errada e falha.
        """
        cur = _FakeCur([_draft()])
        ids = reg.reconcile_thread(cur, "OUTRA_THREAD", sent_at=ENVIADO,
                                   sent_message_id=DRAFT_MSG_ID)

        assert ids == []
        assert cur.drafts[0]["status"] == "pending"
        assert cur.updates == []

    def test_nenhuma_coluna_de_message_id_entra_na_busca(self):
        """Guarda contra refactor: a query so pode filtrar por thread_id.

        Se message id voltar ao WHERE, a reconciliacao para de casar e nao
        levanta erro nenhum — o defeito seria invisivel ate alguem reparar que
        rascunho nenhum fecha.
        """
        import inspect
        src = inspect.getsource(reg.reconcile_thread)
        where = src.split("WHERE")[1].split('"""')[0]
        assert "thread_id = %s" in where
        assert "message_id" not in where
        assert "draft_message_id" not in where


class TestNaoEnviadoContinuaPendente:
    def test_sem_envio_na_thread_o_rascunho_fica_pendente(self):
        """Nenhuma outra thread pode encostar no rascunho."""
        cur = _FakeCur([_draft()])
        assert reg.reconcile_thread(cur, "18ffffffffffffff", sent_at=ENVIADO) == []
        assert cur.drafts[0]["status"] == "pending"

    def test_thread_id_vazio_nao_faz_varredura(self):
        """E-mail sem threadId nao pode virar curinga que fecha tudo."""
        cur = _FakeCur([_draft()])
        assert reg.reconcile_thread(cur, None, sent_at=ENVIADO) == []
        assert cur.selects == []

    def test_envio_anterior_a_criacao_nao_fecha(self):
        """Backfill historico: e-mail antigo da mesma thread nao e o rascunho.

        Sem esta guarda, `max_pages` alto sobre corpus de meses declararia
        enviado o que ninguem enviou — a armadilha que ja fez `post_process`
        existir no gmail_outbound_sync.
        """
        cur = _FakeCur([_draft(criado_em=now_utc())])
        antigo = now_utc() - timedelta(days=90)
        assert reg.reconcile_thread(cur, THREAD, sent_at=antigo) == []
        assert cur.drafts[0]["status"] == "pending"

    def test_envio_logo_apos_a_criacao_ainda_fecha(self):
        """A guarda de ordem nao pode matar o caso normal.

        Relogios diferentes (o nosso e o do cliente que despachou); alguns
        segundos de desvio nao podem custar a reconciliacao.
        """
        agora = now_utc()
        cur = _FakeCur([_draft(criado_em=agora)])
        assert reg.reconcile_thread(cur, THREAD, sent_at=agora - timedelta(seconds=30)) == [1]

    def test_rascunho_ja_enviado_nao_re_reconcilia(self):
        """O sync roda a cada 30min; reescrever sent_at trocaria a data do ato
        do Renato pela data da ultima varredura."""
        cur = _FakeCur([_draft(status="sent")])
        assert reg.reconcile_thread(cur, THREAD, sent_at=now_utc()) == []
        assert cur.updates == []

    def test_descartado_a_mao_nao_ressuscita(self):
        cur = _FakeCur([_draft(status="discarded")])
        assert reg.reconcile_thread(cur, THREAD, sent_at=ENVIADO) == []

    def test_criado_em_nulo_nao_afirma_envio(self):
        """Sem carimbo nao da pra afirmar a ordem dos fatos; pendente e o
        estado honesto."""
        cur = _FakeCur([_draft(criado_em=None)])
        assert reg.reconcile_thread(cur, THREAD, sent_at=ENVIADO) == []


class TestNaiveVsAware:
    def test_criado_em_naive_do_postgres_e_tratado_como_utc(self):
        """A coluna e TIMESTAMP (sem TZ): psycopg2 devolve naive. Comparar naive
        com aware estoura TypeError e mataria a run inteira do sync."""
        naive = CRIADO.replace(tzinfo=None)
        assert reg.draft_matches_sent(_draft(criado_em=naive), ENVIADO) is True

    def test_sent_at_ausente_usa_o_agora(self):
        """Header Date ilegivel: o e-mail esta em `in:sent` AGORA, entao o envio
        aconteceu — nao pode virar rascunho eternamente pendente."""
        assert reg.draft_matches_sent(_draft(), None) is True


# ------------------------------------------- ponta que produz x ponta que le --

class TestFiacaoDoSync:
    def test_outbound_sync_grava_thread_id_no_metadata(self):
        """Era o `threadId` descartado em gmail_outbound_sync que deixava o loop
        sem ancora. As duas pontas tem que usar a MESMA chave."""
        import inspect
        from services import gmail_outbound_sync as gos

        src = inspect.getsource(gos._upsert_outbound_message)
        assert '"thread_id": thread_id' in src

        loop = inspect.getsource(gos.sync_account_outbound)
        assert 'msg.get("threadId")' in loop, "voltou a ler so o id da mensagem"
        assert "reconcile_sent_threads" in loop

    def test_proxy_persiste_o_que_o_create_draft_devolve(self):
        """O `return r.json()` cru era a causa raiz numero 1."""
        import inspect
        import main

        src = inspect.getsource(main.admin_gmail_proxy)
        bloco = src.split('if action == "create_draft":')[1].split('if action == "send_message":')[0]
        assert "register_draft" in bloco, "o proxy voltou a descartar a resposta do Gmail"

    def test_reconciliacao_roda_mesmo_no_backfill(self):
        """Ao contrario do dismiss de propostas, aqui `post_process=False` nao
        pode desligar: a guarda de ordem ja torna o backfill inofensivo."""
        import inspect
        from services import gmail_outbound_sync as gos

        src = inspect.getsource(gos.sync_account_outbound)
        trecho = src.split("if threads_enviadas:")[1]
        assert "post_process" not in trecho.split("return stats")[0]


class TestTimezone:
    def test_modulo_nao_usa_datetime_naive(self):
        """Convencao do repo: services.tz, nunca utcnow()/now() sem tz."""
        import inspect
        src = inspect.getsource(reg)
        assert "utcnow()" not in src
        assert "datetime.now()" not in src
        assert "from services.tz import" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
