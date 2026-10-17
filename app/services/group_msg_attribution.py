"""
Atribuicao de contato as mensagens de GRUPO ja gravadas.

O PROBLEMA QUE ISTO RESOLVE
---------------------------
`group_message_sync` resolve `contact_id` uma unica vez, na linha do INSERT, e
grava com `ON CONFLICT (message_id) DO NOTHING`. Logo **mensagem ja gravada
nunca e reavaliada**: todo contato que entrou na base DEPOIS da mensagem ficou
orfao para sempre. Medido em 17/10/26 contra prod: **53.888 de 69.675 msgs de
grupo (77%) sem `contact_id`, e 11 dos 54 grupos com zero atribuicao**.

O efeito nao e cosmetico. O `board_hunt` ja soma grupo a DM desde 07/08
(`42f1313`) pra nao cobrar por DM o que foi respondido no grupo
([[feedback_cross_group_before_send]]) — mas com 77% das mensagens anonimas o
UNION nao tem a quem atribuir a conversa, e relacao que vive em grupo continua
sendo lida como fria. O Eduardo Marson (frente bh#7) tinha 1.137 mensagens de
grupo que nao contavam para a propria temperatura.

⚠️ NAO confundir com o caso que originou esta frente: os "336 dias" da bh#17
eram **duplicata de ficha** (#18219 fixo do escritorio x #26711 celular), nao
cegueira a grupo. Sao dois defeitos distintos; este modulo so trata o segundo.

A GUARDA, E POR QUE ELA E ASSIMETRICA
------------------------------------
Casar por telefone nao e seguro do mesmo jeito nos dois tipos de linha:

- **celular** e pessoal ⇒ o numero basta. O nome NAO pode ser requisito: o
  `pushName` e escrito pelo proprio remetente e chega como apelido, emoji ou
  rabisco ("👀", "🐱Janaira Franca🐱"). Exigir nome aqui jogaria fora o caso
  legitimo e deixaria a mensagem anonima — que e exatamente o defeito. A
  divergencia e CONTADA e devolvida, nunca silenciada.
- **fixo** e compartilhado ⇒ o numero nao identifica pessoa. E o caso Douglas x
  Orestes que `phone_kind` documenta. Aqui o nome e exigido.

E o LID nao e telefone: `group_message_sync` faz `.replace('@lid', '')` na
ingestao, entao um identificador de 15 digitos fica gravado no lugar do numero
e passa pelo `len > 8` da linha 308. Tentar casar isso com telefone nunca
acerta — e, pior, nao falha de forma visivel. LID se reconhece pelo tamanho
(telefone BR tem 10-13 digitos) e sai da fila declarado, nao descartado calado:
sao 18.196 msgs, e some-las aos "sem ficha" esconderia uma causa estrutural
dentro de um balde de causa diferente ([[feedback_mecanismo_que_nao_mede_o_denominador]]).

`from_me` fica de fora por decisao: a propria flag ja identifica o autor, e
apontar essas 631 msgs pra ficha do Renato (#23419) alimentaria as logicas que
casam "o terceiro" por ficha — o eco que o `session_locks` manda evitar.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from services.contact_dedup import normalize_phone
from services.contact_identity import (
    find_contact_by_phone,
    is_placeholder_name,
    names_match,
    phone_kind,
)

logger = logging.getLogger(__name__)

# Telefone BR tem 10-13 digitos (com ou sem DDI, com ou sem o 9 da Anatel).
# 14+ e LID do WhatsApp com o sufixo '@lid' removido pela ingestao.
LID_MIN_DIGITOS = 14

# Motivos — todo remetente examinado cai em exatamente um. A soma fecha com o
# total da fila de proposito: balde que nao fecha e balde que esconde.
ATRIBUIDA = "atribuida"
ATRIBUIDA_NOME_DIVERGE = "atribuida_celular_nome_diverge"
ATRIBUIDA_FICHA_PLACEHOLDER = "atribuida_ficha_placeholder"
PULADA_LID = "pulada_lid"
PULADA_SEM_FICHA = "pulada_sem_ficha"
PULADA_FIXO_NOME_DIVERGE = "pulada_fixo_nome_diverge"


def _digitos(valor: Any) -> str:
    return "".join(ch for ch in str(valor or "") if ch.isdigit())


def ficha_sem_nome_identificador(nome: Any) -> bool:
    """
    A ficha tem nome que identifica alguem, ou so um rotulo + o numero?

    `is_placeholder_name` casa o nome INTEIRO e por isso NAO pega o padrao real
    da base: **69 fichas em prod se chamam "Desconhecido +5511986236954"** —
    rotulo seguido do telefone. Ela e conservadora de proposito (e usada pelo
    merge, que apaga dados) e nao se mexe por causa deste caso; aqui a pergunta
    e outra e mais estreita: "da pra comparar nome contra esta ficha?".

    Tokens puramente numericos sao descartados antes do teste — o telefone no
    nome nao identifica ninguem, so repete o campo pelo qual a ficha foi achada.
    """
    base = str(nome or "")
    restante = " ".join(t for t in base.replace("+", " ").split()
                        if not _digitos(t) or not t.strip("+").isdigit())
    return is_placeholder_name(restante)


def resolver_remetente(cursor, sender_phone: Any,
                       sender_name: Any) -> Tuple[Optional[int], str]:
    """
    Decide a ficha de UM remetente de grupo. Devolve `(contact_id, motivo)`.

    Nao escreve. `contact_id` None sempre vem com motivo `pulada_*`, e todo
    motivo `atribuida_*` sempre vem com um id — o chamador nao precisa cruzar
    os dois pra saber o que aconteceu.
    """
    digitos = _digitos(sender_phone)
    if not digitos or len(digitos) >= LID_MIN_DIGITOS:
        return None, PULADA_LID

    ficha = find_contact_by_phone(cursor, sender_phone, columns="id, nome")
    if not ficha:
        return None, PULADA_SEM_FICHA

    nome_ficha = ficha.get("nome") or ""
    # Ficha placeholder ("Desconhecido +5511...") e dona legitima do numero: a
    # mensagem pertence a ela, e atribuir e o que permite a ficha ganhar nome
    # depois ([[feedback_passive_labeling_over_manual]]). Checar ANTES do
    # names_match, que nao tem o que comparar contra um placeholder.
    if ficha_sem_nome_identificador(nome_ficha):
        return ficha["id"], ATRIBUIDA_FICHA_PLACEHOLDER

    kind = phone_kind(normalize_phone(sender_phone) or digitos)
    bate = names_match(str(sender_name or ""), nome_ficha, kind=kind)

    if kind == "landline" and not bate:
        return None, PULADA_FIXO_NOME_DIVERGE
    if not bate:
        return ficha["id"], ATRIBUIDA_NOME_DIVERGE
    return ficha["id"], ATRIBUIDA


def atribuir_orfas(cursor, limite_remetentes: Optional[int] = None,
                   commit_cb=None) -> Dict[str, Any]:
    """
    Reavalia as mensagens de grupo sem `contact_id` e atribui o que casa.

    Trabalha por REMETENTE DISTINTO, nao por mensagem: a resolucao e uma query
    por telefone, e os 53.888 orfaos vem de 1.741 remetentes — por mensagem
    seriam 31x mais idas ao banco pro mesmo resultado.

    `limite_remetentes` corta a fila pelos remetentes de maior volume (o cron
    leva um pedaco por rodada). `commit_cb` e chamado a cada remetente aplicado:
    o Neon derruba conexao em lote longo sem erro, e commit por item e o que
    impede a rodada de morrer pendurada no socket.

    Devolve contagem por motivo + `msgs_atribuidas`. A soma dos remetentes por
    motivo fecha com `remetentes_examinados`.
    """
    cursor.execute(
        """
        SELECT sender_phone, MAX(sender_name) AS sender_name, COUNT(*) AS n
          FROM group_messages
         WHERE contact_id IS NULL AND NOT COALESCE(from_me, FALSE)
         GROUP BY sender_phone
         ORDER BY COUNT(*) DESC
        """
        + ("" if limite_remetentes is None else " LIMIT %s"),
        () if limite_remetentes is None else (limite_remetentes,),
    )
    fila = [dict(r) for r in cursor.fetchall()]

    motivos: Dict[str, int] = {}
    msgs: Dict[str, int] = {}
    msgs_atribuidas = 0
    divergencias: List[Dict[str, Any]] = []

    for item in fila:
        fone = item["sender_phone"]
        nome = item["sender_name"]
        contact_id, motivo = resolver_remetente(cursor, fone, nome)
        motivos[motivo] = motivos.get(motivo, 0) + 1
        msgs[motivo] = msgs.get(motivo, 0) + int(item["n"])

        if contact_id is None:
            continue

        # O UPDATE repete as condicoes da fila (inclusive `from_me`): entre a
        # leitura e a escrita o webhook pode ter gravado mensagem nova desse
        # mesmo remetente, e `contact_id IS NULL` garante que nao se sobrescreve
        # atribuicao que a ingestao ja fez certo.
        cursor.execute(
            """
            UPDATE group_messages
               SET contact_id = %s
             WHERE sender_phone = %s
               AND contact_id IS NULL
               AND NOT COALESCE(from_me, FALSE)
            """,
            (contact_id, fone),
        )
        msgs_atribuidas += cursor.rowcount

        if motivo == ATRIBUIDA_NOME_DIVERGE:
            divergencias.append({
                "sender_phone": fone,
                "sender_name": nome,
                "contact_id": contact_id,
                "msgs": int(item["n"]),
            })

        if commit_cb is not None:
            commit_cb()

    return {
        "remetentes_examinados": len(fila),
        "msgs_atribuidas": msgs_atribuidas,
        "por_motivo": motivos,
        "msgs_por_motivo": msgs,
        # Devolvidas inteiras, nao so contadas: celular com nome divergente e o
        # unico ramo que escreve sem confirmacao de nome, e se virar numero
        # grande a politica e que precisa mudar, nao o volume.
        "divergencias_de_nome": sorted(
            divergencias, key=lambda d: -d["msgs"])[:50],
    }


def orfandade(cursor) -> Dict[str, Any]:
    """
    Fotografia da orfandade, pra medir antes e depois sem recalcular a regra.

    Mede por CANAL (grupo), nao por tabela: a mesma licao do
    [[feedback_medidor_agregado_mascara_fonte_morta]] — total que desce nao
    prova que grupo nenhum ficou pra tras, e grupo com zero atribuicao e o que
    some num percentual global.
    """
    cursor.execute(
        """
        SELECT COUNT(*) AS total,
               COUNT(contact_id) AS com_contato,
               COUNT(*) FILTER (WHERE contact_id IS NULL
                                  AND NOT COALESCE(from_me, FALSE)) AS orfas
          FROM group_messages
        """
    )
    geral = dict(cursor.fetchone())

    cursor.execute(
        """
        SELECT COUNT(*) AS grupos_zerados
          FROM (SELECT group_jid
                  FROM group_messages
                 GROUP BY group_jid
                HAVING COUNT(contact_id) = 0) t
        """
    )
    geral.update(dict(cursor.fetchone()))

    total = int(geral["total"]) or 1
    geral["pct_com_contato"] = round(100.0 * int(geral["com_contato"]) / total, 1)
    return geral
