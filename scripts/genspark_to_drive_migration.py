"""
Migração Genspark AI Drive → Google Drive (14/06/26)

Renato baixou ZIP do Genspark (519MB) com 2 pastas:
1. Hindiana_Proposta (24KB, 2 .md) → cria projeto INTEL "Hindiana", vincula
   Alfredo Villela (contact_id 157), upload no Drive profissional.
2. ERê (514MB, ~muitos arquivos) → upload Drive pessoal em A-Personal/EAS/ERê
   preservando subpastas (Cartoons, Audios, Stickers, Projeto_Livro, Fotos,
   SauVon, Emma + raiz).

Uso:
    python3 scripts/genspark_to_drive_migration.py hindiana
    python3 scripts/genspark_to_drive_migration.py ere
    python3 scripts/genspark_to_drive_migration.py all
"""
from __future__ import annotations

import sys
import os
import asyncio
import mimetypes
import json
from pathlib import Path
from datetime import datetime

# Path setup pra importar do INTEL
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from database import get_db  # noqa
from integrations import google_drive as gdrive  # noqa
from integrations.google_contacts import refresh_access_token  # noqa


SOURCE_ROOT = Path("/tmp/genspark_archive")
ALFREDO_CONTACT_ID = 157


async def get_token_for_account(account_type: str) -> str:
    """Get fresh token for 'professional' or 'personal' account."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, refresh_token FROM google_accounts WHERE tipo = %s AND conectado = TRUE LIMIT 1",
            (account_type,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Nenhuma conta google {account_type} conectada")
        tokens = await refresh_access_token(row["refresh_token"])
        access_token = tokens["access_token"]
        cur.execute(
            "UPDATE google_accounts SET access_token=%s, token_expiry=NOW()+INTERVAL '1 hour' WHERE id=%s",
            (access_token, row["id"]),
        )
        conn.commit()
        return access_token


async def find_or_create_folder(token: str, name: str, parent_id: str | None) -> str:
    """Acha folder existente por nome+parent ou cria."""
    folders = await gdrive.list_folders(token, parent_id=parent_id)
    for f in folders:
        if f["name"] == name:
            print(f"  · existing folder: {name} -> {f['id']}")
            return f["id"]
    created = await gdrive.create_folder(token, name=name, parent_id=parent_id)
    print(f"  + created folder:  {name} -> {created['id']}")
    return created["id"]


async def file_exists_in_folder(token: str, name: str, folder_id: str) -> bool:
    """Check if file with this name already exists in folder."""
    import httpx
    safe_name = name.replace("'", "\\'")
    q = f"name='{safe_name}' and '{folder_id}' in parents and trashed=false"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{gdrive.GOOGLE_DRIVE_API}/files",
            params={"q": q, "fields": "files(id,name)", "pageSize": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            return False
        return len(resp.json().get("files", [])) > 0


class TokenRotator:
    """Holds a token + refresh on demand. Used pra evitar expiracao mid-upload."""

    def __init__(self, account_type: str):
        self.account_type = account_type
        self.token: str | None = None
        self.refresh_count = 0

    async def get(self) -> str:
        if not self.token:
            await self.rotate()
        return self.token  # type: ignore

    async def rotate(self):
        self.token = await get_token_for_account(self.account_type)
        self.refresh_count += 1
        print(f"  ↻ token refreshed (#{self.refresh_count})", flush=True)


UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"


async def _do_upload_raw(token: str, content: bytes, filename: str, mime: str, folder_id: str) -> tuple[int, str]:
    """Upload inline com httpx, retorna (http_status, body_or_id).
    Status 200/201 = sucesso, body = file id JSON.
    Outros = erro, body = mensagem detalhada.
    """
    import httpx

    metadata: dict = {"name": filename, "parents": [folder_id]}
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=180.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        init = await client.post(
            UPLOAD_URL,
            json=metadata,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(len(content)),
            },
        )
        if init.status_code != 200:
            return init.status_code, f"init: {init.text[:400]}"
        upload_url = init.headers.get("Location")
        if not upload_url:
            return -1, "no Location header in init response"

        put = await client.put(
            upload_url,
            content=content,
            headers={"Content-Type": mime},
        )
        if put.status_code not in (200, 201):
            return put.status_code, f"put: {put.text[:400]}"
        return put.status_code, put.text[:200]


async def upload_file_with_retry(
    rotator: "TokenRotator",
    local_path: Path,
    folder_id: str,
    max_retries: int = 4,
) -> dict:
    """Upload com retry, refresh token em 401, backoff em 429/5xx."""
    token = await rotator.get()
    # Dedup
    try:
        if await file_exists_in_folder(token, local_path.name, folder_id):
            return {"skipped": True, "reason": "already_exists"}
    except Exception:
        pass  # se check falha, segue pro upload

    content = local_path.read_bytes()
    mime, _ = mimetypes.guess_type(local_path.name)
    if not mime:
        mime = "application/octet-stream"

    last_err = "no_attempt"
    for attempt in range(max_retries):
        token = await rotator.get()
        try:
            status, body = await _do_upload_raw(token, content, local_path.name, mime, folder_id)
        except Exception as e:
            last_err = f"exc {type(e).__name__}: {e}"
            await asyncio.sleep(1)
            continue

        if status in (200, 201):
            return {"ok": True, "body": body}

        last_err = f"HTTP {status} {body[:200]}"
        if status == 401:
            await rotator.rotate()
            continue
        if status in (429, 500, 502, 503, 504):
            await asyncio.sleep(2 ** attempt)
            continue
        # Erro 4xx nao-401 = nao retryavel
        if 400 <= status < 500:
            break
        await asyncio.sleep(1)

    raise RuntimeError(f"upload falhou apos {max_retries} tries — {last_err}")


async def upload_dir_recursive(
    rotator: "TokenRotator",
    local_dir: Path,
    drive_folder_id: str,
    indent: int = 0,
    stats: dict | None = None,
):
    """Upload tudo dentro de local_dir pra drive_folder_id, recursive."""
    if stats is None:
        stats = {"ok": 0, "skip": 0, "fail": 0, "failed_files": []}
    pad = "  " * indent
    MAX_SIZE_MB = 80  # uploads in-memory ficam pesados acima disso
    files = sorted(p for p in local_dir.iterdir() if p.is_file())
    dirs = sorted(p for p in local_dir.iterdir() if p.is_dir())
    for f in files:
        # Skip .zip redundantes (conteudo descompactado existe ao lado)
        if f.suffix.lower() == ".zip":
            size_mb = f.stat().st_size / 1024 / 1024
            print(f"{pad}— skip .zip {f.name} ({size_mb:.0f}MB) — redundante", flush=True)
            stats["skip"] += 1
            continue
        size_mb = f.stat().st_size / 1024 / 1024
        if size_mb > MAX_SIZE_MB:
            print(f"{pad}— skip {f.name} ({size_mb:.0f}MB > {MAX_SIZE_MB}MB cap)", flush=True)
            stats["skip"] += 1
            stats.setdefault("skipped_large", []).append(str(f))
            continue
        try:
            size_kb = f.stat().st_size / 1024
            result = await upload_file_with_retry(rotator, f, drive_folder_id)
            if result.get("skipped"):
                stats["skip"] += 1
                tag = "skip"
            else:
                stats["ok"] += 1
                tag = "↑"
            print(f"{pad}{tag} {f.name} ({size_kb:.1f}KB)", flush=True)
        except Exception as e:
            stats["fail"] += 1
            stats["failed_files"].append(str(f))
            print(f"{pad}✗ FAILED {f.name}: {e}", flush=True)
    for d in dirs:
        try:
            token = await rotator.get()
            sub_id = await find_or_create_folder(token, d.name, parent_id=drive_folder_id)
            await upload_dir_recursive(rotator, d, sub_id, indent=indent + 1, stats=stats)
        except Exception as e:
            print(f"{pad}✗ FOLDER FAILED {d.name}: {e}", flush=True)
    return stats


# ============== Hindiana ==============

async def run_hindiana():
    print("\n=== Hindiana — Proposta de Advisor ===")
    src = SOURCE_ROOT / "Hindiana_Proposta"
    if not src.exists():
        print(f"✗ Pasta nao encontrada: {src}")
        return None

    token = await get_token_for_account("professional")
    # Cria folder no root Drive profissional
    folder_id = await find_or_create_folder(token, "Hindiana — Proposta de Advisor", parent_id=None)

    # Upload arquivos
    for f in sorted(src.glob("*")):
        if f.is_file():
            print(f"↑ {f.name}")
            await upload_file(token, f, folder_id)

    # Cria projeto INTEL
    with get_db() as conn:
        cur = conn.cursor()
        # Idempotency
        cur.execute("SELECT id FROM projects WHERE LOWER(nome) LIKE %s LIMIT 1", ("hindiana%",))
        existing = cur.fetchone()
        if existing:
            project_id = existing["id"]
            cur.execute(
                "UPDATE projects SET google_drive_folder_id=%s, owner_contact_id=%s, atualizado_em=NOW() WHERE id=%s",
                (folder_id, ALFREDO_CONTACT_ID, project_id),
            )
            print(f"✓ Projeto Hindiana ja existia (#{project_id}), atualizado.")
        else:
            cur.execute(
                """
                INSERT INTO projects (
                    nome, descricao, tipo, status, prioridade,
                    owner_contact_id, google_drive_folder_id, tags
                ) VALUES (%s, %s, 'negocio', 'ativo', 3, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    "Hindiana — Proposta de Advisor",
                    "Proposta de advisory pra Hindiana. Material origem: Genspark AI Drive (export 14/06/26). Owner: Alfredo Villela. Conteúdo no Drive (folder vinculado).",
                    ALFREDO_CONTACT_ID,
                    folder_id,
                    json.dumps(["advisory", "imported_from_genspark"]),
                ),
            )
            project_id = cur.fetchone()["id"]
            print(f"+ Projeto Hindiana criado: #{project_id}")

        # Vincula Alfredo como project_member tambem (defensivo)
        cur.execute(
            """
            INSERT INTO project_members (project_id, contact_id, papel)
            VALUES (%s, %s, 'owner')
            ON CONFLICT DO NOTHING
            """,
            (project_id, ALFREDO_CONTACT_ID),
        )
        conn.commit()

    print(f"✓ Hindiana feito. project_id={project_id}, drive_folder={folder_id}")
    return {"project_id": project_id, "drive_folder_id": folder_id}


