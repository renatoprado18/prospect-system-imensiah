"""
LinkedIn Posting Integration - OAuth + Posts API

Handles OAuth 2.0 flow and publishing posts to LinkedIn.
Uses LinkedIn Posts API (v2).
"""
import os
import json
import logging
from datetime import datetime
from typing import Dict, Optional
import httpx

from database import get_db

logger = logging.getLogger(__name__)

LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_API_URL = "https://api.linkedin.com/v2"
LINKEDIN_POSTS_URL = "https://api.linkedin.com/rest/posts"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

SCOPES = "openid profile w_member_social"


def get_auth_url(state: str = "intel") -> str:
    """Generate LinkedIn OAuth authorization URL."""
    client_id = os.getenv("LINKEDIN_CLIENT_ID", "")
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI", "")
    return (
        f"{LINKEDIN_AUTH_URL}?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri={redirect_uri}&"
        f"state={state}&"
        f"scope={SCOPES}"
    )


async def exchange_code_for_token(code: str) -> Dict:
    """Exchange authorization code for access token."""
    client_id = os.getenv("LINKEDIN_CLIENT_ID", "")
    client_secret = os.getenv("LINKEDIN_CLIENT_SECRET", "")
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI", "")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            LINKEDIN_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if resp.status_code != 200:
        logger.error(f"LinkedIn token error: {resp.status_code} - {resp.text}")
        return {"error": resp.text}

    token_data = resp.json()

    # Get user profile (sub = person URN)
    access_token = token_data["access_token"]
    profile = await _get_user_profile(access_token)

    # Store token in database
    _save_token(access_token, token_data.get("expires_in", 5184000), profile)

    return {
        "success": True,
        "profile": profile,
        "expires_in": token_data.get("expires_in"),
    }


async def _get_user_profile(access_token: str) -> Dict:
    """Get LinkedIn user profile (sub = person URN)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            LINKEDIN_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code == 200:
        data = resp.json()
        return {
            "sub": data.get("sub", ""),
            "name": data.get("name", ""),
            "email": data.get("email", ""),
            "picture": data.get("picture", ""),
        }
    return {}


def _save_token(access_token: str, expires_in: int, profile: Dict):
    """Save LinkedIn token to database."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Use metadata table or a simple key-value approach
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS linkedin_tokens (
                id SERIAL PRIMARY KEY,
                person_urn TEXT,
                access_token TEXT NOT NULL,
                name TEXT,
                email TEXT,
                expires_at TIMESTAMP,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Delete old tokens and insert new
        cursor.execute("DELETE FROM linkedin_tokens")
        cursor.execute("""
            INSERT INTO linkedin_tokens (person_urn, access_token, name, email, expires_at)
            VALUES (%s, %s, %s, %s, NOW() + INTERVAL '%s seconds')
        """, (
            profile.get("sub", ""),
            access_token,
            profile.get("name", ""),
            profile.get("email", ""),
            expires_in,
        ))
        conn.commit()


def get_stored_token() -> Optional[Dict]:
    """Get stored LinkedIn token if still valid."""
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT person_urn, access_token, name, email, expires_at
                FROM linkedin_tokens
                WHERE expires_at > NOW()
                ORDER BY criado_em DESC LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                return dict(row)
        except Exception:
            pass
    return None


async def publish_post(text: str, article_url: str = None) -> Dict:
    """
    Publish a post to LinkedIn.

    Args:
        text: Post content
        article_url: Optional article URL to share

    Returns:
        Dict with post ID and URL
    """
    token_data = get_stored_token()
    if not token_data:
        return {"error": "LinkedIn nao conectado. Acesse /api/linkedin/authorize para conectar."}

    access_token = token_data["access_token"]
    person_urn = token_data["person_urn"]

    if not person_urn:
        return {"error": "Person URN nao encontrado. Reconecte o LinkedIn."}

    # Build post body
    post_body = {
        "author": f"urn:li:person:{person_urn}",
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
    }

    # Add article if provided
    if article_url:
        post_body["content"] = {
            "article": {
                "source": article_url,
                "title": "",
                "description": "",
            }
        }

    # LinkedIn-Version: YYYYMM. LinkedIn deprecia versoes ~12 meses depois;
    # bumpar a cada ~6 meses pra ficar com folga. 2026-05-03: "202401"
    # estourou HTTP 426 NONEXISTENT_VERSION e travou todas as publicacoes.
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            LINKEDIN_POSTS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
                "LinkedIn-Version": "202604",
            },
            json=post_body,
        )

    if resp.status_code in (200, 201):
        post_id = resp.headers.get("x-restli-id", "")
        post_url = f"https://www.linkedin.com/feed/update/{post_id}/" if post_id else ""
        return {"success": True, "post_id": post_id, "post_url": post_url}

    logger.error(f"LinkedIn post error: {resp.status_code} - {resp.text}")
    return {"error": f"Erro {resp.status_code}: {resp.text[:300]}"}
