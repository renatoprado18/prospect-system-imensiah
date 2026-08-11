"""O verificador de procedência tem que morder — e nos dois sentidos.

CONTEXTO. Em 10/08/26 o Renato corrigiu a sessão CoS quatro vezes na mesma
conversa, sempre afirmação sobre ele escrita a partir do que soava bem, com a
evidência contrária disponível. A regra virou memo
([[feedback_procedencia_em_peca_assinada]]) e o memo virou este instrumento,
pelo motivo que ele mesmo registra: regra que depende de lembrar falha; regra
que vira instrumento morde.

O QUE ESTES TESTES GUARDAM, em ordem de gravidade:
  1. afirmação factual SEM ponteiro é acusada — é o furo que custou as quatro
     correções;
  2. afirmação COM fonte não é acusada — verificador que acusa tudo é ignorado
     em uma semana, e aí não protege nada ([[feedback_notificacao_valor_medido]]);
  3. `[inferido]` passa — é a segunda saída legítima da regra: sem fonte não
     some na prosa, fica MARCADO;
  4. texto sem fato nenhum devolve AMARELO, não verde — "não achei o que medir"
     não é "está tudo certo" ([[feedback_medidor_que_nao_mede_a_si_mesmo]]).

Rodar: .venv/bin/python -m pytest tests/test_verifica_procedencia.py -v
"""
import importlib.util
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "_verifica_proc", _ROOT / "scripts" / "verifica_procedencia.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["_verifica_proc"] = m
    spec.loader.exec_module(m)
    return m


V = _mod()


def _frases(achados):
    return " | ".join(a["frase"] for a in achados)


# ---------------------------------------------------- 1. acusa o que importa --

def test_acusa_numero_sem_fonte():
    """O caso real: '115%' é o número que ele leva ao Gui."""
    achados, total = V.analisar("A companhia cresceu 115% nesse período.")
    assert total == 1 and len(achados) == 1
    assert "percentual" in achados[0]["sinais"]


def test_acusa_feito_biografico_sem_fonte():
    achados, _ = V.analisar("Fundei a Natique em 1996 e vendi a companhia à Osborne.")
    assert len(achados) == 1
    assert {"ano", "feito"} <= set(achados[0]["sinais"])


def test_acusa_contagem_e_periodo():
    """'62 itens desde abril' é a prova que sustenta o posicionamento — e foi
    escrita a partir de um board que não tinha data de conclusão nenhuma."""
    achados, _ = V.analisar("São 62 itens desde abril, 52 concluídos.")
    assert len(achados) == 1


def test_acusa_absoluto():
    """'sempre', 'nunca', 'único' são os que passam por serem retóricos."""
    achados, _ = V.analisar("Nunca precisei operar no lugar do executivo da empresa.")
    assert len(achados) == 1 and "absoluto" in achados[0]["sinais"]


# ------------------------------------------- 2. NÃO acusa o que tem ponteiro --

def test_nao_acusa_com_id_de_registro():
    achados, total = V.analisar("O encontro ficou para 13/08 (messages#27742).")
    assert total == 1 and achados == []


def test_frase_sem_sinal_mensuravel_nao_e_medida():
    """A heurística mede o que tem ÂNCORA — número, data, feito, absoluto.

    'Ele confirmou o horário' é afirmação sobre o mundo, mas sem nada a conferir
    contra fonte; marcá-la encheria a lista de prosa e mataria a ferramenta pelo
    uso. É limitação declarada, não descuido: o script cobre o que soa bem
    PORQUE traz número, que é o que deu errado em 10/08.
    """
    achados, total = V.analisar("O Pretola confirmou o horário do encontro.")
    assert total == 0 and achados == []


def test_nao_acusa_com_fonte_nomeada():
    achados, _ = V.analisar("São 62 itens desde abril, 52 concluídos [fonte: RACI Vallen].")
    assert achados == []


def test_nao_acusa_com_minuto_de_audio():
    achados, _ = V.analisar("Ele disse que faz sentido para a casa (áudio 14:32).")
    assert achados == []


def test_nao_acusa_com_linha_do_cv():
    achados, _ = V.analisar("Fiquei no conselho de 2013 a 2017 (CV, linha 12).")
    assert achados == []


# --------------------------------------------- 3. marcação explícita PASSA ---

def test_inferido_passa():
    """A 2ª saída legítima: sem fonte não some, fica marcado."""
    for marca in ("[inferido]", "[sem base]", "[a confirmar]", "[hipótese]"):
        achados, total = V.analisar(f"O Panico ofereceu 15% dos fees {marca}.")
        assert total == 1, marca
        assert achados == [], f"{marca} deveria passar — é o que a regra pede"


# ------------------------------------------- 4. o medidor se mede a si mesmo --

def test_texto_sem_fato_nao_e_aprovacao():
    """Zero factuais = 'não achei o que medir', não 'está tudo certo'.

    Sem isto, arquivo vazio, HTML que não foi limpo ou seletor errado sairiam
    verdes — o medidor certificando o que nunca leu.
    """
    achados, total = V.analisar("Entrego cadência entre as reuniões, com método.")
    assert total == 0 and achados == []


def test_saida_amarela_quando_nada_foi_medido(capsys, monkeypatch):
    # ⚠️ O texto de teste não pode conter "nenhum"/"sempre"/"único": eles são
    # sinal de ABSOLUTO e a primeira versão deste teste caiu por isso — o
    # verificador acusou a frase que dizia não ter fato.
    monkeypatch.setattr(sys, "argv", ["v", "--texto", "Entrego cadência entre as reuniões."])
    codigo = V.main()
    saida = capsys.readouterr().out
    assert codigo == 1, "nada medido não pode sair 0 — parece aprovação"
    assert "nada foi verificado" in saida


def test_reprova_com_exit_1(capsys, monkeypatch):
    """Verificador que só sussurra é o mesmo que não existir."""
    monkeypatch.setattr(sys, "argv", ["v", "--texto", "A companhia cresceu 115% no período."])
    assert V.main() == 1
    assert "SEM ponteiro" in capsys.readouterr().out


def test_aprova_com_exit_0(capsys, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["v", "--texto", "A companhia cresceu 115% no período (CV, linha 18)."])
    assert V.main() == 0
    assert "todas com fonte" in capsys.readouterr().out


# ------------------------------------------------------------- estrutura ------

def test_titulo_e_tabela_nao_viram_achado():
    """Ruído estrutural mata a ferramenta pelo uso: 30 achados de menu e o
    autor para de rodar."""
    achados, _ = V.analisar("## Resultados de 2026\n|---|---|\n----\n")
    assert achados == []


def test_le_html_sem_marcar_a_tag():
    achados, _ = V.analisar("<p>A companhia cresceu <strong>115%</strong> no período.</p>")
    assert len(achados) == 1
    assert "<" not in achados[0]["frase"], "tag vazou pro achado"


def test_cada_frase_e_um_achado_separado():
    """Juntar três afirmações num achado só deixa o autor sem saber qual citar."""
    achados, _ = V.analisar(
        "Fundei em 1996. Vendi à Osborne. A companhia cresceu 115%.")
    assert len(achados) == 3
