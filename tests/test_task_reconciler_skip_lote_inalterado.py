"""O RE-JULGAMENTO DO MESMO LOTE — 30/09/2026 (migration 083).

MEDIDO em prod antes do conserto: a run diaria do reconciler faz **197 chamadas
de LLM**, e **100 (51%) julgam um lote que nao mudou desde a run anterior** —
mesma task, mesmas mensagens, mesmo enunciado. Mediana de idade das julgadas: 27
dias; a mais velha, 165. A causa e uma linha: o lote vem de
`_fetch_messages_since(scope, task["data_criacao"])`, a data de CRIACAO da task,
nunca o ultimo julgamento. A curva 151->193/dia seguia o TAMANHO DA FILA, nao
atividade nova — com a fila abrindo 3,8x mais do que fecha, crescia sem teto.

O conserto e o hash do PROMPT INTEIRO. Estes testes prendem as quatro coisas que
um `last_judged_at` simples erraria, e que foram o motivo de nao usar timestamp:

  (a) mensagem nova  -> hash muda -> julga;
  (b) enunciado da task editado pela CoS, MESMAS mensagens -> hash muda -> julga;
  (c) transcricao/OCR que chega DEPOIS por outro cano (mensagem antiga, texto
      novo, `ts` IDENTICO) -> hash muda -> julga. Este e o caso que um timestamp
      de "ultima mensagem" deixaria passar calado, e a transcricao e justamente o
      que faz uma resposta por audio encerrar uma espera;
  (d) o proprio prompt muda num deploy -> todo marcador se invalida sozinho.

E a contraprova que faltaria em todos: com o kill-switch `off`, ele volta a
julgar tudo. Sem ela, um `return` que pula sempre passaria nos testes de cima.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_task_reconciler_skip_lote_inalterado.py -q
"""
import os
import re
import sys
import types
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services import task_reconciler as tr  # noqa: E402


def _task(tid=901, titulo="Aguardar retorno do Joao sobre a minuta", descricao="Espera do Joao."):
    return {
        "id": tid, "titulo": titulo, "descricao": descricao,
        "contact_id": 2869, "project_id": None,
        "data_criacao": datetime(2026, 4, 18, 9, 30),
        "data_vencimento": None,
        "_scope": {"contact_ids": [2869], "emails": [], "origem": "ficha"},
    }


def _msg(texto, dia=20, direcao="incoming"):
    return {"direcao": direcao, "conteudo": texto, "canal": "whatsapp",
            "parte": "Joao Piccino", "ts": datetime(2026, 9, dia, 10, 0)}


class _Banca:
    """A migration 083 em memoria + o CLIENTE DA ANTHROPIC falsificado.

    Intercepta no cliente HTTP, nao em `_judge`: e a unica forma de a contagem de
    chamadas ser a de verdade e de o caminho de GRUPO rodar inteiro — montagem do
    prompt, parse do array, casamento por rotulo, guarda de citacao. Trocar
    `_judge` por um contador mediria a intencao do teste, nao o codigo.

    `chamadas` = requisicoes ao modelo. `judged` (do resumo) = tasks julgadas. O
    conserto e exatamente a distancia entre os dois numeros."""

    def __init__(self):
        self.marks = {}
        self.skips = []
        self.prompts = []
        self.resposta = None     # None = gera done=false pra cada rotulo do prompt

    def instalar(self, monkeypatch, lote, tasks=None):
        self.lote = lote
        monkeypatch.setattr(tr, "is_reconciler_enabled", lambda: True)
        monkeypatch.setattr(tr, "sweep_on_hold", lambda dry_run=False: {"items": []})
        monkeypatch.setattr(tr, "_fetch_candidate_tasks",
                            lambda: (list(tasks or [_task()]), 0, 0))
        monkeypatch.setattr(tr, "_fetch_messages_since",
                            lambda scope, since: list(self.lote))
        monkeypatch.setattr(tr, "_fetch_judgment_marks",
                            lambda ids: {i: h for i, h in self.marks.items() if i in set(ids)})
        monkeypatch.setattr(tr, "_record_judgment",
                            lambda tid, h, v: self.marks.__setitem__(tid, h))
        monkeypatch.setattr(tr, "_record_skip", lambda ids: self.skips.extend(ids))
        monkeypatch.setattr(tr, "_close_task", lambda t, v: None)
        # A telemetria de custo escreveria no banco; ela ja tem teste proprio.
        monkeypatch.setattr(tr.llm_usage, "record_response", lambda *a, **kw: None)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")

        banca = self

        class _Cliente:
            def __init__(self, *a, **kw):
                self.messages = self

            def create(self, **kw):
                prompt = kw["messages"][0]["content"]
                banca.prompts.append(prompt)
                texto = banca.resposta if banca.resposta is not None else _resposta_padrao(prompt)

                class _B:
                    text = texto

                class _M:
                    content = [_B()]

                    def model_dump(self_):
                        return {}

                return _M()

        fake = types.ModuleType("anthropic")
        fake.Anthropic = _Cliente
        monkeypatch.setitem(sys.modules, "anthropic", fake)
        return self

    @property
    def chamadas(self):
        return len(self.prompts)


