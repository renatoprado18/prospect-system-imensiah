"""
Social Groups Service - Mapeia grupos de WhatsApp como circulos sociais

Usa cache no banco para evitar chamadas lentas à Evolution API.
Sync em background (cron) atualiza o cache diariamente.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def get_group_info(group_jid: str) -> Optional[Dict]:
    """Busca info do grupo via Evolution API."""
    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f'{base_url}/group/findGroupInfos/{instance}?groupJid={group_jid}',
                headers={'apikey': api_key}
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning(f"Erro ao buscar info do grupo {group_jid}: {e}")
    return None


def _cross_phone_with_contacts(phones: List[str]) -> List[Dict]:
    """Cruza lista de telefones com contatos INTEL."""
    if not phones:
        return []

    known = []
    with get_db() as conn:
        cursor = conn.cursor()
        for phone in phones:
            last_digits = phone[-8:] if len(phone) >= 8 else phone
            if len(last_digits) < 6:
                continue
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, health_score, foto_url
                FROM contacts WHERE telefones::text LIKE %s LIMIT 1
            """, (f'%{last_digits}%',))
            contact = cursor.fetchone()
            if contact:
                known.append(dict(contact))
    return known


async def sync_all_groups_cache() -> Dict:
    """Sincroniza cache de todos os grupos. Roda no cron diario."""
    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return {"error": "Evolution API nao configurada"}

    results = {"synced": 0, "errors": 0}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # 1. Buscar todos os chats
            resp = await client.post(
                f'{base_url}/chat/findChats/{instance}',
                headers={'apikey': api_key, 'Content-Type': 'application/json'},
                json={}
            )
            chats = resp.json()
            group_jids = []
            named = {}

            for c in chats:
                jid = c.get('remoteJid', '')
                name = c.get('pushName', '')
                if '@g.us' in jid:
                    group_jids.append(jid)
                    if name:
                        named[jid] = name

            # 2. Buscar metadados de cada grupo (nome, participantes)
            import asyncio as aio

            async def fetch_group(jid):
                try:
                    r = await client.get(
                        f'{base_url}/group/findGroupInfos/{instance}?groupJid={jid}',
                        headers={'apikey': api_key},
                        timeout=10.0
                    )
                    if r.status_code == 200:
                        info = r.json()
                        name = info.get('subject', named.get(jid, jid))
                        participants = info.get('participants', [])
                        phones = []
                        for p in participants:
                            phone_jid = p.get('phoneNumber', p.get('id', ''))
                            phone = phone_jid.replace('@s.whatsapp.net', '').replace('@lid', '')
                            if phone and len(phone) > 8:
                                phones.append(phone)
                        return {
                            'jid': jid,
                            'name': name,
                            'total': info.get('size', len(participants)),
                            'phones': phones
                        }
                except Exception:
                    pass
                # Fallback: usar nome do chat se disponivel
                if jid in named:
                    return {'jid': jid, 'name': named[jid], 'total': 0, 'phones': []}
                return None

            # Processar em batches de 10
            all_groups = []
            for i in range(0, len(group_jids), 10):
                batch = group_jids[i:i+10]
                batch_results = await aio.gather(*[fetch_group(jid) for jid in batch])
                all_groups.extend([r for r in batch_results if r])

            # 3. Cruzar telefones com contatos e salvar cache
            with get_db() as conn:
                cursor = conn.cursor()

                for g in all_groups:
                    try:
                        known = _cross_phone_with_contacts(g['phones'])
                        known_ids = [c['id'] for c in known]
                        healths = [c['health_score'] for c in known if c.get('health_score') is not None]
                        health_medio = round(sum(healths) / len(healths)) if healths else None

                        cursor.execute("""
                            INSERT INTO social_groups_cache (group_jid, group_name, total_participants,
                                participants_phones, known_contact_ids, known_count, health_medio, last_synced_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                            ON CONFLICT (group_jid) DO UPDATE SET
                                group_name = EXCLUDED.group_name,
                                total_participants = EXCLUDED.total_participants,
                                participants_phones = EXCLUDED.participants_phones,
                                known_contact_ids = EXCLUDED.known_contact_ids,
                                known_count = EXCLUDED.known_count,
                                health_medio = EXCLUDED.health_medio,
                                last_synced_at = NOW()
                        """, (
                            g['jid'], g['name'], g['total'],
                            json.dumps(g['phones']), json.dumps(known_ids),
                            len(known), health_medio
                        ))
                        results["synced"] += 1
                    except Exception as e:
                        results["errors"] += 1
                        logger.warning(f"Erro ao cachear grupo {g['name']}: {e}")

                conn.commit()

    except Exception as e:
        logger.error(f"Erro no sync de grupos: {e}")
        results["error"] = str(e)

    return results


