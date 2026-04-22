"""
Google Drive Integration
Manages folders, documents, and file uploads linked to projects/contacts
"""
import os
import json
import secrets
import httpx
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from urllib.parse import urlencode

# Google Drive API endpoint
GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


async def get_valid_token(db_conn, account_type: str = 'professional') -> Optional[str]:
    """Get a valid access token, refreshing if needed

    Args:
        db_conn: Database connection
        account_type: 'professional' or 'personal'
    """
    from integrations.google_contacts import refresh_access_token

    cursor = db_conn.cursor()
    cursor.execute("""
        SELECT access_token, refresh_token, token_expiry
        FROM google_accounts
        WHERE tipo = %s
        LIMIT 1
    """, (account_type,))
    row = cursor.fetchone()

    if not row:
        return None

    access_token = row['access_token']
    refresh_token = row['refresh_token']
    expires_at = row['token_expiry']

    # Check if token is expired
    if expires_at and datetime.now() >= expires_at:
        try:
            tokens = await refresh_access_token(refresh_token)
            access_token = tokens['access_token']

            # Update in database
            cursor.execute("""
                UPDATE google_accounts
                SET access_token = %s,
                    token_expiry = NOW() + INTERVAL '1 hour'
                WHERE tipo = %s
            """, (access_token, account_type))
            db_conn.commit()
        except Exception as e:
            print(f"Error refreshing token: {e}")
            return None

    return access_token


async def list_folders(access_token: str, parent_id: str = None) -> List[Dict]:
    """
    List folders in Google Drive
    If parent_id is provided, list folders within that folder
    If parent_id is None, list root-level folders
    """
    query = "mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    else:
        # Root-level folders
        query += " and 'root' in parents"

    params = {
        "q": query,
        "fields": "files(id,name,parents,createdTime,modifiedTime)",
        "orderBy": "name",
        "pageSize": 100
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{GOOGLE_DRIVE_API}/files",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"}
        )

        if response.status_code != 200:
            raise Exception(f"Drive API error: {response.text}")

        data = response.json()
        return data.get("files", [])


async def get_folder_contents(access_token: str, folder_id: str) -> List[Dict]:
    """
    Get all files and folders within a specific folder
    """
    query = f"'{folder_id}' in parents and trashed=false"

    params = {
        "q": query,
        "fields": "files(id,name,mimeType,size,createdTime,modifiedTime,webViewLink,iconLink,thumbnailLink)",
        "orderBy": "folder,name",
        "pageSize": 200
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{GOOGLE_DRIVE_API}/files",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"}
        )

        if response.status_code != 200:
            raise Exception(f"Drive API error: {response.text}")

        data = response.json()
        return data.get("files", [])


async def get_file_metadata(access_token: str, file_id: str) -> Dict:
    """Get metadata for a specific file"""
    params = {
        "fields": "id,name,mimeType,size,createdTime,modifiedTime,webViewLink,iconLink,thumbnailLink,parents"
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{GOOGLE_DRIVE_API}/files/{file_id}",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"}
        )

        if response.status_code != 200:
            raise Exception(f"Drive API error: {response.text}")

        return response.json()


async def create_folder(access_token: str, name: str, parent_id: str = None) -> Dict:
    """Create a new folder in Google Drive"""
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }

    if parent_id:
        metadata["parents"] = [parent_id]

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GOOGLE_DRIVE_API}/files",
            json=metadata,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
        )

        if response.status_code not in [200, 201]:
            raise Exception(f"Drive API error: {response.text}")

        return response.json()


