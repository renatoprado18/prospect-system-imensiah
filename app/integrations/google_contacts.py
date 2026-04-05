"""
Google Contacts Integration via People API
Supports multiple Google accounts (personal + professional)
"""
import os
import json
import httpx
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from urllib.parse import urlencode

# Google API endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_PEOPLE_API = "https://people.googleapis.com/v1"

# Scopes for contacts + Gmail + Calendar + Tasks + Drive
CONTACTS_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/contacts",  # Read/write contacts
    "https://www.googleapis.com/auth/gmail.readonly",  # Read emails
    "https://www.googleapis.com/auth/gmail.send",  # Send emails
    "https://www.googleapis.com/auth/gmail.modify",  # Modify emails (labels, read status)
    "https://www.googleapis.com/auth/calendar",  # Full calendar access (read/write)
    "https://www.googleapis.com/auth/tasks",  # Full tasks access (read/write)
    "https://www.googleapis.com/auth/drive",  # Full Google Drive access
]


def get_oauth_config():
    """Get OAuth configuration"""
    return {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "base_url": os.getenv("BASE_URL", "https://intel.almeida-prado.com")
    }


def get_connect_url(account_type: str = "professional") -> str:
    """
    Generate OAuth URL for connecting a Google account
    account_type: 'professional' or 'personal'
    """
    config = get_oauth_config()
    redirect_uri = f"{config['base_url']}/api/google/callback"

    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(CONTACTS_SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # Force consent to get refresh_token
        "state": account_type  # Pass account type in state
    }

    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
    """Exchange authorization code for access and refresh tokens"""
    config = get_oauth_config()
    redirect_uri = f"{config['base_url']}/api/google/callback"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri
            }
        )

        if response.status_code != 200:
            raise Exception(f"Token exchange failed: {response.text}")

        return response.json()


async def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh an expired access token"""
    config = get_oauth_config()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }
        )

        if response.status_code != 200:
            raise Exception(f"Token refresh failed: {response.text}")

        return response.json()


async def get_valid_token(email: str) -> Optional[str]:
    """
    Get a valid access token for a Google account by email.
    Refreshes the token if expired.

    Args:
        email: The Google account email

    Returns:
        Valid access token or None
    """
    from database import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT access_token, refresh_token, token_expiry
            FROM google_accounts
            WHERE email = %s
        """, (email,))

        account = cursor.fetchone()
        if not account:
            return None

        access_token = account["access_token"]
        refresh_token = account["refresh_token"]
        token_expiry = account["token_expiry"]

        # Check if token is expired or will expire in 5 minutes
        if token_expiry:
            expiry_buffer = datetime.now() + timedelta(minutes=5)
            if token_expiry < expiry_buffer and refresh_token:
                # Refresh the token
                try:
                    new_tokens = await refresh_access_token(refresh_token)
                    access_token = new_tokens.get("access_token")

                    # Calculate new expiry
                    expires_in = new_tokens.get("expires_in", 3600)
                    new_expiry = datetime.now() + timedelta(seconds=expires_in)

                    # Update database
                    cursor.execute("""
                        UPDATE google_accounts
                        SET access_token = %s, token_expiry = %s
                        WHERE email = %s
                    """, (access_token, new_expiry, email))
                    conn.commit()

                except Exception as e:
                    print(f"Error refreshing token for {email}: {e}")
                    return None

        return access_token


async def get_user_email(access_token: str) -> str:
    """Get email address of the authenticated user"""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )

        if response.status_code != 200:
            raise Exception(f"Failed to get user info: {response.text}")

        return response.json().get("email", "")


async def fetch_all_contacts(access_token: str, page_size: int = 1000) -> List[Dict]:
    """
    Fetch all contacts from Google Contacts using People API
    Returns list of contact dictionaries
    """
    contacts = []
    next_page_token = None

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            params = {
                "pageSize": page_size,
                "personFields": "names,emailAddresses,phoneNumbers,organizations,photos,birthdays,urls,biographies,memberships,addresses,relations",
                "sources": "READ_SOURCE_TYPE_CONTACT"
            }

            if next_page_token:
                params["pageToken"] = next_page_token

            response = await client.get(
                f"{GOOGLE_PEOPLE_API}/people/me/connections",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params
            )

            if response.status_code == 401:
                raise Exception("Token expired")

            if response.status_code != 200:
                raise Exception(f"Failed to fetch contacts: {response.text}")

            data = response.json()
            connections = data.get("connections", [])
            contacts.extend(connections)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

    return contacts


