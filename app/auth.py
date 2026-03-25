"""
Google OAuth Authentication for Prospect System
Authenticates Renato (admin) and Andressa (operador) via Google
"""
import os
import secrets
import httpx
from urllib.parse import urlencode
from typing import Optional
from datetime import datetime

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

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

def get_config():
    """Get OAuth config lazily"""
    return {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "secret_key": os.getenv("SECRET_KEY", secrets.token_hex(32)),
        "base_url": os.getenv("BASE_URL", "https://prospect-system.vercel.app")
    }

# Google OAuth endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

def get_serializer():
    config = get_config()
    return URLSafeTimedSerializer(config["secret_key"])

def create_session_token(user_data: dict) -> str:
    """Create a signed session token"""
    serializer = get_serializer()
    return serializer.dumps({
        "email": user_data["email"],
        "name": user_data["name"],
        "role": user_data["role"],
        "created_at": datetime.now().isoformat()
    })

def verify_session_token(token: str, max_age: int = 86400 * 7) -> Optional[dict]:
    """Verify and decode session token (default 7 days)"""
    try:
        serializer = get_serializer()
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
        raise HTTPException(status_code=401, detail="Autenticacao necessaria")
    return user

def require_admin(request: Request) -> dict:
    """Dependency that requires admin role"""
    user = require_auth(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    return user

def require_operador(request: Request) -> dict:
    """Dependency that requires operador or admin role"""
    user = require_auth(request)
    if user.get("role") not in ["admin", "operador"]:
        raise HTTPException(status_code=403, detail="Acesso restrito")
    return user

async def google_login(request: Request):
    """Initiate Google OAuth login"""
    config = get_config()
    redirect_uri = f"{config['base_url']}/auth/google/callback"

    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account"
    }

    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url=auth_url, status_code=302)

async def google_callback(request: Request):
    """Handle Google OAuth callback"""
    config = get_config()

    # Get authorization code
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        return RedirectResponse(
            url=f"/login?error={error}",
            status_code=302
        )

    if not code:
        return RedirectResponse(
            url="/login?error=no_code",
            status_code=302
        )

    redirect_uri = f"{config['base_url']}/auth/google/callback"

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        try:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri
                }
            )

            if token_response.status_code != 200:
                error_detail = token_response.text
                return RedirectResponse(
                    url=f"/login?error=token_error&detail={error_detail[:100]}",
                    status_code=302
                )

            tokens = token_response.json()
            access_token = tokens.get("access_token")

            if not access_token:
                return RedirectResponse(
                    url="/login?error=no_access_token",
                    status_code=302
                )

            # Get user info
            userinfo_response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"}
            )

            if userinfo_response.status_code != 200:
                return RedirectResponse(
                    url="/login?error=userinfo_error",
                    status_code=302
                )

            user_info = userinfo_response.json()

        except Exception as e:
            return RedirectResponse(
                url=f"/login?error=oauth_error&message={str(e)[:100]}",
                status_code=302
            )

    email = user_info.get("email", "").lower()

    # Check if user is allowed
    if email not in ALLOWED_USERS:
        return RedirectResponse(
            url=f"/login?error=unauthorized&email={email}",
            status_code=302
        )

    # Create session
    user_data = {
        "email": email,
        "name": user_info.get("name", ALLOWED_USERS[email]["name"]),
        "role": ALLOWED_USERS[email]["role"],
        "picture": user_info.get("picture", "")
    }

    session_token = create_session_token(user_data)

    # Redirect to INTEL dashboard
    redirect_url = "/"

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=86400 * 7
    )

    return response

def logout():
    """Clear session and redirect to login"""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response

# For backwards compatibility
SECRET_KEY = get_config()["secret_key"]
oauth = None  # Not used anymore
