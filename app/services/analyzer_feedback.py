"""
Analyzer Feedback Service - Learning from user responses

Tracks user acceptance/rejection of proposals and adjusts analyzer behavior.
"""
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import get_db


class AnalyzerFeedbackService:
    """Manages feedback learning for the realtime analyzer."""

    def record_feedback(
        self,
        proposal_id: int,
        user_action: str,
        option_chosen: str = None
    ) -> bool:
        """
        Record user feedback for a proposal.

        Args:
            proposal_id: ID of the action proposal
            user_action: 'accepted', 'rejected', 'dismissed', 'expired'
            option_chosen: ID of the option the user chose

        Returns:
            True if recorded successfully
        """
        with get_db() as conn:
            cursor = conn.cursor()

            # Get proposal details
            cursor.execute("""
                SELECT action_type, confidence, urgency, contact_id, trigger_text
                FROM action_proposals
                WHERE id = %s
            """, (proposal_id,))

            proposal = cursor.fetchone()
            if not proposal:
                return False

            # Extract intent_type from action_type (they map 1:1 mostly)
            intent_type = self._action_to_intent(proposal['action_type'])

            cursor.execute("""
                INSERT INTO analyzer_feedback (
                    proposal_id, intent_type, action_type, confidence, urgency,
                    user_action, option_chosen, contact_id, message_preview
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                proposal_id,
                intent_type,
                proposal['action_type'],
                proposal['confidence'],
                proposal['urgency'],
                user_action,
                option_chosen,
                proposal.get('contact_id'),
                proposal.get('trigger_text', '')[:200]
            ))

            conn.commit()
            return True

    def _action_to_intent(self, action_type: str) -> str:
        """Map action_type back to intent_type."""
        mapping = {
            'reschedule_event': 'reschedule_meeting',
            'cancel_event': 'cancel_meeting',
            'confirm_event': 'confirm_meeting',
            'urgent_alert': 'urgent_request',
            'pending_response': 'question',
            'financial_alert': 'payment_mention',
            'introduction_request': 'introduction_request',
            'opportunity_alert': 'opportunity_signal',
            'complaint_alert': 'complaint',
            'meeting_request': 'meeting_request',
            'follow_up_alert': 'follow_up_needed',
        }
        return mapping.get(action_type, action_type)

    def get_intent_stats(self, days: int = 30) -> Dict[str, Dict]:
        """
        Get acceptance/rejection stats per intent type.

        Returns:
            {
                'intent_type': {
                    'total': int,
                    'accepted': int,
                    'rejected': int,
                    'dismissed': int,
                    'acceptance_rate': float,
                    'avg_confidence': float
                }
            }
        """
        stats = {}
        since = datetime.now() - timedelta(days=days)

        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    intent_type,
                    COUNT(*) as total,
                    SUM(CASE WHEN user_action = 'accepted' THEN 1 ELSE 0 END) as accepted,
                    SUM(CASE WHEN user_action = 'rejected' THEN 1 ELSE 0 END) as rejected,
                    SUM(CASE WHEN user_action = 'dismissed' THEN 1 ELSE 0 END) as dismissed,
                    AVG(confidence) as avg_confidence
                FROM analyzer_feedback
                WHERE responded_at >= %s
                GROUP BY intent_type
            """, (since,))

            for row in cursor.fetchall():
                total = row['total'] or 1
                accepted = row['accepted'] or 0
                stats[row['intent_type']] = {
                    'total': total,
                    'accepted': accepted,
                    'rejected': row['rejected'] or 0,
                    'dismissed': row['dismissed'] or 0,
                    'acceptance_rate': accepted / total,
                    'avg_confidence': row['avg_confidence'] or 0.7
                }

        return stats

    def get_recommended_threshold(self, intent_type: str) -> float:
        """
        Get recommended confidence threshold based on feedback.

        If acceptance rate is high, we can lower the threshold.
        If acceptance rate is low, we should raise it.

        Returns:
            Recommended confidence threshold (0.5-0.9)
        """
        stats = self.get_intent_stats(days=30)

        if intent_type not in stats:
            return 0.7  # Default threshold

        intent_stats = stats[intent_type]

        if intent_stats['total'] < 5:
            return 0.7  # Not enough data

        acceptance_rate = intent_stats['acceptance_rate']

        # Adjust threshold based on acceptance rate
        if acceptance_rate >= 0.8:
            # High acceptance - can be less strict
            return max(0.5, intent_stats['avg_confidence'] - 0.1)
        elif acceptance_rate >= 0.5:
            # Medium acceptance - keep current
            return intent_stats['avg_confidence']
        else:
            # Low acceptance - be more strict
            return min(0.9, intent_stats['avg_confidence'] + 0.15)

    def should_propose(self, intent_type: str, confidence: float) -> bool:
        """
        Determine if we should create a proposal based on learned thresholds.

        Args:
            intent_type: Type of intent detected
            confidence: Confidence score from analyzer

        Returns:
            True if we should create a proposal
        """
        # Get user settings
        settings = self.get_settings()
        min_confidence = settings.get('min_confidence', 0.7)
        enabled_intents = settings.get('enabled_intents', [])

        # Check if intent is enabled
        if enabled_intents and intent_type not in enabled_intents:
            return False

        # Get learned threshold
        learned_threshold = self.get_recommended_threshold(intent_type)

        # Use the higher of user setting and learned threshold
        effective_threshold = max(min_confidence, learned_threshold)

        return confidence >= effective_threshold

    def get_settings(self) -> Dict:
        """Get analyzer settings."""
        settings = {}

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT setting_key, setting_value FROM analyzer_settings")

            for row in cursor.fetchall():
                try:
                    settings[row['setting_key']] = json.loads(row['setting_value'])
                except (json.JSONDecodeError, TypeError):
                    settings[row['setting_key']] = row['setting_value']

        return settings

    def update_setting(self, key: str, value) -> bool:
        """Update a single setting."""
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO analyzer_settings (setting_key, setting_value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (setting_key)
                DO UPDATE SET setting_value = %s, updated_at = NOW()
            """, (key, json.dumps(value), json.dumps(value)))

            conn.commit()
            return True

    def get_learning_summary(self) -> Dict:
        """
        Get a summary of what the system has learned.

        Returns:
            Summary with stats and recommendations
        """
        stats = self.get_intent_stats(days=30)
        settings = self.get_settings()

        summary = {
            'total_feedback': sum(s['total'] for s in stats.values()),
            'overall_acceptance_rate': 0,
            'intent_stats': stats,
            'recommendations': [],
            'current_settings': settings
        }

        if summary['total_feedback'] > 0:
            total_accepted = sum(s['accepted'] for s in stats.values())
            summary['overall_acceptance_rate'] = total_accepted / summary['total_feedback']

        # Generate recommendations
        for intent_type, data in stats.items():
            if data['total'] >= 5:
                if data['acceptance_rate'] < 0.3:
                    summary['recommendations'].append({
                        'intent_type': intent_type,
                        'recommendation': 'disable',
                        'reason': f"Baixa taxa de aceitacao ({data['acceptance_rate']:.0%})"
                    })
                elif data['acceptance_rate'] < 0.5:
                    summary['recommendations'].append({
                        'intent_type': intent_type,
                        'recommendation': 'increase_threshold',
                        'reason': f"Taxa de aceitacao moderada ({data['acceptance_rate']:.0%})"
                    })

        return summary


# Singleton
_feedback_service = None


def get_feedback_service() -> AnalyzerFeedbackService:
    """Get singleton instance."""
    global _feedback_service
    if _feedback_service is None:
        _feedback_service = AnalyzerFeedbackService()
    return _feedback_service
