"""Os dois defeitos que faziam 7 tasks falharem no push a cada run, desde abril.

Contexto (22/11/26). O cron `sync-tasks` reportava `push: {errors: 7, pushed: 1}`
duas vezes por dia, sempre os mesmos 7. Duas causas distintas:

① **400 "Request contains an invalid argument."** — o Google Tasks recusa `notes`
   acima de 8192 caracteres e nao diz qual argumento nem cita tamanho. Limite
   medido contra a API com sonda descartavel: 8192 passa, 8193 nao. Cinco tasks
   (de 9.950 a 18.553 caracteres de descricao) caiam nisso.

② **404 "Requested entity was not found."** — `google_task_id` apontando pra
   task apagada no Google. O push insistia no mesmo id pra sempre: #64 e #84
   falharam 2x/dia de abril a 22/11.

O terceiro defeito — o cron se declarar `success` com os 7 erros dentro do corpo
— esta em `test_cron_erro_em_subdict.py`.
"""
import asyncio

import pytest

from database import get_db
from services.tasks_sync import TasksSyncService


# ---------------------------------------------------------------- ① truncamento

class TestNotesNoLimiteDoGoogle:
    def test_descricao_curta_passa_intacta(self):
        assert TasksSyncService._notes_para_google("bom dia", 1) == "bom dia"

    def test_none_e_vazio_nao_viram_string(self):
        assert TasksSyncService._notes_para_google(None, 1) is None
        assert TasksSyncService._notes_para_google("", 1) == ""

    def test_exatamente_no_limite_passa_intacta(self):
        """8192 e o ultimo tamanho que a API aceita — cortar aqui seria perder
        texto a troco de nada."""
        desc = "x" * 8192
        assert TasksSyncService._notes_para_google(desc, 1) == desc

    def test_um_caractere_acima_do_limite_e_cortado(self):
        saida = TasksSyncService._notes_para_google("x" * 8193, 1)
        assert len(saida) <= 8192

    @pytest.mark.parametrize("tamanho", [8193, 9950, 10140, 10526, 11762, 18553])
    def test_os_tamanhos_reais_que_falhavam_cabem_depois_do_corte(self, tamanho):
        """Os cinco primeiros sao as descricoes das tasks que davam 400."""
        saida = TasksSyncService._notes_para_google("x" * tamanho, 999604)
        assert len(saida) <= TasksSyncService.GOOGLE_NOTES_MAX

    def test_o_corte_diz_onde_esta_a_integra(self):
        """Sem o ponteiro, quem le a task no celular nao sabe que falta texto —
        e o truncamento silencioso passa a esconder conteudo."""
        saida = TasksSyncService._notes_para_google("x" * 20000, 999604)
        assert "#999604" in saida
        assert "cortado" in saida.lower()

    def test_rodape_entra_DENTRO_do_orcamento(self):
        """Somar o rodape depois de cortar em 8192 estouraria o limite outra vez
        — e o 400 do Google nao diria que foi isso."""
        saida = TasksSyncService._notes_para_google("x" * 50000, 1)
        assert len(saida) <= TasksSyncService.GOOGLE_NOTES_MAX

    def test_corta_em_fim_de_linha_quando_ha_um_proximo_do_teto(self):
        desc = ("linha cheia " * 700) + "\n" + ("cauda " * 2000)
        saida = TasksSyncService._notes_para_google(desc, 1)
        assert len(saida) <= TasksSyncService.GOOGLE_NOTES_MAX
        corpo = saida.split("\n\n[...]")[0]
        assert not corpo.endswith("linha chei")   # nao parte palavra no meio


# ------------------------------------------------------------------- ② 404 orfao

class TestReconheceO404:
    def test_o_404_real_da_api(self):
        erro = '{\n  "error": {\n    "code": 404,\n    "message": "Requested entity was not found."\n  }\n}'
        assert TasksSyncService._e_404(erro)

    def test_o_400_de_notes_grande_nao_e_confundido_com_404(self):
        erro = '{\n  "error": {\n    "code": 400,\n    "message": "Request contains an invalid argument."\n  }\n}'
        assert not TasksSyncService._e_404(erro)

    def test_exige_as_duas_marcas(self):
        """Um 400 cujo texto por acaso mencione 'not found' nao deve soltar o
        id: soltar por engano faz o run seguinte CRIAR uma task duplicada."""
        assert not TasksSyncService._e_404('{"error": {"code": 400, "message": "not found in list"}}')
        assert not TasksSyncService._e_404('{"error": {"code": 404, "message": "quota exceeded"}}')

    def test_erro_nao_string_nao_quebra(self):
        assert not TasksSyncService._e_404(None)
        assert not TasksSyncService._e_404({"code": 404})


