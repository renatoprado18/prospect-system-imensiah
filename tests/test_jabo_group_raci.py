"""Testes do caminho que deixa o grupo Governança Jabô atualizar o RACI do #28.

Decisão do Renato em 16/08/26: o RACI do Jabô não passa mais pelo clique dele.
Estes testes prendem as três coisas que, se soltarem, tornam o auto-apply
perigoso:

  - id alucinado pelo modelo NÃO vira escrita (escreveria em outro item do RACI,
    e o livro-razão registraria como legítima);
  - vocabulário de status é o de `raci_itens` (pt-BR), não o de `tasks`;
  - um item muda no máximo UMA vez por rodada, pela mensagem mais recente — a
    cascata que custou um fix em `cos_agent/run.py` no mesmo dia.

Rodar: .venv/bin/pytest tests/test_jabo_group_raci.py -v
"""
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import jabo_group_raci as J  # noqa: E402


def _j(msg_id, item_id, status, conf=0.9, corrige=False, motivo="m"):
    return {"msg_id": msg_id, "task_id": item_id, "novo_status": status,
            "confianca": conf, "corrige": corrige, "motivo": motivo}


class TestUmaPorItem:
    """Quatro mensagens da Andressa (04→12/08) propuseram a MESMA mudança no
    item 10. Sem dedup seriam 4 escritas no mesmo alvo."""

    def test_caso_real_do_backfill(self):
        vencedoras = J._uma_por_item([
            _j(103209, 10, "em_andamento", 0.85),
            _j(106149, 10, "em_andamento", 0.85),
            _j(313381, 10, "em_andamento", 0.80),
            _j(341824, 10, "em_andamento", 0.85),
        ])
        assert list(vencedoras) == [10]
        assert vencedoras[10]["msg_id"] == 341824, "vence a MAIS RECENTE"

    def test_mais_recente_vence_mesmo_com_confianca_menor(self):
        """Aqui a ordem é cronologia real, não acidente de iteração: a mensagem
        de hoje sabe o que a de ontem sabia, e mais. É o oposto do desempate do
        `cos_agent`, e a diferença é deliberada."""
        v = J._uma_por_item([_j(1, 10, "concluido", 0.95), _j(2, 10, "em_andamento", 0.78)])
        assert v[10]["msg_id"] == 2

    def test_itens_distintos_nao_competem(self):
        v = J._uma_por_item([_j(1, 10, "em_andamento"), _j(2, 11, "concluido")])
        assert set(v) == {10, 11}

    def test_sem_item_ou_sem_status_nao_entra(self):
        assert J._uma_por_item([_j(1, None, "concluido"), _j(2, 10, None)]) == {}


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return {"content": [{"text": self._p}], "usage": {}}


class TestValidacaoDoJulgamento:
    """O filtro entre o modelo e o banco. `raci_smart_updates` já registrou em
    08/06 que o modelo às vezes devolve id inventado."""

    def _rodar(self, payload, monkeypatch, mensagens=None, itens=None):
        mensagens = mensagens or [{"id": 500, "timestamp": datetime(2026, 8, 12, 10, 0),
                                   "sender_name": "Andressa", "content": "Amostra enviada"}]
        itens = itens or [{"id": 10, "acao": "Envio de amostras", "status": "pendente",
                           "responsavel_r": "Andressa"}]

        class _Cli:
            async def __aenter__(self_): return self_
            async def __aexit__(self_, *a): return False
            async def post(self_, *a, **k): return _FakeResp(payload)

        monkeypatch.setattr(J.httpx, "AsyncClient", lambda **k: _Cli())
        monkeypatch.setattr(J, "ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(J.llm_usage, "record_response", lambda *a, **k: None)
        import asyncio
        return asyncio.run(J._julgar(mensagens, itens))

    def test_item_alucinado_e_zerado(self, monkeypatch):
        out = self._rodar('[{"msg_id":500,"item_id":99999,"novo_status":"concluido",'
                          '"confianca":0.95,"corrige":false,"motivo":"x"}]', monkeypatch)
        assert out[0]["task_id"] is None, "id fora do contexto não pode virar escrita"

    def test_msg_fora_do_contexto_e_descartada(self, monkeypatch):
        out = self._rodar('[{"msg_id":123456,"item_id":10,"novo_status":"concluido",'
                          '"confianca":0.9,"corrige":false,"motivo":"x"}]', monkeypatch)
        assert out == []

    def test_status_em_ingles_e_recusado(self, monkeypatch):
        """`raci_itens` fala pt-BR. 'completed' estouraria o CHECK do banco — ou
        pior, passaria significando outra coisa (o erro `active`×`ativo`)."""
        out = self._rodar('[{"msg_id":500,"item_id":10,"novo_status":"completed",'
                          '"confianca":0.9,"corrige":false,"motivo":"x"}]', monkeypatch)
        assert out[0]["novo_status"] is None

    def test_eco_do_status_atual_nao_vira_escrita(self, monkeypatch):
        out = self._rodar('[{"msg_id":500,"item_id":10,"novo_status":"pendente",'
                          '"confianca":0.9,"corrige":false,"motivo":"x"}]', monkeypatch)
        assert out[0]["novo_status"] is None, "propor o que já está lá é eco"

    def test_correcao_e_preservada(self, monkeypatch):
        out = self._rodar('[{"msg_id":500,"item_id":10,"novo_status":"em_andamento",'
                          '"confianca":0.9,"corrige":true,"motivo":"na verdade nao enviei"}]',
                          monkeypatch)
        assert out[0]["corrige"] is True

    def test_json_lixo_nao_derruba_a_rodada(self, monkeypatch):
        assert self._rodar("desculpe, não consegui", monkeypatch) == []

    def test_sem_api_key_devolve_vazio(self, monkeypatch):
        monkeypatch.setattr(J, "ANTHROPIC_API_KEY", "")
        import asyncio
        assert asyncio.run(J._julgar([{"id": 1, "timestamp": datetime(2026, 8, 1),
                                       "sender_name": "x", "content": "y"}], [])) == []


class TestContratoDaOperacao:
    def test_escreve_em_raci_itens_e_nao_em_tasks(self):
        """A peça que a executora lê vem de `raci_itens`; os 10 itens do #28 têm
        `task_id` nulo. Escrever em `tasks` teria sucesso e efeito nenhum."""
        from services import agent_write
        op = agent_write.OPERACOES["atualizar_status_raci"]
        assert op.tabela == "public.raci_itens"
        assert op.tipo == "update"

    def test_notas_fora_da_operacao(self):
        """Campo de texto curado sobrescrito inteiro foi o dano de 14/08."""
        from services import agent_write
        assert "notas" not in agent_write.OPERACOES["atualizar_status_raci"].campos

    def test_procedencia_faz_parte_da_operacao(self):
        from services import agent_write
        assert "concluido_em_fonte" in agent_write.OPERACOES["atualizar_status_raci"].campos
