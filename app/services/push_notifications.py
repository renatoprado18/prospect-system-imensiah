"""
Browser Push Notifications Service for INTEL

Uses Web Push protocol with VAPID authentication.
"""
import os
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Try to import pywebpush, handle if not installed
try:
    from pywebpush import webpush, WebPushException
    WEBPUSH_AVAILABLE = True
except ImportError:
    WEBPUSH_AVAILABLE = False
    logger.warning("pywebpush not installed. Push notifications disabled.")


class PushNotificationService:
    """Manages browser push notification subscriptions and sending."""

    def __init__(self):
        self.vapid_private_key = os.getenv("VAPID_PRIVATE_KEY", "")
        self.vapid_public_key = os.getenv("VAPID_PUBLIC_KEY", "")
        self.vapid_claims = {
            "sub": f"mailto:{os.getenv('VAPID_EMAIL', 'admin@intel.almeida-prado.com')}"
        }

    def is_configured(self) -> bool:
        """Check if push notifications are properly configured."""
        return bool(WEBPUSH_AVAILABLE and self.vapid_private_key and self.vapid_public_key)

    def get_public_key(self) -> Optional[str]:
        """Return VAPID public key for frontend subscription."""
        return self.vapid_public_key if self.vapid_public_key else None

    def save_subscription(self, subscription: Dict, user_id: str = None) -> bool:
        """Save push subscription to database."""
        from database import get_db

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Check if subscription already exists
                cursor.execute("""
                    SELECT id FROM push_subscriptions
                    WHERE endpoint = %s
                """, (subscription.get('endpoint'),))

                existing = cursor.fetchone()

                if existing:
                    # Update existing subscription
                    cursor.execute("""
                        UPDATE push_subscriptions
                        SET keys = %s, user_id = %s, atualizado_em = NOW()
                        WHERE endpoint = %s
                    """, (
                        json.dumps(subscription.get('keys', {})),
                        user_id,
                        subscription.get('endpoint')
                    ))
                else:
                    # Insert new subscription
                    cursor.execute("""
                        INSERT INTO push_subscriptions (endpoint, keys, user_id, criado_em)
                        VALUES (%s, %s, %s, NOW())
                    """, (
                        subscription.get('endpoint'),
                        json.dumps(subscription.get('keys', {})),
                        user_id
                    ))

                conn.commit()
                return True

        except Exception as e:
            logger.error(f"Error saving push subscription: {e}")
            return False

    def remove_subscription(self, endpoint: str) -> bool:
        """Remove push subscription from database."""
        from database import get_db

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    DELETE FROM push_subscriptions WHERE endpoint = %s
                """, (endpoint,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing subscription: {e}")
            return False

    def get_all_subscriptions(self) -> List[Dict]:
        """Get all active push subscriptions."""
        from database import get_db

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT endpoint, keys, user_id FROM push_subscriptions
                """)
                return [
                    {
                        'endpoint': row['endpoint'],
                        'keys': json.loads(row['keys']) if isinstance(row['keys'], str) else row['keys'],
                        'user_id': row.get('user_id')
                    }
                    for row in cursor.fetchall()
                ]
        except Exception as e:
            logger.error(f"Error getting subscriptions: {e}")
            return []

    def send_notification(
        self,
        title: str,
        body: str,
        data: Dict = None,
        actions: List[Dict] = None,
        tag: str = None,
        urgent: bool = False,
        subscription: Dict = None
    ) -> Dict:
        """
        Send push notification to a specific subscription or all subscribers.

        Args:
            title: Notification title
            body: Notification body text
            data: Additional data for click handling
            actions: Action buttons [{action: str, title: str}]
            tag: Unique tag for notification grouping
            urgent: If True, notification requires interaction
            subscription: Specific subscription to send to (if None, sends to all)

        Returns:
            {'success': bool, 'sent': int, 'failed': int, 'errors': []}
        """
        if not self.is_configured():
            return {
                'success': False,
                'sent': 0,
                'failed': 0,
                'errors': ['Push notifications not configured']
            }

        payload = {
            'title': title,
            'body': body,
            'tag': tag or f'intel-{datetime.now().timestamp()}',
            'data': {**(data or {}), 'urgent': urgent},
            'actions': actions or []
        }

        subscriptions = [subscription] if subscription else self.get_all_subscriptions()

        sent = 0
        failed = 0
        errors = []

        for sub in subscriptions:
            try:
                subscription_info = {
                    'endpoint': sub['endpoint'],
                    'keys': sub['keys']
                }

                webpush(
                    subscription_info=subscription_info,
                    data=json.dumps(payload),
                    vapid_private_key=self.vapid_private_key,
                    vapid_claims=self.vapid_claims
                )
                sent += 1

            except WebPushException as e:
                failed += 1
                errors.append(str(e))

                # Remove invalid subscriptions (410 Gone)
                if e.response and e.response.status_code == 410:
                    self.remove_subscription(sub['endpoint'])
                    logger.info(f"Removed expired subscription: {sub['endpoint'][:50]}...")

            except Exception as e:
                failed += 1
                errors.append(str(e))
                logger.error(f"Error sending push notification: {e}")

        return {
            'success': sent > 0,
            'sent': sent,
            'failed': failed,
            'errors': errors[:5]  # Limit error list
        }

    def send_proposal_notification(self, proposal: Dict) -> Dict:
        """Send push notification for an action proposal."""
        urgency = proposal.get('urgency', 'medium')
        is_urgent = urgency == 'high'

        # Build notification
        title = f"{'🔴 ' if is_urgent else ''}INTEL: {proposal.get('title', 'Nova acao')}"
        body = proposal.get('description', '')[:200]

        # Get first two options as actions
        options = proposal.get('options', [])
        actions = []
        for opt in options[:2]:
            if opt.get('action') != 'dismiss':
                actions.append({
                    'action': 'execute',
                    'title': opt.get('label', 'Executar')[:20]
                })

        data = {
            'proposal_id': proposal.get('id'),
            'contact_id': proposal.get('contact_id'),
            'action_type': proposal.get('action_type'),
            'url': f"/rap#proposal-{proposal.get('id')}"
        }

        if options:
            data['option_id'] = options[0].get('id')

        return self.send_notification(
            title=title,
            body=body,
            data=data,
            actions=actions,
            tag=f"proposal-{proposal.get('id')}",
            urgent=is_urgent
        )


# Singleton instance
_push_service = None


def get_push_service() -> PushNotificationService:
    """Get singleton instance of push notification service."""
    global _push_service
    if _push_service is None:
        _push_service = PushNotificationService()
    return _push_service
