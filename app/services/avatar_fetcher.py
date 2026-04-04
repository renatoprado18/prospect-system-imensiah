"""
Avatar Fetcher Service - Busca fotos de perfil do WhatsApp e Google

Usa Evolution API para buscar fotos de perfil do WhatsApp
e Google People API para re-sincronizar fotos de contatos Google.
"""
import asyncio
import logging
import httpx
import json
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
            # Exclui contatos já verificados (avatar_checked_at preenchido)
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
                AND avatar_checked_at IS NULL
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
                    # Pegar o primeiro telefone (campo pode ser 'number' ou 'numero')
                    if isinstance(telefones[0], str):
                        phone = telefones[0]
                    else:
                        phone = telefones[0].get('number') or telefones[0].get('numero', '')
                    if phone:
                        contact['phone'] = phone
                        contacts.append(contact)

            return contacts

    async def fetch_whatsapp_photo(self, phone: str) -> Optional[str]:
        """Busca foto de perfil do WhatsApp via Evolution API."""
        from integrations.evolution_api import get_evolution_client

        try:
            evolution = get_evolution_client()
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

    def mark_avatar_checked(self, contact_id: int) -> bool:
        """Marca que já tentamos buscar avatar deste contato (mesmo sem sucesso)."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE contacts
                SET avatar_checked_at = NOW()
                WHERE id = %s
            """, (contact_id,))
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

    def get_contacts_needing_google_photos(self, limit: int = 100) -> List[Dict]:
        """
        Retorna contatos com google_contact_id que precisam de fotos reais.
        Estes contatos podem ter fotos melhores no Google que nao foram importadas.
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, nome, google_contact_id, foto_url
                FROM contacts
                WHERE google_contact_id IS NOT NULL
                AND (
                    foto_url IS NULL
                    OR foto_url = ''
                    OR foto_url LIKE '%%googleusercontent%%'
                )
                ORDER BY circulo ASC, atualizado_em DESC
                LIMIT %s
            """, (limit,))

            return [dict(row) for row in cursor.fetchall()]

    async def fetch_google_photo(self, google_contact_id: str) -> Optional[str]:
        """
        Busca foto de perfil diretamente do Google People API.
        Retorna URL da foto se for uma foto real (nao iniciais).
        """
        from integrations.google_contacts import get_valid_token

        try:
            # Pegar token de uma conta Google conectada
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT email FROM google_accounts LIMIT 1")
                row = cursor.fetchone()
                if not row:
                    logger.warning("No Google account connected")
                    return None
                account_email = row['email']

            access_token = await get_valid_token(account_email)
            if not access_token:
                logger.warning("Could not get valid Google token")
                return None

            # Buscar contato com foto
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://people.googleapis.com/v1/people/{google_contact_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"personFields": "photos"}
                )

                if response.status_code != 200:
                    logger.warning(f"Google API error for {google_contact_id}: {response.status_code}")
                    return None

                data = response.json()
                photos = data.get("photos", [])

                for photo in photos:
                    url = photo.get("url", "")
                    # Verificar se e uma foto real (nao padrao/iniciais)
                    # Fotos default tem metadata.default = true
                    if photo.get("metadata", {}).get("default"):
                        continue
                    # URLs de iniciais geralmente tem "/s100/" pattern e sao muito genericas
                    if url and "googleusercontent" in url:
                        # Fotos reais tem patterns diferentes
                        # Iniciais: lh3.googleusercontent.com/contacts/s100/...
                        # Fotos reais: lh3.googleusercontent.com/a/...
                        if "/a/" in url or "=s" in url:
                            return url
                    elif url:
                        return url

                return None

        except Exception as e:
            logger.warning(f"Error fetching Google photo for {google_contact_id}: {e}")
            return None

    async def fetch_google_photos_batch(
        self,
        limit: int = 50,
        delay_between: float = 0.5,
        progress_callback = None
    ) -> Dict:
        """
        Busca fotos do Google em lote para contatos com google_contact_id.
        """
        stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}

        contacts = self.get_contacts_needing_google_photos(limit)
        stats['total'] = len(contacts)

        logger.info(f"Starting Google photo fetch for {len(contacts)} contacts")

        for i, contact in enumerate(contacts):
            google_id = contact.get('google_contact_id', '')

            if not google_id:
                stats['skipped'] += 1
                continue

            photo_url = await self.fetch_google_photo(google_id)

            if photo_url:
                success = self.update_contact_photo(contact['id'], photo_url)
                if success:
                    stats['success'] += 1
                    logger.info(f"Updated Google photo for {contact['nome']}")
                else:
                    stats['failed'] += 1
            else:
                stats['failed'] += 1

            if progress_callback:
                progress_callback({
                    'current': i + 1,
                    'total': len(contacts),
                    'contact': contact['nome'],
                    'success': photo_url is not None,
                    'stats': stats
                })

            if delay_between > 0 and i < len(contacts) - 1:
                await asyncio.sleep(delay_between)

        logger.info(f"Google photo fetch complete: {stats}")
        return stats

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

            # Contatos com telefone que podem ter foto buscada via WhatsApp
            # Exclui os já verificados
            cursor.execute("""
                SELECT COUNT(*) as c FROM contacts
                WHERE telefones IS NOT NULL
                AND telefones::text != '[]'
                AND (foto_url IS NULL OR foto_url LIKE '%%googleusercontent%%')
                AND avatar_checked_at IS NULL
            """)
            stats['potencial_whatsapp'] = cursor.fetchone()['c']

            # Contatos já verificados (tentamos buscar mas não tinha foto)
            cursor.execute("""
                SELECT COUNT(*) as c FROM contacts
                WHERE avatar_checked_at IS NOT NULL
                AND (foto_url IS NULL OR foto_url LIKE '%%googleusercontent%%')
            """)
            stats['ja_verificados_sem_foto'] = cursor.fetchone()['c']

            # Contatos com google_contact_id que podem ter foto real
            cursor.execute("""
                SELECT COUNT(*) as c FROM contacts
                WHERE google_contact_id IS NOT NULL
                AND (foto_url IS NULL OR foto_url LIKE '%%googleusercontent%%')
            """)
            stats['potencial_google'] = cursor.fetchone()['c']

            # Contatos com LinkedIn que podem ter foto buscada (Proxycurl)
            cursor.execute("""
                SELECT COUNT(*) as c FROM contacts
                WHERE linkedin IS NOT NULL
                AND (foto_url IS NULL OR foto_url LIKE '%%googleusercontent%%')
            """)
            stats['potencial_linkedin'] = cursor.fetchone()['c']

            return stats

    async def fetch_all_whatsapp_photos_with_job(self, job_id: int, delay_between: float = 1.0):
        """
        Busca fotos de TODOS os contatos com telefone.
        Atualiza o status do job na tabela background_jobs.
        """
        # Buscar todos os contatos que precisam de foto (sem limite)
        contacts = self.get_contacts_needing_photos(limit=10000)
        total = len(contacts)

        logger.info(f"[Job {job_id}] Starting avatar fetch for {total} contacts")

        # Atualizar job com total
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE background_jobs
                SET total_items = %s
                WHERE id = %s
            """, (total, job_id))
            conn.commit()

        stats = {'success': 0, 'failed': 0, 'skipped': 0}

        for i, contact in enumerate(contacts):
            phone = contact.get('phone', '')

            if not phone:
                stats['skipped'] += 1
            else:
                try:
                    photo_url = await self.fetch_whatsapp_photo(phone)

                    if photo_url:
                        success = self.update_contact_photo(contact['id'], photo_url)
                        if success:
                            stats['success'] += 1
                            logger.info(f"[Job {job_id}] {i+1}/{total} - Updated photo for {contact['nome']}")
                        else:
                            stats['failed'] += 1
                    else:
                        stats['failed'] += 1
                except Exception as e:
                    stats['failed'] += 1
                    logger.warning(f"[Job {job_id}] Error for {contact['nome']}: {e}")

            # Atualizar progresso a cada 10 contatos
            if (i + 1) % 10 == 0 or i == total - 1:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE background_jobs
                        SET processed_items = %s,
                            success_count = %s,
                            failed_count = %s,
                            skipped_count = %s
                        WHERE id = %s
                    """, (i + 1, stats['success'], stats['failed'], stats['skipped'], job_id))
                    conn.commit()

            # Delay para evitar rate limit
            if delay_between > 0 and i < total - 1:
                await asyncio.sleep(delay_between)

        # Marcar job como completo
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE background_jobs
                SET status = 'completed',
                    processed_items = %s,
                    success_count = %s,
                    failed_count = %s,
                    skipped_count = %s,
                    result = %s,
                    completed_at = NOW()
                WHERE id = %s
            """, (total, stats['success'], stats['failed'], stats['skipped'],
                  json.dumps(stats), job_id))
            conn.commit()

        logger.info(f"[Job {job_id}] Completed: {stats}")
        return stats


# Singleton
_avatar_fetcher = None


def get_avatar_fetcher() -> AvatarFetcherService:
    """Get singleton instance."""
    global _avatar_fetcher
    if _avatar_fetcher is None:
        _avatar_fetcher = AvatarFetcherService()
    return _avatar_fetcher
