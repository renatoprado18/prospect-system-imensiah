"""
Mapa LID → telefone: colheita, consulta e resolucao.

O PROBLEMA
----------
O WhatsApp identifica participante por LID (`<digitos>@lid`), que nao e telefone
e nao casa com ficha nenhuma. A ingestao faz `.replace('@lid','')` e grava os
digitos no campo `sender_phone`, onde eles passam por telefone e nunca acertam.
Medido em prod em 17/10/26: **17.968 mensagens de grupo de 692 LIDs distintos em
49 grupos**, mais as 25 DMs que as quedas de 15-16/10 fecharam como
`unresolvable_jid`.

O backlog registrava isso como IRRECUPERAVEL, e a conclusao vinha de uma medida
correta sobre o caminho errado: o `findMessages` nao devolve `remoteJidAlt` em
replay, logo **LID nao volta pela MENSAGEM**. Mas ele volta pelo GRUPO:
`GET /group/findGroupInfos` devolve, por participante, o par
`{id: "<lid>@lid", phoneNumber: "<e164>@s.whatsapp.net"}`. Nos 6 grupos com mais
LID, 276 de 276 participantes vieram com telefone.

⚠️ A licao que isto repete: "irrecuperavel" era "irrecuperavel por ESTE
caminho". Antes de registrar impossibilidade, nomear o caminho testado
([[feedback_ausencia_concluida_de_corpus_parcial]]).

LIMITE HONESTO
--------------
O mapa so conhece LID de quem e participante de um grupo que o Renato tambem
participa. LID de DM com quem nunca esteve num grupo comum **nao tem como ser
resolvido aqui** — e sai declarado, nao somado aos resolvidos.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Telefone BR/internacional tem 10-15 digitos; LID vem com 14+. A faixa 14-15 e
# ambigua no tamanho, e por isso o LID NUNCA e inferido por comprimento aqui:
# quem chama diz se o valor e LID (veio de `@lid`) ou telefone.
_SO_DIGITOS = re.compile(r"\D")

FONTE_GROUP_INFOS = "group_infos"


def so_digitos(valor: Any) -> str:
    return _SO_DIGITOS.sub("", str(valor or ""))


def _extrair_par(participante: Dict) -> Optional[Tuple[str, str]]:
    """
    Tira o par (lid, telefone) de um participante do `findGroupInfos`.

    O `id` vem como `<digitos>@lid` (ou `<fone>@s.whatsapp.net` em instancia
    antiga) e o `phoneNumber` como `<e164>@s.whatsapp.net`. Participante sem
    `phoneNumber` devolve None — e o caso em que a Evolution nao conhece o
    numero, e inventar um casamento ali seria pior que nao ter o par.
    """
    bruto_id = str(participante.get("id") or "")
    bruto_fone = str(participante.get("phoneNumber") or "")
    if "@lid" not in bruto_id:
        return None                      # ja e telefone: nao e par de mapa
    lid = so_digitos(bruto_id)
    fone = so_digitos(bruto_fone)
    if not lid or not (10 <= len(fone) <= 15):
        return None
    return lid, fone


async def colher_grupo(group_jid: str, timeout: float = 30.0) -> List[Tuple[str, str]]:
    """
    Busca os pares (lid, telefone) de UM grupo. Nao escreve.

    Devolve lista vazia em qualquer falha (sem levantar): a colheita e
    oportunista e um grupo que nao responde nao pode derrubar a rodada. Quem
    chama conta os vazios — ver `colher_todos`.
    """
    base = (os.getenv("EVOLUTION_API_URL") or "").strip()
    key = (os.getenv("EVOLUTION_API_KEY") or "").strip()
    inst = (os.getenv("EVOLUTION_INSTANCE") or "default").strip()
    if not base:
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.get(f"{base}/group/findGroupInfos/{inst}",
                              params={"groupJid": group_jid},
                              headers={"apikey": key})
            if r.status_code != 200:
                logger.warning("findGroupInfos %s: HTTP %s", group_jid, r.status_code)
                return []
            d = r.json()
    except Exception as e:
        logger.warning("findGroupInfos %s falhou: %s", group_jid, e)
        return []

    pares = []
    for p in (d.get("participants") or []):
        par = _extrair_par(p)
        if par:
            pares.append(par)
    return pares


def gravar_pares(cursor, pares: List[Tuple[str, str]], origem_jid: str,
                 fonte: str = FONTE_GROUP_INFOS) -> Dict[str, int]:
    """
    Persiste pares no mapa. Devolve {'novos', 'conflitos', 'iguais'}.

    ⚠️ CONFLITO E SINAL, NAO RUIDO: o mesmo LID apontando para outro telefone
    significa ou que a pessoa trocou de numero mantendo o LID, ou que um dos
    dois pares esta errado. Aqui o par ANTIGO e preservado e o conflito e
    CONTADO — sobrescrever calado faria a identidade de uma mensagem depender
    de qual grupo foi colhido por ultimo.

    EM LOTE, nao par por par: a 1a versao fazia um SELECT por par e gravou 13
    pares em alguns minutos contra o Neon (6.668 pares dariam >13 mil queries).
    Pior que a lentidao do backfill, isso **nao caberia no cron**, cujo budget e
    de 200s — um ramo que so falha em producao sob carga.
    """
    if not pares:
        return {"novos": 0, "conflitos": 0, "iguais": 0}

    # dedup intra-lote: o mesmo LID pode vir 2x do mesmo grupo
    quer = {}
    for lid, fone in pares:
        quer.setdefault(lid, fone)

    cursor.execute("SELECT lid, phone FROM wa_lid_map WHERE lid = ANY(%s)",
                   (list(quer.keys()),))
    existentes = {}
    for r in cursor.fetchall():
        if isinstance(r, dict):
            existentes[r["lid"]] = r["phone"]
        else:
            existentes[r[0]] = r[1]

    conflitos = iguais = 0
    a_inserir = []
    for lid, fone in quer.items():
        atual = existentes.get(lid)
        if atual is None:
            a_inserir.append((lid, fone, fonte, origem_jid))
        elif atual == fone:
            iguais += 1
        else:
            conflitos += 1
            logger.warning("LID %s: mapa tem %s, grupo %s diz %s — mantido o antigo",
                           lid, atual, origem_jid, fone)

    novos = 0
    if a_inserir:
        # `execute_values` manda tudo num round-trip. O `DO NOTHING` continua
        # sendo a trava real contra corrida: entre o SELECT e o INSERT outra
        # rodada pode ter gravado o mesmo LID.
        from psycopg2.extras import execute_values
        execute_values(
            cursor,
            """INSERT INTO wa_lid_map (lid, phone, fonte, origem_jid)
               VALUES %s ON CONFLICT (lid) DO NOTHING""",
            a_inserir,
        )
        novos = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    return {"novos": novos, "conflitos": conflitos, "iguais": iguais}


def telefone_de_lid(cursor, lid: Any) -> Optional[str]:
    """Telefone de um LID, ou None. Nao escreve, nao infere."""
    d = so_digitos(lid)
    if not d:
        return None
    cursor.execute("SELECT phone FROM wa_lid_map WHERE lid = %s", (d,))
    row = cursor.fetchone()
    if not row:
        return None
    return row["phone"] if isinstance(row, dict) else row[0]


def grupos_com_lid_descoberto(cursor, limite: int = 5) -> List[str]:
    """
    Grupos que ainda tem LID orfao FORA do mapa, do que mais rende pro que menos.

    E o alvo da colheita continua: participante novo entra em grupo todo mes e
    traz LID novo, entao colher uma vez nao basta — mas varrer os ~130 grupos a
    cada rodada e caro e quase todo inutil. Isto pergunta onde ainda falta.
    """
    cursor.execute(
        """
        SELECT g.group_jid, COUNT(*) AS n
          FROM group_messages g
          LEFT JOIN wa_lid_map m
                 ON m.lid = regexp_replace(g.sender_phone, '[^0-9]', '', 'g')
         WHERE g.contact_id IS NULL
           AND NOT COALESCE(g.from_me, FALSE)
           AND length(regexp_replace(g.sender_phone, '[^0-9]', '', 'g')) >= 14
           AND m.lid IS NULL
         GROUP BY g.group_jid
         ORDER BY COUNT(*) DESC
         LIMIT %s
        """,
        (limite,),
    )
    return [(r["group_jid"] if isinstance(r, dict) else r[0])
            for r in cursor.fetchall()]


async def colher_pendentes(cursor, limite_grupos: int = 3,
                           commit_cb=None, timeout_grupo: float = 10.0,
                           budget_s: float = 45.0) -> Dict[str, Any]:
    """
    Colhe o mapa dos grupos onde ainda falta cobertura. Para o cron.

    ⚠️ TIMEOUT E BUDGET CURTOS DE PROPOSITO. Isto roda no
    `cron_run_group_messages`, **depois** do `wait_for` que protege o sync — ou
    seja, FORA do budget dele. Com o default antigo (5 grupos × 30s) o pior
    caso somava 150s por cima dos 200s do sync, e a rodada so estouraria em
    producao sob instancia lenta. O corte aqui e o RELOGIO (`budget_s`), nao a
    contagem: um grupo que demora nao deve impedir o proximo de nem ser
    tentado na rodada seguinte, e e por isso que o cursor e a propria
    cobertura (quem ainda falta volta a ser alvo).

    Devolve contagem por efeito. Grupo que NAO responde e contado separado de
    grupo sem LID: somá-los faria a cobertura parecer completa quando a
    instancia esta apenas muda.
    """
    import time as _time

    jids = grupos_com_lid_descoberto(cursor, limite=limite_grupos)
    out = {"grupos_alvo": len(jids), "grupos_tentados": 0, "sem_resposta": 0,
           "novos": 0, "conflitos": 0, "iguais": 0, "parcial": False}
    t0 = _time.monotonic()
    for jid in jids:
        if _time.monotonic() - t0 > budget_s:
            # Declarado, nao silencioso: "parcial" e o que impede a rodada de
            # parecer ter coberto tudo que pediu.
            out["parcial"] = True
            break
        out["grupos_tentados"] += 1
        pares = await colher_grupo(jid, timeout=timeout_grupo)
        if not pares:
            out["sem_resposta"] += 1
            continue
        r = gravar_pares(cursor, pares, origem_jid=jid)
        for k in ("novos", "conflitos", "iguais"):
            out[k] += r[k]
        if commit_cb is not None:
            commit_cb()
    return out


def cobertura(cursor) -> Dict[str, Any]:
    """
    Quanto do LID em `group_messages` o mapa ja cobre.

    Mede o DENOMINADOR junto: "500 pares no mapa" nao diz nada sem quantos LIDs
    existem para cobrir ([[feedback_mecanismo_que_nao_mede_o_denominador]]).
    """
    cursor.execute("SELECT COUNT(*) AS pares FROM wa_lid_map")
    row = cursor.fetchone()
    pares = (row["pares"] if isinstance(row, dict) else row[0])

    cursor.execute(
        """
        WITH lids AS (
            SELECT DISTINCT regexp_replace(sender_phone, '[^0-9]', '', 'g') AS lid,
                   COUNT(*) OVER (PARTITION BY regexp_replace(sender_phone,'[^0-9]','','g')) AS n
              FROM group_messages
             WHERE contact_id IS NULL
               AND length(regexp_replace(sender_phone, '[^0-9]', '', 'g')) >= 14
        )
        SELECT COUNT(*) AS lids_distintos,
               COUNT(*) FILTER (WHERE m.lid IS NOT NULL) AS lids_cobertos,
               COALESCE(SUM(l.n), 0) AS msgs,
               COALESCE(SUM(l.n) FILTER (WHERE m.lid IS NOT NULL), 0) AS msgs_cobertas
          FROM lids l
          LEFT JOIN wa_lid_map m ON m.lid = l.lid
        """
    )
    r = cursor.fetchone()
    out = dict(r) if isinstance(r, dict) else {
        "lids_distintos": r[0], "lids_cobertos": r[1], "msgs": r[2], "msgs_cobertas": r[3]}
    out["pares_no_mapa"] = pares
    tot = int(out["lids_distintos"]) or 1
    out["pct_lids_cobertos"] = round(100.0 * int(out["lids_cobertos"]) / tot, 1)
    return out
