"""Resolve `<N>@lid` → JID com telefone, por cruzamento de `message_id`.

POR QUE EXISTE, com o preço (14-16/10/2026). Duas quedas do WhatsApp em 24h
somaram ~26h de cegueira. O grupo foi recuperado nas duas (145 + 35
mensagens); a DM foi **ZERO nas duas**, e não por não existir: a Evolution
tinha 33 DMs da primeira janela, os replays devolveram 0 linhas, e o
`webhook_audit` registrou `unresolvable_jid` 33 vezes.

A CAUSA, e ela é uma assimetria entre dois caminhos da mesma API:

- **ao vivo**, a Evolution já entrega o JID resolvido — o webhook traz
  `remoteJid = 5511986415942@s.whatsapp.net` com `addressingMode: "lid"`
  apenas sinalizando que o chat é LID;
- **em replay** (`/chat/findMessages`, que lê o banco dela), o mesmo chat vem
  como `remoteJid = 129167014264951@lid` e **`remoteJidAlt` vazio**.

Sem telefone não há contato a resolver, e o handler descarta. Então DM de
contato migrado para LID entra ao vivo e **nunca** volta em replay — e a
migração está longe de ser marginal: `findContacts` devolve 3.687 de 8.059
contatos em `@lid`.

⚠️ E a régua que me enganou antes de medir isto: contar
`webhook_audit.remote_jid LIKE '%@lid'` dava **1 em 30 dias**, e com isso eu
descartei LID como risco. O audit só registra o que ENTROU, e o que entra já
vem resolvido — eu estava medindo a ausência do bug como se fosse a ausência
do problema ([[feedback_mecanismo_que_nao_mede_o_denominador]]).

COMO RESOLVE, sem heurística. O `findChats` traz `pushName` ("Emma") e casar
nome contra `contacts` seria tentador e ambíguo. O caminho determinístico é
outro: **o mesmo `message_id` existe nos dois lados.** Pede-se ao
`findMessages` as mensagens daquele chat LID, colhem-se os ids, e procura-se
esses ids no que já entrou ao vivo — ali o `remote_jid` é o telefone. Medido
no caso da Emma: 60 ids colhidos, 36 eventos apontando para
`5511986415942@s.whatsapp.net`, e o contato #Emma Sakamoto com esse número.

GUARDA DE AMBIGUIDADE. Se os ids apontarem para mais de um JID com peso
parecido, **não resolve** — devolve `None`. Um id colidido ou um chat
remontado valem menos que gravar mensagem na ficha do contato errado, que é
dano silencioso e difícil de desfazer.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Quantas mensagens do chat LID pedir à Evolution. 60 foi o medido como
# suficiente (o caso real resolveu com 36 das 60 cruzando); pedir muito mais
# só aumenta o custo da chamada sem mudar o veredito.
IDS_POR_CHAT = int((os.getenv("WA_LID_IDS_POR_CHAT") or "60").strip())

# Vantagem mínima do vencedor sobre o 2º colocado para o veredito valer.
# Com 2 JIDs empatados, gravar na ficha errada é pior que não gravar.
MARGEM_MINIMA = 3

_cache: Dict[str, Optional[str]] = {}


def _evolution() -> tuple[str, str, str]:
    base = (os.getenv("EVOLUTION_API_URL") or "").strip().strip('"').rstrip("/")
    key = (os.getenv("EVOLUTION_API_KEY") or "").strip().strip('"')
    inst = (os.getenv("EVOLUTION_INSTANCE") or "rap-whatsapp").strip()
    return base, key, inst


def _message_ids_do_chat(lid: str) -> list:
    """Ids das mensagens que a Evolution tem para este chat LID."""
    base, key, inst = _evolution()
    if not base or not key:
        return []
    req = urllib.request.Request(
        f"{base}/chat/findMessages/{inst}",
        data=json.dumps(
            {"where": {"key": {"remoteJid": lid}}, "page": 1, "offset": IDS_POR_CHAT}
        ).encode(),
        method="POST",
        headers={"apikey": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read())
    except Exception as e:
        logger.warning(f"lid_resolver: findMessages({lid}) falhou: {e}")
        return []

    if isinstance(payload, list):
        registros = payload
    else:
        registros = (payload.get("messages") or {}).get("records") or []
    ids = []
    for m in registros:
        mid = (m.get("key") or {}).get("id")
        if mid:
            ids.append(mid)
    return ids


def _jid_mais_votado(conn, ids: list) -> Optional[str]:
    """Qual JID com telefone aparece para esses ids no que JÁ entrou.

    Olha `webhook_audit`, que é onde o JID resolvido pela Evolution ficou
    gravado. Exige vencedor com folga: ver `MARGEM_MINIMA`.
    """
    if not ids:
        return None
    cur = conn.cursor()
    cur.execute(
        """
        SELECT remote_jid, count(*) AS n
          FROM webhook_audit
         WHERE message_id = ANY(%s)
           AND remote_jid IS NOT NULL
           AND remote_jid NOT LIKE '%%@lid'
           AND remote_jid NOT LIKE '%%@g.us'
         GROUP BY 1
         ORDER BY n DESC
         LIMIT 2
        """,
        (ids,),
    )
    linhas = cur.fetchall()
    if not linhas:
        return None

    def _val(linha, chave, pos):
        try:
            return linha[chave]
        except (TypeError, KeyError, IndexError):
            return linha[pos]

    vencedor, n1 = _val(linhas[0], "remote_jid", 0), _val(linhas[0], "n", 1)
    if len(linhas) > 1:
        n2 = _val(linhas[1], "n", 1)
        if (n1 - n2) < MARGEM_MINIMA:
            logger.warning(
                f"lid_resolver: AMBÍGUO ({vencedor} com {n1} vs 2º com {n2}) — "
                "não resolvo; gravar na ficha errada é pior que não gravar"
            )
            return None
    return vencedor


def resolve_lid(lid: str, conn) -> Optional[str]:
    """`<N>@lid` → `<telefone>@s.whatsapp.net`, ou `None` se não der.

    `None` nunca é um palpite: significa "não sei", e quem chama deve deixar a
    mensagem de fora em vez de atribuí-la a alguém.
    """
    if not lid or "@lid" not in lid:
        return None
    if lid in _cache:
        return _cache[lid]

    resolvido = _jid_mais_votado(conn, _message_ids_do_chat(lid))
    _cache[lid] = resolvido
    if resolvido:
        logger.info(f"lid_resolver: {lid} → {resolvido}")
    else:
        logger.info(f"lid_resolver: {lid} não resolvido")
    return resolvido


def injeta_alt(registro: Dict, conn) -> bool:
    """Preenche `key.remoteJidAlt` do registro quando o JID é LID.

    Por que `remoteJidAlt` e não sobrescrever o `remoteJid`: o handler de
    webhook JÁ sabe preferir o `alt` quando o jid é LID
    ([[reference_whatsapp_lid_migration]]). Preenchendo o campo que ele espera,
    a recuperação passa pelo MESMO caminho de gravação do tráfego ao vivo —
    sem um segundo caminho que divirja do primeiro em silêncio.

    Devolve True se injetou algo.
    """
    chave = registro.get("key")
    if not isinstance(chave, dict):
        return False
    jid = str(chave.get("remoteJid") or "")
    if "@lid" not in jid:
        return False
    if chave.get("remoteJidAlt"):
        return False
    telefone = resolve_lid(jid, conn)
    if not telefone:
        return False
    chave["remoteJidAlt"] = telefone
    return True
