""""Ele já fez isso?" — o cruzamento que a triagem faz antes de propor.

O CASO QUE ORIGINOU (07/08/2026), e que estes testes preservam: a task #999732
("Avaliar inscrição no Concurso de Qualidade — Sudoeste de Minas") tinha
`contact_id` = Reginaldo, e o Renato JÁ tinha mandado o regulamento a ele por
WhatsApp. A triagem propôs mesmo assim, porque olhou as pessoas citadas no texto
da task (Andressa, Orestes) e não o contato dela.

No mesmo contato e no mesmo dia havia "o controle do portão não funcionou" e uma
figurinha. É por isso que o filtro de relevância não é enfeite: sem ele,
qualquer conversa viraria prova de que a task foi resolvida — e dar por feito o
que não foi feito é o erro caro desta família.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from services.atos_task import _termos  # noqa: E402


def test_termos_ignora_acento_e_curtas():
    t = _termos("Inscrição no Concurso de Qualidade")
    assert "inscricao" in t and "concurso" in t and "qualidade" in t
    assert "no" not in t and "de" not in t


def test_termos_descarta_verbo_generico_de_titulo_de_task():
    """"Avaliar", "verificar", "confirmar" abrem metade dos títulos: se contarem
    como termo, casam com qualquer conversa e o score deixa de discriminar."""
    t = _termos("Avaliar e confirmar o envio")
    assert "avaliar" not in t and "confirmar" not in t


def test_o_caso_reginaldo_casa():
    titulo = "Avaliar inscrição no Concurso de Qualidade — Sudoeste de Minas 2026 (café)"
    msg = ("Reginaldo, tudo bem? Chegou aqui o regulamento do Concurso de "
           "Qualidade do Sudoeste de Minas 2026. Você está participando?")
    comuns = _termos(titulo) & _termos(msg)
    assert {"concurso", "qualidade", "sudoeste", "minas"} <= comuns


def test_ruido_do_mesmo_contato_nao_casa():
    """Mesma pessoa, mesmo dia, assunto nenhum a ver — tem de dar zero."""
    titulo = "Avaliar inscrição no Concurso de Qualidade — Sudoeste de Minas 2026 (café)"
    for ruido in ("o controle do portão não funcionou então deixei lá na cozinha",
                  "[Figurinha]",
                  "Ah eu peguei um que estava na gaveta. Apertei os 2 botões"):
        assert not (_termos(titulo) & _termos(ruido)), f"casou à toa com: {ruido}"


def test_texto_vazio_nao_explode():
    assert _termos(None) == set()
    assert _termos("") == set()
