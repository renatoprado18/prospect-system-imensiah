"""Texto EFETIVO de uma mensagem de WhatsApp — o que ela de fato diz.

O defeito que este modulo fecha (08/09/26): o pipeline de anexos transcreve
audio (whisper-large-v3) e faz OCR de imagem ha meses, gravando em
`wa_attachments.extracted_text` — 386 de 412 audios, 3.027 imagens. E NENHUM
leitor de conversa usava esse texto. Todos liam `messages.conteudo`, que para
midia guarda so o literal `[Audio]` / `[Imagem]`.

O custo nao foi teorico. Os 3 audios da Andressa de 24/08 19h34 — em que ela
relata que Itau e Banco do Brasil recusaram os extratos da Carambola e propoe
pedir ao juiz — estavam transcritos no banco desde o dia seguinte. A camada
executiva os tratou como canal cego por duas semanas, com prazo em 11/09, e o
que se soube veio do Renato ouvir e relatar. Os 2 audios do Kesley de 05/09
(valor da outorga) idem. [[feedback_consumidor_morto_wiring]]

Por que SUBQUERY ESCALAR e nao LEFT JOIN: `wa_attachments` e UNIQUE por
(message_id, kind), nao por message_id — uma mensagem com imagem E pdf daria
DUAS linhas e um JOIN duplicaria silenciosamente cada mensagem no resultado.
Hoje nenhuma tem mais de um anexo, mas "hoje nenhuma tem" nao e invariante.
A subquery tambem e drop-in: troca `m.conteudo` na SELECT list sem exigir
mudanca no FROM, no GROUP BY nem nos aliases de quem chama.
"""
from typing import Optional


def texto_efetivo_sql(
    alias_msg: str = "m",
    col_texto: str = "conteudo",
    col_id: str = "external_id",
) -> str:
    """Expressao SQL que devolve o texto extraido do anexo, ou o conteudo.

    Args:
        alias_msg: alias da tabela de mensagens na query do chamador ("m", "g").
        col_texto: coluna de texto — `conteudo` em `messages`, `content` em
            `group_messages`.
        col_id: coluna que casa com `wa_attachments.message_id` —
            `external_id` em `messages`, `message_id` em `group_messages`.

    Uso:
        SELECT {texto_efetivo_sql()} AS conteudo, m.direcao FROM messages m ...

    `NULLIF(..., '')` porque extracao que falhou grava string vazia, e nesse
    caso o placeholder original e mais informativo que nada. `ORDER BY id`
    para desempate estavel quando ha mais de um anexo na mesma mensagem.
    """
    return (
        "COALESCE("
        "NULLIF((SELECT wa_x.extracted_text FROM wa_attachments wa_x"
        f" WHERE wa_x.message_id = {alias_msg}.{col_id}"
        " AND wa_x.extracted_text IS NOT NULL AND wa_x.extracted_text <> ''"
        " ORDER BY wa_x.id LIMIT 1), '')"
        f", {alias_msg}.{col_texto})"
    )


def texto_efetivo_grupo_sql(alias_msg: str = "g") -> str:
    """Atalho para `group_messages` (colunas `content` / `message_id`)."""
    return texto_efetivo_sql(alias_msg, col_texto="content", col_id="message_id")


# Placeholders que o ingest grava quando a mensagem e midia. Duas grafias por
# canal ([Audio] sem acento vem do caminho de grupo, [Áudio] do de DM) — quem
# casar por so uma acha metade e conclui ausencia, que foi como a medicao de
# 08/09 se enganou nos dois sentidos.
PLACEHOLDERS_MIDIA = (
    "[Áudio]", "[Audio]", "[Imagem]", "[Vídeo]", "[Video]",
    "[Figurinha]", "[Sticker]",
)


def e_placeholder_midia(texto: Optional[str]) -> bool:
    """True se o texto e so o marcador de midia, sem conteudo avaliado.

    Serve a quem precisa DISTINGUIR "mensagem vazia" de "mensagem cujo
    conteudo nao foi lido" — tratar as duas como iguais e o que fazia um
    audio decisivo contar como silencio.
    """
    if not texto:
        return False
    t = texto.strip()
    if t in PLACEHOLDERS_MIDIA:
        return True
    # `[Documento: nome.pdf]` e `[Contato: Fulano]` sao variaveis
    return (t.startswith("[Documento:") or t.startswith("[Contato:")) and t.endswith("]")
