"""
Por que a camada esperta nao age — 30/07/2026.

MEDIDO EM PROD antes de escrever qualquer linha (10 dias de `cron_runs`):
  - 52 emails cairam no balde `renato`, e `smart_resolved` = 0 em TODOS
  - `inbox_smart.judge` (o LLM que decide) foi chamado 11 vezes em 14 dias
  - logo ~79% dos casos desistiram ANTES do LLM, no curto-circuito de
    `_smart_candidate_tasks` -- mas isso era invisivel
  - `smart_reason` era NULL em 51 dos 52 itens: o motivo ia so pro
    `smart_needs_you`, que alimenta o RESUMO DE WHATSAPP, e o gate de silencio
    por produtor (58e6ec1) rebaixou o inbox_smart pra push em 29/07 -- desde
    27/07 nenhum resumo dele chega ao Renato. O unico lugar onde o "por que"
    vivia deixou de ser lido.

Consequencia: `smart_resolved: 0` era indecidivel. Podia ser NATUREZA (recibo da
Anthropic e newsletter do Jusbrasil nao fecham task nenhuma -- e metade do balde
e isso) ou DEFEITO (o gate de candidatas nunca casando). Decidir o futuro da
Parte B sem distinguir os dois seria decidir no escuro.

Estes testes garantem que o motivo e o ESTAGIO da desistencia sobrevivem em
`cron_runs.result_json`, que e o que resta quando o canal de aviso e silencioso.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_inbox_smart_stage_telemetry.py -q
"""
import ast
import asyncio
import inspect
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import email_triage as et  # noqa: E402

HEADERS = {"subject": "Extratos 2025 - Empresas Inativas", "from": "Andressa <a@x.com>"}


def _run(**over):
    kw = dict(
        gmail_id="g1", headers=HEADERS, from_email="a@x.com", contact_id=None,
        body_text="segue o material", project_index=None, api_key="sk-test",
    )
    kw.update(over)
    return asyncio.run(et._smart_triage_renato(**kw))


# ---------------------------------------------------------------------------
# Origem do dado: cada saida sem resolucao carimba ONDE desistiu
# ---------------------------------------------------------------------------
def test_sem_candidata_carimba_estagio_e_nao_chama_llm(monkeypatch):
    """O caso de ~79% do volume. Antes era indistinguivel de 'o LLM recusou'."""
    monkeypatch.setattr(et, "_smart_candidate_tasks", lambda *a, **k: [])
    chamou = []
    async def _nunca(*a, **k):
        chamou.append(1)
        return {}
    monkeypatch.setattr(et, "_smart_judge_resolution", _nunca)

    out = _run()
    assert out["resolved"] is False
    assert out["record"]["stage"] == "no_candidate_task"
    assert not chamou, "o LLM nao deve ser chamado sem task candidata (custo)"


def test_llm_recusou_carimba_estagio_distinto(monkeypatch):
    """Chegou ao LLM e ele disse nao. E o caso que diz se a barra esta
    calibrada — e o unico em que faz sentido mexer no prompt/barra."""
    monkeypatch.setattr(et, "_smart_candidate_tasks",
                        lambda *a, **k: [{"id": 1, "titulo": "t", "project_id": 47}])
    async def _recusa(*a, **k):
        return {"resolved": False, "confidence": 0.4, "reason": "email nao cumpre a task"}
    monkeypatch.setattr(et, "_smart_judge_resolution", _recusa)

    out = _run()
    assert out["record"]["stage"] == "llm_declined"
    assert out["record"]["reason"] == "email nao cumpre a task"


def test_barra_de_confianca_tambem_cai_em_llm_declined(monkeypatch):
    """Resolvido mas abaixo de 0.85: recusa de CALIBRACAO, nao de conteudo —
    e tem que ficar visivel como tal, senao a barra nunca e revisada."""
    monkeypatch.setattr(et, "_smart_candidate_tasks",
                        lambda *a, **k: [{"id": 1, "titulo": "t", "project_id": 47}])
    async def _quase(*a, **k):
        return {"resolved": True, "confidence": 0.7, "task_id": 1,
                "action": "close", "reason": "provavel"}
    monkeypatch.setattr(et, "_smart_judge_resolution", _quase)

    out = _run()
    assert out["resolved"] is False
    assert out["record"]["stage"] == "llm_declined"


