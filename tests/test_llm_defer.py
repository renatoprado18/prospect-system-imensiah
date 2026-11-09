"""O JULGAMENTO QUE NÃO ACONTECE — 09/11/2026 (migration 089).

MEDIDO no dia: a conta Anthropic está sem saldo desde 28/10 19:33 BRT, o flag
`anthropic_credit_down` de pé há **284 horas**, e o `task_reconciler` — 41% dos
fechamentos de task do sistema — passou esse período devolvendo `judge_failed` e
logando um warning. Nada registrava o julgamento que não foi feito. O efeito
aparecia no board executivo como "326 tasks atrasadas", sem nada apontando a
causa.

O conserto é uma fila: com o canário aceso o passo LLM é ENFILEIRADO, e uma sessão
`/sys` responde o prompt pela assinatura em vez da API. Estes testes prendem as
cinco coisas que decidem se isso é um dreno ou um buraco novo:

  (a) com o canário APAGADO nada muda — a API é chamada e a fila fica vazia. Sem
      esta, um bug no gate transformaria o caminho normal em fila silenciosa;
  (b) com ele ACESO a API **não** é chamada e o job entra na fila;
  (c) diferir NÃO carimba o marcador do skip. É o ponto fino: `judge_failed`
      significa "a pergunta não foi respondida", e carimbar aqui congelaria a task
      para sempre — o lote não muda mais, então ela nunca voltaria a julgamento;
  (d) **a guarda de citação de 23/08 vale no caminho novo.** Resposta com citação
      inventada tem de ser DESCARTADA pelo aplicador. Esta é a que importa: um
      dreno que afrouxa a guarda fecha tasks com prova que não existe;
  (e) o kill-switch do reconciler continua barrando a ESCRITA. Se o gate do Renato
      está `off`, a resposta é julgada e relatada, e nada fecha.

E a mecânica da fila (dedup, claim atômico, expiração) contra o Postgres local,
que é onde `ON CONFLICT` com índice parcial e `FOR UPDATE SKIP LOCKED` ou
funcionam ou não — teste com conexão dublada seria cego justamente a eles
([[feedback_teste_que_dubla_a_conexao_e_cego_ao_destino]]).

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_llm_defer.py -q
"""
import json
import os
import sys
import types
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from database import get_db  # noqa: E402
from services import llm_defer  # noqa: E402
from services import task_reconciler as tr  # noqa: E402


# ----------------------------------------------------------------- fixtures

def _task(tid=901, titulo="Aguardar retorno do Joao sobre a minuta"):
    return {
        "id": tid, "titulo": titulo, "descricao": "Espera do Joao.",
        "contact_id": 2869, "project_id": None,
        "data_criacao": datetime(2026, 4, 18, 9, 30),
        "data_vencimento": None,
        "_scope": {"contact_ids": [2869], "emails": [], "origem": "ficha"},
    }


# A frase é a evidência real: o aplicador confere a citação contra ela.
_FRASE = "Minuta revisada e devolvida, segue em anexo para assinatura"


def _msg(texto=_FRASE, dia=20, direcao="incoming"):
    return {"direcao": direcao, "conteudo": texto, "canal": "whatsapp",
            "parte": "Joao Piccino", "ts": datetime(2026, 9, dia, 10, 0)}


@pytest.fixture
def limpa_fila():
    """A tabela é compartilhada com o dev local — limpa só o que este teste cria."""
    marcas = []
    yield marcas
    if marcas:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM llm_defer_jobs WHERE function LIKE %s",
                        ("teste.%",))
            cur.execute("DELETE FROM llm_defer_jobs WHERE prompt LIKE %s",
                        ("%" + marcas[0] + "%",))
            conn.commit()


@pytest.fixture(autouse=True)
def sem_cache_de_canario():
    """O cache de 60s do canário faria o 2º teste ler o veredito do 1º."""
    llm_defer._canary_cache = (0.0, False)
    yield
    llm_defer._canary_cache = (0.0, False)


class _ClienteFalso:
    """Conta as chamadas ao modelo. Instalado como o módulo `anthropic`."""

    chamadas = 0

    def __init__(self, *a, **kw):
        self.messages = self

    def create(self, **kw):
        type(self).chamadas += 1

        class _B:
            type = "text"
            text = json.dumps({"done": False, "confidence": 0.1, "reason": "nao"})

        class _M:
            content = [_B()]

            def model_dump(self_):
                return {}

        return _M()