def _resposta_padrao(prompt) -> str:
    """`done=false` pra cada tarefa que o prompt apresentar — o desfecho da esmagadora
    maioria dos julgamentos reais. Le os ROTULOS do proprio prompt, entao um prompt de
    grupo recebe um array completo e um individual recebe um objeto."""
    rotulos = re.findall(r"^\[(T\d+)\]$", prompt, re.MULTILINE)
    if not rotulos:
        return '{"done": false, "confidence": 0.1, "reason": "espera em curso"}'
    itens = ", ".join(
        f'{{"tarefa": "{r}", "done": false, "confidence": 0.1, "reason": "espera em curso"}}'
        for r in rotulos
    )
    return f'{{"vereditos": [{itens}]}}'


# ===========================================================================
# 1. Controle positivo — o lote nao mudou, o modelo nao e chamado
# ===========================================================================

@pytest.mark.asyncio
async def test_segunda_run_com_lote_identico_nao_chama_o_modelo(monkeypatch):
    """O caso dos 51%: a task ja foi julgada e nada aconteceu desde entao."""
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")])

    r1 = await tr.run_task_reconciler()
    assert b.chamadas == 1, "a 1a run tem que julgar — sem marcador nada pode ser pulado"
    assert r1["judged"] == 1 and r1["skipped_unchanged"] == 0

    r2 = await tr.run_task_reconciler()
    assert b.chamadas == 1, "a 2a run chamou o modelo com a MESMA entrada"
    assert r2["judged"] == 0 and r2["skipped_unchanged"] == 1
    assert b.skips == [901], "o skip tem que ser CONTADO, senao nao se distingue de skip que nao houve"


@pytest.mark.asyncio
async def test_duas_tasks_do_mesmo_lote_viram_UMA_chamada(monkeypatch):
    """O AGRUPAMENTO: 55 terceiros concentravam as 197 tasks, e tasks do mesmo
    contato fazem a mesma pergunta sobre a mesma conversa. Duas tasks, uma chamada
    — e as DUAS julgadas, cada uma com seu marcador."""
    duas = [_task(901, "Aguardar retorno do Joao sobre a minuta"),
            _task(902, "Cobrar do Joao a planilha de custos")]
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")], tasks=duas)

    r1 = await tr.run_task_reconciler()
    assert b.chamadas == 1, "as duas tasks veem o mesmo lote e deviam caber numa chamada"
    assert r1["judged"] == 2, "agrupar nao pode deixar task sem veredito"
    assert r1["llm_calls"] == 1 and r1["grupos"] == 1
    assert set(b.marks) == {901, 902}, "cada task tem o SEU marcador"
    assert b.marks[901] != b.marks[902], "enunciados diferentes, fingerprints diferentes"

    r2 = await tr.run_task_reconciler()
    assert b.chamadas == 1 and r2["skipped_unchanged"] == 2 and r2["llm_calls"] == 0


@pytest.mark.asyncio
async def test_skip_de_UMA_task_do_grupo_nao_pula_a_outra(monkeypatch):
    """A task que recebeu mensagem nova volta a julgamento; a que nao recebeu, nao.
    Se o fingerprint fosse do prompt de GRUPO, a composicao mudaria e AS DUAS
    re-julgariam — o skip perderia o efeito justamente nos contatos ativos, que sao
    os que concentram as tasks."""
    duas = [_task(901, "Aguardar retorno do Joao sobre a minuta"),
            _task(902, "Cobrar do Joao a planilha de custos")]
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")], tasks=duas)
    await tr.run_task_reconciler()

    # A #902 e editada (novo enunciado); a #901 segue identica.
    duas[1]["descricao"] = "Planilha de custos do 2o semestre, com rateio."
    r = await tr.run_task_reconciler()
    assert r["judged"] == 1 and r["skipped_unchanged"] == 1
    assert b.chamadas == 2, "a task editada tinha que ir a julgamento"
    assert "T2" not in b.prompts[-1], "sobrou 1 task no grupo: tem de ir pelo prompt individual"


