"""
O parser de RACI era cego para o que o PROPRIO sistema escreve (31/08/26).

Medido no projeto #26 (Alba): 51 de 59 tasks sem `accountable_id`. Duas causas,
ambas mudas — a task so nascia sem dono, sem erro nenhum:

  1. TAG DE PROJETO. O sistema escreve `[Alba #26] SANDRA: enviar a DRE`, e o
     `^` do _PREFIX_PATTERN fazia o match falhar. Nas 6 tasks `delegated` do
     projeto, TODAS retornavam source='none'. Quem punha a tag era o mesmo
     sistema que depois nao conseguia ler o dono.

  2. CAIXA ALTA COM ACENTO. A classe interna era `[a-zA-Zà-ÿ]` — tem as
     minusculas acentuadas, nao as MAIUSCULAS (`À-Ý`). `SANDRA:` casava,
     `ANDRÉ:` nao. E caixa alta e a forma que o recap usa.

Rodar: PYTHONPATH=app python -m pytest tests/test_raci_prefixo_tag_e_caixa_alta.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.raci_parser import parse_raci, strip_tag_de_projeto  # noqa: E402


class TestTagDeProjetoNaoCega:
    def test_as_seis_delegated_reais_do_26(self):
        """Todas retornavam source='none' antes do conserto."""
        reais = [
            ("[Alba #26] SANDRA: agendar reunião com a Larisse (2M)", "SANDRA"),
            ("[Alba #26] SANDRA: enviar a DRE para o Guilherme", "SANDRA"),
            ("[Alba #26] SANDRA: enviar as perguntas preparatórias", "SANDRA"),
            ("[Alba #26] GUILHERME: enviar a DRE para o Israel e o André", "GUILHERME"),
            ("[Alba #26] GUILHERME: pedir que a Kelly documente processos", "GUILHERME"),
            ("[Alba #26] ANDRÉ: analisar os custos de TI da Alba", "ANDRÉ"),
        ]
        for titulo, esperado in reais:
            r = parse_raci(titulo, "")
            assert r.source == "prefix", titulo
            assert r.responsible == esperado, titulo
            assert not r.is_renato, titulo

    def test_tag_de_outro_projeto_tambem(self):
        r = parse_raci("[Assespro #12] ELIANE: concluir o diagnóstico", "")
        assert r.responsible == "ELIANE"

    def test_so_a_primeira_tag_e_so_no_inicio(self):
        assert strip_tag_de_projeto("[Alba #26] x") == "x"
        # tag no meio e conteudo, nao rotulo
        assert strip_tag_de_projeto("Sandra: ver [anexo 3]") == "Sandra: ver [anexo 3]"

    def test_sem_tag_continua_funcionando(self):
        assert parse_raci("Sandra: enviar a DRE", "").responsible == "Sandra"


class TestCaixaAltaComAcento:
    def test_andre_com_acento_em_caixa_alta(self):
        """O caso que falhava mesmo depois de tirar a tag."""
        assert parse_raci("ANDRÉ: analisar custos", "").responsible == "ANDRÉ"

    def test_outros_nomes_br_com_acento(self):
        for nome in ["JOSÉ", "INÊS", "ANTÔNIO", "VERÔNICA"]:
            r = parse_raci(f"{nome}: fazer algo", "")
            assert r.responsible == nome, nome

    def test_sem_acento_nunca_quebrou_e_segue_ok(self):
        assert parse_raci("ANDRE: analisar", "").responsible == "ANDRE"
        assert parse_raci("SANDRA: enviar", "").responsible == "SANDRA"

    def test_capitalizacao_normal_intacta(self):
        assert parse_raci("André: analisar", "").responsible == "André"


class TestNaoRegrediuOQueJaAcertava:
    def test_renato_continua_sendo_renato(self):
        r = parse_raci("Renato: preparar o comparativo", "")
        assert r.is_renato

    def test_multi_nome(self):
        r = parse_raci("Renato + Alexandre Medeiros: cruzar o questionário", "")
        assert r.source == "prefix"

    def test_frase_sem_dono_nao_inventa_prefixo(self):
        r = parse_raci("Reunir-se sexta 28/08 para definir o modelo", "")
        assert r.source == "none"

    def test_titulo_vazio_nao_quebra(self):
        assert parse_raci("", "").source == "none"
        assert strip_tag_de_projeto(None) == ""
