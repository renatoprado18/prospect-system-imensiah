"""
Google OAuth Authentication for Prospect System
Authenticates Renato (admin) and Andressa (operador) via Google
"""
import os
import secrets
from typing import Optional
from datetime import datetime, timedelta

from fastapi import Request, HTTPException, Depends
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# Config
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
BASE_URL = os.getenv("BASE_URL", "https://prospect-system.vercel.app")

# Allowed users and their roles
ALLOWED_USERS = {
    "renato@almeida-prado.com": {
        "role": "admin",
        "name": "Renato"
    },
    "andressa@almeida-prado.com": {
        "role": "operador",
        "name": "Andressa"
    }
}

# Session serializer
serializer = URLSafeTimedSerializer(SECRET_KEY)

# OAuth setup
oauth = OAuth()
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile'
    }
)


def create_session_token(user_data: dict) -> str:
    """Create a signed session token"""
    return serializer.dumps({
        "email": user_data["email"],
        "name": user_data["name"],
        "role": user_data["role"],
        "created_at": datetime.now().isoformat()
    })


def verify_session_token(token: str, max_age: int = 86400 * 7) -> Optional[dict]:
    """Verify and decode session token (default 7 days)"""
    try:
        return serializer.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request) -> Optional[dict]:
    """Get current user from session cookie"""
    token = request.cookies.get("session")
    if not token:
        return None
    return verify_session_token(token)


def require_auth(request: Request) -> dict:
    """Dependency that requires authentication"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Autenticacao necessaria"
        )
    return user


def require_admin(request: Request) -> dict:
    """Dependency that requires admin role"""
    user = require_auth(request)
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Acesso restrito a administradores"
        )
    return user


def require_operador(request: Request) -> dict:
    """Dependency that requires operador or admin role"""
    user = require_auth(request)
    if user.get("role") not in ["admin", "operador"]:
        raise HTTPException(
            status_code=403,
            detail="Acesso restrito"
        )
    return user


async def google_login(request: Request):
    """Initiate Google OAuth login"""
    redirect_uri = f"{BASE_URL}/auth/google/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


async def google_callback(request: Request):
    """Handle Google OAuth callback"""
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        return RedirectResponse(
            url=f"/login?error=oauth_failed&message={str(e)}",
            status_code=302
        )

    user_info = token.get('userinfo')
    if not user_info:
        return RedirectResponse(
            url="/login?error=no_user_info",
            status_code=302
        )

    email = user_info.get('email', '').lower()

    # Check if user is allowed
    if email not in ALLOWED_USERS:
        return RedirectResponse(
            url="/login?error=unauthorized&email=" + email,
            status_code=302
        )

    # Create session
    user_data = {
        "email": email,
        "name": user_info.get('name', ALLOWED_USERS[email]["name"]),
        "role": ALLOWED_USERS[email]["role"],
        "picture": user_info.get('picture', '')
    }

    session_token = create_session_token(user_data)

    # Redirect based on role
    redirect_url = "/admin" if user_data["role"] == "admin" else "/"

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=86400 * 7  # 7 days
    )

    return response


def logout():
    """Clear session and redirect to login"""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response
