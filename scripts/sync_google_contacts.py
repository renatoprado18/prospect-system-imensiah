#!/usr/bin/env python3
"""
Script para sincronizar contatos do Google localmente.
Evita timeouts do serverless function.

Uso:
    python scripts/sync_google_contacts.py

Requer:
    - .env com DATABASE_URL
    - Tokens OAuth salvos no banco (conectar via web primeiro)
"""
import os
import sys
import json
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Carregar .env
load_dotenv()

# Adicionar app ao path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

# Google API endpoints
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_PEOPLE_API = "https://people.googleapis.com/v1"

def get_db_connection():
    """Conectar ao banco PostgreSQL"""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise Exception("DATABASE_URL não definida. Crie um arquivo .env")
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def get_google_accounts(conn):
    """Buscar contas Google conectadas"""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM google_accounts WHERE conectado = TRUE")
    return cursor.fetchall()


async def refresh_access_token(refresh_token: str) -> dict:
    """Renovar token de acesso"""
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise Exception("GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET necessários no .env")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }
        )

        if response.status_code != 200:
            raise Exception(f"Erro ao renovar token: {response.text}")

        return response.json()


async def fetch_all_contacts(access_token: str, account_email: str) -> dict:
    """
    Buscar todos os contatos de uma conta Google.
    Retorna: {contacts: list, sync_token: str}
    """
    contacts = []
    next_page_token = None
    next_sync_token = None
    page = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        while True:
            page += 1
            params = {
                "pageSize": 1000,
                "personFields": "names,emailAddresses,phoneNumbers,organizations,photos,birthdays,urls,biographies,memberships",
                "sources": "READ_SOURCE_TYPE_CONTACT",
                "requestSyncToken": True  # Request sync token for incremental sync later
            }

            if next_page_token:
                params["pageToken"] = next_page_token

            print(f"  [{account_email}] Buscando página {page}... ({len(contacts)} contatos até agora)")

            response = await client.get(
                f"{GOOGLE_PEOPLE_API}/people/me/connections",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params
            )

            if response.status_code == 401:
                raise Exception("Token expirado")

            if response.status_code != 200:
                raise Exception(f"Erro ao buscar contatos: {response.text}")

            data = response.json()
            connections = data.get("connections", [])
            contacts.extend(connections)

            next_page_token = data.get("nextPageToken")
            next_sync_token = data.get("nextSyncToken")

            if not next_page_token:
                break

    print(f"  [{account_email}] Total: {len(contacts)} contatos")
    return {"contacts": contacts, "sync_token": next_sync_token}


def parse_google_contact(person: dict, account_email: str) -> dict:
    """Converter contato Google para nosso formato"""
    # Nome
    names = person.get("names", [])
    name = ""
    if names:
        name_obj = names[0]
        name = name_obj.get("displayName", "")
        if not name:
            given = name_obj.get("givenName", "")
            family = name_obj.get("familyName", "")
            name = f"{given} {family}".strip()

    # Emails
    emails = []
    for email_obj in person.get("emailAddresses", []):
        emails.append({
            "email": email_obj.get("value", "").lower(),
            "type": email_obj.get("type", "other").lower(),
            "primary": email_obj.get("metadata", {}).get("primary", False)
        })

    # Telefones
    telefones = []
    for phone_obj in person.get("phoneNumbers", []):
        phone_type = phone_obj.get("type", "other").lower()
        telefones.append({
            "number": phone_obj.get("value", ""),
            "type": phone_type,
            "whatsapp": phone_type in ["mobile", "cell"]
        })

    # Organização
    empresa = ""
    cargo = ""
    orgs = person.get("organizations", [])
    if orgs:
        org = orgs[0]
        empresa = org.get("name", "")
        cargo = org.get("title", "")

    # Foto
    foto_url = None
    photos = person.get("photos", [])
    if photos:
        foto_url = photos[0].get("url", "")

    # Aniversário
    aniversario = None
    birthdays = person.get("birthdays", [])
    if birthdays:
        bday = birthdays[0].get("date", {})
        if bday.get("year") and bday.get("month") and bday.get("day"):
            aniversario = f"{bday['year']}-{bday['month']:02d}-{bday['day']:02d}"
        elif bday.get("month") and bday.get("day"):
            aniversario = f"1900-{bday['month']:02d}-{bday['day']:02d}"

    # LinkedIn
    linkedin = None
    for url_obj in person.get("urls", []):
        url = url_obj.get("value", "")
        if "linkedin.com" in url:
            linkedin = url
            break

    # Google Contact ID
    resource_name = person.get("resourceName", "")
    google_contact_id = resource_name.replace("people/", "") if resource_name else None

    # Contexto baseado na conta
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
        "google_contact_id": google_contact_id,
        "contexto": contexto,
        "origem": f"google_{account_email}"
    }


