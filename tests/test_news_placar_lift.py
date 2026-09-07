"""O placar do NEWS certificava 93% de valor que não existia.

O CASO QUE ORIGINOU (06/09/2026). O degrau 5 ("seguidos de CONTATO em 7d")
imprimia 98 de 105 — 93% — e esse número foi lido como valor entregue pelos
watchers. A mesma regra rodada na janela ANTES do alerta dá 103 de 105 (98%):
MAIOR do que depois. O lift real é 1 contato novo contra 6 perdidos.

A régua perguntava "houve alguma mensagem outgoing para algum membro do projeto
nos 7 dias seguintes" — e em projeto vivo isso é sempre verdade, por motivos que
nada têm com a notícia. Emma: 56 de 56 nos dois lados.

O docstring do script já prometia "o contraste com a taxa de base"; o script
nunca o calculava. Medidor que promete denominador e não entrega certifica o que
nunca mediu.

Rodar: PYTHONPATH=app python -m pytest tests/test_news_placar_lift.py -v
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))


def _carrega():
    """Importa o script pelo caminho — `scripts/` não é pacote."""
    caminho = os.path.join(_ROOT, "scripts", "news_placar.py")
    spec = importlib.util.spec_from_file_location("news_placar", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_M = _carrega()
veredito_do_lift = _M.veredito_do_lift


def _hit(depois, antes):
    return {"virou_contato": depois, "ja_havia_contato": antes}


def test_o_numero_real_de_producao_nao_e_lido_como_valor():
    """98/105 depois, 103/105 antes: a régua não pode dizer que o alerta funcionou."""
    linhas = ([_hit(True, True)] * 97 +      # já falava antes e continuou
              [_hit(True, False)] * 1 +      # o único ganho real
              [_hit(False, True)] * 6 +      # existia contato e sumiu
              [_hit(False, False)] * 1)
    v = veredito_do_lift(linhas)
    assert v["n"] == 105
    assert v["depois"] == 98 and v["antes"] == 103
    assert round(v["taxa_depois"]) == 93
    assert round(v["taxa_antes"]) == 98
    assert v["lift_positivo"] == 1
    assert v["lift_negativo"] == 6
    # −4,8 p.p. é ruído, não queda: chamar isso de "a taxa cai" seria
    # superinterpretar na direção oposta. O que o dado sustenta é que as duas
    # janelas não se distinguem — e uma régua que não separa não decide watcher.
    assert "não separa" in v["veredito"]
    assert "93" not in v["veredito"]        # o número nunca sai sozinho como valor


def test_alerta_que_realmente_antecede_contato_e_reconhecido():
    """O medidor não pode ser pessimista por construção: se houver efeito, ele aparece."""
    linhas = [_hit(True, False)] * 60 + [_hit(False, False)] * 40
    v = veredito_do_lift(linhas)
    assert v["lift_positivo"] == 60
    assert "ANTECEDE mais contato" in v["veredito"]


def test_janelas_iguais_dizem_que_a_regua_nao_separa():
    linhas = [_hit(True, True)] * 50 + [_hit(False, False)] * 50
    v = veredito_do_lift(linhas)
    assert v["lift_positivo"] == 0 and v["lift_negativo"] == 0
    assert "não separa" in v["veredito"]


def test_projeto_vivo_nao_conta_como_ganho():
    """Contato que já existia antes e continuou não é alerta funcionando."""
    linhas = [_hit(True, True)] * 56          # o caso Emma: 56 de 56 nos dois lados
    v = veredito_do_lift(linhas)
    assert v["taxa_depois"] == 100.0
    assert v["lift_positivo"] == 0            # taxa cheia, ganho zero


def test_sem_hit_nao_inventa_numero():
    v = veredito_do_lift([])
    assert v["n"] == 0
    assert "nada a medir" in v["veredito"]
    assert v["taxa_depois"] == 0.0
