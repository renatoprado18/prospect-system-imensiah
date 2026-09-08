"""RACI completa publicada no grupo — o que ela traz e o banco nao tem (08/09/26).

Em 04/09 a Kelly Souza publicou no grupo da Alba uma RACI com 16 itens;
`raci_itens` do #26 tinha 6. Dez viviam so como mensagem, e um constava
`em_andamento` no banco havendo sido marcado concluido na peca — a CoS chegou a
cobrar do Renato algo ja feito.

O shadow processou a mensagem (`raci_processed_at` carimbado) e gerou ZERO
propostas: o vocabulario dele e add_note/update_status/update_prazo/complete,
todos sobre item que JA existe. Item inedito nao tinha como virar proposta,
entao sumiu calado. Este modulo faz o furo virar numero.

Rodar: PYTHONPATH=app python -m pytest tests/test_raci_publicada.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.raci_publicada import (  # noqa: E402
    auditar_texto,
    itens_nao_absorvidos,
    parece_raci_publicada,
    parse_itens_publicados,
)

# A peca REAL da Kelly (group_messages #1454869, 04/09/2026), reduzida mas com
# as tres armadilhas preservadas: bullet `*` em vez de `•`, travessao DENTRO da
# acao e prazo entre parenteses depois do responsavel.
PECA_KELLY = """Bom dia! Segue RACI atualizada

*RACI — Alba Consultoria*

*04/09/2026*

🔄 *Em andamento (3):*
* Business plan detalhado com 3 cenários (otimista/realista/pessimista) — Sandra (15/09)
* Opção pelo Simples em setembro (reversível até novembro) — manter a janela — Larisse (2emmes) (30/09)
* Enviar a proposta final do projeto Insi — Sandra – aguardando agenda da cliente

⏳ *Pendentes (1):*
* Agendar café presencial com Paulo Vasconcelos (professor do IBGC) — Sandra (05/09)

