"""O portão do cos-agent chega ao Renato.

O DEFEITO (22/08/2026). Ele disse "não estou recebendo nada do cos-agent" e
estava certo: o agente **não tinha canal de saída**. Nenhuma linha de notificação
no código dele. O runner grava o julgamento em `cos_daily_review`; o
`cockpit.py --quieto` vira HTML local ("gera sem abrir"); e o briefing da Tônia —
o canal que ele lê — nunca consultou essa tabela.

14 rodadas/dia, ~44,5M tokens/dia (10% do consumo do Max), produzindo 14 portões
que terminavam num arquivo em disco. Na rodada das 17h21 estavam lá a ligação
com o Israel antes da mesa de segunda, a proposta da Phisalia, a procuração do
IDPJ e a multa da DCTF com desconto até 26/08.

Mesma falha do artigo do Gui, do mesmo dia: captura que funciona, entrega que não
existe. [[feedback_consumidor_morto_wiring]]

CAMINHO PROVADO ATÉ A PONTA (não só até o emit):
  emit_signal → tabela `signals` → view `copilot.signals` (SELECT sem filtro de
  tipo) → `tonia/app/services/briefing.py:300` `fetch_open_signals(limit=40)`
  → contexto do briefing das 7h.

Rodar: .venv/bin/python -m pytest tests/test_cos_portao_entrega.py -v
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services import frente_review as fr  # noqa: E402


def _frente(pid, nome, o_que=None):
    return {
        "project_id": pid, "frente": nome,
        "precisa_de_voce": {"sim": bool(o_que), "o_que": o_que or ""},
    }


PAYLOAD = {"frentes": [
    _frente(26, "Alba Consultoria",
            "Combinar com o Israel a ligação de 20 minutos anterior à mesa de segunda."),
    _frente(60, "MilClean", "Mandar HOJE um WhatsApp curto ao Eduardo."),
    _frente(12, "Assespro", None),                       # sem portão
    _frente(47, "Regularização", ""),                    # 'sim' sem texto
]}


# ==================== extração ====================

def test_so_as_frentes_com_portao():
    p = fr.portoes_abertos(PAYLOAD)
    assert [x["project_id"] for x in p] == [26, 60]


def test_sim_sem_texto_nao_vira_portao():
    """`sim: True` com `o_que` vazio não é pedido — seria um signal sem conteúdo
    no briefing, que gasta o canal sem dizer nada."""
    p = fr.portoes_abertos({"frentes": [_frente(1, "X", "")]})
    assert p == []


def test_payload_vazio_nao_estoura():
    assert fr.portoes_abertos({}) == []
    assert fr.portoes_abertos({"frentes": []}) == []


# ==================== a chave do dedup ====================

def test_reescrita_trivial_mantem_a_chave():
    """O agente roda 14×/dia e reescreve o mesmo pedido com outra pontuação. Se
    a chave mudasse, cada rodada viraria signal novo e inundaria o briefing."""
    a = fr._assinatura_portao("Ligar para o Israel, hoje.")
    b = fr._assinatura_portao("ligar para o Israel — HOJE!")
    assert a == b


def test_pedido_diferente_muda_a_chave():
    """Contrapeso: chave só do project_id seria estável demais. Portão resolvido
    não reabre, então um pedido NOVO na mesma frente nunca chegaria."""
    a = fr._assinatura_portao("Ligar para o Israel antes da mesa")
    b = fr._assinatura_portao("Revisar o consolidado antes de circular")
    assert a != b


# ==================== emissão ====================

class _FakeCur:
    def execute(self, *a, **kw):
        pass

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self):
        self.commits = 0

    def cursor(self, **kw):
        return _FakeCur()

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def espiao(monkeypatch):
    """Captura o que foi emitido e expirado, sem tocar no banco."""
    estado = {"emitidos": [], "expirados": []}

    def _emit(conn, **kw):
        estado["emitidos"].append(kw)
        return "emitted"

    def _expire(conn, *, detector, current_hashes, reason="x"):
        estado["expirados"].append({"detector": detector,
                                    "hashes": list(current_hashes),
                                    "reason": reason})
        return 0

    monkeypatch.setattr("services.detectors._base.emit_signal", _emit)
    monkeypatch.setattr("services.detectors._base.expire_stale_signals", _expire)
    monkeypatch.setattr(fr, "get_db", lambda: _FakeConn())
    monkeypatch.setattr(fr, "latest_review", lambda: PAYLOAD)
    return estado


@pytest.mark.asyncio
async def test_um_signal_por_portao(espiao):
    """Não um signal com a lista dentro: cada portão tem relógio próprio, e
    agrupar faria o conjunto inteiro renascer quando UM item mudasse."""
    r = await fr.alertar_portoes()
    assert r["portoes"] == 2 and r["emitidos"] == 2
    assert {e["tipo"] for e in espiao["emitidos"]} == {"cos_portao"}


@pytest.mark.asyncio
async def test_urgencia_8_pra_entrar_no_briefing(espiao):
    """O briefing puxa 40 por urgência DESC. Os digests periódicos são urg 6;
    abaixo disso o portão sairia atrás deles."""
    await fr.alertar_portoes()
    assert all(e["urgencia"] == 8 for e in espiao["emitidos"])


@pytest.mark.asyncio
async def test_o_texto_do_pedido_viaja_no_contexto(espiao):
    """Sem o `o_que` no contexto, o briefing recebe um ponteiro em vez do
    pedido — e a Tônia não teria o que dizer."""
    await fr.alertar_portoes()
    ctx = [e["contexto"] for e in espiao["emitidos"]]
    assert any("Israel" in c["o_que"] for c in ctx)
    assert all(c.get("project_id") for c in ctx)


@pytest.mark.asyncio
async def test_portao_que_sumiu_expira(espiao):
    """Se o debriefing parou de pedir, o que estava aberto tem que fechar —
    senão o briefing cobra para sempre o que ele já fez."""
    await fr.alertar_portoes()
    assert espiao["expirados"], "não chamou expire_stale_signals"
    assert len(espiao["expirados"][0]["hashes"]) == 2


@pytest.mark.asyncio
async def test_sem_portao_ainda_expira_os_antigos(espiao, monkeypatch):
    """Sair cedo em 'nada a fazer' deixaria os signals de ontem abertos. Este é
    o caso que um early-return teria quebrado calado."""
    monkeypatch.setattr(fr, "latest_review", lambda: {"frentes": []})
    r = await fr.alertar_portoes()
    assert r["skipped_reason"] == "sem_portao_aberto"
    assert espiao["expirados"], "sem portão não é no-op — tem que fechar os velhos"
    assert espiao["expirados"][0]["hashes"] == []


@pytest.mark.asyncio
async def test_portao_ja_resolvido_e_contado_a_parte(espiao, monkeypatch):
    """`emit_signal` devolve 'skipped' quando o signal está resolved/dismissed.
    Somar isso a 'atualizados' esconderia o agente insistindo num pedido que o
    Renato já fechou."""
    monkeypatch.setattr("services.detectors._base.emit_signal",
                        lambda conn, **kw: "skipped")
    r = await fr.alertar_portoes()
    assert r["ja_resolvidos"] == 2 and r["emitidos"] == 0


# ==================== o canal ====================

def test_nao_manda_whatsapp_daqui():
    """O porta-voz único é a Tônia — ela decide se e como interrompe. Enviar WA
    deste módulo reabriria o que o A3 fechou em 12/07.
    [[feedback_wa_silencio_produtor]]"""
    import inspect
    fonte = inspect.getsource(fr.alertar_portoes)
    for proibido in ("send_text", "send_intel_notification", "evolution",
                     "notification_router"):
        assert proibido not in fonte, f"{proibido} no caminho do portão"