def parse_google_contact(person: Dict, account_email: str) -> Dict:
    """
    Parse a Google People API person object into our contact format
    """
    # Extract name
    names = person.get("names", [])
    name = ""
    if names:
        name_obj = names[0]
        name = name_obj.get("displayName", "")
        if not name:
            given = name_obj.get("givenName", "")
            family = name_obj.get("familyName", "")
            name = f"{given} {family}".strip()

    # Extract emails
    emails = []
    for email_obj in person.get("emailAddresses", []):
        emails.append({
            "email": email_obj.get("value", "").lower(),
            "type": email_obj.get("type", "other").lower(),
            "primary": email_obj.get("metadata", {}).get("primary", False)
        })

    # Extract phones
    telefones = []
    for phone_obj in person.get("phoneNumbers", []):
        phone_type = phone_obj.get("type", "other").lower()
        telefones.append({
            "number": phone_obj.get("value", ""),
            "type": phone_type,
            "whatsapp": phone_type in ["mobile", "cell"]
        })

    # Extract organization
    empresa = ""
    cargo = ""
    orgs = person.get("organizations", [])
    if orgs:
        org = orgs[0]
        empresa = org.get("name", "")
        cargo = org.get("title", "")

    # Extract photo
    foto_url = None
    photos = person.get("photos", [])
    if photos:
        foto_url = photos[0].get("url", "")

    # Extract birthday
    aniversario = None
    birthdays = person.get("birthdays", [])
    if birthdays:
        bday = birthdays[0].get("date", {})
        if bday.get("year") and bday.get("month") and bday.get("day"):
            aniversario = f"{bday['year']}-{bday['month']:02d}-{bday['day']:02d}"
        elif bday.get("month") and bday.get("day"):
            aniversario = f"1900-{bday['month']:02d}-{bday['day']:02d}"

    # Extract LinkedIn URL
    linkedin = None
    for url_obj in person.get("urls", []):
        url = url_obj.get("value", "")
        if "linkedin.com" in url:
            linkedin = url
            break

    # Extract addresses
    enderecos = []
    for addr_obj in person.get("addresses", []):
        tipo_map = {"home": "residencial", "work": "comercial", "other": "outro"}
        addr_type = addr_obj.get("type", "other").lower()
        enderecos.append({
            "tipo": tipo_map.get(addr_type, "outro"),
            "logradouro": addr_obj.get("streetAddress", ""),
            "cidade": addr_obj.get("city", ""),
            "estado": addr_obj.get("region", ""),
            "cep": addr_obj.get("postalCode", ""),
            "pais": addr_obj.get("country", "Brasil")
        })

    # Extract relations
    relacionamentos = []
    tipo_map = {
        "spouse": "conjuge",
        "child": "filho",
        "parent": "pai",
        "sibling": "irmao",
        "friend": "amigo",
        "manager": "chefe",
        "assistant": "assistente",
        "domesticPartner": "conjuge",
        "relative": "parente"
    }
    for rel_obj in person.get("relations", []):
        rel_type = rel_obj.get("type", "").lower()
        relacionamentos.append({
            "tipo": tipo_map.get(rel_type, rel_type),
            "nome": rel_obj.get("person", ""),
            "contact_id": None  # Will be linked later if possible
        })

    # Resource name is the unique Google ID
    resource_name = person.get("resourceName", "")
    google_contact_id = resource_name.replace("people/", "") if resource_name else None

    # Determine context based on account
    contexto = "personal" if "gmail.com" in account_email else "professional"

    return {
        "nome": name,
        "empresa": empresa,
        "cargo": cargo,
        "emails": emails,
        "telefones": telefones,
        "foto_url": foto_url,
        "linkedin": linkedin,
        "aniversario": aniversario,
        "enderecos": enderecos,
        "relacionamentos": relacionamentos,
        "google_contact_id": google_contact_id,
        "contexto": contexto,
        "origem": f"google_{account_email}"
    }


