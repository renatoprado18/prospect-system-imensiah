"""
Extracao do TEOR das decisoes num alerta processual (19/10/26).

Medido nos 30 alertas de 30 dias: **7 com teor**, 6 sem teor de verdade
(andamento automatico — nao houve decisao) e 15 do Jusbrasil, que **nao manda
teor nenhum**, so aviso + link.

Prova:
  (a) 🔴 OS DOIS FORMATOS do Astrea — a 1a versao conhecia so `Teor do ato:` e
      achou teor em 2 de 13, ignorando 11 corpos de ate 5.090 chars que usam
      `Conteudo:`. Cobertura baixa de parser e hipotese sobre o PARSER antes de
      ser conclusao sobre a FONTE;
  (b) 🔴 O ALCANCE sai LITERAL, nao tokenizado: "Renato de Faria E Almeida
      Prado" tem a conjuncao DENTRO do nome e "Framboesa Engenharia,
      Arquitetura e Design" tem a virgula dentro. Duas tentativas de split
      falharam em sentidos opostos (inventar parte / omitir parte) — num ato
      que bloqueia conta, os dois sao inaceitaveis;
  (c) `Parte: X Polo: ATIVO|PASSIVO` e extraido (identificacao oficial);
  (d) a traducao por IA do Astrea fica em campo SEPARADO do teor — e
      interpretacao, nao o ato;
  (e) ausencia de teor sai com MOTIVO, nunca como silencio;
  (f) os sinais de triagem apontam o que exige reacao.

Rodar: .venv/bin/python -m pytest tests/test_processo_teor.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services.processo_teor import extrair, menciona  # noqa: E402

ASTREA = "astrea@aurum.com.br"
JUSBRASIL = "nao-responda@jusbrasil.com.br"

# Corpo REAL (recortado) do formato "movimentacao" — usa `Teor do ato:`
CORPO_MOVIMENTACAO = """
CARAMBOLA TECNOLOGIA LTDA,
Seu processo teve movimentação no tribunal.
Nk Parking SA x Pare Net Estacionamentos Serviços e Comercio Ltda
Instância: 1º Grau
Nome da ação: CUMPRIMENTO DE SENTENÇA EXECUÇÃO DE HONORÁRIOS
Número: 0012411-71.2020.8.26.0100
Juízo: 1ª VARA EMPRESARIAL E CONFLITOS DE ARBITRAGEM Foro Central Cível
Tradução do Juridiquês
Gerada por Inteligência Artificial*
O juiz deferiu um pedido para bloquear valores financeiros.
Entendi
Movimentação oficial
Disponibilizada pelo tribunal
Remetido ao DJE
Relação: 2502/2026
Teor do ato: Vistos. Defiro o pedido formulado pela exequente na petição
sigilosa. Proceda-se ao bloqueio de ativos financeiros, por meio do sistema
SISBAJUD, na modalidade de teimosinha, pelo prazo de 30 dias, até o limite do
débito executado, em nome de Renato de Faria e Almeida Prado, Gustavo Glasser
Peluso, Alexandro Bereska, Carambola Tecnologia Ltda. e Framboesa Engenharia,
Arquitetura e Design Ltda. Quanto a Alexandro Bereska, deverá ser considerado
o CPF nº 219.296.928-70.
"""

# Corpo REAL (recortado) do formato "publicacao" — usa `Conteudo:`
CORPO_PUBLICACAO = """
Olá Renato de Faria e Almeida Prado,
Seu processo teve 1 publicação(ões):
15/10/2026
Publicação
Processo: 0012411-71.2020.8.26.0100
Órgão: Foro Central Cível - 1ª VARA EMPRESARIAL E CONFLITOS DE ARBITRAGEM
Data de disponibilização: 15/10/2026
Tipo de comunicação: Intimação
Meio: Diário de Justiça Eletrônico Nacional
Inteiro teor: https://www.dje.tjsp.jus.br
Parte: NK PARKING S/A Polo: ATIVO
Parte: ALEXANDRO BERESKA Polo: PASSIVO
Parte: CARAMBOLA TECNOLOGIA LTDA Polo: PASSIVO
Parte: RENATO DE FARIA E ALMEIDA PRADO Polo: PASSIVO
Advogado: MARCELLO ANTONIO FIORE - OAB SP-123734
Classe: CUMPRIMENTO PROVISóRIO DE DECISãO
Conteúdo: Processo 0012411-71.2020.8.26.0100 (processo principal
1014017-57.2019.8.26.0002) - Vistos. Defiro o bloqueio de ativos financeiros
via SISBAJUD, modalidade teimosinha, pelo prazo de 30 dias.
"""

CORPO_JUSBRASIL = """
Jusbrasil
Olá, Renato! Seu processo possui uma nova informação sobre uma decisão.
Acesse para conferir mais detalhes e entender o que está acontecendo.
Jose Reinaldo Michel x Makerfest Tecnologia Ltda. TJSP · Central
Acessar processo
"""

CORPO_ANDAMENTO = """
Olá Renato de Faria e Almeida Prado,
Seu processo teve 3 andamento(s) automático(s)
23/09/2026
Processo encaminhado para o Magistrado
22/09/2026
"""


# --------------------------------------------- (a) 🔴 OS DOIS FORMATOS --------

def test_formato_movimentacao_tem_teor():
    d = extrair("Seu processo teve 5 movimentações", CORPO_MOVIMENTACAO, ASTREA)
    assert d["tem_teor"] is True
    assert d["numero"] == "0012411-71.2020.8.26.0100"
    assert d["acao"] == "CUMPRIMENTO DE SENTENÇA EXECUÇÃO DE HONORÁRIOS"
    assert "bloqueio de ativos financeiros" in d["movimentacoes"][0]["teor"]


def test_formato_publicacao_tem_teor():
    """🔴 O formato que a 1a versao IGNORAVA — 11 de 13 e-mails."""
    d = extrair("Seu processo teve 1 Publicação", CORPO_PUBLICACAO, ASTREA)
    assert d["tem_teor"] is True, "o formato `Conteudo:` voltou a ser ignorado"
    assert d["numero"] == "0012411-71.2020.8.26.0100"
    assert d["processo_principal"] == "1014017-57.2019.8.26.0002"
    assert d["tipo_comunicacao"] == "Intimação"
    assert d["inteiro_teor_url"].startswith("https://")


# ------------------------------------------ (b) 🔴 O NOME DO RENATO ----------

def test_alcance_sai_literal_e_completo():
    """A frase como o juiz escreveu: todas as 5 partes, nenhuma inventada,
    nenhuma omitida. É o que substitui o split que falhou duas vezes."""
    d = extrair("x", CORPO_MOVIMENTACAO, ASTREA)
    alc = d["movimentacoes"][0]["alcance_literal"]
    assert alc is not None
    for parte in ("Renato de Faria e Almeida Prado", "Gustavo Glasser Peluso",
                  "Alexandro Bereska", "Carambola Tecnologia Ltda",
                  "Framboesa Engenharia, Arquitetura e Design Ltda"):
        assert parte in alc, f"{parte!r} ficou de fora de: {alc!r}"


def test_menciona_acerta_nome_com_conjuncao_dentro():
    """A pergunta que importa ("este bloqueio me alcança?") se responde por
    substring normalizada, sem tokenizar — justamente porque o nome tem " e "."""
    d = extrair("x", CORPO_MOVIMENTACAO, ASTREA)
    teor = d["movimentacoes"][0]["teor"]
    assert menciona(teor, "Renato de Faria e Almeida Prado") is True
    assert menciona(teor, "RENATO DE FARIA E ALMEIDA PRADO") is True
    assert menciona(teor, "Framboesa Engenharia") is True
    assert menciona(teor, "Fulano de Tal") is False


# ------------------------------------------------- (c) partes com polo -------

def test_partes_com_polo_sao_extraidas():
    d = extrair("x", CORPO_PUBLICACAO, ASTREA)
    por_nome = {p["nome"]: p["polo"] for p in d["partes"]}
    assert por_nome["NK PARKING S/A"] == "ATIVO"
    assert por_nome["RENATO DE FARIA E ALMEIDA PRADO"] == "PASSIVO"
    assert por_nome["CARAMBOLA TECNOLOGIA LTDA"] == "PASSIVO"


# --------------------------------- (d) traducao IA separada do teor ----------

def test_traducao_ia_nao_se_mistura_ao_teor():
    """A traducao e do Astrea e e INTERPRETACAO. Citar ela como o ato seria
    atribuir ao juiz o que a IA do fornecedor escreveu."""
    d = extrair("x", CORPO_MOVIMENTACAO, ASTREA)
    mv = d["movimentacoes"][0]
    assert "Vistos." in mv["teor"]
    assert "O juiz deferiu um pedido" not in mv["teor"]
    assert mv["traducao_ia"] and "O juiz deferiu" in mv["traducao_ia"]


# ----------------------------------------- (e) ausencia COM motivo -----------

def test_jusbrasil_declara_que_nao_tem_teor():
    d = extrair("Nova decisão no processo", CORPO_JUSBRASIL, JUSBRASIL)
    assert d["tem_teor"] is False
    assert d["motivo_sem_teor"] and "Jusbrasil" in d["motivo_sem_teor"]


def test_andamento_automatico_nao_tem_teor_e_isso_e_real():
    """Ausencia de verdade: nao houve decisao, logo nao ha teor. Diferente de
    furo de parser — e por isso o motivo e distinto do do Jusbrasil."""
    d = extrair("Seu processo teve 3 andamento(s)", CORPO_ANDAMENTO, ASTREA)
    assert d["tem_teor"] is False
    assert d["motivo_sem_teor"] and "Jusbrasil" not in d["motivo_sem_teor"]


def test_corpo_vazio_nao_quebra():
    d = extrair("", "", "")
    assert d["tem_teor"] is False
    assert d["movimentacoes"] == []


# ------------------------------------------------- (f) sinais de triagem -----

def test_sinais_apontam_o_que_exige_reacao():
    d = extrair("x", CORPO_MOVIMENTACAO, ASTREA)
    sinais = d["movimentacoes"][0]["sinais"]
    assert any("SISBAJUD" in s for s in sinais)
    assert any("bloqueio" in s for s in sinais)


CORPO_BANAL = """
CARAMBOLA TECNOLOGIA LTDA,
Seu processo teve movimentação no tribunal.
Número: 0012411-71.2020.8.26.0100
Movimentação oficial
Disponibilizada pelo tribunal
Juntada de Petição
Teor do ato: Vistos. Junte-se a petição aos autos e aguarde-se o cumprimento
do que já foi determinado anteriormente.
"""


def test_ato_banal_nao_gera_sinal_critico():
    """Controle NEGATIVO dos sinais. ⚠️ A 1a versao deste teste fazia `replace`
    de uma LINHA do corpo crítico e o resto do teor continuava falando de
    bloqueio — passava sinal e o teste acusava o código. Corpo banal inteiro."""
    d = extrair("x", CORPO_BANAL, ASTREA)
    assert d["tem_teor"] is True
    assert d["movimentacoes"][0]["sinais"] == [], d["movimentacoes"][0]["sinais"]