✅ *Concluídos (2):*
* Consolidar as respostas do questionário dos sócios — mapear convergências — Renato
* Passar à Sandra o contato de Michelle Skeff — Clarice
"""


class TestDeteccao:
    def test_reconhece_a_peca_real(self):
        assert parece_raci_publicada(PECA_KELLY)

    def test_mensagem_que_so_cita_raci_nao_conta(self):
        """`propose` ja trata reporte solto. Se a auditoria disparasse em
        qualquer texto com a palavra RACI, todo 'mando a RACI amanha' viraria
        um aviso — e o Renato aprenderia a ignorar o canal."""
        assert not parece_raci_publicada("Segue a RACI amanhã, pessoal. " * 20)

    def test_texto_curto_nao_conta(self):
        assert not parece_raci_publicada("*RACI — Alba*")
        assert not parece_raci_publicada(None)

    def test_exige_dois_buckets(self):
        """Um cabecalho e um bucket sozinho e mais provavelmente um recorte
        citado do que a peca inteira."""
        um_bucket = "*RACI — Alba Consultoria*\n\n🔄 *Em andamento (1):*\n* Fazer algo — Sandra\n" + "x" * 200
        assert not parece_raci_publicada(um_bucket)


class TestParse:
    def test_extrai_todos_os_itens_com_status_do_bucket(self):
        itens = parse_itens_publicados(PECA_KELLY)
        assert len(itens) == 6
        assert [i["status"] for i in itens] == (
            ["em_andamento"] * 3 + ["pendente"] + ["concluido"] * 2
        )

    def test_responsavel_vem_do_ULTIMO_travessao(self):
        """A acao costuma ter travessao proprio ('...até novembro) — manter a
        janela'). Partir pelo PRIMEIRO jogaria metade da acao no nome do
        responsavel, e o item nunca casaria com o banco."""
        itens = parse_itens_publicados(PECA_KELLY)
        simples = next(i for i in itens if "Simples em setembro" in i["acao"])
        assert simples["responsavel"] == "Larisse (2emmes)"
        assert simples["acao"].endswith("manter a janela")

    def test_prazo_sai_de_dentro_do_responsavel(self):
        itens = parse_itens_publicados(PECA_KELLY)
        bp = next(i for i in itens if i["acao"].startswith("Business plan"))
        assert bp["responsavel"] == "Sandra"
        assert bp["prazo"] == "15/09"

    def test_negrito_do_whatsapp_nao_vira_item(self):
        """`*RACI — Alba*` e negrito, nao bullet. O espaco depois do `*` e o
        que separa os dois — sem ele o cabecalho entraria como item."""
        acoes = [i["acao"] for i in parse_itens_publicados(PECA_KELLY)]
        assert not any(a.startswith("RACI") for a in acoes)
        assert not any("04/09/2026" in a for a in acoes)

    def test_aceita_bullet_do_sistema_e_do_humano(self):
        com_ponto = PECA_KELLY.replace("\n* ", "\n• ")
        assert len(parse_itens_publicados(com_ponto)) == 6


class TestNaoAbsorvidos:
    def test_banco_vazio_devolve_tudo(self):
        itens = parse_itens_publicados(PECA_KELLY)
        assert len(itens_nao_absorvidos(itens, [])) == 6

    def test_item_igual_nao_conta(self):
        itens = parse_itens_publicados(PECA_KELLY)
        fora = itens_nao_absorvidos(itens, [i["acao"] for i in itens])
        assert fora == []

    def test_reescrita_humana_nao_vira_item_novo(self):
        """CASO REAL: o #36 da Alba esta no banco como 'salário fixo ×
        dividendos' e foi publicado como 'salário-fixo vs. dividendos'. Sem
        tolerancia a reescrita, o mesmo item apareceria como inedito e o aviso
        nasceria com falso positivo — o jeito mais rapido de queimar o canal."""
        publicado = [{"acao": "Definir modelo de remuneração dos sócios (salário-fixo vs. dividendos)"}]
        banco = ["Definir modelo de remuneração dos sócios (salário fixo × dividendos)"]
        assert itens_nao_absorvidos(publicado, banco) == []

    def test_item_truncado_na_publicacao_casa_com_o_inteiro(self):
        """O preview corta acao longa com `…`. Truncado nao e inedito."""
        publicado = [{"acao": "Reunir com Alexandre Medeiros para cruzar o questionário dos sócios com o…"}]
        banco = ["Reunir com Alexandre Medeiros para cruzar o questionário dos sócios com o plano de viabilidade"]
        assert itens_nao_absorvidos(publicado, banco) == []

    def test_item_de_verdade_ausente_aparece(self):
        """O item real que sobrou depois da reconciliacao manual da CoS."""
        publicado = [{"acao": "Consolidar as respostas do questionário dos sócios — mapear convergências"}]
        banco = ["Reunir com Alexandre Medeiros para cruzar o questionário dos sócios com o plano de viabilidade",
                 "Workshop de viabilidade financeira + finalização dos questionários dos sócios"]
        fora = itens_nao_absorvidos(publicado, banco)
        assert len(fora) == 1

    def test_na_duvida_considera_absorvido(self):
        """Subcontar e o erro barato: um falso 'inedito' vira ruido no aviso;
        um falso 'ja tenho' so mantem o estado de hoje."""
        publicado = [{"acao": "Business plan detalhado com 3 cenários"}]
        banco = ["Business plan detalhado com 3 cenarios (otimista/realista/pessimista)"]
        assert itens_nao_absorvidos(publicado, banco) == []


class TestAuditoria:
    def test_resumo_completo(self):
        r = auditar_texto(PECA_KELLY, ["Passar à Sandra o contato de Michelle Skeff"])
        assert r["itens_publicados"] == 6
        assert r["itens_no_banco"] == 1
        assert r["total_nao_absorvidos"] == 5

    def test_texto_que_nao_e_raci_devolve_none(self):
        """None e nao dict-vazio: o chamador distingue 'nao era peca' de 'peca
        sem divergencia', e so a segunda merece contar como auditada."""
        assert auditar_texto("bom dia pessoal, tudo certo por aqui?", []) is None