def test_remarcacao_sem_data_tem_estagio_proprio(monkeypatch):
    monkeypatch.setattr(et, "_smart_candidate_tasks",
                        lambda *a, **k: [{"id": 1, "titulo": "t", "project_id": 47}])
    async def _sem_data(*a, **k):
        return {"resolved": True, "confidence": 0.95, "task_id": 1,
                "action": "reschedule", "new_due_date": None, "reason": "adiar"}
    monkeypatch.setattr(et, "_smart_judge_resolution", _sem_data)

    out = _run()
    assert out["resolved"] is False
    assert out["record"]["stage"] == "reschedule_no_date"


def test_sem_api_key_tem_estagio_proprio(monkeypatch):
    monkeypatch.setattr(et, "_smart_candidate_tasks",
                        lambda *a, **k: [{"id": 1, "titulo": "t", "project_id": 47}])
    out = _run(api_key=None)
    assert out["record"]["stage"] == "no_api_key"


def test_resolucao_legitima_segue_agindo(monkeypatch):
    """Guarda contra eu ter estreitado o caminho de sucesso ao instrumentar."""
    monkeypatch.setattr(et, "_smart_candidate_tasks",
                        lambda *a, **k: [{"id": 9, "titulo": "Extrato", "project_id": 47}])
    async def _resolve(*a, **k):
        return {"resolved": True, "confidence": 0.93, "task_id": 9,
                "action": "close", "reason": "material entregue"}
    monkeypatch.setattr(et, "_smart_judge_resolution", _resolve)

    out = _run()
    assert out["resolved"] is True
    assert out["record"]["task_id"] == 9
    assert out["record"]["action_label"] == "fechei"


def test_todo_estagio_vem_acompanhado_de_motivo(monkeypatch):
    """Estagio sem motivo legivel nao resolve nada pra quem le a telemetria."""
    monkeypatch.setattr(et, "_smart_candidate_tasks", lambda *a, **k: [])
    rec = _run()["record"]
    assert rec["stage"] and rec["reason"]


# ---------------------------------------------------------------------------
# Consumidor: o motivo tem que chegar em cron_runs.result_json
# ---------------------------------------------------------------------------
def test_motivo_chega_na_telemetria_no_caminho_needs_you():
    """O defeito original: `entry['smart_reason']` so era gravado no caminho
    `auto_sender_no_escalate`. No caminho needs_you — 51 dos 52 itens medidos —
    o motivo era descartado.

    Prova por PROXIMIDADE no fonte em vez de rodar o pipeline: rodar
    `apply_triage_to_inbox` exige Gmail, Neon e LLM, e o alvo aqui e uma unica
    atribuicao dentro de um branch. Exigir que a gravacao venha DEPOIS do
    incremento de `smart_needs_you` amarra a asserçao ao branch certo — achar a
    string em qualquer lugar da funcao nao provaria nada (ela ja existia no branch
    do auto_sender e o defeito persistia)."""
    linhas = inspect.getsource(et.apply_triage_to_inbox).splitlines()
    i_needs = next((i for i, l in enumerate(linhas)
                    if 'stats["smart_needs_you"] += 1' in l), None)
    assert i_needs is not None, "o branch needs_you desapareceu — teste obsoleto"

    janela = linhas[i_needs:i_needs + 12]
    assert any('entry["smart_reason"]' in l for l in janela), (
        "apply_triage_to_inbox nao grava entry['smart_reason'] junto ao branch "
        "needs_you: o motivo volta a morrer em memoria (o resumo de WhatsApp esta "
        "silenciado desde 27/07) e `smart_resolved: 0` fica indecidivel de novo"
    )


def test_agregado_por_estagio_existe():
    """Contador por estagio na run — sem ele, ler o denominador exige abrir os 52
    itens um por um (a licao de feedback_regua_cobertura_parcial)."""
    src = inspect.getsource(et.apply_triage_to_inbox)
    assert "smart_declined_by_stage" in src
    assert 'entry["smart_stage"]' in src


def test_estagios_do_produtor_e_do_consumidor_sao_os_mesmos():
    """Se um lado ganhar estagio novo e o outro nao, o agregado passa a mentir
    silenciosamente (viraria 'unknown' sem ninguem notar)."""
    produtor = set()
    for node in ast.walk(ast.parse(inspect.getsource(et._smart_triage_renato))):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].slice, ast.Constant)
                and node.targets[0].slice.value == "stage"
                and isinstance(node.value, ast.Constant)):
            produtor.add(node.value.value)
    assert produtor == {"no_candidate_task", "no_api_key",
                        "reschedule_no_date", "llm_declined"}, produtor