@pytest.fixture
def anthropic_falso(monkeypatch):
    _ClienteFalso.chamadas = 0
    fake = types.ModuleType("anthropic")
    fake.Anthropic = _ClienteFalso
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")
    monkeypatch.setattr(tr.llm_usage, "record_response", lambda *a, **kw: None)
    return _ClienteFalso


# -------------------------------------------------- (a) e (b): o gate

def test_canario_apagado_chama_a_api_e_nao_enfileira(monkeypatch, anthropic_falso):
    """Com saldo, o caminho é o de hoje. O gate não pode inventar fila."""
    monkeypatch.setattr(llm_defer, "is_canary_lit", lambda use_cache=True: False)
    enfileirados = []
    monkeypatch.setattr(llm_defer, "enqueue",
                        lambda *a, **kw: enfileirados.append(a) or 1)

    verdict = tr._judge(_task(), [_msg()])

    assert anthropic_falso.chamadas == 1, "a API tinha de ser chamada"
    assert enfileirados == [], "nada deveria ir pra fila com o canário apagado"
    assert verdict["done"] is False
    assert not verdict.get("deferred")


def test_canario_aceso_enfileira_e_nao_chama_a_api(monkeypatch, anthropic_falso):
    """Aceso: a pergunta vai pra fila em vez de bater numa API que recusa."""
    monkeypatch.setattr(llm_defer, "is_canary_lit", lambda use_cache=True: True)
    capturado = {}

    def _enqueue(function, prompt, model, max_tokens, context):
        capturado.update(function=function, prompt=prompt, context=context)
        return 4242

    monkeypatch.setattr(llm_defer, "enqueue", _enqueue)

    verdict = tr._judge(_task(), [_msg()])

    assert anthropic_falso.chamadas == 0, "a API NÃO pode ser chamada com o canário aceso"
    assert capturado["function"] == "task_reconciler.judge"
    assert verdict["judge_failed"] is True
    assert verdict["deferred"] is True
    assert "4242" in verdict["reason"]
    # O contexto tem de carregar o que o aplicador precisa pra aplicar a resposta
    # COM AS MESMAS GUARDAS — em particular `exibidas`, que é contra o que a
    # citação é conferida.
    assert capturado["context"]["kind"] == "judge"
    assert [t["id"] for t in capturado["context"]["tasks"]] == [901]
    assert capturado["context"]["tasks"][0]["fingerprint"]
    assert _FRASE in " ".join(capturado["context"]["exibidas"].values())


def test_grupo_aceso_difere_com_os_rotulos_de_quem_montou_o_prompt(
        monkeypatch, anthropic_falso):
    """Os rótulos vêm do prompt real — recalculá-los por fora é como um veredito
    acaba aplicado na task errada."""
    monkeypatch.setattr(llm_defer, "is_canary_lit", lambda use_cache=True: True)
    capturado = {}
    monkeypatch.setattr(llm_defer, "enqueue",
                        lambda f, p, m, mt, c: capturado.update(prompt=p, ctx=c) or 7)

    tasks = [_task(901), _task(902, "Enviar o contrato ao Joao")]
    vereditos = tr._judge_group(tasks, [_msg()])

    assert anthropic_falso.chamadas == 0
    assert set(vereditos) == {901, 902}
    assert all(v["deferred"] for v in vereditos.values())
    rotulos = {t["id"]: t["rotulo"] for t in capturado["ctx"]["tasks"]}
    assert rotulos == {901: "T1", 902: "T2"}
    # Contraprova de que os rótulos são os do prompt de verdade, não os que o
    # teste supõe: eles aparecem no texto enviado.
    assert "[T1]" in capturado["prompt"] and "[T2]" in capturado["prompt"]


# ----------------------------- (c): diferir não carimba o marcador

def test_diferir_nao_carimba_o_marcador_do_skip(monkeypatch, anthropic_falso):
    """`judge_failed` é "a pergunta não foi respondida". Carimbar aqui congelaria a
    task para sempre: o lote não muda mais, então ela nunca voltaria a julgamento."""
    monkeypatch.setattr(llm_defer, "is_canary_lit", lambda use_cache=True: True)
    monkeypatch.setattr(llm_defer, "enqueue", lambda *a, **kw: 11)
    monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: True)
    monkeypatch.setattr(tr, "sweep_on_hold", lambda dry_run=False: {"items": []})
    monkeypatch.setattr(tr, "_fetch_candidate_tasks", lambda: ([_task()], 0, 0))
    monkeypatch.setattr(tr, "_fetch_messages_since", lambda scope, since: [_msg()])
    monkeypatch.setattr(tr, "_fetch_judgment_marks", lambda ids: {})
    marcados, skipados, fechados = [], [], []
    monkeypatch.setattr(tr, "_record_judgment",
                        lambda tid, h, v: marcados.append(tid))
    monkeypatch.setattr(tr, "_record_skip", lambda ids: skipados.extend(ids))
    monkeypatch.setattr(tr, "_close_task", lambda t, v: fechados.append(t["id"]))

    import asyncio
    resumo = asyncio.run(tr.run_task_reconciler(dry_run=False))

    assert marcados == [], "diferido NÃO pode carimbar marcador"
    assert fechados == [], "nada fecha sem veredito"
    assert resumo.get("judged", 0) >= 0