def sync_contacts_to_db(conn, contacts: list, account_email: str) -> dict:
    """Sincronizar contatos com o banco de dados"""
    stats = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}
    cursor = conn.cursor()

    for i, person in enumerate(contacts):
        if i % 100 == 0:
            print(f"  Processando {i}/{len(contacts)}...")
            conn.commit()  # Commit a cada 100

        try:
            contact = parse_google_contact(person, account_email)

            if not contact["nome"]:
                stats["skipped"] += 1
                continue

            # Verificar se existe por google_contact_id
            if contact["google_contact_id"]:
                cursor.execute(
                    "SELECT id FROM contacts WHERE google_contact_id = %s",
                    (contact["google_contact_id"],)
                )
                existing = cursor.fetchone()

                if existing:
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
                        contact["contexto"],
                        contact["google_contact_id"]
                    ))
                    stats["updated"] += 1
                    continue

            # Verificar por email
            if contact["emails"]:
                primary_email = contact["emails"][0]["email"]
                cursor.execute(
                    "SELECT id FROM contacts WHERE emails @> %s::jsonb",
                    (json.dumps([{"email": primary_email}]),)
                )
                existing = cursor.fetchone()

                if existing:
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

            # Inserir novo
            cursor.execute('''
                INSERT INTO contacts (
                    nome, empresa, cargo, emails, telefones, foto_url,
                    linkedin, aniversario, google_contact_id, contexto, origem
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                contact["nome"],
                contact["empresa"],
                contact["cargo"],
                json.dumps(contact["emails"]),
                json.dumps(contact["telefones"]),
                contact["foto_url"],
                contact["linkedin"],
                contact["aniversario"],
                contact["google_contact_id"],
                contact["contexto"],
                contact["origem"]
            ))
            stats["imported"] += 1

        except Exception as e:
            stats["errors"] += 1
            if stats["errors"] < 10:
                print(f"    Erro: {e}")

    conn.commit()

    # Atualizar ultima_sync
    cursor.execute('''
        UPDATE google_accounts SET ultima_sync = CURRENT_TIMESTAMP
        WHERE email = %s
    ''', (account_email,))
    conn.commit()

    return stats


async def sync_account(conn, account: dict) -> dict:
    """Sincronizar uma conta Google"""
    email = account["email"]
    access_token = account["access_token"]
    refresh_token = account["refresh_token"]

    print(f"\n{'='*60}")
    print(f"Sincronizando: {email}")
    print(f"{'='*60}")

    # Verificar se token precisa ser renovado
    token_expiry = account.get("token_expiry")
    if token_expiry:
        if isinstance(token_expiry, str):
            token_expiry = datetime.fromisoformat(token_expiry.replace("Z", "+00:00"))
        if token_expiry < datetime.now(token_expiry.tzinfo if token_expiry.tzinfo else None):
            print("  Token expirado, renovando...")
            try:
                new_tokens = await refresh_access_token(refresh_token)
                access_token = new_tokens["access_token"]

                # Atualizar no banco
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE google_accounts
                    SET access_token = %s, token_expiry = %s
                    WHERE email = %s
                ''', (
                    access_token,
                    (datetime.now() + timedelta(seconds=new_tokens.get("expires_in", 3600))).isoformat(),
                    email
                ))
                conn.commit()
                print("  Token renovado!")
            except Exception as e:
                print(f"  ERRO ao renovar token: {e}")
                return {"error": str(e)}

    # Buscar contatos
    try:
        result = await fetch_all_contacts(access_token, email)
        contacts = result["contacts"]
        sync_token = result["sync_token"]
    except Exception as e:
        if "Token expirado" in str(e):
            print("  Token expirado, tentando renovar...")
            try:
                new_tokens = await refresh_access_token(refresh_token)
                access_token = new_tokens["access_token"]

                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE google_accounts
                    SET access_token = %s, token_expiry = %s
                    WHERE email = %s
                ''', (
                    access_token,
                    (datetime.now() + timedelta(seconds=new_tokens.get("expires_in", 3600))).isoformat(),
                    email
                ))
                conn.commit()

                result = await fetch_all_contacts(access_token, email)
                contacts = result["contacts"]
                sync_token = result["sync_token"]
            except Exception as refresh_error:
                print(f"  ERRO: {refresh_error}")
                return {"error": str(refresh_error)}
        else:
            print(f"  ERRO ao buscar contatos: {e}")
            return {"error": str(e)}

    # Sincronizar com banco
    print(f"\n  Sincronizando {len(contacts)} contatos com o banco...")
    stats = sync_contacts_to_db(conn, contacts, email)

    # Salvar sync_token para incremental sync futuro
    if sync_token:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE google_accounts SET sync_token = %s WHERE email = %s
        ''', (sync_token, email))
        conn.commit()
        print(f"  Sync token salvo para sincronização incremental")

    print(f"\n  Resultado:")
    print(f"    - Importados: {stats['imported']}")
    print(f"    - Atualizados: {stats['updated']}")
    print(f"    - Pulados: {stats['skipped']}")
    print(f"    - Erros: {stats['errors']}")

    return stats


