"""Montagem do MIME de rascunho/envio do Gmail — pura e testável.

POR QUE EXISTE SEPARADO (15/10/2026). O `create_draft` do gmail-proxy montava
o MIME inline, dentro de um endpoint que precisa de conta Google, token e rede
para ser exercitado. Testar aquilo de verdade era inviável, e o que se escreveu
no lugar foi um ESPELHO da lógica no arquivo de teste — que passava verde com o
`main.py` revertido, porque testava a si mesmo. Só as guardas de classe (ler o
fonte e procurar `MIMEText(`) pegavam a regressão, e guarda de classe prova que
o texto mudou, não que o programa está certo
([[feedback_fake_frouxo_da_verde_a_bug_real]]).

Com a montagem aqui, o teste chama o CÓDIGO que roda em produção.

O DEFEITO QUE ORIGINOU TUDO. Até 10/10/2026 a montagem era um `MIMEText` com
só `To` e `Subject`: o `cc` passado em `params` era **jogado fora sem erro**. O
e-mail à Fernanda saiu sem o Orestes em cópia e o corpo terminava com "Pai,
consegue mandar para ela?" — dirigido a quem não recebeu, e teve de ser
reenviado à mão. Parâmetro aceito e descartado em silêncio é pior que parâmetro
recusado: quem chama não tem como saber que não funcionou.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
from email.message import EmailMessage
from typing import Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

# Teto do Gmail: 25 MB, e ele mede a MENSAGEM, não o arquivo. O MIME codifica
# anexo em base64, que infla ~33% — 20 MB de PDF viram ~27 MB de mensagem e são
# recusados. Conferir o arquivo contra 25 MB deixaria passar justamente o caso
# de borda, e o erro voltaria da API do Google como um 400 opaco, depois de
# subir 27 MB. Por isso a guarda compara o TAMANHO ESTIMADO DA MENSAGEM.
LIMITE_GMAIL_BYTES = 25 * 1024 * 1024


class AnexoInvalido(ValueError):
    """Anexo que não pode ser montado (sem nome, sem conteúdo, acima do teto).

    Existe porque os dois consumidores querem tratamentos OPOSTOS, e tratá-los
    igual erra num dos dois:

    - **rascunho** (`create_draft`): anexo ruim é ignorado **com log**. Perder o
      anexo é ruim; perder o rascunho com o texto já escrito é pior, e o Renato
      vê o rascunho antes de despachar — há revisão humana depois do defeito.
    - **envio** (`send_message`): anexo ruim **aborta**. Não há revisão depois:
      o e-mail chega ao destinatário sem a proposta e ninguém é avisado. Foi
      exatamente o modo de falha do `cc` descartado em silêncio (10/10/2026),
      só que com o arquivo no lugar do endereço.
    """


def normaliza_enderecos(valor: Union[None, str, Sequence]) -> str:
    """Aceita `"a@x, b@y"` OU `["a@x", "b@y"]` e devolve o cabeçalho.

    A camada manda os dois formatos. Uma lista crua atribuída ao cabeçalho
    viraria `['a@x']` e o Gmail silenciaria o endereço — exatamente o modo de
    falha do incidente de 10/10, uma camada acima: aceito, não recusado, e
    perdido.
    """
    if not valor:
        return ""
    if isinstance(valor, (list, tuple, set)):
        itens = [str(v).strip() for v in valor]
    else:
        itens = [p.strip() for p in str(valor).split(",")]
    return ", ".join([i for i in itens if i])


def decodifica_anexo(content_b64: str) -> bytes:
    """Decodifica base64 padrão OU base64URL, com ou sem padding.

    O `get_attachment` deste mesmo proxy devolve base64URL **sem padding**, que
    é como o Gmail entrega. Reanexar o que o proxy acabou de baixar não pode
    exigir recompor padding à mão, senão o caminho natural — baixar de uma
    thread, anexar noutra — falha por detalhe de alfabeto. `urlsafe_b64decode`
    também aceita o alfabeto padrão, então um caminho cobre os dois.
    """
    texto = str(content_b64)
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def carrega_anexo(anexo: Optional[Dict]) -> Tuple[str, bytes, str, str]:
    """Um spec de anexo → `(nome, bytes, maintype, subtype)`, ou `AnexoInvalido`.

    Três formas de passar o conteúdo, porque o caso de uso real usa duas delas:

    - `content_b64` — base64 padrão ou base64URL, com ou sem padding. É o que o
      `get_attachment` do mesmo proxy devolve.
    - `content_bytes` — `bytes` crus, para quem já tem o arquivo em memória.
    - **`path`** — caminho de arquivo local. **É o caso de uso real**: as peças
      vivem em `~/cockpit`, e exigir que o chamador leia o arquivo e faça o
      base64 à mão é o atrito que manda o trabalho para fora do sistema — foi o
      que aconteceu em 06/11/2026 com a proposta da Building 8.

    `filename` é opcional quando há `path` (sai do basename): o WhatsApp e o
    Gmail mostram esse nome, e repetir o nome do arquivo que já está no caminho
    é só mais uma chance de divergirem.

    `mimetype` inferido do nome pela stdlib quando não informado — sem ele o
    PPTX/DOCX chega como arquivo genérico. É a mesma lição do `send_document`
    da Evolution, e vale igual aqui.
    """
    anexo = anexo or {}
    caminho = anexo.get("path")
    nome = anexo.get("filename")

    if anexo.get("content_bytes") is not None:
        conteudo = bytes(anexo["content_bytes"])
    elif anexo.get("content_b64"):
        try:
            conteudo = decodifica_anexo(anexo["content_b64"])
        except Exception as e:
            raise AnexoInvalido(f"anexo {nome or '?'}: base64 ilegível ({e})")
    elif caminho:
        caminho = os.path.expanduser(str(caminho))
        if not os.path.isfile(caminho):
            raise AnexoInvalido(f"anexo: arquivo não encontrado em {caminho}")
        try:
            with open(caminho, "rb") as fh:
                conteudo = fh.read()
        except OSError as e:
            raise AnexoInvalido(f"anexo {caminho}: ilegível ({e})")
        nome = nome or os.path.basename(caminho)
    else:
        raise AnexoInvalido(
            f"anexo {nome or '?'}: falta content_b64, content_bytes ou path"
        )

    if not nome:
        raise AnexoInvalido("anexo: falta filename (o Gmail mostra este nome)")

    tipo = anexo.get("mimetype") or mimetypes.guess_type(nome)[0] or "application/octet-stream"
    maintype, _, subtype = tipo.partition("/")
    return nome, conteudo, (maintype or "application"), (subtype or "octet-stream")


def valida_tamanho(total_bytes: int) -> None:
    """Recusa aqui o que o Gmail recusaria depois — com o número na mensagem.

    Sem esta guarda o caminho é: codificar, subir 27 MB e receber um 400 cujo
    corpo não diz que o problema é tamanho. Recusar antes de tentar, nomeando o
    bruto E o estimado, é a diferença entre "anexo grande demais" e "erro 400".
    """
    estimado = int(total_bytes * 4 / 3)
    if estimado > LIMITE_GMAIL_BYTES:
        raise AnexoInvalido(
            f"anexos somam {total_bytes / 1048576:.1f} MB, que viram "
            f"~{estimado / 1048576:.1f} MB de mensagem em base64 — o Gmail "
            f"recusa acima de {LIMITE_GMAIL_BYTES / 1048576:.0f} MB. "
            f"Use link do Drive ou divida o envio."
        )


def monta_mensagem(
    to: Union[None, str, Sequence] = None,
    subject: str = "",
    body: str = "",
    cc: Union[None, str, Sequence] = None,
    bcc: Union[None, str, Sequence] = None,
    attachments: Optional[List[Dict]] = None,
    html_body: Optional[str] = None,
    estrito: bool = False,
) -> EmailMessage:
    """Monta o `EmailMessage` completo.

    `EmailMessage` em vez de `MIMEText` porque o defeito não era o `cc`
    isolado: era a classe não ter onde pôr cópia, cópia oculta nem anexo.

    `attachments`: `[{filename?, content_b64|content_bytes|path, mimetype?}]`.

    `estrito` decide o que fazer com anexo ruim, e os dois valores existem por
    um motivo (ver `AnexoInvalido`): `False` (rascunho) ignora com log, `True`
    (envio) levanta. O default é o antigo de propósito — quem já chamava isto
    era o `create_draft`.
    """
    msg = EmailMessage()
    msg["To"] = normaliza_enderecos(to)
    for valor, cabecalho in ((cc, "Cc"), (bcc, "Bcc")):
        normalizado = normaliza_enderecos(valor)
        if normalizado:
            # Só cria o cabeçalho se houver endereço: um `Cc:` vazio é lixo
            # visível para quem abre o e-mail.
            msg[cabecalho] = normalizado
    msg["Subject"] = subject or ""
    msg.set_content(body or "")
    if html_body:
        # `multipart/alternative` — o cliente escolhe. Com anexo, a stdlib
        # reaninha isto sozinha em `multipart/mixed` ao `add_attachment`.
        msg.add_alternative(html_body, subtype="html")

    carregados = []
    total = 0
    for anexo in (attachments or []):
        try:
            nome, conteudo, maintype, subtype = carrega_anexo(anexo)
        except AnexoInvalido as e:
            if estrito:
                raise
            logger.warning(f"monta_mensagem: anexo ignorado — {e}")
            continue
        total += len(conteudo)
        carregados.append((nome, conteudo, maintype, subtype))

    if carregados:
        try:
            valida_tamanho(total)
        except AnexoInvalido:
            if estrito:
                raise
            logger.warning(
                f"monta_mensagem: {total / 1048576:.1f} MB de anexo acima do teto "
                f"do Gmail — anexos descartados, corpo preservado"
            )
            carregados = []

    for nome, conteudo, maintype, subtype in carregados:
        msg.add_attachment(conteudo, maintype=maintype, subtype=subtype, filename=nome)
    return msg


def monta_raw(params: Dict, estrito: bool = False) -> str:
    """`params` do gmail-proxy → `raw` base64URL que a Gmail API espera."""
    msg = monta_mensagem(
        to=params.get("to"),
        subject=params.get("subject", ""),
        body=params.get("body", ""),
        cc=params.get("cc"),
        bcc=params.get("bcc"),
        attachments=params.get("attachments"),
        html_body=params.get("html_body"),
        estrito=estrito,
    )
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()