# ===========================================================================
# 2. As quatro formas de o lote MUDAR — todas voltam a julgamento
# ===========================================================================

@pytest.mark.asyncio
async def test_mensagem_nova_traz_a_task_de_volta(monkeypatch):
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")])
    await tr.run_task_reconciler()

    b.lote = [_msg("devolvi a minuta assinada hoje", dia=29), _msg("vou olhar a minuta e te falo")]
    r = await tr.run_task_reconciler()
    assert b.chamadas == 2, "chegou mensagem nova e a task NAO foi a julgamento"
    assert r["judged"] == 1 and r["skipped_unchanged"] == 0


@pytest.mark.asyncio
async def test_enunciado_editado_traz_a_task_de_volta(monkeypatch):
    """Mesmas mensagens, outra pergunta. A CoS reescreve titulo/descricao e o que
    antes nao fechava passa a fechar — um marcador que so olhasse as mensagens
    congelaria o veredito velho para sempre."""
    tarefa = _task()
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")], tasks=[tarefa])
    await tr.run_task_reconciler()

    tarefa["descricao"] = "Espera do Joao — basta ele confirmar que recebeu."
    r = await tr.run_task_reconciler()
    assert b.chamadas == 2, "o enunciado mudou e a task ficou presa no veredito antigo"
    assert r["judged"] == 1


@pytest.mark.asyncio
async def test_transcricao_que_chega_depois_traz_a_task_de_volta(monkeypatch):
    """O caso que um `last_judged_at` por timestamp deixaria passar CALADO: a
    mensagem e a mesma, o `ts` e IDENTICO, e o texto passou de `[Audio]` para a
    transcricao — que e exatamente a prova de que a espera acabou. O texto efetivo
    vem de outro cano (`wa_attachments.extracted_text`, costurado na leitura pelo
    `texto_efetivo_sql`), horas depois da mensagem."""
    b = _Banca().instalar(monkeypatch, [_msg("[Audio]")])
    await tr.run_task_reconciler()

    b.lote = [_msg("ja devolvi a minuta assinada, ta no seu e-mail")]  # MESMO ts
    assert b.lote[0]["ts"] == datetime(2026, 9, 20, 10, 0)
    r = await tr.run_task_reconciler()
    assert b.chamadas == 2, "a transcricao chegou e ninguem foi reavaliar"
    assert r["judged"] == 1


def test_mudanca_nas_REGRAS_invalida_o_marcador(monkeypatch):
    """Deploy que mexe nas regras do julgamento tem que invalidar TODO marcador de
    uma vez — senao a run seguinte pula tasks que nunca foram julgadas pela regra
    nova. Nao ha versao a bumpar a mao: as regras entram no hash, logo e automatico."""
    t, m = _task(), [_msg("vou olhar")]
    h1 = tr.task_fingerprint(t, m)
    monkeypatch.setattr(tr, "_REGRAS", tr._REGRAS + "\n- E seja rigoroso.")
    assert tr.task_fingerprint(t, m) != h1


def test_o_hash_NAO_depende_da_companhia_no_grupo():
    """O fingerprint responde "mudou algo NESTA task?", nao "com quem ela foi
    julgada?". Se ele fosse do prompt de GRUPO, uma task entrar ou sair (porque
    fechou, porque chegou mensagem) mudaria o hash de todas as outras e o skip
    perderia metade do efeito justamente nos contatos mais ativos — que sao os que
    concentram as tasks."""
    a, b = _task(901), _task(902, "Cobrar do Joao a planilha")
    m = [_msg("vou olhar")]
    sozinha = tr.task_fingerprint(a, m)
    tr._build_group_prompt([a, b], m)          # a companhia existe...
    assert tr.task_fingerprint(a, m) == sozinha  # ...e nao entra no hash
    assert tr.task_fingerprint(b, m) != sozinha  # enunciado diferente, hash diferente


