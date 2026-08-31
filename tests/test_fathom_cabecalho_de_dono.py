"""
O cabecalho de dono virava task fantasma e o item sob o ULTIMO cabecalho SUMIA.

O recap do Fathom agrupa os proximos passos por responsavel — `Sandra:` / item /
item · `Guilherme:` / item — e o importador quebrava linha a linha. Dois efeitos,
o segundo pior que o primeiro:

  1. cada cabecalho virava uma task de titulo vazio. Nas 2 reunioes Alba de
     24/08: #999972 `Renato:` #999987 `Sandra:` #999991 `Guilherme:`
     #999994 `Andre:` #999996 `Sandra, Israel, Andre e Guilherme:`.
  2. cada cabecalho GASTAVA UM SLOT do cap de 10, e o item sob o ultimo
     cabecalho nao virava task nenhuma. Foi assim que se perdeu "Reunir-se
     sexta 28/08 14h30 para definir o modelo de negocio..." — o gate da frente.
     O defeito nao dava erro: so sumia.

A CoS cancelou as 5 de 24/08 a mao; o importador nao foi consertado e reincidiu
em 25/08 (`Eliane:`, `Renato:`) e 29/08 (`Renato F A Prado:`, `Almeida Prado:` —
ainda pending em 31/08, e vindas dos `action_items` da API, nao do fallback).

A propriedade que importa: cabecalho nao vira task, mas TAMBEM NAO SE PERDE —
vira a posse do item abaixo, senao tarefa de terceiro cai na caixa do Renato.

Rodar: PYTHONPATH=app python -m pytest tests/test_fathom_cabecalho_de_dono.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from integrations.fathom import (  # noqa: E402
    _e_cabecalho_de_dono, separar_cabecalhos,
)


def _ai(desc, url=""):
    return {"description": desc, "recording_playback_url": url}


class TestDeteccaoDeCabecalho:
    def test_os_cinco_cabecalhos_reais_de_24_08(self):
        """Os que viraram #999972, #999987, #999991, #999994 e #999996."""
        for nome in ["Renato:", "Sandra:", "Guilherme:", "André:",
                     "Sandra, Israel, André e Guilherme:"]:
            assert _e_cabecalho_de_dono(nome), nome

    def test_os_reincidentes_de_25_08_e_29_08(self):
        for nome in ["Eliane:", "Renato:", "Renato F A Prado:", "Almeida Prado:"]:
            assert _e_cabecalho_de_dono(nome), nome

    def test_negrito_do_markdown_nao_engana(self):
        assert _e_cabecalho_de_dono("**Sandra:**")

    def test_o_contraste_que_fixa_a_regra(self):
        """A #999971 nasceu CERTA: nome e texto na MESMA linha, nao termina em ':'."""
        assert not _e_cabecalho_de_dono(
            "Israel: Apresentar suas preocupações sobre a sociedade")

    def test_item_real_nao_e_cabecalho(self):
        assert not _e_cabecalho_de_dono(
            "Reunir-se sexta 28/08 14h30 para definir o modelo de negócio")

    def test_frase_longa_terminada_em_dois_pontos_nao_e_engolida(self):
        """Guarda de tamanho: cabecalho e curto; texto longo e conteudo."""
        longa = ("Levantar com o contador todos os pontos que seguem em aberto "
                 "para a proxima reuniao, a saber:")
        assert len(longa) > 60
        assert not _e_cabecalho_de_dono(longa)

    def test_vazio_e_none_nao_quebram(self):
        assert not _e_cabecalho_de_dono("")
        assert not _e_cabecalho_de_dono(None)


class TestSeparacaoPreservaAPosse:
    def test_cabecalho_nao_vira_task(self):
        itens, descartados = separar_cabecalhos([
            _ai("Sandra:"), _ai("enviar a DRE para o Guilherme"),
        ])
        assert len(itens) == 1
        assert descartados == ["Sandra:"]

    def test_o_dono_desce_para_o_item(self):
        """Sem isto o item chega anonimo e cai na caixa do Renato."""
        itens, _ = separar_cabecalhos([
            _ai("Sandra:"), _ai("enviar a DRE para o Guilherme"),
        ])
        assert itens[0]["_dono_da_secao"] == "Sandra"

    def test_troca_de_dono_no_meio_da_lista(self):
        itens, desc = separar_cabecalhos([
            _ai("Sandra:"), _ai("enviar a DRE"),
            _ai("Guilherme:"), _ai("documentar processos"), _ai("mandar o BP"),
        ])
        assert [i["_dono_da_secao"] for i in itens] == [
            "Sandra", "Guilherme", "Guilherme"]
        assert len(desc) == 2

    def test_item_antes_do_primeiro_cabecalho_fica_sem_dono(self):
        itens, _ = separar_cabecalhos([_ai("item solto"), _ai("Sandra:"), _ai("x")])
        assert itens[0]["_dono_da_secao"] is None
        assert itens[1]["_dono_da_secao"] == "Sandra"

    def test_playback_url_do_item_e_preservado(self):
        itens, _ = separar_cabecalhos([_ai("Sandra:"), _ai("x", "http://f/1")])
        assert itens[0]["recording_playback_url"] == "http://f/1"

    def test_nao_muta_a_entrada(self):
        entrada = [_ai("Sandra:"), _ai("enviar a DRE")]
        separar_cabecalhos(entrada)
        assert "_dono_da_secao" not in entrada[1]


class TestOItemQueSumia:
    def test_cabecalhos_nao_gastam_mais_o_slot_do_cap_de_10(self):
        """O caso de 24/08: 5 cabecalhos + 9 itens. Com o cap aplicado sobre os
        14 brutos, os itens finais sumiam — inclusive o gate da frente."""
        bruto = []
        for dono in ["Renato", "Sandra", "Guilherme", "André",
                     "Sandra, Israel, André e Guilherme"]:
            bruto.append(_ai(f"{dono}:"))
            bruto.append(_ai(f"item A de {dono}"))
            bruto.append(_ai(f"item B de {dono}"))
        assert len(bruto) == 15

        itens, descartados = separar_cabecalhos(bruto)
        assert len(descartados) == 5
        assert len(itens) == 10, "10 itens reais cabem no cap; 15 brutos nao cabiam"

        ultimo = itens[-1]
        assert ultimo["_dono_da_secao"] == "Sandra, Israel, André e Guilherme"
        assert ultimo in itens[:10], "o item sob o ULTIMO cabecalho tem que sobreviver"

    def test_lista_vazia_e_none_degradam_gracioso(self):
        assert separar_cabecalhos([]) == ([], [])
        assert separar_cabecalhos(None) == ([], [])

    def test_lista_so_de_cabecalhos_nao_gera_task_nenhuma(self):
        itens, desc = separar_cabecalhos([_ai("Sandra:"), _ai("André:")])
        assert itens == []
        assert len(desc) == 2
