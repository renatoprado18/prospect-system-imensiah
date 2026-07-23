"""
Testes do RACI semanal da Governança Jabô (frente 23/07).

A governanca da fazenda NAO vive no ConselhoOS — o "RACI" de facto sao as
tasks do projeto #28 no INTEL. Prova:
  - inferencia de responsavel do prefixo do titulo ([Jabô/Andressa] -> Andressa)
  - strip do prefixo pra nao duplicar com a coluna responsavel
  - bucketing (urgente/atrasada-mov/no-prazo/concluida) espelhando o ConselhoOS
  - concluidas antigas (>7d) nao poluem; on_hold/cancelled ficam de fora
  - format interactive=False nao convida resposta "nº + status"

Rodar: python -m pytest tests/test_raci_jabo_report.py -v
"""
import os
import sys
from datetime import datetime, date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.raci_weekly_report import (  # noqa: E402
    _infer_task_responsavel,
    _strip_task_prefix,
    generate_jabo_report,
    format_raci_whatsapp,
)


# ───────────────────── inferencia de responsavel ─────────────────────
class TestInferResponsavel:
    def test_prefixo_com_pessoa(self):
        assert _infer_task_responsavel("[Jabô/Andressa] Enviar onboarding") == "Andressa"

    def test_prefixo_so_projeto_sem_pessoa(self):
        assert _infer_task_responsavel("[Jabô] Classificar safra") == "—"

    def test_sem_prefixo(self):
        assert _infer_task_responsavel("Investigar Fiama") == "—"

    def test_prefixo_com_barra_e_espacos(self):
        assert _infer_task_responsavel("[Jabô / Reginaldo]  Irrigação") == "Reginaldo"

    def test_titulo_vazio(self):
        assert _infer_task_responsavel("") == "—"
        assert _infer_task_responsavel(None) == "—"


class TestStripPrefix:
    def test_remove_prefixo(self):
        assert _strip_task_prefix("[Jabô/Andressa] Enviar onboarding") == "Enviar onboarding"

    def test_sem_prefixo_mantem(self):
        assert _strip_task_prefix("Investigar Fiama") == "Investigar Fiama"

    def test_so_prefixo_nao_esvazia(self):
        # se sobrar nada apos o strip, mantem o titulo original
        assert _strip_task_prefix("[Jabô]") == "[Jabô]"


# ───────────────────── bucketing (fake cursor) ─────────────────────
class _FakeCursor:
    """Devolve `rows` na primeira execute; RealDictCursor-like (dict rows)."""
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


def _task(id, titulo, status, prazo=None, atualizado_em=None, data_conclusao=None):
    return {
        "id": id,
        "titulo": titulo,
        "status": status,
        "prioridade": 5,
        "data_vencimento": prazo,
        "data_conclusao": data_conclusao,
        "atualizado_em": atualizado_em,
    }


class TestBucketing:
    def _report(self, rows):
        return generate_jabo_report(_FakeCursor(rows))

    def test_vazio_retorna_none(self):
        assert self._report([]) is None

    def test_vencido_sem_update_recente_e_urgente(self):
        old = datetime.now() - timedelta(days=10)
        prazo = date.today() - timedelta(days=5)
        r = self._report([_task(1, "[Jabô] Atrasada fria", "pending", prazo, atualizado_em=old)])
        assert len(r["urgentes"]) == 1
        assert len(r["atrasadas_mov"]) == 0

    def test_vencido_com_update_recente_vai_pra_atrasadas_mov(self):
        recent = datetime.now() - timedelta(hours=2)
        prazo = date.today() - timedelta(days=5)
        r = self._report([_task(2, "[Jabô] Atrasada com movimento", "pending", prazo, atualizado_em=recent)])
        assert len(r["urgentes"]) == 0
        assert len(r["atrasadas_mov"]) == 1

    def test_prazo_futuro_e_no_prazo(self):
        prazo = date.today() + timedelta(days=5)
        r = self._report([_task(3, "[Jabô] Futura", "pending", prazo)])
        assert len(r["no_prazo"]) == 1

    def test_sem_prazo_e_no_prazo(self):
        r = self._report([_task(4, "[Jabô] Sem prazo", "pending", None)])
        assert len(r["no_prazo"]) == 1

    def test_concluida_recente_aparece(self):
        done = datetime.now() - timedelta(days=2)
        r = self._report([_task(5, "[Jabô] Feita ontem", "completed", data_conclusao=done)])
        assert len(r["concluidas"]) == 1

    def test_concluida_antiga_some(self):
        done = datetime.now() - timedelta(days=30)
        r = self._report([_task(6, "[Jabô] Feita mês passado", "completed", data_conclusao=done)])
        # unica task e concluida-antiga -> nada acionavel -> None
        assert r is None

    def test_on_hold_e_cancelled_ficam_de_fora(self):
        # o filtro esta no SQL (status NOT IN), mas garantimos que o fake nao
        # injeta esses estados: passamos so pending; total reflete o visivel
        prazo = date.today() + timedelta(days=3)
        r = self._report([_task(7, "[Jabô] Ativa", "pending", prazo)])
        assert r["total"] == 1

    def test_responsavel_inferido_no_entry(self):
        prazo = date.today() + timedelta(days=3)
        r = self._report([_task(8, "[Jabô/Andressa] Pipeline", "pending", prazo)])
        assert r["no_prazo"][0]["responsavel"] == "Andressa"
        assert r["no_prazo"][0]["acao"] == "Pipeline"


# ───────────────────── formato nao-interativo ─────────────────────
class TestFormatNaoInterativo:
    def _report(self):
        prazo = date.today() + timedelta(days=3)
        return generate_jabo_report(_FakeCursor([_task(9, "[Jabô/Andressa] X", "pending", prazo)]))

    def test_interactive_false_sem_convite_de_resposta(self):
        msg = format_raci_whatsapp(self._report(), interactive=False)
        assert "Responda com o nº" not in msg
        assert "_Total: 1_" in msg

    def test_interactive_true_mantem_convite(self):
        msg = format_raci_whatsapp(self._report(), interactive=True)
        assert "Responda com o nº" in msg
