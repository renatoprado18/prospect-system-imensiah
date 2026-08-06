"""Testes do dispatch de anexos WA (defeito medido em 06/08/2026).

Em 30 dias, **50 de 52 audios DM incoming** nao deixaram uma linha em
`wa_attachments`. Nao era falha de transcricao: o audio DM incoming tinha um
caminho proprio (`_transcribe_audio_inline`) que chamava o Groq de dentro do
`intel-api` — e `GROQ_API_KEY` nunca foi configurada nesse servico do Railway.
A funcao retornava na PRIMEIRA linha, so com um `logger.warning`. Sem erro,
sem registro, **nada pra reprocessar**. No mesmo periodo, o caminho de GRUPO
— que ja usava o worker — fez 63 de 64.

Sao dois defeitos independentes, e cada um tem seu teste aqui:

  (a) EXTRATOR NO PROCESSO ERRADO -- o app chamando uma API cuja chave mora
      no worker. Enquanto houver um segundo extrator dentro do `app/`, o
      defeito pode voltar pela porta de tras;
  (b) FRACASSO NAO REGISTRADO -- dispatch que nao roda e nao grava nada. Uma
      fila de reprocessamento construida sobre `wa_attachments` nasce cega:
      nao da pra reprocessar o que nunca foi registrado.

Rodar: .venv/bin/python -m pytest tests/test_wa_audio_dispatch.py -v
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from services import wa_attachment_dispatch as wad  # noqa: E402

_EVOLUTION = os.path.join(_ROOT, "app", "integrations", "evolution_api.py")


# --------------------------------------------------------------------------
# (a) o extrator mora no worker — o app so despacha
# --------------------------------------------------------------------------

def test_app_nao_chama_groq_direto():
    """`GROQ_API_KEY` so existe no servico do worker.

    Qualquer chamada ao Groq de dentro de `app/` roda num processo que nao tem
    a chave — e o codigo atual trata chave ausente com `return` silencioso.
    """
    ofensores = []
    for raiz, _, arquivos in os.walk(os.path.join(_ROOT, "app")):
        if "__pycache__" in raiz:
            continue
        for nome in arquivos:
            if not nome.endswith(".py"):
                continue
            caminho = os.path.join(raiz, nome)
            with open(caminho, encoding="utf-8") as fh:
                if "api.groq.com" in fh.read():
                    ofensores.append(os.path.relpath(caminho, _ROOT))
    assert not ofensores, (
        "extracao de audio deve sair pelo worker (que tem GROQ_API_KEY); "
        f"chamada direta ao Groq em: {ofensores}"
    )


def test_audio_incoming_dm_e_despachado():
    """A condicao de dispatch em DM nao pode excluir audio incoming.

    Era exatamente essa a exclusao — `message_type == "audio" and direction ==
    "outgoing"` — que mandava o incoming pro caminho morto.
    """
    with open(_EVOLUTION, encoding="utf-8") as fh:
        arvore = ast.parse(fh.read())

    alvo = None
    for no in ast.walk(arvore):
        if isinstance(no, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "should_dispatch_attachment"
            for t in no.targets
        ):
            alvo = no
    assert alvo is not None, "should_dispatch_attachment sumiu de evolution_api.py"

    fonte = ast.dump(alvo)
    for tipo in ("audio", "image", "document"):
        assert f"'{tipo}'" in fonte, f"{tipo} fora da condicao de dispatch"
    assert "outgoing" not in fonte, (
        "dispatch em DM voltou a depender da direcao — audio incoming "
        "(50 de 52 perdidos em 30d) nao pode ter caminho separado"
    )


def test_deteccao_de_tipo_cobre_os_tres():
    m = {"audioMessage": {}}
    assert wad._detect_attachment_kind(m) == "audio"
    assert wad._detect_attachment_kind({"imageMessage": {}}) == "image"
    assert wad._detect_attachment_kind(
        {"documentMessage": {"mimetype": "application/pdf"}}
    ) == "pdf"
    assert wad._detect_attachment_kind({"conversation": "oi"}) is None


# --------------------------------------------------------------------------
# (b) o que nao rodou tem que deixar rastro
# --------------------------------------------------------------------------

@pytest.fixture
def falhas_registradas(monkeypatch):
    """Captura o que o dispatch gravaria em `wa_attachments`."""
    registradas = []
    monkeypatch.setattr(
        wad, "_registrar_falha",
        lambda message_id, phone, kind, motivo: registradas.append(
            {"message_id": message_id, "phone": phone, "kind": kind, "motivo": motivo}
        ),
    )
    return registradas


@pytest.mark.asyncio
async def test_worker_url_ausente_deixa_rastro(monkeypatch, falhas_registradas):
    """Config faltando e o caso do incidente: falhava calado."""
    monkeypatch.setenv("AUDIO_WORKER_URL", "")
    monkeypatch.setattr(wad, "get_worker_secret", lambda: "s3cr3t")

    r = await wad.dispatch_attachment_to_worker(
        {"audioMessage": {}}, {"id": "ABC"}, "5511999999999", "ABC",
    )

    assert r["dispatched"] is False
    assert len(falhas_registradas) == 1
    assert falhas_registradas[0]["kind"] == "audio"
    assert falhas_registradas[0]["message_id"] == "ABC"


@pytest.mark.asyncio
async def test_secret_ausente_deixa_rastro(monkeypatch, falhas_registradas):
    monkeypatch.setenv("AUDIO_WORKER_URL", "https://worker.exemplo")
    monkeypatch.setattr(wad, "get_worker_secret", lambda: "")

    r = await wad.dispatch_attachment_to_worker(
        {"imageMessage": {}}, {"id": "IMG1"}, "5511999999999", "IMG1",
    )

    assert r["dispatched"] is False
    assert [f["kind"] for f in falhas_registradas] == ["image"]


@pytest.mark.asyncio
async def test_erro_de_rede_deixa_rastro(monkeypatch, falhas_registradas):
    monkeypatch.setenv("AUDIO_WORKER_URL", "https://worker.exemplo")
    monkeypatch.setattr(wad, "get_worker_secret", lambda: "s3cr3t")

    class ClienteQueCai:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise ConnectionError("recusou")

    monkeypatch.setattr(wad.httpx, "AsyncClient", lambda **kw: ClienteQueCai())

    r = await wad.dispatch_attachment_to_worker(
        {"audioMessage": {}}, {"id": "NET1"}, "5511999999999", "NET1",
    )

    assert r["dispatched"] is False
    assert len(falhas_registradas) == 1
    assert "ConnectionError" in falhas_registradas[0]["motivo"]


@pytest.mark.asyncio
async def test_http_de_erro_do_worker_deixa_rastro(monkeypatch, falhas_registradas):
    """200 nao e a unica resposta possivel — 5xx tambem precisa de rastro."""
    monkeypatch.setenv("AUDIO_WORKER_URL", "https://worker.exemplo")
    monkeypatch.setattr(wad, "get_worker_secret", lambda: "s3cr3t")

    class Resposta:
        status_code = 502

    class ClienteOk:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return Resposta()

    monkeypatch.setattr(wad.httpx, "AsyncClient", lambda **kw: ClienteOk())

    r = await wad.dispatch_attachment_to_worker(
        {"documentMessage": {"mimetype": "application/pdf"}},
        {"id": "PDF1"}, "5511999999999", "PDF1",
    )

    assert r["dispatched"] is False
    assert "502" in falhas_registradas[0]["motivo"]


@pytest.mark.asyncio
async def test_sucesso_nao_registra_falha(monkeypatch, falhas_registradas):
    """O contraponto: rastro de erro em anexo que deu certo polui a fila."""
    monkeypatch.setenv("AUDIO_WORKER_URL", "https://worker.exemplo")
    monkeypatch.setattr(wad, "get_worker_secret", lambda: "s3cr3t")

    class Resposta:
        status_code = 200

    class ClienteOk:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return Resposta()

    monkeypatch.setattr(wad.httpx, "AsyncClient", lambda **kw: ClienteOk())

    r = await wad.dispatch_attachment_to_worker(
        {"audioMessage": {}}, {"id": "OK1"}, "5511999999999", "OK1",
    )

    assert r["dispatched"] is True
    assert falhas_registradas == []


@pytest.mark.asyncio
async def test_mensagem_sem_anexo_nao_registra_falha(monkeypatch, falhas_registradas):
    """Texto puro nao e anexo perdido — nao pode entrar na fila."""
    monkeypatch.setenv("AUDIO_WORKER_URL", "")
    r = await wad.dispatch_attachment_to_worker(
        {"conversation": "oi"}, {"id": "TXT1"}, "5511999999999", "TXT1",
    )
    assert r == {"dispatched": False, "reason": "no_attachment"}
    assert falhas_registradas == []
