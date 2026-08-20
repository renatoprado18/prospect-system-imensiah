"""
Group Message Sync - Sincroniza mensagens de grupos WhatsApp marcados para sync.

Busca mensagens novas dos grupos com sync_enabled=TRUE e salva na tabela group_messages.
Cruza remetentes com contatos INTEL pelo telefone.
"""
import asyncio
import os
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List
import httpx

from database import get_db
from services.contact_identity import find_contact_by_phone
from services.tz import UTC

logger = logging.getLogger(__name__)


async def sync_group_messages(
    limit_per_group: int = 50,
    limit: int = None,
    offset: int = 0,
    budget_s: float = None,
    timeout_por_grupo: float = 45.0,
) -> Dict:
    """
    Sincroniza mensagens dos grupos marcados para sync.

    Chamado pelo cron isolado /api/cron/run-group-messages (paginado) e pelo
    group_digest (varredura completa, sem limit).

    PAGINACAO (18/08/26): sao 49 grupos com sync_enabled, cada um uma chamada
    HTTP sequencial a Evolution — a varredura completa passou de 90s em 06/08 e
    o step dentro do daily-sync passou a estourar TODO DIA. Com `limit`/`offset`
    o cron processa um pedaco por rodada e retoma pelo cursor, mesmo desenho de
    run-social-groups e run-whatsapp-sync. Sem `limit`, varre tudo (default
    preservado pros chamadores antigos).

    ORCAMENTO DE TEMPO (20/08/26) — o conserto de 18/08 trocou o defeito de lugar
    em vez de tirar. Com corte so por `limit`, uma rodada de 12 grupos oscilava
    entre 2,3s e >240s (18% das rodadas estouraram em 2 dias), e como o cursor
    NAO avanca no timeout, um lote caro **trava a fila ali**: os grupos seguintes
    nunca chegam a vez, e o job volta a ser sempre-vermelho — que e exatamente o
    problema que esta frente veio consertar.

    Agora quem corta e o RELOGIO: processa grupos ate `budget_s` e devolve
    `next_offset` pelo que REALMENTE processou. A rodada termina sozinha antes do
    limite, o cursor sempre anda e a categoria "timeout" deixa de existir.
    `timeout_por_grupo` cobre o caso restante — um unico grupo patologico que
    sozinho consumiria a janela conta como erro DELE, nao da rodada.
    """
    from services.social_groups import get_sync_enabled_groups

    all_groups = get_sync_enabled_groups()
    if not all_groups:
        return {"skipped": "no groups with sync enabled",
                "total_groups": 0, "next_offset": 0, "more_pages": False}

    total_groups = len(all_groups)
    if limit:
        # offset alem do fim = volta pro comeco (a fila e circular, nao acaba)
        if offset >= total_groups:
            offset = 0
        groups = all_groups[offset:offset + limit]
        next_offset = offset + len(groups)
        more_pages = next_offset < total_groups
        if not more_pages:
            next_offset = 0
    else:
        groups = all_groups
        next_offset, more_pages = 0, False

    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return {"error": "Evolution API not configured"}

    results = {
        "groups_synced": 0, "messages_saved": 0, "errors": 0,
        "total_groups": total_groups, "offset_used": offset,
        "next_offset": next_offset, "more_pages": more_pages,
        "parcial": False,
    }

    inicio = time.monotonic()
    processados = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        for group in groups:
            # Antes de PEGAR o proximo, nao depois: parar no meio de um grupo
            # deixaria o cursor mentindo sobre o que foi coberto.
            if budget_s and (time.monotonic() - inicio) >= budget_s:
                results["parcial"] = True
                break

            jid = group['group_jid']
            name = group['group_name']

            try:
                saved = await asyncio.wait_for(
                    _sync_single_group(
                        client, base_url, api_key, instance,
                        jid, name, limit_per_group
                    ),
                    timeout=timeout_por_grupo,
                )
                results["messages_saved"] += saved
                results["groups_synced"] += 1
            except asyncio.TimeoutError:
                # Grupo que sozinho estoura a janela. Conta como erro dele e a
                # fila segue — antes, levava a rodada inteira junto.
                logger.error(f"group sync timeout > {timeout_por_grupo}s: {name}")
                results["errors"] += 1
            except Exception as e:
                logger.error(f"Error syncing group {name}: {e}")
                results["errors"] += 1
            processados += 1

    # O cursor anda pelo que foi coberto de fato. Numa rodada parcial o resto do
    # lote volta na proxima, sem pular ninguem e sem repetir o que ja passou.
    if limit:
        alcancou_fim = offset + processados >= total_groups
        results["next_offset"] = 0 if alcancou_fim else offset + processados
        results["more_pages"] = not alcancou_fim

    return results


