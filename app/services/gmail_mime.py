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
from email.message import EmailMessage
from typing import Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)


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


def monta_mensagem(
    to: Union[None, str, Sequence] = None,
    subject: str = "",
    body: str = "",
    cc: Union[None, str, Sequence] = None,
    bcc: Union[None, str, Sequence] = None,
    attachments: Optional[List[Dict]] = None,
) -> EmailMessage:
    """Monta o `EmailMessage` completo.

    `EmailMessage` em vez de `MIMEText` porque o defeito não era o `cc`
    isolado: era a classe não ter onde pôr cópia, cópia oculta nem anexo.

    `attachments`: `[{filename, content_b64, mimetype?}]`. Anexo incompleto ou
    ilegível é **ignorado com log**, não exceção — perder o anexo é ruim;
    perder o rascunho inteiro, com o texto já escrito, é pior.
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

    for anexo in (attachments or []):
        nome = (anexo or {}).get("filename")
        bruto_b64 = (anexo or {}).get("content_b64")
        if not nome or not bruto_b64:
            logger.warning("create_draft: anexo ignorado, falta filename ou content_b64")
            continue
        tipo = (anexo.get("mimetype") or "application/octet-stream")
        maintype, _, subtype = tipo.partition("/")
        try:
            conteudo = decodifica_anexo(bruto_b64)
        except Exception as e:
            logger.warning(f"create_draft: anexo {nome} ilegível: {e}")
            continue
        msg.add_attachment(
            conteudo,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=nome,
        )
    return msg


def monta_raw(params: Dict) -> str:
    """`params` do gmail-proxy → `raw` base64URL que a Gmail API espera."""
    msg = monta_mensagem(
        to=params.get("to"),
        subject=params.get("subject", ""),
        body=params.get("body", ""),
        cc=params.get("cc"),
        bcc=params.get("bcc"),
        attachments=params.get("attachments"),
    )
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()
