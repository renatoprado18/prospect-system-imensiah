"""
LinkedIn Scraper Service
Uses RapidAPI to fetch recent posts and activity from LinkedIn profiles.
"""
import os
import json
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any


class LinkedInScraper:
    """Fetches LinkedIn profile data and posts via RapidAPI."""

    # Fresh LinkedIn Profile Data API
    BASE_URL = "https://fresh-linkedin-profile-data.p.rapidapi.com"

    def __init__(self):
        self.api_key = os.getenv("RAPIDAPI_KEY")
        if not self.api_key:
            raise ValueError("RAPIDAPI_KEY not configured")

        self.headers = {
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": "fresh-linkedin-profile-data.p.rapidapi.com"
        }

    def get_profile_posts(self, linkedin_url: str) -> Dict[str, Any]:
        """
        Fetch recent posts from a LinkedIn profile.

        Args:
            linkedin_url: LinkedIn profile URL (e.g., https://linkedin.com/in/username)

        Returns:
            Dict with posts data and last_activity timestamp
        """
        if not linkedin_url:
            return {"error": "No LinkedIn URL provided", "posts": [], "last_activity": None}

        try:
            # Get profile posts using Fresh LinkedIn Profile Data API
            response = requests.get(
                f"{self.BASE_URL}/get-linkedin-posts",
                headers=self.headers,
                params={"linkedin_url": linkedin_url, "type": "posts"},
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                posts = data.get("data", [])

                if posts is None:
                    posts = []

                # Extract last activity date
                last_activity = None
                if posts and isinstance(posts, list) and len(posts) > 0:
                    first_post = posts[0]
                    if first_post and isinstance(first_post, dict):
                        # Fresh LinkedIn API format
                        last_activity = (
                            first_post.get("posted_date") or
                            first_post.get("time") or
                            first_post.get("date")
                        )

                return {
                    "posts": posts[:5] if isinstance(posts, list) else [],
                    "last_activity": last_activity,
                    "total_posts": len(posts) if isinstance(posts, list) else 0,
                    "has_recent_posts": len(posts) > 0 if isinstance(posts, list) else False
                }

            elif response.status_code == 429:
                return {"error": "Rate limit exceeded", "posts": [], "last_activity": None}
            elif response.status_code == 403:
                return {"error": "Not subscribed to API. Subscribe at RapidAPI.", "posts": [], "last_activity": None}
            else:
                return {
                    "error": f"API error: {response.status_code} - {response.text[:200]}",
                    "posts": [],
                    "last_activity": None
                }

        except requests.exceptions.Timeout:
            return {"error": "Request timeout", "posts": [], "last_activity": None}
        except Exception as e:
            return {"error": str(e), "posts": [], "last_activity": None}

    def _extract_username(self, linkedin_url: str) -> Optional[str]:
        """Extract username from LinkedIn URL."""
        if not linkedin_url:
            return None

        # Handle various URL formats
        url = linkedin_url.strip().rstrip("/")

        # https://www.linkedin.com/in/username
        # https://linkedin.com/in/username
        # linkedin.com/in/username
        if "/in/" in url:
            parts = url.split("/in/")
            if len(parts) > 1:
                username = parts[1].split("/")[0].split("?")[0]
                return username

        return None

    def _has_recent_posts(self, posts: List, days: int = 30) -> bool:
        """Check if there are posts from the last N days."""
        if not posts or not isinstance(posts, list):
            return False

        # This is a simplified check - actual date parsing depends on API response format
        # For now, if there are posts, we consider it has recent activity
        return len(posts) > 0

    def enrich_contact(self, contact_id: int, linkedin_url: str) -> Dict[str, Any]:
        """
        Enrich a contact with LinkedIn data and update the database.

        Returns:
            Dict with enrichment results
        """
        from database import get_connection

        result = self.get_profile_posts(linkedin_url)

        if "error" in result and result["error"]:
            return {
                "contact_id": contact_id,
                "success": False,
                "error": result["error"]
            }

        # Update contact in database
        conn = get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                UPDATE contacts SET
                    linkedin_posts = %s,
                    linkedin_last_activity = %s,
                    linkedin_enriched_at = NOW()
                WHERE id = %s
            """, (
                json.dumps(result.get("posts", [])),
                result.get("last_activity"),
                contact_id
            ))
            conn.commit()

            return {
                "contact_id": contact_id,
                "success": True,
                "posts_found": result.get("total_posts", 0),
                "has_recent_posts": result.get("has_recent_posts", False),
                "last_activity": result.get("last_activity")
            }

        except Exception as e:
            conn.rollback()
            return {
                "contact_id": contact_id,
                "success": False,
                "error": str(e)
            }
        finally:
            cursor.close()
            conn.close()


def enrich_campaign_contacts(campaign_id: int, limit: int = 50) -> Dict[str, Any]:
    """
    Enrich contacts from a campaign with LinkedIn posts data.
    Prioritizes contacts by circle (lower = more important).

    Args:
        campaign_id: Campaign to enrich contacts from
        limit: Max contacts to enrich (default 50 for free tier)

    Returns:
        Summary of enrichment results
    """
    from database import get_connection

    conn = get_connection()
    cursor = conn.cursor()

    # Get contacts from campaign, prioritized by circle
    # Exclude already enriched (in last 7 days)
    cursor.execute("""
        SELECT c.id, c.nome, c.linkedin
        FROM contacts c
        JOIN campaign_enrollments ce ON ce.contact_id = c.id
        WHERE ce.campaign_id = %s
          AND ce.status = 'active'
          AND c.linkedin IS NOT NULL
          AND c.linkedin != ''
          AND (c.linkedin_enriched_at IS NULL OR c.linkedin_enriched_at < NOW() - INTERVAL '7 days')
        ORDER BY c.circulo ASC, c.health_score DESC
        LIMIT %s
    """, (campaign_id, limit))

    contacts = cursor.fetchall()
    cursor.close()
    conn.close()

    if not contacts:
        return {
            "success": True,
            "message": "No contacts to enrich",
            "total": 0,
            "enriched": 0,
            "with_posts": 0,
            "errors": 0
        }

    # Check if API key is configured
    api_key = os.getenv("RAPIDAPI_KEY")
    if not api_key:
        return {
            "success": False,
            "error": "RAPIDAPI_KEY not configured. Add it to .env file.",
            "contacts_to_enrich": len(contacts)
        }

    scraper = LinkedInScraper()

    results = {
        "total": len(contacts),
        "enriched": 0,
        "with_posts": 0,
        "errors": 0,
        "details": []
    }

    for contact_id, nome, linkedin_url in contacts:
        result = scraper.enrich_contact(contact_id, linkedin_url)
        results["details"].append({
            "contact_id": contact_id,
            "nome": nome,
            **result
        })

        if result.get("success"):
            results["enriched"] += 1
            if result.get("has_recent_posts"):
                results["with_posts"] += 1
        else:
            results["errors"] += 1

    results["success"] = True
    results["message"] = f"Enriched {results['enriched']}/{results['total']} contacts. {results['with_posts']} have recent posts."

    return results


def get_enrichment_stats(campaign_id: int) -> Dict[str, Any]:
    """Get LinkedIn enrichment statistics for a campaign."""
    from database import get_connection

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE c.linkedin IS NOT NULL AND c.linkedin != '') as with_linkedin,
            COUNT(*) FILTER (WHERE c.linkedin_enriched_at IS NOT NULL) as enriched,
            COUNT(*) FILTER (WHERE c.linkedin_posts::text != '[]' AND c.linkedin_posts IS NOT NULL) as with_posts,
            COUNT(*) FILTER (WHERE c.linkedin_enriched_at > NOW() - INTERVAL '7 days') as recently_enriched
        FROM contacts c
        JOIN campaign_enrollments ce ON ce.contact_id = c.id
        WHERE ce.campaign_id = %s AND ce.status = 'active'
    """, (campaign_id,))

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    return {
        "total_enrolled": row[0],
        "with_linkedin_url": row[1],
        "enriched": row[2],
        "with_recent_posts": row[3],
        "recently_enriched": row[4],
        "pending_enrichment": row[1] - row[2] if row[1] and row[2] else 0
    }