async def sync_contacts_from_google(
    access_token: str,
    refresh_token: str,
    account_email: str,
    db_connection
) -> Dict[str, int]:
    """
    Sync all contacts from a Google account to local database
    Returns stats: {imported, updated, skipped, errors}
    """
    stats = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    try:
        # Fetch all contacts from Google
        google_contacts = await fetch_all_contacts(access_token)
    except Exception as e:
        if "Token expired" in str(e):
            # Try to refresh token
            try:
                new_tokens = await refresh_access_token(refresh_token)
                access_token = new_tokens["access_token"]
                google_contacts = await fetch_all_contacts(access_token)

                # Update token in database
                cursor = db_connection.cursor()
                cursor.execute('''
                    UPDATE google_accounts
                    SET access_token = %s, token_expiry = %s
                    WHERE email = %s
                ''', (
                    access_token,
                    (datetime.now() + timedelta(seconds=new_tokens.get("expires_in", 3600))).isoformat(),
                    account_email
                ))
                db_connection.commit()
            except Exception as refresh_error:
                raise Exception(f"Token refresh failed: {refresh_error}")
        else:
            raise

    cursor = db_connection.cursor()

    for person in google_contacts:
        try:
            contact = parse_google_contact(person, account_email)

            if not contact["nome"]:
                stats["skipped"] += 1
                continue

            # Check if contact exists by google_contact_id
            if contact["google_contact_id"]:
                cursor.execute(
                    "SELECT id FROM contacts WHERE google_contact_id = %s",
                    (contact["google_contact_id"],)
                )
                existing = cursor.fetchone()

                if existing:
                    # Update existing contact
                    cursor.execute('''
                        UPDATE contacts SET
                            nome = %s,
                            empresa = %s,
                            cargo = %s,
                            emails = %s,
                            telefones = %s,
                            foto_url = COALESCE(%s, foto_url),
                            linkedin = COALESCE(%s, linkedin),
                            aniversario = COALESCE(%s, aniversario),
                            enderecos = COALESCE(%s, enderecos),
                            relacionamentos = COALESCE(%s, relacionamentos),
                            contexto = %s,
                            atualizado_em = CURRENT_TIMESTAMP
                        WHERE google_contact_id = %s
                    ''', (
                        contact["nome"],
                        contact["empresa"],
                        contact["cargo"],
                        json.dumps(contact["emails"]),
                        json.dumps(contact["telefones"]),
                        contact["foto_url"],
                        contact["linkedin"],
                        contact["aniversario"],
                        json.dumps(contact.get("enderecos", [])),
                        json.dumps(contact.get("relacionamentos", [])),
                        contact["contexto"],
                        contact["google_contact_id"]
                    ))
                    stats["updated"] += 1
                    continue

            # Check by email match
            if contact["emails"]:
                primary_email = contact["emails"][0]["email"]
                cursor.execute(
                    "SELECT id FROM contacts WHERE emails @> %s::jsonb",
                    (json.dumps([{"email": primary_email}]),)
                )
                existing = cursor.fetchone()

                if existing:
                    # Update with google_contact_id
                    cursor.execute('''
                        UPDATE contacts SET
                            google_contact_id = %s,
                            foto_url = COALESCE(%s, foto_url),
                            linkedin = COALESCE(%s, linkedin),
                            aniversario = COALESCE(%s, aniversario),
                            atualizado_em = CURRENT_TIMESTAMP
                        WHERE id = %s
                    ''', (
                        contact["google_contact_id"],
                        contact["foto_url"],
                        contact["linkedin"],
                        contact["aniversario"],
                        existing["id"]
                    ))
                    stats["updated"] += 1
                    continue

            # Insert new contact
            cursor.execute('''
                INSERT INTO contacts (
                    nome, empresa, cargo, emails, telefones, foto_url,
                    linkedin, aniversario, enderecos, relacionamentos,
                    google_contact_id, contexto, origem
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                contact["nome"],
                contact["empresa"],
                contact["cargo"],
                json.dumps(contact["emails"]),
                json.dumps(contact["telefones"]),
                contact["foto_url"],
                contact["linkedin"],
                contact["aniversario"],
                json.dumps(contact.get("enderecos", [])),
                json.dumps(contact.get("relacionamentos", [])),
                contact["google_contact_id"],
                contact["contexto"],
                contact["origem"]
            ))
            stats["imported"] += 1

        except Exception as e:
            stats["errors"] += 1
            continue

    db_connection.commit()

    # Update last sync time
    cursor.execute('''
        UPDATE google_accounts SET ultima_sync = CURRENT_TIMESTAMP
        WHERE email = %s
    ''', (account_email,))
    db_connection.commit()

    return stats


async def create_google_contact(access_token: str, contact_data: Dict) -> Optional[str]:
    """
    Create a new contact in Google Contacts
    Returns the resourceName (Google contact ID) if successful
    """
    person = {
        "names": [{"givenName": contact_data.get("nome", "").split()[0]}]
    }

    # Add full name
    name_parts = contact_data.get("nome", "").split()
    if len(name_parts) > 1:
        person["names"][0]["familyName"] = " ".join(name_parts[1:])

    # Add emails
    if contact_data.get("emails"):
        person["emailAddresses"] = [
            {"value": e["email"], "type": e.get("type", "other")}
            for e in contact_data["emails"]
        ]

    # Add phones
    if contact_data.get("telefones"):
        person["phoneNumbers"] = [
            {"value": t["number"], "type": t.get("type", "mobile")}
            for t in contact_data["telefones"]
        ]

    # Add organization
    if contact_data.get("empresa") or contact_data.get("cargo"):
        person["organizations"] = [{
            "name": contact_data.get("empresa", ""),
            "title": contact_data.get("cargo", "")
        }]

    # Add addresses
    if contact_data.get("enderecos"):
        tipo_map = {"residencial": "home", "comercial": "work", "outro": "other"}
        person["addresses"] = [
            {
                "streetAddress": a.get("logradouro", ""),
                "city": a.get("cidade", ""),
                "region": a.get("estado", ""),
                "postalCode": a.get("cep", ""),
                "country": a.get("pais", "Brasil"),
                "type": tipo_map.get(a.get("tipo", "outro"), "other")
            }
            for a in contact_data["enderecos"]
        ]

    # Add relations
    if contact_data.get("relacionamentos"):
        tipo_map = {
            "conjuge": "spouse",
            "filho": "child",
            "pai": "parent",
            "mae": "parent",
            "irmao": "sibling",
            "amigo": "friend",
            "chefe": "manager",
            "assistente": "assistant",
            "socio": "relative",
            "parente": "relative"
        }
        person["relations"] = [
            {
                "person": r.get("nome", ""),
                "type": tipo_map.get(r.get("tipo", ""), r.get("tipo", ""))
            }
            for r in contact_data["relacionamentos"]
        ]

    # Add birthday
    if contact_data.get("aniversario"):
        try:
            # Parse date string (format: YYYY-MM-DD)
            date_str = str(contact_data["aniversario"])
            if date_str and date_str != "None":
                parts = date_str.split("-")
                if len(parts) == 3:
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    birthday = {"date": {"month": month, "day": day}}
                    # Only include year if it's not 1900 (placeholder year)
                    if year != 1900:
                        birthday["date"]["year"] = year
                    person["birthdays"] = [birthday]
        except (ValueError, IndexError):
            pass  # Invalid date format, skip

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GOOGLE_PEOPLE_API}/people:createContact",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            json=person
        )

        if response.status_code != 200:
            raise Exception(f"Failed to create contact: {response.text}")

        result = response.json()
        return result.get("resourceName", "").replace("people/", "")


async def update_google_contact(
    access_token: str,
    resource_name: str,
    contact_data: Dict,
    update_person_fields: str = "names,emailAddresses,phoneNumbers,organizations,addresses,relations,birthdays"
) -> bool:
    """Update an existing Google contact"""
    person = {}

    # Names
    if contact_data.get("nome"):
        name_parts = contact_data["nome"].split()
        person["names"] = [{
            "givenName": name_parts[0],
            "familyName": " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
        }]

    # Emails
    if contact_data.get("emails"):
        person["emailAddresses"] = [
            {"value": e["email"], "type": e.get("type", "other")}
            for e in contact_data["emails"]
        ]

    # Phones
    if contact_data.get("telefones"):
        person["phoneNumbers"] = [
            {"value": t["number"], "type": t.get("type", "mobile")}
            for t in contact_data["telefones"]
        ]

    # Organization
    if contact_data.get("empresa") or contact_data.get("cargo"):
        person["organizations"] = [{
            "name": contact_data.get("empresa", ""),
            "title": contact_data.get("cargo", "")
        }]

    # Addresses
    if contact_data.get("enderecos"):
        tipo_map = {"residencial": "home", "comercial": "work", "outro": "other"}
        person["addresses"] = [
            {
                "streetAddress": a.get("logradouro", ""),
                "city": a.get("cidade", ""),
                "region": a.get("estado", ""),
                "postalCode": a.get("cep", ""),
                "country": a.get("pais", "Brasil"),
                "type": tipo_map.get(a.get("tipo", "outro"), "other")
            }
            for a in contact_data["enderecos"]
        ]

    # Relations
    if contact_data.get("relacionamentos"):
        tipo_map = {
            "conjuge": "spouse",
            "filho": "child",
            "pai": "parent",
            "mae": "parent",
            "irmao": "sibling",
            "amigo": "friend",
            "chefe": "manager",
            "assistente": "assistant",
            "socio": "relative",
            "parente": "relative"
        }
        person["relations"] = [
            {
                "person": r.get("nome", ""),
                "type": tipo_map.get(r.get("tipo", ""), r.get("tipo", ""))
            }
            for r in contact_data["relacionamentos"]
        ]

    # Birthday
    if contact_data.get("aniversario"):
        try:
            date_str = str(contact_data["aniversario"])
            if date_str and date_str != "None":
                parts = date_str.split("-")
                if len(parts) == 3:
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    birthday = {"date": {"month": month, "day": day}}
                    if year != 1900:
                        birthday["date"]["year"] = year
                    person["birthdays"] = [birthday]
        except (ValueError, IndexError):
            pass

    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"{GOOGLE_PEOPLE_API}/people/{resource_name}:updateContact",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            params={"updatePersonFields": update_person_fields},
            json=person
        )

        return response.status_code == 200


async def delete_google_contact(access_token: str, resource_name: str) -> bool:
    """Delete a contact from Google Contacts"""
    async with httpx.AsyncClient() as client:
        response = await client.delete(
            f"{GOOGLE_PEOPLE_API}/people/{resource_name}:deleteContact",
            headers={"Authorization": f"Bearer {access_token}"}
        )

        return response.status_code == 200


async def fetch_contacts_incremental(
    access_token: str,
    sync_token: Optional[str] = None,
    page_size: int = 100
) -> Dict[str, Any]:
    """
    Fetch contacts incrementally using sync token.
    Returns: {contacts: [], next_sync_token: str, full_sync_required: bool}
    """
    contacts = []
    next_page_token = None
    next_sync_token = None

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            params = {
                "pageSize": page_size,
                "personFields": "names,emailAddresses,phoneNumbers,organizations,photos,birthdays,urls,biographies,memberships,addresses,relations",
                "sources": "READ_SOURCE_TYPE_CONTACT",
                "requestSyncToken": True  # Request a sync token in response
            }

            if sync_token and not next_page_token:
                # Use sync token for incremental sync
                params["syncToken"] = sync_token
            elif next_page_token:
                params["pageToken"] = next_page_token

            response = await client.get(
                f"{GOOGLE_PEOPLE_API}/people/me/connections",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params
            )

            if response.status_code == 401:
                raise Exception("Token expired")

            if response.status_code == 410:
                # Sync token expired, need full sync
                return {
                    "contacts": [],
                    "next_sync_token": None,
                    "full_sync_required": True
                }

            if response.status_code != 200:
                raise Exception(f"Failed to fetch contacts: {response.text}")

            data = response.json()
            connections = data.get("connections", [])
            contacts.extend(connections)

            next_page_token = data.get("nextPageToken")
            next_sync_token = data.get("nextSyncToken")

            if not next_page_token:
                break

    return {
        "contacts": contacts,
        "next_sync_token": next_sync_token,
        "full_sync_required": False
    }


async def sync_contacts_incremental(
    access_token: str,
    refresh_token: str,
    account_email: str,
    sync_token: Optional[str],
    db_connection
) -> Dict[str, Any]:
    """
    Incremental sync - only fetch and process changed contacts.
    Much faster than full sync, suitable for cron jobs.
    Returns: {imported, updated, deleted, skipped, errors, next_sync_token, full_sync_required}
    """
    stats = {
        "imported": 0,
        "updated": 0,
        "deleted": 0,
        "skipped": 0,
        "errors": 0,
        "next_sync_token": None,
        "full_sync_required": False
    }

    # If no sync token, we need a full sync first
    if not sync_token:
        stats["full_sync_required"] = True
        return stats

    try:
        result = await fetch_contacts_incremental(access_token, sync_token)
    except Exception as e:
        if "Token expired" in str(e):
            # Refresh token and retry
            try:
                new_tokens = await refresh_access_token(refresh_token)
                access_token = new_tokens["access_token"]

                # Update token in database
                cursor = db_connection.cursor()
                cursor.execute('''
                    UPDATE google_accounts
                    SET access_token = %s, token_expiry = %s
                    WHERE email = %s
                ''', (
                    access_token,
                    (datetime.now() + timedelta(seconds=new_tokens.get("expires_in", 3600))).isoformat(),
                    account_email
                ))
                db_connection.commit()

                result = await fetch_contacts_incremental(access_token, sync_token)
            except Exception as refresh_error:
                raise Exception(f"Token refresh failed: {refresh_error}")
        else:
            raise

    if result["full_sync_required"]:
        stats["full_sync_required"] = True
        return stats

    stats["next_sync_token"] = result["next_sync_token"]
    cursor = db_connection.cursor()

    for person in result["contacts"]:
        try:
            # Check if contact was deleted
            metadata = person.get("metadata", {})
            if metadata.get("deleted"):
                # Handle deletion
                resource_name = person.get("resourceName", "")
                google_contact_id = resource_name.replace("people/", "") if resource_name else None
                if google_contact_id:
                    cursor.execute(
                        "DELETE FROM contacts WHERE google_contact_id = %s",
                        (google_contact_id,)
                    )
                    if cursor.rowcount > 0:
                        stats["deleted"] += 1
                continue

            contact = parse_google_contact(person, account_email)

            if not contact["nome"]:
                stats["skipped"] += 1
                continue

            # Check if contact exists
            if contact["google_contact_id"]:
                cursor.execute(
                    "SELECT id FROM contacts WHERE google_contact_id = %s",
                    (contact["google_contact_id"],)
                )
                existing = cursor.fetchone()

                if existing:
                    # Update existing (including addresses and relations)
                    cursor.execute('''
                        UPDATE contacts SET
                            nome = %s,
                            empresa = %s,
                            cargo = %s,
                            emails = %s,
                            telefones = %s,
                            foto_url = COALESCE(%s, foto_url),
                            linkedin = COALESCE(%s, linkedin),
                            aniversario = COALESCE(%s, aniversario),
                            enderecos = COALESCE(%s, enderecos),
                            relacionamentos = COALESCE(%s, relacionamentos),
                            contexto = %s,
                            atualizado_em = CURRENT_TIMESTAMP
                        WHERE google_contact_id = %s
                    ''', (
                        contact["nome"],
                        contact["empresa"],
                        contact["cargo"],
                        json.dumps(contact["emails"]),
                        json.dumps(contact["telefones"]),
                        contact["foto_url"],
                        contact["linkedin"],
                        contact["aniversario"],
                        json.dumps(contact.get("enderecos", [])),
                        json.dumps(contact.get("relacionamentos", [])),
                        contact["contexto"],
                        contact["google_contact_id"]
                    ))
                    stats["updated"] += 1
                else:
                    # Insert new (including addresses and relations)
                    cursor.execute('''
                        INSERT INTO contacts (
                            nome, empresa, cargo, emails, telefones, foto_url,
                            linkedin, aniversario, enderecos, relacionamentos,
                            google_contact_id, contexto, origem
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        contact["nome"],
                        contact["empresa"],
                        contact["cargo"],
                        json.dumps(contact["emails"]),
                        json.dumps(contact["telefones"]),
                        contact["foto_url"],
                        contact["linkedin"],
                        contact["aniversario"],
                        json.dumps(contact.get("enderecos", [])),
                        json.dumps(contact.get("relacionamentos", [])),
                        contact["google_contact_id"],
                        contact["contexto"],
                        contact["origem"]
                    ))
                    stats["imported"] += 1

        except Exception as e:
            stats["errors"] += 1
            continue

    db_connection.commit()

    # Update sync token and last sync time
    cursor.execute('''
        UPDATE google_accounts
        SET ultima_sync = CURRENT_TIMESTAMP, sync_token = %s
        WHERE email = %s
    ''', (result["next_sync_token"], account_email))
    db_connection.commit()

    return stats