async def upload_file(
    access_token: str,
    file_content: bytes,
    filename: str,
    mime_type: str,
    folder_id: str = None
) -> Dict:
    """
    Upload a file to Google Drive
    Uses resumable upload for reliability
    """
    # File metadata
    metadata = {"name": filename}
    if folder_id:
        metadata["parents"] = [folder_id]

    # Create upload session
    async with httpx.AsyncClient() as client:
        # Step 1: Initiate resumable upload
        init_response = await client.post(
            f"{GOOGLE_UPLOAD_API}/files?uploadType=resumable",
            json=metadata,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(len(file_content))
            }
        )

        if init_response.status_code != 200:
            raise Exception(f"Upload init failed: {init_response.text}")

        upload_url = init_response.headers.get("Location")

        # Step 2: Upload file content
        upload_response = await client.put(
            upload_url,
            content=file_content,
            headers={
                "Content-Type": mime_type,
                "Content-Length": str(len(file_content))
            }
        )

        if upload_response.status_code not in [200, 201]:
            raise Exception(f"Upload failed: {upload_response.text}")

        return upload_response.json()


async def search_files(access_token: str, query: str, folder_id: str = None) -> List[Dict]:
    """
    Search for files by name or content
    """
    search_query = f"fullText contains '{query}' and trashed=false"
    if folder_id:
        search_query += f" and '{folder_id}' in parents"

    params = {
        "q": search_query,
        "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink,parents)",
        "pageSize": 50
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{GOOGLE_DRIVE_API}/files",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"}
        )

        if response.status_code != 200:
            raise Exception(f"Drive API error: {response.text}")

        data = response.json()
        return data.get("files", [])


async def get_folder_path(access_token: str, folder_id: str) -> str:
    """Get the full path of a folder (for display)"""
    path_parts = []
    current_id = folder_id

    async with httpx.AsyncClient() as client:
        while current_id:
            response = await client.get(
                f"{GOOGLE_DRIVE_API}/files/{current_id}",
                params={"fields": "id,name,parents"},
                headers={"Authorization": f"Bearer {access_token}"}
            )

            if response.status_code != 200:
                break

            data = response.json()
            path_parts.insert(0, data.get("name", ""))

            parents = data.get("parents", [])
            current_id = parents[0] if parents else None

    return "/" + "/".join(path_parts)