def test_o_hash_e_deterministico():
    """Um `now()` ou um `set` na montagem faria o hash mudar a cada run e o skip
    nunca pegaria nada — falhando CALADO, com o custo intacto e o resumo dizendo
    `skipped_unchanged: 0` como se nao houvesse o que poupar."""
    t, m = _task(), [_msg("a", dia=21), _msg("b", dia=22)]
    assert len({tr.task_fingerprint(t, m) for _ in range(5)}) == 1


# ===========================================================================
# 3. Contraprovas — sem estas, um "pula sempre" passaria em tudo acima
# ===========================================================================

@pytest.mark.asyncio
async def test_kill_switch_off_volta_a_julgar_tudo(monkeypatch):
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")])
    await tr.run_task_reconciler()
    assert b.chamadas == 1

    monkeypatch.setattr(tr, "is_skip_unchanged_enabled", lambda: False)
    r = await tr.run_task_reconciler()
    assert b.chamadas == 2, "com a chave off o julgamento cego tem que voltar"
    assert r["judged"] == 1 and r["skipped_unchanged"] == 0 and r["skip_enabled"] is False


@pytest.mark.asyncio
async def test_marcador_indisponivel_julga_tudo_em_vez_de_pular_tudo(monkeypatch):
    """Migration nao aplicada no alvo, ou tabela fora do ar: a degradacao correta e
    gastar uma run a mais, NAO parar de julgar o sistema inteiro em silencio
    ([[feedback_guarda_abstencao_vira_fabrica]])."""
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")])
    await tr.run_task_reconciler()
    monkeypatch.setattr(tr, "_fetch_judgment_marks", lambda ids: {})

    r = await tr.run_task_reconciler()
    assert b.chamadas == 2 and r["judged"] == 1 and r["skipped_unchanged"] == 0


@pytest.mark.asyncio
async def test_falha_do_modelo_nao_carimba_o_marcador(monkeypatch):
    """O FURO MAIS PERIGOSO DESTE CONSERTO, e ele nao aparece em nenhum dos testes
    de cima: `_judge` devolve `done=False` tanto quando o modelo RECUSA quanto
    quando a chamada FALHA (timeout de rede, parse quebrado, chave ausente).
    Carimbar a falha congelaria a task PARA SEMPRE — o lote nunca mais muda, e a
    pergunta nunca chegou a ser feita. Falha custa um re-julgamento na run
    seguinte, que e o preco certo."""
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")])
    b.resposta = "desculpe, nao consegui processar"   # sem JSON: parse falha
    r1 = await tr.run_task_reconciler()
    assert r1["judge_failed"] == 1, "a falha tem que aparecer no resumo"
    assert b.marks == {}, "carimbou uma pergunta que nunca foi respondida"

    # Run seguinte, API de volta: a task TEM de ser julgada.
    b2 = _Banca()
    b2.marks = b.marks
    b2.instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")])
    r2 = await tr.run_task_reconciler()
    assert b2.chamadas == 1 and r2["judged"] == 1, "a task ficou congelada por um timeout"


@pytest.mark.asyncio
async def test_recusa_do_modelo_SIM_carimba(monkeypatch):
    """CONTRAPROVA do de cima: `done=False` sem `judge_failed` e veredito de
    verdade — o modelo leu e disse que nao fechou. Esse carimba, senao o conserto
    nao corta nada (a maioria esmagadora dos vereditos e `done=False`)."""
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta e te falo")])
    r = await tr.run_task_reconciler()
    assert r["judge_failed"] == 0 and b.marks != {}


@pytest.mark.asyncio
async def test_lote_novo_ainda_FECHA_a_task(monkeypatch):
    """A contraprova que importa de verdade: o conserto e sobre QUANDO perguntar,
    nunca sobre fechar menos. Task cujo lote mudou e cujo veredito e `done` a 0,95
    continua sendo fechada."""
    b = _Banca().instalar(monkeypatch, [_msg("ja devolvi a minuta assinada hoje")])
    fechadas = []
    monkeypatch.setattr(tr, "_close_task", lambda t, v: fechadas.append(t["id"]))
    monkeypatch.setattr(tr, "_notify_closed", _noop_async)
    b.resposta = ('{"done": true, "confidence": 0.95, "evidencia_id": "M1", '
                  '"evidencia_trecho": "ja devolvi a minuta assinada hoje", '
                  '"reason": "devolveu assinada"}')
    r = await tr.run_task_reconciler()
    assert fechadas == [901] and r["closed"] == 1