# ============== ERê ==============

async def run_ere():
    print("\n=== ERê (Pessoal) ===")
    src = SOURCE_ROOT / "ERê"
    if not src.exists():
        print(f"✗ Pasta nao encontrada: {src}")
        return None

    rotator = TokenRotator("personal")
    token = await rotator.get()

    # Estrutura A-Personal/EAS/ERê
    a_personal_id = await find_or_create_folder(token, "A-Personal", parent_id=None)
    eas_id = await find_or_create_folder(token, "EAS", parent_id=a_personal_id)
    ere_id = await find_or_create_folder(token, "ERê", parent_id=eas_id)

    # Upload recursive
    start = datetime.now()
    stats = await upload_dir_recursive(rotator, src, ere_id, indent=0)
    elapsed = (datetime.now() - start).total_seconds()

    print(
        f"\n✓ ERê concluido em {elapsed:.0f}s. "
        f"ok={stats['ok']} skip={stats['skip']} fail={stats['fail']}. "
        f"drive_folder={ere_id}"
    )
    if stats["failed_files"]:
        print(f"\nFalhados ({len(stats['failed_files'])}):")
        for ff in stats["failed_files"]:
            print(f"  - {ff}")
    return {"drive_folder_id": ere_id, "elapsed_s": elapsed, **stats}


# ============== Main ==============

async def main():
    if len(sys.argv) < 2:
        print("Uso: python3 scripts/genspark_to_drive_migration.py [hindiana|ere|all]")
        sys.exit(1)
    target = sys.argv[1].lower()
    if target == "hindiana":
        await run_hindiana()
    elif target == "ere":
        await run_ere()
    elif target == "all":
        await run_hindiana()
        await run_ere()
    else:
        print(f"target invalido: {target}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