def index_document_to_db(
    db_conn,
    nome: str,
    google_drive_id: str,
    mime_type: str,
    web_view_link: str,
    tamanho_bytes: int = None,
    pasta_id: str = None
) -> int:
    """
    Index a document in the database
    Returns the document ID
    """
    cursor = db_conn.cursor()

    # Check if already exists
    cursor.execute(
        "SELECT id FROM documentos WHERE google_drive_id = %s",
        (google_drive_id,)
    )
    existing = cursor.fetchone()

    if existing:
        # Update existing
        cursor.execute("""
            UPDATE documentos
            SET nome = %s, mime_type = %s, google_drive_url = %s,
                tamanho_bytes = %s, atualizado_em = NOW()
            WHERE google_drive_id = %s
            RETURNING id
        """, (nome, mime_type, web_view_link, tamanho_bytes, google_drive_id))
    else:
        # Insert new
        cursor.execute("""
            INSERT INTO documentos
            (nome, google_drive_id, google_drive_url, mime_type, tamanho_bytes, pasta_origem_id, indexado_em)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (nome, google_drive_id, web_view_link, mime_type, tamanho_bytes, pasta_id))

    doc_id = cursor.fetchone()['id']
    db_conn.commit()

    return doc_id


def link_document_to_entity(
    db_conn,
    documento_id: int,
    entidade_tipo: str,  # 'projeto', 'contato', 'reuniao', 'tarefa'
    entidade_id: int
):
    """
    Create a link between a document and an entity
    """
    cursor = db_conn.cursor()

    # Use upsert to avoid duplicates
    cursor.execute("""
        INSERT INTO documento_links (documento_id, entidade_tipo, entidade_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (documento_id, entidade_tipo, entidade_id) DO NOTHING
    """, (documento_id, entidade_tipo, entidade_id))

    db_conn.commit()


def get_documents_for_entity(
    db_conn,
    entidade_tipo: str,
    entidade_id: int
) -> List[Dict]:
    """
    Get all documents linked to a specific entity
    """
    cursor = db_conn.cursor()

    cursor.execute("""
        SELECT d.*, dl.entidade_tipo, dl.entidade_id
        FROM documentos d
        JOIN documento_links dl ON d.id = dl.documento_id
        WHERE dl.entidade_tipo = %s AND dl.entidade_id = %s
        ORDER BY d.indexado_em DESC
    """, (entidade_tipo, entidade_id))

    rows = cursor.fetchall()
    return [dict(row) for row in rows]


async def index_folder_documents(
    db_conn,
    access_token: str,
    folder_id: str,
    entidade_tipo: str = None,
    entidade_id: int = None,
    recursive: bool = True,
    _path_prefix: str = ""
) -> int:
    """
    Index all documents in a folder and optionally link them to an entity.
    If recursive=True, also indexes documents in subfolders.
    Returns count of indexed documents.
    """
    files = await get_folder_contents(access_token, folder_id)
    count = 0

    for file in files:
        mime_type = file.get('mimeType', '')

        # Handle subfolders recursively
        if mime_type == 'application/vnd.google-apps.folder':
            if recursive:
                subfolder_path = f"{_path_prefix}{file['name']}/"
                subcount = await index_folder_documents(
                    db_conn,
                    access_token,
                    file['id'],
                    entidade_tipo,
                    entidade_id,
                    recursive=True,
                    _path_prefix=subfolder_path
                )
                count += subcount
            continue

        # Index document with path prefix for subfolder documents
        doc_name = file['name']
        if _path_prefix:
            doc_name = f"{_path_prefix}{file['name']}"

        doc_id = index_document_to_db(
            db_conn,
            google_drive_id=file['id'],
            nome=doc_name,
            mime_type=mime_type,
            web_view_link=file.get('webViewLink', ''),
            tamanho_bytes=int(file.get('size', 0)) if file.get('size') else None,
            pasta_id=folder_id
        )

        if entidade_tipo and entidade_id:
            link_document_to_entity(db_conn, doc_id, entidade_tipo, entidade_id)

        count += 1

    return count


def fix_document_column_shift(db_conn, doc_id: int = 87):
    """
    Fix document with shifted columns.
    Document 87 has: nome=drive_id, google_drive_id=nome, google_drive_url=mime_type, mime_type=url
    """
    cursor = db_conn.cursor()
    cursor.execute("SELECT nome, google_drive_id, google_drive_url, mime_type FROM documentos WHERE id = %s", (doc_id,))
    row = cursor.fetchone()
    if not row:
        return False

    # The values are shifted: nome has drive_id, google_drive_id has nome, etc.
    actual_drive_id = row['nome']
    actual_nome = row['google_drive_id']
    actual_mime_type = row['google_drive_url']
    actual_url = row['mime_type']

    cursor.execute("""
        UPDATE documentos
        SET nome = %s, google_drive_id = %s, google_drive_url = %s, mime_type = %s, atualizado_em = NOW()
        WHERE id = %s
    """, (actual_nome, actual_drive_id, actual_url, actual_mime_type, doc_id))
    db_conn.commit()
    return True


async def watch_folder(access_token: str, folder_id: str, webhook_url: str, channel_token: str) -> Dict:
    """
    Set up push notifications for changes in a Drive folder using files.watch.
    Google will POST to webhook_url when files change.
    Channel expires in ~7 days (Google maximum).
    """
    channel_id = f"drive-watch-{folder_id}-{secrets.token_hex(8)}"
    expiration = int((datetime.utcnow() + timedelta(days=7)).timestamp() * 1000)

    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        "token": channel_token,
        "expiration": expiration,
        "params": {
            "ttl": str(7 * 24 * 3600)  # 7 days in seconds
        }
    }

    async with httpx.AsyncClient() as client:
        # Watch changes on the folder itself
        response = await client.post(
            f"{GOOGLE_DRIVE_API}/files/{folder_id}/watch",
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
        )

        if response.status_code not in [200, 201]:
            raise Exception(f"Drive watch API error: {response.text}")

        return response.json()


async def stop_watch_channel(access_token: str, channel_id: str, resource_id: str) -> bool:
    """Stop a previously created watch channel"""
    body = {
        "id": channel_id,
        "resourceId": resource_id
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GOOGLE_DRIVE_API}/channels/stop",
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
        )

        return response.status_code == 204
