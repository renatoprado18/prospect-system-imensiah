"""
Dois defeitos da camada CoS achados na auditoria de 30/07 — ambos SILENCIOSOS.

(1) PARQUEIO INVISÍVEL. O único sinal de estado que chegava ao prompt era
    `[ATRASADA]`, derivado só da data, e o filtro do gather deixa `on_hold`
    passar. Medido em prod: das 11 tasks que a camada apresentava como atrasadas
    nas frentes ativas, **8 estavam PARQUEADAS** pela CoS com janela de espera
    correndo. O modelo lia "atrasada há 8 dias", aplicava a regra dos 7 dias da
    memória e mandava cobrar — desfazendo o parqueio todo dia. Foi o que
    produziu o portão do Filito de 30/07 (#999637, parqueada até 06/08).

(2) CIRCUITO FECHADO DO SELF-CHAT. Toda notificação que a máquina manda pro
    Renato é gravada como `outgoing` na conversa dele com ele mesmo (395 em 30
    dias) e o roteador ligava essas mensagens a frentes (31 links em 10
    frentes). Elas chegavam ao prompt sob "AÇÕES RECENTES DO RENATO — o que ELE
    JÁ enviou", e a REGRA DE OUTBOUND manda não cobrar o que ele já fez: o aviso
    da máquina virava prova de que ele agiu e podia DERRUBAR UM PORTÃO REAL.

⚠️ O teste de `owner_contact_ids` existe porque a 1ª versão do fix (2) usava
`users.id=1.contact_id` e teria ficado INERTE: esse aponta pra ficha #14911
(1 conversa) enquanto o self-chat vive na #23419 (18). Filtro com vocabulário
errado compila, roda, devolve zero linha e não dá erro —
[[feedback_filtro_vocabulario_errado_falha_calado]]. Se alguém "simplificar"
`owner_contact_ids` de volta pra um id só, estes testes quebram.

Rodar: PYTHONPATH=app python -m pytest tests/test_frente_review_parqueio_e_selfchat.py -v
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import frente_review as fr  # noqa: E402
from services.task_reconciler import (  # noqa: E402
    ON_HOLD_WAIT_DAYS, REASON_ESPERA, REASON_INDEFINIDO,
)

HOJE = "2026-07-30"
_ONTEM = datetime(2026, 7, 22)          # vencida há 8 dias
_PARQUEIO = datetime(2026, 7, 30)       # parqueada hoje


def _task(**over):
    t = {"id": 999637, "titulo": "[Filito] Receber evaluation do geólogo",
         "descricao": None, "status": "pending", "prioridade": 7,
         "data_vencimento": _ONTEM, "on_hold_since": None,
         "on_hold_reason": None, "responsavel": None}
    t.update(over)
    return t


class TestMarcadorDeTask:
    """O rótulo de estado que o modelo lê. É a única coisa que ele sabe."""

    def test_vencida_de_verdade_segue_atrasada(self):
        assert fr._task_marker(_task(), HOJE) == " [ATRASADA]"

    def test_sem_prazo_nao_marca(self):
        assert fr._task_marker(_task(data_vencimento=None), HOJE) == ""

    def test_prazo_futuro_nao_marca(self):
        futuro = datetime(2026, 8, 15)
        assert fr._task_marker(_task(data_vencimento=futuro), HOJE) == ""

    def test_parqueada_esperando_terceiro_NAO_e_atrasada(self):
        """O defeito exato do Filito: vencida em 22/07 MAS parqueada."""
        m = fr._task_marker(
            _task(status="on_hold", on_hold_since=_PARQUEIO,
                  on_hold_reason=REASON_ESPERA), HOJE)
        assert "[ATRASADA]" not in m
        assert "PARQUEADA" in m
        assert "NÃO cobrar" in m and "NÃO é portão" in m

    def test_parqueada_mostra_a_data_de_volta(self):
        """A data vem de GREATEST(vencimento, parqueio) + janela — espelha o
        _wait_deadline do sweep, que é quem de fato reabre."""
        m = fr._task_marker(
            _task(status="on_hold", on_hold_since=_PARQUEIO,
                  on_hold_reason=REASON_ESPERA), HOJE)
        esperado = (_PARQUEIO + timedelta(days=ON_HOLD_WAIT_DAYS)).strftime("%d/%m")
        assert esperado in m, m

    def test_parqueio_indefinido_diz_que_saiu_do_radar(self):
        m = fr._task_marker(
            _task(status="on_hold", on_hold_since=_PARQUEIO,
                  on_hold_reason=REASON_INDEFINIDO), HOJE)
        assert "tirou do radar" in m
        assert "[ATRASADA]" not in m

    def test_parqueada_sem_carimbo_ainda_nao_e_atrasada(self):
        """Passivo pré-migration-058: on_hold sem on_hold_since. Degrada pro
        rótulo genérico — nunca de volta pra [ATRASADA]."""
        m = fr._task_marker(_task(status="on_hold"), HOJE)
        assert "PARQUEADA" in m and "[ATRASADA]" not in m

    def test_em_andamento_nao_vira_atrasada(self):
        m = fr._task_marker(_task(status="in_progress"), HOJE)
        assert m == " [EM ANDAMENTO]"

    def test_janela_dos_7_dias_nao_e_recopiada(self):
        """A regra vive UMA vez, no task_reconciler. Este módulo a lê."""
        import inspect
        src = inspect.getsource(fr._task_marker)
        assert "ON_HOLD_WAIT_DAYS" in src
        assert "timedelta(days=7)" not in src


class TestPromptMostraOParqueio:
    def test_bloco_de_tarefas_carrega_o_rotulo(self):
        g = {"project": {"id": 44, "nome": "Mina de Filito", "descricao": ""},
             "tasks": [_task(status="on_hold", on_hold_since=_PARQUEIO,
                             on_hold_reason=REASON_ESPERA)],
             "members": [], "dms": [], "routed": [], "notes": [],
             "groups": [], "memories": [], "renato_outbound": []}
        txt = fr._fmt_gather(g)
        assert "PARQUEADA" in txt
        assert "[ATRASADA]" not in txt

    def test_regra_de_parqueio_esta_no_system_prompt(self):
        """Regra em comentário Python o modelo não lê —
        [[feedback_prompt_nao_le_comentario]]. Tem de estar no _SYSTEM."""
        assert "PARQUEADA" in fr._SYSTEM
        assert "não vira `precisa_de_voce`" in fr._SYSTEM


class _FakeCursor:
    """Cursor mínimo: responde a busca por telefone e a users.id=1."""

    def __init__(self, por_telefone, user_contact_id):
        self._por_telefone = por_telefone
        self._user_contact_id = user_contact_id
        self._rows = []

    def execute(self, sql, params=None):
        if "FROM contacts" in sql:
            self._rows = [{"id": i} for i in self._por_telefone]
        elif "FROM users" in sql:
            self._rows = ([{"contact_id": self._user_contact_id}]
                          if self._user_contact_id is not None else [])
        else:  # pragma: no cover
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class TestResolvedorDoDono:
    """owner_contact_ids: a base tem 4 fichas do Renato. Tratar como uma só
    foi o que deixou o 1º fix inerte."""

    def test_une_telefone_e_users(self):
        from services.contact_identity import owner_contact_ids
        cur = _FakeCursor(por_telefone=[23419], user_contact_id=14911)
        assert sorted(owner_contact_ids(cur)) == [14911, 23419]

    def test_nao_duplica_quando_batem(self):
        from services.contact_identity import owner_contact_ids
        cur = _FakeCursor(por_telefone=[23419], user_contact_id=23419)
        assert owner_contact_ids(cur) == [23419]

    def test_pega_todas_as_fichas_do_mesmo_telefone(self):
        """O ponto do fix: o self-chat pode estar em QUALQUER uma delas."""
        from services.contact_identity import owner_contact_ids
        cur = _FakeCursor(por_telefone=[23419, 25407, 26638], user_contact_id=14911)
        assert len(owner_contact_ids(cur)) == 4

    def test_users_sozinho_nao_bastaria(self):
        """Regressão do defeito real: users.id=1 -> #14911, self-chat -> #23419."""
        from services.contact_identity import owner_contact_ids
        cur = _FakeCursor(por_telefone=[23419], user_contact_id=14911)
        assert 23419 in owner_contact_ids(cur)

    def test_vazio_quando_nada_resolve(self):
        """Vazio = defeito. Quem chama trata como 'não filtra', nunca
        'filtra tudo' — por isso os call-sites usam `owner_ids or [0]`."""
        from services.contact_identity import owner_contact_ids
        cur = _FakeCursor(por_telefone=[], user_contact_id=None)
        assert owner_contact_ids(cur) == []


class TestSelfChatForaDasJanelas:
    """As 3 janelas de mensagem da camada precisam excluir o self-chat, e o
    roteador precisa parar de criar links novos."""

    def test_as_tres_janelas_filtram(self):
        import inspect
        src = inspect.getsource(fr._gather_frente)
        src += inspect.getsource(fr._gather_renato_outbound)
        # dms, routed e outbound — 3 ocorrências do filtro
        assert src.count("NOT (cv.contact_id = ANY(%s))") == 3, src.count(
            "NOT (cv.contact_id = ANY(%s))")

    def test_roteador_nao_etiqueta_self_chat(self):
        import inspect
        from services import signal_router
        src = inspect.getsource(signal_router._candidate_inbound)
        assert "NOT (cv.contact_id = ANY(%s))" in src
        assert "owner_contact_ids" in src

    def test_ninguem_usa_users_id_1_pra_self_chat(self):
        """Guarda contra a regressão: se alguém trocar o resolvedor pelo
        atalho, o filtro volta a apontar pra ficha errada e fica inerte."""
        import inspect
        from services import signal_router
        for mod in (fr, signal_router):
            src = inspect.getsource(mod)
            assert "SELECT contact_id FROM users WHERE id = 1" not in src, mod.__name__
