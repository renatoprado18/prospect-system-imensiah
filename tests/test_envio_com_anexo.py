"""O INTEL NÃO SABIA ENVIAR ANEXO — e o custo foi trabalho por fora, 3 vezes.

`gmail.send_message` montava `MIMEText`/`MIMEMultipart('alternative')` e não
tinha UMA ocorrência de "attachment" no arquivo. `evolution_api.send_document`
estava correta desde 30/09/2026 e **sem rota em `main.py`**. As duas lacunas são
a mesma: capacidade que não existe (ou existe e não tem porta) não devolve erro
— ela manda o trabalho para fora, e é o trabalho por fora que a esconde.

Medido, não suposto:
- 30/09 — PPT de 4,99 MB ao cliente, por chamada manual à Evolution (nota #2229)
- 04/11 — duas certidões da Pitangueiras, idem
- 06/11 — proposta de mandato da Building 8 aos três sócios, por
  `enviar_proposta.py`: script avulso que remontou o MIME e postou direto na
  Gmail API (message id 1a1b11ddf258ecc8)

Estes testes olham o MIME REAL e o PAYLOAD REAL, não o retorno da função — foi
o retorno que pareceu certo em 10/10 enquanto o `cc` do Orestes se perdia.

Rodar:
  PYTHONPATH=app .venv/bin/python -m pytest tests/test_envio_com_anexo.py -q
"""
import base64
import os
import sys
from email import message_from_bytes

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from services.gmail_mime import (  # noqa: E402
    LIMITE_GMAIL_BYTES,
    AnexoInvalido,
    carrega_anexo,
    monta_mensagem,
)

PDF_FALSO = b"%PDF-1.7\n" + b"conteudo-de-proposta" * 20


# ====================================================== anexo de ARQUIVO LOCAL
#
# O caso de uso real: as peças vivem em ~/cockpit. Exigir que o chamador leia o
# arquivo e faça o base64 à mão é o atrito que manda o trabalho para fora.


def test_anexo_por_caminho_local_entra_no_mime(tmp_path):
    peca = tmp_path / "B8_Proposta_Mandato_Conselheiro.pdf"
    peca.write_bytes(PDF_FALSO)

    m = message_from_bytes(monta_mensagem(
        to="constanza.hummel@building8.com.br",
        subject="Proposta — mandato de conselheiro · Building 8",
        body="Segue a proposta formal, como combinado.",
        attachments=[{"path": str(peca)}],
        estrito=True,
    ).as_bytes())

    anexos = [p for p in m.walk() if p.get_filename()]
    assert [p.get_filename() for p in anexos] == ["B8_Proposta_Mandato_Conselheiro.pdf"], \
        "o nome tem de sair do basename — repetir o nome do caminho é mais uma chance de divergir"
    assert anexos[0].get_payload(decode=True) == PDF_FALSO
    assert anexos[0].get_content_type() == "application/pdf"


def test_mimetype_inferido_do_nome_quando_nao_informado(tmp_path):
    """Sem isto o PPTX chega como arquivo genérico — a lição do envio de 30/09."""
    peca = tmp_path / "Governanca_v2.pptx"
    peca.write_bytes(b"PK\x03\x04")

    nome, _, maintype, subtype = carrega_anexo({"path": str(peca)})

    assert nome == "Governanca_v2.pptx"
    assert f"{maintype}/{subtype}" == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


def test_arquivo_inexistente_e_recusado_nomeando_o_caminho():
    with pytest.raises(AnexoInvalido) as e:
        carrega_anexo({"path": "/caminho/que/nao/existe.pdf"})
    assert "nao/existe.pdf" in str(e.value)