async def sync_one_group_messages(group_jid: str, group_name: str = '', limit: int = 50) -> Dict:
    """Backfill de UM grupo especifico (auto-wire do vinculo projeto<->grupo).

    Reusa a primitiva `_sync_single_group` sem varrer todos os grupos sync_enabled.
    Idempotente por message_id (o INSERT em group_messages faz ON CONFLICT DO NOTHING).
    """
    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return {"error": "Evolution API not configured", "messages_saved": 0}

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            saved = await _sync_single_group(
                client, base_url, api_key, instance,
                group_jid, group_name or group_jid, limit
            )
            return {"group_jid": group_jid, "messages_saved": saved}
        except Exception as e:
            logger.error(f"Error syncing group {group_name or group_jid}: {e}")
            return {"group_jid": group_jid, "error": str(e), "messages_saved": 0}


async def _sync_single_group(
    client: httpx.AsyncClient,
    base_url: str, api_key: str, instance: str,
    group_jid: str, group_name: str,
    limit: int
) -> int:
    """Sincroniza mensagens de um grupo especifico. Retorna quantidade salva.

    PAGINA ATE ALCANCAR O QUE JA TEM — nao pega um teto fixo e para.

    O BUG QUE ISTO CORRIGE (medido em 05/08/2026). A versao anterior pedia as
    `limit` (50) mensagens mais recentes, UMA VEZ, e o chamador e o `daily-sync`
    — 1x/dia. Grupo que passasse de 50 mensagens naquele dia perdia o excedente
    **para sempre**: a rodada seguinte tambem so olhava as 50 ultimas, e nao ha
    catch-up. Medicao no CHARUTO CAP: estourou as 50 em **52 de 66 dias (79%)**,
    media 73/dia, pico 154. O INTEL tinha **3.741 de 19.508** mensagens do grupo.

    Somando os 8 grupos rastreados: **20.646 no INTEL contra 56.037 na
    Evolution — 37%**. E a perda era invisivel porque o sync pedia 50, recebia
    50, gravava e reportava sucesso: ninguem perguntava "havia mais?". Mesma
    classe do webhook fechado por engano e do check G — o mecanismo nao media o
    proprio denominador, entao o painel ficava verde.

    Assimetria que escondeu o problema: grupo calmo (Familia Dansieri, IMEX)
    NUNCA estoura 50/dia. So perde quem tem conversa de verdade — exatamente
    onde o contexto importa.

    Como para: quando a pagina inteira ja e conhecida (todos os `message_id` ja
    no banco), alcancou o passado e nao ha o que buscar. `MAX_PAGINAS` e um
    freio de seguranca contra grupo gigante numa 1a sync, nao a regra.
    """
    MAX_PAGINAS = 40          # 4.000 msgs/grupo/rodada — teto de emergencia
    POR_PAGINA = max(50, limit)

    with get_db() as _c:
        _cur = _c.cursor()
        _cur.execute("SELECT message_id FROM group_messages WHERE group_jid = %s", (group_jid,))
        conhecidos = {r['message_id'] for r in _cur.fetchall()}

    raw_msgs, total_evolution, pagina = [], None, 1
    while pagina <= MAX_PAGINAS:
        resp = await client.post(
            f"{base_url}/chat/findMessages/{instance}",
            headers={'apikey': api_key, 'Content-Type': 'application/json'},
            json={"where": {"key": {"remoteJid": group_jid}},
                  "page": pagina, "offset": POR_PAGINA}
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to fetch messages for {group_name} "
                           f"(page {pagina}): {resp.status_code}")
            break

        container = resp.json().get('messages', {})
        if isinstance(container, dict):
            lote = container.get('records', [])
            if total_evolution is None:
                total_evolution = container.get('total')
        elif isinstance(container, list):
            lote = container
        else:
            lote = []

        if not lote:
            break
        raw_msgs.extend(lote)

        # Alcancou o passado: pagina inteira ja conhecida.
        ids = {(m.get('key') or {}).get('id') for m in lote}
        if ids and ids.issubset(conhecidos):
            break
        pagina += 1

    if not raw_msgs:
        return 0

    # GUARDA COMPARATIVA. Sem isto a proxima regressao volta a ser silenciosa:
    # e so comparando com a fonte que se sabe que faltou algo (mesmo principio
    # do heartbeat de ingestao do WhatsApp, 04/08).
    if total_evolution:
        gap = total_evolution - len(conhecidos)
        if gap > POR_PAGINA * MAX_PAGINAS:
            logger.warning(
                f"[group_sync] {group_name}: Evolution tem {total_evolution}, "
                f"INTEL {len(conhecidos)} — faltam {gap}, mais que o teto de uma "
                f"rodada. Rode o backfill dedicado."
            )

    # Parse and save messages
    saved = 0
    with get_db() as conn:
        cursor = conn.cursor()

        for m in raw_msgs:
            msg_data = m.get('message') or {}
            key = m.get('key') or {}
            msg_type = m.get('messageType', '')
            msg_id = key.get('id', '')

            # Skip reactions, stickers, protocol messages
            if msg_type in ('reactionMessage', 'stickerMessage', 'protocolMessage',
                            'senderKeyDistributionMessage', 'messageContextInfo'):
                continue

            # Extract content
            content = ''
            if 'conversation' in msg_data:
                content = msg_data['conversation']
            elif 'extendedTextMessage' in msg_data:
                content = msg_data['extendedTextMessage'].get('text', '')
            elif 'documentMessage' in msg_data:
                doc_name = msg_data['documentMessage'].get('fileName', 'documento')
                content = msg_data['documentMessage'].get('caption', f'[Documento: {doc_name}]')
                msg_type = 'document'
            elif 'imageMessage' in msg_data:
                content = msg_data['imageMessage'].get('caption', '[Imagem]')
                msg_type = 'image'
            elif 'videoMessage' in msg_data:
                content = msg_data['videoMessage'].get('caption', '[Video]')
                msg_type = 'video'
            elif 'audioMessage' in msg_data:
                content = '[Audio]'
                msg_type = 'audio'

            if not content or len(content) < 2:
                continue

            # Sender info
            sender_phone = (key.get('participantAlt') or key.get('participant') or '') \
                .replace('@s.whatsapp.net', '').replace('@lid', '')
            sender_name = m.get('pushName', '')
            from_me = key.get('fromMe', False)
            if from_me:
                sender_name = 'Renato'

            # Timestamp: messageTimestamp e epoch (UTC). Armazena naive-UTC
            # (convencao do DB — coluna TIMESTAMP sem tz), determinístico
            # independente do TZ do server. ESPELHA persist_group_message_realtime
            # (webhook): sem o tz=UTC, fromtimestamp() usava o TZ LOCAL do server —
            # OK no Railway (UTC) mas 3h defasado em backfill BRT -> dupes/offset.
            timestamp = m.get('messageTimestamp', 0)
            if isinstance(timestamp, (int, float)) and timestamp > 0:
                try:
                    timestamp = datetime.fromtimestamp(int(timestamp), tz=UTC).replace(tzinfo=None)
                except Exception:
                    continue
            else:
                continue

            # Cross sender_phone with contacts
            contact_id = _find_contact_by_phone(cursor, sender_phone) if sender_phone and len(sender_phone) > 8 else None

            # Insert with dedup by message_id
            try:
                cursor.execute("""
                    INSERT INTO group_messages (group_jid, message_id, sender_phone, sender_name,
                        contact_id, content, message_type, timestamp, from_me)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (message_id) DO NOTHING
                """, (
                    group_jid, msg_id, sender_phone, sender_name,
                    contact_id, content, msg_type, timestamp, from_me
                ))
                if cursor.rowcount > 0:
                    saved += 1
            except Exception as e:
                logger.debug(f"Skip message {msg_id}: {e}")

        # Update last sync timestamp
        cursor.execute("""
            UPDATE social_groups_cache SET last_message_sync = NOW()
            WHERE group_jid = %s
        """, (group_jid,))

        conn.commit()

    logger.info(f"Group {group_name}: {saved} new messages saved")
    return saved