def _cria_task(titulo, status, google_task_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tasks (titulo, status, sync_status, google_task_id,
                               google_tasklist_id, etag, contexto)
            VALUES (%s, %s, 'pending_push', %s, '@default', 'etag-velho', 'professional')
            RETURNING id
        """, (titulo, status, google_task_id))
        task_id = cur.fetchone()["id"]
        conn.commit()
    return task_id


def _le(task_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT google_task_id, etag, sync_status, status FROM tasks WHERE id = %s",
                    (task_id,))
        return dict(cur.fetchone())


def _apaga(task_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        conn.commit()


class TestOrfaoNoBanco:
    """O id orfao e soltado, e e isso que tira a task da fila.

    Nao se inventa estado novo: `completed` sem `google_task_id` ja e excluida
    pela query do `push_all_pending` (o create do Google nao aceita status), e
    `pending` cai no create do run seguinte. A regra existia — faltava chegar
    nela.
    """

    @pytest.fixture(autouse=True)
    def _sem_rede(self, monkeypatch):
        monkeypatch.setattr(TasksSyncService, "get_access_token",
                            lambda self: _async("token-de-teste"))

    def _push_com_404(self, monkeypatch, task_id):
        erro = {"error": '{"error": {"code": 404, "message": "Requested entity was not found."}}'}
        svc = TasksSyncService()
        monkeypatch.setattr(svc.tasks_api, "update_task",
                            lambda **kw: _async(erro))
        return asyncio.run(svc.push_task_to_google(task_id))

    def test_task_concluida_perde_o_id_e_sai_da_fila(self, monkeypatch):
        task_id = _cria_task("[teste] orfao concluida", "completed", "id-que-nao-existe-mais")
        try:
            r = self._push_com_404(monkeypatch, task_id)
            assert r.get("orfao_resolvido") is True
            depois = _le(task_id)
            assert depois["google_task_id"] is None
            assert depois["etag"] is None
            assert depois["status"] == "completed"   # nao mexe no estado local
            assert task_id not in _ids_na_fila()
        finally:
            _apaga(task_id)

    def test_task_aberta_perde_o_id_e_sera_recriada(self, monkeypatch):
        task_id = _cria_task("[teste] orfao aberta", "pending", "id-que-nao-existe-mais")
        try:
            self._push_com_404(monkeypatch, task_id)
            assert _le(task_id)["google_task_id"] is None
            assert task_id in _ids_na_fila()   # volta pela porta do create
        finally:
            _apaga(task_id)

    def test_erro_que_NAO_e_404_preserva_o_id(self, monkeypatch):
        """O 400 e transitorio do ponto de vista do id: soltar o
        `google_task_id` por causa dele duplicaria a task no Google."""
        task_id = _cria_task("[teste] erro 400", "pending", "id-valido")
        try:
            svc = TasksSyncService()
            erro = {"error": '{"error": {"code": 400, "message": "Request contains an invalid argument."}}'}
            monkeypatch.setattr(svc.tasks_api, "update_task", lambda **kw: _async(erro))
            r = asyncio.run(svc.push_task_to_google(task_id))
            assert "orfao_resolvido" not in r
            assert _le(task_id)["google_task_id"] == "id-valido"
        finally:
            _apaga(task_id)


def _ids_na_fila():
    """Mesma query do `push_all_pending` — se ela mudar, estes testes acusam."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM tasks
            WHERE sync_status IN ('local_only', 'pending_push')
            AND (status != 'completed' OR google_task_id IS NOT NULL)
        """)
        return {r["id"] for r in cur.fetchall()}


async def _acoroutine(valor):
    return valor


def _async(valor):
    return _acoroutine(valor)
