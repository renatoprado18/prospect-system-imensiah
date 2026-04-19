"""
Social Groups Service - Mapeia grupos de WhatsApp como circulos sociais

Extrai participantes dos grupos, cruza com contatos INTEL,
identifica interesses mutuos e oportunidades de conexao.
"""
import os
import json
import logging
from typing import Dict, List, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)


async def get_group_info(group_jid: str) -> Optional[Dict]:
    """Busca info do grupo via Evolution API (nome, participantes)."""
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


async def get_group_with_members(group_jid: str) -> Optional[Dict]:
    """
    Busca grupo com participantes cruzados com contatos INTEL.
    Retorna: nome, total_participantes, membros_conhecidos (com dados do INTEL).
    """
    info = await get_group_info(group_jid)
    if not info:
        return None

    participants = info.get('participants', [])

    # Extrair telefones dos participantes
    phones = []
    for p in participants:
        phone_jid = p.get('phoneNumber', p.get('id', ''))
        phone = phone_jid.replace('@s.whatsapp.net', '').replace('@lid', '')
        if phone and len(phone) > 8:
            phones.append({
                'phone': phone,
                'admin': p.get('admin') == 'admin' or p.get('admin') == 'superadmin',
                'raw_jid': phone_jid
            })

    # Cruzar com contatos INTEL por telefone
    known_members = []
    with get_db() as conn:
        cursor = conn.cursor()
        for p in phones:
            phone = p['phone']
            # Buscar contato por telefone (parcial, ultimos 8 digitos)
            last_digits = phone[-8:] if len(phone) >= 8 else phone
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, health_score, foto_url, contexto
                FROM contacts
                WHERE telefones::text LIKE %s
                LIMIT 1
            """, (f'%{last_digits}%',))
            contact = cursor.fetchone()
            if contact:
                contact = dict(contact)
                contact['is_admin'] = p['admin']
                contact['phone'] = phone
                known_members.append(contact)

    # Health medio dos conhecidos
    health_scores = [m['health_score'] for m in known_members if m.get('health_score') is not None]
    health_medio = round(sum(health_scores) / len(health_scores)) if health_scores else None

    # Quem precisa atencao
    needs_attention = [m for m in known_members if (m.get('health_score') or 100) < 50]

    return {
        'group_jid': group_jid,
        'name': info.get('subject', group_jid),
        'description': info.get('desc', ''),
        'total_participants': info.get('size', len(participants)),
        'known_members': len(known_members),
        'unknown_members': len(participants) - len(known_members),
        'health_medio': health_medio,
        'needs_attention': len(needs_attention),
        'members': sorted(known_members, key=lambda m: m.get('nome', '')),
        'attention_list': sorted(needs_attention, key=lambda m: m.get('health_score', 0))
    }


async def list_all_social_groups() -> List[Dict]:
    """Lista todos os grupos de WhatsApp com nome."""
    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Buscar chats
            resp = await client.post(
                f'{base_url}/chat/findChats/{instance}',
                headers={'apikey': api_key, 'Content-Type': 'application/json'},
                json={}
            )
            chats = resp.json()

            groups = []
            named_jids = set()
            for c in chats:
                jid = c.get('remoteJid', '')
                name = c.get('pushName', '')
                if '@g.us' in jid and name:
                    groups.append({'jid': jid, 'name': name})
                    named_jids.add(jid)
                elif '@g.us' in jid:
                    # Sem nome, tentar via metadata
                    pass  # Muito lento, pular

            return sorted(groups, key=lambda g: g['name'])

    except Exception as e:
        logger.error(f"Erro ao listar grupos: {e}")
        return []


async def find_common_groups(contact_id_a: int, contact_id_b: int) -> List[Dict]:
    """Encontra grupos em comum entre dois contatos. (Futuro)"""
    # TODO: implementar quando tivermos cache de membros por grupo
    return []