def _find_contact_by_phone(cursor, phone: str):
    """
    Find contact_id by phone number.

    Compara so digitos dos dois lados (`find_contact_by_phone`): o remetente
    chega cru do WhatsApp e na base o numero pode estar formatado pelo Google.
    O LIKE anterior sobre `telefones::text` nao via nenhum numero formatado, o
    que e boa parte do `contact_id` NULL em `group_messages`.
    """
    row = find_contact_by_phone(cursor, phone, columns="id")
    return row["id"] if row else None


def get_group_messages(group_jid: str, limit: int = 50, since: datetime = None) -> List[Dict]:
    """Retorna mensagens de um grupo do cache local."""
    with get_db() as conn:
        cursor = conn.cursor()
        if since:
            cursor.execute("""
                SELECT gm.*, c.nome as contact_nome
                FROM group_messages gm
                LEFT JOIN contacts c ON c.id = gm.contact_id
                WHERE gm.group_jid = %s AND gm.timestamp >= %s
                ORDER BY gm.timestamp DESC LIMIT %s
            """, (group_jid, since, limit))
        else:
            cursor.execute("""
                SELECT gm.*, c.nome as contact_nome
                FROM group_messages gm
                LEFT JOIN contacts c ON c.id = gm.contact_id
                WHERE gm.group_jid = %s
                ORDER BY gm.timestamp DESC LIMIT %s
            """, (group_jid, limit))

        rows = []
        for r in cursor.fetchall():
            row = dict(r)
            for key in ('timestamp', 'criado_em'):
                if row.get(key) and hasattr(row[key], 'isoformat'):
                    row[key] = row[key].isoformat()
            rows.append(row)
        return rows
