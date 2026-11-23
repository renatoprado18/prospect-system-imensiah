"""O pull do Google não grava uma descrição MENOR em cima do original.

Contexto (22/11/26), e o dano foi real — causado pelo conserto anterior desta
mesma sessão. O push passou a cortar `notes` em 8192 caracteres (limite da API
do Google Tasks). O `full_sync` roda push e PULL na mesma execução: o pull
trouxe de volta o texto já cortado e o gravou sobre o original. Quatro tasks
perderam de 1.900 a 3.700 caracteres — #999932 (9.950→8.001), #1000432
(10.140→8.014), #1000600 (10.526→8.099), #1000606 (11.762→8.036) — e o rodapé
que prometia "íntegra na task #N do INTEL" passou a apontar para o próprio
resumo. Recuperadas de um branch PITR do Neon.

O INTEL é a fonte da descrição; o Google é uma superfície de leitura com teto
menor. Encolhimento vindo de fora é perda, não edição
([[feedback_estado_local_morre_no_sync_externo]]).

O critério é o TAMANHO, não a marca do rodapé: qualquer caminho que devolva
texto truncado cai aqui, não só o nosso.
"""
import asyncio

import pytest

from database import get_db
from services.tasks_sync import TasksSyncService

GRANDE = "A" * 10140          # o tamanho real da #1000432
CORTADO = "A" * 8014          # o que o Google devolveu depois do corte


def _cria(descricao, google_task_id, status="pending"):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tasks (titulo, descricao, status, sync_status,
                               google_task_id, google_tasklist_id, contexto,
                               last_synced_at)
            VALUES (%s, %s, %s, 'synced', %s, '@default', 'professional',
                    CURRENT_TIMESTAMP)
            RETURNING id
        """, ("[teste] pull não encurta", descricao, status, google_task_id))
        task_id = cur.fetchone()["id"]
        conn.commit()
    return task_id


def _descricao(task_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT descricao FROM tasks WHERE id = %s", (task_id,))
        return cur.fetchone()["descricao"]


def _apaga(task_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        conn.commit()


def _pull(google_task):
    svc = TasksSyncService()
    return asyncio.run(svc._sync_google_task_to_local(google_task, "@default"))


@pytest.fixture
def task(request):
    """Cria a task, devolve o id, apaga no teardown."""
    criadas = []

    def _fab(descricao, gid, status="pending"):
        task_id = _cria(descricao, gid, status)
        criadas.append(task_id)
        return task_id

    yield _fab
    for task_id in criadas:
        _apaga(task_id)


class TestNaoEncurta:
    def test_versao_truncada_nao_sobrescreve_a_integra(self, task):
        """O defeito exato: 10.140 caracteres viraram 8.014."""
        task_id = task(GRANDE, "g-encurta")
        _pull({"id": "g-encurta", "title": "t", "notes": CORTADO, "status": "needsAction"})
        assert len(_descricao(task_id)) == len(GRANDE)

    def test_notes_vazio_nao_apaga_a_descricao(self, task):
        task_id = task(GRANDE, "g-vazio")
        _pull({"id": "g-vazio", "title": "t", "notes": "", "status": "needsAction"})
        assert len(_descricao(task_id)) == len(GRANDE)

    def test_notes_ausente_nao_apaga_a_descricao(self, task):
        """`notes` nem vem no payload quando a task do Google não tem corpo."""
        task_id = task(GRANDE, "g-sem-notes")
        _pull({"id": "g-sem-notes", "title": "t", "status": "needsAction"})
        assert len(_descricao(task_id)) == len(GRANDE)

    def test_qualquer_encurtamento_e_barrado_nao_so_o_nosso(self, task):
        """Sem marca de rodapé: o critério é tamanho, então truncamento de outra
        origem (outro cliente, outra integração) também não passa."""
        task_id = task(GRANDE, "g-outro")
        _pull({"id": "g-outro", "title": "t", "notes": "texto curto sem rodape",
               "status": "needsAction"})
        assert len(_descricao(task_id)) == len(GRANDE)


class TestAindaAceitaOQueDeveAceitar:
    def test_texto_MAIOR_vindo_do_google_entra(self, task):
        """Edição real feita no celular que ACRESCENTA tem que valer — senão o
        pull deixa de sincronizar descrição."""
        task_id = task(CORTADO, "g-cresce")
        _pull({"id": "g-cresce", "title": "t", "notes": GRANDE, "status": "needsAction"})
        assert len(_descricao(task_id)) == len(GRANDE)

    def test_descricao_local_vazia_e_preenchida_pelo_google(self, task):
        task_id = task(None, "g-nula")
        _pull({"id": "g-nula", "title": "t", "notes": "veio do celular",
               "status": "needsAction"})
        assert _descricao(task_id) == "veio do celular"

    def test_mesmo_tamanho_com_texto_diferente_entra(self, task):
        """Edição que troca palavras sem mudar o tamanho não é encolhimento."""
        task_id = task("B" * 500, "g-igual")
        _pull({"id": "g-igual", "title": "t", "notes": "C" * 500,
               "status": "needsAction"})
        assert _descricao(task_id) == "C" * 500