async def _noop_async(*a, **kw):
    return None


# ===========================================================================
# 3b. O que o AGRUPAMENTO poe em risco
#
# Juntar N tarefas num prompto economiza chamada e cria uma classe de erro que nao
# existia: o veredito aplicado na tarefa ERRADA. A guarda de citacao de 23/08 nao
# cobre isso — ela confere que o trecho existe nas MENSAGENS, nao que pertence
# aquela tarefa. O casamento tem de ser pelo ROTULO, e o que nao casar tem de ser
# descartado, nunca adivinhado.
# ===========================================================================

def _duas():
    return [_task(901, "Aguardar retorno do Joao sobre a minuta"),
            _task(902, "Cobrar do Joao a planilha de custos")]


@pytest.mark.asyncio
async def test_veredito_vai_pra_task_do_ROTULO_nao_pra_ordem(monkeypatch):
    """O modelo devolve os vereditos FORA de ordem, e so T2 fecha. Casar por posicao
    no array — a forma obvia de escrever isto — fecharia a #901 com a evidencia da
    #902. Nao ha barra de confianca que pegue: o veredito e legitimo, so esta na
    tarefa errada."""
    b = _Banca().instalar(monkeypatch, [_msg("mandei a planilha de custos agora")],
                          tasks=_duas())
    fechadas = []
    monkeypatch.setattr(tr, "_close_task", lambda t, v: fechadas.append(t["id"]))
    monkeypatch.setattr(tr, "_notify_closed", _noop_async)
    b.resposta = ('{"vereditos": ['
                  '{"tarefa": "T2", "done": true, "confidence": 0.95, "evidencia_id": "M1",'
                  ' "evidencia_trecho": "mandei a planilha de custos agora",'
                  ' "reason": "planilha enviada"},'
                  '{"tarefa": "T1", "done": false, "confidence": 0.1, "reason": "minuta segue aberta"}'
                  ']}')
    r = await tr.run_task_reconciler()
    assert fechadas == [902], f"fechou a task errada: {fechadas}"
    assert r["judged"] == 2 and r["closed"] == 1


@pytest.mark.asyncio
async def test_rotulo_inventado_e_descartado(monkeypatch):
    """`T9` nao existe no grupo. Descartar e o certo — adivinhar a quem ele se refere
    e como um fechamento aterrissa numa task que ninguem julgou."""
    b = _Banca().instalar(monkeypatch, [_msg("mandei tudo")], tasks=_duas())
    monkeypatch.setattr(tr, "_notify_closed", _noop_async)
    b.resposta = ('{"vereditos": ['
                  '{"tarefa": "T9", "done": true, "confidence": 0.99, "evidencia_id": "M1",'
                  ' "evidencia_trecho": "mandei tudo", "reason": "x"},'
                  '{"tarefa": "T1", "done": false, "confidence": 0.1, "reason": "aberta"}'
                  ']}')
    r = await tr.run_task_reconciler()
    assert r["closed"] == 0, "veredito de rotulo inventado nao pode fechar nada"
    # A T2 ficou sem resposta ⇒ falha, nao `done=false` carimbado.
    assert r["judge_failed"] == 1 and 902 not in b.marks


@pytest.mark.asyncio
async def test_veredito_omitido_no_grupo_vira_falha_e_nao_carimba(monkeypatch):
    """Grupo devolve 1 de 2. A que faltou nao foi julgada — carimbar `done=false`
    ali a congelaria para sempre, porque o lote nao muda mais."""
    b = _Banca().instalar(monkeypatch, [_msg("vou ver")], tasks=_duas())
    b.resposta = ('{"vereditos": [{"tarefa": "T1", "done": false, '
                  '"confidence": 0.1, "reason": "aberta"}]}')
    r = await tr.run_task_reconciler()
    assert r["judge_failed"] == 1
    assert 901 in b.marks and 902 not in b.marks

    b.resposta = None                      # modelo de volta ao normal
    r2 = await tr.run_task_reconciler()
    assert r2["judged"] == 1, "a task omitida tinha que voltar a julgamento"


