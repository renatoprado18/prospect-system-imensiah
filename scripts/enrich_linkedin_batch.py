#!/usr/bin/env python3
"""
Enriquecimento de LinkedIn em Batch

Busca dados do LinkedIn para contatos que tem URL mas faltam dados.
Usa Proxycurl API se disponivel, senao marca para enriquecimento manual.
"""
import os
import sys
import time
import httpx
from datetime import datetime
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                value = value.strip('"').strip("'")
                os.environ.setdefault(key, value)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from database import get_db

PROXYCURL_API_KEY = os.getenv("PROXYCURL_API_KEY", "")
PROXYCURL_URL = "https://nubela.co/proxycurl/api/v2/linkedin"


def fetch_linkedin_profile(linkedin_url: str) -> dict:
    """Busca dados do perfil via Proxycurl"""
    if not PROXYCURL_API_KEY:
        return {"error": "PROXYCURL_API_KEY not configured"}

    try:
        response = httpx.get(
            PROXYCURL_URL,
            params={"url": linkedin_url, "skills": "skip", "inferred_salary": "skip"},
            headers={"Authorization": f"Bearer {PROXYCURL_API_KEY}"},
            timeout=30.0
        )
        if response.status_code == 200:
            return response.json()
        else:
            return {"error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def enrich_batch(limit: int = 50, circulo_max: int = 3):
    """
    Enriquece contatos que tem LinkedIn URL mas faltam dados.

    Args:
        limit: Maximo de contatos por execucao
        circulo_max: Processar apenas circulos ate este valor
    """
    print("=" * 60, flush=True)
    print("ENRIQUECIMENTO LINKEDIN - BATCH", flush=True)
    print("=" * 60, flush=True)
    print(f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"Proxycurl API: {'Configurado' if PROXYCURL_API_KEY else 'NAO CONFIGURADO'}", flush=True)
    print(flush=True)

    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar contatos que precisam de enriquecimento
        cursor.execute("""
            SELECT id, nome, linkedin, linkedin_headline, empresa, cargo
            FROM contacts
            WHERE linkedin IS NOT NULL
            AND linkedin != ''
            AND COALESCE(circulo, 5) <= %s
            AND (
                linkedin_headline IS NULL
                OR empresa IS NULL
                OR cargo IS NULL
                OR ultimo_enriquecimento IS NULL
                OR ultimo_enriquecimento < NOW() - INTERVAL '90 days'
            )
            ORDER BY circulo ASC, ultimo_contato DESC NULLS LAST
            LIMIT %s
        """, (circulo_max, limit))

        contacts = cursor.fetchall()
        print(f"Contatos para enriquecer: {len(contacts)}", flush=True)
        print(flush=True)

        stats = {"success": 0, "skipped": 0, "error": 0}

        for contact in contacts:
            contact_id = contact["id"]
            nome = contact["nome"]
            linkedin_url = contact["linkedin"]

            print(f"Processando: {nome}", flush=True)
            print(f"  LinkedIn: {linkedin_url}", flush=True)

            if not PROXYCURL_API_KEY:
                # Sem API, apenas marcar como pendente
                cursor.execute("""
                    UPDATE contacts
                    SET enriquecimento_status = 'pending_manual',
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (contact_id,))
                stats["skipped"] += 1
                print(f"  -> Marcado para enriquecimento manual", flush=True)
                continue

            # Buscar dados via API
            data = fetch_linkedin_profile(linkedin_url)

            if "error" in data:
                cursor.execute("""
                    UPDATE contacts
                    SET enriquecimento_status = %s,
                        atualizado_em = NOW()
                    WHERE id = %s
                """, (f"error: {data['error']}", contact_id))
                stats["error"] += 1
                print(f"  -> Erro: {data['error']}", flush=True)
                continue

            # Atualizar contato com dados do LinkedIn
            headline = data.get("headline") or data.get("occupation")
            company = data.get("experiences", [{}])[0].get("company") if data.get("experiences") else None
            position = data.get("experiences", [{}])[0].get("title") if data.get("experiences") else None
            location = data.get("city") or data.get("country_full_name")
            photo = data.get("profile_pic_url")
            summary = data.get("summary")

            updates = []
            params = []

            if headline and not contact["linkedin_headline"]:
                updates.append("linkedin_headline = %s")
                params.append(headline)

            if company and not contact["empresa"]:
                updates.append("empresa = %s")
                params.append(company)

            if position and not contact["cargo"]:
                updates.append("cargo = %s")
                params.append(position)

            if photo:
                updates.append("foto_url = COALESCE(foto_url, %s)")
                params.append(photo)

            if summary:
                updates.append("resumo_ai = COALESCE(resumo_ai, %s)")
                params.append(summary)

            updates.append("ultimo_enriquecimento = NOW()")
            updates.append("enriquecimento_status = 'success'")
            updates.append("atualizado_em = NOW()")

            if updates:
                query = f"UPDATE contacts SET {', '.join(updates)} WHERE id = %s"
                params.append(contact_id)
                cursor.execute(query, params)

            stats["success"] += 1
            print(f"  -> Enriquecido: {headline or 'N/A'}", flush=True)

            # Rate limiting
            time.sleep(1)

        conn.commit()

        print(flush=True)
        print("=" * 60, flush=True)
        print("RESULTADO:", flush=True)
        print(f"  Sucesso: {stats['success']}", flush=True)
        print(f"  Pendente manual: {stats['skipped']}", flush=True)
        print(f"  Erros: {stats['error']}", flush=True)
        print("=" * 60, flush=True)

        return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--circulo", type=int, default=3)
    args = parser.parse_args()

    enrich_batch(limit=args.limit, circulo_max=args.circulo)