async def main():
    print("="*60)
    print("SYNC GOOGLE CONTACTS - RAP")
    print("="*60)

    # Conectar ao banco
    print("\nConectando ao banco de dados...")
    conn = get_db_connection()
    print("Conectado!")

    # Buscar contas
    accounts = get_google_accounts(conn)
    if not accounts:
        print("\nNenhuma conta Google conectada.")
        print("Conecte uma conta em: https://prospects.almeida-prado.com/rap/settings")
        return

    print(f"\nContas encontradas: {len(accounts)}")
    for acc in accounts:
        print(f"  - {acc['email']} ({acc['tipo']})")

    # Sincronizar cada conta
    total_stats = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    for account in accounts:
        stats = await sync_account(conn, account)
        if "error" not in stats:
            for key in total_stats:
                total_stats[key] += stats.get(key, 0)

    # Resultado final
    print("\n" + "="*60)
    print("RESULTADO FINAL")
    print("="*60)
    print(f"  Importados: {total_stats['imported']}")
    print(f"  Atualizados: {total_stats['updated']}")
    print(f"  Pulados: {total_stats['skipped']}")
    print(f"  Erros: {total_stats['errors']}")

    # Estatísticas do banco
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as total FROM contacts")
    total = cursor.fetchone()["total"]
    cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE contexto = 'personal'")
    personal = cursor.fetchone()["total"]
    cursor.execute("SELECT COUNT(*) as total FROM contacts WHERE contexto = 'professional'")
    professional = cursor.fetchone()["total"]

    print(f"\n  Total no banco: {total}")
    print(f"    - Pessoais: {personal}")
    print(f"    - Profissionais: {professional}")

    conn.close()
    print("\nConcluído!")


if __name__ == "__main__":
    asyncio.run(main())
