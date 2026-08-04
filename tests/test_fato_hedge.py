"""Hipótese não entra com cara de fato (backfill de 04/08/2026).

O backfill dos 17 gerou 167 fatos, e 22 deles (13%) escreviam palpite como se
fosse certeza — com confiança média 0.84. Do José Guimarães Monforte, ficha que
importa pro board hunt:

    "Monforte é filho de ou tem relação próxima com o círculo do Orestes Prado"
    "relacionamento com Nizan (possivelmente Nizan Guanaes ou executivo homônimo)"

O texto hesitava e o número não. Como a camada passou a consultar
`contact_facts` antes de abrir portão, palpite com confiança alta vira insumo de
decisão.

O prompt agora proíbe hedge — mas prompt é pedido, não contrato. Estes testes
cobrem a GUARDA, que é determinística.

Rodar: .venv/bin/python -m pytest tests/test_fato_hedge.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services.contact_enrichment import _CONF_HEDGE, _tem_hedge  # noqa: E402


# Texto REAL gravado em 04/08 — não exemplo inventado. Se a guarda deixar de
# pegar estes, ela deixou de servir pro caso que a criou.
REAIS_COM_HEDGE = [
    "Monforte é filho de ou tem relação próxima com o círculo do 'Orestes Prado', pai de Renato",
    "Monforte tem relacionamento com Nizan (possivelmente Nizan Guanaes ou executivo homônimo)",
    "Eber estava trabalhando em um projeto em Rio Verde (GO) antes de falecer, sugerindo que",
]

# Fatos afirmados de verdade — a guarda NÃO pode mordê-los, senão rebaixa o bom
# junto com o duvidoso e a confiança perde sentido.
REAIS_AFIRMADOS = [
    "É o presidente do Conselho Consultivo da Associação Despertar",
    "Responde rápido em DM e indica contatos de imediato, mas a indicação tende a não converter",
    "Conduz o processo seletivo de novos conselheiros da Despertar",
    "Monforte sinalizou interesse em ser Strategic Advisor da ImensIAH, com equity",
    "Rosimeri tem folga aos domingos (mencionou em abril/2026)",
]


@pytest.mark.parametrize("texto", REAIS_COM_HEDGE)
def test_pega_hedge_real(texto):
    assert _tem_hedge(texto), f"hedge passou despercebido: {texto[:60]}"


@pytest.mark.parametrize("texto", REAIS_AFIRMADOS)
def test_nao_morde_fato_afirmado(texto):
    assert not _tem_hedge(texto), f"rebaixou fato afirmado: {texto[:60]}"


def test_rebaixa_mas_nao_recusa():
    """A decisão foi rebaixar, não sumir — mesma escolha do portão: o fato
    incerto ("mencionou um Nizan") ainda vale; o que não pode é entrar com cara
    de certeza. Recusar perderia informação real."""
    assert 0 < _CONF_HEDGE <= 0.5, "confiança de hedge tem que ser baixa, não zero"


def test_conf_alta_do_modelo_e_rebaixada():
    """O caso concreto: modelo mandou 0.84 num texto que hesita."""
    conf_modelo = 0.84
    texto = REAIS_COM_HEDGE[1]
    conf_final = min(conf_modelo, _CONF_HEDGE) if _tem_hedge(texto) else conf_modelo
    assert conf_final <= 0.45


def test_conf_baixa_nao_sobe():
    """`min` e não atribuição: se o modelo já foi honesto (0.3), a guarda não
    pode PROMOVER o fato pra 0.45."""
    conf_modelo = 0.30
    conf_final = min(conf_modelo, _CONF_HEDGE)
    assert conf_final == 0.30


def test_prompt_tambem_proibe():
    """Guarda e prompt têm que dizer a mesma coisa. Se alguém relaxar o prompt
    achando que a regex resolve, a qualidade cai antes da rede pegar."""
    import inspect
    from services import contact_enrichment as ce

    fonte = inspect.getsource(ce)
    assert "NAO ESCREVA HIPOTESE COMO SE FOSSE FATO" in fonte
    # os DOIS prompts (enrich_contact_with_ai e enrich_with_context)
    assert fonte.count("NAO ESCREVA HIPOTESE COMO SE FOSSE FATO") == 2
    assert "confianca: 0.3 a 1.0" in fonte, "o piso 0.5 impedia sinalizar dúvida"
