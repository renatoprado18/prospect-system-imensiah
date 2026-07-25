"""
Testes dos helpers puros da consciência de OUTBOUND da camada CoS (frente #3, 25/07).

Contexto: a camada re-surfaçava um portão ("mandar o e-mail para os 4", Luminosità
#48) que o Renato JÁ tinha executado em 24/07. Root cause provado contra prod:
(a) o window de DM ordenava `ts ASC LIMIT 40` e TRUNCAVA o e-mail recém-enviado;
(b) não havia bloco/regra dizendo à LLM que ação outbound já feita fecha o portão.

Estes testes cobrem a lógica DETERMINÍSTICA (sem LLM/DB): kill-switch, render do
bloco de outbound e re-ordenação cronológica das DMs. A qualidade do julgamento
da LLM foi validada por dry-run contra prod (a LLM passou a dizer "e-mail aos 4
enviado em 24/07 ... portão cumprido, não re-cobrado").

Rodar: python -m pytest tests/test_frente_review_outbound.py -v
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import frente_review as fr  # noqa: E402


class TestKillSwitch:
    def teardown_method(self):
        os.environ.pop("COS_OUTBOUND_AWARENESS", None)

    def test_default_on(self):
        os.environ.pop("COS_OUTBOUND_AWARENESS", None)
        assert fr._outbound_awareness_on() is True

    def test_explicit_on(self):
        os.environ["COS_OUTBOUND_AWARENESS"] = "1"
        assert fr._outbound_awareness_on() is True

    def test_off_values(self):
        for v in ("0", "false", "off", "no", "FALSE", "Off"):
            os.environ["COS_OUTBOUND_AWARENESS"] = v
            assert fr._outbound_awareness_on() is False, v


def _base_gather(**over):
    g = {
        "project": {"id": 48, "nome": "Luminosità — Vinho Itália→Japão", "descricao": ""},
        "tasks": [], "members": [], "dms": [], "routed": [], "notes": [],
        "groups": [], "memories": [], "renato_outbound": [],
    }
    g.update(over)
    return g


class TestOutboundBlockRender:
    def test_block_present_when_outbound_exists(self):
        g = _base_gather(renato_outbound=[
            {"id": 24858, "canal": "email", "ts": "2026-07-24 18:37",
             "para": "Nick Salussolia",
             "conteudo": "Prezados Nick, Francine, Lilian, dando sequência..."},
        ])
        txt = fr._fmt_gather(g)
        assert "AÇÕES RECENTES DO RENATO" in txt
        assert "Prezados Nick, Francine, Lilian" in txt
        assert "email → Nick Salussolia" in txt

    def test_no_block_when_empty(self):
        txt = fr._fmt_gather(_base_gather(renato_outbound=[]))
        assert "AÇÕES RECENTES DO RENATO" not in txt


class TestDmChronologicalRender:
    def test_dms_rendered_oldest_to_newest_regardless_of_fetch_order(self):
        # Fetch vem ts DESC (recência sobrevive ao LIMIT); o render tem de
        # re-ordenar cronológico e mostrar as MAIS RECENTES.
        g = _base_gather(dms=[
            {"id": 3, "contact_nome": "Nick", "direcao": "outgoing",
             "ts": "2026-07-24 18:37", "conteudo": "email formal enviado"},
            {"id": 2, "contact_nome": "Nick", "direcao": "incoming",
             "ts": "2026-07-20 10:00", "conteudo": "mensagem do meio ok"},
            {"id": 1, "contact_nome": "Nick", "direcao": "incoming",
             "ts": "2026-07-10 09:00", "conteudo": "mensagem antiga inicial"},
        ])
        txt = fr._fmt_gather(g)
        i_old = txt.index("antiga inicial")
        i_mid = txt.index("do meio")
        i_new = txt.index("email formal enviado")
        assert i_old < i_mid < i_new  # cronológico ascendente no output