# ------------------- (d): a guarda de citação vale no caminho novo

def _ctx(kind="judge", tid=901, fingerprint="fp-abc"):
    return {
        "kind": kind,
        "exibidas": {"1": _FRASE},
        "tasks": [{"id": tid, "titulo": "Aguardar retorno do Joao",
                   "contact_id": 2869, "project_id": None,
                   "rotulo": "T1", "fingerprint": fingerprint}],
    }


def _aplica(ctx, answer):
    import asyncio
    return asyncio.run(tr.apply_deferred_judgment(ctx, answer))


def test_aplicador_fecha_com_citacao_literal(monkeypatch):
    """O caminho feliz: a resposta vem da sessão e o fechamento é o normal."""
    monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: True)
    monkeypatch.setattr(tr, "is_skip_unchanged_enabled", lambda: True)
    fechados, marcados, avisos = [], [], []
    monkeypatch.setattr(tr, "_close_task",
                        lambda t, v: fechados.append((t["id"], v["confidence"])))
    monkeypatch.setattr(tr, "_record_judgment",
                        lambda tid, h, v: marcados.append((tid, h)))

    async def _notifica(closed):
        avisos.extend(closed)

    monkeypatch.setattr(tr, "_notify_closed", _notifica)

    out = _aplica(_ctx(), json.dumps({
        "done": True, "confidence": 0.95, "evidencia_id": "M1",
        "evidencia_trecho": _FRASE, "reason": "Joao devolveu a minuta",
    }))

    assert out["ok"] is True
    assert fechados == [(901, 0.95)]
    # O marcador é carimbado com o fingerprint GRAVADO NO JOB, nunca recalculado:
    # o lote pode ter mudado desde o enfileiramento, e o marcador tem de
    # descrever a pergunta que foi de fato respondida.
    assert marcados == [(901, "fp-abc")]
    assert len(avisos) == 1


def test_citacao_inventada_e_descartada_e_nada_fecha(monkeypatch):
    """A guarda de 23/08 não pode valer só no caminho da API.

    Esta é a prova de que o dreno não afrouxa nada: a sessão `/sys` responde com
    uma justificativa impecável e uma citação que não existe nas mensagens — e o
    veredito morre, como morreria vindo do modelo."""
    monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: True)
    monkeypatch.setattr(tr, "is_skip_unchanged_enabled", lambda: True)
    fechados = []
    monkeypatch.setattr(tr, "_close_task", lambda t, v: fechados.append(t["id"]))
    monkeypatch.setattr(tr, "_record_judgment", lambda tid, h, v: None)

    out = _aplica(_ctx(), json.dumps({
        "done": True, "confidence": 0.99, "evidencia_id": "M1",
        "evidencia_trecho": "Confirmo que o contrato foi assinado ontem",
        "reason": "o contato confirmou a assinatura",
    }))

    assert out["ok"] is True, "a aplicação ocorreu — foi o VEREDITO que caiu"
    assert fechados == [], "citação inventada não pode fechar task"
    assert out["sem_evidencia"], "a recusa tem de ser relatada, não engolida"
    assert "não aparece" in out["sem_evidencia"][0]["motivo"]


def test_resposta_sem_json_e_falha_de_aplicacao(monkeypatch):
    monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: True)
    out = _aplica(_ctx(), "Analisei a conversa e acho que a tarefa foi concluída.")
    assert out["ok"] is False
    assert "JSON" in out["erro"]


def test_rotulo_fora_do_bloco_e_descartado(monkeypatch):
    """Adivinhar por posição é como um veredito acaba na task errada."""
    monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: True)
    monkeypatch.setattr(tr, "is_skip_unchanged_enabled", lambda: False)
    fechados = []
    monkeypatch.setattr(tr, "_close_task", lambda t, v: fechados.append(t["id"]))

    out = _aplica(_ctx(kind="judge_grupo"), json.dumps({"vereditos": [
        {"tarefa": "T9", "done": True, "confidence": 0.99,
         "evidencia_id": "M1", "evidencia_trecho": _FRASE, "reason": "x"},
    ]}))

    assert out["ok"] is False
    assert fechados == []


