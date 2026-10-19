"""
Agrupamento de alertas por PROCESSO (19/10/26).

Os dois produtores identificam o processo de formas diferentes e nenhum usa o
numero sempre: **Astrea** manda CNJ, **Jusbrasil nao manda numero** — manda as
PARTES ("Alexandro Bereska x NK Parking S/A — TJSP · Sao Paulo"). Medido nos 30
alertas dos ultimos 30 dias: só **13 tinham CNJ**, e uma chave que exigisse
numero deixaria 17 de fora parecendo funcionar.

Prova:
  (a) CNJ e extraido com e sem separadores, e normalizado;
  (b) partes sao extraidas do corpo do Jusbrasil;
  (c) 🔴 FRAGMENTACAO: "Decisoes 1 processo Alexandro Bereska x NK Parking" e
      "Alexandro Bereska x NK Parking" sao o MESMO processo — sem a limpeza do
      rotulo de secao, 30 alertas relatavam 7 processos onde ha 5;
  (d) caixa e acento nao criam processo novo;
  (e) marketing nao vira grupo (None nao e falha, e exclusao correta);
  (f) o grupo expoe o alerta MAIS RECENTE — num processo o que importa e a
      ultima movimentacao, nao o volume de avisos;
  (g) CNJ vence rotulo de partes quando os dois aparecem;
  (h) nome de parte exige 3+ caracteres — guarda contra ruido, e por isso
      nomes artificiais de 1 letra ("A x B") NAO casam de proposito.

Rodar: .venv/bin/python -m pytest tests/test_processo_agrupador.py -v
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services.processo_agrupador import agrupar, chave_processo  # noqa: E402

T0 = datetime(2026, 10, 19, 12, 0)


def _a(subject, content="", quando=T0):
    return {"subject": subject, "content": content, "timestamp": quando}


# ----------------------------------------------------------------- (a) CNJ ----

@pytest.mark.parametrize("texto", [
    "Seu processo teve 3 movimentações no 2194965-70.2025.8.26.0000",
    "processo 21949657020258260000 movimentado",
    "Publicação em 2194965-70.2025.8.26.0000 - Escritório",
])
def test_cnj_extraido_com_ou_sem_separadores(texto):
    r = chave_processo(texto)
    assert r is not None and r["tipo"] == "cnj"
    assert r["chave"] == "21949657020258260000"
    assert r["rotulo"] == "2194965-70.2025.8.26.0000"


def test_numero_curto_nao_e_cnj():
    """20 digitos exatos. Telefone e CNPJ nao viram processo."""
    assert chave_processo("ligue 11 98765-4321 agora") is None


# -------------------------------------------------------------- (b) partes ----

def test_partes_do_corpo_real_do_jusbrasil():
    corpo = ("Olá, Renato! Seu processo possui uma nova informação sobre uma "
             "decisão. Jose Reinaldo Michel x Makerfest Tecnologia Ltda. "
             "TJSP · Central Acessar processo")
    r = chave_processo("Nova decisão no processo", corpo)
    assert r is not None and r["tipo"] == "partes"
    assert r["rotulo"] == "Jose Reinaldo Michel x Makerfest Tecnologia Ltda"


# ------------------------------------------------- (c) 🔴 FRAGMENTACAO --------

def test_rotulo_de_secao_nao_cria_processo_novo():
    """O caso REAL: o Jusbrasil prefixa a dupla com o rotulo da secao, e sem a
    limpeza o mesmo processo quebrava em 2 grupos (7 + 5 ocorrencias)."""
    a = chave_processo("", "Decisões 1 processo Alexandro Bereska x NK Parking S/A TJSP")
    b = chave_processo("", "Alexandro Bereska x NK Parking S/A TJSP · São Paulo")
    assert a is not None and b is not None
    assert a["chave"] == b["chave"], f"fragmentou: {a['chave']!r} != {b['chave']!r}"


def test_prefixos_empilhados_sao_removidos():
    """"Novos processos encontrados" + "1 processo" vem juntos — uma passada
    de limpeza deixaria metade do rotulo dentro do nome."""
    r = chave_processo(
        "", "Novos processos encontrados 1 processo Jose Reinaldo Michel x "
            "Makerfest Tecnologia Ltda TJSP")
    assert r is not None
    assert r["rotulo"].startswith("Jose Reinaldo Michel"), r["rotulo"]


def test_os_tres_formatos_reais_colapsam_em_um_grupo():
    """Replay do corpus: as 3 variantes do mesmo processo = 1 grupo."""
    grupos, fora = agrupar([
        _a("⚖️ Processos de sua empresa",
           "Decisões 1 processo Alexandro Bereska x NK Parking S/A TJSP"),
        _a("Nova decisão no processo",
           "Alexandro Bereska x NK Parking S/A TJSP · São Paulo"),
        _a("Audiência no processo",
           "ALEXANDRO BERESKA x NK PARKING S/A TJSP"),
    ])
    assert len(grupos) == 1, [g["rotulo"] for g in grupos]
    assert grupos[0]["n"] == 3
    assert not fora


# ------------------------------------------------------ (d) caixa e acento ----

def test_caixa_e_acento_nao_criam_processo_novo():
    a = chave_processo("", "Alexandro Bereska x NK Parking S/A TJSP")
    b = chave_processo("", "ALEXANDRO BERESKA x NK PARKING S/A TJSP")
    assert a["chave"] == b["chave"]


# ---------------------------------------------------------- (e) marketing -----

@pytest.mark.parametrize("subject,corpo", [
    ("Conferir leva segundos. Descobrir depois, não.", "Jusbrasil Acessar agora"),
    ("O que você começou no Jusbrasil ainda está aqui", "Continue de onde parou"),
])
def test_marketing_nao_vira_grupo(subject, corpo):
    """None nao e falha: contar um grupo "sem chave" junto com os processos
    reais inflaria a conta que o Renato le."""
    assert chave_processo(subject, corpo) is None


def test_marketing_sai_em_fora_e_nao_entra_na_contagem():
    grupos, fora = agrupar([
        _a("Nova decisão no processo", "Jose Reinaldo Michel x Makerfest Ltda TJSP"),
        _a("Conferir leva segundos. Descobrir depois, não.", "Jusbrasil"),
    ])
    assert len(grupos) == 1
    assert len(fora) == 1


# ------------------------------------------------- (f) o mais RECENTE manda ---

def test_grupo_ordena_pelo_alerta_mais_recente():
    velho = _a("Audiência no processo", "Ana Silva x Beta Ltda TJSP",
               T0 - timedelta(days=9))
    novo = _a("Nova decisão no processo", "Ana Silva x Beta Ltda TJSP", T0)
    grupos, _ = agrupar([velho, novo])
    assert grupos[0]["ultimo"] == T0


def test_grupos_vem_do_mais_recente_pro_mais_antigo():
    """O volume de avisos nao e a prioridade: 1 decisao de hoje importa mais
    que 12 avisos de movimentacao da semana passada."""
    antigo_grande = [_a("mov", "Carlos Dias x Delta Ltda TJSP",
                        T0 - timedelta(days=5))] * 4
    recente_pequeno = [_a("Nova decisão no processo",
                         "Ana Silva x Beta Ltda TJSP", T0)]
    grupos, _ = agrupar(antigo_grande + recente_pequeno)
    assert grupos[0]["rotulo"].startswith("Ana Silva")
    assert grupos[0]["n"] == 1
    assert grupos[1]["n"] == 4


# ------------------------------------------------------- (g) CNJ tem prece ----

def test_cnj_vence_quando_os_dois_aparecem():
    r = chave_processo(
        "Seu processo teve 1 Publicação",
        "0012411-71.2020.8.26.0100 — Fulano x Beltrano Ltda TJSP")
    assert r["tipo"] == "cnj"
    assert r["rotulo"] == "0012411-71.2020.8.26.0100"


def test_lista_vazia_nao_quebra():
    grupos, fora = agrupar([])
    assert grupos == [] and fora == []


# ------------------------------------- (h) a guarda de tamanho do nome --------

def test_nome_de_uma_letra_nao_casa():
    """Guarda contra ruido: " x " entre tokens curtos aparece em texto solto
    ("3 x 4", "A x B"). Nome de parte real tem 3+ caracteres — a 1a versao
    DESTE TESTE usou "A x B" e falhou por isso, nao por defeito do codigo."""
    assert chave_processo("", "A x B TJSP") is None
    assert chave_processo("", "Ana Silva x Beta Ltda TJSP") is not None


def test_emenda_entre_assunto_e_corpo_nao_vira_parte():
    """Regressao do defeito real: buscar em `subject + content` fazia o assunto
    virar nome de parte e fragmentar o mesmo processo por assunto."""
    a = chave_processo("Audiência no processo",
                       "Alexandro Bereska x NK Parking S/A TJSP")
    b = chave_processo("Nova decisão no processo",
                       "Alexandro Bereska x NK Parking S/A TJSP")
    assert a["chave"] == b["chave"]
    assert not a["rotulo"].lower().startswith("audi"), a["rotulo"]
