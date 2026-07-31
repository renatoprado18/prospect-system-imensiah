"""
Vazamento de assunto ENTRE frentes na camada CoS (achado e corrigido em 30/07/26).

O defeito: `_gather_frente` puxava a conversa INTEIRA de cada membro, então quem
participa de várias frentes carregava tudo o que disse para todas elas.

Provado no mesmo dia, com a camada já rodando: a nota `estado_cos` escrita no
projeto **#38 (FAAP — Manuela)** registrava como movimento da FAAP o pagamento de
**R$ 5.000 do título do Club Athletico Paulistano do FILHO (projeto #49)** — porque
o Orestes é membro dos dois. Quem lesse a página do projeto concluiria que a
mensalidade estava sendo paga. Alcance medido: **15 das 31 frentes ativas** têm ao
menos um membro compartilhado (Renato em 11, Andressa em 4, Orestes em 4).

Dois consertos, nas duas pontas:

(1) **Usar o rótulo que já existe.** Duas das quatro mensagens vazadas (25826/25827,
    as que carregam "título do DAP" e os dados bancários) JÁ estavam ligadas ao #49
    em `message_project_links`, e a camada as exibiu no #38 sem nunca consultar a
    etiqueta. Agora mensagem atribuída EXCLUSIVAMENTE a outra frente não entra.
    Não é palpite: é decisão que o roteador já tomou e conferiu por LLM.

(2) **Avisar quando o participante é compartilhado.** O modelo CONSEGUE separar
    quando sabe: em 30/07 ele acertou na frente #20 ("as conversas Renato–Orestes
    são sobre Phisalia, não têm relação com esta frente") e errou na #38, com o
    MESMO contato. Não era falta de capacidade, era falta de rótulo.

Bônus da mesma investigação: `_NOTE_CHARS` subiu de 500 para 2500. Medido — das 58
notas que a camada de fato lê, **42 eram cortadas em 500 (72%)**. O caso que provou
foi a nota #123 da FAAP: 708 chars, com "aulas começam 17/08" a 40 caracteres DEPOIS
do corte. A camada dizia honestamente "não posso afirmar" sobre um dado que estava
quatro palavras adiante no mesmo campo.

Rodar: PYTHONPATH=app python -m pytest tests/test_frente_review_vazamento_entre_frentes.py -v
"""
import inspect
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import frente_review as fr  # noqa: E402


def _base_gather(**over):
    g = {
        "project": {"id": 38, "nome": "FAAP — Manuela", "descricao": ""},
        "tasks": [], "members": [], "dms": [], "routed": [], "notes": [],
        "groups": [], "memories": [], "renato_outbound": [],
    }
    g.update(over)
    return g


class TestAvisoDeParticipanteCompartilhado:
    def test_marca_quem_esta_em_outra_frente(self):
        g = _base_gather(members=[
            {"contact_id": 4376, "nome": "Orestes Alves de Almeida Prado",
             "papel": "Responsável Financeiro",
             "outras_frentes": "Controle Mensal de Despesas · DAIA-C"},
        ])
        txt = fr._fmt_gather(g)
        assert "TAMBÉM participa de" in txt
        assert "Controle Mensal de Despesas" in txt

    def test_nao_marca_quem_so_esta_nesta(self):
        g = _base_gather(members=[
            {"contact_id": 4067, "nome": "Manuela", "papel": "filha",
             "outras_frentes": ""},
        ])
        txt = fr._fmt_gather(g)
        assert "Manuela" in txt
        assert "TAMBÉM participa de" not in txt

    def test_membro_sem_a_coluna_nao_quebra(self):
        """Retrocompat: gather antigo/injetado em teste não tem `outras_frentes`."""
        g = _base_gather(members=[{"contact_id": 1, "nome": "Fulano", "papel": "membro"}])
        txt = fr._fmt_gather(g)
        assert "Fulano" in txt

    def test_regra_esta_no_system_prompt(self):
        """Regra em comentário Python o modelo não lê —
        [[feedback_prompt_nao_le_comentario]]."""
        assert "PARTICIPANTE COMPARTILHADO" in fr._SYSTEM
        assert "TAMBÉM participa de" in fr._SYSTEM
        # a instrução operativa: só é evidência se menciona o objeto DESTA frente
        assert "menciona o objeto desta frente" in fr._SYSTEM


class TestFiltroAntiVazamento:
    """A regra: entra se ligada A ESTA frente, ou se não está ligada a nenhuma.
    Sai só quando o roteador a atribuiu EXCLUSIVAMENTE a outra."""

    def _sql(self):
        return inspect.getsource(fr._gather_frente) + inspect.getsource(
            fr._gather_renato_outbound)

    def test_as_duas_janelas_de_mensagem_filtram(self):
        """DMs de membro E outbound do Renato — ambas puxam por participação,
        ambas precisam do filtro."""
        assert self._sql().count("le.project_id = %s") == 2

    def test_mantem_o_que_esta_ligado_a_esta_frente(self):
        sql = self._sql()
        assert "EXISTS (SELECT 1 FROM message_project_links le" in sql

    def test_mantem_o_que_nao_esta_ligado_a_nada(self):
        """A maioria das mensagens não tem link. Se o filtro exigisse link,
        a camada ficaria cega — o remédio seria pior que a doença."""
        sql = self._sql()
        assert "NOT EXISTS (SELECT 1 FROM message_project_links lo" in sql
        assert "lo.project_id > 0" in sql

    def test_outras_frentes_vem_da_query_de_membros(self):
        src = inspect.getsource(fr._gather_frente)
        assert "outras_frentes" in src
        assert "p2.status = 'ativo'" in src, "frente encerrada não deve gerar aviso"


class TestCorteDeNota:
    def test_nota_nao_e_mais_cortada_em_500(self):
        """500 truncava 72% das notas lidas. O caso da FAAP perdia a data das
        aulas por 40 caracteres."""
        assert fr._NOTE_CHARS >= 2000, fr._NOTE_CHARS

    def test_alinhado_com_o_corte_de_memoria(self):
        """Mesmo raciocínio de 28/07 aplicado a notas — sem dois números
        divergentes pra mesma decisão."""
        assert fr._NOTE_CHARS == fr._MEM_CHARS

    def test_nota_dentro_do_corte_chega_inteira(self):
        corpo = "x" * 700  # o tamanho da nota #123 que perdeu a data
        g = _base_gather(notes=[{"titulo": "t", "conteudo": corpo,
                                 "criado_em": "2026-07-24"}])
        txt = fr._fmt_gather(g)
        assert "[…truncado]" not in txt
        assert corpo in txt

    def test_nota_gigante_ainda_trunca(self):
        """Notas-documento (4k-19k) são longas por natureza; truncar é correto."""
        g = _base_gather(notes=[{"titulo": "t", "conteudo": "y" * 9000,
                                 "criado_em": "2026-07-24"}])
        assert "[…truncado]" in fr._fmt_gather(g)