# -------------------------- (e): o kill-switch do Renato continua valendo

def test_kill_switch_off_julga_e_relata_sem_fechar(monkeypatch):
    """O gate é do Renato e não se contorna por porta nova."""
    monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: False)
    monkeypatch.setattr(tr, "is_skip_unchanged_enabled", lambda: True)
    fechados, marcados = [], []
    monkeypatch.setattr(tr, "_close_task", lambda t, v: fechados.append(t["id"]))
    monkeypatch.setattr(tr, "_record_judgment", lambda tid, h, v: marcados.append(tid))

    out = _aplica(_ctx(), json.dumps({
        "done": True, "confidence": 0.95, "evidencia_id": "M1",
        "evidencia_trecho": _FRASE, "reason": "devolveu a minuta",
    }))

    assert out["ok"] is True
    assert out["reconciler_desligado"] is True
    assert fechados == [], "kill-switch off não fecha nada"
    assert [r["id"] for r in out["fecharia"]] == [901], "mas relata o que fecharia"
    assert marcados == [], "desligado não carimba: a medição não vira decisão"


# ---------------------- a mecânica da fila, contra o Postgres de verdade

def test_dedup_vale_entre_vivos_e_libera_depois_de_aplicado(limpa_fila):
    """O reconciler roda todo dia e remontaria o mesmo bloco a cada passada: sem
    dedup a fila cresceria por run, não por trabalho. E depois que o job é
    aplicado, a mesma pergunta tem de poder voltar — lote novo, pergunta nova."""
    marca = "prompt-dedup-089"
    limpa_fila.append(marca)
    ctx = {"kind": "judge", "tasks": [{"id": 1}], "exibidas": {}}

    a = llm_defer.enqueue("teste.dedup", marca, "haiku", 100, ctx)
    b = llm_defer.enqueue("teste.dedup", marca, "haiku", 100, ctx)
    assert a and b
    assert a == b, "o segundo enqueue tem de devolver o job VIVO, não criar outro"

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE llm_defer_jobs SET status='applied' WHERE id=%s", (a,))
        conn.commit()

    c = llm_defer.enqueue("teste.dedup", marca, "haiku", 100, ctx)
    assert c and c != a, "aplicado não pode bloquear a pergunta de amanhã"


def test_claim_nao_entrega_o_mesmo_job_duas_vezes(limpa_fila):
    """Duas sessões `/sys` abertas é cenário normal (o Renato roda `/loop` em mais
    de uma janela). Sem `FOR UPDATE SKIP LOCKED` as duas responderiam o mesmo
    prompt, e a segunda resposta cairia sobre uma task já fechada."""
    marca = "prompt-claim-089"
    limpa_fila.append(marca)
    ctx = {"kind": "judge", "tasks": [{"id": 1}], "exibidas": {}}
    jid = llm_defer.enqueue("teste.claim", marca, "haiku", 100, ctx)
    assert jid

    primeiro = [j["id"] for j in llm_defer.claim(limit=10, by="sys-A")]
    segundo = [j["id"] for j in llm_defer.claim(limit=10, by="sys-B")]

    assert jid in primeiro
    assert jid not in segundo, "job já pego não pode sair de novo"


def test_responder_job_ja_aplicado_e_recusado(limpa_fila):
    """Retry depois de aplicado reabriria a decisão sobre um lote vencido."""
    import asyncio
    marca = "prompt-reaplicar-089"
    limpa_fila.append(marca)
    jid = llm_defer.enqueue("teste.x", marca, "haiku", 100, {})
    assert jid
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE llm_defer_jobs SET status='applied' WHERE id=%s", (jid,))
        conn.commit()

    out = asyncio.run(llm_defer.submit_answer(jid, "{}"))
    assert out["ok"] is False
    assert "applied" in out["erro"]


def test_peek_diz_se_o_canario_esta_aceso(limpa_fila):
    """Fila vazia tem dois significados opostos — com o canário apagado é saúde,
    com ele aceso é produtor que não está enfileirando. Sem o par, o `/sys` leria
    as duas como "nada a fazer" e reportaria verde."""
    estado = llm_defer.peek(limit=5)
    assert estado["disponivel"] is True
    assert "canario_aceso" in estado
    assert "pendentes" in estado
