"""`send_document` POSTAVA NO ENDPOINT DE AUDIO — 30/09/2026.

`evolution_api.py` mandava documento para `/message/sendWhatsAppAudio/`. Nenhum
envio de documento jamais funcionou. O defeito sobreviveu meses porque a funcao
tinha **ZERO chamadores** — e parecia academico por isso.

Nao era. Em 30/09 o PPT de 4,99 MB ao cliente saiu por chamada MANUAL a Evolution
(nota #2229), em `/message/sendMedia/`, com base64 e `mimetype` da presentationml.
Funcao quebrada nao tem chamador: o trabalho vai por fora, e e isso que esconde o
bug ([[feedback_consumidor_morto_wiring]] pelo avesso).

Os tres detalhes alem da URL vieram desse envio manual e cada um tem teste:
mimetype (sem ele o PPTX chega generico), timeout (30s nao sobem 6,6 MB de base64
para uma VPS, e upload cortado devolve o MESMO `{"error"}` de payload invalido), e
a guarda de `media` vazio (o 400 do envio manual vinha daqui, nao da Evolution).

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_evolution_send_document.py -q
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from integrations import evolution_api as ev  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    """Cliente com `_request` espionado — nada sai para a rede."""
    cli = ev.EvolutionAPIClient()
    chamadas = []

    async def _spy(method, endpoint, data=None, timeout=30.0):
        chamadas.append({"method": method, "endpoint": endpoint,
                         "data": data, "timeout": timeout})
        return {"key": {"id": "MSG1"}}

    # `is_configured` é property sem setter, e não precisa: a guarda dela vive
    # dentro do `_request`, que é justamente o que está espionado aqui.
    monkeypatch.setattr(cli, "_request", _spy)
    cli._chamadas = chamadas
    return cli


@pytest.mark.asyncio
async def test_vai_para_sendMedia_e_NAO_para_o_endpoint_de_audio(api):
    """O defeito em si. Sem isto, trocar a URL de volta passa despercebido."""
    await api.send_document("5541991064663", "https://x/y.pptx", "plano.pptx")
    ep = api._chamadas[0]["endpoint"]
    assert "/message/sendMedia/" in ep, f"endpoint errado: {ep}"
    assert "sendWhatsAppAudio" not in ep, "voltou a postar no endpoint de AUDIO"


@pytest.mark.asyncio
async def test_mimetype_do_pptx_e_o_da_presentationml(api):
    """Sem `mimetype` o PPTX chega como arquivo generico. Foi o que o envio manual
    teve de informar a mao."""
    await api.send_document("5541991064663", "BASE64==", "Governanca_v2.pptx")
    d = api._chamadas[0]["data"]
    assert d["mimetype"] == ("application/vnd.openxmlformats-officedocument"
                             ".presentationml.presentation")
    assert d["mediatype"] == "document"
    assert d["fileName"] == "Governanca_v2.pptx", "o WhatsApp mostra este nome"


@pytest.mark.asyncio
@pytest.mark.parametrize("nome,esperado", [
    ("a.pdf", "application/pdf"),
    ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("a.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("a.csv", "text/csv"),
    ("sem_extensao", "application/octet-stream"),   # fallback, nunca None
])
async def test_mimetype_inferido_pelos_formatos_que_ele_manda(api, nome, esperado):
    await api.send_document("5541999999999", "BASE64==", nome)
    assert api._chamadas[-1]["data"]["mimetype"] == esperado


@pytest.mark.asyncio
async def test_mimetype_explicito_vence_a_inferencia(api):
    await api.send_document("5541999999999", "BASE64==", "arquivo.bin",
                            mimetype="application/pdf")
    assert api._chamadas[0]["data"]["mimetype"] == "application/pdf"


@pytest.mark.asyncio
async def test_timeout_grande_por_default(api):
    """30s nao sobem 6,6 MB de base64 para a VPS da Evolution, e um upload cortado
    devolve o MESMO `{"error"}` de um payload invalido — indistinguiveis no log."""
    await api.send_document("5541999999999", "BASE64==", "a.pdf")
    assert api._chamadas[0]["timeout"] >= 120.0


@pytest.mark.asyncio
async def test_media_vazio_e_recusado_AQUI(api):
    """O 400 do envio manual nao vinha da Evolution, vinha de `media` vazio.
    Recusar aqui da a mensagem certa em vez de um 400 que parece do outro lado."""
    r = await api.send_document("5541999999999", "   ", "a.pdf")
    assert "error" in r and "media" in r["error"]
    assert api._chamadas == [], "nao pode chegar a fazer a requisicao"


@pytest.mark.asyncio
async def test_filename_vazio_e_recusado(api):
    r = await api.send_document("5541999999999", "BASE64==", "")
    assert "error" in r and "filename" in r["error"].lower()
    assert api._chamadas == []


@pytest.mark.asyncio
async def test_telefone_ganha_55_como_nos_outros_envios(api):
    """Mesma normalizacao do `send_text`/`send_media` — o numero nao pode divergir
    por caminho de envio."""
    await api.send_document("41991064663", "BASE64==", "a.pdf")
    assert api._chamadas[0]["data"]["number"] == "5541991064663"
