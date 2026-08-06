"""Testes do casamento task→contato pelo título (06/08/2026).

Vínculo errado é PIOR que vínculo ausente: ele faz o check F fechar a tarefa
errada, e aí o sistema mente com confiança. A primeira versão ligou
"Cobertura de recepção/secretária" ao contato *Recepção Buddha Spa* e
"contatar torrefação indicada em Portugal" à *Torrefação Muzambinho* — que
fica em Minas. Dois falsos positivos em 15 (13%).

O corte que resolveu: nome próprio no título vem CAPITALIZADO; substantivo
comum, não. Estes testes travam isso e a regra de ambiguidade.

Rodar: .venv/bin/python -m pytest tests/test_tasks_liga_contato.py -v
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# O script é executável de linha de comando, não um módulo do pacote.
_spec = importlib.util.spec_from_file_location(
    "tasks_liga_contato", os.path.join(_ROOT, "scripts", "tasks_liga_contato.py"))
liga = importlib.util.module_from_spec(_spec)
sys.modules["tasks_liga_contato"] = liga
_spec.loader.exec_module(liga)


ANDRESSA = {"id": 25737, "nome": "Andressa Santos", "papel": "andressa"}
PRISCILA = {"id": 4734, "nome": "Priscila Aquino", "papel": "priscila_contadora"}
RECEPCAO = {"id": 4873, "nome": "Recepção Buddha Spa Club", "papel": None}
TORREFA = {"id": 5843, "nome": "Torrefação Muzambinho", "papel": None}
NICK = {"id": 26443, "nome": "Nick Salussolia", "papel": None}
EDUARDO_A = {"id": 1, "nome": "Eduardo Amiralian", "papel": None}
EDUARDO_M = {"id": 2, "nome": "Eduardo Marson Ferreira", "papel": None}

EXEC = [ANDRESSA, PRISCILA]
OUTROS = [RECEPCAO, TORREFA, NICK, EDUARDO_A, EDUARDO_M]


def test_substantivo_comum_nao_vira_contato():
    """Os dois falsos positivos reais de 06/08."""
    c, motivo = liga.acha(
        "Cobertura de recepção/secretária — saída da Veridiana", EXEC, OUTROS)
    assert c is None, f"ligou substantivo comum: {motivo}"

    c, _ = liga.acha(
        "[Jabô] Contatar torrefação indicada em Portugal + alinhar pendências", EXEC, OUTROS)
    assert c is None, "torrefação de Portugal não é a Torrefação Muzambinho"


def test_nome_proprio_capitalizado_casa():
    c, motivo = liga.acha(
        "[Luminosità #48] Aguardar retorno do Nick sobre o modelo", EXEC, OUTROS)
    assert c is NICK and "Nick" in motivo


def test_executor_tem_precedencia():
    """Título com executor e contraparte: quem EXECUTA é o elo que fecha —
    foi o caso #999562, em que a Andressa pagou e a task ficou aberta."""
    c, motivo = liga.acha(
        "[Despesas/Andressa] Cancelar assinatura Poli Angels na VINDI", EXEC, OUTROS)
    assert c is ANDRESSA and "executor" in motivo


def test_homonimo_nao_vira_palpite():
    """Nove Eduardos no CRM. Escolher um seria adivinhar."""
    c, motivo = liga.acha("Retornar ao Eduardo sobre a proposta", EXEC, OUTROS)
    assert c is None
    assert "AMBÍGUO" in motivo


def test_nome_curto_nao_casa_dentro_de_palavra():
    """'Ana' em 'Analisar' — o piso de 4 letras veio de um falso positivo real."""
    ana = {"id": 9, "nome": "Ana Paula", "papel": None}
    c, _ = liga.acha("Analisar proposta da Vallen", EXEC, OUTROS + [ana])
    assert c is None


def test_sem_nenhum_nome_devolve_motivo():
    c, motivo = liga.acha("Revisar análise mensal 02/07-01/08", EXEC, OUTROS)
    assert c is None and "nenhum contato" in motivo