@pytest.mark.asyncio
async def test_a_citacao_inventada_e_derrubada_TAMBEM_no_grupo(monkeypatch):
    """A guarda de 23/08 nao pode valer so no caminho individual — foi por isso que
    `_finaliza_verdict` passou a ser uma funcao so, usada nos dois."""
    b = _Banca().instalar(monkeypatch, [_msg("vou ver")], tasks=_duas())
    monkeypatch.setattr(tr, "_notify_closed", _noop_async)
    b.resposta = ('{"vereditos": [{"tarefa": "T1", "done": true, "confidence": 0.95,'
                  ' "evidencia_id": "M1",'
                  ' "evidencia_trecho": "minuta assinada e devolvida em 14/07",'
                  ' "reason": "documenta a devolucao"},'
                  '{"tarefa": "T2", "done": false, "confidence": 0.1, "reason": "aberta"}]}')
    r = await tr.run_task_reconciler()
    assert r["closed"] == 0, "citacao que nao existe nas mensagens fechou uma task"
    assert r["blocked_no_evidence"] == 1


@pytest.mark.asyncio
async def test_grupo_grande_e_quebrado_em_blocos(monkeypatch):
    """29 tasks num prompt so seria economia paga com qualidade: quanto mais
    enunciados na mesma janela, mais facil casar a evidencia com a tarefa errada. O
    teto de 8 mantem o corte quase inteiro (20 tasks -> 3 chamadas em vez de 20)."""
    muitas = [_task(900 + i, f"Cobrar do Joao o item {i}") for i in range(20)]
    b = _Banca().instalar(monkeypatch, [_msg("vou ver")], tasks=muitas)
    r = await tr.run_task_reconciler()
    assert tr.MAX_TASKS_POR_CHAMADA == 8
    assert b.chamadas == 3 and r["llm_calls"] == 3, "20 tasks em blocos de 8 = 3 chamadas"
    assert r["judged"] == 20, "nenhuma task pode ficar sem veredito na quebra"
    assert r["grupos"] == 1, "o grupo e um so; a quebra e do bloco, nao do grupo"


@pytest.mark.asyncio
async def test_dry_run_NAO_carimba_e_nao_engole_o_fechamento(monkeypatch):
    """O FURO QUE A VALIDACAO EM PROD MOSTROU. O dry_run carimbava "ja julguei", e
    ele mesmo relatou **5 tasks que fechariam**. A run REAL seguinte acharia o
    marcador com o lote inalterado e pularia justamente essas 5 — fechamento perdido
    ate alguma mensagem nova mexer no lote. Economizar dois centavos ao custo de nao
    fechar o que fechava e trocar a razao de existir do reconciler por troco."""
    b = _Banca().instalar(monkeypatch, [_msg("ja devolvi a minuta assinada hoje")])
    fechadas = []
    monkeypatch.setattr(tr, "_close_task", lambda t, v: fechadas.append(t["id"]))
    monkeypatch.setattr(tr, "_notify_closed", _noop_async)
    b.resposta = ('{"done": true, "confidence": 0.95, "evidencia_id": "M1", '
                  '"evidencia_trecho": "ja devolvi a minuta assinada hoje", '
                  '"reason": "devolveu assinada"}')

    r1 = await tr.run_task_reconciler(dry_run=True)
    assert r1["would_close"] == 1 and r1["marcou"] is False
    assert b.marks == {}, "o dry_run carimbou e vai engolir o fechamento da run real"
    assert b.skips == []

    r2 = await tr.run_task_reconciler()          # a run de verdade
    assert fechadas == [901], "a run real pulou a task que o dry_run tinha carimbado"
    assert r2["closed"] == 1 and r2["marcou"] is True


@pytest.mark.asyncio
async def test_dry_run_com_marcar_explicito_carimba(monkeypatch):
    """CONTRAPROVA: medir o skip exige duas passadas que carimbam. Pedido
    explicitamente — nunca por default."""
    b = _Banca().instalar(monkeypatch, [_msg("vou olhar a minuta")])
    r1 = await tr.run_task_reconciler(dry_run=True, marcar=True)
    assert r1["marcou"] is True and b.marks != {}

    r2 = await tr.run_task_reconciler(dry_run=True, marcar=True)
    assert r2["skipped_unchanged"] == 1 and r2["judged"] == 0


