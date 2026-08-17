"""A camada passou a escrever — estes testes são o que impede isso de virar dano.

Diretriz do Renato (10/08/2026): a inteligência interpreta os fatos e atualiza o
conhecimento; na dúvida, aciona. A camada nasceu read-only em 31/07 justamente
porque agente que escreve pode "consertar" o que não entendeu. A saída não é
proibir nem liberar: é escrita ESTREITA, AUDITADA e REVERSÍVEL.

Cada teste aqui guarda uma forma de perder isso:
  - operação fora da lista       -> a camada faz o que ninguém autorizou
  - campo fora da operação       -> autorizei follow-up, ela mexeu em outra coluna
  - escrita sem motivo           -> o livro-razão vira log de diff, inauditável
  - confiança baixa virando UPDATE -> o palpite entra como fato
  - desfazer apagando INSERT     -> repete o dano em CASCADE do merge

Rodar: PYTHONPATH=app .venv/bin/python -m pytest tests/test_agent_write.py -v
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))


class _Cur:
    """Cursor falso que registra o SQL emitido e devolve linhas plausíveis."""

    def __init__(self, linha_existente=None):
        self.sqls = []
        self.params = []
        self._linha = linha_existente

    def execute(self, sql, params=None):
        self.sqls.append(" ".join(sql.split()))
        self.params.append(params)

    def fetchone(self):
        ultimo = self.sqls[-1].upper() if self.sqls else ""
        if ultimo.startswith("SELECT") and "AGENT_WRITES" in ultimo:
            return self._linha
        if ultimo.startswith("SELECT"):
            return self._linha if self._linha is not None else {"id": 42}
        return {"id": 42}


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def aw(monkeypatch):
    from services import agent_write
    cur = _Cur()
    monkeypatch.setattr(agent_write, "get_db", lambda: _Conn(cur))
    return agent_write, cur


def test_escreve_o_caso_pretola(aw):
    """O caso que originou tudo: a frente existe no mundo e não no banco."""
    mod, cur = aw
    rid = mod.escrever(
        "criar_frente_board_hunt",
        {"nome": "Orbiz / Rodrigo Pretola", "contato_id": 5245, "fase": 2, "status": "ativo"},
        motivo="Renato retomou o fio e propôs data (HH qua 12/08); board hunt registra Orbiz reativado desde 07/08 sem linha no banco.",
        confianca=0.9,
        fato_origem="messages#27573",
    )
    assert rid == 42
    assert any("INSERT INTO board_hunt_frentes" in s for s in cur.sqls)
    assert any("INSERT INTO agent_writes" in s for s in cur.sqls), "escreveu sem registrar no livro-razão"


def test_operacao_fora_da_lista_nao_acontece(aw):
    """Lista fechada: capacidade nova exige decisão, não improviso em runtime."""
    mod, cur = aw
    with pytest.raises(mod.OperacaoNaoPermitida):
        mod.escrever("apagar_contato", {"id": 5245}, motivo="limpeza", confianca=1.0)
    assert not cur.sqls, "chegou a emitir SQL para operação não permitida"


def test_campo_fora_da_operacao_nao_passa(aw):
    """Autorizar 'criar task' não pode autorizar mexer em qualquer coluna de tasks."""
    mod, cur = aw
    with pytest.raises(mod.OperacaoNaoPermitida) as exc:
        mod.escrever(
            "criar_task_followup",
            {"titulo": "FUP Pretola", "data_conclusao": "2026-08-12"},
            motivo="follow-up", confianca=0.9,
        )
    assert "data_conclusao" in str(exc.value)
    assert not cur.sqls


def test_escrita_sem_motivo_e_recusada(aw):
    """Livro-razão sem 'por quê' mostra o que mudou e não deixa julgar se devia."""
    mod, _ = aw
    for vazio in ("", "   "):
        with pytest.raises(mod.OperacaoNaoPermitida):
            mod.escrever("criar_task_followup", {"titulo": "x"}, motivo=vazio, confianca=0.9)


def test_confianca_baixa_vira_proposta_nao_escrita(aw):
    """Abaixo do piso a camada PERGUNTA. E perguntar não é abster: o ambíguo tem
    destino, com payload pronto para virar decisão do Renato."""
    mod, cur = aw
    with pytest.raises(mod.PropostaPendente) as exc:
        mod.escrever(
            "atualizar_fase_frente", {"fase": 4},
            motivo="talvez tenha avançado", confianca=0.4, registro_id=3,
        )
    assert exc.value.payload == {"fase": 4}
    assert exc.value.confianca == 0.4
    assert not cur.sqls, "gravou apesar da confiança abaixo do piso"


def test_piso_e_inclusivo_na_borda(aw):
    """Exatamente no piso escreve — senão o piso documentado não é o piso real."""
    mod, _ = aw
    rid = mod.escrever(
        "registrar_nota_projeto",
        {"project_id": 1, "tipo": "camada", "conteudo": "x"},
        motivo="registro do fato interpretado",
        confianca=mod.PISO_ESCRITA,
    )
    assert rid == 42


def test_update_guarda_o_estado_anterior(monkeypatch):
    """Sem o valor anterior, desfazer é arqueologia — foi assim que o dano do
    merge de contatos ficou inauditável."""
    from services import agent_write
    cur = _Cur(linha_existente={"fase": 2, "status": "ativo", "nota": "antes", "piso_alvo": None})
    monkeypatch.setattr(agent_write, "get_db", lambda: _Conn(cur))

    agent_write.escrever(
        "atualizar_fase_frente", {"fase": 3},
        motivo="reunião marcada para 12/08", confianca=0.85, registro_id=7,
    )
    idx = next(i for i, s in enumerate(cur.sqls) if "INSERT INTO agent_writes" in s)
    anterior = json.loads(cur.params[idx][5])
    assert anterior["fase"] == 2, "não guardou o valor anterior"
    assert anterior["nota"] == "antes"


def test_update_sem_registro_id_e_recusado(aw):
    mod, _ = aw
    with pytest.raises(mod.OperacaoNaoPermitida):
        mod.escrever("atualizar_fase_frente", {"fase": 3}, motivo="x", confianca=0.9)


def test_desfazer_insert_nao_apaga(monkeypatch):
    """Apagar o registro levaria junto o que foi pendurado nele desde então — o
    mesmo formato de dano do DELETE em CASCADE. Marca e devolve ao humano."""
    from services import agent_write
    cur = _Cur(linha_existente={
        "id": 9, "tabela": "board_hunt_frentes", "registro_id": 13,
        "valor_anterior": None, "desfeito_em": None,
    })
    monkeypatch.setattr(agent_write, "get_db", lambda: _Conn(cur))

    r = agent_write.desfazer(9)
    assert r["status"] == "insert_marcado_nao_apagado"
    assert not any(s.upper().startswith("DELETE") for s in cur.sqls), "apagou o registro"


def test_desfazer_update_restaura(monkeypatch):
    from services import agent_write
    cur = _Cur(linha_existente={
        "id": 10, "tabela": "board_hunt_frentes", "registro_id": 7,
        "operacao": "atualizar_fase_frente",
        "valor_anterior": json.dumps({"fase": 2, "status": "ativo"}),
        "desfeito_em": None,
    })
    monkeypatch.setattr(agent_write, "get_db", lambda: _Conn(cur))

    r = agent_write.desfazer(10)
    assert r["status"] == "revertido"
    assert any("UPDATE board_hunt_frentes SET" in s for s in cur.sqls)
    # Reverter também é mexer na linha: o frescor tem que apontar pra agora, não
    # voltar pro timestamp que a escrita desfeita substituiu.
    assert any("atualizado_em = now()" in s for s in cur.sqls)


def test_desfazer_duas_vezes_e_inocuo(monkeypatch):
    from services import agent_write
    cur = _Cur(linha_existente={
        "id": 11, "tabela": "tasks", "registro_id": 5,
        "valor_anterior": None, "desfeito_em": "2026-08-10 12:00:00",
    })
    monkeypatch.setattr(agent_write, "get_db", lambda: _Conn(cur))
    assert agent_write.desfazer(11)["status"] == "ja_desfeito"


def test_dict_vira_json_antes_do_banco(aw):
    """`psycopg2` não adapta `dict`: manda `can't adapt type 'dict'`.

    `registrar_nota_projeto` aceita `metadata`, que é `jsonb` — então TODA nota
    com metadados falhava, e falhava calada, porque até 11/08 o motivo da recusa
    não saía do processo. Apareceu na primeira rodada em que a recusa passou a
    dizer por quê: o regulamento do concurso de café, com `wa_attachments#1957`
    de procedência, foi recusado por isto.
    """
    mod, cur = aw
    mod.escrever(
        "registrar_nota_projeto",
        {"project_id": 28, "conteudo": "regulamento recebido",
         "metadata": {"fonte": "wa_attachments#1957", "tags": ["cafe"]}},
        motivo="o anexo traz cronograma e taxas que a frente precisa",
        confianca=0.95,
    )
    i = next(n for n, s in enumerate(cur.sqls) if "INSERT INTO project_notes" in s)
    enviado = [p for p in cur.params[i] if isinstance(p, str) and "wa_attachments" in p]
    assert enviado, "o metadata não chegou ao banco"
    # Precisa ser JSON — não repr de Python, que é o que `str(dict)` produz e o
    # que `jsonb` recusa.
    assert json.loads(enviado[0])["fonte"] == "wa_attachments#1957"
    assert "'" not in enviado[0].split(":")[0], "mandou repr de Python, não JSON"


def test_livro_razao_guarda_o_dict_como_json(aw):
    """O ledger tem que ser relegível por máquina: `str(dict)` gravaria aspas
    simples e `None` no lugar de `null` — algo que não é JSON."""
    mod, cur = aw
    mod.escrever(
        "registrar_nota_projeto",
        {"project_id": 28, "conteudo": "x", "metadata": {"fonte": "wa#1", "n": None}},
        motivo="registro do fato interpretado", confianca=0.95,
    )
    i = next(n for n, s in enumerate(cur.sqls) if "INSERT INTO agent_writes" in s)
    novo = json.loads(next(p for p in cur.params[i]
                           if isinstance(p, str) and "wa#1" in p))
    assert novo["metadata"] == {"fonte": "wa#1", "n": None}, (
        "o livro-razão guardou o metadata achatado em texto")


def test_tabelas_proibidas_ficam_fora_da_lista():
    """contacts/messages/users não entram por decisão, não por esquecimento:
    apagar ficha é irreversível e mensagem é o registro do que aconteceu — a
    camada interpreta, não reescreve."""
    from services import agent_write
    tabelas = {op.tabela for op in agent_write.OPERACOES.values()}
    for proibida in ("contacts", "messages", "whatsapp_messages", "users", "system_memories"):
        assert proibida not in tabelas, f"{proibida} entrou na lista fechada"


def test_nenhuma_operacao_de_delete():
    from services import agent_write
    tipos = {op.tipo for op in agent_write.OPERACOES.values()}
    assert tipos <= {"insert", "update"}, f"tipo destrutivo na lista: {tipos}"


# ───────────────────── frescor: o UPDATE carimba `atualizado_em` ─────────────────────
#
# Medido em 17/08/2026: o item 10 do RACI do Jabô foi para `em_andamento` pela
# camada em 16/08 12:45 e seguia com `atualizado_em = 2026-07-29 19:20`. O default
# `CURRENT_TIMESTAMP` da coluna só vale no INSERT — num UPDATE ninguém o reavalia.
# Quem paga é quem lê o frescor: `generate_raci_report` separa "ninguém arregaçou"
# de "tem movimento mas falta entregar" por um cooldown de 72h sobre esse campo, e
# apresentava como abandonada uma linha que a camada tinha acabado de mexer.

def test_update_carimba_atualizado_em(monkeypatch):
    from services import agent_write as mod
    cur = _Cur(linha_existente={"status": "pendente", "concluido_em": None,
                                "concluido_em_fonte": None})
    monkeypatch.setattr(mod, "get_db", lambda: _Conn(cur))
    mod.escrever(
        "atualizar_status_raci", {"status": "em_andamento"},
        motivo="a Andressa reportou no grupo que as amostras foram enviadas",
        confianca=0.9, registro_id=10,
    )
    update = next(s for s in cur.sqls if s.startswith("UPDATE public.raci_itens"))
    assert "atualizado_em = now()" in update, (
        "linha mudou de estado e o frescor ficou no valor antigo")


def test_carimbo_vai_no_mesmo_update(monkeypatch):
    """Uma segunda query deixaria uma janela em que a linha já mudou de status e
    ainda jura não ter sido tocada — curta, mas lida por quem passar no meio."""
    from services import agent_write as mod
    cur = _Cur(linha_existente={"fase": 2, "status": "ativo", "nota": None,
                                "piso_alvo": None})
    monkeypatch.setattr(mod, "get_db", lambda: _Conn(cur))
    mod.escrever(
        "atualizar_fase_frente", {"fase": 3},
        motivo="o contato aceitou conversar e a frente saiu de prospecção",
        confianca=0.9, registro_id=7,
    )
    updates = [s for s in cur.sqls if s.startswith("UPDATE board_hunt_frentes")]
    assert len(updates) == 1, f"carimbo saiu em query separada: {updates}"
    assert "fase = %s" in updates[0] and "atualizado_em = now()" in updates[0]


def test_insert_nao_carimba(aw):
    """No INSERT o default da coluna resolve. Carimbar aqui seria escrever uma
    coluna que a operação não declara — o que este módulo existe pra impedir."""
    mod, cur = aw
    mod.escrever(
        "criar_task_followup", {"titulo": "ligar pro Pretola", "project_id": 28},
        motivo="compromisso explícito no fio ('qua 12/08?')", confianca=0.9,
    )
    insert = next(s for s in cur.sqls if s.startswith("INSERT INTO tasks"))
    assert "atualizado_em" not in insert


def test_todo_update_tem_carimbo_declarado():
    """Controle sobre a LISTA, não sobre uma operação: operação de update nova
    entra sem carimbo e o defeito volta calado no alvo novo — foi assim que ele
    passou por `atualizar_fase_frente` e `atualizar_status_raci` ao mesmo tempo."""
    from services import agent_write
    sem_carimbo = [op.nome for op in agent_write.OPERACOES.values()
                   if op.tipo == "update" and not op.carimbo_atualizacao]
    assert not sem_carimbo, (
        f"update sem coluna de frescor declarada: {sem_carimbo} — se a tabela "
        f"realmente não tem a coluna, diga isso num comentário na operação")
