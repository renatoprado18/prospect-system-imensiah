"""
Avatar Fetcher Service - Busca fotos de perfil do WhatsApp

Usa Evolution API para buscar fotos de perfil e atualizar contatos.
"""
import asyncio
import logging
from typing import Dict, List, Optional
from datetime import datetime
from database import get_db

logger = logging.getLogger(__name__)


class AvatarFetcherService:
    """Busca e atualiza fotos de perfil dos contatos."""

    def __init__(self):
        self.stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0
        }

    def get_contacts_needing_photos(self, limit: int = 100) -> List[Dict]:
        """
        Retorna contatos que precisam de fotos reais.
        Prioriza contatos com telefone e sem foto real (WhatsApp/LinkedIn).
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Contatos com telefone mas sem foto real (só Google/iniciais)
            cursor.execute("""
                SELECT id, nome, telefones, foto_url
                FROM contacts
                WHERE telefones IS NOT NULL
                AND telefones::text != '[]'
                AND telefones::text != ''
                AND (
                    foto_url IS NULL
                    OR foto_url = ''
                    OR foto_url LIKE '%%googleusercontent%%'
                )
                ORDER BY
                    circulo ASC,  -- Prioriza circulos mais proximos
                    atualizado_em DESC
                LIMIT %s
            """, (limit,))

            contacts = []
            for row in cursor.fetchall():
                contact = dict(row)
                # Extrair primeiro telefone valido
                telefones = contact.get('telefones', [])
                if isinstance(telefones, str):
                    import json
                    try:
                        telefones = json.loads(telefones)
                    except:
                        telefones = []

                if telefones:
                    # Pegar o primeiro telefone
                    phone = telefones[0] if isinstance(telefones[0], str) else telefones[0].get('numero', '')
                    contact['phone'] = phone
                    contacts.append(contact)

            return contacts

    async def fetch_whatsapp_photo(self, phone: str) -> Optional[str]:
        """Busca foto de perfil do WhatsApp via Evolution API."""
        from integrations.evolution_api import get_evolution_api

        try:
            evolution = get_evolution_api()
            result = await evolution.get_profile_picture(phone)

            if result and not result.get('error'):
                # A resposta pode ter diferentes formatos
                if isinstance(result, dict):
                    return result.get('profilePictureUrl') or result.get('picture') or result.get('url')
                elif isinstance(result, str):
                    return result

            return None

        except Exception as e:
            logger.warning(f"Error fetching WhatsApp photo for {phone}: {e}")
            return None

    def update_contact_photo(self, contact_id: int, photo_url: str) -> bool:
        """Atualiza foto do contato no banco."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE contacts
                SET foto_url = %s, atualizado_em = NOW()
                WHERE id = %s
            """, (photo_url, contact_id))
            conn.commit()
            return cursor.rowcount > 0

    async def fetch_photos_batch(
        self,
        limit: int = 50,
        delay_between: float = 1.0,
        progress_callback = None
    ) -> Dict:
        """
        Busca fotos em lote para contatos sem foto real.

        Args:
            limit: Numero maximo de contatos a processar
            delay_between: Delay em segundos entre requests (evita rate limit)
            progress_callback: Funcao chamada a cada contato processado

        Returns:
            Estatisticas do processamento
        """
        self.stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}

        contacts = self.get_contacts_needing_photos(limit)
        self.stats['total'] = len(contacts)

        logger.info(f"Starting avatar fetch for {len(contacts)} contacts")

        for i, contact in enumerate(contacts):
            phone = contact.get('phone', '')

            if not phone:
                self.stats['skipped'] += 1
                continue

            # Buscar foto
            photo_url = await self.fetch_whatsapp_photo(phone)

            if photo_url:
                # Atualizar contato
                success = self.update_contact_photo(contact['id'], photo_url)
                if success:
                    self.stats['success'] += 1
                    logger.info(f"Updated photo for {contact['nome']}")
                else:
                    self.stats['failed'] += 1
            else:
                self.stats['failed'] += 1

            # Callback de progresso
            if progress_callback:
                progress_callback({
                    'current': i + 1,
                    'total': len(contacts),
                    'contact': contact['nome'],
                    'success': photo_url is not None,
                    'stats': self.stats
                })

            # Delay para evitar rate limit
            if delay_between > 0 and i < len(contacts) - 1:
                await asyncio.sleep(delay_between)

        logger.info(f"Avatar fetch complete: {self.stats}")
        return self.stats

    def get_photo_stats(self) -> Dict:
        """Retorna estatisticas de fotos dos contatos."""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(CASE WHEN foto_url IS NULL OR foto_url = '' THEN 1 END) as sem_foto,
                    COUNT(CASE WHEN foto_url LIKE '%%googleusercontent%%' THEN 1 END) as google_iniciais,
                    COUNT(CASE WHEN foto_url LIKE '%%linkedin%%' THEN 1 END) as linkedin,
                    COUNT(CASE WHEN foto_url LIKE '%%whatsapp%%' OR foto_url LIKE '%%pps.whatsapp%%' THEN 1 END) as whatsapp,
                    COUNT(CASE WHEN foto_url IS NOT NULL
                        AND foto_url NOT LIKE '%%googleusercontent%%'
                        AND foto_url NOT LIKE '%%linkedin%%'
                        AND foto_url NOT LIKE '%%whatsapp%%'
                        AND foto_url != '' THEN 1 END) as outras
                FROM contacts
            """)

            row = cursor.fetchone()
            stats = dict(row)

            # Contatos com telefone que podem ter foto buscada
            cursor.execute("""
                SELECT COUNT(*) as c FROM contacts
                WHERE telefones IS NOT NULL
                AND telefones::text != '[]'
                AND (foto_url IS NULL OR foto_url LIKE '%%googleusercontent%%')
            """)
            stats['potencial_whatsapp'] = cursor.fetchone()['c']

            # Contatos com LinkedIn que podem ter foto buscada
            cursor.execute("""
                SELECT COUNT(*) as c FROM contacts
                WHERE linkedin IS NOT NULL
                AND (foto_url IS NULL OR foto_url LIKE '%%googleusercontent%%')
            """)
            stats['potencial_linkedin'] = cursor.fetchone()['c']

            return stats


# Singleton
_avatar_fetcher = None


def get_avatar_fetcher() -> AvatarFetcherService:
    """Get singleton instance."""
    global _avatar_fetcher
    if _avatar_fetcher is None:
        _avatar_fetcher = AvatarFetcherService()
    return _avatar_fetcher
