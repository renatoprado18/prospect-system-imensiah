"""
Testes do apply do RACI human-in-loop (fix 22/07, reescrito 27/10).

Contexto do bug original: a metade "apply" do human-in-loop estava aparentemente
MORTA — a CoS recebia sempre "item nao encontrado no RACI" ao aplicar proposta
shadow. A investigacao provou que o item_id RESOLVE (todas as 72 propostas em
prod casam com raci_itens). O defeito real: apply_proposal colapsava QUATRO
desfechos (env ausente, item inexistente, no-op, excecao de DB) num unico
`return None`, e apply_group_proposal traduzia esse None SEMPRE como "item nao
encontrado" — diagnostico falso que mandava quem revisa cacar um bug de
mapeamento inexistente.

Estes testes provam o contrato de DESFECHO:
  - strict=True distingue os 4 casos via erros tipados;
  - strict=False (auto-apply do webhook/batch) mantem o contrato historico (None).

⚠️ O PONTO DE CORTE MUDOU EM 27/10/26, e o motivo importa para quem ler isto
depois. Antes estes testes dublavam `psycopg2.connect` — ou seja, assumiam que
`apply_proposal` abre a propria conexao e sabe em que banco escreve. Era essa
premissa que estava errada no sistema: a escolha do banco morava espalhada em
seis pontos, e por isso a fonte unica de 06/10 passou cinco meses pela metade
sem ninguem ver. Agora quem decide o banco e' o `raci_store`, e estes testes
dublam ELE. O roteamento (qual banco, sob que chave) tem teste proprio em
`test_raci_store.py` — separar as duas perguntas e' o que permite responder
"o desfecho esta certo?" sem responder junto "o destino esta certo?".

Rodar: python -m pytest tests/test_raci_apply_strict.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import raci_store  # noqa: E402
from services import raci_smart_updates as rsu  # noqa: E402
from services.raci_smart_updates import (  # noqa: E402
    apply_proposal,
    RaciConfigError,
    RaciItemNotFound,
    RaciNoChange,
    RaciApplyError,
)

# O id de ConselhoOS continua aqui de proposito: e' o formato que as 92
# propostas `pending_review` carregam, e o apply tem de seguir aceitando-o.
ITEM_ID = "6e37a6ef-78f7-4f3a-a092-9a77ab698847"

SCOPE = raci_store.RaciScope(
    target=raci_store.TARGET_INTEL, project_id=24,
    empresa_uuid="9c246a3a-3e7e-4472-9854-ce83f6e22e14", nome="Vallen Clinic",
)


class _StoreDouble:
    """Dublê do `raci_store`: um item e o registro das escritas tentadas."""

    def __init__(self, item=None, scope=SCOPE, scope_erro=None, update_erro=None):
        self.item = item
        self.scope = scope
        self.scope_erro = scope_erro
        self.update_erro = update_erro
        self.updates = []           # [(ref, status, prazo, nota)]

    def install(self, monkeypatch):
        def resolve_scope(_chave):
            if self.scope_erro:
                raise self.scope_erro
            return self.scope

        def get_item(_scope, _ref):
            return dict(self.item) if self.item else None

        def update_item(_scope, ref, *, status=None, prazo=None, append_note=None):
            if self.update_erro:
                raise self.update_erro
            self.updates.append((str(ref), status, prazo, append_note))
            # Espelha o contrato real: False quando nao havia o que mudar.
            return bool(status or prazo or append_note)

        monkeypatch.setattr(raci_store, "resolve_scope", resolve_scope)
        monkeypatch.setattr(raci_store, "get_item", get_item)
        monkeypatch.setattr(raci_store, "update_item", update_item)
        monkeypatch.setattr(raci_store, "undo_hint_update",
                            lambda s, r, old: f"-- undo {r} -> {old}")
        # O audit log nao deve tocar DB real no teste.
        import services.agent_actions as aa
        monkeypatch.setattr(aa, "log_action", lambda *a, **k: None)
        return self

    @property
    def escreveu(self):
        return bool(self.updates)


def _prop(**over):
    base = {"item_id": ITEM_ID, "action": "update_status", "new_status": "em_andamento",
            "new_prazo": None, "notes": None, "evidencia": "ev", "confianca": "alta"}
    base.update(over)
    return base


def _item(status, acao="Tabela Camila"):
    return {"id": "47", "acao": acao, "status": status, "prazo": None, "notas": ""}


# ───────────────────────── strict=True (human-in-loop) ─────────────────────────

def test_strict_config_error_quando_escopo_nao_resolve(monkeypatch):
    """Escopo irresolvivel -> RaciConfigError (era None -> 'item nao encontrado').

    O caso cresceu em 27/10: alem da env do ConselhoOS vazia, agora cobre
    "o projeto nao tem vinculo". Os dois sao falha de AMBIENTE, nao da proposta,
    e por isso nao marcam a proposta como `apply_error` — ela segue aplicavel.
    """
    _StoreDouble(scope_erro=raci_store.RaciScopeError("sem vinculo")).install(monkeypatch)
    with pytest.raises(RaciConfigError):
        apply_proposal(_prop(), "emp-1", strict=True)


def test_strict_item_not_found(monkeypatch):
    """Item sem par do lado do target -> RaciItemNotFound."""
    _StoreDouble(item=None).install(monkeypatch)
    with pytest.raises(RaciItemNotFound) as ei:
        apply_proposal(_prop(), "emp-1", strict=True)
    assert ITEM_ID in str(ei.value)


def test_strict_no_change(monkeypatch):
    """Item existe mas ja esta no estado alvo e sem notes/prazo -> RaciNoChange (no-op)."""
    _StoreDouble(item=_item("em_andamento")).install(monkeypatch)
    with pytest.raises(RaciNoChange):
        apply_proposal(_prop(new_status="em_andamento", notes=None, new_prazo=None),
                       "emp-1", strict=True)


def test_strict_success_muda_status(monkeypatch):
    """Item resolve + mudanca real -> escreve e retorna dict (o caso #16)."""
    store = _StoreDouble(item=_item("pendente")).install(monkeypatch)
    out = apply_proposal(_prop(new_status="em_andamento", notes="repasse aceito"),
                         "emp-1", strict=True)
    assert out is not None
    assert out["old_status"] == "pendente"
    assert out["new_status"] == "em_andamento"
    assert store.escreveu
    ref, status, _prazo, nota = store.updates[0]
    assert status == "em_andamento"
    # A nota CONCATENA com carimbo de dia — nunca substitui o texto curado.
    assert nota and "repasse aceito" in nota and nota.startswith("\n[")


def test_strict_db_exception_vira_apply_error(monkeypatch):
    """Excecao inesperada -> RaciApplyError (nao mais 'item nao encontrado')."""
    _StoreDouble(item=_item("pendente"),
                 update_erro=RuntimeError("connection reset")).install(monkeypatch)
    with pytest.raises(RaciApplyError):
        apply_proposal(_prop(), "emp-1", strict=True)


def test_item_id_da_fila_chega_ao_store_sem_ser_mutilado(monkeypatch):
    """O UUID da fila viaja INTEIRO ate o store, que e' quem traduz.

    Guarda contra a tentacao de "limpar" o id no caminho: as 92 propostas
    pendentes carregam UUID do ConselhoOS e e' o `resolve_item_ref` que acha o
    par no INTEL. Qualquer normalizacao no meio quebraria a traducao.
    """
    store = _StoreDouble(item=_item("pendente")).install(monkeypatch)
    apply_proposal(_prop(new_status="concluido"), "emp-1")
    assert store.updates[0][0] == ITEM_ID


# ───────────────────────── strict=False (auto-apply backcompat) ─────────────────────────

def test_nonstrict_escopo_irresolvivel_retorna_none(monkeypatch):
    _StoreDouble(scope_erro=raci_store.RaciScopeError("env vazia")).install(monkeypatch)
    assert apply_proposal(_prop(), "emp-1") is None


def test_nonstrict_item_not_found_retorna_none(monkeypatch):
    _StoreDouble(item=None).install(monkeypatch)
    assert apply_proposal(_prop(), "emp-1") is None


def test_nonstrict_no_change_retorna_none(monkeypatch):
    _StoreDouble(item=_item("em_andamento")).install(monkeypatch)
    assert apply_proposal(_prop(new_status="em_andamento", notes=None), "emp-1") is None


def test_nonstrict_db_exception_retorna_none(monkeypatch):
    _StoreDouble(item=_item("pendente"),
                 update_erro=RuntimeError("connection reset")).install(monkeypatch)
    assert apply_proposal(_prop(), "emp-1") is None


# ───────────────── trava de rebaixamento (29/07) ─────────────────
#
# Contexto: o apply so comparava `new_status != status_atual`, entao QUALQUER
# status diferente passava — inclusive pra tras. Como o webhook auto-aplica o
# que a IA marca como confianca 'alta', uma mensagem no grupo podia reabrir
# sozinha um item que o Renato ja tinha dado por concluido.
#
# Medido no Neon prod antes do fix: 17 applies, 1 rebaixamento (em_andamento ->
# pendente, 08/06), nenhum a partir de `concluido`. Risco latente, nao incendio.

from services.raci_smart_updates import (  # noqa: E402
    RaciDowngradeBlocked,
    is_downgrade,
)


@pytest.mark.parametrize("velho,novo", [
    ("concluido", "pendente"),
    ("concluido", "em_andamento"),
    ("concluido", "atrasado"),
    ("em_andamento", "pendente"),
])
def test_is_downgrade_pega_o_retrocesso(velho, novo):
    assert is_downgrade(velho, novo) is True


@pytest.mark.parametrize("velho,novo", [
    ("pendente", "em_andamento"),
    ("pendente", "concluido"),
    ("em_andamento", "concluido"),
    ("concluido", "concluido"),
    ("pendente", "atrasado"),      # reclassificacao de prazo, nao retrocesso
    ("atrasado", "pendente"),      # idem, na volta
])
def test_is_downgrade_nao_barra_progresso_nem_empate(velho, novo):
    assert is_downgrade(velho, novo) is False


@pytest.mark.parametrize("velho,novo", [
    ("cancelado", "pendente"),     # status fora da escala dos dois lados
    (None, "concluido"),
    ("concluido", None),
])
def test_status_fora_da_escala_nao_vira_palpite(velho, novo):
    """Inventar ordem pra valor desconhecido bloquearia update legitimo."""
    assert is_downgrade(velho, novo) is False


def test_auto_apply_nao_reabre_item_concluido(monkeypatch):
    """O caso que motivou a trava: webhook nao desfaz conclusao sozinho."""
    store = _StoreDouble(item=_item("concluido", "Revisar contrato")).install(monkeypatch)
    r = apply_proposal(_prop(new_status="pendente"), "emp-1")
    assert r["blocked"] == "downgrade"
    assert r["old_status"] == "concluido" and r["new_status"] == "pendente"
    assert not store.escreveu


def test_bloqueado_nao_e_none_mudo(monkeypatch):
    """Devolver None faria a mensagem sumir: o chamador precisa conseguir
    transformar a proposta em revisao humana."""
    _StoreDouble(item=_item("concluido", "Revisar contrato")).install(monkeypatch)
    r = apply_proposal(_prop(new_status="em_andamento"), "emp-1")
    assert r is not None and r.get("acao") == "Revisar contrato"


def test_strict_levanta_erro_tipado(monkeypatch):
    _StoreDouble(item=_item("concluido")).install(monkeypatch)
    with pytest.raises(RaciDowngradeBlocked):
        apply_proposal(_prop(new_status="pendente"), "emp-1", strict=True)


def test_humano_que_aprovou_pode_reabrir(monkeypatch):
    """apply_group_proposal passa allow_downgrade=True: a trava e contra o
    automatico, nao contra o Renato desfazer um fechamento errado."""
    store = _StoreDouble(item=_item("concluido")).install(monkeypatch)
    r = apply_proposal(_prop(new_status="pendente"), "emp-1", allow_downgrade=True)
    assert r.get("blocked") is None
    assert store.escreveu


def test_progresso_normal_segue_aplicando(monkeypatch):
    """A trava nao pode custar o caso comum (o que o path faz 16 de 17 vezes)."""
    store = _StoreDouble(item=_item("em_andamento")).install(monkeypatch)
    r = apply_proposal(_prop(new_status="concluido"), "emp-1")
    assert r.get("blocked") is None
    assert store.escreveu
