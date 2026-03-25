"""
Gmail Integration for INTEL
Supports multiple Google accounts (professional + personal)
"""
import os
import json
import base64
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from urllib.parse import urlencode


class GmailIntegration:
    """
    Integration with Gmail API for email sync
    Supports multiple Google accounts with OAuth 2.0
    """

    # OAuth endpoints
    GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

    # Gmail API endpoints
    GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

    # OAuth scopes for Gmail
    SCOPES = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify"
    ]

    def __init__(self):
        self.client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.base_url = os.getenv("BASE_URL", "https://intel.almeida-prado.com")

    def get_auth_url(self, account_type: str = "professional") -> str:
        """
        Generate OAuth URL for connecting a Gmail account

        Args:
            account_type: 'professional' or 'personal'

        Returns:
            Authorization URL to redirect user
        """
        redirect_uri = f"{self.base_url}/api/gmail/callback"

        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.SCOPES),
            "access_type": "offline",
            "prompt": "consent",  # Force consent to get refresh_token
            "state": account_type  # Pass account type in state
        }

        return f"{self.GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> Dict[str, Any]:
        """
        Exchange authorization code for access and refresh tokens

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Token response with access_token, refresh_token, etc.
        """
        redirect_uri = f"{self.base_url}/api/gmail/callback"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri
                },
                timeout=30.0
            )

            if response.status_code != 200:
                return {"error": response.text}

            return response.json()

    async def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """
        Refresh an expired access token

        Args:
            refresh_token: Stored refresh token

        Returns:
            New token response
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token"
                },
                timeout=30.0
            )

            if response.status_code != 200:
                return {"error": response.text}

            return response.json()

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """
        Get user info (email, name, picture) from token
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self.GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0
            )

            if response.status_code != 200:
                return {"error": response.text}

            return response.json()

    async def list_messages(
        self,
        access_token: str,
        query: str = "",
        max_results: int = 50,
        page_token: str = None
    ) -> Dict[str, Any]:
        """
        List messages from Gmail

        Args:
            access_token: Valid OAuth access token
            query: Gmail search query (e.g., "from:someone@example.com")
            max_results: Maximum number of messages to return
            page_token: Token for pagination

        Returns:
            List of message IDs and metadata
        """
        params = {
            "maxResults": max_results,
            "includeSpamTrash": False
        }

        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.GMAIL_API_BASE}/users/me/messages",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                timeout=30.0
            )

            if response.status_code == 401:
                return {"error": "token_expired"}
            elif response.status_code != 200:
                return {"error": response.text}

            return response.json()

    async def get_message(
        self,
        access_token: str,
        message_id: str,
        format: str = "full"
    ) -> Dict[str, Any]:
        """
        Get a single message with full content

        Args:
            access_token: Valid OAuth access token
            message_id: Gmail message ID
            format: 'full', 'metadata', 'minimal', or 'raw'

        Returns:
            Message object with headers, body, etc.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.GMAIL_API_BASE}/users/me/messages/{message_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"format": format},
                timeout=30.0
            )

            if response.status_code == 401:
                return {"error": "token_expired"}
            elif response.status_code != 200:
                return {"error": response.text}

            return response.json()

    async def get_thread(
        self,
        access_token: str,
        thread_id: str,
        format: str = "full"
    ) -> Dict[str, Any]:
        """
        Get a complete email thread

        Args:
            access_token: Valid OAuth access token
            thread_id: Gmail thread ID
            format: 'full', 'metadata', or 'minimal'

        Returns:
            Thread object with all messages
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.GMAIL_API_BASE}/users/me/threads/{thread_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"format": format},
                timeout=30.0
            )

            if response.status_code == 401:
                return {"error": "token_expired"}
            elif response.status_code != 200:
                return {"error": response.text}

            return response.json()

    async def send_message(
        self,
        access_token: str,
        to: str,
        subject: str,
        body: str,
        html_body: str = None,
        reply_to_message_id: str = None,
        thread_id: str = None
    ) -> Dict[str, Any]:
        """
        Send an email message

        Args:
            access_token: Valid OAuth access token
            to: Recipient email address
            subject: Email subject
            body: Plain text body
            html_body: Optional HTML body
            reply_to_message_id: Message ID to reply to
            thread_id: Thread ID for threading

        Returns:
            Sent message details
        """
        import email.mime.text as mime_text
        import email.mime.multipart as mime_multipart

        # Build message
        if html_body:
            msg = mime_multipart.MIMEMultipart('alternative')
            msg.attach(mime_text.MIMEText(body, 'plain'))
            msg.attach(mime_text.MIMEText(html_body, 'html'))
        else:
            msg = mime_text.MIMEText(body)

        msg['To'] = to
        msg['Subject'] = subject

        if reply_to_message_id:
            msg['In-Reply-To'] = reply_to_message_id
            msg['References'] = reply_to_message_id

        # Encode message
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.GMAIL_API_BASE}/users/me/messages/send",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=30.0
            )

            if response.status_code == 401:
                return {"error": "token_expired"}
            elif response.status_code not in [200, 201]:
                return {"error": response.text}

            return response.json()

    def parse_message_headers(self, message: Dict) -> Dict[str, str]:
        """
        Extract common headers from a message
        """
        headers = {}
        payload = message.get("payload", {})

        for header in payload.get("headers", []):
            name = header.get("name", "").lower()
            value = header.get("value", "")

            if name in ["from", "to", "cc", "bcc", "subject", "date", "message-id"]:
                headers[name] = value

        return headers

    def parse_message_body(self, message: Dict) -> Dict[str, str]:
        """
        Extract body content from a message (plain text and HTML)
        """
        result = {"text": "", "html": ""}
        payload = message.get("payload", {})

        def extract_parts(part):
            mime_type = part.get("mimeType", "")
            body = part.get("body", {})
            data = body.get("data", "")

            if data:
                decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                if mime_type == "text/plain":
                    result["text"] = decoded
                elif mime_type == "text/html":
                    result["html"] = decoded

            # Recurse into parts
            for sub_part in part.get("parts", []):
                extract_parts(sub_part)

        extract_parts(payload)

        return result

    def extract_email_address(self, header_value: str) -> str:
        """
        Extract email address from a header like 'Name <email@example.com>'
        """
        import re
        match = re.search(r'<([^>]+)>', header_value)
        if match:
            return match.group(1).lower()
        # If no angle brackets, assume it's just the email
        return header_value.strip().lower()


# Helper functions for parsing
def parse_gmail_date(date_str: str) -> Optional[datetime]:
    """
    Parse Gmail date string to datetime
    """
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return None