def test_lotes_diferentes_NAO_se_agrupam():
    """O furo que agrupar por CONTATO abriria: as tasks de um mesmo contato tem datas
    de criacao diferentes, e a mais recente veria mensagens ANTERIORES a ela — as que
    nao bastaram, que e por isso que a task existe. Agrupando pelo LOTE, janelas
    diferentes caem em grupos diferentes."""
    lote_a = [_msg("mensagem antiga", dia=10), _msg("mensagem nova", dia=28)]
    lote_b = [_msg("mensagem nova", dia=28)]
    assert tr.batch_key(lote_a) != tr.batch_key(lote_b)
    assert tr.batch_key(lote_b) == tr.batch_key([_msg("mensagem nova", dia=28)])


# ===========================================================================
# 4. O SQL da migration 083, de verdade
#
# Os testes de cima trocam `_record_judgment`/`_fetch_judgment_marks` por um dict:
# provam o COMPORTAMENTO do skip e nao executam uma linha de SQL. Se o UPSERT
# estiver errado, ou a tabela nao existir no alvo, tudo la em cima segue verde e o
# skip nunca pega nada em producao — marcador que nao grava e skip que nao
# acontece ([[feedback_teste_com_ambiente_declarado_nao_aplicado]]). Aqui o
# caminho real roda contra Postgres, em transacao com rollback.
# ===========================================================================

@pytest.fixture
def banco_real(monkeypatch):
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        conn = psycopg2.connect(
            os.getenv("LOCAL_TEST_DATABASE_URL", "postgresql://rap@localhost:5432/intel"),
            cursor_factory=RealDictCursor, connect_timeout=2,
        )
    except Exception:
        pytest.skip("Postgres local indisponivel")
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.task_reconciler_judgments') AS t")
    if cur.fetchone()["t"] is None:
        conn.close()
        pytest.skip("migration 083 nao aplicada neste banco")
    cur.execute("SELECT id FROM tasks ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if not row:
        conn.close()
        pytest.skip("sem tasks no banco de teste")

    class _Conn:
        def cursor(self_):
            return cur

        def commit(self_):
            pass            # o rollback do fixture e que manda

        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

    monkeypatch.setattr(tr, "get_db", lambda: _Conn())
    yield cur, row["id"]
    conn.rollback()
    conn.close()


def test_o_upsert_grava_le_e_ACUMULA(banco_real):
    cur, tid = banco_real
    v = {"done": False, "confidence": 0.42, "reason": "espera em curso"}

    tr._record_judgment(tid, "hash-um", v)
    assert tr._fetch_judgment_marks([tid]) == {tid: "hash-um"}

    tr._record_judgment(tid, "hash-dois", {"done": True, "confidence": 0.95, "reason": "fechou"})
    assert tr._fetch_judgment_marks([tid]) == {tid: "hash-dois"}, "o hash tem que ser o do ULTIMO julgamento"

    tr._record_skip([tid])
    tr._record_skip([tid])
    cur.execute("SELECT judged_count, skipped_count, last_done, last_confidence "
                "FROM task_reconciler_judgments WHERE task_id = %s", (tid,))
    r = cur.fetchone()
    assert r["judged_count"] == 2, "o ON CONFLICT nao esta acumulando — a auditoria nasce zerada"
    assert r["skipped_count"] == 2
    assert r["last_done"] is True and float(r["last_confidence"]) == 0.95


def test_marcador_de_outra_task_nao_vaza_na_leitura(banco_real):
    """`_fetch_judgment_marks` le o LOTE inteiro numa query; filtro errado no
    `ANY(%s)` devolveria marcador de outra task e pularia julgamento alheio."""
    cur, tid = banco_real
    tr._record_judgment(tid, "hash-um", {"done": False, "confidence": 0.1, "reason": "x"})
    assert tr._fetch_judgment_marks([tid + 10_000_000]) == {}
    assert tr._fetch_judgment_marks([]) == {}


def test_reason_longa_nao_estoura_a_gravacao(banco_real):
    """O `reason` vem do modelo. Sem o corte, um veredito verboso derruba a
    gravacao do marcador e a task volta a ser julgada para sempre, calada."""
    cur, tid = banco_real
    tr._record_judgment(tid, "h", {"done": False, "confidence": 0.1, "reason": "x" * 5000})
    cur.execute("SELECT length(last_reason) AS n FROM task_reconciler_judgments WHERE task_id = %s", (tid,))
    assert cur.fetchone()["n"] == 400