def test_caminho_com_til_e_expandido(tmp_path, monkeypatch):
    """`~/cockpit/...` é como o Renato se refere às peças."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "cockpit").mkdir()
    (tmp_path / "cockpit" / "p.pdf").write_bytes(PDF_FALSO)

    nome, conteudo, _, _ = carrega_anexo({"path": "~/cockpit/p.pdf"})

    assert nome == "p.pdf" and conteudo == PDF_FALSO


# ======================================================== o teto de 25 MB
#
# O Gmail mede a MENSAGEM, não o arquivo, e base64 infla ~33%. Conferir o
# arquivo contra 25 MB deixaria passar a borda, e o erro voltaria como um 400
# opaco do Google — depois de subir 27 MB.


def test_anexo_que_estoura_o_teto_e_recusado_com_os_numeros():
    grande = b"\0" * (20 * 1024 * 1024)  # 20 MB de arquivo -> ~26,7 MB de mensagem

    with pytest.raises(AnexoInvalido) as e:
        monta_mensagem(to="a@x.com", subject="s", body="b", estrito=True,
                       attachments=[{"filename": "g.pdf", "content_bytes": grande}])

    msg = str(e.value)
    assert "20.0 MB" in msg, f"não nomeou o tamanho bruto: {msg}"
    assert "26.6 MB" in msg or "26.7 MB" in msg, f"não nomeou o estimado: {msg}"
    assert "25 MB" in msg


def test_teto_mede_a_mensagem_e_nao_o_arquivo():
    """A regressão que um teto de 25 MB sobre o ARQUIVO não pegaria.

    20 MB passa por "menor que 25", e o Gmail recusa de volta. Se este teste
    ficar verde com a guarda comparando o bruto, o 400 opaco volta.
    """
    assert 20 * 1024 * 1024 < LIMITE_GMAIL_BYTES, "premissa do teste"
    with pytest.raises(AnexoInvalido):
        monta_mensagem(to="a@x.com", subject="s", body="b", estrito=True,
                       attachments=[{"filename": "g.pdf",
                                     "content_bytes": b"\0" * (20 * 1024 * 1024)}])


# ============================== rascunho IGNORA, envio ABORTA (e não o inverso)


def test_rascunho_ignora_anexo_ruim_e_preserva_o_corpo():
    """`estrito=False` é o default de propósito — quem já chamava era o draft.

    No rascunho há revisão humana depois do defeito: o Renato vê antes de
    despachar. Perder o texto já escrito é pior que perder o anexo.
    """
    m = message_from_bytes(monta_mensagem(
        to="a@x.com", subject="s", body="corpo que custou tempo",
        attachments=[{"filename": "sem_conteudo.pdf"}, {}],
    ).as_bytes())

    assert m["To"] == "a@x.com"
    assert [p.get_filename() for p in m.walk() if p.get_filename()] == []


def test_envio_aborta_em_anexo_ruim():
    """No envio NÃO há revisão depois: o e-mail chega sem a proposta e ninguém
    é avisado. É o modo de falha do `cc` descartado, com arquivo no lugar do
    endereço."""
    with pytest.raises(AnexoInvalido):
        monta_mensagem(to="a@x.com", subject="s", body="b", estrito=True,
                       attachments=[{"filename": "sem_conteudo.pdf"}])


# ======================================= `gmail.send_message` — compatibilidade
#
# A função tem 3 chamadores vivos (`/api/gmail/send`, o gmail-proxy e o
# `cos_agent`). O parâmetro novo é opcional, e SEM ele o e-mail tem de sair
# igual ao que saía — estes testes são o contrato disso.


@pytest.fixture
def gmail_espiado(monkeypatch):
    """`GmailIntegration` com a rede espionada: devolve o `raw` que FOI enviado."""
    import httpx

    from integrations.gmail import GmailIntegration

    enviados = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "MSG1", "threadId": "TH1"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            enviados.append({"url": url, "json": json, "timeout": timeout})
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    cli = GmailIntegration()
    cli._enviados = enviados
    return cli


def _mime_enviado(cli):
    raw = cli._enviados[-1]["json"]["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


@pytest.mark.asyncio
async def test_sem_anexo_o_email_de_texto_sai_igual(gmail_espiado):
    r = await gmail_espiado.send_message(
        access_token="T", to="renato@almeida-prado.com",
        subject="Assunto", body="Corpo simples",
    )

    m = _mime_enviado(gmail_espiado)
    assert r["id"] == "MSG1"
    assert m["To"] == "renato@almeida-prado.com"
    assert m["Subject"] == "Assunto"
    assert m.get_content_type() == "text/plain", "virou multipart sem precisar"
    assert m.get_payload(decode=True).decode().strip() == "Corpo simples"
    assert gmail_espiado._enviados[-1]["timeout"] == 30.0, \
        "e-mail de texto que trava em 30s é rede; esperar 120s não ajuda"


@pytest.mark.asyncio
async def test_sem_anexo_o_html_continua_alternative(gmail_espiado):
    await gmail_espiado.send_message(
        access_token="T", to="a@x.com", subject="s",
        body="texto", html_body="<p>rico</p>",
    )

    m = _mime_enviado(gmail_espiado)
    assert m.get_content_type() == "multipart/alternative"
    tipos = [p.get_content_type() for p in m.walk() if not p.is_multipart()]
    assert tipos == ["text/plain", "text/html"], \
        "a ordem importa: o cliente escolhe a ÚLTIMA que entende"


@pytest.mark.asyncio
async def test_sem_anexo_o_threading_continua(gmail_espiado):
    await gmail_espiado.send_message(
        access_token="T", to="a@x.com", subject="s", body="b",
        reply_to_message_id="<abc@mail>", thread_id="TH9",
    )

    m = _mime_enviado(gmail_espiado)
    assert m["In-Reply-To"] == "<abc@mail>"
    assert m["References"] == "<abc@mail>"
    assert gmail_espiado._enviados[-1]["json"]["threadId"] == "TH9"


# ================================= `gmail.send_message` — a capacidade nova


@pytest.mark.asyncio
async def test_send_message_anexa_arquivo_local(gmail_espiado, tmp_path):
    """O caso de 06/11, agora pelo sistema: o que o script avulso fazia."""
    peca = tmp_path / "B8_Proposta.pdf"
    peca.write_bytes(PDF_FALSO)

    r = await gmail_espiado.send_message(
        access_token="T",
        to=["constanza.hummel@building8.com.br", "alessandro.rico@building8.com.br"],
        subject="Proposta — mandato de conselheiro · Building 8",
        body="Segue a proposta formal.",
        attachments=[{"path": str(peca)}],
    )

    assert "error" not in r
    m = _mime_enviado(gmail_espiado)
    assert m["To"] == ("constanza.hummel@building8.com.br, "
                       "alessandro.rico@building8.com.br"), \
        "lista crua no cabeçalho viraria repr de Python e o Gmail silenciaria o endereço"
    anexos = [p for p in m.walk() if p.get_filename()]
    assert [p.get_filename() for p in anexos] == ["B8_Proposta.pdf"]
    assert anexos[0].get_payload(decode=True) == PDF_FALSO
    assert gmail_espiado._enviados[-1]["timeout"] == 120.0, \
        "30s não sobem anexo, e o upload cortado é indistinguível de payload inválido"


@pytest.mark.asyncio
async def test_send_message_leva_cc_e_bcc(gmail_espiado):
    """🔴 `cc`/`bcc` eram DESCARTADOS EM SILÊNCIO no envio até 06/11/2026.

    O defeito de 10/10 (e-mail à Fernanda sem o Orestes em cópia) foi corrigido
    só do lado do `create_draft`. O lado do ENVIO continuou com `MIMEText`, que
    não tem onde pôr cópia.
    """
    await gmail_espiado.send_message(
        access_token="T", to="fernanda@exemplo.com", subject="Planilha",
        body="Pai, consegue mandar para ela?",
        cc="orestes@exemplo.com", bcc="oculto@exemplo.com",
    )

    m = _mime_enviado(gmail_espiado)
    assert m["Cc"] == "orestes@exemplo.com", "o cc foi descartado outra vez, agora no envio"
    assert m["Bcc"] == "oculto@exemplo.com"


@pytest.mark.asyncio
async def test_anexo_grande_e_recusado_SEM_chamar_a_api(gmail_espiado):
    """Recusar antes de tentar: um 400 do Google por tamanho não diz que o
    problema é tamanho, e chega depois de subir o arquivo."""
    r = await gmail_espiado.send_message(
        access_token="T", to="a@x.com", subject="s", body="b",
        attachments=[{"filename": "g.pdf", "content_bytes": b"\0" * (20 * 1024 * 1024)}],
    )

    assert "error" in r and r["error"].startswith("anexo recusado:")
    assert gmail_espiado._enviados == [], "não pode chegar a fazer a requisição"


@pytest.mark.asyncio
async def test_anexo_inexistente_devolve_erro_e_nao_envia_sem_anexo(gmail_espiado):
    """O pior resultado possível seria o e-mail sair SEM a proposta."""
    r = await gmail_espiado.send_message(
        access_token="T", to="a@x.com", subject="Proposta", body="Segue em anexo.",
        attachments=[{"path": "/nao/existe.pdf"}],
    )

    assert "error" in r
    assert gmail_espiado._enviados == [], "saiu o e-mail prometendo anexo que não foi"


# ============================== `POST /api/whatsapp/send-document` — a rota nova


@pytest.fixture(scope="module")
def client():
    os.environ["INTEL_API_KEY"] = "chave-de-teste-anexo"
    from fastapi.testclient import TestClient

    import main

    return TestClient(main.app, raise_server_exceptions=False)


AUTH = {"X-API-Key": "chave-de-teste-anexo"}


@pytest.fixture
def evolution_espiada(monkeypatch):
    """`send_document` espionado — nada sai para a Evolution."""
    from integrations import evolution_api as ev

    chamadas = []

    async def _spy(**kwargs):
        chamadas.append(kwargs)
        return {"key": {"id": "WA1"}}

    cliente = ev.get_evolution_client()
    monkeypatch.setattr(cliente, "send_document", _spy)
    return chamadas


def test_rota_existe_e_exige_credencial(client):
    """Dispara integração externa: sem cookie e sem key, 401 — nunca 200."""
    r = client.post("/api/whatsapp/send-document",
                    json={"phone": "5511999999999", "document": "BASE64=="})
    assert r.status_code == 401, f"respondeu {r.status_code}: {r.text[:200]}"


def test_caminho_local_vira_base64_com_nome_e_mimetype(client, evolution_espiada, tmp_path):
    """O caso de 04/11 e 06/11: o arquivo está no disco, não numa URL."""
    peca = tmp_path / "Certidao_Pitangueiras.pdf"
    peca.write_bytes(PDF_FALSO)

    r = client.post("/api/whatsapp/send-document", headers=AUTH, json={
        "phone": "41991064663",
        "document": str(peca),
        "caption": "Segue a certidão.",
    })

    assert r.status_code == 200, r.text[:300]
    assert len(evolution_espiada) == 1
    enviado = evolution_espiada[0]
    assert enviado["filename"] == "Certidao_Pitangueiras.pdf", \
        "o nome sai do basename — o WhatsApp exibe este nome"
    assert enviado["mimetype"] == "application/pdf"
    assert base64.b64decode(enviado["document_url"]) == PDF_FALSO
    assert enviado["caption"] == "Segue a certidão."
    assert enviado["phone"] == "41991064663", \
        "a normalização é do evolution_api, que acerta DDD fora de SP"


def test_url_publica_passa_adiante_sem_virar_base64(client, evolution_espiada):
    r = client.post("/api/whatsapp/send-document", headers=AUTH, json={
        "phone": "5511999999999",
        "document": "https://exemplo.com/proposta.pdf",
        "filename": "proposta.pdf",
    })

    assert r.status_code == 200, r.text[:300]
    assert evolution_espiada[0]["document_url"] == "https://exemplo.com/proposta.pdf"


def test_base64_cru_passa_adiante(client, evolution_espiada):
    b64 = base64.b64encode(PDF_FALSO).decode()
    r = client.post("/api/whatsapp/send-document", headers=AUTH, json={
        "phone": "5511999999999", "document": b64, "filename": "p.pdf",
    })

    assert r.status_code == 200, r.text[:300]
    assert evolution_espiada[0]["document_url"] == b64


def test_url_sem_filename_e_recusada(client, evolution_espiada):
    """Sem nome, o WhatsApp exibe um arquivo anônimo — recusar é melhor."""
    r = client.post("/api/whatsapp/send-document", headers=AUTH, json={
        "phone": "5511999999999", "document": "https://exemplo.com/x",
    })

    assert r.status_code == 400
    assert evolution_espiada == []


def test_phone_e_document_sao_obrigatorios(client, evolution_espiada):
    assert client.post("/api/whatsapp/send-document", headers=AUTH,
                       json={"document": "BASE64=="}).status_code == 400
    assert client.post("/api/whatsapp/send-document", headers=AUTH,
                       json={"phone": "5511999999999"}).status_code == 400
    assert evolution_espiada == []


# ------------------------------------------------------------ guarda de classe
#
# POR AST, não por substring: os nomes dos clientes aparecem em comentário e
# docstring (inclusive na prosa que EXPLICA por que um deles não é usado), e
# `"send_media" not in fonte` reprovaria código certo — já custou três falsos
# positivos na guarda do `MIMEText`.


def _chamadas_no_trecho(fonte, inicio, tamanho=6000):
    """Nomes de função CHAMADOS no trecho que começa em `inicio`.

    Comentário e docstring não produzem nó `Call`, então ficam de fora por
    construção — que é exatamente o ponto.
    """
    import ast
    import textwrap

    i = fonte.index(inicio)
    trecho = fonte[i:i + tamanho]
    # Corta na próxima rota pra não varrer o vizinho.
    j = trecho.find("\n@app.", 10)
    if j > 0:
        trecho = trecho[:j]
    arvore = ast.parse(textwrap.dedent(trecho))

    nomes = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Call):
            f = no.func
            if isinstance(f, ast.Name):
                nomes.add(f.id)
            elif isinstance(f, ast.Attribute):
                nomes.add(f.attr)
    return nomes


def test_a_rota_usa_o_send_document_consertado_e_nao_o_cliente_antigo():
    """`whatsapp.send_media` reproduziria os três defeitos de 30/09.

    Ele não manda `fileName` nem `mimetype` (o PPTX chega genérico), usa timeout
    de 30s e não tem guarda de `media` vazio — e o `_normalize_phone` dele só
    acerta DDD de São Paulo. Envolvê-lo seria criar o segundo caminho pelo qual
    o defeito volta.
    """
    with open(os.path.join(_ROOT, "app", "main.py"), encoding="utf-8") as fh:
        fonte = fh.read()

    chamadas = _chamadas_no_trecho(fonte, '@app.post("/api/whatsapp/send-document"')

    assert "send_document" in chamadas, "a rota parou de chamar a função consertada"
    assert "get_evolution_client" in chamadas
    assert "send_media" not in chamadas, \
        "a rota passou a usar o cliente antigo — os 3 defeitos de 30/09 voltam"


def test_send_message_do_gmail_nao_remonta_o_mime():
    """A montagem tem de estar em `services.gmail_mime`, uma só.

    Esta função montava `MIMEText` — a classe que descartou o `cc` do Orestes em
    silêncio. Dois montadores é manter a porta por onde o defeito volta.
    """
    import ast

    caminho = os.path.join(_ROOT, "app", "integrations", "gmail.py")
    with open(caminho, encoding="utf-8") as fh:
        arvore = ast.parse(fh.read())

    chamadas = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Call):
            f = no.func
            if isinstance(f, ast.Name):
                chamadas.add(f.id)
            elif isinstance(f, ast.Attribute):
                chamadas.add(f.attr)

    assert "MIMEText" not in chamadas, "gmail.py voltou a chamar MIMEText — cc e anexo somem calados"
    assert "MIMEMultipart" not in chamadas
    assert "monta_mensagem" in chamadas, "parou de delegar a montagem"
