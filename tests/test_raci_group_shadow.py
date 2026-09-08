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
        self.propose_calls = 0         # quantas vezes a IA foi chamada
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
        self.propose_calls += 1
        if callable(self._propose_result):
            return self._propose_result(content, empresa_id)
        return list(self._propose_result)

    def run(self, msgs, **kw):
        return asyncio.run(_run_sweep(
            msgs,
            resolve_empresa=self._resolve,
            propose=self.propose,
            fetch_acoes=self.fetch_acoes,
            store_proposal=self.store_proposal,
            mark_processed=self.mark_processed,
            **kw,
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


# --- Cap de idade da EVIDENCIA (fix 22/07) ----------------------------------
# Full drain de backlog nao pode inundar a fila de review da CoS com msg velha
# (decisao ja absorvida na ponte manual). Msg-evidencia > threshold: drena
# (marca processada) mas NAO gera pending_review nem chama a IA.

from datetime import datetime, timedelta, timezone  # noqa: E402

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _now_fn():
    return _NOW


def _tsmsgs(*specs):
    """specs: (id, jid, sender, content, timestamp). timestamp NAIVE (como o DB)."""
    return [{"id": i, "group_jid": j, "sender_name": s, "content": c, "timestamp": ts}
            for (i, j, s, c, ts) in specs]


def test_evidencia_velha_suprimida_mas_avanca():
    """Msg de 40 dias atras (> cap 21): drena sem gerar proposta nem chamar IA."""
    prop = [{"item_id": "aaa", "action": "complete", "new_status": "concluido",
             "confianca": "alta", "evidencia": "x", "notes": None, "new_prazo": None}]
    h = _Harness(propose_result=prop)
    velha = (_NOW - timedelta(days=40)).replace(tzinfo=None)  # naive, como o DB
    out, props = h.run(_tsmsgs((1, "g@us", "Ana", "reporte antigo ja absorvido", velha)),
                       max_age_days=21, now_fn=_now_fn)
    assert out["stale_skipped"] == 1
    assert out["proposals"] == 0
    assert h.stored == []
    assert h.propose_calls == 0        # nem chamou a IA (barato)
    assert h.marked == [1]             # drenou: nao re-trava a janela
    assert out["processed_msgs"] == 1


def test_evidencia_fresca_passa_normal():
    """Msg de hoje (< cap): gera proposta como antes — nao regride o caso normal."""
    prop = [{"item_id": "aaa", "action": "complete", "new_status": "concluido",
             "confianca": "alta", "evidencia": "kommo ativado", "notes": None, "new_prazo": None}]
    h = _Harness(propose_result=prop)
    fresca = (_NOW - timedelta(days=2)).replace(tzinfo=None)
    out, props = h.run(_tsmsgs((1, "g@us", "Ana", "Kommo ativado hoje", fresca)),
                       max_age_days=21, now_fn=_now_fn)
    assert out["stale_skipped"] == 0
    assert out["proposals"] == 1
    assert h.propose_calls == 1
    assert h.marked == [1]


def test_borda_do_cap_msg_no_limite_passa():
    """Msg exatamente dentro do cap (20 dias, cap 21) ainda gera proposta."""
    prop = [{"item_id": "aaa", "action": "add_note", "confianca": "media",
             "evidencia": "y", "notes": "n", "new_status": None, "new_prazo": None}]
    h = _Harness(propose_result=prop)
    borda = (_NOW - timedelta(days=20)).replace(tzinfo=None)
    out, props = h.run(_tsmsgs((1, "g@us", "Ana", "reporte ainda recente", borda)),
                       max_age_days=21, now_fn=_now_fn)
    assert out["stale_skipped"] == 0
    assert out["proposals"] == 1


def test_cap_desligado_nao_filtra_msg_velha():
    """max_age_days=None (cap off): msg velha volta a gerar proposta (comportamento antigo)."""
    prop = [{"item_id": "aaa", "action": "complete", "new_status": "concluido",
             "confianca": "alta", "evidencia": "x", "notes": None, "new_prazo": None}]
    h = _Harness(propose_result=prop)
    velha = (_NOW - timedelta(days=200)).replace(tzinfo=None)
    out, props = h.run(_tsmsgs((1, "g@us", "Ana", "reporte antigo", velha)))  # sem max_age_days
    assert out["stale_skipped"] == 0
    assert out["proposals"] == 1


def test_cap_mistura_velha_e_fresca():
    """Batch misto: velha drena silenciosa, fresca vira proposta. So a fresca chama IA."""
    prop = [{"item_id": "aaa", "action": "complete", "new_status": "concluido",
             "confianca": "alta", "evidencia": "x", "notes": None, "new_prazo": None}]
    h = _Harness(propose_result=prop)
    velha = (_NOW - timedelta(days=90)).replace(tzinfo=None)
    fresca = (_NOW - timedelta(days=1)).replace(tzinfo=None)
    out, props = h.run(_tsmsgs(
        (1, "g@us", "Ana", "decisao velha de abril", velha),
        (2, "g@us", "Bruno", "reporte de hoje", fresca),
    ), max_age_days=21, now_fn=_now_fn)
    assert out["stale_skipped"] == 1
    assert out["proposals"] == 1
    assert out["processed_msgs"] == 2
    assert sorted(h.marked) == [1, 2]
    assert h.propose_calls == 1        # so a fresca chamou a IA


def test_env_threshold_parsing():
    """_evidence_max_age_days: default 21, strip do \\n, <=0 desliga, invalido->default."""
    from services.raci_group_shadow import _evidence_max_age_days
    import os as _os

    saved = _os.environ.get("RACI_EVIDENCE_MAX_AGE_DAYS")
    try:
        _os.environ.pop("RACI_EVIDENCE_MAX_AGE_DAYS", None)
        assert _evidence_max_age_days() == 21
        _os.environ["RACI_EVIDENCE_MAX_AGE_DAYS"] = " 30\n"   # Vercel cola \n
        assert _evidence_max_age_days() == 30
        _os.environ["RACI_EVIDENCE_MAX_AGE_DAYS"] = "0"        # desliga
        assert _evidence_max_age_days() is None
        _os.environ["RACI_EVIDENCE_MAX_AGE_DAYS"] = "-5"       # negativo desliga
        assert _evidence_max_age_days() is None
        _os.environ["RACI_EVIDENCE_MAX_AGE_DAYS"] = "abc"      # invalido -> default
        assert _evidence_max_age_days() == 21
    finally:
        if saved is None:
            _os.environ.pop("RACI_EVIDENCE_MAX_AGE_DAYS", None)
        else:
            _os.environ["RACI_EVIDENCE_MAX_AGE_DAYS"] = saved


# ---------------------------------------------------------------- peca inteira
# 08/09/26: o sweep processava uma RACI COMPLETA publicada por terceiro e gerava
# ZERO propostas, porque `propose` so sabe mexer em item que ja existe
# (add_note/update_status/update_prazo/complete). Os 10 itens ineditos da peca
# da Kelly (Alba, 04/09) sumiram calados, com a mensagem marcada processada.


def test_peca_publicada_com_item_inedito_e_contada():
    """O sweep tem de CONTAR o que a auditoria achou — sem criar proposta."""
    h = _Harness()
    out, props = h.run(
        _msgs((1, "jid-1", "Kelly Souza", "RACI completa aqui")),
        auditar_publicada=lambda m: {"itens_publicados": 16, "itens_no_banco": 6,
                                     "nao_absorvidos": [{"acao": "x"}] * 10,
                                     "total_nao_absorvidos": 10},
    )
    assert out["raci_publicadas"] == 1
    assert out["itens_nao_absorvidos"] == 10
    assert props == [], "auditoria NAO pode virar proposta: a fila ja tem 33 paradas"
    assert h.marked == [1]


def test_sem_auditoria_o_sweep_segue_igual():
    """Retrocompatibilidade: a dep e opcional e o default nao muda nada."""
    out, _ = _Harness().run(_msgs((1, "jid-1", "Kelly", "texto qualquer")))
    assert out["raci_publicadas"] == 0
    assert out["itens_nao_absorvidos"] == 0
    assert "auditorias" not in out


def test_auditoria_que_explode_nao_derruba_a_mensagem():
    """Mesma garantia de progresso monotonico do resto do sweep: a auditoria e
    acessoria e nunca pode custar o processamento da mensagem."""
    def boom(m):
        raise RuntimeError("parser explodiu")

    h = _Harness()
    out, _ = h.run(_msgs((1, "jid-1", "Kelly", "RACI")), auditar_publicada=boom)
    assert h.marked == [1], "a mensagem tem de avancar mesmo com a auditoria quebrada"
    assert out["processed_msgs"] == 1
    assert out["raci_publicadas"] == 0


def test_mensagem_que_nao_e_peca_nao_conta_como_auditada():
    """`None` = nao era peca. Contar isso inflaria o numero que vai pro Renato."""
    h = _Harness()
    out, _ = h.run(_msgs((1, "jid-1", "Thalita", "item 3 concluido")),
                   auditar_publicada=lambda m: None)
    assert out["raci_publicadas"] == 0
    assert "auditorias" not in out


def test_producao_liga_a_auditoria_de_verdade():
    """Guard de wiring: os testes acima injetam um fake, entao passariam mesmo
    com a auditoria desligada em producao. Aqui se checa o call-site real —
    era exatamente esse tipo de furo (produtor sem consumidor) que deixou os
    10 itens da Alba sumirem. [[feedback_consumidor_morto_wiring]]"""
    import inspect
    from services import raci_group_shadow as sh

    src = inspect.getsource(sh.process_unreviewed_groups)
    assert "auditar_publicada=_auditar_peca_publicada" in src
    assert "_notify_peca_publicada" in src