def list_cached_groups() -> List[Dict]:
    """Lista grupos do cache (rapido, sem chamada à Evolution API)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT group_jid, group_name, total_participants, known_count, health_medio, last_synced_at
            FROM social_groups_cache
            WHERE group_name IS NOT NULL
            ORDER BY group_name
        """)
        return [dict(r) for r in cursor.fetchall()]


def get_cached_group_detail(group_jid: str) -> Optional[Dict]:
    """Detalhe de um grupo do cache com contatos conhecidos."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM social_groups_cache WHERE group_jid = %s", (group_jid,))
        group = cursor.fetchone()
        if not group:
            return None
        group = dict(group)

        # Buscar dados dos contatos conhecidos
        known_ids = group.get('known_contact_ids', [])
        if isinstance(known_ids, str):
            known_ids = json.loads(known_ids)

        members = []
        if known_ids:
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, health_score, foto_url, telefones, emails
                FROM contacts WHERE id = ANY(%s)
                ORDER BY nome
            """, (known_ids,))
            for row in cursor.fetchall():
                c = dict(row)
                telefones = c.get('telefones', [])
                if isinstance(telefones, str):
                    telefones = json.loads(telefones) if telefones else []
                phone = ''
                for t in telefones:
                    if t.get('number'):
                        phone = t['number']
                        break
                c['phone'] = phone
                members.append(c)

        needs_attention = [m for m in members if (m.get('health_score') or 100) < 50]

        return {
            'group_jid': group['group_jid'],
            'name': group['group_name'],
            'total_participants': group['total_participants'],
            'known_members': len(members),
            'unknown_members': group['total_participants'] - len(members),
            'health_medio': group.get('health_medio'),
            'needs_attention': len(needs_attention),
            'members': sorted(members, key=lambda m: m.get('nome', '')),
            'attention_list': sorted(needs_attention, key=lambda m: m.get('health_score', 0)),
            'last_synced': group.get('last_synced_at')
        }


async def list_all_social_groups() -> List[Dict]:
    """Lista grupos — do cache se disponivel, senao da API."""
    cached = list_cached_groups()
    if cached:
        return [{'jid': g['group_jid'], 'name': g['group_name'],
                 'known': g['known_count'], 'total': g['total_participants'],
                 'health': g.get('health_medio')} for g in cached]

    # Fallback: buscar da API (lento, primeira vez)
    return await _fetch_groups_from_api()


async def _fetch_groups_from_api() -> List[Dict]:
    """Busca grupos da Evolution API (lento, usado apenas se cache vazio)."""
    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f'{base_url}/chat/findChats/{instance}',
                headers={'apikey': api_key, 'Content-Type': 'application/json'},
                json={}
            )
            chats = resp.json()
            return sorted(
                [{'jid': c.get('remoteJid', ''), 'name': c.get('pushName', '')}
                 for c in chats if '@g.us' in c.get('remoteJid', '') and c.get('pushName')],
                key=lambda g: g['name']
            )
    except Exception as e:
        logger.error(f"Erro ao listar grupos: {e}")
        return []


async def get_group_with_members(group_jid: str) -> Optional[Dict]:
    """Detalhe do grupo — do cache se disponivel."""
    cached = get_cached_group_detail(group_jid)
    if cached:
        return cached

    # Fallback: buscar ao vivo (lento)
    info = await get_group_info(group_jid)
    if not info:
        return None

    participants = info.get('participants', [])
    phones = []
    for p in participants:
        phone_jid = p.get('phoneNumber', p.get('id', ''))
        phone = phone_jid.replace('@s.whatsapp.net', '').replace('@lid', '')
        if phone and len(phone) > 8:
            phones.append(phone)

    known = _cross_phone_with_contacts(phones)
    healths = [m['health_score'] for m in known if m.get('health_score') is not None]

    return {
        'group_jid': group_jid,
        'name': info.get('subject', group_jid),
        'total_participants': info.get('size', len(participants)),
        'known_members': len(known),
        'unknown_members': len(participants) - len(known),
        'health_medio': round(sum(healths) / len(healths)) if healths else None,
        'needs_attention': len([m for m in known if (m.get('health_score') or 100) < 50]),
        'members': sorted(known, key=lambda m: m.get('nome', '')),
        'attention_list': sorted([m for m in known if (m.get('health_score') or 100) < 50],
                                  key=lambda m: m.get('health_score', 0)),
    }
