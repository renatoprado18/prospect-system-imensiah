"""
Testes de resiliencia do sweep shadow de RACI (fix 22/07).

Reproduz o stall que segurou ~5.548 msgs: uma mensagem que levantava excecao NAO
era marcada como processada e voltava a ocupar as vagas do LIMIT em todo tick
(livelock). Prova que agora:
  - msg poison (propose/store falham) -> pula + AVANCA (marca) + errors++, sem abortar;
  - jid que nao resolve deterministicamente -> msgs viram sem-empresa e avancam;
  - infra global caida -> aborta o run limpo, SEM marcar (retry no proximo tick).

Rodar: python -m pytest tests/test_raci_group_shadow.py -v
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.raci_group_shadow import _run_sweep, TransientInfraError  # noqa: E402


def _msgs(*specs):
    """specs: (id, jid, sender, content)."""
    return [{"id": i, "group_jid": j, "sender_name": s, "content": c} for (i, j, s, c) in specs]


class _Harness:
    """Fakes injetaveis pro _run_sweep."""
    def __init__(self, *, resolve=None, propose_result=None):
        self.marked = []               # ids marcados como processados
        self.stored = []               # propostas gravadas
        self._resolve = resolve or (lambda jid: ("emp-1", "Vallen"))
        self._propose_result = propose_result if propose_result is not None else []

    def mark_processed(self, mid):
        self.marked.append(mid)

    def fetch_acoes(self, ids):
        return {i: f"acao {i}" for i in ids}

    def store_proposal(self, mid, jid, empresa_id, empresa_nome, sender, p, acoes):
        row = {"id": len(self.stored) + 1, "mid": mid, **p}
        self.stored.append(row)
        return row

    async def propose(self, content, empresa_id):
        if callable(self._propose_result):
            return self._propose_result(content, empresa_id)
        return list(self._propose_result)

    def run(self, msgs):
        return asyncio.run(_run_sweep(
            msgs,
            resolve_empresa=self._resolve,
            propose=self.propose,
            fetch_acoes=self.fetch_acoes,
            store_proposal=self.store_proposal,
            mark_processed=self.mark_processed,
        ))


def test_poison_message_nao_derruba_o_batch():
    """A msg #2 explode no propose; as outras 2 seguem e TODAS avancam (o stall)."""
    def propose_result(content, emp):
        if "BOOM" in content:
            raise RuntimeError("pessoas Vallen: sender nao resolve")
        return [{"item_id": "aaa", "action": "complete", "new_status": "concluido",
                 "confianca": "alta", "evidencia": "ok", "notes": None, "new_prazo": None}]

    h = _Harness(propose_result=propose_result)
    out, props = h.run(_msgs(
        (1, "g@us", "Ana", "reporte normal um"),
        (2, "g@us", "Bruno", "conteudo BOOM que explode"),
        (3, "g@us", "Célia", "reporte normal dois"),
    ))
    assert out["scanned"] == 3
    assert out["errors"] == 1
    # progresso monotonico: as 3 marcadas (inclusive a poison), nada re-trava a janela
    assert sorted(h.marked) == [1, 2, 3]
    assert out["processed_msgs"] == 3
    assert out["proposals"] == 2  # msg 1 e 3 geraram proposta


def test_store_proposal_falho_e_poison_mas_avanca():
    """Se o INSERT da proposta falha, a msg vira poison: pula + avanca."""
    h = _Harness(propose_result=[{"item_id": "aaa", "action": "add_note",
                                  "confianca": "media", "evidencia": "x",
                                  "notes": "n", "new_status": None, "new_prazo": None}])

    def boom_store(*a, **k):
        raise RuntimeError("INSERT raci_group_proposals falhou")
    h.store_proposal = boom_store

    out, props = h.run(_msgs((1, "g@us", "Ana", "mensagem que gera proposta")))
    assert out["errors"] == 1
    assert h.marked == [1]          # avancou mesmo com o INSERT quebrado
    assert out["processed_msgs"] == 1


def test_jid_que_nao_resolve_deterministico_avanca_sem_erro():
    """resolve_empresa levanta erro nao-infra -> jid vira sem-empresa, msgs avancam."""
    def resolve(jid):
        raise ValueError("mapping quebrado pra esse jid")
    h = _Harness(resolve=resolve)
    out, props = h.run(_msgs(
        (1, "bad@us", "Ana", "mensagem um"),
        (2, "bad@us", "Bruno", "mensagem dois"),
    ))
    # tratadas como sem-empresa: marcadas, sem contar como erro, sem propostas
    assert sorted(h.marked) == [1, 2]
    assert out["processed_msgs"] == 2
    assert out["proposals"] == 0
    assert out["errors"] == 0
    assert "aborted_transient" not in out


def test_infra_transiente_aborta_run_sem_queimar_msgs():
    """DB/CONSELHOOS caido -> aborta o run; msgs NAO marcadas (retry no proximo tick)."""
    calls = {"n": 0}

    def resolve(jid):
        calls["n"] += 1
        raise TransientInfraError("CONSELHOOS connection refused")
    h = _Harness(resolve=resolve)
    out, props = h.run(_msgs(
        (1, "g@us", "Ana", "mensagem um"),
        (2, "g@us", "Bruno", "mensagem dois"),
    ))
    assert out.get("aborted_transient") is True
    assert h.marked == []            # NADA marcado -> backlog preservado
    assert out["processed_msgs"] == 0
    assert calls["n"] == 1           # abortou na 1a falha, nao varreu o resto


def test_happy_path_resolve_e_gera_propostas():
    h = _Harness(propose_result=[{"item_id": "aaa", "action": "complete",
                                  "new_status": "concluido", "confianca": "alta",
                                  "evidencia": "kommo ativado", "notes": None, "new_prazo": None}])
    out, props = h.run(_msgs(
        (1, "g@us", "Ana", "Kommo CRM ativado hoje"),
        (2, "g@us", "Bruno", "Segue o relatorio anexo"),
    ))
    assert out["scanned"] == 2
    assert out["errors"] == 0
    assert out["proposals"] == 2
    assert sorted(h.marked) == [1, 2]
