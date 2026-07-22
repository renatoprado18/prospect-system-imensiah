"""
Testes do guardrail semantico do RACI smart-updates (fix 22/07).

Reproduz o FP observado ao vivo: mensagens de CORTESIA pos-reuniao fecharam o item
"Revisar o questionario dos socios". Prova que agora:
  - cortesia -> NAO gera/deixa passar proposta (FP)
  - evidencia real do entregavel -> passa (TP)
  - item de julgamento so vira PROPOR (media), nunca auto-close alta

Rodar: python -m pytest tests/test_raci_smart_updates.py -v
"""
import os
import sys

# app/ precisa estar no path (modulo faz `from services import llm`)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.raci_smart_updates import (  # noqa: E402
    _is_courtesy_only,
    _is_judgment_item,
    _references_deliverable,
    _apply_guardrails,
    _judgment_autoclose_enabled,
)

# UUID ficticio do item Vallen "Revisar o questionario dos socios"
ITEM_REVISAO = "11111111-1111-1111-1111-111111111111"
ACAO_REVISAO = "Revisar o questionario dos socios (Conselho + Diretora Executiva)"
ITEM_OPERACIONAL = "22222222-2222-2222-2222-222222222222"
ACAO_OPERACIONAL = "Ativar o Kommo CRM para o time comercial"

ITEMS = {ITEM_REVISAO: ACAO_REVISAO, ITEM_OPERACIONAL: ACAO_OPERACIONAL}


# ────────────────────────── cortesia (o FP real) ──────────────────────────
class TestCourtesyOnly:
    def test_clarice_fico_feliz_teras_cuidado(self):
        assert _is_courtesy_only("Fico muito feliz, sei que teras todo o cuidado com isso") is True

    def test_grazielle_feliz_colaborar(self):
        assert _is_courtesy_only("Fico feliz em colaborar com voces!") is True

    def test_sandra_agradece_feedbacks_reuniao(self):
        assert _is_courtesy_only("Agradeco muito os feedbacks da reuniao de hoje, foi excelente") is True

    def test_obrigado_puro(self):
        assert _is_courtesy_only("Muito obrigado a todos, grande abraco") is True

    def test_cortesia_com_entregavel_nao_e_cortesia_pura(self):
        # gratidao MAS fala do documento -> substantivo presente -> nao e cortesia-pura
        assert _is_courtesy_only("Obrigado! Ja revisei o questionario e esta aprovado") is False

    def test_mensagem_operacional_nao_e_cortesia(self):
        assert _is_courtesy_only("Kommo CRM ativado hoje, ja subimos os contatos") is False

    def test_texto_com_prefixo_de_sender(self):
        # o batch injeta "[Nome em dd/mm HH:MM]\n" — nao pode virar substantivo (data)
        assert _is_courtesy_only("[Sandra em 22/07 14:30]\nFico feliz em colaborar") is True


# ────────────────────────── classificacao de item ──────────────────────────
class TestJudgmentItem:
    def test_revisar_e_julgamento(self):
        assert _is_judgment_item(ACAO_REVISAO) is True

    def test_aprovar_e_julgamento(self):
        assert _is_judgment_item("Aprovar a minuta do contrato") is True

    def test_kommo_nao_e_julgamento(self):
        assert _is_judgment_item(ACAO_OPERACIONAL) is False


class TestReferencesDeliverable:
    def test_cortesia_nao_referencia(self):
        assert _references_deliverable("fico feliz em colaborar", "", ACAO_REVISAO) is False

    def test_menciona_questionario_referencia(self):
        assert _references_deliverable("terminei de revisar o questionario", "", ACAO_REVISAO) is True

    def test_evidencia_carrega_a_referencia(self):
        assert _references_deliverable("segue", "documento questionario revisado", ACAO_REVISAO) is True


# ────────────────────────── guardrail integrado ──────────────────────────
def _completion_proposal(item_id, conf="alta", evid=""):
    return {
        "item_id": item_id, "action": "complete", "new_status": "concluido",
        "new_prazo": None, "notes": None, "evidencia": evid, "confianca": conf,
    }


class TestApplyGuardrails:
    def test_fp_cortesia_dropa_tudo(self):
        """FP real: cortesia gerou proposta de fechar item de revisao -> descartada."""
        props = [_completion_proposal(ITEM_REVISAO, "alta", evid="fico feliz em colaborar")]
        out = _apply_guardrails(props, ITEMS, "Fico feliz em colaborar com voces!")
        assert out == []

    def test_julgamento_sem_ref_ao_entregavel_dropa(self):
        """Msg nao-cortesia mas que NAO fala do documento nao fecha item de revisao."""
        props = [_completion_proposal(ITEM_REVISAO, "alta", evid="a reuniao foi otima")]
        out = _apply_guardrails(props, ITEMS, "A reuniao de conselho foi muito produtiva e alinhada")
        assert out == []

    def test_tp_julgamento_com_ref_vira_proposta_media(self):
        """TP: msg fala do questionario -> NAO some, mas rebaixa alta->media (human-in-loop)."""
        text = "Pessoal, terminei de revisar o questionario dos socios, esta ok pra seguir"
        props = [_completion_proposal(ITEM_REVISAO, "alta", evid="terminei de revisar o questionario")]
        out = _apply_guardrails(props, ITEMS, text)
        assert len(out) == 1
        assert out[0]["confianca"] == "media"  # propor, nao executar
        assert out[0]["new_status"] == "concluido"

    def test_tp_operacional_alta_preservado(self):
        """Item operacional com evidencia clara mantem auto-close alta (nao regride)."""
        text = "Kommo CRM ativado hoje, ja subimos todos os contatos do comercial"
        props = [_completion_proposal(ITEM_OPERACIONAL, "alta", evid="Kommo CRM ativado hoje")]
        out = _apply_guardrails(props, ITEMS, text)
        assert len(out) == 1
        assert out[0]["confianca"] == "alta"

    def test_killswitch_reabilita_autoclose_julgamento(self, monkeypatch):
        monkeypatch.setenv("RACI_JUDGMENT_AUTOCLOSE", "1")
        assert _judgment_autoclose_enabled() is True
        text = "revisei o questionario, aprovado"
        props = [_completion_proposal(ITEM_REVISAO, "alta", evid="revisei o questionario aprovado")]
        out = _apply_guardrails(props, ITEMS, text)
        assert len(out) == 1
        assert out[0]["confianca"] == "alta"  # kill-switch ON => volta a auto-fechar

    def test_add_note_em_item_julgamento_nao_e_afetado(self):
        """Guardrail so mira CONCLUSAO; add_note/progresso passa normal."""
        p = {"item_id": ITEM_REVISAO, "action": "add_note", "new_status": None,
             "new_prazo": None, "notes": "andamento", "evidencia": "seguimos revisando",
             "confianca": "media"}
        out = _apply_guardrails([p], ITEMS, "Seguimos revisando o material, sem novidade")
        assert len(out) == 1
